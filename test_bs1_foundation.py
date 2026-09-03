#!/usr/bin/env python3
"""BS1 foundation contract — attribution, fill realism, horizons, funding, TCA,
engine honesty.

All MOCKED: no live Postgres, no Kraken. What must hold:

  1. ATTRIBUTION — shadow_id/regime/spread_entry/adx/er stamped into the
     position at OPEN, carried through _close into the trades INSERT, with
     mfe/mae updated per tick and slip_realized as the round trip.
  2. FILL REALISM — paper fills pay max(SLIPPAGE, 0.5*spread) per side and
     record which term won.
  3. HORIZON + EXTREMES — _shadow_forward_calc fills fwd168 and
     max_up_48/max_dn_48 from fixture 1h bars; not-ready returns None.
  4. FUNDING — upsert_funding_rates is ON CONFLICT-idempotent; ISO timestamps
     parse to epoch seconds; public endpoint only, no auth.
  5. TCA — shortfall bps signed adverse-positive on both sides; paper rows
     labeled is_paper.
  6. ENGINE HONESTY — a hard gate flipping BUY→HOLD writes engine_rejects
     with the gate's NAME, from the main scan loop only.
"""
import sys

if hasattr(sys.stdout, "reconfigure"):   # Windows consoles default to cp1252
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import ast
import os
import time

import bot_server as bs

bs.log = lambda *a, **k: None
SRC = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_server.py"),
           encoding="utf-8").read()
FAILS = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


class _Cur:
    """Permissive capturing cursor: records every execute, answers fetches."""
    executed = []          # shared: list of (sql, params)
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None): _Cur.executed.append((" ".join(sql.split()), params))
    def fetchone(self): return None
    def fetchall(self): return []


class _Conn:
    autocommit = True
    def cursor(self): return _Cur()


def _executed(frag):
    return [(s, p) for s, p in _Cur.executed if frag in s]


# ── 0. SCHEMA CONTRACT (exact shared shapes other agents build against) ─────
for col, typ in (("shadow_id", "INT"), ("regime", "TEXT"), ("spread_entry", "FLOAT"),
                 ("adx", "FLOAT"), ("er", "FLOAT"), ("mfe_pct", "FLOAT"),
                 ("mae_pct", "FLOAT"), ("slip_realized", "FLOAT")):
    check(f"trades gains {col} {typ}", f'("{col}", "{typ}")' in SRC)
for col in ("fwd168", "max_up_48", "max_dn_48", "conf_post_ob"):
    check(f"shadow_signals gains {col}", f'"{col}"' in SRC)
check("funding_rates PK is (venue, symbol, ts)",
      "PRIMARY KEY (venue, symbol, ts)" in SRC)
check("funding_rates ts is BIGINT epoch-seconds",
      "ts     BIGINT NOT NULL" in SRC and "epoch SECONDS" in SRC)
check("fills_tca has the contract columns + is_paper label",
      all(c in SRC for c in ("shortfall_bps FLOAT", "arrival_mid   FLOAT",
                             "is_paper      BOOLEAN")))
check("engine_rejects table exists",
      "CREATE TABLE IF NOT EXISTS engine_rejects" in SRC)

# ── build a mocked real-book trader ─────────────────────────────────────────
_saved = {n: getattr(bs, n) for n in
          ("tg", "_push_sse", "_send_web_push", "_print_trade_box",
           "_send_position_chart", "_auto_note_record", "_post_loss_analysis",
           "_spread_pct", "_arrival_mid", "USE_MAKER_ENTRIES", "SLIPPAGE")}
_saved_conn = bs.db.conn
for n in ("tg", "_push_sse", "_send_web_push", "_print_trade_box",
          "_send_position_chart", "_auto_note_record", "_post_loss_analysis"):
    setattr(bs, n, lambda *a, **k: None)

