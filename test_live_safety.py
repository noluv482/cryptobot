"""test_live_safety.py — the caps that stand between a bug and real money.

Plain script, no pytest: exit 0 == pass.

An audit on 2026-09-13 found four seams in the live path, all verified in the
source before anything here was written:

  1. LIVE_MODE derived itself from KEY PRESENCE in two separate places — the
     environment (bot_server ~298) and data/api_keys.json saved from the
     dashboard (~700). PAPER_LOCK was the only thing in front of it and
     PAPER_LOCK DEFAULTS TO OFF, so a fresh deploy was armed by a key alone.
  2. There was NO notional ceiling of any kind. `grep MAX_NOTIONAL` returned
     nothing.
  3. The kraken_futures live branch hard-codes `leverage = 20` at confidence
     >= 0.84. (LEVERAGE_MAX = 3 governs the PAPER path only — different code.)
  4. RISK_MAX is dashboard-editable to 0.30, which drags MAX_TOTAL_RISK to 0.60.
     Those are sizing policy, not safety: they scale with the account and can be
     changed from a phone.

So: 12% risk on a $1,000 balance at 20x is $2,400 of notional on one signal,
from a book whose measured edge is indistinguishable from zero.

Everything pinned here can only REFUSE or SHRINK a live order. If a check in
this file ever fails, the live path got MORE permissive — which is the only
direction that matters.
"""
from __future__ import annotations

import io
import os
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import bot_server as bs                                       # noqa: E402

SRC = io.open(ROOT / "bot_server.py", encoding="utf-8").read()
_failed: list = []


def check(label, cond, detail=""):
    ok = bool(cond)
    print(("  PASS  " if ok else "  FAIL  ") + label + ("" if ok else "  — " + str(detail)))
    if not ok:
        _failed.append(label)


# ── 1. keys are not a decision ───────────────────────────────────────────────
print("[1] having keys is not a decision to trade real money")
check("ALLOW_LIVE is unset in this environment, so the default is paper",
      bs._allow_live_env() is False and bs.LIVE_MODE is False,
      (bs._allow_live_env(), bs.LIVE_MODE))
check("the env-key derivation requires ALLOW_LIVE",
      "LIVE_MODE         = bool(_LIVE_KEYS_PRESENT and _allow_live_env())" in SRC)
check("the file-key derivation requires it too",
      SRC.count("if _allow_live_env():") >= 1
      and "KRAKEN_API_SECRET = _s\n                # Keys still LOAD" in SRC)
# Derived from the source, not enumerated by hand. This is the check that
# earned its keep: the THIRD arming site — the dashboard /api/keys endpoint,
# which set LIVE_MODE True and announced "bot will trade live on the next
# signal" — was missed when the other two were fixed, and only this found it.
_SRC_LINES = SRC.splitlines()
_arm_lines = [i + 1 for i, l in enumerate(_SRC_LINES)
              if "LIVE_MODE" in l and "= True" in l]
_ungated = [n for n in _arm_lines
            if "_allow_live_env()" not in " ".join(_SRC_LINES[max(0, n - 4):n - 1])]
check("EVERY assignment of LIVE_MODE = True is guarded by _allow_live_env()",
      _arm_lines and not _ungated,
      {"sites": _arm_lines, "ungated": _ungated})
check("the dashboard key-save endpoint cannot arm live by itself",
      "ALLOW_LIVE is not set on the server" in SRC)
check("keys still LOAD without it (read-only calls keep working)",
      "usable for read-only calls" in SRC)

_saved = os.environ.get("ALLOW_LIVE")
try:
    for val, want in (("1", True), ("true", True), ("on", True),
                      ("0", False), ("", False), ("no", False), ("off", False)):
        os.environ["ALLOW_LIVE"] = val
        check("ALLOW_LIVE=%r -> %s" % (val, want), bs._allow_live_env() is want)
    os.environ.pop("ALLOW_LIVE", None)
    check("absent -> False", bs._allow_live_env() is False)
finally:
    if _saved is None:
        os.environ.pop("ALLOW_LIVE", None)
    else:
        os.environ["ALLOW_LIVE"] = _saved

check("PAPER_LOCK still outranks everything, independently",
      "if PAPER_LOCK:\n        return False" in SRC)

# ── 2. the clamp only ever shrinks ───────────────────────────────────────────
print("\n[2] _live_clamp can never raise a size or a leverage")
check("LIVE_MAX_LEVERAGE defaults to 1", bs.LIVE_MAX_LEVERAGE == 1, bs.LIVE_MAX_LEVERAGE)
check("LIVE_MAX_NOTIONAL_USD defaults to 100", bs.LIVE_MAX_NOTIONAL_USD == 100.0,
      bs.LIVE_MAX_NOTIONAL_USD)

