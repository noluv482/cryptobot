#!/usr/bin/env python3
"""research_loop — PURE research-side helpers for the hypothesis tournament.

Importable on the PC (research pass) AND in-container (autopilot.py). It
imports NOTHING from bot_server/autopilot, touches no exchange, no DB, no
file: every function here is a deterministic transform over the numbers the
caller hands it. There is no order-placing symbol anywhere in this module.

What lives here
---------------
  budget_remaining      how many MORE families the tournament can honestly
                        admit before the N-trials hurdle SR0(N) climbs so
                        close to a plausible per-decision SR (0.17) that 24
                        months of decisions could not tell them apart.
  cluster_streams       greedy correlation clustering of per-decision NET
                        return streams (rho > 0.7 on >= 8 shared decisions)
                        -> N_eff for the DSR hurdle.
  family_posteriors     Beta(s, f) evidence per family from the graveyard
                        (kill = f) and the survivors (alive >= 4 weeks = s).
  pick_families         Thompson sampling over those posteriors (B picks,
                        seeded, novelty bonus for never-tried families).
  family_templates      the deterministic no-LLM fallback: parameterized cf
                        specs per family that the RECORDED stream can
                        express. Every template passes
                        autopilot.sanitize_hypotheses (pinned by test).

Data honesty: the posterior/picker output is a *sampling decision*, not a
measurement; the reason strings say so. MinTRL month estimates in the
templates are formula outputs from stated inputs, labeled "estimate".
"""

import math
import random
import re
import statistics
import time

# ── Contract constants ([H]/[G]/[R] in the workstream brief) ──────────────────
FAMILIES = ("trend", "carry", "reversion_pattern", "exit_rule", "regime_gate",
            "cross_section", "lead_lag")
# The built-in carry_or_trend allocator scores the SWITCH itself; hypotheses
# may not submit this family (it is not in FAMILIES) but the goal block lists it.
BUILTIN_EXTRA_FAMILIES = ("switch",)
KINDS = ("price", "trend", "carry", "switch")
# "template" = this module's deterministic fallback. Labeled honestly instead
# of masquerading as an LLM pre-registration.
ORIGINS = ("llm_prereg", "owner_idea", "human", "template")
HYP_MAX_SLOTS = 5
HYP_ID_RE = re.compile(r"^hyp_[a-z0-9_]{1,24}\Z")   # \Z, never $ (trailing-newline lookalikes)
PREREG_KEYS = ("mechanism", "expected_decisions_per_month",
               "mintrl_estimate_months", "kill_bar", "cost_model")

PLAUSIBLE_SR = 0.17       # per-decision SR a *plausible* real edge might carry
RESOLVE_Z = 1.645         # one-sided 95% resolution threshold
BUDGET_MONTHS = 24
SECS_PER_WEEK = 7 * 86400
ALIVE_WEEKS = 4           # a survivor counts as a success only past this age
CLUSTER_RHO = 0.7
CLUSTER_MIN_OVERLAP = 8

# Decision cadence per family (decisions/year) — the DEFAULT when a family has
# no live member to measure. Weekly rules = 52, daily marks/signals = 365.
FAMILY_CADENCE_DEFAULT = {
    "trend": 52, "carry": 365, "reversion_pattern": 365, "exit_rule": 365,
    "regime_gate": 365, "cross_section": 52, "lead_lag": 365, "switch": 52,
}


def kind_cadence(kind, cf=None):
    """decisions/year implied by an entrant's kind (+ weekly flag for price)."""
    cf = cf or {}
    if kind in ("trend", "switch"):
        return 52
    if kind == "carry":
        return 365
    return 52 if cf.get("weekly") else 365


# ── Normal quantile + expected max SR (mirrors autopilot.py; pinned by test) ──
_EM_GAMMA = 0.5772156649015329


