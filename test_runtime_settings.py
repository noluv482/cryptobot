#!/usr/bin/env python3
"""Runtime-settings persistence contract: what he turns on stays on.

The bot restarts on every push, and every dashboard/Telegram control used to
write an in-memory global — so pause state, coin toggles, price alerts and
the settings sliders silently reverted constantly.

  1. ROUND TRIP — save with every field changed, wipe globals, load, assert
     every value came back (through the real save/load functions).
  2. GUARDED — corrupt values in the file are dropped individually, and a
     truncated file cannot crash boot.
  3. NEVER LIVE — the file must not contain or restore PAPER_LOCK, LIVE_MODE
     or key material. A settings file must never be a path around the lock.
  4. WIRED (from the AST) — every mutating path calls _save_runtime_settings:
     both pause handlers (TG + web), settings POST, togglepair, alert add,
     alert delete. The sim toggle taught this lesson: two handlers, one
     patched, caught only in live verification.
"""
import ast
import io
import json
import os
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import bot_server as bs

bs.log = lambda *a, **k: None
SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_server.py")
FAILS = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name
          + (("   [" + str(detail) + "]") if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


with tempfile.TemporaryDirectory() as td:
    bs._RUNTIME_SETTINGS_FILE = os.path.join(td, "runtime_settings.json")

    # 1. round trip
    bs._paused = True
    bs._rt_max_positions = 3
    bs._rt_max_drawdown = 12.5
    bs._trade_preview_mode = True
    bs._daily_limits = True
    bs.RISK_MAX = 0.12
    bs._disabled_pairs = {"PEPEUSD", "SHIBUSD"}
    bs._price_alerts = {"XBTUSD": [{"target": 80000.0, "above": True, "label": "ath"}]}
    bs._save_runtime_settings()
    check("save writes the file", os.path.exists(bs._RUNTIME_SETTINGS_FILE))
    check("no leftover .tmp", not os.path.exists(bs._RUNTIME_SETTINGS_FILE + ".tmp"))

    bs._paused = False
    bs._rt_max_positions = 0
    bs._rt_max_drawdown = 0.0
    bs._trade_preview_mode = False
    bs._daily_limits = False
    bs.RISK_MAX = 0.08
    bs._disabled_pairs = set()
    bs._price_alerts = {}
    bs._load_runtime_settings()
    check("paused survives restart", bs._paused is True)
    check("max positions survives", bs._rt_max_positions == 3)
    check("max drawdown survives", bs._rt_max_drawdown == 12.5)
    check("trade preview survives", bs._trade_preview_mode is True)
    check("daily limits survives", bs._daily_limits is True)
    check("risk slider survives (and derives min/total)",
          bs.RISK_MAX == 0.12 and bs.RISK_MIN == round(0.12 * 0.35, 3))
    check("coin toggles survive", bs._disabled_pairs == {"PEPEUSD", "SHIBUSD"})
    check("price alerts survive",
          bs._price_alerts.get("XBTUSD", [{}])[0].get("target") == 80000.0)

    # 2. guarded: corrupt fields dropped individually, rest still applies
    with open(bs._RUNTIME_SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump({"paused": "yes-please", "rt_max_positions": 99,
                   "risk_max": 5.0, "daily_limits": True,
                   "disabled_pairs": ["OK1USD", 42]}, f)
    bs._paused = False
    bs._rt_max_positions = 2
    bs.RISK_MAX = 0.08
    bs._daily_limits = False
    bs._load_runtime_settings()
    check("bad 'paused' type ignored", bs._paused is False)
    check("out-of-range max_positions ignored", bs._rt_max_positions == 2)
    check("out-of-range risk ignored", bs.RISK_MAX == 0.08)
    check("good field beside bad ones still applies", bs._daily_limits is True)
    check("non-string pair entries dropped", bs._disabled_pairs == {"OK1USD"})

    with open(bs._RUNTIME_SETTINGS_FILE, "w", encoding="utf-8") as f:
        f.write('{"paused": tru')          # torn write
    try:
        bs._load_runtime_settings()
        check("truncated file cannot crash boot", True)
    except Exception as e:
        check("truncated file cannot crash boot", False, e)

# 3. never live — CODE references, not prose. A comment naming PAPER_LOCK to
# say it is excluded must not fail the guard (the manual_lab docstring taught
# this: match the mechanism, not the mention).
src = io.open(SRC, encoding="utf-8").read()
tree3 = ast.parse(src)
names = set()
for node in ast.walk(tree3):
    if isinstance(node, ast.FunctionDef) and node.name in (
            "_save_runtime_settings", "_load_runtime_settings"):
        for s in ast.walk(node):
            if isinstance(s, ast.Name):
                names.add(s.id)
            elif isinstance(s, ast.Constant) and isinstance(s.value, str):
                names.add(s.value)          # dict keys in the payload
for word in ("PAPER_LOCK", "LIVE_MODE", "KRAKEN_API_KEY", "KRAKEN_API_SECRET",
             "TG_TOKEN"):
    check(f"settings file never references {word} in code", word not in names)

# 4. wired — from the AST, every mutating function calls the save
tree = ast.parse(src)
MUST_SAVE = {"_web_settings", "_web_togglepair", "_web_alert_post",
             "_web_alert_delete"}
found = {}
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name in MUST_SAVE:
        calls = {s.func.id for s in ast.walk(node)
                 if isinstance(s, ast.Call) and isinstance(s.func, ast.Name)}
        found[node.name] = "_save_runtime_settings" in calls
for fn in sorted(MUST_SAVE):
    check(f"{fn} persists on change", found.get(fn, False))
# both pause handlers, which live inside larger dispatchers: count call sites
n_sites = src.count("_save_runtime_settings()")
check("pause persisted on BOTH TG and web paths (>=6 call sites total)",
      n_sites >= 6, n_sites)

print()
if FAILS:
    print(f"{len(FAILS)} FAILURES")
    sys.exit(1)
print("all runtime-settings checks pass")
