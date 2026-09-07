#!/usr/bin/env python3
"""Autonomous PAPER autopilot — champion/challenger allocator over the bot's OWN configs.

WHAT THIS IS (and is NOT)
-------------------------
This is an MLOps allocator, not a trading strategy. It invents NO indicators, NO
signals, and makes NO market or profit claims. It runs an array of *paper*
challengers — each a `PaperTrader(force_paper=True)` carrying a small set of
per-instance "Tier-A" levers over the bot's EXISTING SignalEngine — scores each
one's recent OUT-OF-SAMPLE, net-of-fee paper performance with the find_signal.py
discipline, and decides which single config (if any) has earned the right to run
the real *paper* book. The honest default is FLAT (cash): a config must EARN
allocation by clearing every gate out-of-sample. When nothing clears, the book
stays flat.

HARD SANDBOX INVARIANTS (this module can NEVER place a real order)
-----------------------------------------------------------------
1. Every challenger is `PaperTrader(force_paper=True, no_persist=True)`. Its
   `_is_live()` is wired to return False FOREVER (bot_server.py PaperTrader._is_live),
   independent of PAPER_LOCK / LIVE_MODE / _paper_mode. force_paper traders route
   through the paper branch of _open/_close and touch no exchange.
2. This module NEVER imports or references any order/private endpoint
   (_kraken_place_order / _kf_place_order / _binance_place_order / _kraken_private /
   _kf_private / _binance_private). It only calls PaperTrader.on_signal and reads
   .trades / .balance.
3. On __init__ and on EVERY decide(), it asserts `bot_server.is_live()` is False.
   If is_live() is ever True, allocation is forced FLAT, an alert is logged, and no
   config is ever allocated.
4. It NEVER reads, writes, or clears PAPER_LOCK, and NEVER mutates module globals
   (CONFIRM_TICKS/ADX_MIN/RISK_MIN/... are shared by every instance). All config
   differences are per-instance levers applied additively BEFORE on_signal.

ALLOCATION SEMANTICS
--------------------
The real paper book is represented by the config id "base" (it runs the bot's
default paper gates). `allows(pair, "base")` is True only when the current champion
IS "base" and the champion is non-FLAT. When a *challenger* is champion, or nothing
clears the cost gate, the real book stays FLAT rather than being silently
re-parametrised to an unproven-in-production lever set — the conservative choice for
a real-money-capable (though paper-locked) bot. Champion promotion among challengers
is allowed (paper only), gated by hysteresis, and logged.

LAB INTAKE (research_lab.py nominations)
----------------------------------------
Overnight, research_lab.py sweeps the SAME Tier-A levers over historical CSVs and
nominates up to 3 candidate configs into {DATA_DIR}/lab_challengers.json. This
module is the ONLY reader of that file, and it trusts NOTHING in it: every entry
is re-sanitized on intake (keys whitelisted; id forced to ^lab_[a-z0-9_]{1,24}$ so
it can never shadow a built-in; entry_conf_floor clamped to [0.28,0.90] and dropped
outright if it isn't a confidence fraction at all; min_rr None or clamped to
[0.5,5.0]; risk_min/risk_max None or clamped to [0.01,0.50] with min<=max;
allowed_strategies None or a subset of the bot's 8 _classify_strategy keys,
normalized list->set; hard cap 3). Bad entries are dropped with a log line, never
raised on. Survivors become ORDINARY challengers — the same
PaperTrader(force_paper=True, no_persist=True) sandbox, the same OOS scoring, the
same cost gate — appended after the 6 built-ins. decide() hot-swaps the lab slots
at most every 10 minutes on file mtime change: new ids join with a fresh paper
bankroll, removed ids retire (a retired champion falls back to FLAT explicitly,
never lingers), and no other challenger's in-memory trades are touched. The lab
file therefore cannot mutate globals, touch the real book, or bypass a gate:
a nomination still has to EARN allocation in live paper, out-of-sample.
"""

import json
import math
import os
import re
import statistics
import time
from datetime import datetime, timezone

import bot_server as bs

log = bs.log

# research_loop.py is the PURE research-side module (budget, N_eff clustering,
# family posteriors, templates). It ships alongside this file, but a boot must
# never depend on it: if it is missing, every consumer below degrades to the
# honest 'unknown' (budget None, N_eff = trials_count, empty posteriors) and
# says so in the log — it never guesses and never crashes the allocator.
try:
    import research_loop as rl
except Exception as _rl_err:          # pragma: no cover — deploy footgun guard
    rl = None
    log("AUTOPILOT", f"research_loop unavailable ({_rl_err}) — budget/N_eff/posteriors report unknown", "WRN")


# ── Tunables ──────────────────────────────────────────────────────────────────
CHALLENGER_START      = 2000.0   # virtual bankroll per challenger (mirrors _sim_trader)
MIN_OOS_TRADES        = 20       # a config needs this many OOS closed trades to be scored
MIN_TOTAL_TRADES      = 40       # ...and this many total before it can be considered
T_MARGIN              = 2.0      # multiple-comparison bar: best-of-N must clear |t|>this OOS
SWITCH_MARGIN         = 0.0010   # challenger must beat champion's OOS edge by this (fraction) to switch
DECISION_LOG_MAX      = 60       # rolling decision-log length kept in state
ATTEMPT_CONF_MIN      = 0.28     # mirrors _sim_trader's minimum confidence to attempt an entry

# Fee rate used at close (matches PaperTrader._close's _sim_fee selection).
_FEE_RATE = (bs.BINANCE_FEE        if bs.USE_BINANCE else
             bs.KRAKEN_FUTURES_FEE if bs.USE_FUTURES else
             bs.KRAKEN_FEE)


# ── Tournament survival statistics (DSR / PSR / MinTRL) ───────────────────────
# Bailey & Lopez de Prado's Deflated Sharpe Ratio machinery, applied to each
# entrant's NET per-decision return stream (per-trade for the intraday books,
# per-week or per-day for the weekly/carry entrants — the SR is per-decision,
# never annualized, and is only ever compared against a hurdle built from the
# SAME per-decision streams).
#
# N-TRIALS HONESTY: the SR0 hurdle is the expected max SR of N unskilled
# trials, where N = every entrant EVER tried, not just the survivors on the
# board today. trials_count is persisted and ONLY increments — retiring or
# killing an entrant never lowers the bar its siblings must clear.
KILL_PSR    = 0.20        # past MinTRL with deflated PSR below this -> KILLED
PROVEN_DSR  = 0.95        # past MinTRL with deflated PSR at/above this -> PROVEN (goal)
MINTRL_CONF = 0.95        # MinTRL target confidence (Phi^-1(0.95) in the formula)
_EM_GAMMA   = 0.5772156649015329   # Euler-Mascheroni, for the expected-max-SR term

# TRIALS_SEED documents the honest N at the moment this counter shipped
# (2026-09-03): 13 entrants existed (10 built-in configs — base, selective,
# high_conviction, momentum, strict_gates, exit_6h, exit_24h, trend_rr,
# reversion, loose — plus the 3 lab nomination slots), and 3 gap_fade lab
# entrants had already been tried and died before any counter existed.
# 13 + 3 = 16. Lab ids seen after the seed each increment the counter; if one
# of them happens to be a re-nomination of a seeded slot the count OVERSTATES
# N — which only RAISES the hurdle, the conservative direction.
TRIALS_SEED = 16
# Built-in ids already covered by TRIALS_SEED (so re-registering them at every
# boot does not double-count). Entrants added AFTER the seed are absent here
# on purpose: their registration increments trials_count exactly once.
PRE_SEED_IDS = frozenset({
    "base", "selective", "high_conviction", "momentum", "strict_gates",
    "exit_6h", "exit_24h", "trend_rr", "reversion", "loose",
})

# ── Honest cost model for the weekly / carry counterfactual entrants ──────────
# Spot legs assume TAKER + slippage each way (no maker assumption at weekly
# cadence — crossing the spread is the honest default), Kraken base tier.
# Perp legs use the Kraken Futures taker fee + the same slippage.
CF_SPOT_RT     = 2.0 * (bs.KRAKEN_FEE + bs.SLIPPAGE)          # spot round trip (~1.8%)
CARRY_SPOT_LEG = bs.KRAKEN_FEE + bs.SLIPPAGE                  # one spot leg
CARRY_PERP_LEG = bs.KRAKEN_FUTURES_FEE + bs.SLIPPAGE          # one perp leg
CARRY_RT_4LEG  = 2 * CARRY_SPOT_LEG + 2 * CARRY_PERP_LEG      # open+close the pair (~2.1%)
# Enter carry only when trailing 7d ANNUALIZED funding clears 4 full 4-leg
# round trips a year (budget for churn) plus a 10% absolute floor (minimum
# annualized carry worth the operational/basis risk at all).
CARRY_HURDLE_ANN = CARRY_RT_4LEG * 4.0 + 0.10
CARRY_MIN_7D_HOURS = 120   # trailing-7d window must hold >=120 of 168 hourly rates to decide

SECS_PER_WEEK  = 7 * 86400
SECS_PER_MONTH = 2629800.0   # 1/12 Julian year, for the "verdict in ~N months" estimate


def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p):
    """Inverse standard-normal CDF (Acklam's rational approximation, ~1e-9)."""
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
    """E[max SR] of n_trials unskilled strategies with cross-trial Var[SR].

    The DSR hurdle SR0*. Rises with n_trials (more trials tried => the best
    of them looks better by pure luck) and with cross-entrant SR dispersion.
    Returns None when it cannot be computed honestly (var unknown, <2 trials).
    """
    if var_sr is None or var_sr < 0 or n_trials is None or n_trials < 2:
        return None
    sd = math.sqrt(var_sr)
    return sd * ((1 - _EM_GAMMA) * _norm_ppf(1 - 1.0 / n_trials)
                 + _EM_GAMMA * _norm_ppf(1 - 1.0 / (n_trials * math.e)))


def dsr_stats(nets, sr0, born_ts=None, now=None):
    """{sr, psr, dsr, min_trl, trades_n, verdict} for one entrant's net stream.

    sr      : per-decision Sharpe (mean/sd of nets — NOT annualized).
    psr     : Probabilistic SR vs 0 (prob. the true SR is positive).
    dsr     : PSR evaluated at the N-trials hurdle SR0* — the Deflated SR.
              The pre-registered kill bar "PSR < 0.20" means THIS number:
              PSR against the multiple-comparison hurdle, not against zero.
    min_trl : Minimum Track Record Length vs SR0* at MINTRL_CONF. The formula
              squares (sr - sr0), so it is finite on BOTH sides of the hurdle:
              a clearly-bad entrant reaches its (small) MinTRL quickly and can
              be killed; one hugging the hurdle needs a long record either way.
    verdict : honest English. Weekly entrants surface "verdict in ~N months at
              current signal rate" until their track record is long enough —
              pre-registered: no weekly entrant gets ANY verdict before ~6
              months of decisions (MinTRL at ~1 decision/week says so itself).
    Never raises; missing inputs degrade to 'insufficient data'.
    """
    n = len(nets)
    out = {"trades_n": n, "sr": None, "psr": None, "dsr": None,
           "min_trl": None, "months_to_verdict": None, "verdict": "insufficient data"}
    if n < 2:
        return out
    mu = statistics.fmean(nets)
    sd = statistics.pstdev(nets)
    if sd < 1e-12:
        out["verdict"] = "degenerate returns (sd=0) — cannot score"
        return out
    sr = mu / sd
    m2 = sd * sd
    m3 = statistics.fmean([(x - mu) ** 3 for x in nets])
    m4 = statistics.fmean([(x - mu) ** 4 for x in nets])
    skew = m3 / (m2 ** 1.5)
    kurt = m4 / (m2 ** 2)          # normal = 3 (non-excess)
    denom = 1.0 - skew * sr + ((kurt - 1.0) / 4.0) * sr * sr
    if denom <= 0:                 # pathological higher moments — fall back to
        denom = 1.0                # the normal-returns case rather than crash
    out["sr"] = sr
    out["psr"] = _norm_cdf(sr * math.sqrt(n - 1) / math.sqrt(denom))
    if sr0 is None:
        out["verdict"] = "no cross-entrant hurdle yet (need >=2 scored entrants)"
        return out
    out["dsr"] = _norm_cdf((sr - sr0) * math.sqrt(n - 1) / math.sqrt(denom))
    if abs(sr - sr0) < 1e-12:
        out["verdict"] = "SR sits exactly on the hurdle — no resolution possible yet"
        return out
    min_trl = 1.0 + denom * (_norm_ppf(MINTRL_CONF) / (sr - sr0)) ** 2
    out["min_trl"] = min_trl
    if n < min_trl:
        now = time.time() if now is None else now
        months = None
        if born_ts and now > born_ts:
            rate = n / max((now - born_ts) / SECS_PER_MONTH, 1e-9)  # decisions/month
            if rate > 0:
                months = (min_trl - n) / rate
        if months is not None and math.isfinite(months):
            out["months_to_verdict"] = months
            out["verdict"] = f"verdict in ~{max(1, math.ceil(months))} months at current signal rate"
        else:
            out["verdict"] = f"track record too short (n={n} < MinTRL {min_trl:.0f})"
        return out
    out["verdict"] = ("SURVIVES (past MinTRL, DSR>=%.2f)" % KILL_PSR
                      if out["dsr"] >= KILL_PSR else
                      "KILL (past MinTRL, DSR<%.2f)" % KILL_PSR)
    return out


# ── Weekly-bar machinery for the trend/switch entrants ────────────────────────
def _iso_week_key(ts):
    """(iso_year, iso_week) of an epoch-seconds timestamp, UTC."""
    d = datetime.fromtimestamp(float(ts), tz=timezone.utc).isocalendar()
    return (d[0], d[1])


