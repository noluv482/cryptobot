#!/usr/bin/env python3
"""BS2 layer-2 contract: spread gate, watchdog teeth, shadow sizing, dd
circuit, fee tier. Every behavior is EXECUTED against mocked state — no live
Postgres, no Kraken, no Telegram.

  1. SPREAD GATE — min(hard cap, 2x pair+hour median) when the cell has
     n >= 100; absent/thin cells fall back to the hard cap ALONE; unknown
     spread never blocks; the kill switch actually kills it.
  2. WATCHDOG TEETH — paused blocks NEW ENTRIES ONLY (exits keep managing,
     nothing auto-flattened); a stale tick (> 3 scan intervals) re-fetches a
     direct REST price for exit management and refuses entries outright.
  3. SHADOW SIZING — _open stamps BOTH sizes (engine + module) with the full
     audit dict and the engine's multiplier vector; the record survives into
     the closed-trade ledger. Sizes do not change today.
  4. DD CIRCUIT — entries blocked at EXACTLY -15%% from HWM, open again a cent
     above; it never touches an existing position.
  5. FEE TIER — proven volume only; unknown volume proves NO tier; a ladder
     mismatch is reported while KRAKEN_FEE stays exactly what the env said.
"""
import sys

if hasattr(sys.stdout, "reconfigure"):   # Windows consoles default to cp1252
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import os
import time

import bot_server as bs
import sizing

bs.log = lambda *a, **k: None
SRC = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_server.py"),
           encoding="utf-8").read()
FAILS = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


# quiet everything that would talk to the outside world
_tg_calls = []
bs.tg = lambda *a, **k: _tg_calls.append(a[0] if a else "")
bs.tg_photo = lambda *a, **k: None
bs.tg_buttons = lambda *a, **k: None
bs._send_position_chart = lambda *a, **k: None
bs._send_web_push = lambda *a, **k: None
bs._push_sse = lambda *a, **k: None

PAIR = "TSTUSD"

# ── 1. SPREAD GATE ──────────────────────────────────────────────────────────
# Inject a map and pin the loader so no file I/O happens mid-test.
bs._spread_hours_map = {
    PAIR: {"12": {"median_pct": 0.0005, "p75_pct": 0.0009, "n": 150},
           "13": {"median_pct": 0.0005, "p75_pct": 0.0009, "n": 50}},
}
bs._spread_hours_checked = time.time() + 1e9   # loader returns the injected map

blk, thr, why = bs._spread_gate_check(PAIR, 0.0012, hour=12)
check("2x median wins when tighter than the hard cap (0.12% > 0.10% blocks)",
      blk is True and abs(thr - 0.001) < 1e-12 and "median" in why, f"{blk} {thr} {why}")

blk, thr, _ = bs._spread_gate_check(PAIR, 0.0008, hour=12)
check("spread under 2x median passes", blk is False and abs(thr - 0.001) < 1e-12)

# n=50 cell: below the floor -> the median is NOT trusted, hard cap alone
blk, thr, why = bs._spread_gate_check(PAIR, 0.0012, hour=13)
check("n<100 cell falls back to the hard cap (0.12% < 0.25% passes)",
      blk is False and abs(thr - bs.SPREAD_GATE_HARD_CAP) < 1e-12 and why == "hard cap",
      f"{blk} {thr} {why}")
blk, _, _ = bs._spread_gate_check(PAIR, 0.0030, hour=13)
check("hard cap still bites on the thin cell (0.30% > 0.25%)", blk is True)

blk, thr, why = bs._spread_gate_check("NOPEUSD", 0.0030, hour=12)
check("unmapped pair uses the hard cap alone", blk is True and why == "hard cap")

check("unknown spread (None) never blocks", bs._spread_gate_check(PAIR, None, hour=12)[0] is False)
check("unknown spread (0.0 = _spread_pct's 'cannot know') never blocks",
      bs._spread_gate_check(PAIR, 0.0, hour=12)[0] is False)

_saved_en = bs.SPREAD_GATE_ENABLED
bs.SPREAD_GATE_ENABLED = False
check("SPREAD_GATE_ENABLED=0 disables the gate even at 1% spread",
      bs._spread_gate_check(PAIR, 0.01, hour=12)[0] is False)
bs.SPREAD_GATE_ENABLED = _saved_en