m, l = bs._live_clamp(50, 20, "T")
check("the futures 20x tier is cut to the cap", l == 1 and m * l <= 100.0, (m, l))
m, l = bs._live_clamp(500, 1, "T")
check("an oversized spot margin is cut to the notional cap", abs(m * l - 100.0) < 1e-6, (m, l))
m, l = bs._live_clamp(10, 1, "T")
check("a small order is untouched", m == 10.0 and l == 1, (m, l))

random.seed(11)
bad = []
for _ in range(2000):
    mi = random.uniform(0.01, 5000)
    li = random.randint(1, 50)
    mo, lo = bs._live_clamp(mi, li, "T")
    if lo > li or lo > bs.LIVE_MAX_LEVERAGE or mo > mi + 1e-9 \
       or mo * lo > bs.LIVE_MAX_NOTIONAL_USD + 1e-6 or mo < 0:
        bad.append((mi, li, mo, lo))
check("over 2000 random inputs: never raises margin or leverage, never exceeds the cap",
      not bad, bad[:3])

check("leverage is floored at 1, never zero or negative",
      bs._live_clamp(10, 0, "T")[1] == 1 and bs._live_clamp(10, -5, "T")[1] == 1)
_sav = bs.LIVE_MAX_NOTIONAL_USD
try:
    bs.LIVE_MAX_NOTIONAL_USD = 0.0
    check("a zero cap refuses outright rather than placing something tiny",
          bs._live_clamp(100, 1, "T")[0] == 0.0)
finally:
    bs.LIVE_MAX_NOTIONAL_USD = _sav

# ── 2b. a mistyped limit must not take the bot down ──────────────────────────
print("\n[2b] a typo in a safety limit falls back, it does not crash the boot")
# These are read at import. A bare int()/float() on junk raises there and the
# whole bot fails to start — paper included — turning a mistyped safety limit
# into a total outage. Measured before the fix: LIVE_MAX_NOTIONAL_USD=abc gave
# "ValueError: could not convert string to float: 'abc'" at import.
check("junk falls back to the safe default",
      bs._safe_env_num("NO_SUCH_VAR_XYZ", 7, int) == 7)
_sv = os.environ.get("LIVE_MAX_NOTIONAL_USD")
try:
    os.environ["LIVE_MAX_NOTIONAL_USD"] = "abc"
    check("a non-numeric value returns the default, does not raise",
          bs._safe_env_num("LIVE_MAX_NOTIONAL_USD", 100.0, float) == 100.0)
    os.environ["LIVE_MAX_NOTIONAL_USD"] = ""
    check("an empty value returns the default",
          bs._safe_env_num("LIVE_MAX_NOTIONAL_USD", 100.0, float) == 100.0)
    os.environ["LIVE_MAX_NOTIONAL_USD"] = "50"
    check("a valid value is honoured",
          bs._safe_env_num("LIVE_MAX_NOTIONAL_USD", 100.0, float) == 50.0)
finally:
    if _sv is None:
        os.environ.pop("LIVE_MAX_NOTIONAL_USD", None)
    else:
        os.environ["LIVE_MAX_NOTIONAL_USD"] = _sv
check("both caps are parsed through it",
      '_safe_env_num("LIVE_MAX_LEVERAGE"' in SRC
      and '_safe_env_num("LIVE_MAX_NOTIONAL_USD"' in SRC)

# ── 3. no live branch may skip it ────────────────────────────────────────────
print("\n[3] every live order path goes through the clamp")
live_block = SRC.split("if self._is_live():", 1)[-1].split("\n        else:", 1)[0]
check("all three exchange branches call it",
      live_block.count("_live_clamp(margin, leverage, name)") == 3,
      live_block.count("_live_clamp(margin, leverage, name)"))
for venue, marker in (("kraken_futures", "size_usd = round(margin * leverage, 2)"),
                      ("binance", "_binance_place_order("),
                      ("kraken spot", "order_side = \"buy\" if side ==")):
    i = live_block.find(marker)
    j = live_block.rfind("_live_clamp(margin, leverage, name)", 0, i)
    check("%s clamps BEFORE it places" % venue, 0 < j < i, (j, i))
check("a refused (zero) margin returns instead of placing",
      live_block.count("live notional cap is zero — entry refused") == 3)

