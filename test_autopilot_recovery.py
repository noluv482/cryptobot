#!/usr/bin/env python3
"""Autopilot must not be able to stay silently off.

The owner's report was "the autopilot keeps turning off." The state file, the
DB row and the running instance all said ON — the real mechanisms were (a) a
boot-time init failure stayed failed for the container's whole life, which a
deploy-per-push pipeline turns into "randomly off until the next deploy", and
(b) the toggle button was hardcoded OFF in the markup until the first fetch
answered, asserting a state it did not know.

  1. RETRY EXISTS — a background loop retries construction while the
     persisted choice is ON and the instance is None (source-verified: the
     thread is started, sleeps, guards on persisted state, clears the error).
  2. RETRY RESPECTS THE OWNER — it must check autopilot_persisted_state()
     is True, so a deliberate OFF is never resurrected.
  3. UI NEVER ASSERTS BLIND — the button ships as an ellipsis, not OFF.
  4. FAILURE EXPLAINS ITSELF — the off-message renders boot_error and says a
     retry is coming.
"""
import io
import os
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_server.py")
src = io.open(SRC, encoding="utf-8").read()
FAILS = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name
          + (("   [" + str(detail) + "]") if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


m = re.search(r"def _autopilot_retry_loop\(\):(.*?)threading\.Thread\(target=_autopilot_retry_loop",
              src, re.S)
check("retry loop exists and is started", bool(m))
if m:
    body = m.group(1)
    check("retry guards on the owner's persisted choice",
          "autopilot_persisted_state() is not True" in body)
    check("retry clears the boot error on success",
          "_autopilot_boot_error = None" in body)
    check("retry sleeps between attempts", "time.sleep(" in body)
    check("retry never constructs over a live instance",
          "_autopilot is not None" in body)

check("boot failure log no longer says 'staying disabled'",
      "staying disabled" not in src)
check("toggle button ships as ellipsis, not a blind OFF",
      'id="ap_toggle_btn" onclick="toggleAutopilot()">&#8230;<' in src)
check("off-message surfaces boot_error with retry note",
      "retrying " in src and "d.boot_error" in src)

print()
if FAILS:
    print(f"{len(FAILS)} FAILURES")
    sys.exit(1)
print("all autopilot-recovery checks pass")