# wiring: rejections land in BOTH ledgers with the agreed gate name
check("scan loop stamps the shadow row spread_gated",
      'db.mark_shadow(_sid, rejected="spread_gated")' in SRC)
check("scan loop writes engine_rejects gate='spread_gated'",
      'db.log_engine_reject(pair, time.time(), "spread_gated", conf)' in SRC)
check("absence semantics documented at the constant",
      "never an invented median" in SRC)

# ── 2. WATCHDOG TEETH ───────────────────────────────────────────────────────
# 2a. paused = ENTRIES ONLY: an open position still gets managed to a close.
pt = bs.PaperTrader(no_persist=True)
pt.positions[PAIR] = {
    "side": "LONG", "entry": 100.0, "contracts": 1.0, "margin": 100.0,
    "target": float("inf"), "opened_at": time.time(), "confidence": 0.5,
    "leverage": 1, "pair": PAIR, "name": "TST", "trail_stop": 95.0,
    "trail_peak": 100.0, "atr_dist": 5.0, "vol_dist": 5.0, "fkey": "",
    "pillars": {}, "mfe": 0.0, "mae": 0.0,
}
_saved_paused = bs._paused
bs._paused = True
pt.on_signal("HOLD", 90.0, 0, 0, "TST", 0.0, PAIR)          # through the stop
check("PAUSED: stop-loss exit still fires (never abandon an open position)",
      PAIR not in pt.positions and len(pt.trades) == 1 and pt.trades[-1]["pnl"] < 0,
      f"pos={list(pt.positions)} trades={len(pt.trades)}")
# that stop-out was >5% of balance, so _close auto-disabled the pair for the
# day (correct, tested elsewhere) — clear it so the NEXT checks see the gate
# under test rather than this one
bs._disabled_pairs.discard(PAIR)

# 2b. paused blocks the ENTRY branch of the same call
pt2 = bs.PaperTrader(no_persist=True)
_opened = []
pt2._open = lambda *a, **k: _opened.append(a)
pt2.can_open_new = lambda: True
pt2.effective_min_conf = lambda *a, **k: 0.0
_saved_classify = bs._classify_strategy
_saved_preview = bs._trade_preview_mode
bs._classify_strategy = lambda *a, **k: ("test", {"emoji": "", "name": ""})
bs._trade_preview_mode = False
pt2._strategy_gate = lambda *a, **k: (None, False)
pt2._correlated_open = lambda *a, **k: 0
pt2._ab_resolved = True

pt2.on_signal("BUY", 100.0, 95.0, 200.0, "TST", 0.9, PAIR, atr=1.0)
check("PAUSED: the entry branch is blocked", _opened == [])
bs._paused = False
pt2.on_signal("BUY", 100.0, 95.0, 200.0, "TST", 0.9, PAIR, atr=1.0)
check("unpaused: the same signal reaches _open", len(_opened) == 1)
bs._paused = _saved_paused
bs._classify_strategy = _saved_classify
bs._trade_preview_mode = _saved_preview

# 2c. stale tick: entries refused outright (real book), loudly but once
pt3 = bs.PaperTrader(no_persist=True)
pt3._no_persist = False          # guard is real-book-only; flip AFTER init
pt3._save = lambda: None
_opened3 = []
pt3._open = lambda *a, **k: _opened3.append(a)
bs._price_fresh_ts[PAIR] = time.time() - bs.STALE_TICK_SECS - 5
bs._stale_warn_ts.pop(PAIR, None)
_tg_calls.clear()
pt3.on_signal("BUY", 100.0, 95.0, 200.0, "TST", 0.9, PAIR, atr=1.0)
check("STALE tick with no position: entry skipped", _opened3 == [])
check("...and it says so on Telegram", any("Stale price tick" in m for m in _tg_calls))
_tg_calls.clear()
pt3.on_signal("BUY", 100.0, 95.0, 200.0, "TST", 0.9, PAIR, atr=1.0)
check("...but only once per pair-hour (no spam)", _tg_calls == [])