# -- 3b. the EXIT path (found by adversarial review, not by me) -------------
print("")
print("[3b] a real close cannot be oversized, mis-levered, or paper-born")
_close_block = SRC.split("def _close", 1)[-1][:9000]
# F3, a regression the CLAMP introduced: _live_clamp rewrote the OPEN leverage
# to 1 (which makes _kraken_place_order omit the param -> SPOT buy) while the
# close still read KRAKEN_LEVERAGE, floored at 2 -> MARGIN sell. A leveraged
# sell with no leveraged long to net against opens a SHORT and leaves the spot
# coin unsold. Before the clamp both sides read the same constant and agreed.
check("the close uses the leverage the POSITION was opened with",
      '_pos_lev   = int(p.get("leverage") or 1)' in _close_block
      and "lev_arg    = _pos_lev if (KRAKEN_MARGIN and _pos_lev >= 2) else None" in _close_block)
check("...and never the raw env constant",
      "lev_arg    = KRAKEN_LEVERAGE" not in _close_block, "close still reads KRAKEN_LEVERAGE")
check("a 1x (spot) open therefore closes as spot, not on margin",
      bs._live_clamp(120.0, 2, "T")[1] == 1)

# F1: is_live() describes the BOT now, not whether THIS position was ever
# placed on an exchange. Positions survive restarts and mode flips.
check("the open records whether it was actually placed live",
      '"opened_live": bool(self._is_live()),' in SRC)
check("the close refuses a real order for a position not opened live",
      "if self._is_live() and p.get(\"opened_live\"):" in SRC)
check("a position from before the field existed is treated as paper",
      "treated as paper, which is the safe direction" in SRC)

# F2: the /control else-branch caught EVERY action, so "pause" set _paused
# True and then flipped it back, and "live" overwrote its own warning.
check("/control only bare-toggles pause for an UNRECOGNISED action",
      "elif action not in _HANDLED:" in SRC)
check("...and the handled list covers every explicit action",
      all(a in SRC.split("_HANDLED = (")[1].split(")")[0]
          for a in ("pause", "resume", "paper", "live", "toggle_mode")))

# ── 4. paper is untouched ────────────────────────────────────────────────────
print("\n[4] none of this changes paper behaviour")
check("the clamp is only reachable inside the is_live() branch",
      "_live_clamp(margin, leverage, name)" not in
      SRC.split("if self._is_live():", 1)[0].split("def _open")[-1])
check("the paper leverage constants are unchanged",
      bs.LEVERAGE_MIN == 1 and bs.LEVERAGE_MAX == 3, (bs.LEVERAGE_MIN, bs.LEVERAGE_MAX))
check("RISK_MAX and MAX_TOTAL_RISK are untouched by this change",
      abs(bs.RISK_MAX - 0.12) < 1e-9 and abs(bs.MAX_TOTAL_RISK - 0.25) < 1e-9,
      (bs.RISK_MAX, bs.MAX_TOTAL_RISK))
check("the absolute caps are env-only — no dashboard writer touches them",
      "LIVE_MAX_NOTIONAL_USD =" not in SRC.split("def _live_clamp")[0].split(
          "LIVE_MAX_NOTIONAL_USD = max(")[-1])

# ── 5. the LAST quantisation must not undo the cap ───────────────────────────
print("\n[5] whole-contract rounding cannot exceed the cap")
# PF_ contracts are 1 USD each, so _kf_place_order quantises the notional — the
# last place a size changes before it reaches the exchange. It used to read
# `max(1, int(round(size_usd)))`, which undid _live_clamp twice over: round()
# carries 99.6 up to 100, and max(1, ...) turns a legitimate $0.50 cap into a
# $1 order. Small in dollars; the point is that the cap did not hold.
check("the futures sizer floors instead of rounding",
      "size = int(math.floor(float(size_usd)))" in SRC)
check("...and refuses a sub-contract size instead of bumping it to $1",
      "refusing rather than rounding up to $1" in SRC)
check("no max(1, int(round(...))) remains in the futures sizer",
      "max(1, int(round(size_usd)))" not in SRC)
import math as _math                                            # noqa: E402
_bad_q = [v for v in (100.0, 99.6, 100.4, 50.5, 0.5, 0.99, 1.0, 1.999)
          if int(_math.floor(float(v))) > v]
check("flooring never exceeds its input for any candidate size", not _bad_q, _bad_q)

print("\n" + "=" * 60)
if _failed:
    print("FAILED (%d): %s" % (len(_failed), ", ".join(_failed)))
    sys.exit(1)
print("all live-safety checks passed")
