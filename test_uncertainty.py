#!/usr/bin/env python3
"""Uncertainty layer contract — colour is a claim, and a claim needs n.

Tests the ACTUAL JavaScript from the served page (extracted and run under
Node), not a Python re-derivation that could drift from what the browser
runs — the fixture-drift lesson applied to statistics.

  1. MATH — Wilson 95% intervals match reference values (computed
     independently): 37/168 -> [16.7%, 29.2%]; 0/10 upper bound ~27.8%;
     8/8 lower bound ~67.6% (a perfect small streak still does not clear 50
     in the LOWER bound... it does at 8/8 — see check — the point is the
     interval is what decides, and the test pins the real numbers).
  2. REFUSAL — below n=30 rateClaim returns the count, no percentage,
     muted colour.
  3. COLOUR IS THE INTERVAL — 22/40 (55% point estimate) must NOT go green
     against goodAbove=50, because the lower bound is ~40%. Green requires
     the LOWER bound to clear the line.
  4. WIRED — the served page calls rateClaim at the main WR display, the
     hourly grid, the DOW grid, and the sim panel.
"""
import json
import os
import re
import subprocess
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import bot_server as bs

bs.log = lambda *a, **k: None
FAILS = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name
          + (("   [" + str(detail) + "]") if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


page = bs._DASHBOARD_HTML
# extract the two functions exactly as served
m = re.search(r"(function _wilson\(w,n\)\{.*?\n\})", page, re.S)
m2 = re.search(r"(function rateClaim\(wins,n,opts\)\{.*?\n\})", page, re.S)
check("_wilson present in served page", bool(m))
check("rateClaim present in served page", bool(m2))

if m and m2:
    js = m.group(1) + "\n" + m2.group(1) + """
const out = {
  a: _wilson(37,168),          // his real book
  b: _wilson(0,10),
  c: _wilson(8,8),
  refuse: rateClaim(5,12,{goodAbove:50}),
  notGreen: rateClaim(22,40,{goodAbove:50}),
  green: rateClaim(90,100,{goodAbove:50}),
  red: rateClaim(37,168,{goodAbove:50}),
};
console.log(JSON.stringify(out));
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as f:
        f.write(js)
        path = f.name
    try:
        r = subprocess.run(["node", path], capture_output=True, text=True,
                           timeout=30)
        out = json.loads(r.stdout.strip())
    finally:
        os.unlink(out_path if (out_path := path) else path)

    a = out["a"]
    check("Wilson 37/168 ≈ [16.7%, 29.2%]",
          abs(a[0] - 0.1665) < 0.005 and abs(a[1] - 0.2920) < 0.005,
          [round(a[0], 4), round(a[1], 4)])
    check("Wilson 0/10 upper ≈ 27.8%", abs(out["b"][1] - 0.2775) < 0.01,
          out["b"])
    check("Wilson 8/8 lower ≈ 67.6%", abs(out["c"][0] - 0.676) < 0.01,
          out["c"])
    check("n=12 refuses: count shown, no percent",
          out["refuse"]["refused"] is True and "%" not in out["refuse"]["text"])
    check("55% on n=40 is NOT green (lower bound ~40%)",
          out["notGreen"]["color"] != "var(--g)")
    check("90/100 IS green (lower bound ~82%)",
          out["green"]["color"] == "var(--g)")
    check("his real 22% on n=168 IS red (upper bound < 50)",
          out["red"]["color"] == "var(--r)")

# 4. wired at the call sites
for marker, name in (
    ("rateClaim(d.wins||0,(d.wins||0)+(d.losses||0)", "main win-rate display"),
    ("rateClaim(h.wins,tot", "hourly grid"),
    ("rateClaim(b.wins,tot", "day-of-week grid"),
    ("rateClaim(d.wins||0,d.trades||0", "sim panel"),
):
    check(f"{name} uses rateClaim", marker in page)

print()
if FAILS:
    print(f"{len(FAILS)} FAILURES")
    sys.exit(1)
print("all uncertainty-layer checks pass")