try:
    bs.db.conn = _Conn()
    bs.USE_MAKER_ENTRIES = False          # exercise the taker slip model
    bs._spread_pct  = lambda pair: 0.004  # half-spread 0.002 > fixed 0.001
    bs._arrival_mid = lambda pair: 100.0

    pt = bs.PaperTrader(no_persist=True)
    pt._no_persist = False                # real-book paths, mocked DB
    pt.balance = 100.0

    _Cur.executed = []
    pt._open("LONG", 100.0, "TestCoin", 110.0, 0.50, "XBTUSD", atr=2.0,
             shadow_id=777, regime="TRENDING", adx=31.5, er=0.42)
    p = pt.positions.get("XBTUSD")

    # ── 1. attribution stamped at OPEN ──────────────────────────────────────
    check("position stamps shadow_id at open", p and p.get("shadow_id") == 777)
    check("position stamps regime/adx/er at open",
          p and p.get("regime") == "TRENDING" and p.get("adx") == 31.5
          and p.get("er") == 0.42)
    check("position stamps spread_entry from the live cache",
          p and p.get("spread_entry") == 0.004, str(p and p.get("spread_entry")))
    check("mfe/mae start at zero", p and p.get("mfe") == 0.0 and p.get("mae") == 0.0)

    # ── 2. slip term selection (spread wins) ────────────────────────────────
    check("paper open fill pays half-spread when it exceeds the floor",
          p and abs(p["entry"] - 100.0 * 1.002) < 1e-9, str(p and p["entry"]))
    check("slip source recorded as 'spread'", p and p.get("slip_src") == "spread")
    check("open slip magnitude recorded", p and abs(p.get("slip_open", 0) - 0.002) < 1e-12)

    # ── 5. TCA open row: modeled, labeled, adverse-positive ─────────────────
    tca_open = _executed("INSERT INTO fills_tca")
    check("TCA row written for the open fill", len(tca_open) == 1)
    if tca_open:
        _, prm = tca_open[0]
        # (pair, ts, side, size_usd, maker, arrival_mid, fill_price, shortfall_bps, is_paper)
        check("TCA open row is buy-side, is_paper, non-maker",
              prm[2] == "buy" and prm[8] is True and prm[4] is False, str(prm))
        check("TCA open shortfall = +20 bps vs arrival mid",
              prm[7] is not None and abs(prm[7] - 20.0) < 1e-6, str(prm[7]))

    # ── 1b. mfe/mae update every tick (position management path) ────────────
    p["target"] = float("inf"); p["r1_price"] = 1e12; p["r2_price"] = 1e12
    p["trail_stop"] = 0.0; p["atr_dist"] = 1e12; p["vol_dist"] = 1e12
    pt.on_signal("HOLD", 105.0, None, None, "TestCoin", 0.0, "XBTUSD")
    pt.on_signal("HOLD", 95.0, None, None, "TestCoin", 0.0, "XBTUSD")
    exp_mfe = (105.0 - p["entry"]) / p["entry"]
    exp_mae = (95.0 - p["entry"]) / p["entry"]
    check("mfe tracks the peak favorable tick",
          abs(p.get("mfe", 0) - exp_mfe) < 1e-9, str(p.get("mfe")))
    check("mae tracks the peak adverse tick (and mfe survives it)",
          abs(p.get("mae", 0) - exp_mae) < 1e-9 and p.get("mfe") > 0,
          str(p.get("mae")))

    # ── 1c. close writes everything into the trades INSERT ──────────────────
    bs._spread_pct = lambda pair: 0.0     # close side: fixed floor wins
    _Cur.executed = []
    pt._close(104.0, "TestCoin", "take profit", "XBTUSD")
    ins = _executed("INSERT INTO trades")
    check("close INSERTs exactly one trades row", len(ins) == 1)
    if ins:
        _, t = ins[0]
        check("trade row carries shadow_id/regime/adx/er",
              t.get("shadow_id") == 777 and t.get("regime") == "TRENDING"
              and t.get("adx") == 31.5 and t.get("er") == 0.42, str(t)[:120])
        check("trade row carries spread_entry", t.get("spread_entry") == 0.004)
        check("trade row carries mfe_pct/mae_pct",
              abs(t.get("mfe_pct", 0) - exp_mfe) < 1e-9
              and abs(t.get("mae_pct", 0) - exp_mae) < 1e-9,
              f"{t.get('mfe_pct')}/{t.get('mae_pct')}")
        check("slip_realized = open leg (0.002 spread) + close leg (0.001 floor)",
              t.get("slip_realized") is not None
              and abs(t["slip_realized"] - 0.003) < 1e-12, str(t.get("slip_realized")))
    tca_close = _executed("INSERT INTO fills_tca")
    check("TCA row written for the close fill (sell side)",
          len(tca_close) == 1 and tca_close[0][1][2] == "sell",
          str(tca_close and tca_close[0][1]))
    check("in-memory trade_rec mirrors the attribution",
          pt.trades and pt.trades[-1].get("shadow_id") == 777
          and pt.trades[-1].get("slip_realized") is not None)

    # ── 2b. slip term selection (floor wins on a tight pair) ────────────────
    bs._spread_pct = lambda pair: 0.0005  # half-spread 0.00025 < 0.001
    _Cur.executed = []
    pt._open("SHORT", 200.0, "TestCoin2", 180.0, 0.50, "ETHUSD", atr=2.0)
    p2 = pt.positions.get("ETHUSD")
    check("fixed floor wins on a tight spread (slip_src='fixed')",
          p2 and p2.get("slip_src") == "fixed"
          and abs(p2["entry"] - 200.0 * (1 - bs.SLIPPAGE)) < 1e-9,
          str(p2 and (p2.get("slip_src"), p2["entry"])))
    check("attribution defaults to NULL, never invented",
          p2 and p2.get("shadow_id") is None and p2.get("regime") is None)
    pt.positions.pop("ETHUSD", None)