def _norm_ppf(p):
    if not (0.0 < p < 1.0):
        raise ValueError(f"ppf domain: {p}")
    a = (-3.969683028665376e+01,  2.209460984245205e+02, -2.759285104469687e+02,
          1.383577518672690e+02, -3.066479806614716e+01,  2.506628277459239e+00)
    b = (-5.447609879822406e+01,  1.615858368580409e+02, -1.556989798598866e+02,
          6.680131188771972e+01, -1.328068155288572e+01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00,  4.374664141464968e+00,  2.938163982698783e+00)
    d = ( 7.784695709041462e-03,  3.224671290700398e-01,  2.445134137142996e+00,
          3.754408661907416e+00)
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
           (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def expected_max_sr(var_sr, n_trials):
    """E[max SR] of n_trials unskilled tries (the DSR hurdle). None if unknowable.

    var_sr == 0 is refused, not answered. Algebraically E[max] of a zero-variance
    field IS 0, but this estimator sees two to four entrants: a measured zero is
    overwhelmingly "not enough dispersion to see" rather than "no dispersion
    exists", and the two answers are opposites downstream. Returning 0.0 set the
    hurdle to zero -- which quietly turns the Deflated Sharpe into a plain t-test
    against zero while still being reported as a DSR -- and made
    budget_remaining(0.0, 24, 52) return 500, i.e. the intake gate wide open.
    None says "unknowable", which every caller already handles.
    """
    if var_sr is None or var_sr <= 0 or n_trials is None or n_trials < 2:
        return None
    sd = math.sqrt(var_sr)
    return sd * ((1 - _EM_GAMMA) * _norm_ppf(1 - 1.0 / n_trials)
                 + _EM_GAMMA * _norm_ppf(1 - 1.0 / (n_trials * math.e)))


# ── Budget ────────────────────────────────────────────────────────────────────
def resolution_threshold(expected_decisions_per_year, months=BUDGET_MONTHS, z=RESOLVE_Z):
    """z / sqrt(decisions in `months`) — the SR gap `months` of decisions can resolve."""
    try:
        d = float(expected_decisions_per_year) * months / 12.0
    except Exception:
        return None
    if d <= 0:
        return None
    return z / math.sqrt(d)


def budget_remaining(var_sr, trials_count, expected_decisions_per_year,
                     months=BUDGET_MONTHS, plausible_sr=PLAUSIBLE_SR, z=RESOLVE_Z,
                     max_k=500):
    """Additional families registrable before the hurdle out-climbs resolution.

    Counts k = 1, 2, ... while  plausible_sr - SR0(trials_count + k)  >=
    z / sqrt(decisions in `months`). SIGNED, not |.|: the brief writes the
    gap as an absolute value, but the far side (SR0 above the plausible SR)
    would "resolve" only by proving the entrant WORSE than luck — admitting
    on that basis would loosen the bar, so it is refused here. Monotone
    non-increasing in trials_count, non-decreasing in decisions/year.
    Returns None (unknown) when var_sr is unknown — never a guess.
    """
    thr = resolution_threshold(expected_decisions_per_year, months, z)
    # var_sr <= 0, not < 0: a measured zero is unknowable at these sample
    # sizes (see expected_max_sr), and 'unknowable' must propagate as None.
    # Falling through instead made the loop break at k=0 and report the
    # budget EXHAUSTED -- the opposite conclusion from the same evidence.
    if thr is None or var_sr is None or var_sr <= 0 or trials_count is None:
        return None
    base = max(int(trials_count), 1)
    k = 0
    while k < max_k:
        sr0 = expected_max_sr(var_sr, base + k + 1)
        if sr0 is None or plausible_sr - sr0 < thr:
            break
        k += 1
    return k


def registration_check(var_sr, trials_count, expected_decisions_per_year,
                       months=BUDGET_MONTHS):
    """Budget wrapper for an intake decision.

    {"allowed": bool, "budget_remaining": k|None, "sr0": float|None,
     "threshold": float|None, "reason": str}. Unknown budget (no cross-
    entrant variance yet) ADMITS with reason 'budget unknown' — admission
    loosens nothing, the DSR gate still grades the entrant; a budget of 0
    REFUSES.
    """
    k = budget_remaining(var_sr, trials_count, expected_decisions_per_year, months)
    n = max(int(trials_count or 0), 2)
    sr0 = expected_max_sr(var_sr, n) if var_sr is not None else None
    thr = resolution_threshold(expected_decisions_per_year, months)
    if k is None:
        return {"allowed": True, "budget_remaining": None, "sr0": sr0, "threshold": thr,
                "reason": "budget unknown (no cross-entrant SR variance yet) — admitted, DSR gate still applies"}
    if k <= 0:
        nxt = expected_max_sr(var_sr, n + 1)
        # nxt can be None (unknowable variance); formatting it crashed the
        # intake path outright, so the refusal could not even be reported.
        nxt_txt = f"{nxt:.4f}" if nxt is not None else "unknown"
        thr_txt = f"{thr:.4f}" if thr is not None else "unknown"
        return {"allowed": False, "budget_remaining": 0, "sr0": sr0, "threshold": thr,
                "reason": (f"budget exhausted: SR0({n + 1})={nxt_txt} leaves "
                           f"< {thr_txt} of resolvable gap to a plausible SR {plausible_sr_text()}")}
    return {"allowed": True, "budget_remaining": k, "sr0": sr0, "threshold": thr,
            "reason": f"budget ok: {k} more registrable"}


def plausible_sr_text():
    return f"{PLAUSIBLE_SR:.2f}/decision"


# ── N_eff clustering ──────────────────────────────────────────────────────────
def pearson(xs, ys):
    n = len(xs)
    if n < 2 or n != len(ys):
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx < 1e-18 or syy < 1e-18:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / math.sqrt(sxx * syy)


def stream_correlation(a, b, min_overlap=CLUSTER_MIN_OVERLAP):
    """Pearson rho of two {decision_ts: net} streams on their SHARED decisions,
    or None when fewer than min_overlap decisions overlap (or sd=0)."""
    keys = [k for k in a if k in b]
    if len(keys) < min_overlap:
        return None
    return pearson([a[k] for k in keys], [b[k] for k in keys])


def cluster_streams(streams, rho=CLUSTER_RHO, min_overlap=CLUSTER_MIN_OVERLAP):
    """Greedy single-linkage clustering of {id: {ts: net}} streams.

    Ids are visited in insertion order; each joins the FIRST existing cluster
    holding any member it correlates with above `rho` on >= min_overlap
    shared decisions, else opens its own. Streams with too little overlap
    with everyone stay singletons (their redundancy is unknown, so they are
    counted as full trials). Deterministic. Returns a list of id lists.
    """
    clusters = []
    for cid, s in streams.items():
        placed = False
        for cl in clusters:
            for other in cl:
                r = stream_correlation(s, streams[other], min_overlap)
                if r is not None and r > rho:
                    cl.append(cid)
                    placed = True
                    break
            if placed:
                break
        if not placed:
            clusters.append([cid])
    return clusters


def n_eff_from_clusters(trials_count, n_scored, n_clusters):
    """N_eff = trials_count minus the redundancy the clustering DEMONSTRATED.

    Every trial in the monotone ledger (dead, retired, seeded ghosts) keeps
    counting as 1; only currently-scored entrants shown to be near-copies of
    a sibling collapse into one. Never below n_clusters, never below 2.
    """
    n_eff = int(trials_count) - (int(n_scored) - int(n_clusters))
    return max(2, n_eff, int(n_clusters))


# ── Family posteriors + Thompson picker ───────────────────────────────────────
def family_posteriors(graveyard, alive, now=None, window_weeks=None,
                      alive_weeks=ALIVE_WEEKS, families=None):
    """{family: {"s", "f", "trials", "alive": [ids], "killed": [ids]}}.

    graveyard: iterable of {id, family, killed_ts}; alive: iterable of
    {id, family, born_ts}. A kill is one failure (f); a survivor older than
    alive_weeks is one success (s); a younger survivor counts as a trial
    with no verdict. window_weeks (optional) drops kills older than the
    window. Unknown families are still reported (data, not a crash).
    """
    now = time.time() if now is None else float(now)
    fams = list(families) if families else list(FAMILIES) + list(BUILTIN_EXTRA_FAMILIES)
    out = {f: {"s": 0, "f": 0, "trials": 0, "alive": [], "killed": []} for f in fams}

    def slot(fam):
        fam = str(fam or "unknown")
        if fam not in out:
            out[fam] = {"s": 0, "f": 0, "trials": 0, "alive": [], "killed": []}
        return out[fam]

    for g in graveyard or []:
        try:
            kts = float(g.get("killed_ts") or g.get("ts") or 0)
        except Exception:
            kts = 0.0
        if window_weeks is not None and kts and now - kts > window_weeks * SECS_PER_WEEK:
            continue
        p = slot(g.get("family"))
        p["f"] += 1
        p["trials"] += 1
        p["killed"].append(g.get("id"))
    for a in alive or []:
        p = slot(a.get("family"))
        p["trials"] += 1
        p["alive"].append(a.get("id"))
        try:
            born = float(a.get("born_ts") or 0)
        except Exception:
            born = 0.0
        if born and now - born >= alive_weeks * SECS_PER_WEEK:
            p["s"] += 1
    return out


def pick_families(posteriors, B=2, seed=None, novelty_bonus=0.25, window_weeks=26):
    """Thompson sampling over Beta(s+1, f+1) per family; top-B by sampled theta.

    Never-tried families (trials == 0) get +novelty_bonus on their draw.
    `seed` makes the draw reproducible. window_weeks is the recency window
    the caller used to build `posteriors` (recorded in the reason string so
    the pick is auditable). Returns [{family, theta, reason}] with len <= B.
    """
    rng = random.Random(seed)
    draws = []
    for fam in sorted(posteriors):            # sorted: seed -> same order -> same draw
        p = posteriors[fam] or {}
        s, f, t = int(p.get("s", 0)), int(p.get("f", 0)), int(p.get("trials", 0))
        theta = rng.betavariate(s + 1, f + 1)
        bonus = novelty_bonus if t == 0 else 0.0
        theta += bonus
        draws.append((theta, fam, s, f, t, bonus))
    draws.sort(key=lambda x: (-x[0], x[1]))
    picks = []
    for theta, fam, s, f, t, bonus in draws[:max(0, int(B))]:
        picks.append({
            "family": fam, "theta": round(theta, 6),
            "reason": (f"Thompson draw Beta({s+1},{f+1}) over {window_weeks}w window"
                       + (f" + novelty bonus {bonus}" if bonus else "")
                       + f" (s={s}, f={f}, trials={t}); sampled, not measured"),
        })
    return picks


def plan_research_pass(graveyard, alive, B=2, seed=None, now=None, window_weeks=26):
    """Convenience: posteriors -> picks in one call (what the PC pass runs)."""
    post = family_posteriors(graveyard, alive, now=now, window_weeks=window_weeks)
    return post, pick_families(post, B=B, seed=seed, window_weeks=window_weeks)


# ── Deterministic no-LLM templates (must pass autopilot.sanitize_hypotheses) ──
KILL_BAR_TEXT = ("deflated PSR (DSR vs SR0 hurdle) < 0.20 once n >= MinTRL — "
                 "autopilot KILL_PSR, never loosened")
COST_TEXT = {
    "price": ("ROUND_TRIP_COST_PCT charged on every counterfactual fill; "
              "fixed-horizon exit; non-overlapping per pair"),
    "trend": ("spot taker+slippage round trip (CF_SPOT_RT), half charged on the "
              "entry week and half on the exit week"),
    "carry": ("4-leg round trip (2 spot + 2 perp legs, taker+slippage, "
              "CARRY_RT_4LEG), half at entry and half at exit, marked daily"),
}


def mintrl_months_estimate(decisions_per_month, gap=0.10, conf_z=1.645):
    """MinTRL (decisions) for an SR gap `gap` under normal returns, in months.
    Formula: (1 + (z/gap)^2) / decisions_per_month. An ESTIMATE from stated
    inputs, not a measurement."""
    if not decisions_per_month or decisions_per_month <= 0:
        return None
    return round((1.0 + (conf_z / gap) ** 2) / float(decisions_per_month), 1)


def _entry(kind, family, cf, mechanism, dpm):
    return {
        "kind": kind, "family": family, "cf": dict(cf), "origin": "template",
        "note": "HYPOTHESIS: " + mechanism,
        "prereg": {
            "mechanism": mechanism,
            "expected_decisions_per_month": dpm,
            "mintrl_estimate_months": mintrl_months_estimate(dpm),
            "kill_bar": KILL_BAR_TEXT,
            "cost_model": COST_TEXT[kind],
        },
    }


def family_templates(family):
    """{hyp_id: entry} for one family — {} for families the recorded stream
    cannot express (reversion_pattern needs a strategy-family replay,
    cross_section/lead_lag need data the tables do not hold). Saying so is
    the honest answer; the picker still lists them for the LLM path."""
    W = 4.3   # weekly decisions per month
    D = 30.0  # daily marks per month
    S = 30.0  # price-stream estimate: ~1/day after per-pair de-overlap (estimate)
    if family == "trend":
        return {
            "hyp_tsmom_btc_26w": _entry("trend", "trend",
                {"rule": "tsmom", "pair": "XBTUSD", "weeks": 26, "horizon": "fwd168"},
                "slower 26-week time-series momentum on BTC — a longer lookback than the "
                "20w built-in trades less and may hold trends the 20w rule exits early; "
                "one decision per ISO week, graded on the next week", W),
            "hyp_tsmom_btc_52w": _entry("trend", "trend",
                {"rule": "tsmom", "pair": "XBTUSD", "weeks": 52, "horizon": "fwd168"},
                "52-week time-series momentum on BTC — the classic 12-month lookback; "
                "one decision per ISO week, graded on the next week", W),
            "hyp_tsmom_eth_52w": _entry("trend", "trend",
                {"rule": "tsmom", "pair": "ETHUSD", "weeks": 52, "horizon": "fwd168"},
                "52-week time-series momentum on ETH — same 12-month rule, second asset "
                "so the two can disagree", W),
            "hyp_donch_btc_100_50": _entry("trend", "trend",
                {"rule": "donchian", "pair": "XBTUSD", "enter_days": 100, "exit_days": 50,
                 "horizon": "fwd168"},
                "wider Donchian channel on BTC (enter 100-day high, exit 50-day low) — fewer "
                "whipsaws than the 50/25 built-in at the price of later exits", W),
        }
    if family == "carry":
        return {
            "hyp_carry_hm2": _entry("carry", "carry",
                {"symbol": "PF_XBTUSD", "hurdle_mult": 2.0},
                "funding carry on BTC perp with a 2x stricter entry hurdle — only deploys "
                "into unusually rich funding, so fewer round trips pay the 4-leg cost", D),
            "hyp_carry_hm3": _entry("carry", "carry",
                {"symbol": "PF_XBTUSD", "hurdle_mult": 3.0},
                "funding carry on BTC perp with a 3x entry hurdle — the extreme-funding-only "
                "variant", D),
            "hyp_carry_eth_hm2": _entry("carry", "carry",
                {"symbol": "PF_ETHUSD", "hurdle_mult": 2.0},
                "funding carry on ETH perp with a 2x entry hurdle — second venue of the same "
                "return source (scored only once ETH funding history exists)", D),
        }
    if family == "exit_rule":
        return {
            "hyp_exit_168h": _entry("price", "exit_rule",
                {"conf": None, "horizon": "fwd168"},
                "same entries as base, exit at a fixed 168h — the loss decomposition put the "
                "bleed in exits, so the slowest expressible exit competes", S / 3),
            "hyp_exit_6h_c55": _entry("price", "exit_rule",
                {"conf": 0.55, "horizon": "fwd6"},
                "fast 6h exit only on conf >= 0.55 entries — short holding period on the "
                "higher-quality subset", S),
            "hyp_exit_24h_c55": _entry("price", "exit_rule",
                {"conf": 0.55, "horizon": "fwd24"},
                "24h exit only on conf >= 0.55 entries", S),
        }
    if family == "regime_gate":
        return {
            "hyp_gate_adx25": _entry("price", "regime_gate",
                {"adx": 25.0, "horizon": "fwd48"},
                "trade only when ADX >= 25 (a middle regime floor between strict_gates' 18 "
                "and trend_rr's 30)", S / 2),
            "hyp_gate_er20": _entry("price", "regime_gate",
                {"er": 0.20, "horizon": "fwd48"},
                "trade only when the efficiency ratio >= 0.20 — a cleaner-trend gate than "
                "ADX alone", S / 2),
            "hyp_gate_c60_rr2": _entry("price", "regime_gate",
                {"conf": 0.60, "rr": 2.0, "horizon": "fwd48"},
                "conf >= 0.60 AND net R:R >= 2.0 — the joint quality gate", S / 3),
        }
    return {}


EXPRESSIBLE_FAMILIES = ("trend", "carry", "exit_rule", "regime_gate")


def all_templates():
    out = {}
    for fam in FAMILIES:
        out.update(family_templates(fam))
    return out


def templates_for_picks(picks, exclude_ids=()):
    """Template entries for a picker result, skipping ids already in play."""
    out = {}
    for p in picks or []:
        for hid, e in family_templates(p.get("family")).items():
            if hid not in exclude_ids:
                out[hid] = e
    return out
