#!/usr/bin/env python3
"""Learning-module contracts: meta_lab, learning_report, sizing.

All mocked — no live Postgres, no Kraken, no bot_server import. What must
hold:

  meta_lab — the statistical floors REFUSE (500 rows, 5% minority) instead
  of fitting noise; the purged splits leak nothing (no train label window
  inside test+embargo); on a synthetic learnable sample the OOF metrics find
  the signal; score_signal is pure math that matches the trained model and
  survives sparse dicts; labels are net of spread AND fees with SELL flipped.

  learning_report — Wilson math is right; verdict vocabulary is honest
  ('n too small' below floor, 'candidate (rig)' only when the LOWER bound
  clears 50% with positive mean); the spread map enforces its n>=100 floor
  by OMISSION and emits the exact JSON contract the bot will read.

  sizing — every degenerate input (n=0, zero vol, negative equity, no stop)
  sizes ZERO without raising; Kelly is shrunk by 1/sqrt(n) and capped; the
  15% drawdown circuit opens; the audit dict names every term and is
  JSON-safe.
"""
import json
import math
import os
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):   # Windows consoles default to cp1252
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import meta_lab
import learning_report as lr
import sizing

FAILS = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name
          + (f"   [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def approx(a, b, tol=1e-9):
    return a is not None and b is not None and abs(a - b) <= tol


# ── fake DB (context-manager cursor, SQL-dispatch results) ───────────────────
class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append((sql, params))
        self._rows = []
        for key, rows in self.conn.results.items():
            if key in sql:
                self._rows = rows
                break

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    def __init__(self, results=None):
        self.results = results or {}   # SQL-substring -> rows
        self.executed = []
        self.commits = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def close(self):
        pass


# ═════════════════════════════════════ meta_lab ══════════════════════════════
print("── meta_lab ──")


def mk_row(i, n_total=600, learnable=True):
    """Synthetic shadow row: rsi drives the outcome when learnable."""
    rsi = (i * 37) % 100
    sig = "BUY" if i % 2 == 0 else "SELL"
    up = rsi > 50 if learnable else (i % 3 == 0)
    move = 0.05 if up else -0.05
    fwd24 = move if sig == "BUY" else -move   # signed() recovers +/-0.05
    return {"ts": 1700000000.0 + i * 43200.0, "sig": sig,
            "regime": ("TRENDING", "CHOPPY", "NEUTRAL")[i % 3],
            "rsi": float(rsi), "atr_pct": 1.0 + (i % 7) * 0.1,
            "adx": 20.0 + (i % 5), "er": 0.3 + (i % 4) * 0.1,
            "spread": 0.001, "conf": 50.0 + (i % 10), "hour": i % 24,
            "fwd24": fwd24}


# labels: net of spread and fees, SELL flipped
y, net, miss = meta_lab.label_row(
    {"sig": "BUY", "fwd24": 0.05, "spread": 0.002}, fees_rt=0.012)
check("label BUY: net = fwd24 - spread - fees", approx(net, 0.036) and y == 1)
y, net, _ = meta_lab.label_row(
    {"sig": "SELL", "fwd24": -0.05, "spread": 0.002}, fees_rt=0.012)
check("label SELL: a drop is a win, sign flipped", approx(net, 0.036) and y == 1)
y, net, _ = meta_lab.label_row(
    {"sig": "BUY", "fwd24": 0.010, "spread": 0.002}, fees_rt=0.012)
check("label: gross-positive but net-negative labels 0",
      y == 0 and approx(net, -0.004))
y, net, miss = meta_lab.label_row({"sig": "BUY", "fwd24": 0.05, "spread": None})
check("missing spread counted 0.0 and flagged", miss and y == 1)

# floors refuse instead of fitting noise
res = meta_lab.train_from_rows([mk_row(i) for i in range(499)])
check("floor: refuses below 500 resolved rows",
      "refused" in res and "500" in res["refused"], str(res)[:80])
rows_imb = [mk_row(i) for i in range(600)]
for r in rows_imb:                                   # force ~2% positive
    r["fwd24"] = -0.05 if r["sig"] == "BUY" else 0.05
for r in rows_imb[:12]:
    r["fwd24"] = 0.05 if r["sig"] == "BUY" else -0.05
res = meta_lab.train_from_rows(rows_imb)
check("floor: refuses when minority class < 5%",
      "refused" in res and "minority" in res["refused"].lower(), str(res)[:80])

# purged splits leak nothing
ts = [1700000000.0 + i * 43200.0 for i in range(600)]
leaks, folds = 0, 0
for tr, te in meta_lab.purged_splits(ts):
    folds += 1
    t0, t1 = ts[te[0]], ts[te[-1]]
    for i in tr:
        inside = not ((ts[i] + meta_lab.LABEL_HORIZON_S <
                       t0 - meta_lab.EMBARGO_S) or
                      (ts[i] > t1 + meta_lab.EMBARGO_S))
        leaks += 1 if inside else 0
check("purged splits: no train label window touches test+embargo",
      folds == meta_lab.N_FOLDS and leaks == 0, f"folds={folds} leaks={leaks}")

# on a learnable sample the model finds the signal, out of fold
rows = [mk_row(i) for i in range(600)]
res = meta_lab.train_from_rows(rows)
check("train succeeds on 600 learnable rows", "refused" not in res, str(res)[:90])
if "refused" not in res:
    check("OOF precision beats base rate on a real signal",
          res["precision_oof"] is not None
          and res["precision_oof"] > res["base_rate"] + 0.1,
          f"prec={res['precision_oof']} base={res['base_rate']}")
    check("OOF brier beats a coin flip", res["brier_oof"] < 0.25,
          str(res["brier_oof"]))
    check("calibration table has bins with counts",
          len(res["calibration"]) == meta_lab.CALIB_BINS
          and sum(b["n"] for b in res["calibration"]) == len(rows))
    model = res["model"]
    check("model is JSON-serializable and complete",
          json.loads(json.dumps(model))["feature_names"]
          == list(meta_lab.FEATURE_NAMES))

    # score_signal: pure-math scoring matches the numpy path
    import numpy as np
    feats = {"regime": "TRENDING", "rsi": 80.0, "atr_pct": 1.2, "adx": 22.0,
             "er": 0.5, "spread": 0.001, "conf": 55.0, "hour": 3}
    p_pure = meta_lab.score_signal(feats, model)
    p_np = float(meta_lab._predict(
        np.array([meta_lab.featurize(feats)]),
        np.array(model["mu"]), np.array(model["sd"]),
        np.array(model["w"]))[0])
    check("score_signal matches the trained model's prediction",
          approx(p_pure, p_np, 1e-9), f"{p_pure} vs {p_np}")
    check("score_signal: high-rsi signal scores high, low scores low",
          p_pure > 0.5 > meta_lab.score_signal(dict(feats, rsi=10.0), model),
          f"hi={p_pure}")
    p_sparse = meta_lab.score_signal({"regime": None, "hour": None}, model)
    check("score_signal survives a sparse dict", 0.0 < p_sparse < 1.0)
    check("score_signal with no model returns None",
          meta_lab.score_signal(feats, None) is None)

# numpy-missing degrade path
_saved = meta_lab.HAVE_NUMPY
meta_lab.HAVE_NUMPY = False
res_nonp = meta_lab.train_from_rows(rows)
meta_lab.HAVE_NUMPY = _saved
check("degrades with a message when numpy is missing",
      "refused" in res_nonp and "numpy" in res_nonp["refused"])

# DB plumbing: schema via handle, save writes its own table only, load roundtrip
conn = FakeConn()
meta_lab.ensure_schema(conn)
check("ensure_schema creates meta_lab via the passed handle",
      any("CREATE TABLE IF NOT EXISTS meta_lab" in s for s, _ in conn.executed)
      and conn.commits == 1)
if "refused" not in res:
    conn = FakeConn()
    meta_lab.save_result(conn, res)
    ins = [(s, p) for s, p in conn.executed if "INSERT INTO meta_lab" in s]
    check("save_result inserts one meta_lab row", len(ins) == 1 and conn.commits == 1)
    check("save_result touches NO trading table",
          all("shadow_signals" not in s and "trades" not in s
              and "manual_lab" not in s for s, _ in conn.executed))
    conn2 = FakeConn({"SELECT model FROM meta_lab": [(json.dumps(res["model"]),)]})
    m2 = meta_lab.load_latest(conn2)
    check("load_latest roundtrips the model", m2 == res["model"])
check("load_latest with empty table returns None",
      meta_lab.load_latest(FakeConn()) is None)

# fetch SQL is read-only against shadow_signals
check("fetch SQL reads shadow_signals only (no UPDATE/INSERT/DELETE)",
      "SELECT" in meta_lab.FETCH_SQL
      and not any(k in meta_lab.FETCH_SQL.upper()
                  for k in ("UPDATE", "INSERT", "DELETE", "DROP")))

# ═════════════════════════════ learning_report ═══════════════════════════════
print()
print("── learning_report ──")

lo, hi = lr.wilson(8, 10)
check("wilson 8/10 brackets the point estimate", lo < 0.8 < hi
      and approx(lo, 0.4901, 5e-3) and approx(hi, 0.9433, 5e-3),
      f"[{lo},{hi}]")
lo, hi = lr.wilson(0, 0)
check("wilson n=0 returns None, not a number", lo is None and hi is None)
check("percentile: median and p75 of 1..5",
      approx(lr.percentile([5, 1, 3, 2, 4], 0.5), 3.0)
      and approx(lr.percentile([5, 1, 3, 2, 4], 0.75), 4.0))
check("net_fwd24 flips SELL",
      approx(lr.net_fwd24("SELL", -0.05, 0.002, 0.012), 0.036))

# regime verdicts
def rrow(regime, sig, fwd24, spread=0.001):
    return {"regime": regime, "sig": sig, "fwd24": fwd24, "spread": spread}

rows = ([rrow("TRENDING", "BUY", 0.05)] * 40         # 40 clean wins
        + [rrow("TRENDING", "BUY", -0.05)] * 5       # 5 losses -> 89% wins
        + [rrow("CHOPPY", "BUY", 0.05)] * 10         # n=10 < floor
        + [rrow("NEUTRAL", "SELL", 0.05)] * 35)      # SELL vs rise: losers
t = {(r["regime"], r["sig"]): r for r in lr.regime_table(rows)}
tr = t[("TRENDING", "BUY")]
check("regime: strong sample -> 'candidate (rig)'",
      tr["verdict"] == "candidate (rig)" and tr["n"] == 45
      and tr["wilson_lo"] > 0.5, str(tr))
check("regime: mean net is net of spread+fees",
      approx(tr["mean_net"], (40 * 0.037 + 5 * -0.063) / 45, 1e-9),
      str(tr["mean_net"]))
check("regime: below floor -> 'n too small'",
      t[("CHOPPY", "BUY")]["verdict"] == "n too small")
check("regime: losing side -> 'no edge shown'",
      t[("NEUTRAL", "SELL")]["verdict"] == "no edge shown"
      and t[("NEUTRAL", "SELL")]["mean_net"] < 0)

grows = [dict(rrow("X", "BUY", -0.05), taken=0, rejected_by="spread")] * 31 \
      + [dict(rrow("X", "BUY", 0.05), taken=1, rejected_by="")] * 31
gt = {r["group"]: r for r in lr.gate_table(grows)}
check("gates: TAKEN and rejected_by grouped separately",
      set(gt) == {"TAKEN", "spread"} and gt["spread"]["mean_net"] < 0
      < gt["TAKEN"]["mean_net"])

# spread map floor by omission + exact contract
srows = ([{"pair": "SOLUSD", "hour": 3, "spread": 0.001 + i * 1e-5}
          for i in range(100)]                        # exactly at floor
         + [{"pair": "SOLUSD", "hour": 4, "spread": 0.002}] * 99  # under it
         + [{"pair": "XBTUSD", "hour": 3, "spread": None}] * 200)  # no spread
smap = lr.spread_map(srows)
check("spread map: n=100 cell kept, n=99 cell OMITTED",
      "3" in smap.get("SOLUSD", {}) and "4" not in smap.get("SOLUSD", {}))
check("spread map: null spreads never form a cell", "XBTUSD" not in smap)
cell = smap["SOLUSD"]["3"]
check("spread map cell carries median_pct/p75_pct/n as fractions",
      set(cell) == {"median_pct", "p75_pct", "n"} and cell["n"] == 100
      and 0.001 < cell["median_pct"] < 0.002
      and cell["p75_pct"] > cell["median_pct"])

with tempfile.TemporaryDirectory() as td:
    path = os.path.join(td, "spread_hours.json")
    lr.write_spread_json(smap, path)
    doc = json.load(open(path, encoding="utf-8"))
    check("spread_hours.json matches the bot contract exactly",
          doc["SOLUSD"]["3"]["n"] == 100 and "_meta" in doc
          and doc["_meta"]["min_n"] == 100 and "units" in doc["_meta"])

    # end-to-end report against the fake DB — read-only, returns 0
    conn = FakeConn({
        "fwd_done=1": [("TRENDING", "BUY", 0.05, 0.001, 1, "")] * 35,
        "spread IS NOT NULL": [("SOLUSD", 3, 0.001)] * 120,
    })
    rc = lr.run_report(conn, out_path=path)
    check("run_report runs on mocked DB and returns 0", rc == 0)
    check("run_report never writes to the DB",
          all(sql.strip().upper().startswith("SELECT")
              for sql, _ in conn.executed) and conn.commits == 0,
          str([s[:30] for s, _ in conn.executed]))
    doc = json.load(open(path, encoding="utf-8"))
    check("run_report regenerates the json from live rows",
          doc.get("SOLUSD", {}).get("3", {}).get("n") == 120)

# ═════════════════════════════════ sizing ════════════════════════════════════
print()
print("── sizing ──")

k = sizing.kelly_fraction(60, 40, 0.02, 0.01)
check("kelly: 60/40 b=2 raw 0.40, shrunk x0.9 = 0.36, capped at 0.25",
      approx(k, 0.25), str(k))
k = sizing.kelly_fraction(6, 4, 0.015, 0.01)
check("kelly: small n shrinks harder (n=10 keeps 68% of raw 1/3)",
      approx(k, (0.6 - 0.4 / 1.5) * (1 - 1 / math.sqrt(10)), 1e-9), str(k))
check("kelly: n=0 sizes zero", sizing.kelly_fraction(0, 0, 0.02, 0.01) == 0.0)
check("kelly: negative expectancy sizes zero",
      sizing.kelly_fraction(30, 70, 0.01, 0.01) == 0.0)
check("kelly: zero avg_loss refuses (no div-by-zero)",
      sizing.kelly_fraction(60, 40, 0.02, 0.0) == 0.0)
check("kelly: None inputs size zero",
      sizing.kelly_fraction(None, 5, 0.02, 0.01) == 0.0
      and sizing.kelly_fraction(60, 40, None, 0.01) == 0.0)

check("vol: 2% target on 4% realized halves",
      approx(sizing.vol_target_scalar(0.04, 0.02), 0.5))
check("vol: scaling up is capped at 2x",
      approx(sizing.vol_target_scalar(0.005, 0.02), 2.0))
check("vol: zero/None vol refuses to size",
      sizing.vol_target_scalar(0.0, 0.02) == 0.0
      and sizing.vol_target_scalar(None, 0.02) == 0.0
      and sizing.vol_target_scalar(0.02, 0.0) == 0.0)

c = sizing.risk_caps(10000, 10000)
check("caps: at HWM the circuit is closed, full base risk",
      not c["dd_circuit_open"] and approx(c["per_trade_risk_pct"], 0.01)
      and c["drawdown"] == 0.0)
c = sizing.risk_caps(9250, 10000)
check("caps: 7.5% dd tapers risk to half",
      approx(c["per_trade_risk_pct"], 0.005) and not c["dd_circuit_open"])
c = sizing.risk_caps(8500, 10000)
check("caps: 15% dd OPENS the circuit",
      c["dd_circuit_open"] and c["per_trade_risk_pct"] == 0.0)
c = sizing.risk_caps(-50, 10000)
check("caps: negative equity opens the circuit, drawdown honest None",
      c["dd_circuit_open"] and c["drawdown"] is None)
check("caps: hwm=0 opens the circuit",
      sizing.risk_caps(100, 0)["dd_circuit_open"])

healthy = dict(equity=10000, hwm=10000, wins=60, losses=40,
               avg_win=0.02, avg_loss=0.01, realized_vol_20d=0.02,
               target_vol=0.02, price=100.0, stop_pct=0.02)
a = sizing.size_position(**healthy)
check("size: healthy inputs -> risk capped at 1%, $100 risk, $5000 notional",
      approx(a["risk_fraction"], 0.01) and approx(a["risk_dollars"], 100.0)
      and approx(a["notional"], 5000.0) and approx(a["size_units"], 50.0),
      str({k: a[k] for k in ("risk_fraction", "risk_dollars", "notional")}))
NAMED = {"equity", "hwm", "wins", "losses", "avg_win", "avg_loss",
         "realized_vol_20d", "target_vol", "price", "stop_pct", "drawdown",
         "dd_circuit_open", "per_trade_risk_cap", "kelly_shrunk",
         "vol_scalar", "risk_fraction", "risk_dollars", "notional",
         "size_units", "reasons"}
check("size: audit names every term", set(a) == NAMED,
      str(sorted(set(a) ^ NAMED)))
check("size: audit is JSON-safe", json.loads(json.dumps(a))["size_units"] == 50.0)

a = sizing.size_position(**dict(healthy, equity=8400))
check("size: drawdown circuit zeroes the size with a reason",
      a["size_units"] == 0.0 and a["dd_circuit_open"]
      and any("dd_circuit_open" in r for r in a["reasons"]))
a = sizing.size_position(**dict(healthy, realized_vol_20d=0.0))
check("size: unmeasured vol zeroes the size with a reason",
      a["size_units"] == 0.0 and any("vol_scalar=0" in r for r in a["reasons"]))
a = sizing.size_position(**dict(healthy, stop_pct=0.0))
check("size: no stop distance -> no size, says so",
      a["size_units"] == 0.0 and any("stop_pct" in r for r in a["reasons"]))
a = sizing.size_position(**dict(healthy, wins=0, losses=0))
check("size: zero trade history -> kelly 0 -> size 0",
      a["size_units"] == 0.0 and a["kelly_shrunk"] == 0.0)
a = sizing.size_position(**dict(healthy, price=0.0))
check("size: zero price cannot convert to units, says so",
      a["size_units"] == 0.0 and any("price" in r for r in a["reasons"]))
try:
    a = sizing.size_position(equity=None, hwm=None, wins=None, losses=None,
                             avg_win=None, avg_loss=None,
                             realized_vol_20d=None, target_vol=None,
                             price=None, stop_pct=None)
    ok = a["size_units"] == 0.0 and a["dd_circuit_open"]
except Exception as e:
    ok = False
check("size: all-None inputs never raise, size 0", ok)
a = sizing.size_position(**dict(healthy, realized_vol_20d=0.005))
check("size: vol scaling may never exceed the per-trade cap",
      a["risk_fraction"] <= a["per_trade_risk_cap"] + 1e-12)

print()
if FAILS:
    print(f"{len(FAILS)} FAILURES")
    sys.exit(1)
print("all learning-module contract checks pass")