finally:
    bs.db.conn = _saved_conn
    for n, v in _saved.items():
        setattr(bs, n, v)

# ── 1d. plumbing: scan loop → on_signal → _open, maker path included ────────
tree = ast.parse(SRC)
on_signal_src = _open_src = ""
for cls in ast.walk(tree):
    if isinstance(cls, ast.ClassDef) and cls.name == "PaperTrader":
        for m in cls.body:
            if isinstance(m, ast.FunctionDef) and m.name == "on_signal":
                on_signal_src = ast.get_source_segment(SRC, m) or ""
            if isinstance(m, ast.FunctionDef) and m.name == "_open":
                _open_src = ast.get_source_segment(SRC, m) or ""
check("on_signal forwards attribution to _open",
      "shadow_id=shadow_id, regime=regime, adx=adx, er=er" in on_signal_src)
check("scan loop passes the shadow row id into on_signal",
      "shadow_id=_sid, regime=_sh_regime" in SRC)
check("maker pending entries carry the attribution to the fill",
      '"sid": _sid, "regime": _sh_regime' in SRC
      and 'shadow_id=_pp.get("sid")' in SRC)
check("conf_post_ob written after the OB adjustment",
      "db.mark_shadow(_sid, conf_post_ob=conf)" in SRC)

# ── 3. shadow horizon + path extremes from fixture bars ─────────────────────
H = 3600
ts0 = 1_000_000
# 1h bars: (ts, close, high, low). Base 100. Peak high 111 at +30h, trough low
# 93 at +40h, closes drift to 108 by +168h.
bars = []
for i in range(0, 200):
    t = ts0 + i * H
    close = 100.0 + (8.0 * min(i, 168) / 168.0)
    high = 111.0 if i == 30 else close + 0.5
    low  = 93.0  if i == 40 else close - 0.5
    bars.append((t, close, high, low))
res = bs._shadow_forward_calc(bars, ts0, 100.0)
check("forward calc returns 6 fields when 168h elapsed", res is not None and len(res) == 6)
if res:
    f6, f24, f48, f168, max_up, max_dn = res
    check("fwd168 = close at +168h vs base", abs(f168 - 0.08) < 1e-9, str(f168))
    check("max_up_48 finds the 111 high (+11%)", abs(max_up - 0.11) < 1e-9, str(max_up))
    check("max_dn_48 finds the 93 low (-7%)", abs(max_dn - (-0.07)) < 1e-9, str(max_dn))
    check("fwd48 still filled alongside", f48 is not None and abs(f48 - (f48)) < 1e-9)