def daily_bars_from_hourly(rows):
    """[(day_start_ts, high, low, close)] from hourly (ts, high, low, close) rows.

    Buckets by UTC day; close = last hourly close of the day. Input need not be
    contiguous — gaps just mean fewer daily bars (data honesty: no fabrication).
    """
    days = {}
    for ts, hi, lo, cl in rows:
        try:
            day = int(float(ts) // 86400) * 86400
            b = days.get(day)
            if b is None:
                days[day] = [day, float(hi), float(lo), float(cl), float(ts)]
            else:
                b[1] = max(b[1], float(hi))
                b[2] = min(b[2], float(lo))
                if float(ts) >= b[4]:
                    b[3] = float(cl)
                    b[4] = float(ts)
        except Exception:
            continue
    return [(d[0], d[1], d[2], d[3]) for d in sorted(days.values())]


def weekly_closes_from_daily(daily):
    """[(week_key, decision_ts, close, daily_index)] — EXACTLY one per ISO week.

    decision_ts is the last daily bar's day-start in that week: the moment the
    weekly decision is taken. The dict-keyed grouping IS the one-decision-per-
    ISO-week enforcement — duplicate days in a week collapse to the last one.
    """
    weeks = {}
    for i, (day_ts, _hi, _lo, close) in enumerate(daily):
        weeks[_iso_week_key(day_ts)] = (day_ts, close, i)
    out = [(k, v[0], v[1], v[2]) for k, v in weeks.items()]
    out.sort(key=lambda x: x[1])
    return out


def tsmom_decisions(weekly, sma_weeks=20):
    """[(decision_ts, pos)] — pos=1 when weekly close > SMA(sma_weeks), else 0.

    One decision per ISO week (weekly is already deduped). The decision at
    week i governs exposure over week i+1 — the scorer pairs it with the NEXT
    weekly return, so no decision ever sees the bar it is graded on.
    """
    out = []
    closes = [w[2] for w in weekly]
    for i in range(len(weekly)):
        if i + 1 < sma_weeks:
            continue
        sma = statistics.fmean(closes[i + 1 - sma_weeks: i + 1])
        out.append((weekly[i][1], 1 if closes[i] > sma else 0))
    return out


def donchian_decisions(daily, weekly, enter_days=50, exit_days=25):
    """[(decision_ts, pos)] — enter on close > prior enter_days-day high, exit on
    close < prior exit_days-day low; evaluated ONCE per ISO week (state machine
    stepped only at weekly closes). Long-flat, one decision per week."""
    out = []
    pos = 0
    for _key, dts, close, di in weekly:
        if di < enter_days:            # not enough daily history behind this week
            continue
        ch_high = max(d[1] for d in daily[di - enter_days: di])   # prior N days' highs
        ch_low = min(d[2] for d in daily[max(0, di - exit_days): di])
        if pos == 0 and close > ch_high:
            pos = 1
        elif pos == 1 and close < ch_low:
            pos = 0
        out.append((dts, pos))
    return out


# ── Challenger configs (per-instance Tier-A levers ONLY) ──────────────────────
# Each lever is applied additively to the SHARED SignalEngine's output before the
# challenger's on_signal runs. No new indicators, no global mutation.
#   entry_conf_floor  : minimum confidence to attempt an entry (>= ATTEMPT_CONF_MIN)
#   min_rr            : minimum reward:risk after ATR reshaping (None -> PAPER_MIN_RR)
#   allowed_strategies: subset of _classify_strategy keys the config will trade (None -> all)
#   risk_min/risk_max : per-instance stake band (sizing only; None -> global RISK_MIN/MAX)
# Counterfactual configs are scored only on shadow_signals rows recorded AFTER
# this epoch (set at feature ship). strict_gates in particular was suggested by
# an audit OVER the existing rows — grading it on those same rows would be the
# in-sample sin this whole module exists to prevent. Future rows only.
#
# FAMILY TAGS (2026-09-06): every entrant carries a hypothesis family so the
# goal block can keep per-family evidence (kills = f, survivors past 4 weeks
# = s). Legacy price entrants are tagged by what their lever actually varies:
# gate floors -> regime_gate, fixed-horizon exits -> exit_rule, strategy
# subsets -> trend / reversion_pattern. carry_or_trend scores the allocator
# itself and gets the built-in-only family "switch".
#
# NO AUTO-MUTATION, NO BREEDING. New entrants enter this list ONLY by a
# written hypothesis with hardcoded born_ts — never by machine-generated
# parameter sweeps over the survivors. OOS evidence for the rule: the 3
# gap_fade lab entrants (auto-nominated from historical sweeps) all died
# out-of-sample; sweep-born configs are the N-trials problem incarnate.
AP_CF_EPOCH = 1787545000.0    # 2026-08-24, counterfactual scoring shipped

CHALLENGER_CONFIGS = [
    {   # CHAMPION starts here — mirrors the default paper book / _sim_trader
        "id": "base", "family": "regime_gate",
        "cf": {"conf": None, "horizon": "fwd48"},
        "entry_conf_floor": ATTEMPT_CONF_MIN,
        "min_rr": None,
        "allowed_strategies": None,
        "risk_min": None, "risk_max": None,
    },
    {   # more selective: higher confidence + tighter R:R.
        # cf_only since 2026-09-03: the live sandbox books were STRUCTURALLY
        # STARVED — a challenger only trades when the main pipeline fires
        # (~1-2 signals/day shared across the whole pool), so none of these
        # books could mathematically reach MIN_OOS_TRADES on the live path.
        # The counterfactual stream is the only path that actually feeds them.
        "id": "selective", "family": "regime_gate", "cf_only": True,
        "cf": {"conf": 0.55, "rr": 1.5, "horizon": "fwd48"},
    },
    {   # strictest: only the highest-quality named setups
        "id": "high_conviction", "family": "trend",
        "entry_conf_floor": 0.65,
        "min_rr": 2.0,
        "allowed_strategies": {"MULTI_SIGNAL", "MOMENTUM_BREAKOUT", "TREND_CONTINUATION"},
        "risk_min": 0.04, "risk_max": 0.08,
    },
    {   # trend/momentum family only.
        # cf_only since 2026-09-03 (structurally starved on the live path — see
        # "selective"). NO cf spec on purpose: the strategy-family lever is not
        # expressible from the recorded shadow stream yet (it needs a pillars
        # replay through _classify_strategy), and pretending a bare conf floor
        # IS "momentum" would score a different hypothesis under this id.
        # Parked and honestly unscored until the stream can express it.
        "id": "momentum", "family": "trend", "cf_only": True,
        "entry_conf_floor": 0.50,
        "min_rr": 1.5,
        "allowed_strategies": {"MOMENTUM_BREAKOUT", "TREND_CONTINUATION", "MULTI_SIGNAL"},
    },
    {   # ORIGINAL gate thresholds, as a tournament entrant instead of a belief.
        # The gate-loosening audit (analyze_gate_loosening.py) could not judge
        # these at n_indep=22; here they compete on every future signal and get
        # crowned only by the same bars as everyone else. cf_only: the live
        # challenger path cannot filter on ADX/ER — shadow rows now record both.
        "id": "strict_gates", "family": "regime_gate", "cf_only": True,
        "cf": {"conf": 0.50, "adx": 18.0, "er": 0.15, "horizon": "fwd48"},
    },
    {   # EXIT family: the measured loss decomposition put 0.000% in entries and
        # -0.211% in exits, yet every other challenger varies entries. These two
        # hold the same entries and vary only how long the trade lives.
        "id": "exit_6h", "family": "exit_rule", "cf_only": True,
        "cf": {"conf": None, "horizon": "fwd6"},
    },
    {
        "id": "exit_24h", "family": "exit_rule", "cf_only": True,
        "cf": {"conf": None, "horizon": "fwd24"},
    },
    {   # TREND-LOOSENING hypothesis (the owner's, 2026-08-27): "take the
        # signals net_rr blocks when the market is trending" — born from one
        # winning hand trade on SOL. The retrospective slice said no (ADX>=30:
        # gross +0.78% but net -0.52% at n_indep=6 — too small to prove either
        # side), so the idea competes here instead of changing the live gate.
        # No conf/er/rr floors on purpose: this IS the loosening, isolated.
        # born_ts is HARDCODED to the registration moment: the rows that
        # generated the hypothesis must never be the rows that grade it.
        "id": "trend_rr", "family": "regime_gate", "cf_only": True, "born_ts": 1787875000.0,
        "cf": {"adx": 30.0, "horizon": "fwd48"},
    },
    {   # mean-reversion / pattern family only.
        # cf_only since 2026-09-03 (structurally starved on the live path — see
        # "selective"). No cf spec for the same reason as "momentum": the
        # family lever cannot be expressed from the recorded stream yet.
        "id": "reversion", "family": "reversion_pattern", "cf_only": True,
        "entry_conf_floor": 0.50,
        "min_rr": 1.5,
        "allowed_strategies": {"RSI_REVERSAL", "PATTERN_BREAKOUT"},
    },
    {   # least selective: trades the most, clears the lowest bar.
        # cf_only since 2026-09-03 (structurally starved on the live path).
        # Counterfactually "loose" has NO expressible difference from "base"
        # (same conf floor, no other stream-expressible lever) — its cf record
        # will mirror base's, which is itself an honest statement: the lever
        # set only ever differed on the starved live path.
        "id": "loose", "family": "regime_gate", "cf_only": True,
        "cf": {"conf": None, "horizon": "fwd48"},
    },
    # ── Weekly / carry entrants (2026-09-03 registration) ─────────────────────
    # All cf_only, all with HARDCODED born_ts = registration time (rounded UP a
    # few hours so no pre-registration bar can ever slip into the grade).
    # PRE-REGISTERED EXPECTATION, stated before any data: at ~1 decision per
    # week, MIN_OOS_TRADES=20 alone takes ~5 months and MinTRL will typically
    # ask for more — NO verdict on any weekly entrant before ~6+ months. The
    # scorer surfaces this honestly ("verdict in ~N months at current signal
    # rate") instead of pretending an early number means anything.
    # Hypotheses (written, not bred):
    #   tsmom_*   — time-series momentum: the live intraday books keep showing
    #               entries are fine and exits bleed (loss decomposition:
    #               ~0.000% entries, -0.211% exits OOS) — the slow weekly
    #               trend-following exit is the written alternative.
    #   donchian  — the same slow-exit hypothesis in breakout form, so the
    #               two trend expressions can disagree and be told apart.
    #   carry_*   — funding carry is a DIFFERENT return source than price
    #               direction; graded only on recorded funding_rates history.
    {   # long-flat BTC: long when weekly close > 20-week SMA, one decision per
        # ISO week, graded on the NEXT week's return (fwd168), spot RT costs.
        "id": "tsmom_btc_20w", "family": "trend", "cf_only": True, "kind": "trend",
        "born_ts": 1788500000.0,   # 2026-09-04 ~05:30 UTC — registration, rounded up
        "cf": {"rule": "tsmom", "pair": "XBTUSD", "weeks": 20, "horizon": "fwd168"},
    },
    {   # Donchian long-flat BTC: enter 50-day-high breakout, exit 25-day low,
        # state stepped once per ISO week, graded on the next week (fwd168).
        "id": "donchian_btc", "family": "trend", "cf_only": True, "kind": "trend",
        "born_ts": 1788500000.0,
        "cf": {"rule": "donchian", "pair": "XBTUSD",
               "enter_days": 50, "exit_days": 25, "horizon": "fwd168"},
    },
    {   # same 20-week TSMOM hypothesis on ETH.
        "id": "tsmom_eth_20w", "family": "trend", "cf_only": True, "kind": "trend",
        "born_ts": 1788500000.0,
        "cf": {"rule": "tsmom", "pair": "ETHUSD", "weeks": 20, "horizon": "fwd168"},
    },
    {   # counterfactual paired position (long spot + short perp) harvesting
        # funding. Enters when trailing 7d ANNUALIZED funding > CARRY_HURDLE_ANN
        # (4x the 4-leg RT cost + 10% floor), exits below hurdle/2 or negative.
        # Marked daily; graded on accrued funding minus the modeled 4-leg costs.
        # Reads the funding_rates contract table — empty means the honest
        # status "waiting on funding history", not a fabricated score.
        "id": "carry_harvest", "family": "carry", "cf_only": True, "kind": "carry",
        "born_ts": 1788500000.0,
        "cf": {"symbol": "PF_XBTUSD"},
    },
    {   # scores the SWITCH itself: weekly, allocate to carry_harvest's
        # condition if live, else tsmom_btc_20w's if live, else flat. Costs
        # charged on every allocation change (each sleeve's own legs).
        "id": "carry_or_trend", "family": "switch", "cf_only": True, "kind": "switch",
        "born_ts": 1788500000.0,
        "cf": {"pair": "XBTUSD", "symbol": "PF_XBTUSD", "weeks": 20},
    },
]

MAIN_BOOK_CONFIG_ID = "base"   # the config identity the REAL paper book represents


# ── Lab intake (nominations from research_lab.py) ─────────────────────────────
# The lab file is DATA, not code: it crosses a process boundary from the overnight
# research job, so every field is re-validated here as if it were hostile. Ids are
# forced into the lab_ namespace by regex, so a nomination can never collide with
# (or shadow) a built-in config id like "base".
LAB_MAX_SLOTS    = 3                                  # hard cap on lab challengers
LAB_REFRESH_SECS = 600.0                              # decide() re-stats the file at most this often
_LAB_ID_RE       = re.compile(r"^lab_[a-z0-9_]{1,24}\Z")  # \Z not $: $ admits a trailing \n, which would allow "lab_x" vs "lab_x\n" lookalike ids
_LAB_CONF_CLAMP  = (0.28, 0.90)                       # entry_conf_floor bounds (>= ATTEMPT_CONF_MIN)
_LAB_RR_CLAMP    = (0.5, 5.0)                         # min_rr bounds
_LAB_RISK_CLAMP  = (0.01, 0.50)                       # risk_min/risk_max bounds
# The 8 strategy keys _classify_strategy can emit (MULTI_SIGNAL, MOMENTUM_BREAKOUT,
# TREND_CONTINUATION, RSI_REVERSAL, NEWS_CATALYST, PATTERN_BREAKOUT, CONFLUENCE,
# LEARNING_SIGNAL) — derived from the source dict so the two can never drift apart.
_LAB_STRATEGY_KEYS = frozenset(bs._STRATEGIES)
# Levers whose change means a lab config is a NEW experiment (its trader must be
# rebuilt fresh so its score history is earned under exactly one lever set).
_LAB_LEVER_KEYS = ("entry_conf_floor", "min_rr", "allowed_strategies",
                   "risk_min", "risk_max")


def _lab_path():
    """The nomination handoff file — written ONLY by research_lab.py, read ONLY here."""
    return os.path.join(bs._DATA_DIR, "lab_challengers.json")


def _num(v):
    """Finite float or None. Rejects bools (json true/false are not numbers here)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if math.isfinite(v) else None


def sanitize_lab_configs(raw):
    """Validate raw lab-file content into at most LAB_MAX_SLOTS config dicts.

    NEVER raises — a malformed file yields [], a malformed entry is dropped with a
    log line (the allocator must boot no matter what the research job wrote).
    Accepts the parsed file dict ({"configs": [...]}) or a bare list. Each survivor
    carries exactly the Tier-A lever keys scan() reads (allowed_strategies already
    normalized list->set) plus id/born_ts/note for the dashboard.
    """
    out = []
    try:
        entries = raw.get("configs") if isinstance(raw, dict) else raw
        if not isinstance(entries, list):
            return out
        builtin_ids = {c["id"] for c in CHALLENGER_CONFIGS}
        for e in entries:
            try:
                if not isinstance(e, dict):
                    raise ValueError("entry is not an object")
                cid = e.get("id")
                if not isinstance(cid, str) or not _LAB_ID_RE.match(cid):
                    raise ValueError(f"id {cid!r} fails ^lab_[a-z0-9_]{{1,24}}$")
                # Unreachable given the regex (built-ins never start with lab_),
                # kept as belt-and-suspenders against a future built-in rename.
                if cid in builtin_ids:
                    raise ValueError(f"id {cid!r} shadows a built-in config")
                if any(c["id"] == cid for c in out):
                    raise ValueError(f"duplicate id {cid!r}")

                # entry_conf_floor: must BE a confidence (fraction of 1) to clamp at
                # all — anything outside [0,1] is garbage, not a bold choice.
                floor = _num(e.get("entry_conf_floor"))
                if floor is None or not (0.0 <= floor <= 1.0):
                    raise ValueError(f"entry_conf_floor {e.get('entry_conf_floor')!r} is not a confidence fraction")
                floor = min(max(floor, _LAB_CONF_CLAMP[0]), _LAB_CONF_CLAMP[1])

                min_rr = e.get("min_rr")
                if min_rr is not None:
                    min_rr = _num(min_rr)
                    if min_rr is None:
                        raise ValueError("min_rr is neither null nor a number")
                    min_rr = min(max(min_rr, _LAB_RR_CLAMP[0]), _LAB_RR_CLAMP[1])

                risk = {}
                for k in ("risk_min", "risk_max"):
                    v = e.get(k)
                    if v is not None:
                        v = _num(v)
                        if v is None:
                            raise ValueError(f"{k} is neither null nor a number")
                        v = min(max(v, _LAB_RISK_CLAMP[0]), _LAB_RISK_CLAMP[1])
                    risk[k] = v
                if (risk["risk_min"] is not None and risk["risk_max"] is not None
                        and risk["risk_min"] > risk["risk_max"]):
                    raise ValueError("risk_min > risk_max")

                allowed = e.get("allowed_strategies")
                if allowed is not None:
                    if not isinstance(allowed, (list, tuple, set)):
                        raise ValueError("allowed_strategies is neither null nor a list")
                    allowed = set(allowed)                     # normalize list -> set
                    if not allowed or not allowed <= _LAB_STRATEGY_KEYS:
                        raise ValueError(f"allowed_strategies {sorted(allowed)} not a subset of the 8 strategy keys")

                born = _num(e.get("born_ts"))                  # tolerant: bad ts != bad config
                out.append({
                    "id": cid,
                    "entry_conf_floor": floor,
                    "min_rr": min_rr,
                    "allowed_strategies": allowed,
                    "risk_min": risk["risk_min"],
                    "risk_max": risk["risk_max"],
                    "born_ts": born if born is not None else time.time(),
                    "note": str(e.get("note", ""))[:200],
                })
            except Exception as ex:
                log("AUTOPILOT", f"lab config dropped: {ex}", "WRN")
        if len(out) > LAB_MAX_SLOTS:
            log("AUTOPILOT", f"lab file has {len(out)} valid configs — keeping first {LAB_MAX_SLOTS}", "WRN")
            out = out[:LAB_MAX_SLOTS]
    except Exception as ex:
        log("AUTOPILOT", f"lab sanitize failed: {ex}", "WRN")
        out = []
    return out


def _read_lab_file():
    """(sanitized configs, file mtime) — ([], None) when the file doesn't exist.

    mtime is captured BEFORE the read so a write that lands mid-read is seen as a
    change on the next refresh rather than silently swallowed.
    """
    path = _lab_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return [], None
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as ex:
        log("AUTOPILOT", f"lab file unreadable: {ex}", "WRN")
        return [], mtime
    return sanitize_lab_configs(raw), mtime


# ── Hypothesis intake ({DATA_DIR}/hypotheses.json — contract [H]) ─────────────
# Written by the PC-side research pass (LLM pre-registration, owner ideas,
# human, or the deterministic template fallback); read ONLY here, trusted
# NOWHERE. Mirrors the lab sanitizer: own id namespace (hyp_), per-kind cf
# whitelist COPIED from the scorer's own keys (a key the scorer ignores would
# score a different hypothesis under that id — dropped, never tolerated),
# mandatory 'HYPOTHESIS:' note + full pre-registration block, born_ts forced
# to >= intake time so no pre-registration bar can ever be graded, hard cap
# HYP_MAX_SLOTS, cf_only FORCED (a hypothesis never gets a live sandbox
# book, let alone the real one). A bad file yields [] with a log line.
HYP_MAX_SLOTS  = rl.HYP_MAX_SLOTS if rl else 5
_HYP_ID_RE     = re.compile(r"^hyp_[a-z0-9_]{1,24}\Z")
HYP_KINDS      = ("price", "trend", "carry", "switch")
HYP_FAMILIES   = (rl.FAMILIES if rl else
                  ("trend", "carry", "reversion_pattern", "exit_rule", "regime_gate",
                   "cross_section", "lead_lag"))
HYP_ORIGINS    = (rl.ORIGINS if rl else ("llm_prereg", "owner_idea", "human", "template"))
HYP_PREREG_KEYS = ("mechanism", "expected_decisions_per_month",
                   "mintrl_estimate_months", "kill_bar", "cost_model")
_HYP_HORIZONS  = ("fwd6", "fwd24", "fwd48", "fwd168")
_HYP_PAIR_RE   = re.compile(r"^[A-Z0-9]{2,10}USD\Z")
_HYP_SYMBOL_RE = re.compile(r"^PF_[A-Z0-9]{2,10}USD\Z")
# Per-kind cf whitelist — EXACTLY the keys each _score_cf_* branch reads.
HYP_CF_KEYS = {
    "price":  ("conf", "adx", "er", "rr", "horizon", "weekly"),
    "trend":  ("rule", "pair", "weeks", "enter_days", "exit_days", "horizon"),
    "carry":  ("symbol", "hurdle_mult"),
    "switch": ("pair", "symbol", "weeks"),
}
_HYP_NOTE_MAX  = 600
KILL_REASON_CODES = ("cost", "overlap_artifact", "no_signal", "regime_flip", "unknown")


def family_of(cfg):
    """The entrant's hypothesis family (explicit tag, else derived from kind)."""
    fam = cfg.get("family") if isinstance(cfg, dict) else None
    if fam:
        return str(fam)
    kind = cfg.get("kind", "price") if isinstance(cfg, dict) else "price"
    return {"trend": "trend", "carry": "carry", "switch": "switch"}.get(kind, "regime_gate")


def origin_of(cid, cfg=None):
    if isinstance(cid, str) and cid.startswith("hyp_"):
        return str((cfg or {}).get("origin") or "unknown")
    if isinstance(cid, str) and cid.startswith("lab_"):
        return "lab"
    return "builtin"


def decisions_per_year_of(cfg):
    """Decision cadence implied by kind (+weekly flag): weekly=52, daily=365."""
    kind = cfg.get("kind", "price") if isinstance(cfg, dict) else "price"
    cf = (cfg.get("cf") if isinstance(cfg, dict) else None) or {}
    if rl:
        return rl.kind_cadence(kind, cf)
    if kind in ("trend", "switch"):
        return 52
    if kind == "carry":
        return 365
    return 52 if cf.get("weekly") else 365


def horizon_of(cfg):
    kind = cfg.get("kind", "price")
    cf = cfg.get("cf") or {}
    if kind == "carry":
        return "daily"
    if kind in ("trend", "switch"):
        return "fwd168"
    return str(cf.get("horizon", "fwd48")) if cfg.get("cf") or cfg.get("cf_only") else "live"


def cost_model_of(cfg):
    """The cost model the SCORER actually charged (not the prereg's prose)."""
    kind = cfg.get("kind", "price")
    if kind == "trend":
        return f"spot_rt {CF_SPOT_RT:.4f} (half at entry/exit week)"
    if kind == "carry":
        return f"4leg_rt {CARRY_RT_4LEG:.4f} (half at entry/exit day)"
    if kind == "switch":
        return f"sleeve legs: spot_rt {CF_SPOT_RT:.4f} / 4leg_rt {CARRY_RT_4LEG:.4f}"
    if cfg.get("cf") or cfg.get("cf_only"):
        return f"round_trip {bs.ROUND_TRIP_COST_PCT:.4f} per cf fill"
    return f"live paper fee {_FEE_RATE:.4f} + slippage in fills"


def _hyp_cf(kind, raw_cf):
    """Validate one hypothesis cf spec against the scorer's whitelist. Raises."""
    if not isinstance(raw_cf, dict):
        raise ValueError("cf is not an object")
    allowed = HYP_CF_KEYS[kind]
    extra = sorted(set(raw_cf) - set(allowed))
    if extra:
        raise ValueError(f"cf keys {extra} not in the {kind} scorer whitelist {list(allowed)}")
    cf = {}
    if kind == "price":
        for k, hi in (("conf", 1.0), ("adx", 100.0), ("er", 1.0), ("rr", 10.0)):
            if k in raw_cf:
                v = raw_cf[k]
                if v is not None:
                    v = _num(v)
                    if v is None or not (0.0 <= v <= hi):
                        raise ValueError(f"cf.{k} {raw_cf[k]!r} not a number in [0,{hi}]")
                cf[k] = v
        hz = raw_cf.get("horizon", "fwd48")
        if hz not in _HYP_HORIZONS:
            raise ValueError(f"cf.horizon {hz!r} not in {_HYP_HORIZONS}")
        cf["horizon"] = hz
        if "weekly" in raw_cf:
            if not isinstance(raw_cf["weekly"], bool):
                raise ValueError("cf.weekly must be a boolean")
            cf["weekly"] = raw_cf["weekly"]
        if "conf" not in cf:
            cf["conf"] = None
    elif kind == "trend":
        rule = raw_cf.get("rule", "tsmom")
        if rule not in ("tsmom", "donchian"):
            raise ValueError(f"cf.rule {rule!r} not tsmom|donchian")
        pair = raw_cf.get("pair")
        if not isinstance(pair, str) or not _HYP_PAIR_RE.match(pair):
            raise ValueError(f"cf.pair {pair!r} fails ^[A-Z0-9]{{2,10}}USD$")
        cf["rule"], cf["pair"] = rule, pair
        if rule == "tsmom":
            w = _num(raw_cf.get("weeks", 20))
            if w is None or w != int(w) or not (4 <= w <= 104):
                raise ValueError(f"cf.weeks {raw_cf.get('weeks')!r} not an int in [4,104]")
            cf["weeks"] = int(w)
        else:
            e = _num(raw_cf.get("enter_days", 50))
            x = _num(raw_cf.get("exit_days", 25))
            if e is None or e != int(e) or not (5 <= e <= 400):
                raise ValueError(f"cf.enter_days {raw_cf.get('enter_days')!r} not an int in [5,400]")
            if x is None or x != int(x) or not (2 <= x <= e):
                raise ValueError(f"cf.exit_days {raw_cf.get('exit_days')!r} not an int in [2,enter_days]")
            cf["enter_days"], cf["exit_days"] = int(e), int(x)
        hz = raw_cf.get("horizon", "fwd168")
        if hz != "fwd168":
            raise ValueError("trend entrants are graded on the next week: horizon must be fwd168")
        cf["horizon"] = "fwd168"
    elif kind == "carry":
        sym = raw_cf.get("symbol")
        if not isinstance(sym, str) or not _HYP_SYMBOL_RE.match(sym):
            raise ValueError(f"cf.symbol {sym!r} fails ^PF_[A-Z0-9]{{2,10}}USD$")
        cf["symbol"] = sym
        if "hurdle_mult" in raw_cf:
            m = _num(raw_cf["hurdle_mult"])
            # >= 1.0 by construction: a hypothesis may only RAISE the carry hurdle
            if m is None or not (1.0 <= m <= 4.0):
                raise ValueError(f"cf.hurdle_mult {raw_cf['hurdle_mult']!r} not in [1.0,4.0]")
            cf["hurdle_mult"] = m
    elif kind == "switch":
        pair = raw_cf.get("pair", "XBTUSD")
        sym = raw_cf.get("symbol", "PF_XBTUSD")
        if not isinstance(pair, str) or not _HYP_PAIR_RE.match(pair):
            raise ValueError(f"cf.pair {pair!r} fails ^[A-Z0-9]{{2,10}}USD$")
        if not isinstance(sym, str) or not _HYP_SYMBOL_RE.match(sym):
            raise ValueError(f"cf.symbol {sym!r} fails ^PF_[A-Z0-9]{{2,10}}USD$")
        w = _num(raw_cf.get("weeks", 20))
        if w is None or w != int(w) or not (4 <= w <= 104):
            raise ValueError(f"cf.weeks {raw_cf.get('weeks')!r} not an int in [4,104]")
        cf["pair"], cf["symbol"], cf["weeks"] = pair, sym, int(w)
    return cf


def sanitize_hypotheses(raw, intake_ts=None):
    """Validate raw hypotheses-file content into at most HYP_MAX_SLOTS configs.

    raw is the parsed file: {"hyp_<id>": {kind, family, cf, born_ts, origin,
    note, prereg}}. NEVER raises. Each survivor is an ORDINARY cf_only
    entrant: {id, cf_only=True, kind, family, cf, born_ts, origin, note,
    prereg}. born_ts is ALWAYS the intake time: an earlier value would let
    the rows that generated a hypothesis be the rows that grade it, and a
    later one would make the entrant unkillable (empty window, undefined
    rate). The file's value is only logged when it disagrees. Entries beyond
    the cap are dropped in file order with a log line.
    """
    out = []
    intake_ts = time.time() if intake_ts is None else float(intake_ts)
    try:
        if not isinstance(raw, dict):
            return out
        builtin_ids = {c["id"] for c in CHALLENGER_CONFIGS}
        for hid, e in raw.items():
            try:
                if not isinstance(hid, str) or not _HYP_ID_RE.match(hid):
                    raise ValueError(f"id {hid!r} fails ^hyp_[a-z0-9_]{{1,24}}$")
                if hid in builtin_ids:
                    raise ValueError(f"id {hid!r} shadows a built-in config")
                if not isinstance(e, dict):
                    raise ValueError("entry is not an object")
                kind = e.get("kind", "price")
                if kind not in HYP_KINDS:
                    raise ValueError(f"kind {kind!r} not in {HYP_KINDS}")
                fam = e.get("family")
                if fam not in HYP_FAMILIES:
                    raise ValueError(f"family {fam!r} not in {HYP_FAMILIES}")
                origin = e.get("origin")
                if origin not in HYP_ORIGINS:
                    raise ValueError(f"origin {origin!r} not in {HYP_ORIGINS}")
                note = e.get("note")
                if not isinstance(note, str) or not note.startswith("HYPOTHESIS:") \
                        or len(note.replace("HYPOTHESIS:", "", 1).strip()) < 10:
                    raise ValueError("note must start with 'HYPOTHESIS:' and state a mechanism")
                pre = e.get("prereg")
                if not isinstance(pre, dict):
                    raise ValueError("prereg block missing")
                missing = [k for k in HYP_PREREG_KEYS if k not in pre]
                if missing:
                    raise ValueError(f"prereg missing {missing}")
                for k in ("mechanism", "kill_bar", "cost_model"):
                    if not isinstance(pre[k], str) or not pre[k].strip():
                        raise ValueError(f"prereg.{k} must be a non-empty string")
                for k in ("expected_decisions_per_month", "mintrl_estimate_months"):
                    v = _num(pre[k])
                    if v is None or v <= 0:
                        raise ValueError(f"prereg.{k} must be a positive number")
                cf = _hyp_cf(kind, e.get("cf"))
                # BIRTH IS INTAKE TIME, clamped in BOTH directions.
                # Earlier is the obvious cheat: the rows that generated a
                # hypothesis would then also grade it. LATER is the quieter
                # one — a born_ts in the future leaves the scorer's
                # `bars since born` window empty and its decisions/month rate
                # undefined, so the entrant can never reach MinTRL and can
                # never be killed: an immortal slot squatter. The file's value
                # is therefore never adopted, only logged when it disagrees.
                born_raw = _num(e.get("born_ts"))
                born = intake_ts
                if born_raw is not None and abs(born_raw - intake_ts) > 1.0:
                    log("AUTOPILOT",
                        f"hypothesis {hid!r} born_ts {born_raw:.0f} ignored — "
                        f"re-stamped to intake {intake_ts:.0f}", "WRN")
                if len(out) >= HYP_MAX_SLOTS:
                    raise ValueError(f"slot cap {HYP_MAX_SLOTS} reached — dropped")
                out.append({
                    "id": hid, "cf_only": True, "kind": kind, "family": fam, "cf": cf,
                    "born_ts": float(born), "origin": origin,
                    "note": note[:_HYP_NOTE_MAX],
                    "prereg": {
                        "mechanism": str(pre["mechanism"])[:_HYP_NOTE_MAX],
                        "expected_decisions_per_month": _num(pre["expected_decisions_per_month"]),
                        "mintrl_estimate_months": _num(pre["mintrl_estimate_months"]),
                        "kill_bar": str(pre["kill_bar"])[:300],
                        "cost_model": str(pre["cost_model"])[:300],
                    },
                })
            except Exception as ex:
                log("AUTOPILOT", f"hypothesis {hid!r} dropped: {ex}", "WRN")
    except Exception as ex:
        log("AUTOPILOT", f"hypotheses sanitize failed: {ex}", "WRN")
        out = []
    return out


def _hyp_path():
    """The hypotheses handoff file — written by the PC research pass, read ONLY here."""
    return os.path.join(bs._DATA_DIR, "hypotheses.json")


def _read_hyp_file(intake_ts=None):
    """(sanitized configs, mtime) — ([], None) when absent. mtime captured before the read."""
    path = _hyp_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return [], None
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as ex:
        log("AUTOPILOT", f"hypotheses file unreadable: {ex}", "WRN")
        return [], mtime
    return sanitize_hypotheses(raw, intake_ts), mtime


def _hyp_sig(cfg):
    """Identity of a hypothesis's SCORED content — a change means a new trial."""
    return json.dumps({"kind": cfg.get("kind"), "cf": cfg.get("cf")}, sort_keys=True)


def kill_reason_code(score, nets=None, cluster_size=1):
    """Structured reason for a kill, from MEASURED numbers by fixed rules:
      cost             gross edge positive, net edge <= 0 (costs ate it)
      regime_flip      first half of the stream positive, second half negative
      no_signal        gross edge <= 0 / SR <= 0 (nothing there before costs)
      overlap_artifact edge exists but the entrant is a near-copy of a
                       sibling (cluster_size >= 2) — died to deflation only
      unknown          none of the above could be established
    Heuristic labels over measured artifacts; never LLM prose."""
    try:
        g = score.get("gross_edge")
        e = score.get("oos_edge")
        sr = score.get("sr")
        if g is not None and e is not None and g > 0 and e <= 0:
            return "cost"
        if nets and len(nets) >= 8:
            h = len(nets) // 2
            a, b = statistics.fmean(nets[:h]), statistics.fmean(nets[h:])
            if a > 0 and b < 0:
                return "regime_flip"
        if (g is not None and g <= 0) or (sr is not None and sr <= 0):
            return "no_signal"
        if cluster_size >= 2:
            return "overlap_artifact"
    except Exception:
        pass
    return "unknown"


def _state_path():
    return os.path.join(bs._DATA_DIR, "autopilot_state.json")


# ── Persisted enabled flag (survives `git reset --hard` + rebuild) ─────────────
# The flag lives in RUNTIME STATE only — the mounted data volume (autopilot_state.json
# in _DATA_DIR) AND the Postgres bot_state row id=2 — never in the code tree. A deploy
# that does `git reset --hard origin/main` + docker rebuild touches only the code tree;
# the ./data volume mount and the db container are untouched, so the flag persists.
# id=2 is used (id=1 is the bot's own paper_state) with the exact same upsert shape.
def _db_read_state():
    try:
        if bs.db.connected and bs.db.conn is not None:
            with bs.db.conn.cursor() as cur:
                cur.execute("SELECT data FROM bot_state WHERE id = 2")
                row = cur.fetchone()
                if row and row[0]:
                    return row[0] if isinstance(row[0], dict) else json.loads(row[0])
    except Exception:
        pass
    return None


def _file_read_state():
    try:
        with open(_state_path(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _db_write_state(state):
    try:
        if bs.db.connected and bs.db.conn is not None:
            with bs.db.conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO bot_state (id, data, updated_at)
                    VALUES (2, %s, %s)
                    ON CONFLICT (id) DO UPDATE
                      SET data = EXCLUDED.data, updated_at = EXCLUDED.updated_at
                """, (json.dumps(state), time.time()))
        return True
    except Exception as e:
        log("AUTOPILOT", f"db write (id=2) failed: {e}", "WRN")
        return False


def _file_write_state(state):
    try:
        with open(_state_path(), "w", encoding="utf-8") as f:
            json.dump(state, f)
        return True
    except Exception as e:
        log("AUTOPILOT", f"state save failed: {e}", "ERR")
        return False


def autopilot_persisted_enabled():
    """True if the autopilot was left ON. Reads DB (id=2) first, falls back to the
    json file in the data volume, defaults False on a fresh install."""
    d = _db_read_state()
    if d is None:
        d = _file_read_state()
    if not isinstance(d, dict):
        return False
    return bool(d.get("enabled", False))


def autopilot_persisted_state():
    """Tri-state view of the persisted flag: True/False when the owner has an
    EXPLICIT recorded choice (button toggle, or a running instance's periodic
    save), or None when no choice was ever recorded (fresh install).

    Boot uses this so an explicit on/off choice always wins and survives a
    redeploy, while env AUTOPILOT is treated as the first-boot DEFAULT only.
    Never raises — any read error is treated as 'no recorded choice' (None)."""
    try:
        d = _db_read_state()
        if d is None:
            d = _file_read_state()
        if isinstance(d, dict) and "enabled" in d:
            return bool(d["enabled"])
    except Exception:
        pass
    return None


def autopilot_set_persisted_enabled(b):
    """Dual-write the enabled flag to BOTH the DB (id=2) and the json file, merging
    into any existing state so the champion/audit fields are preserved."""
    b = bool(b)
    d = _db_read_state()
    if d is None:
        d = _file_read_state()
    if not isinstance(d, dict):
        d = {}
    d["enabled"] = b
    d["updated_at"] = time.time()
    _db_write_state(d)
    _file_write_state(d)
    return b


class Autopilot:
    def __init__(self):
        # SANDBOX assertion #1 — never construct while live orders are possible.
        if bs.is_live():
            raise RuntimeError("Autopilot refuses to start while is_live() is True")

        self.configs = {}
        self.order   = []
        self.champion_id = None            # None == FLAT (honest default)
        self.allocation  = "FLAT"
        self.last_switch = {"ts": 0.0, "from": None, "to": None, "why": "init"}
        self.decision_log = []             # rolling list of {ts, champion, allocation, why}
        self.scores = {}                   # id -> last score dict
        self._last_alert_ts = 0.0
        # N-trials accounting (monotone) + the kill graveyard, both persisted.
        self.trials_count = TRIALS_SEED    # every entrant EVER tried (see TRIALS_SEED doc)
        self.trials_ids   = []             # post-seed ids already counted
        self.killed       = {}             # cid -> {ts, reason, config, final_score, +graveyard keys}
        # Goal ledgers (persisted): proven ids, ids that ever cleared the cost
        # gate (autopilot_clears fires once), hypothesis intake memory
        # (born_ts + scored-content signature per hyp id), last measured
        # hurdle numbers so goal() can answer before the first score().
        self.proven       = []
        self.cleared_ids  = []
        self.hyp_seen     = {}             # hyp id -> {"born_ts", "sig"}
        self.n_eff = self.n_clusters = None
        self.sd_sr = self.sr0 = None
        self._last_nets = {}               # transient: id -> nets (for kill reason codes)
        self._last_clusters = []           # transient: [[ids]] from the last score()
        self._hyp_refused = set()          # budget refusals already announced this process
        # A live instance is by definition enabled; persisted on every _save so the
        # flag survives a redeploy (see autopilot_persisted_enabled / _set...).
        self.enabled = True

        # Build one force_paper challenger per config. no_persist=True keeps them
        # entirely in-memory and out of the dual-write path (id=1 / paper_state.json).
        self.traders = {}
        self.engines = {}                  # id -> {pair -> SignalEngine}
        self.last_sig = {}                 # id -> {pair -> last signal}
        for cfg in CHALLENGER_CONFIGS:
            self._add_challenger(cfg)

        # Restore the decision pointer + ledgers FIRST: hypothesis intake below
        # needs hyp_seen (born_ts memory) and the persisted scores (var_sr for
        # the budget check). The champion heal runs after the full pool exists.
        self._load()

        # Lab intake: append this cycle's nominations AFTER the 6 built-ins so the
        # built-ins keep their identities/positions no matter what the lab wrote.
        # _lab_check_ts=0 lets the first decide() re-stat the file immediately (a
        # no-op unless research_lab wrote between now and then — mtime is cached).
        self._lab_check_ts = 0.0
        lab_cfgs, self._lab_mtime = _read_lab_file()
        for cfg in lab_cfgs:
            self._add_challenger(cfg)
            log("AUTOPILOT", f"lab challenger loaded: {cfg['id']}")

        # Hypothesis intake (contract [H]) — same cadence + diff logic as the lab.
        self._hyp_check_ts = 0.0
        self._hyp_mtime = None
        self._hyp_apply(*_read_hyp_file(time.time()), boot=True)

        self._heal_champion()
        # After restore: count any config id the persisted trials ledger has
        # never seen (first boot with a new written hypothesis lands here).
        self._register_trials()

    def _add_challenger(self, cfg):
        """Register one config (built-in or lab) with its own sandboxed trader.

        The ONE place a challenger enters the pool, so the sandbox invariant
        (force_paper + no_persist + the _is_live assert) is enforced identically
        for lab nominations and built-ins — a lab entry gets zero extra powers.
        """
        cid = cfg["id"]
        if cfg.get("cf_only"):
            # Scored purely from the recorded signal stream — no live sandbox
            # trader, no engines. It exists in configs/order so it appears in
            # standings and can be crowned like anyone else.
            self.configs[cid] = cfg
            if cid not in self.order:
                self.order.append(cid)
            return
        # Each live challenger persists its OWN book (the sim's state_path
        # mechanism). Without this, every deploy wiped all challenger trades —
        # and this pipeline deploys on every push, so no challenger could ever
        # mathematically reach MIN_OOS_TRADES. The tournament ran forever and
        # could never conclude.
        t = bs.PaperTrader(force_paper=True, no_persist=True,
                           start_balance=CHALLENGER_START,
                           state_path=os.path.join(bs._DATA_DIR, f"ap_{cid}.json"))
        if cfg.get("risk_min") is not None:
            t._force_risk_min = cfg["risk_min"]
        if cfg.get("risk_max") is not None:
            t._force_risk_max = cfg["risk_max"]
        # Hard belt-and-suspenders: a force_paper trader must never be live.
        assert not t._is_live(), f"challenger {cid} unexpectedly live"
        self.configs[cid] = cfg
        if cid not in self.order:
            self.order.append(cid)
        self.traders[cid]  = t
        self.engines[cid]  = {}
        self.last_sig[cid] = {}

    def _retire_challenger(self, cid):
        try:
            _f = os.path.join(bs._DATA_DIR, f"ap_{cid}.json")
            if os.path.exists(_f):
                os.remove(_f)
        except Exception:
            pass
        """Drop one config from every per-id structure; heal the champion pointer.

        Touches ONLY the retired id's entries — every other challenger keeps its
        trader object (and therefore its in-memory trade history) untouched. If
        the retiree was champion, fall back to FLAT EXPLICITLY here rather than
        relying on the next decide() to notice the dangling id.
        """
        for d in (self.configs, self.traders, self.engines, self.last_sig, self.scores):
            d.pop(cid, None)
        if cid in self.order:
            self.order.remove(cid)
        if self.champion_id == cid:
            self.champion_id = None
            self.allocation  = "FLAT"
            self.last_switch = {"ts": time.time(), "from": cid, "to": None,
                                "why": f"champion {cid} retired from pool -> FLAT"}
            log("AUTOPILOT", f"champion {cid} retired from pool -> FLAT", "WRN")

    def refresh_lab_configs(self):
        """Hot-swap lab slots from the nomination file. Throttled + mtime-gated.

        Called from decide(); at most one os.path.getmtime per LAB_REFRESH_SECS,
        and the file is re-read/re-sanitized only when its mtime moved. Diff logic:
        new lab ids are ADDED with a fresh trader/engine; ids gone from the file
        are RETIRED (via _retire_challenger, which also heals the champion); an id
        present in both but with CHANGED levers is retired-and-readded so its score
        history can't mix two lever sets. Built-ins are never in the diff.
        """
        now = time.time()
        if now - self._lab_check_ts < LAB_REFRESH_SECS:
            return
        self._lab_check_ts = now
        try:
            mtime = os.path.getmtime(_lab_path())
        except OSError:
            mtime = None
        if mtime == self._lab_mtime:
            return
        cfgs, self._lab_mtime = _read_lab_file()
        want = {c["id"]: c for c in cfgs}
        have = [cid for cid in self.order if cid.startswith("lab_")]
        for cid in have:
            if cid not in want:
                self._retire_challenger(cid)
                log("AUTOPILOT", f"lab challenger retired: {cid}")
            elif any(want[cid].get(k) != self.configs[cid].get(k)
                     for k in _LAB_LEVER_KEYS):
                self._retire_challenger(cid)
                self._add_challenger(want[cid])
                log("AUTOPILOT", f"lab challenger re-nominated with new levers — fresh start: {cid}")
            else:
                self.configs[cid] = want[cid]   # same levers: refresh note/born_ts only
        for cid, cfg in want.items():
            if cid not in self.configs:
                self._add_challenger(cfg)
                log("AUTOPILOT", f"lab challenger added: {cid}")

    def _heal_champion(self):
        """A persisted champion outside the current pool can never gate the
        real book — force FLAT now rather than at the next decide()."""
        if self.champion_id is not None and self.champion_id not in self.configs:
            log("AUTOPILOT", f"restored champion {self.champion_id} not in pool -> FLAT", "WRN")
            self.champion_id = None
            self.allocation  = "FLAT"

    def _current_var_sr(self):
        """Cross-entrant Var[SR] from the last scores (persisted) — None if <2."""
        srs = [s.get("sr") for s in (self.scores or {}).values()
               if isinstance(s, dict) and s.get("sr") is not None]
        return statistics.pvariance(srs) if len(srs) >= 2 else None

    def budget_remaining(self, decisions_per_year=None):
        """Contract [G] budget_remaining: how many more families are honestly
        registrable (research_loop.budget_remaining). decisions_per_year
        defaults to the SLOWEST cadence among live families (conservative).
        None = unknown (no cross-entrant variance yet, or research_loop missing)."""
        if rl is None:
            return None
        if decisions_per_year is None:
            rates = [decisions_per_year_of(c) for cid, c in self.configs.items()
                     if cid not in self.killed]
            decisions_per_year = min(rates) if rates else 52
        return rl.budget_remaining(self._current_var_sr(), self.trials_count,
                                   decisions_per_year)

    def _hyp_apply(self, cfgs, mtime, boot=False):
        """Diff sanitized hypothesis configs against the pool (lab-style):
        removed ids retire; a changed scored-content signature retires +
        re-adds with a fresh born_ts (a new trial under the same name is
        re-counted in the ledger); NEW ids pass the registration budget or
        are refused (logged + SSE autopilot_budget_exhausted). Known ids keep
        their persisted born_ts — the file can never move a birth earlier."""
        self._hyp_mtime = mtime
        now = time.time()
        want = {c["id"]: c for c in cfgs}
        have = [cid for cid in self.order if cid.startswith("hyp_")]
        for cid in have:
            if cid not in want:
                self._retire_challenger(cid)
                log("AUTOPILOT", f"hypothesis retired (gone from file): {cid}")
        for cid, cfg in want.items():
            sig = _hyp_sig(cfg)
            seen = self.hyp_seen.get(cid) if isinstance(self.hyp_seen, dict) else None
            if seen and seen.get("sig") == sig:
                # known, unchanged: the persisted birth wins — the file can
                # never move it earlier, and a reload never moves it later
                # (that would keep erasing the record the entrant has earned).
                try:
                    cfg["born_ts"] = float(seen.get("born_ts") or cfg["born_ts"])
                except Exception:
                    pass
                if cid in self.configs:
                    self.configs[cid] = cfg          # refresh note/prereg only
                else:
                    self._add_challenger(cfg)        # restart: re-enter the pool
                continue
            if seen and seen.get("sig") != sig:
                # changed hypothesis under the same id = a NEW trial: fresh
                # start, fresh birth, re-counted in the monotone ledger.
                if cid in self.configs:
                    self._retire_challenger(cid)
                if cid in self.trials_ids:
                    self.trials_ids.remove(cid)
                cfg["born_ts"] = max(float(cfg["born_ts"]), now)
                self.hyp_seen[cid] = {"born_ts": cfg["born_ts"], "sig": sig}
                self._add_challenger(cfg)
                log("AUTOPILOT", f"hypothesis re-registered with new content — fresh start: {cid}")
                continue
            # brand new id: budget gate
            dpy = decisions_per_year_of(cfg)
            pre = cfg.get("prereg") or {}
            if pre.get("expected_decisions_per_month"):
                dpy = min(dpy, float(pre["expected_decisions_per_month"]) * 12.0)
            chk = (rl.registration_check(self._current_var_sr(), self.trials_count, dpy)
                   if rl else {"allowed": True, "budget_remaining": None, "sr0": None,
                               "reason": "research_loop missing — budget unknown"})
            if not chk.get("allowed", True):
                if cid not in self._hyp_refused:
                    self._hyp_refused.add(cid)
                    log("AUTOPILOT", f"hypothesis {cid} REFUSED: {chk.get('reason')}", "WRN")
                    self._push("autopilot_budget_exhausted",
                               {"entrant": cid, "trials_count": int(self.trials_count),
                                "sr0": chk.get("sr0"), "reason": chk.get("reason")})
                continue
            self.hyp_seen[cid] = {"born_ts": cfg["born_ts"], "sig": sig}
            self._add_challenger(cfg)
            log("AUTOPILOT", f"hypothesis registered: {cid} ({cfg.get('family')}, "
                             f"{cfg.get('origin')}) — {chk.get('reason')}")

    def refresh_hypotheses(self):
        """Hot-swap hypothesis slots from hypotheses.json — the lab's cadence
        (LAB_REFRESH_SECS throttle) and its mtime gate."""
        now = time.time()
        if now - self._hyp_check_ts < LAB_REFRESH_SECS:
            return
        self._hyp_check_ts = now
        try:
            mtime = os.path.getmtime(_hyp_path())
        except OSError:
            mtime = None
        if mtime == self._hyp_mtime:
            return
        self._hyp_apply(*_read_hyp_file(now))

    def _push(self, event_type, payload):
        """SSE + assistant spine, best-effort: a sink hiccup never touches state."""
        try:
            bs._push_sse(event_type, payload)
        except Exception:
            pass

    # ── introspection ─────────────────────────────────────────────────────────
    def n_configs(self):
        return len(self.configs)

    def _paper_ok(self):
        """SANDBOX assertion — call before any allocation. Forces FLAT if live."""
        if bs.is_live():
            self.champion_id = None
            self.allocation  = "FLAT"
            now = time.time()
            if now - self._last_alert_ts > 300:
                self._last_alert_ts = now
                log("AUTOPILOT", "is_live() is True — allocation FORCED FLAT, no config allocated", "ERR")
                try:
                    bs.tg("Autopilot: is_live() TRUE — allocation forced FLAT (no real orders).", plain=True)
                except Exception:
                    pass
            return False
        return True

    # ── driving the challengers (mirrors how trading_loop drives _sim_trader) ──
    def manage_positions(self):
        """Manage every open challenger position (mirror of the sim-manage block)."""
        for cid, t in self.traders.items():
            for pair in list(t.positions.keys()):
                p = t.positions.get(pair)
                if not p:
                    continue
                try:
                    price = bs.get_price(pair)
                    try:
                        c, h, l, _, _ = bs.get_klines(pair)
                        atr = bs.calc_atr(h, l, c)
                    except Exception:
                        atr = None
                    t.on_signal("HOLD", price, 0, 0, p["name"], 0.0, pair, atr=atr)
                except Exception as e:
                    log("AUTOPILOT", f"{cid} manage {pair}: {e}", "ERR")
            # Keep the experiment alive: a bankrupt paper challenger stops trading
            # (on_signal floors at PAPER_FLOOR), which starves its OOS sample. Reset
            # it — paper only, force_paper guarantees no exchange contact.
            if t.balance < bs.PAPER_FLOOR:
                log("AUTOPILOT", f"{cid} paper bankroll reset ${t.balance:.2f} -> ${CHALLENGER_START:.0f}", "WRN")
                t.balance = CHALLENGER_START
                t.peak = CHALLENGER_START
                t.positions = {}

    def scan(self, pair, coin, closes, highs, lows, volumes, opens, price, atr, signal_ts=None):
        """Evaluate `pair` for each challenger (mirror of the sim-scan block).

        Uses the SHARED SignalEngine per (config, pair); the only differences
        between configs are the Tier-A levers applied here before on_signal.
        """
        for cid, t in self.traders.items():
            try:
                if pair in t.positions or not t.can_open_new():
                    continue
                cfg = self.configs[cid]
                emap = self.engines[cid]
                if pair not in emap:
                    emap[pair] = bs.SignalEngine()
                eng = emap[pair]
                sig, plan, _, _, conf = eng.evaluate(
                    closes, highs, lows, volumes, price, coin["alert_buffer"],
                    pair=pair, opens=opens)

                # Pullback + momentum filters — identical to the _sim_trader path.
                if sig in ("BUY", "SELL") and len(closes) >= 2:
                    try:
                        if bs.detect_regime(closes, highs, lows) == "TRENDING":
                            if sig == "BUY"  and closes[-1] >= closes[-2]: sig = "HOLD"
                            elif sig == "SELL" and closes[-1] <= closes[-2]: sig = "HOLD"
                    except Exception:
                        pass
                if sig in ("BUY", "SELL") and len(closes) >= 16:
                    try:
                        m5, m15 = closes[-1] - closes[-6], closes[-1] - closes[-16]
                        if sig == "BUY"  and m5 <= 0 and m15 <= 0: sig = "HOLD"
                        elif sig == "SELL" and m5 >= 0 and m15 >= 0: sig = "HOLD"
                    except Exception:
                        pass

                last = self.last_sig[cid].get(pair)
                floor = max(ATTEMPT_CONF_MIN, cfg.get("entry_conf_floor", ATTEMPT_CONF_MIN))
                if sig != last and sig in ("BUY", "SELL") and conf >= floor:
                    stop   = plan.get("stop", price * 0.985 if sig == "BUY" else price * 1.015)
                    target = plan.get("exit", price * 1.030 if sig == "BUY" else price * 0.970)
                    fkey    = plan.get("fkey", "")
                    pillars = plan.get("pillars", {})
                    if atr and atr > 0:
                        r = (price - stop)   if sig == "BUY" else (stop - price)
                        w = (target - price) if sig == "BUY" else (price - target)
                        if r <= 0 or w / max(r, 1e-12) < bs.MIN_RR_RATIO:
                            stop   = round((price - atr * bs.ATR_MULTIPLIER)       if sig == "BUY" else (price + atr * bs.ATR_MULTIPLIER),       8)
                            target = round((price + atr * bs.ATR_MULTIPLIER * 2.2) if sig == "BUY" else (price - atr * bs.ATR_MULTIPLIER * 2.2), 8)

                    # Lever: minimum reward:risk for this config.
                    min_rr = cfg.get("min_rr")
                    if min_rr is not None:
                        r = (price - stop)   if sig == "BUY" else (stop - price)
                        w = (target - price) if sig == "BUY" else (price - target)
                        if r <= 0 or (w / r) < min_rr:
                            self.last_sig[cid][pair] = sig
                            continue

                    # Lever: allowed-strategy subset (via the bot's own classifier).
                    allowed = cfg.get("allowed_strategies")
                    if allowed is not None:
                        skey, _ = bs._classify_strategy(sig, pillars or {}, conf, pair)
                        if skey not in allowed:
                            self.last_sig[cid][pair] = sig
                            continue

                    t.on_signal(sig, price, stop, target, coin["name"], conf, pair,
                                atr=atr, fkey=fkey, pillars=pillars, signal_ts=signal_ts)
                self.last_sig[cid][pair] = sig
            except Exception as e:
                log("AUTOPILOT", f"{cid} scan {pair}: {e}", "ERR")

    # ── scoring (find_signal.py discipline) ────────────────────────────────────
    @staticmethod
    def _trade_returns(trades):
        """Per-trade (ts, net_return, gross_move) from realised paper trades.

        gross_move = signed price move (exit already carries slippage). The paper
        close books pnl = notional*(move - fee_rate), so net_return = move - fee_rate
        is exactly pnl/notional — scale-free and already net of fees + slippage.
        """
        out = []
        for tr in trades:
            try:
                entry = float(tr.get("entry", 0) or 0)
                exit_ = float(tr.get("exit", 0) or 0)
                if entry <= 0:
                    continue
                move = (exit_ - entry) / entry
                if tr.get("side") == "SHORT":
                    move = -move
                out.append((float(tr.get("ts", 0) or 0), move - _FEE_RATE, move))
            except Exception:
                continue
        out.sort(key=lambda x: x[0])
        return out

    @staticmethod
    def _stats(rows):
        """(n, edge_net, t, gross_edge) over a set of (ts, net, gross) rows."""
        n = len(rows)
        if n < 2:
            return n, None, None, None
        net = [r[1] for r in rows]
        gross = [r[2] for r in rows]
        edge = statistics.fmean(net)
        sd = statistics.pstdev(net)
        if sd < 1e-12:
            return n, edge, None, statistics.fmean(gross)
        t = edge / (sd / math.sqrt(n))
        return n, edge, t, statistics.fmean(gross)

    def score_counterfactual(self, cfg):
        """Score one config against RECORDED history. Dispatches on cfg['kind'].

        kind='price'  (default) — today's path: the shadow_signals stream.
        kind='trend'  — weekly long-flat rules over stored hourly candles.
        kind='carry'  — funding-carry over the funding_rates contract table.
        kind='switch' — weekly allocator between the carry and trend sleeves.
        The seam exists so multi-leg / weekly entrants never have to fake
        themselves into the per-signal price path. Every branch returns either
        None (nothing scorable) or a dict with at least
        {n_oos, oos_edge, t, gross_edge, clears_cost, via, nets} — nets being
        the per-decision NET return stream the DSR machinery runs on.
        """
        kind = cfg.get("kind", "price")
        if kind == "price":
            return self._score_cf_price(cfg)
        if kind == "trend":
            return self._score_cf_trend(cfg)
        if kind == "carry":
            return self._score_cf_carry(cfg)
        if kind == "switch":
            return self._score_cf_switch(cfg)
        log("AUTOPILOT", f"cf score {cfg.get('id')}: unknown kind {kind!r}", "WRN")
        return None

    def _score_cf_price(self, cfg):
        """Score one config against the RECORDED signal stream.

        The live sandbox books starve: challengers only trade when the main
        pipeline fires (~1-2/day), so MIN_OOS_TRADES took months even before
        deploys wiped the books. But shadow_signals records EVERY evaluated
        signal with 6/24/48h (and, once backfilled, 168h) outcomes — a config
        with expressible filters can be scored against all of them.

        Honesty terms, stated: fills are the recorded signal price (optimistic
        — no queue, no spread), the exit is a FIXED horizon (fwdN), and rows
        are non-overlapping per pair at that horizon (overlap inflated t ~3x
        elsewhere in this project). Costs are charged in full. Rows before
        AP_CF_EPOCH (or the config's own birth, if later) never count — a
        config suggested by an audit over past rows is graded only on rows it
        has never seen. fwd168 is in the whitelist but the column ships from
        another workstream: until it exists, the SELECT fails, is caught, and
        the config is simply not-ready (None) — never a crash, never a number.
        spec['weekly']=True additionally enforces at most ONE scored decision
        per ISO week (across all pairs) for weekly-cadence hypotheses.
        """
        spec = cfg.get("cf")
        if not spec or not bs.db.connected:
            return None
        horizon = spec.get("horizon", "fwd48")
        if horizon not in ("fwd6", "fwd24", "fwd48", "fwd168"):
            return None
        born = max(AP_CF_EPOCH, float(cfg.get("born_ts") or 0))
        try:
            with bs.db.conn.cursor() as cur:
                cur.execute(f"""SELECT ts, pair, sig, conf, adx, er, rr_net, {horizon}
                               FROM shadow_signals
                               WHERE fwd_done=1 AND {horizon} IS NOT NULL
                                 AND sig IN ('BUY','SELL') AND ts > %s
                               ORDER BY pair, ts""", (born,))
                rows = cur.fetchall()
        except Exception as e:
            log("AUTOPILOT", f"cf score {cfg['id']}: {e}", "WRN")
            return None
        hor_s = {"fwd6": 6, "fwd24": 24, "fwd48": 48, "fwd168": 168}[horizon] * 3600
        weekly = bool(spec.get("weekly"))
        nets, last_kept, weeks_seen = [], {}, set()
        for ts, pair, sig, conf, adx, er, rr_net, fwd in rows:
            if spec.get("conf") is not None and (conf is None or float(conf) < spec["conf"]):
                continue
            if spec.get("adx") is not None and (adx is None or float(adx) < spec["adx"]):
                continue
            if spec.get("er") is not None and (er is None or float(er) < spec["er"]):
                continue
            if spec.get("rr") is not None and (rr_net is None or float(rr_net) < spec["rr"]):
                continue
            lk = last_kept.get(pair)
            if lk is not None and float(ts) - lk < hor_s:
                continue                       # overlapping forward window
            if weekly:
                wk = _iso_week_key(ts)
                if wk in weeks_seen:
                    continue                   # max one scored decision per ISO week
                weeks_seen.add(wk)
            last_kept[pair] = float(ts)
            gross = float(fwd) if sig == "BUY" else -float(fwd)
            nets.append((gross - bs.ROUND_TRIP_COST_PCT, gross, float(ts)))
        return self._cf_result(cfg, [x[0] for x in nets], [x[1] for x in nets],
                               via="cf", gross_bar=bs.ROUND_TRIP_COST_PCT,
                               ts=[x[2] for x in nets])

    @staticmethod
    def _cf_result(cfg, net, gross_l, via, gross_bar=None, extra=None, ts=None):
        """Common tail for every cf path: edge/t/clears + the nets stream.
        `ts` (optional, same length as net) = the decision timestamps, so
        streams of different entrants can be ALIGNED for N_eff clustering."""
        n = len(net)
        if n < 2:
            out = {"n_oos": n, "oos_edge": None, "t": None, "gross_edge": None,
                   "clears_cost": False, "via": via, "nets": list(net)}
        else:
            edge = statistics.fmean(net)
            sd = statistics.pstdev(net)
            t = (edge / (sd / math.sqrt(n))) if sd > 1e-12 else None
            g = statistics.fmean(gross_l) if gross_l else None
            clears = (n >= MIN_OOS_TRADES and t is not None and edge > 0
                      and t >= T_MARGIN
                      and (gross_bar is None or (g is not None and g > gross_bar)))
            out = {"n_oos": n, "oos_edge": edge, "t": t, "gross_edge": g,
                   "clears_cost": bool(clears), "via": via, "nets": list(net)}
        out["net_ts"] = list(ts) if (ts is not None and len(ts) == n) else None
        if extra:
            out.update(extra)
        return out

    # ── data pulls for the weekly/carry paths (contract tables; may be empty) ──
    def _fetch_hourly_candles(self, pair):
        """[(ts, high, low, close)] hourly bars from the candles table, or None.

        History BEFORE born_ts is deliberately included: indicator warm-up
        (SMA/channel lookback) is not grading — only decisions after born are
        ever scored."""
        if not bs.db.connected:
            return None
        try:
            with bs.db.conn.cursor() as cur:
                cur.execute("""SELECT ts, high, low, close FROM candles
                               WHERE pair=%s AND interval_m=60 ORDER BY ts""",
                            (pair,))
                return cur.fetchall()
        except Exception as e:
            log("AUTOPILOT", f"cf candles {pair}: {e}", "WRN")
            return None

    def _fetch_funding(self, symbol):
        """[(ts, rate)] hourly funding from the funding_rates CONTRACT table.

        The table ships from another workstream (venue TEXT, symbol TEXT,
        ts BIGINT epoch-seconds, rate FLOAT). Missing table or empty history
        returns None — callers surface 'waiting on funding history', they do
        not invent a number. Deduped by ts (first venue wins) so a second
        venue could never silently double-count accrual."""
        if not bs.db.connected:
            return None
        try:
            with bs.db.conn.cursor() as cur:
                cur.execute("""SELECT ts, rate FROM funding_rates
                               WHERE symbol=%s ORDER BY ts""", (symbol,))
                rows = cur.fetchall()
        except Exception as e:
            log("AUTOPILOT", f"cf funding {symbol}: {e}", "WRN")
            return None
        out, seen = [], set()
        for ts, rate in rows:
            try:
                ts = int(float(ts))
                if ts in seen or rate is None:
                    continue
                seen.add(ts)
                out.append((ts, float(rate)))
            except Exception:
                continue
        return out or None

    def _score_cf_trend(self, cfg):
        """Weekly long-flat rules (tsmom / donchian) over stored hourly candles.

        One decision per ISO week (weekly_closes_from_daily enforces it by
        construction); the decision at week i is graded on week i+1's close-to-
        close return — fwd168, never the bar the decision saw. Costs: the
        honest SPOT round trip (taker + slippage each way, CF_SPOT_RT), half
        charged on the entry decision's scored week and half on the exit's.
        Every completed post-birth week is a scored decision (flat weeks score
        0) — the entrant is graded as an allocation, not on cherry-picked
        long weeks. Insufficient candle history is reported, not papered over.
        """
        spec = cfg.get("cf") or {}
        hourly = self._fetch_hourly_candles(spec.get("pair"))
        if not hourly:
            return self._cf_result(cfg, [], [], via="cf_trend",
                                   extra={"status": "waiting on price history"})
        daily = daily_bars_from_hourly(hourly)
        weekly = weekly_closes_from_daily(daily)
        rule = spec.get("rule", "tsmom")
        if rule == "donchian":
            decisions = donchian_decisions(daily, weekly,
                                           int(spec.get("enter_days", 50)),
                                           int(spec.get("exit_days", 25)))
        else:
            decisions = tsmom_decisions(weekly, int(spec.get("weeks", 20)))
        if not decisions:
            need = (spec.get("enter_days", 50) if rule == "donchian"
                    else spec.get("weeks", 20))
            return self._cf_result(cfg, [], [], via="cf_trend",
                                   extra={"status": f"waiting on price history "
                                                    f"(warm-up {need} {'days' if rule=='donchian' else 'weeks'} not met)"})
        born = max(AP_CF_EPOCH, float(cfg.get("born_ts") or 0))
        # weekly close lookup by decision_ts for the forward return pairing
        wk_by_ts = {w[1]: (i, w[2]) for i, w in enumerate(weekly)}
        net, gross_l, tss = [], [], []
        prev_pos = 0
        for dts, pos in decisions:
            idx, close = wk_by_ts[dts]
            entered = pos == 1 and prev_pos == 0
            exited = pos == 0 and prev_pos == 1
            prev_pos = pos
            if dts <= born:
                continue                        # pre-registration rule
            if idx + 1 >= len(weekly):
                continue                        # forward week not complete yet
            fwd = weekly[idx + 1][2] / close - 1.0
            g = fwd if pos == 1 else 0.0
            cost = (CF_SPOT_RT / 2.0) if (entered or exited) else 0.0
            net.append(g - cost)
            gross_l.append(g)
            tss.append(float(dts))
        return self._cf_result(cfg, net, gross_l, via="cf_trend", ts=tss)

    def _score_cf_carry(self, cfg):
        """Counterfactual funding-carry pair (long spot + short perp), marked daily.

        Decision each UTC day: enter when trailing 7d ANNUALIZED funding >
        CARRY_HURDLE_ANN (4x the 4-leg RT cost + 10% floor), exit when it
        drops below hurdle/2 or turns negative. A day's mark while deployed is
        that day's summed hourly funding accrual (shorts RECEIVE positive
        funding); entry charges the two opening legs, exit the two closing
        legs (CARRY_RT_4LEG/2 each). The trailing window must hold >= 120 of
        168 hourly rates to decide at all — thin data holds state, it never
        fabricates a decision. Also tracks the worst consecutive negative-
        funding streak (days, while deployed). Empty/missing funding_rates ->
        honest status 'waiting on funding history'.
        """
        spec = cfg.get("cf") or {}
        rates = self._fetch_funding(spec.get("symbol", "PF_XBTUSD"))
        if not rates:
            return self._cf_result(cfg, [], [], via="cf_carry",
                                   extra={"status": "waiting on funding history"})
        born = max(AP_CF_EPOCH, float(cfg.get("born_ts") or 0))
        # hurdle_mult (hypothesis lever, sanitizer-clamped to [1,4]) can only
        # RAISE the entry hurdle above the built-in CARRY_HURDLE_ANN.
        hurdle = CARRY_HURDLE_ANN * max(1.0, float(spec.get("hurdle_mult", 1.0) or 1.0))
        by_hour = dict(rates)
        first_day = (min(by_hour) // 86400) * 86400
        last_day = (max(by_hour) // 86400) * 86400   # last day may be partial: excluded
        ts_sorted = sorted(by_hour)
        net, gross_l, tss = [], [], []
        in_pos = False
        neg_run = worst_neg = 0
        day = first_day + 86400
        i_lo = 0
        while day < last_day:
            # trailing 7d window [day-7d, day)
            window = [by_hour[t] for t in ts_sorted
                      if day - SECS_PER_WEEK <= t < day]
            entered = exited = False
            if len(window) >= CARRY_MIN_7D_HOURS:
                ann = statistics.fmean(window) * 24.0 * 365.0
                if not in_pos and ann > hurdle:
                    in_pos, entered = True, True
                elif in_pos and (ann < hurdle / 2.0 or ann < 0.0):
                    in_pos, exited = False, True
            if day > born:
                if exited:
                    net.append(-CARRY_RT_4LEG / 2.0)
                    gross_l.append(0.0)
                    tss.append(float(day))
                elif in_pos:
                    accr = sum(by_hour[t] for t in ts_sorted if day <= t < day + 86400)
                    cost = (CARRY_RT_4LEG / 2.0) if entered else 0.0
                    net.append(accr - cost)
                    gross_l.append(accr)
                    tss.append(float(day))
                    if accr < 0:
                        neg_run += 1
                        worst_neg = max(worst_neg, neg_run)
                    else:
                        neg_run = 0
            day += 86400
        return self._cf_result(cfg, net, gross_l, via="cf_carry", ts=tss,
                               extra={"worst_neg_funding_days": worst_neg,
                                      "hurdle_ann": hurdle})

    def _score_cf_switch(self, cfg):
        """Weekly allocator: carry_harvest's condition if live, else
        tsmom_btc_20w's if live, else flat — this entrant scores the SWITCH
        itself, not either sleeve. Costs are charged on every allocation
        change (each sleeve's own entry/exit legs). Every completed post-birth
        week is one scored decision; flat weeks score 0. Missing funding
        history degrades honestly: the carry condition is treated as NOT live
        (and said so in status); missing price history means nothing is
        scorable at all.
        """
        spec = cfg.get("cf") or {}
        hourly = self._fetch_hourly_candles(spec.get("pair", "XBTUSD"))
        if not hourly:
            return self._cf_result(cfg, [], [], via="cf_switch",
                                   extra={"status": "waiting on price history"})
        rates = self._fetch_funding(spec.get("symbol", "PF_XBTUSD"))
        note = None if rates else "funding history missing — carry leg treated as not live"
        by_hour = dict(rates) if rates else {}
        ts_sorted = sorted(by_hour)
        daily = daily_bars_from_hourly(hourly)
        weekly = weekly_closes_from_daily(daily)
        tsm = dict(tsmom_decisions(weekly, int(spec.get("weeks", 20))))  # dts -> pos
        born = max(AP_CF_EPOCH, float(cfg.get("born_ts") or 0))
        net, gross_l, tss = [], [], []
        prev_alloc = "flat"
        for i, (_key, dts, close, _di) in enumerate(weekly):
            if i + 1 >= len(weekly):
                break                          # forward week not complete
            # carry condition at the decision point: trailing 7d ann funding
            carry_live = False
            window = [by_hour[t] for t in ts_sorted if dts - SECS_PER_WEEK <= t < dts]
            if len(window) >= CARRY_MIN_7D_HOURS:
                carry_live = statistics.fmean(window) * 24.0 * 365.0 > CARRY_HURDLE_ANN
            trend_live = bool(tsm.get(dts, 0))
            alloc = "carry" if carry_live else ("trend" if trend_live else "flat")
            cost = 0.0
            if alloc != prev_alloc:
                # unwind the old sleeve's legs + open the new sleeve's legs
                for leg in (prev_alloc, alloc):
                    if leg == "carry":
                        cost += CARRY_RT_4LEG / 2.0
                    elif leg == "trend":
                        cost += CF_SPOT_RT / 2.0
            prev_alloc = alloc
            if dts <= born:
                continue                       # pre-registration rule
            nxt = weekly[i + 1]
            if alloc == "carry":
                g = sum(by_hour[t] for t in ts_sorted if dts <= t < nxt[1])
            elif alloc == "trend":
                g = nxt[2] / close - 1.0
            else:
                g = 0.0
            net.append(g - cost)
            gross_l.append(g)
            tss.append(float(dts))
        return self._cf_result(cfg, net, gross_l, via="cf_switch", ts=tss,
                               extra=({"status": note} if note else None))

    def score(self):
        """Per-config OOS net-of-cost scoring. Returns {id: {...}}.

        Discipline copied from find_signal.py:
          - IS/OOS split BY TIME (first half ranks, second half judges).
          - MULTIPLE-COMPARISON margin: best-of-N must clear |t| > T_MARGIN OOS.
          - Edge must exceed ROUND_TRIP_COST_PCT (gross), in the SAME direction IS.
        The direction-matched baseline collapses to the CASH line (0) here, because
        the allocator's counterfactual is literally FLAT (hold cash), and the
        net-of-fee return already charges the cost that baseline exists to subtract.
        """
        out = {}
        nets_by_id = {}                # transient per-decision NET streams (never persisted)
        ts_by_id = {}                  # transient decision timestamps (for N_eff alignment)
        for cid, cfg in self.configs.items():
            t = self.traders.get(cid)
            res = {"id": cid, "n": 0, "n_oos": 0, "oos_edge": None,
                   "t": None, "avg_pnl_net": None, "gross_edge": None,
                   "clears_cost": False, "via": "live",
                   "balance": round(t.balance, 2) if t else None}
            live_nets, live_ts = [], []
            if t is not None:
                rows = self._trade_returns(t.trades)
                live_nets = [r[1] for r in rows]
                live_ts = [r[0] for r in rows]
                n_all = len(rows)
                res["n"] = n_all
                if n_all >= MIN_TOTAL_TRADES:
                    mid = n_all // 2
                    is_rows, oos_rows = rows[:mid], rows[mid:]
                    _, is_edge, _, _ = self._stats(is_rows)
                    n_o, o_edge, o_t, o_gross = self._stats(oos_rows)
                    res.update({"n_oos": n_o, "oos_edge": o_edge, "t": o_t,
                                "avg_pnl_net": o_edge, "gross_edge": o_gross})
                    if (n_o >= MIN_OOS_TRADES and o_edge is not None and o_t is not None
                            and is_edge is not None):
                        clears = (o_edge > 0 and o_t >= T_MARGIN
                                  and o_gross is not None and o_gross > bs.ROUND_TRIP_COST_PCT
                                  and (is_edge > 0))  # same direction in-sample
                        res["clears_cost"] = bool(clears)
            # Counterfactual read from the recorded stream. Real fills outrank
            # simulated ones: cf drives the record ONLY while the live book is
            # still short of MIN_OOS_TRADES. Once a live book graduates, it
            # speaks for itself and cf becomes a footnote.
            cf = self.score_counterfactual(cfg) if cfg.get("cf") or cfg.get("cf_only") else None
            cf_nets = cf_ts = None
            if cf is not None:
                cf_nets = cf.pop("nets", None)
                cf_ts = cf.pop("net_ts", None)
                res["cf_n_oos"] = cf["n_oos"]
                res["cf_edge"] = cf["oos_edge"]
                res["cf_t"] = cf["t"]
                for k in ("status", "worst_neg_funding_days", "hurdle_ann"):
                    if k in cf:
                        res[k] = cf[k]
                if res["n_oos"] < MIN_OOS_TRADES:
                    res.update({"n_oos": cf["n_oos"], "oos_edge": cf["oos_edge"],
                                "t": cf["t"], "gross_edge": cf["gross_edge"],
                                "clears_cost": cf["clears_cost"], "via": cf["via"]})
            # The DSR stats run on whichever stream is driving the record.
            use_cf = cf_nets is not None and str(res["via"]).startswith("cf")
            nets_by_id[cid] = cf_nets if use_cf else live_nets
            ts_by_id[cid] = (cf_ts if use_cf else live_ts) or None
            res["family"] = family_of(cfg)
            out[cid] = res
        self._attach_survival_stats(out, nets_by_id, ts_by_id)
        self._last_nets = nets_by_id          # transient (kill reason codes)
        self.scores = out
        return out

    def _attach_survival_stats(self, out, nets_by_id, ts_by_id=None):
        """Attach {sr, dsr, psr, min_trl, trades_n, verdict, sd_sr, sr0, n_eff}.

        The SR0 hurdle is the expected max SR of N_eff unskilled tries, with
        Var[SR] measured ACROSS the current entrants' per-decision SRs.
        N_eff = trials_count (every entrant EVER registered, monotone — see
        TRIALS_SEED) MINUS the redundancy the correlation clustering can
        DEMONSTRATE: entrants whose net per-decision streams correlate > 0.7
        on >= 8 shared decisions collapse into one cluster; anything with
        too little overlap, no timestamps, or no stream stays a full trial.
        trials_count itself never moves. sd_SR and SR0 are printed next to
        every verdict. A KILLED entrant keeps its frozen verdict and can
        never clear the gate.
        """
        now = time.time()
        srs = []
        streams = {}
        for cid, nets in nets_by_id.items():
            if len(nets) >= 2:
                sd = statistics.pstdev(nets)
                if sd > 1e-12:
                    srs.append(statistics.fmean(nets) / sd)
            tss = (ts_by_id or {}).get(cid)
            if tss and len(tss) == len(nets) and len(nets) >= 2:
                streams[cid] = {float(t): float(x) for t, x in zip(tss, nets)}
        var_sr = statistics.pvariance(srs) if len(srs) >= 2 else None
        sd_sr = math.sqrt(var_sr) if var_sr is not None else None
        if rl is not None and streams:
            clusters = rl.cluster_streams(streams)
            n_scored = len(streams)
            n_clusters = len(clusters)
            n_eff = rl.n_eff_from_clusters(self.trials_count, n_scored, n_clusters)
        else:
            clusters = [[cid] for cid in streams]
            n_clusters = len(clusters)
            n_eff = max(int(self.trials_count), 2)
        sr0 = expected_max_sr(var_sr, max(int(n_eff), 2))
        self.n_eff, self.n_clusters, self.sd_sr, self.sr0 = int(n_eff), int(n_clusters), sd_sr, sr0
        self._last_clusters = clusters
        cluster_of = {}
        for cl in clusters:
            for cid in cl:
                cluster_of[cid] = len(cl)
        hurdle_txt = (f"sd_SR {sd_sr:.4f} · SR0 {sr0:.4f} · N_eff {int(n_eff)}/{int(self.trials_count)}"
                      if sr0 is not None else
                      f"sd_SR unknown · SR0 unknown · N_eff {int(n_eff)}/{int(self.trials_count)}")
        for cid, res in out.items():
            born = max(AP_CF_EPOCH, float(self.configs.get(cid, {}).get("born_ts") or 0))
            st = dsr_stats(nets_by_id.get(cid, []), sr0, born_ts=born, now=now)
            res.update(st)
            res["sr0"] = sr0
            res["sd_sr"] = sd_sr
            res["n_eff"] = int(n_eff)
            res["cluster_size"] = cluster_of.get(cid, 1)
            res["trials_count"] = int(self.trials_count)
            res["verdict"] = f"{res['verdict']} [{hurdle_txt}]"
            if cid in self.killed:
                res["verdict"] = "KILLED"
                res["killed_reason"] = self.killed[cid].get("reason")
                res["clears_cost"] = False   # a killed config is never allocatable

    def _register_trials(self):
        """Monotone N-trials counter. Every config id ever seen counts once —
        forever. PRE_SEED_IDS are already inside TRIALS_SEED; anything else
        (new built-ins, lab nominations) increments on first sight. The count
        NEVER decreases: retirement and death do not un-try a hypothesis."""
        changed = False
        for cid in self.configs:
            if cid in PRE_SEED_IDS or cid in self.trials_ids:
                continue
            self.trials_ids.append(cid)
            self.trials_count += 1
            changed = True
            log("AUTOPILOT", f"trials_count -> {self.trials_count} (registered {cid})")
            cfg = self.configs.get(cid) or {}
            # Contract [S]: autopilot_register once per new id (trials_ids is
            # persisted, so a redeploy never re-announces).
            self._push("autopilot_register", {
                "entrant": cid, "origin": origin_of(cid, cfg), "family": family_of(cfg),
                "born_ts": cfg.get("born_ts"), "trials_count": int(self.trials_count),
                "n_eff": getattr(self, "n_eff", None),
            })
        return changed

    def _apply_kill_rule(self):
        """KILL entrants past MinTRL with deflated PSR < KILL_PSR. Fires ONCE
        per entrant (the killed dict is persisted and checked first), freezes
        the config + final score with the reason, and is never auto-revived —
        resurrection requires a human writing a NEW hypothesis with a NEW id
        and a fresh born_ts."""
        for cid, s in self.scores.items():
            if cid in self.killed:
                continue
            mt, n, dsr = s.get("min_trl"), s.get("trades_n"), s.get("dsr")
            if mt is None or n is None or dsr is None or n < mt or dsr >= KILL_PSR:
                continue
            cfg = self.configs.get(cid, {})
            reason = (f"past MinTRL ({n} decisions >= {mt:.0f}) with deflated "
                      f"PSR {dsr:.3f} < {KILL_PSR} vs SR0 hurdle {s.get('sr0')}")
            code = kill_reason_code(s, getattr(self, "_last_nets", {}).get(cid),
                                    int(s.get("cluster_size") or 1))
            kts = time.time()
            self.killed[cid] = {
                # original shape (unchanged)
                "ts": kts,
                "reason": reason,
                "config": {k: (sorted(v) if isinstance(v, set) else v)
                           for k, v in cfg.items()},
                "final_score": {k: s.get(k) for k in
                                ("sr", "dsr", "psr", "min_trl", "trades_n",
                                 "n_oos", "oos_edge", "t")},
                # graveyard record (additive — contract [E])
                "id": cid, "family": family_of(cfg), "horizon": horizon_of(cfg),
                "cost_model": cost_model_of(cfg), "reason_code": code,
                "sr": s.get("sr"), "sr0": s.get("sr0"), "dsr": dsr, "n": n,
                "killed_ts": kts,
            }
            s["verdict"] = "KILLED"
            s["killed_reason"] = reason
            s["clears_cost"] = False
            log("AUTOPILOT", f"KILLED {cid}: {reason}", "WRN")
            # Kill -> dashboard SSE + assistant spine (cryptobot.tournament.kill via
            # bs._sse_to_event). Best-effort: a sink hiccup must never touch the kill.
            try:
                bs._push_sse("autopilot_kill", {"entrant": cid, "reason": reason,
                                                "reason_code": code, "family": family_of(cfg),
                                                "was_champion": self.champion_id == cid})
            except Exception:
                pass
            if self.champion_id == cid:
                self.champion_id = None
                self.allocation = "FLAT"
                self.last_switch = {"ts": time.time(), "from": cid, "to": None,
                                    "why": f"champion {cid} KILLED -> FLAT"}

    def _apply_proven_rule(self):
        """PROVEN = past MinTRL (trades_n >= min_trl) AND deflated PSR >= PROVEN_DSR.
        Fires ONCE per entrant (persisted list), pushes autopilot_proven. A
        killed entrant can never be proven; a proven one is never un-proven
        here (the goal block keeps the record)."""
        for cid, s in self.scores.items():
            if cid in self.killed or cid in self.proven:
                continue
            mt, n, dsr = s.get("min_trl"), s.get("trades_n"), s.get("dsr")
            if mt is None or n is None or dsr is None or n < mt or dsr < PROVEN_DSR:
                continue
            self.proven.append(cid)
            log("AUTOPILOT", f"PROVEN {cid}: n={n} >= MinTRL {mt:.0f}, DSR {dsr:.3f} >= {PROVEN_DSR}")
            self._push("autopilot_proven", {"entrant": cid, "dsr": dsr, "n": n,
                                            "min_trl": mt, "family": s.get("family")})

    def _announce_clears(self):
        """autopilot_clears once per entrant on its FIRST clears_cost flip."""
        for cid, s in self.scores.items():
            if s.get("clears_cost") and cid not in self.cleared_ids and cid not in self.killed:
                self.cleared_ids.append(cid)
                self._push("autopilot_clears", {"entrant": cid, "n": s.get("n_oos"),
                                                "sr": s.get("sr"), "sr0": s.get("sr0")})

    # ── decision ───────────────────────────────────────────────────────────────
    def decide(self):
        """Refresh scores and pick champion (or FLAT). Hysteresis-guarded. Paper-only."""
        if not self._paper_ok():
            self._append_log("forced FLAT: is_live() True")
            self._save()
            return {"champion": None, "allocation": "FLAT", "scores": self.scores}

        # Lab intake first (throttled + mtime-gated inside), so this decision runs
        # over the current pool. A retired champion is healed to FLAT in there;
        # score() below then rebuilds self.scores from the surviving traders only,
        # so a dangling lab id can never appear in `eligible`.
        self.refresh_lab_configs()
        self.refresh_hypotheses()          # same cadence, same diff logic
        self._register_trials()            # lab/hypothesis hot-swaps may have added ids

        scores = self.score()
        # Survival first: a KILLED entrant is frozen out BEFORE eligibility, so
        # the kill and the promotion can never disagree within one decision.
        self._apply_kill_rule()
        self._apply_proven_rule()
        self._announce_clears()
        eligible = {cid: s for cid, s in scores.items()
                    if s["clears_cost"] and cid not in self.killed}

        prev = self.champion_id
        why = None

        if not eligible:
            new = None
            if prev is not None:
                why = f"{prev} no longer clears cost gate; nothing eligible -> FLAT"
        else:
            best = max(eligible.values(), key=lambda s: s["oos_edge"])
            best_id = best["id"]
            if prev is None:
                new = best_id
                why = f"FLAT -> {best_id} (OOS edge {best['oos_edge']*100:+.3f}%, t {best['t']:+.2f})"
            elif prev not in eligible:
                new = best_id
                why = f"{prev} dropped below cost gate; promote {best_id}"
            else:
                # Champion still eligible — only switch on a clear margin (anti-noise).
                cur = eligible[prev]
                if (best_id != prev
                        and best["oos_edge"] - cur["oos_edge"] >= SWITCH_MARGIN
                        and best["n_oos"] >= MIN_OOS_TRADES):
                    new = best_id
                    why = (f"{best_id} beats {prev} by "
                           f"{(best['oos_edge']-cur['oos_edge'])*100:.3f}% OOS -> switch")
                else:
                    new = prev  # hold champion

        if new != prev:
            self.last_switch = {"ts": time.time(), "from": prev, "to": new,
                                "why": why or "switch"}
            log("AUTOPILOT", f"champion {prev or 'FLAT'} -> {new or 'FLAT'} :: {why}")
            self._push("autopilot_switch", {"from": prev, "to": new, "why": why or "switch"})

        self.champion_id = new
        self.allocation = "FLAT" if new is None else new
        self._append_log(why or f"hold {new or 'FLAT'}")
        self._save()
        return {"champion": self.champion_id, "allocation": self.allocation,
                "scores": self.scores}

    def allows(self, pair, active_config_id):
        """True only when the current champion IS active_config_id and non-FLAT.

        The real paper book passes MAIN_BOOK_CONFIG_ID; it therefore trades only
        when 'base' is the proven champion, and stays FLAT otherwise.
        """
        if not self._paper_ok():
            return False
        return self.champion_id is not None and self.champion_id == active_config_id

    # ── persistence (isolated: autopilot_state.json + optional bot_state id=2) ──
    def _append_log(self, why):
        self.decision_log.append({
            "ts": time.time(),
            "champion": self.champion_id,
            "allocation": self.allocation,
            "why": why,
        })
        if len(self.decision_log) > DECISION_LOG_MAX:
            self.decision_log = self.decision_log[-DECISION_LOG_MAX:]

    def _state_dict(self):
        return {
            "enabled": bool(self.enabled),
            "champion_id": self.champion_id,
            "allocation": self.allocation,
            "last_switch": self.last_switch,
            "scores": self.scores,
            "decision_log": self.decision_log,
            "trials_count": int(self.trials_count),
            "trials_ids": list(self.trials_ids),
            "killed": self.killed,
            "proven": list(self.proven),
            "cleared_ids": list(self.cleared_ids),
            "hyp_seen": dict(self.hyp_seen),
            "n_eff": self.n_eff, "n_clusters": self.n_clusters,
            "sd_sr": self.sd_sr, "sr0": self.sr0,
            "updated_at": time.time(),
        }

    def _save(self):
        # Dual-write: DB mirror at id=2 (NEVER id=1) + json file in the data volume.
        # _state_dict carries enabled=True, so a live instance's periodic save keeps
        # the persisted flag ON (it is cleared only via autopilot_set_persisted_enabled).
        state = self._state_dict()
        _db_write_state(state)
        _file_write_state(state)

    def _load(self):
        data = _db_read_state()
        if data is None:
            data = _file_read_state()
        if not data:
            return
        # Restore only the decision pointer + audit trail; challenger .trades are
        # in-memory and rebuild from live scanning (like _sim_trader).
        self.enabled      = bool(data.get("enabled", True))
        self.champion_id  = data.get("champion_id")
        self.allocation   = data.get("allocation", "FLAT")
        self.last_switch  = data.get("last_switch", self.last_switch)
        self.decision_log = data.get("decision_log", [])
        self.scores       = data.get("scores", {})
        # Trials ledger: monotone by construction — the restored count can only
        # be raised (never below the documented seed), and the kill graveyard
        # is restored verbatim: a KILLED entrant stays dead across redeploys.
        try:
            self.trials_count = max(int(data.get("trials_count", TRIALS_SEED)), TRIALS_SEED)
        except Exception:
            self.trials_count = TRIALS_SEED
        tids = data.get("trials_ids")
        self.trials_ids = [str(x) for x in tids] if isinstance(tids, list) else []
        killed = data.get("killed")
        self.killed = killed if isinstance(killed, dict) else {}
        pv = data.get("proven")
        self.proven = [str(x) for x in pv] if isinstance(pv, list) else []
        cl = data.get("cleared_ids")
        self.cleared_ids = [str(x) for x in cl] if isinstance(cl, list) else []
        hs = data.get("hyp_seen")
        self.hyp_seen = {str(k): v for k, v in hs.items() if isinstance(v, dict)} \
            if isinstance(hs, dict) else {}
        for k in ("n_eff", "n_clusters", "sd_sr", "sr0"):
            v = data.get(k)
            setattr(self, k, v if isinstance(v, (int, float)) and not isinstance(v, bool) else None)
        # The champion heal (persisted champion outside the pool -> FLAT) runs
        # in _heal_champion() once the FULL pool (built-ins + lab + hypotheses)
        # exists — see __init__.
        log("AUTOPILOT", f"state restored — champion={self.champion_id or 'FLAT'}")

    # ── surfacing ──────────────────────────────────────────────────────────────
    def graveyard(self):
        """Contract [E]: structured kill records, old entries normalized
        (missing keys -> 'unknown'/None, never invented)."""
        out = []
        for cid, k in self.killed.items():
            if not isinstance(k, dict):
                continue
            cfg = k.get("config") if isinstance(k.get("config"), dict) else {}
            fs = k.get("final_score") if isinstance(k.get("final_score"), dict) else {}
            out.append({
                "id": k.get("id", cid),
                "family": k.get("family") or (family_of(cfg) if cfg else "unknown"),
                "horizon": k.get("horizon") or (horizon_of(cfg) if cfg else "unknown"),
                "cost_model": k.get("cost_model") or "unknown",
                "reason_code": k.get("reason_code") if k.get("reason_code") in KILL_REASON_CODES else "unknown",
                "sr": k.get("sr", fs.get("sr")), "sr0": k.get("sr0"),
                "dsr": k.get("dsr", fs.get("dsr")), "n": k.get("n", fs.get("trades_n")),
                "killed_ts": k.get("killed_ts", k.get("ts")),
            })
        return out

    def goal(self, now=None):
        """Contract [G]: the goal block. Numbers come from the last score()
        (persisted across restarts); None means 'not measured yet'."""
        now = time.time() if now is None else now
        alive_ids = [cid for cid in self.order if cid in self.configs and cid not in self.killed]
        alive_recs = [{"id": cid, "family": family_of(self.configs[cid]),
                       "born_ts": max(AP_CF_EPOCH, float(self.configs[cid].get("born_ts") or 0))}
                      for cid in alive_ids]
        grave = self.graveyard()
        if rl is not None:
            post = rl.family_posteriors(grave, alive_recs, now=now)
        else:
            post = {}
        fam_names = list(HYP_FAMILIES) + ["switch"]
        for r in alive_recs + grave:
            if r["family"] not in fam_names:
                fam_names.append(r["family"])
        families = {}
        for fam in fam_names:
            members = [cid for cid in alive_ids if family_of(self.configs[cid]) == fam]
            rates = [decisions_per_year_of(self.configs[cid]) for cid in members]
            default = (rl.FAMILY_CADENCE_DEFAULT.get(fam, 52) if rl else 52)
            p = post.get(fam) or {"s": 0, "f": 0, "alive": members,
                                  "killed": [g["id"] for g in grave if g["family"] == fam]}
            families[fam] = {
                "decisions_per_year": min(rates) if rates else default,
                "alive": list(p.get("alive", members)),
                "killed": list(p.get("killed", [])),
                "posterior": {"s": int(p.get("s", 0)), "f": int(p.get("f", 0))},
            }
        # nearest pending verdict from the MinTRL projections
        nearest = {"id": None, "months": None}
        for cid in alive_ids:
            s = self.scores.get(cid) or {}
            m = s.get("months_to_verdict")
            if isinstance(m, (int, float)) and math.isfinite(m) and m > 0 \
                    and cid not in self.proven:
                if nearest["months"] is None or m < nearest["months"]:
                    nearest = {"id": cid, "months": round(float(m), 2)}
        return {
            "proven": list(self.proven),
            "alive": len(alive_ids),
            "killed": len(self.killed),
            "trials_count": int(self.trials_count),
            "n_eff": self.n_eff,
            "n_clusters": self.n_clusters,
            "sd_sr": self.sd_sr,
            "sr0": self.sr0,
            "proven_dsr": PROVEN_DSR,
            "nearest_verdict": nearest,
            "families": families,
            "budget_remaining": self.budget_remaining(),
            "book_state": "flat" if self.champion_id is None else f"champion:{self.champion_id}",
            "hyp_slots": [cid for cid in self.order if cid.startswith("hyp_")],
            "hyp_max_slots": HYP_MAX_SLOTS,
        }

    def status(self):
        """The dict the web routes render."""
        challengers = []
        for cid in self.order:
            # cf_only entrants have no live sandbox trader — they exist purely
            # in the counterfactual scoring path. The KeyError here took the
            # whole /autopilot payload down on the first deploy with them.
            t = self.traders.get(cid)
            s = self.scores.get(cid, {})
            n = len(t.trades) if t else 0
            wins = sum(1 for tr in t.trades if tr.get("pnl", 0) >= 0) if t else 0
            is_lab = cid.startswith("lab_") or cid.startswith("hyp_")   # sanitizers guarantee the namespaces
            challengers.append({
                "id": cid,
                "origin": origin_of(cid, self.configs[cid]),
                "family": family_of(self.configs[cid]),
                "born_ts": self.configs[cid].get("born_ts") if is_lab else None,
                "config": {k: (list(v) if isinstance(v, set) else v)
                           for k, v in self.configs[cid].items() if k != "id"},
                "balance": round(t.balance, 2) if t else None,
                "pnl": round(t.balance - CHALLENGER_START, 2) if t else None,
                "trades": n,
                "wins": wins,
                "losses": n - wins,
                "win_rate": round(wins / max(n, 1) * 100, 1),
                "open_positions": len(t.positions) if t else 0,
                "n_oos": s.get("n_oos", 0),
                "oos_edge_pct": round(s["oos_edge"] * 100, 4) if s.get("oos_edge") is not None else None,
                "t": round(s["t"], 2) if s.get("t") is not None else None,
                "gross_edge_pct": round(s["gross_edge"] * 100, 4) if s.get("gross_edge") is not None else None,
                "clears_cost": bool(s.get("clears_cost", False)),
                "is_champion": cid == self.champion_id,
                # survival statistics (DSR discipline) — None until computable
                "sr": round(s["sr"], 4) if s.get("sr") is not None else None,
                "psr": round(s["psr"], 4) if s.get("psr") is not None else None,
                "dsr": round(s["dsr"], 4) if s.get("dsr") is not None else None,
                "min_trl": round(s["min_trl"], 1) if s.get("min_trl") is not None else None,
                "trades_n": s.get("trades_n"),
                "verdict": s.get("verdict"),
                "sd_sr": s.get("sd_sr"), "sr0": s.get("sr0"), "n_eff": s.get("n_eff"),
                "status_note": s.get("status"),   # e.g. 'waiting on funding history'
                "worst_neg_funding_days": s.get("worst_neg_funding_days"),
                "killed": cid in self.killed,
                "proven": cid in self.proven,
            })
        return {
            "enabled": bool(self.enabled),
            "is_live": bs.is_live(),          # must always be False here
            "champion": self.champion_id,     # None == FLAT
            "allocation": self.allocation,
            "main_book_config": MAIN_BOOK_CONFIG_ID,
            "main_book_active": self.champion_id == MAIN_BOOK_CONFIG_ID,
            "last_switch": self.last_switch,
            "cost_gate_pct": round(bs.ROUND_TRIP_COST_PCT * 100, 3),
            "t_margin": T_MARGIN,
            "min_oos_trades": MIN_OOS_TRADES,
            "cf_epoch": AP_CF_EPOCH,
            "trials_count": int(self.trials_count),
            "n_eff": self.n_eff, "sd_sr": self.sd_sr, "sr0": self.sr0,
            "kill_psr": KILL_PSR,
            "proven_dsr": PROVEN_DSR,
            # A malformed kill record (corrupted state file, hand-edit) is
            # rendered as 'unknown', never allowed to raise: graveyard() already
            # skips non-dicts, and status() feeds the dashboard AND /api/goal.
            "killed": {cid: ({"ts": k.get("ts"), "reason": k.get("reason"),
                              "reason_code": k.get("reason_code", "unknown"),
                              "family": k.get("family")}
                             if isinstance(k, dict) else
                             {"ts": None, "reason": "malformed kill record",
                              "reason_code": "unknown", "family": "unknown"})
                       for cid, k in self.killed.items()},
            "graveyard": self.graveyard(),
            "goal": self.goal(),
            "standings": [
                {**{k: (self.scores.get(cid) or {}).get(k) for k in
                    ("n", "n_oos", "oos_edge", "t", "clears_cost", "via",
                     "sr", "dsr", "psr", "min_trl", "trades_n", "verdict",
                     "sd_sr", "sr0", "n_eff")},
                 "id": cid,
                 "family": family_of(self.configs.get(cid, {})),
                 # e.g. 'waiting on funding history' — the honest reason a row
                 # has no number yet, so the dashboard never shows a bare 0/20
                 "status_note": (self.scores.get(cid) or {}).get("status"),
                 "cf_only": bool(self.configs.get(cid, {}).get("cf_only"))}
                for cid in self.order if cid in self.configs
            ],
            "lab_slots": [cid for cid in self.order if cid.startswith("lab_")],
            "hyp_slots": [cid for cid in self.order if cid.startswith("hyp_")],
            "challengers": challengers,
            "decision_log": self.decision_log[-15:],
        }