# 2d. stale tick WITH a position: exits run on a direct REST re-fetch
pt4 = bs.PaperTrader(no_persist=True)
pt4._no_persist = False
pt4._save = lambda: None
pt4.positions[PAIR] = {
    "side": "LONG", "entry": 100.0, "contracts": 1.0, "margin": 100.0,
    "target": float("inf"), "opened_at": time.time(), "confidence": 0.5,
    "leverage": 1, "pair": PAIR, "name": "TST", "trail_stop": 50.0,
    "trail_peak": 100.0, "atr_dist": 50.0, "vol_dist": 50.0, "fkey": "",
    "pillars": {}, "mfe": 0.0, "mae": 0.0,
}
_saved_gp = bs.get_price
bs.get_price = lambda pair: 101.0          # the DIRECT REST answer
bs._price_fresh_ts[PAIR] = time.time() - bs.STALE_TICK_SECS - 5
pt4.on_signal("HOLD", 150.0, 0, 0, "TST", 0.0, PAIR)   # 150 is the STALE price
p4 = pt4.positions.get(PAIR, {})
check("STALE tick with a position: management used the fresh REST price, not the stale one",
      abs(p4.get("mfe", -1) - 0.01) < 1e-9, f"mfe={p4.get('mfe')}")

# 2e. REST re-fetch fails -> position left untouched this tick, said loudly
def _boom(pair):
    raise RuntimeError("kraken down")
bs.get_price = _boom
bs._price_fresh_ts[PAIR] = time.time() - bs.STALE_TICK_SECS - 5
bs._stale_warn_ts.pop(PAIR, None)
_tg_calls.clear()
pt4.on_signal("HOLD", 10.0, 0, 0, "TST", 0.0, PAIR)    # 10 would stop it out
check("STALE + REST failed: nothing closed on the untrusted price",
      PAIR in pt4.positions and any("refetch failed" in m for m in _tg_calls))
bs.get_price = _saved_gp
bs._price_fresh_ts.pop(PAIR, None)

# 2f. the watchdog itself: >10min pauses, recovery lifts ONLY its own pause
_wd = SRC[SRC.index("def _watchdog_loop"):SRC.index("def _heartbeat_loop")]
check("watchdog pauses entries past 600s of scan stall",
      "age > 600" in _wd and "_paused = True" in _wd)
check("watchdog never auto-flattens (no close/position calls in the loop)",
      "_close(" not in _wd and ".positions" not in _wd)
check("watchdog pause is persisted like the button press",
      _wd.count("_save_runtime_settings()") >= 2)
check("recovery lifts the pause only when the WATCHDOG set it",
      "if not _paused:" in _wd and "_wd_stall_paused" in _wd)

# ── 3. SHADOW SIZING ────────────────────────────────────────────────────────
pt5 = bs.PaperTrader(no_persist=True)
pt5._open("LONG", 100.0, "TST", 200.0, 0.5, PAIR, atr=2.0, stop=95.0)
ss = pt5.positions.get(PAIR, {}).get("sizing_shadow")
check("_open stamps a sizing_shadow record", isinstance(ss, dict) and "error" not in ss,
      str(ss)[:120])
if isinstance(ss, dict) and "error" not in ss:
    check("BOTH sizes recorded: engine margin > 0 next to the module's size",
          ss.get("engine_margin_usd", 0) > 0 and "module_size_units" in ss)
    check("full audit dict rides along (reasons name every zeroing)",
          isinstance(ss.get("audit"), dict) and "reasons" in ss["audit"]
          and "size_units" in ss["audit"])
    check("zero history + unmeasured vol -> module honestly sizes 0 and says why",
          ss["audit"]["size_units"] == 0.0 and len(ss["audit"]["reasons"]) > 0,
          str(ss.get("audit", {}).get("reasons")))
    check("the engine's own multiplier vector is recorded beside it",
          isinstance(ss.get("engine_multipliers"), dict)
          and {"base_risk", "wr", "vol", "corr", "reentry"} <= set(ss["engine_multipliers"]))
    check("actual size is NOT the module's size (shadow means shadow)",
          pt5.positions[PAIR]["margin"] > 0 and ss["audit"]["size_units"] == 0.0)
pt5._close(101.0, "TST", "test", PAIR)
check("sizing_shadow survives into the closed-trade ledger",
      pt5.trades and isinstance(pt5.trades[-1].get("sizing_shadow"), dict))
check("mfe/mae discipline intact after the sizing edit (mfe >= 0 >= mae)",
      pt5.trades[-1]["mfe_pct"] >= 0 >= pt5.trades[-1]["mae_pct"])