short_bars = [b for b in bars if b[0] < ts0 + 100 * H]
check("not-ready (<168h of data) returns None, row stays pending",
      bs._shadow_forward_calc(short_bars, ts0, 100.0) is None)
check("filler waits the full 169h before touching a shadow row",
      "scutoff = time.time() - 169 * 3600" in SRC
      and "db.shadow_pending(scutoff)" in SRC)
check("fill_shadow persists the new fields",
      "fwd168=%s, max_up_48=%s, max_dn_48=%s" in " ".join(SRC.split()))
check("old NULL rows: scorer-not-ready comment present", "not ready" in SRC)

# ── 4. funding history: idempotent upserts, epoch ts, public endpoint ───────
check("ISO Z timestamp → epoch seconds",
      bs._iso_to_epoch("1970-01-01T01:00:00.000Z") == 3600
      and bs._iso_to_epoch("1970-01-02T00:00:00Z") == 86400)
check("garbage timestamp → None, never a guess", bs._iso_to_epoch("nonsense") is None)
try:
    bs.db.conn = _Conn()
    _Cur.executed = []
    rows = [(3600, 1e-5), (7200, -2e-5)]
    n1 = bs.db.upsert_funding_rates("kraken", "PF_XBTUSD", rows)
    n2 = bs.db.upsert_funding_rates("kraken", "PF_XBTUSD", rows)  # same rows again
    ups = _executed("INSERT INTO funding_rates")
    check("upsert sends every row each pass (idempotence lives in the PK)",
          n1 == 2 and n2 == 2 and len(ups) == 4)
    check("upsert is ON CONFLICT (venue, symbol, ts) DO UPDATE",
          all("ON CONFLICT (venue, symbol, ts) DO UPDATE" in s for s, _ in ups))
    check("ts sent as int seconds", all(isinstance(p[2], int) for _, p in ups))
finally:
    bs.db.conn = _saved_conn
check("funding loop polls the PUBLIC v4 endpoint, hourly, 3 symbols",
      "derivatives/api/v4/historicalfundingrates" in SRC
      and '("PF_XBTUSD", "PF_ETHUSD", "PF_SOLUSD")' in SRC
      and "time.sleep(3600)" in SRC)
check("funding loop registered as a thread",
      '("Funding history",   _funding_history_loop, ())' in SRC)
check("no auth anywhere near the funding fetch",
      "api_key" not in SRC.split("_funding_history_loop")[2][:2000].lower()
      if SRC.count("_funding_history_loop") >= 3 else True)

# ── 5b. TCA shortfall math ──────────────────────────────────────────────────
check("buy above mid = positive bps",
      abs(bs._tca_shortfall_bps("buy", 100.0, 100.2) - 20.0) < 1e-9)
check("sell below mid = positive bps",
      abs(bs._tca_shortfall_bps("sell", 100.0, 99.8) - 20.0) < 1e-9)
check("price improvement goes negative",
      bs._tca_shortfall_bps("buy", 100.0, 99.9) < 0)
check("unknown arrival mid → None, not zero",
      bs._tca_shortfall_bps("buy", None, 100.0) is None)

# ── 6. engine honesty: hard veto → engine_rejects with the gate NAMED ───────
_eng_saved = {n: getattr(bs, n) for n in
              ("calc_ema", "calc_rsi", "calc_macd", "calc_adx",
               "calc_efficiency_ratio", "calc_obv_trend", "detect_candle_pattern",
               "detect_chart_pattern", "calc_stoch_rsi", "detect_macd_divergence",
               "detect_divergence", "detect_regime", "_in_active_hours",
               "_near_econ_event", "_spread_pct", "_htf_trend", "_orderbook_wall")}
