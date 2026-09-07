#!/usr/bin/env python3
"""research_loop contract — pure, honest, deterministic.

Pins the research-side helpers the tournament goal depends on:

  1. PURITY   — no bot_server/autopilot import, no order-placing symbol, no
                exchange or file access; the statistical core (ppf, expected
                max SR) is numerically identical to autopilot.py's.
  2. BUDGET   — budget_remaining is monotone non-increasing in trials_count,
                non-decreasing in decisions/year, 'unknown' (None) without
                cross-entrant variance, and SIGNED (the far side of the hurdle
                never counts as resolvable).
  3. N_eff    — greedy correlation clustering on ALIGNED streams: near-copies
                merge, independents stay apart, thin overlap stays a full trial.
  4. FAMILIES — Beta(s, f) evidence from graveyard + survivors (kill = f,
                alive >= 4 weeks = s), recency window honoured; Thompson picks
                are seeded-deterministic, respect B, and pay the novelty bonus.
  5. TEMPLATES— every deterministic fallback passes autopilot.sanitize_hypotheses
                unchanged, is cf_only, and only names expressible families.
"""
import io
import os
import sys
import math
import random

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import research_loop as rl
import bot_server as bs
import autopilot as ap

bs.log = lambda *a, **k: None
ap.log = lambda *a, **k: None
FAILS = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name
          + (("   [" + str(detail) + "]") if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


HERE = os.path.dirname(os.path.abspath(__file__))
SRC_RL = io.open(os.path.join(HERE, "research_loop.py"), encoding="utf-8").read()

# 1. purity -------------------------------------------------------------------
FORBIDDEN = ("_kraken_place_order", "_kf_place_order", "_binance_place_order",
             "_kraken_private", "_kf_private", "_binance_private", "place_order",
             "AddOrder", "sendorder", "PAPER_LOCK", "import bot_server",
             "import autopilot", "requests.", "psycopg", "open(")
check("research_loop never names an order path / lock / IO",
      not any(tok in SRC_RL for tok in FORBIDDEN),
      [t for t in FORBIDDEN if t in SRC_RL])
check("research_loop imports only stdlib",
      all(m in ("math", "random", "re", "statistics", "time")
          for m in [l.split()[1] for l in SRC_RL.splitlines() if l.startswith("import ")]))
grid = [(v, n) for v in (0.001, 0.01, 0.04, 0.25) for n in (2, 5, 16, 50, 500)]
check("expected_max_sr identical to autopilot.py's",
      all(abs(rl.expected_max_sr(v, n) - ap.expected_max_sr(v, n)) < 1e-12 for v, n in grid))
check("_norm_ppf identical to autopilot.py's",
      all(abs(rl._norm_ppf(p) - ap._norm_ppf(p)) < 1e-12 for p in (0.01, 0.2, 0.5, 0.9, 0.999)))

# 2. budget -------------------------------------------------------------------
V = 0.0025          # sd_SR 0.05 — the realistic cross-entrant dispersion
ks = [rl.budget_remaining(V, n, 365) for n in (5, 10, 20, 40, 80, 160)]
check("budget is monotone non-increasing in trials_count",
      all(a >= b for a, b in zip(ks, ks[1:])), ks)
kd = [rl.budget_remaining(V, 20, d) for d in (12, 52, 365, 365 * 4)]
check("budget rises (non-decreasing) with decisions/year",
      all(a <= b for a, b in zip(kd, kd[1:])) and kd[-1] > kd[0], kd)
check("budget is 'unknown' (None) without cross-entrant variance",
      rl.budget_remaining(None, 20, 365) is None)
check("budget is 0 (not unknown) once SR0 already sits above a plausible SR",
      rl.budget_remaining(0.25, 20, 365 * 4) == 0)
# SIGNED gap: with sd_SR = 0.5 the hurdle is far ABOVE 0.17 — |gap| is huge, but
# that side "resolves" only by proving an entrant worse than luck. Refused.
check("budget uses the signed gap, never |gap| on the wrong side",
      rl.budget_remaining(0.25, 5, 365 * 10) == 0)
chk_ok = rl.registration_check(V, 5, 365)
chk_no = rl.registration_check(0.25, 30, 52)
chk_un = rl.registration_check(None, 30, 52)
check("registration_check: allowed with budget",
      chk_ok["allowed"] is True and chk_ok["budget_remaining"] > 0, chk_ok)
check("registration_check: refused at budget 0 with sr0 + reason",
      chk_no["allowed"] is False and chk_no["budget_remaining"] == 0
      and chk_no["sr0"] is not None and "exhausted" in chk_no["reason"], chk_no)
check("registration_check: unknown budget admits and SAYS unknown",
      chk_un["allowed"] is True and chk_un["budget_remaining"] is None
      and "unknown" in chk_un["reason"], chk_un)
thr52, thr365 = rl.resolution_threshold(52), rl.resolution_threshold(365)
check("resolution threshold = 1.645/sqrt(decisions in 24 months)",
      abs(thr52 - 1.645 / math.sqrt(104)) < 1e-12 and abs(thr365 - 1.645 / math.sqrt(730)) < 1e-12)

# 3. N_eff clustering ---------------------------------------------------------
rng = random.Random(7)
T = [1000.0 + i * 100 for i in range(40)]
A = {t: rng.gauss(0, 1) for t in T}
B = {t: 0.9 * A[t] + 0.1 * rng.gauss(0, 1) for t in T}          # near-copy of A
C = {t: rng.gauss(0, 1) for t in T}                              # independent
D = {t: A[t] for t in T[:5]}                                     # 5 shared decisions only
E = {t + 7.0: A[t] for t in T}                                   # same values, never aligned
cl = rl.cluster_streams({"A": A, "B": B, "C": C, "D": D, "E": E})
check("near-copies cluster together", ["A", "B"] in cl, cl)
check("independent stream stays alone", ["C"] in cl, cl)
check("<8 overlapping decisions = own cluster", ["D"] in cl, cl)
check("un-aligned timestamps never correlate (own cluster)", ["E"] in cl, cl)
check("clustering is deterministic",
      rl.cluster_streams({"A": A, "B": B, "C": C, "D": D, "E": E}) == cl)
check("N_eff = trials_count minus demonstrated redundancy (16 - (5-4) = 15)",
      rl.n_eff_from_clusters(16, 5, 4) == 15)
check("N_eff never below the cluster count or 2",
      rl.n_eff_from_clusters(3, 10, 5) == 5 and rl.n_eff_from_clusters(2, 2, 1) == 2)
check("rho on shared decisions, None when thin",
      rl.stream_correlation(A, B) > 0.7 and rl.stream_correlation(A, D) is None)
check("sd=0 stream yields no correlation (not a crash)",
      rl.stream_correlation(A, {t: 1.0 for t in T}) is None)

# 4. family posteriors + Thompson picker ---------------------------------------
NOW = 1_800_000_000.0
W = rl.SECS_PER_WEEK
grave = [{"id": "k1", "family": "trend", "killed_ts": NOW - 2 * W},
         {"id": "k2", "family": "trend", "killed_ts": NOW - 40 * W},   # outside a 26w window
         {"id": "k3", "family": "carry", "killed_ts": NOW - 1 * W}]
alive = [{"id": "a1", "family": "trend", "born_ts": NOW - 6 * W},    # past 4 weeks -> s
         {"id": "a2", "family": "trend", "born_ts": NOW - 1 * W},    # young -> trial only
         {"id": "a3", "family": "exit_rule", "born_ts": NOW - 10 * W}]
post = rl.family_posteriors(grave, alive, now=NOW)
check("posterior: kill = f, alive >= 4 weeks = s, young alive = trial only",
      post["trend"] == {"s": 1, "f": 2, "trials": 4, "alive": ["a1", "a2"], "killed": ["k1", "k2"]},
      post["trend"])
check("posterior: never-tried families listed with zeros",
      post["lead_lag"] == {"s": 0, "f": 0, "trials": 0, "alive": [], "killed": []}
      and "switch" in post)
post_w = rl.family_posteriors(grave, alive, now=NOW, window_weeks=26)
check("recency window drops old kills", post_w["trend"]["f"] == 1 and post_w["trend"]["killed"] == ["k1"])
check("unknown family is reported, not raised",
      rl.family_posteriors([{"id": "z", "family": "made_up", "killed_ts": NOW}], [], now=NOW)
      ["made_up"]["f"] == 1)

p1 = rl.pick_families(post, B=2, seed=42)
p2 = rl.pick_families(post, B=2, seed=42)
p3 = rl.pick_families(post, B=3, seed=42)
check("picker respects B", len(p1) == 2 and len(p3) == 3)
check("picker is seeded-deterministic", p1 == p2)
check("picker output shape {family, theta, reason} and says 'sampled'",
      all(set(p) == {"family", "theta", "reason"} and "sampled" in p["reason"] for p in p1), p1)
check("different seeds can disagree (it IS a draw)",
      any(rl.pick_families(post, B=1, seed=s) != p1[:1] for s in range(20)))
# novelty bonus: a never-tried family beats a repeatedly-killed one nearly always
pp = {"dead": {"s": 0, "f": 6, "trials": 6}, "fresh": {"s": 0, "f": 0, "trials": 0}}
wins = sum(rl.pick_families(pp, B=1, seed=s)[0]["family"] == "fresh" for s in range(200))
wins0 = sum(rl.pick_families(pp, B=1, seed=s, novelty_bonus=0.0)[0]["family"] == "fresh"
            for s in range(200))
check("novelty bonus lifts a never-tried family", wins >= 190, wins)
check("novelty bonus is what does it (bonus=0 wins less)", wins0 < wins, (wins0, wins))
check("novelty bonus is named in the reason",
      "novelty" in rl.pick_families(pp, B=2, seed=1)[0]["reason"]
      or "novelty" in rl.pick_families(pp, B=2, seed=1)[1]["reason"])
check("bonus only for trials == 0",
      all("novelty" not in p["reason"] for p in rl.pick_families({"x": {"s": 0, "f": 0, "trials": 1}}, B=1, seed=1)))
post2, picks2 = rl.plan_research_pass(grave, alive, B=2, seed=3, now=NOW, window_weeks=26)
check("plan_research_pass = windowed posteriors + picks",
      post2["trend"]["f"] == 1 and len(picks2) == 2 and "26w" in picks2[0]["reason"])

# 5. templates ----------------------------------------------------------------
tpl = rl.all_templates()
INTAKE = NOW
san = ap.sanitize_hypotheses(dict(tpl), INTAKE)
check("every template passes sanitize_hypotheses (cap aside)",
      len(san) == min(len(tpl), ap.HYP_MAX_SLOTS) and len(tpl) >= 10, (len(tpl), len(san)))
# cap aside: each family batch must ALSO pass on its own (<= 5 per family)
per_fam_ok = True
for fam in rl.FAMILIES:
    ft = rl.family_templates(fam)
    if ft and len(ap.sanitize_hypotheses(dict(ft), INTAKE)) != len(ft):
        per_fam_ok = False
    if fam not in rl.EXPRESSIBLE_FAMILIES and ft:
        per_fam_ok = False
    if len(ft) > ap.HYP_MAX_SLOTS:
        per_fam_ok = False
check("each family's template batch passes intact and fits the slot cap", per_fam_ok)
check("inexpressible families honestly have NO template",
      all(rl.family_templates(f) == {} for f in ("reversion_pattern", "cross_section", "lead_lag")))
check("templates carry origin 'template' (never masquerade as an LLM prereg)",
      all(e["origin"] == "template" for e in tpl.values()))
check("templates state the mechanism and every prereg key",
      all(e["note"].startswith("HYPOTHESIS:") and all(k in e["prereg"] for k in rl.PREREG_KEYS)
          for e in tpl.values()))
check("sanitized templates are cf_only with born_ts >= intake",
      all(c["cf_only"] is True and c["born_ts"] >= INTAKE for c in san))
check("template ids stay inside the hyp_ namespace",
      all(rl.HYP_ID_RE.match(h) for h in tpl))
check("carry templates only RAISE the hurdle (hurdle_mult >= 1)",
      all(e["cf"].get("hurdle_mult", 1.0) >= 1.0 for e in rl.family_templates("carry").values()))
check("mintrl estimate follows the stated formula",
      abs(rl.mintrl_months_estimate(4.3) - round((1 + (1.645 / 0.10) ** 2) / 4.3, 1)) < 1e-9
      and rl.mintrl_months_estimate(0) is None)
pk = [{"family": "trend"}, {"family": "lead_lag"}]
sel = rl.templates_for_picks(pk, exclude_ids={"hyp_tsmom_btc_26w"})
check("templates_for_picks skips ids already in play and empty families",
      "hyp_tsmom_btc_26w" not in sel and "hyp_tsmom_btc_52w" in sel
      and all(e["family"] == "trend" for e in sel.values()))
check("kind_cadence: weekly rules 52, daily marks 365, weekly price 52",
      rl.kind_cadence("trend") == 52 and rl.kind_cadence("carry") == 365
      and rl.kind_cadence("price") == 365 and rl.kind_cadence("price", {"weekly": True}) == 52)

print()
if FAILS:
    print(f"{len(FAILS)} FAILURES")
    sys.exit(1)
print("all research_loop checks pass")
