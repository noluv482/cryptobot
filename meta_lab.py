#!/usr/bin/env python3
"""META-LABELING on the shadow book — shadow-only, honest floors, purged CV.

The shadow book (shadow_signals) records EVERY signal that reached the entry
gates, taken or not, with the bot's view at that moment. Meta-labeling asks a
narrower question than "is the signal good": GIVEN this signal fired, what is
the probability it nets positive after real costs? A well-calibrated answer
lets a future bot_server scale confidence (or skip) per signal instead of
using one global gate.

TRAINING DATA — resolved shadow rows only:
    label   y = 1 iff  signed(fwd24) - recorded_spread - HONEST_FEES_RT > 0
            signed(): SELL flips the sign of fwd24 (a drop is a SELL win).
            HONEST_FEES_RT default 0.012 = maker entry 0.4% + taker exit 0.8%
            (the bot's base-tier Kraken fees; see bot_server KRAKEN_MAKER_FEE /
            KRAKEN_FEE). The bot's flat 0.1% slippage guess is REPLACED by the
            measured live spread recorded per row — a real number beats a
            constant. Rows missing spread use 0.0 and are counted/disclosed.
    features: regime one-hot (TRENDING/CHOPPY/NEUTRAL), rsi, atr_pct, adx,
            er, spread, conf, hour-of-day as sin/cos (23:00 and 00:00 are
            neighbours, not opposite ends of a line).

MODEL — logistic regression in pure numpy (IRLS with a small ridge term).
sklearn is NOT assumed; numpy itself is checked at import and train() refuses
with a message if it is missing rather than crashing.

VALIDATION — PURGED time-series K-fold with embargo:
    folds are contiguous in time. For each test fold, every training row whose
    label window [ts, ts + LABEL_HORIZON_S] overlaps the test window widened
    by EMBARGO_S (48h) on both sides is DROPPED. Without the purge, a training
    row 1h before the test window shares 23h of its forward return with test
    rows — leakage dressed up as skill. All reported metrics are OUT-OF-FOLD.

STATISTICAL FLOORS (hard, in code — not advice):
    - refuses to train below MIN_ROWS (500) resolved usable rows
    - refuses when the minority class is under MIN_MINORITY_FRAC (5%)
    Refusal prints the reason and writes nothing.

OUTPUT — one row per train run into the meta_lab table (schema created here
via the passed db handle), holding OOF precision/recall/Brier, a 10-bin
calibration table, and the deployable model (coefficients + standardization)
as JSON.

SCORING API (the contract the next bot_server agent wires):
    model = meta_lab.load_latest(conn)          # dict or None
    p     = meta_lab.score_signal(features, model)   # float in (0,1)
    features dict keys: regime (str), rsi, atr_pct, adx, er, spread, conf,
    hour (int 0-23). Missing/None numeric features fall back to the training
    mean (standardized 0) — score_signal never raises on a sparse dict.
    Shadow-scoring only until OOF metrics earn more: log the prob next to the
    signal, change nothing about entries.

CLI (dsn comes from --dsn or the DATABASE_URL env var — never hardcoded):
    python meta_lab.py train  [--dsn postgres://...]
    python meta_lab.py report [--dsn postgres://...]

No writes to any trading table. meta_lab writes only its own meta_lab table.
"""
import json
import math
import os
import sys
import time

try:
    import numpy as np
    HAVE_NUMPY = True
except Exception:                                    # pragma: no cover
    np = None
    HAVE_NUMPY = False

# ── honest constants ─────────────────────────────────────────────────────────
MIN_ROWS          = 500      # refuse to train below this many usable rows
MIN_MINORITY_FRAC = 0.05     # refuse when either class is under 5%
HONEST_FEES_RT    = float(os.environ.get("META_FEES_RT", "0.012"))
LABEL_HORIZON_S   = 24 * 3600    # fwd24 is the label
EMBARGO_S         = 48 * 3600    # >= 48h embargo around every test fold
N_FOLDS           = 5
RIDGE_LAM         = 1e-3
CALIB_BINS        = 10

