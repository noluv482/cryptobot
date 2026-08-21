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
"""

import json
import math
import os
import statistics
import time

import bot_server as bs

log = bs.log


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


# ── Challenger configs (per-instance Tier-A levers ONLY) ──────────────────────
# Each lever is applied additively to the SHARED SignalEngine's output before the
# challenger's on_signal runs. No new indicators, no global mutation.
#   entry_conf_floor  : minimum confidence to attempt an entry (>= ATTEMPT_CONF_MIN)
#   min_rr            : minimum reward:risk after ATR reshaping (None -> PAPER_MIN_RR)
#   allowed_strategies: subset of _classify_strategy keys the config will trade (None -> all)
#   risk_min/risk_max : per-instance stake band (sizing only; None -> global RISK_MIN/MAX)
CHALLENGER_CONFIGS = [
    {   # CHAMPION starts here — mirrors the default paper book / _sim_trader
        "id": "base",
        "entry_conf_floor": ATTEMPT_CONF_MIN,
        "min_rr": None,
        "allowed_strategies": None,
        "risk_min": None, "risk_max": None,
    },
    {   # more selective: higher confidence + tighter R:R
        "id": "selective",
        "entry_conf_floor": 0.55,
        "min_rr": 1.5,
        "allowed_strategies": None,
        "risk_min": 0.04, "risk_max": 0.10,
    },
    {   # strictest: only the highest-quality named setups
        "id": "high_conviction",
        "entry_conf_floor": 0.65,
        "min_rr": 2.0,
        "allowed_strategies": {"MULTI_SIGNAL", "MOMENTUM_BREAKOUT", "TREND_CONTINUATION"},
        "risk_min": 0.04, "risk_max": 0.08,
    },
    {   # trend/momentum family only
        "id": "momentum",
        "entry_conf_floor": 0.50,
        "min_rr": 1.5,
        "allowed_strategies": {"MOMENTUM_BREAKOUT", "TREND_CONTINUATION", "MULTI_SIGNAL"},
        "risk_min": None, "risk_max": None,
    },
    {   # mean-reversion / pattern family only
        "id": "reversion",
        "entry_conf_floor": 0.50,
        "min_rr": 1.5,
        "allowed_strategies": {"RSI_REVERSAL", "PATTERN_BREAKOUT"},
        "risk_min": None, "risk_max": None,
    },
    {   # least selective: trades the most, clears the lowest bar
        "id": "loose",
        "entry_conf_floor": ATTEMPT_CONF_MIN,
        "min_rr": None,
        "allowed_strategies": None,
        "risk_min": None, "risk_max": None,
    },
]

MAIN_BOOK_CONFIG_ID = "base"   # the config identity the REAL paper book represents


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

        self.configs = {c["id"]: c for c in CHALLENGER_CONFIGS}
        self.order   = [c["id"] for c in CHALLENGER_CONFIGS]
        self.champion_id = None            # None == FLAT (honest default)
        self.allocation  = "FLAT"
        self.last_switch = {"ts": 0.0, "from": None, "to": None, "why": "init"}
        self.decision_log = []             # rolling list of {ts, champion, allocation, why}
        self.scores = {}                   # id -> last score dict
        self._last_alert_ts = 0.0
        # A live instance is by definition enabled; persisted on every _save so the
        # flag survives a redeploy (see autopilot_persisted_enabled / _set...).
        self.enabled = True

        # Build one force_paper challenger per config. no_persist=True keeps them
        # entirely in-memory and out of the dual-write path (id=1 / paper_state.json).
        self.traders = {}
        self.engines = {}                  # id -> {pair -> SignalEngine}
        self.last_sig = {}                 # id -> {pair -> last signal}
        for cid, cfg in self.configs.items():
            t = bs.PaperTrader(force_paper=True, no_persist=True,
                               start_balance=CHALLENGER_START)
            if cfg.get("risk_min") is not None:
                t._force_risk_min = cfg["risk_min"]
            if cfg.get("risk_max") is not None:
                t._force_risk_max = cfg["risk_max"]
            # Hard belt-and-suspenders: a force_paper trader must never be live.
            assert not t._is_live(), f"challenger {cid} unexpectedly live"
            self.traders[cid]  = t
            self.engines[cid]  = {}
            self.last_sig[cid] = {}

        self._load()

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
        for cid, t in self.traders.items():
            rows = self._trade_returns(t.trades)
            n_all = len(rows)
            res = {"id": cid, "n": n_all, "n_oos": 0, "oos_edge": None,
                   "t": None, "avg_pnl_net": None, "gross_edge": None,
                   "clears_cost": False, "balance": round(t.balance, 2)}
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
            out[cid] = res
        self.scores = out
        return out

    # ── decision ───────────────────────────────────────────────────────────────
    def decide(self):
        """Refresh scores and pick champion (or FLAT). Hysteresis-guarded. Paper-only."""
        if not self._paper_ok():
            self._append_log("forced FLAT: is_live() True")
            self._save()
            return {"champion": None, "allocation": "FLAT", "scores": self.scores}

        scores = self.score()
        eligible = {cid: s for cid, s in scores.items() if s["clears_cost"]}

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
        log("AUTOPILOT", f"state restored — champion={self.champion_id or 'FLAT'}")

    # ── surfacing ──────────────────────────────────────────────────────────────
    def status(self):
        """The dict the web routes render."""
        challengers = []
        for cid in self.order:
            t = self.traders[cid]
            s = self.scores.get(cid, {})
            n = len(t.trades)
            wins = sum(1 for tr in t.trades if tr.get("pnl", 0) >= 0)
            challengers.append({
                "id": cid,
                "config": {k: (list(v) if isinstance(v, set) else v)
                           for k, v in self.configs[cid].items() if k != "id"},
                "balance": round(t.balance, 2),
                "pnl": round(t.balance - CHALLENGER_START, 2),
                "trades": n,
                "wins": wins,
                "losses": n - wins,
                "win_rate": round(wins / max(n, 1) * 100, 1),
                "open_positions": len(t.positions),
                "n_oos": s.get("n_oos", 0),
                "oos_edge_pct": round(s["oos_edge"] * 100, 4) if s.get("oos_edge") is not None else None,
                "t": round(s["t"], 2) if s.get("t") is not None else None,
                "gross_edge_pct": round(s["gross_edge"] * 100, 4) if s.get("gross_edge") is not None else None,
                "clears_cost": bool(s.get("clears_cost", False)),
                "is_champion": cid == self.champion_id,
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
            "challengers": challengers,
            "decision_log": self.decision_log[-15:],
        }
