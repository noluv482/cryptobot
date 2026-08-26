#!/usr/bin/env python3
"""Config stamping contract.

Every real-book trade gets stamped at OPEN with a fingerprint of the settings
that decided it, and the ledger keeps the full snapshot behind each
fingerprint. This is the difference between "the stats changed" and "the
stats changed BECAUSE we moved the confidence floor on the 14th": without the
stamp, every settings tweak silently splits the trade population and no one
can ever prove which config produced which results.

What must hold:
  1. DETERMINISTIC — same settings, same stamp; any settings change, new stamp.
  2. AT OPEN — the stamp reflects the config that DECIDED the trade, captured
     in the position dict and carried through close into the trades table.
     Stamping at close would attribute the trade to whatever the sliders
     happened to say hours later.
  3. SECRET-FREE — the snapshot lands in the DB verbatim. No keys, no tokens,
     no PAPER_LOCK: nothing that must not leave the box.
  4. SAFE — a missing cfg_hash key can never crash save_trade, and
     ensure_config survives no DB. Attribution is a bonus; a close is not.
"""
import ast
import json
import os
import sys

if hasattr(sys.stdout, "reconfigure"):   # Windows consoles default to cp1252
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import bot_server as bs

bs.log = lambda *a, **k: None
SRC_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_server.py")
SRC = open(SRC_PATH, encoding="utf-8").read()
FAILS = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


# ── 1. DETERMINISTIC ────────────────────────────────────────────────────────
h1 = bs._cfg_fingerprint()
h2 = bs._cfg_fingerprint()
check("fingerprint is 10 hex chars", len(h1) == 10 and all(c in "0123456789abcdef" for c in h1), h1)
check("same settings → same stamp", h1 == h2)

_saved_adx = bs.ADX_MIN
try:
    bs.ADX_MIN = _saved_adx + 1
    h3 = bs._cfg_fingerprint()
finally:
    bs.ADX_MIN = _saved_adx
check("a settings change → a NEW stamp", h3 != h1, f"{h1} vs {h3}")
check("restoring the setting restores the stamp", bs._cfg_fingerprint() == h1)

# ── 2. AT OPEN, carried through close ───────────────────────────────────────
tree = ast.parse(SRC)


def trader_method(name):
    """Source of a method on the class that owns _open (the trader), so a
    same-named method on another class can never satisfy the check."""
    for cls in ast.walk(tree):
        if isinstance(cls, ast.ClassDef) and any(
                isinstance(m, ast.FunctionDef) and m.name == "_open" for m in cls.body):
            for m in cls.body:
                if isinstance(m, ast.FunctionDef) and m.name == name:
                    return ast.get_source_segment(SRC, m) or ""
    return ""


open_src = trader_method("_open")
check("_open stamps cfg_hash into the position",
      '"cfg_hash": _cfg_h' in open_src and "_cfg_fingerprint()" in open_src)
check("_open records the snapshot in the ledger", "ensure_config" in open_src)
check("sim/backtest opens never touch the ledger",
      "not self._no_persist and not self._force_paper" in open_src)

close_src = trader_method("_close")
check("close carries the OPEN-time stamp, not a fresh one",
      'p.get("cfg_hash", "")' in close_src and "_cfg_fingerprint()" not in close_src)

check("trades table has the column",
      'ALTER TABLE trades ADD COLUMN IF NOT EXISTS cfg_hash TEXT' in SRC)
check("config_ledger table exists",
      "CREATE TABLE IF NOT EXISTS config_ledger" in SRC)
check("save_trade INSERT includes cfg_hash", "%(cfg_hash)s" in SRC)
check("/settings serves the current fingerprint",
      '"cfg_hash":           _cfg_fingerprint()' in SRC)

# ── 3. SECRET-FREE ──────────────────────────────────────────────────────────
snap = bs._cfg_snapshot()
dumped = json.dumps(snap).upper()
check("snapshot carries no secrets",
      not any(bad in dumped for bad in ("KEY", "SECRET", "TOKEN", "PAPER_LOCK", "PIN")),
      dumped[:120])
check("snapshot keys are a fixed set", set(snap) == {
    "interval_m", "min_confidence", "adx_min", "er_min", "min_rr_live",
    "min_rr_paper", "risk_max", "max_positions", "max_drawdown",
    "daily_limits", "disabled_pairs", "round_trip_cost", "ap_champion",
    "code_version"}, str(sorted(snap)))
check("snapshot is JSON-serializable with sorted keys",
      json.dumps(snap, sort_keys=True) == json.dumps(snap, sort_keys=True))

# ── 4. SAFE ─────────────────────────────────────────────────────────────────
_saved_conn = bs.db.conn
try:
    bs.db.conn = None
    ok = True
    try:
        bs.db.ensure_config("deadbeef00", "{}")
    except Exception:
        ok = False
    check("ensure_config survives no DB", ok)
finally:
    bs.db.conn = _saved_conn


class _Cur:
    """Captures the params save_trade actually sends."""
    captured = None
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None): _Cur.captured = params


class _Conn:
    def cursor(self): return _Cur()


try:
    bs.db.conn = _Conn()
    bs.db.save_trade({
        "ts": 0, "coin": "X", "pair": "XUSD", "side": "LONG", "entry": 1.0,
        "exit_price": 1.0, "pnl": 0.0, "held_mins": 1.0, "reason": "t",
        "confidence": 0.5, "nasdaq_mood": "N", "news_sent": "N",
        "balance_after": 100.0, "strategy": "", "timeframe": 15,
    })   # deliberately NO cfg_hash key — the pre-stamp caller shape
finally:
    bs.db.conn = _saved_conn
check("save_trade without cfg_hash does not crash and defaults it",
      _Cur.captured is not None and _Cur.captured.get("cfg_hash") == "",
      str(_Cur.captured)[:80] if _Cur.captured else "no execute captured")

print()
if FAILS:
    print(f"{len(FAILS)} FAILURES")
    sys.exit(1)
print("all config-stamp contract checks pass")