REGIMES = ("TRENDING", "CHOPPY", "NEUTRAL")
NUMERIC_FEATURES = ("rsi", "atr_pct", "adx", "er", "spread", "conf")
FEATURE_NAMES = tuple(f"regime_{r}" for r in REGIMES) + NUMERIC_FEATURES + (
    "hour_sin", "hour_cos")

SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS meta_lab (
        id SERIAL PRIMARY KEY,
        ts FLOAT,
        n_rows INT, n_used INT, n_spread_missing INT, n_folds INT,
        base_rate FLOAT,
        precision_oof FLOAT, recall_oof FLOAT, brier_oof FLOAT,
        calibration TEXT,
        model TEXT,
        fees_rt FLOAT,
        note TEXT
    )
"""


def ensure_schema(conn):
    """Create the meta_lab table via the passed db handle (boot pattern)."""
    with conn.cursor() as cur:
        cur.execute(SCHEMA_SQL)
    conn.commit()


# ── dataset ──────────────────────────────────────────────────────────────────
FETCH_SQL = """
    SELECT ts, sig, regime, rsi, atr_pct, adx, er, spread, conf, hour, fwd24
    FROM shadow_signals
    WHERE fwd_done=1 AND fwd24 IS NOT NULL AND sig IN ('BUY','SELL')
    ORDER BY ts