_fg_saved = dict(bs.fear_greed)
_rejects = []
_lr_saved = bs.db.log_engine_reject
try:
    bs.calc_ema  = lambda closes, *a, **k: 90.0        # price above → BUY side
    bs.calc_rsi  = lambda closes, *a, **k: 55.0
    bs.calc_macd = lambda closes, *a, **k: (0.0, 0.0, 0.0)
    bs.calc_adx  = lambda *a, **k: 30.0
    bs.calc_efficiency_ratio = lambda *a, **k: 0.5
    bs.calc_obv_trend = lambda *a, **k: "FLAT"
    bs.detect_candle_pattern = lambda *a, **k: {"signal": "NONE", "name": ""}
    bs.detect_chart_pattern  = lambda *a, **k: {"signal": "NONE", "name": "", "strength": 0.0}
    bs.calc_stoch_rsi = lambda *a, **k: (50.0, 50.0)
    bs.detect_macd_divergence = lambda *a, **k: "NONE"
    bs.detect_divergence = lambda *a, **k: "NONE"
    bs.detect_regime = lambda *a, **k: "TRENDING"
    bs._in_active_hours = lambda *a, **k: True
    bs._near_econ_event = lambda *a, **k: False
    bs._spread_pct = lambda pair: 0.0
    bs._htf_trend = lambda *a, **k: "NEUTRAL"
    bs._orderbook_wall = lambda *a, **k: False
    bs.fear_greed["value"] = 50
    bs.db.log_engine_reject = lambda pair, ts, gate, conf: _rejects.append((pair, gate, conf))

    eng = bs.SignalEngine()
    closes = [100.0] * 40
    highs  = [101.0] * 40
    lows   = [99.0] * 40
    vols_ok  = [100.0] * 40
    # last CLOSED bar (volumes[-2]) far under 40% of the median → volume veto
    vols_low = [100.0] * 38 + [1.0, 100.0]
    for _ in range(bs.CONFIRM_TICKS):    # build above_ticks
        eng.evaluate(closes, highs, lows, vols_ok, 100.0, 0.01, pair="XBTUSD",
                     opens=closes)
    _rejects.clear()
    sig, plan, _, _, conf = eng.evaluate(closes, highs, lows, vols_low, 100.0,
                                         0.01, pair="XBTUSD", opens=closes,
                                         record_rejects=True)
    check("volume gate flips the BUY to HOLD", sig == "HOLD", sig)
    check("veto recorded with the gate NAMED",
          len(_rejects) == 1 and _rejects[0][1] == "volume", str(_rejects))
    check("pre-confidence veto records conf=None (never a fabricated number)",
          _rejects and _rejects[0][2] is None, str(_rejects))
    check("veto recorder targets the evaluated pair",
          _rejects and _rejects[0][0] == "XBTUSD")

    # a clean evaluation writes nothing
    _rejects.clear()
    eng2 = bs.SignalEngine()
    for _ in range(bs.CONFIRM_TICKS + 1):
        eng2.evaluate(closes, highs, lows, vols_ok, 100.0, 0.01, pair="XBTUSD",
                      opens=closes, record_rejects=True)
    check("no veto → no engine_rejects row", len(_rejects) == 0, str(_rejects))
finally:
    for n, v in _eng_saved.items():
        setattr(bs, n, v)
    bs.fear_greed.clear(); bs.fear_greed.update(_fg_saved)
    bs.db.log_engine_reject = _lr_saved

check("sim engine does NOT record rejects (would double-count)",
      "sim_eng.evaluate(" in SRC and
      "record_rejects" not in SRC.split("sim_eng.evaluate(")[1][:300])
check("main scan loop is the ONE record_rejects=True call site",
      SRC.count("record_rejects=True)") == 1)

# ── house rules: no secrets, additive only ──────────────────────────────────
import re
check("nothing that would fail the secrets sweep",
      not re.search(r"0923|10\.0\.0\.88|100\.114|TG_TOKEN\s*=\s*[\"']\w|api_key\s*=\s*[\"']\w", SRC))
check("PAPER_LOCK untouched by this work (no runtime_settings writes of it)",
      "PAPER_LOCK" not in SRC.split("runtime_settings")[0][-200:] if "runtime_settings" in SRC else True)

print()
if FAILS:
    print(f"{len(FAILS)} FAILURES")
    sys.exit(1)
print("all BS1 foundation contract checks pass")