# ── 4. DD CIRCUIT at exactly -15% from HWM ──────────────────────────────────
check("sizing.risk_caps opens the circuit at exactly 15%",
      sizing.risk_caps(85.0, 100.0)["dd_circuit_open"] is True
      and sizing.risk_caps(85.01, 100.0)["dd_circuit_open"] is False)

pt6 = bs.PaperTrader(no_persist=True, start_balance=100.0)
pt6.peak = 100.0
pt6.balance = 85.0
_ctr_before = bs._gate_counters["dd_circuit"]
pt6._open("LONG", 100.0, "TST", 200.0, 0.5, PAIR, atr=2.0, stop=95.0)
check("entry REFUSED at exactly -15% from HWM",
      PAIR not in pt6.positions and bs._gate_counters["dd_circuit"] == _ctr_before + 1)
check("the block is announced", pt6._dd_circuit_announced is True)

pt6.balance = 85.01
pt6._open("LONG", 100.0, "TST", 200.0, 0.5, PAIR, atr=2.0, stop=95.0)
check("one cent above the circuit the entry opens again", PAIR in pt6.positions)
check("recovery clears the announcement flag", pt6._dd_circuit_announced is False)

# circuit must never touch an existing position: it lives in _open only
_dd_seg = SRC[SRC.index("Drawdown circuit breaker (2026-09-03"):]
_dd_seg = _dd_seg[:_dd_seg.index("Per-pair daily profit cap")]
check("circuit is entries-only (no _close call in its block)", "_close(" not in _dd_seg)

# ── 5. FEE TIER (report-only, proven only) ──────────────────────────────────
check("unknown volume proves NO tier (never optimistic)",
      bs._proven_fee_tier(None) == (None, None))
check("no ladder configured -> the only tier IS the env fee",
      bs._proven_fee_tier(1e9) == (0.0, bs.KRAKEN_FEE))

os.environ["KRAKEN_FEE_TIERS_JSON"] = "[[50000, 0.006], [100000, 0.004]]"
try:
    check("ladder rung selected by PROVEN volume",
          bs._proven_fee_tier(60000.0) == (50000.0, 0.006))
    check("under the first rung stays at base", bs._proven_fee_tier(100.0) == (0.0, bs.KRAKEN_FEE))

    _fee_before = bs.KRAKEN_FEE
    _saved_vol = bs.db.fills_volume_30d
    bs.db.fills_volume_30d = lambda: 60000.0
    st = bs._fee_tier_check()
    check("mismatch is REPORTED...", st["mismatch"] is True and st["tier_fee"] == 0.006)
    check("...but KRAKEN_FEE is NEVER changed by the job",
          bs.KRAKEN_FEE == _fee_before and st["env_fee"] == _fee_before)

    bs.db.fills_volume_30d = lambda: None
    st = bs._fee_tier_check()
    check("DB down -> tier unproven, env fee stays law",
          st["tier_fee"] is None and st["mismatch"] is False and "unproven" in st["note"])
    bs.db.fills_volume_30d = _saved_vol
finally:
    os.environ.pop("KRAKEN_FEE_TIERS_JSON", None)

_fee_seg = SRC[SRC.index("def _fee_tier_ladder"):SRC.index("def _watchdog_loop")]
check("no assignment to KRAKEN_FEE anywhere in the fee-tier job",
      "KRAKEN_FEE =" not in _fee_seg and "KRAKEN_FEE=" not in
      _fee_seg.replace("KRAKEN_FEE={", "").replace("env `KRAKEN_FEE", ""))
check("fee tier surfaces in the status payload", '"fee_tier":' in SRC)
check("fee tier job registered as a thread", '("Fee tier",          _fee_tier_loop' in SRC)
check("volume query counts REAL fills only (is_paper = FALSE)",
      "is_paper = FALSE" in SRC)

# ── house rules ─────────────────────────────────────────────────────────────
# (TG_TOKEN the env-var NAME legitimately appears; the sweep is about values)
check("no secrets/IPs in the new code",
      all(s not in SRC for s in ("0923", "10.0.0.88", "100.114")))
check("PAPER_LOCK untouched", "PAPER_LOCK" not in _fee_seg and "PAPER_LOCK" not in _wd)

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1)
print("all BS2 layer-2 contract checks pass")