"""
FETCH_COLS = ("ts", "sig", "regime", "rsi", "atr_pct", "adx", "er",
              "spread", "conf", "hour", "fwd24")


def fetch_resolved(conn):
    """Resolved BUY/SELL shadow rows as dicts, oldest first."""
    with conn.cursor() as cur:
        cur.execute(FETCH_SQL)
        rows = cur.fetchall()
    return [dict(zip(FETCH_COLS, r)) for r in rows]


def label_row(row, fees_rt=HONEST_FEES_RT):
    """(y, net, spread_missing) — y is 1 iff net of spread+fees positive."""
    f = row.get("fwd24")
    if f is None:
        return None, None, False
    signed = -f if row.get("sig") == "SELL" else f
    spread = row.get("spread")
    missing = spread is None
    net = signed - (0.0 if missing else spread) - fees_rt
    return (1 if net > 0 else 0), net, missing


def featurize(row):
    """Feature vector in FEATURE_NAMES order. None numerics become nan
    (imputed to the train mean during standardization)."""
    regime = row.get("regime") or "NEUTRAL"
    vec = [1.0 if regime == r else 0.0 for r in REGIMES]
    for k in NUMERIC_FEATURES:
        v = row.get(k)
        vec.append(float("nan") if v is None else float(v))
    hour = row.get("hour")
    if hour is None:
        vec += [float("nan"), float("nan")]
    else:
        ang = 2.0 * math.pi * (int(hour) % 24) / 24.0
        vec += [math.sin(ang), math.cos(ang)]
    return vec


def build_dataset(rows, fees_rt=HONEST_FEES_RT):
    """rows(dicts) -> dict with X (list of vecs), y, ts, counts. Pure."""
    X, y, ts = [], [], []
    n_spread_missing = 0
    for r in rows:
        yy, _net, miss = label_row(r, fees_rt)
        if yy is None:
            continue
        n_spread_missing += 1 if miss else 0
        X.append(featurize(r))
        y.append(yy)
        ts.append(float(r["ts"]))
    return {"X": X, "y": y, "ts": ts, "n_spread_missing": n_spread_missing}


# ── floors ───────────────────────────────────────────────────────────────────
def floor_check(y):
    """Return a refusal string, or None if training may proceed."""
    n = len(y)
    if n < MIN_ROWS:
        return (f"REFUSED: {n} resolved rows < floor {MIN_ROWS}. "
                "A model fit on this sample would be noise wearing a suit. "
                "Let the shadow book grow.")
    pos = sum(y)
    minority = min(pos, n - pos) / n
    if minority < MIN_MINORITY_FRAC:
        return (f"REFUSED: minority class {minority*100:.1f}% < "
                f"{MIN_MINORITY_FRAC*100:.0f}% floor ({pos}/{n} positive). "
                "Precision/recall are meaningless at this imbalance.")
    return None


# ── purged time-series CV ────────────────────────────────────────────────────
def purged_splits(ts, n_folds=N_FOLDS, embargo_s=EMBARGO_S,
                  horizon_s=LABEL_HORIZON_S):
    """Yield (train_idx, test_idx) with contiguous-in-time test folds.

    ts must be ascending. A training row i is kept only if its label window
    [ts[i], ts[i]+horizon_s] is clear of the test window widened by embargo_s
    on both sides — i.e. ts[i]+horizon_s < test_start-embargo, or
    ts[i] > test_end+embargo.
    """
    n = len(ts)
    fold = n // n_folds
    for k in range(n_folds):
        lo = k * fold
        hi = n if k == n_folds - 1 else (k + 1) * fold
        if hi <= lo:
            continue
        t0, t1 = ts[lo], ts[hi - 1]
        train = [i for i in range(n)
                 if (ts[i] + horizon_s < t0 - embargo_s) or
                    (ts[i] > t1 + embargo_s)]
        yield train, list(range(lo, hi))


# ── pure-numpy logistic regression (IRLS + ridge) ────────────────────────────
def _standardize_fit(X):
    """Column means/stds from TRAIN data only; nan-aware."""
    mu = np.nanmean(X, axis=0)
    mu = np.where(np.isnan(mu), 0.0, mu)
    sd = np.nanstd(X, axis=0)
    sd = np.where((sd < 1e-12) | np.isnan(sd), 1.0, sd)
    return mu, sd


def _standardize_apply(X, mu, sd):
    Z = (X - mu) / sd
    return np.where(np.isnan(Z), 0.0, Z)   # missing -> train mean


def _fit_logistic(Z, y, lam=RIDGE_LAM, iters=25):
    """IRLS with ridge. Returns weight vector (last element = intercept)."""
    A = np.hstack([Z, np.ones((Z.shape[0], 1))])
    w = np.zeros(A.shape[1])
    reg = lam * np.eye(A.shape[1])
    reg[-1, -1] = 0.0                       # do not shrink the intercept
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-np.clip(A @ w, -35, 35)))
        W = np.maximum(p * (1 - p), 1e-9)
        H = (A * W[:, None]).T @ A + reg * len(y)
        g = A.T @ (y - p) - (reg @ w) * len(y)
        try:
            step = np.linalg.solve(H, g)
        except np.linalg.LinAlgError:       # pragma: no cover
            break
        w = w + step
        if np.max(np.abs(step)) < 1e-8:
            break
    return w


def _predict(Xraw, mu, sd, w):
    Z = _standardize_apply(Xraw, mu, sd)
    A = np.hstack([Z, np.ones((Z.shape[0], 1))])
    return 1.0 / (1.0 + np.exp(-np.clip(A @ w, -35, 35)))


# ── metrics ──────────────────────────────────────────────────────────────────
def precision_recall(y_true, prob, thresh=0.5):
    tp = fp = fn = 0
    for yt, p in zip(y_true, prob):
        pred = 1 if p >= thresh else 0
        if pred and yt:
            tp += 1
        elif pred:
            fp += 1
        elif yt:
            fn += 1
    prec = tp / (tp + fp) if (tp + fp) else None
    rec = tp / (tp + fn) if (tp + fn) else None
    return prec, rec


def brier(y_true, prob):
    if not len(y_true):
        return None
    return float(sum((p - yt) ** 2 for yt, p in zip(y_true, prob)) / len(y_true))


def calibration_table(y_true, prob, bins=CALIB_BINS):
    """[{lo, hi, n, mean_pred, obs_rate}] — the honesty plot in numbers."""
    out = []
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        sel = [(yt, p) for yt, p in zip(y_true, prob)
               if (p >= lo and (p < hi or (b == bins - 1 and p <= hi)))]
        if sel:
            out.append({"lo": lo, "hi": hi, "n": len(sel),
                        "mean_pred": sum(p for _, p in sel) / len(sel),
                        "obs_rate": sum(yt for yt, _ in sel) / len(sel)})
        else:
            out.append({"lo": lo, "hi": hi, "n": 0,
                        "mean_pred": None, "obs_rate": None})
    return out


# ── training ─────────────────────────────────────────────────────────────────
def train_from_rows(rows, fees_rt=HONEST_FEES_RT, n_folds=N_FOLDS):
    """Pure trainer: shadow rows(dicts) -> result dict or refusal.

    Returns {"refused": str} on any floor, else
    {"n_rows", "n_used", "n_spread_missing", "n_folds_run", "base_rate",
     "precision_oof", "recall_oof", "brier_oof", "calibration", "model"}.
    model = {feature_names, mu, sd, w, fees_rt} — everything score_signal
    needs, JSON-serializable.
    """
    if not HAVE_NUMPY:
        return {"refused": "REFUSED: numpy is not installed — "
                           "pip install numpy (see requirements.txt)."}
    ds = build_dataset(rows, fees_rt)
    y, ts = ds["y"], ds["ts"]
    reason = floor_check(y)
    if reason:
        return {"refused": reason, "n_rows": len(y)}
    if any(ts[i] > ts[i + 1] for i in range(len(ts) - 1)):
        order = sorted(range(len(ts)), key=lambda i: ts[i])
        ds["X"] = [ds["X"][i] for i in order]
        y = [y[i] for i in order]
        ts = [ts[i] for i in order]
    X = np.array(ds["X"], dtype=float)
    ya = np.array(y, dtype=float)

    oof_y, oof_p, folds_run = [], [], 0
    for tr, te in purged_splits(ts, n_folds=n_folds):
        if len(tr) < 50 or not te:
            continue                       # a fold with no clean train data
        if len(set(int(v) for v in ya[tr])) < 2:
            continue                       # one-class train fold: unfittable
        mu, sd = _standardize_fit(X[tr])
        w = _fit_logistic(_standardize_apply(X[tr], mu, sd), ya[tr])
        p = _predict(X[te], mu, sd, w)
        oof_y += [int(v) for v in ya[te]]
        oof_p += [float(v) for v in p]
        folds_run += 1
    if folds_run == 0:
        return {"refused": "REFUSED: purging+embargo left no usable folds — "
                           "the sample is too short in TIME even if not in "
                           "rows. Let the shadow book age.", "n_rows": len(y)}

    prec, rec = precision_recall(oof_y, oof_p)
    mu, sd = _standardize_fit(X)
    w = _fit_logistic(_standardize_apply(X, mu, sd), ya)
    model = {"feature_names": list(FEATURE_NAMES),
             "mu": [float(v) for v in mu], "sd": [float(v) for v in sd],
             "w": [float(v) for v in w], "fees_rt": fees_rt}
    return {"n_rows": len(rows), "n_used": len(y),
            "n_spread_missing": ds["n_spread_missing"],
            "n_folds_run": folds_run,
            "base_rate": float(sum(oof_y) / len(oof_y)),
            "precision_oof": prec, "recall_oof": rec,
            "brier_oof": brier(oof_y, oof_p),
            "calibration": calibration_table(oof_y, oof_p),
            "model": model}


def save_result(conn, res):
    """Persist one training run into the meta_lab table."""
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO meta_lab
               (ts, n_rows, n_used, n_spread_missing, n_folds, base_rate,
                precision_oof, recall_oof, brier_oof, calibration, model,
                fees_rt, note)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (time.time(), res["n_rows"], res["n_used"],
             res["n_spread_missing"], res["n_folds_run"], res["base_rate"],
             res["precision_oof"], res["recall_oof"], res["brier_oof"],
             json.dumps(res["calibration"]), json.dumps(res["model"]),
             res["model"]["fees_rt"], "oof metrics; model fit on all rows"))
    conn.commit()


# ── scoring API (the bot_server wiring contract) ─────────────────────────────
def load_latest(conn):
    """Most recent trained model dict from the meta_lab table, or None."""
    with conn.cursor() as cur:
        cur.execute("SELECT model FROM meta_lab WHERE model IS NOT NULL "
                    "ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return None


def score_signal(features, model):
    """P(net-positive | signal fired) for one feature dict. Never raises on a
    sparse dict; returns None if model is None. Pure math, no DB, no numpy
    needed at score time (the hot path must not depend on an import)."""
    if not model:
        return None
    vec = featurize(features)
    z = 0.0
    w = model["w"]
    for i, v in enumerate(vec):
        vv = model["mu"][i] if (v != v) else v          # nan -> train mean
        z += w[i] * (vv - model["mu"][i]) / model["sd"][i]
    z += w[len(vec)]                                     # intercept
    z = max(-35.0, min(35.0, z))
    return 1.0 / (1.0 + math.exp(-z))


# ── CLI ──────────────────────────────────────────────────────────────────────
def _connect(dsn):
    if not dsn:
        print("no dsn: pass --dsn or set DATABASE_URL "
              "(never hardcoded here on purpose)")
        return None
    import psycopg2
    return psycopg2.connect(dsn)


def _cmd_train(conn):
    ensure_schema(conn)
    rows = fetch_resolved(conn)
    res = train_from_rows(rows)
    if "refused" in res:
        print(res["refused"])
        return 3
    save_result(conn, res)
    _print_result(res)
    return 0


def _print_result(res):
    print(f"trained on {res['n_used']} resolved rows "
          f"({res['n_spread_missing']} missing spread, counted as 0.0), "
          f"{res['n_folds_run']} purged folds")
    fmt = lambda v: "  n/a" if v is None else f"{v:.3f}"
    print(f"  base rate      {fmt(res['base_rate'])}")
    print(f"  precision OOF  {fmt(res['precision_oof'])}   "
          f"(vs base rate — below it the model subtracts value)")
    print(f"  recall OOF     {fmt(res['recall_oof'])}")
    print(f"  Brier OOF      {fmt(res['brier_oof'])}   (0.25 = coin flip)")
    print("  calibration (predicted -> observed):")
    for b in res["calibration"]:
        if b["n"]:
            print(f"    [{b['lo']:.1f},{b['hi']:.1f})  n={b['n']:>5d}  "
                  f"pred {b['mean_pred']:.2f}  obs {b['obs_rate']:.2f}")


def _cmd_report(conn):
    ensure_schema(conn)
    with conn.cursor() as cur:
        cur.execute("""SELECT ts, n_used, n_folds, base_rate, precision_oof,
                              recall_oof, brier_oof
                       FROM meta_lab ORDER BY id DESC LIMIT 10""")
        runs = cur.fetchall()
    if not runs:
        print("no training runs recorded yet — python meta_lab.py train")
        return 0
    print(f"{'when':>19s} {'n':>6s} {'folds':>5s} {'base':>6s} "
          f"{'prec':>6s} {'rec':>6s} {'brier':>6s}")
    for ts, n, k, base, prec, rec, br in runs:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
        f = lambda v: "   n/a" if v is None else f"{v:6.3f}"
        print(f"{when:>19s} {n:>6d} {k:>5d} {f(base)} {f(prec)} {f(rec)} {f(br)}")
    return 0


def main(argv):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = list(argv)
    dsn = os.environ.get("DATABASE_URL")
    if "--dsn" in args:
        i = args.index("--dsn")
        dsn = args[i + 1]
        del args[i:i + 2]
    cmd = args[0] if args else "report"
    if cmd not in ("train", "report"):
        print(__doc__.split("CLI", 1)[1])
        return 2
    conn = _connect(dsn)
    if conn is None:
        return 2
    try:
        return _cmd_train(conn) if cmd == "train" else _cmd_report(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
