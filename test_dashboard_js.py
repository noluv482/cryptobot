#!/usr/bin/env python3
"""Syntax-check the dashboard JavaScript AS PYTHON ACTUALLY SERVES IT.

This exists because of a real outage. The check that ran before this one
extracted the <script> block from bot_server.py's SOURCE TEXT and ran
`node --check` on it. It passed. The dashboard was still dead.

_DASHBOARD_HTML is a plain triple-quoted string, not a raw one, so Python
consumes backslash escapes before the browser ever sees the page. A JS regex
written in the source as

    .replace(/\\n/g, '<br>')          # <- two characters in the source

reaches the browser as a regex literal containing a REAL newline: an
unterminated regex, a parse error, and because one parse error kills an entire
<script> element, every function on the page vanishes at once. The page loads,
renders its static HTML, and nothing works.

Checking the source could never catch that. This imports the module and checks
_DASHBOARD_HTML -- the actual string that goes over the wire.

Usage:
    python test_dashboard_js.py
"""
import os
import re
import subprocess
import sys
import tempfile

# Escapes Python consumes inside a non-raw string. \\d, \\s, \\w and friends are
# left alone (with a SyntaxWarning), which is why those regexes survive and
# these do not.
#
# FATAL ones produce a control character. A raw line break or tab inside a JS
# regex or string literal is a parse error, and one parse error takes out the
# whole script element.
FATAL = {
    "\\n": "newline", "\\t": "tab", "\\r": "carriage return",
    "\\b": "backspace", "\\f": "form feed", "\\v": "vertical tab",
    "\\a": "bell", "\\0": "null",
}
# These produce a printable character and are almost always deliberate -- the
# page uses \\u2212 for a real minus sign and \\u2713 for a tick. Reported only
# when something actually failed, so they do not drown out the real finding.
BENIGN = {"\\x": "hex escape", "\\u": "unicode escape"}


def main():
    import bot_server as bs

    html = bs._DASHBOARD_HTML
    print(f"served page: {len(html):,} bytes")

    blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    blocks = [b for b in blocks if len(b.strip()) > 40]
    if not blocks:
        print("FAIL: no <script> block in the served page")
        return 1

    fails = 0
    for n, js in enumerate(blocks):
        path = os.path.join(tempfile.gettempdir(), f"served_dash_{n}.js")
        with open(path, "w", encoding="utf-8") as f:
            f.write(js)
        r = subprocess.run(["node", "--check", path], capture_output=True, text=True)
        ok = r.returncode == 0
        print(f"  block {n}: {len(js.splitlines()):,} lines — {'ok' if ok else 'PARSE ERROR'}")
        if not ok:
            fails += 1
            print("\n" + r.stderr[:1500])
            # Point at the offending line in the SOURCE, which is what gets edited.
            m = re.search(r":(\d+)\n", r.stderr)
            if m:
                ln = int(m.group(1))
                lines = js.split("\n")
                lo, hi = max(0, ln - 3), min(len(lines), ln + 2)
                print("  served text around the error:")
                for i in range(lo, hi):
                    mark = ">>" if i == ln - 1 else "  "
                    print(f"   {mark} {i+1:5d}| {lines[i][:120]}")

    # Independently of node, name the escapes so the CAUSE is obvious rather
    # than only the symptom. A raw newline inside a // comment is normal; one
    # inside a regex or string literal is the bug.
    src = open(os.path.join(os.path.dirname(os.path.abspath(bs.__file__)),
                            "bot_server.py"), encoding="utf-8").read()
    i = src.index("<script>")
    j = src.index("</script>", i)
    block = src[i:j]
    base = src[:i].count("\n") + 1

    print()
    print("control-character escapes in the dashboard string (these break JS):")
    found = 0
    for esc, name in FATAL.items():
        for k, line in enumerate(block.split("\n")):
            if esc in line:
                found += 1
                fails += 1          # fatal even if node somehow parsed around it
                print(f"   bot_server.py:{base+k}  {esc} ({name})")
                print(f"      {line.strip()[:110]}")
    if not found:
        print("   none")

    if fails:
        print()
        print("printable escapes (deliberate — shown only because something failed):")
        for esc, name in BENIGN.items():
            n = sum(1 for line in block.split("\n") if esc in line)
            if n:
                print(f"   {esc} ({name}) on {n} lines")

    print()
    if fails:
        print(f"{fails} block(s) FAILED to parse. The dashboard is broken.")
        print("Fix: keep backslash escapes out of _DASHBOARD_HTML. Prefer CSS")
        print("(white-space:pre-wrap) or a character class over an escape, or")
        print("double the backslash so Python emits one.")
        return 1
    print("all served script blocks parse — the dashboard will run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
