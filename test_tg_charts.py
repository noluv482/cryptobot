#!/usr/bin/env python3
"""Telegram chart contract — no more blank photos, no more duplicates.

The owner's screenshot showed two identical "position open" messages whose
chart images were nearly blank. Three causes, each pinned here:

  1. plt.savefig saved the process-global "current" figure — under two
     concurrent chart threads, one thread saved the other's just-created
     EMPTY canvas. Now fig.savefig, and a lock serializes the whole build.
  2. bbox_inches="tight" forced a renderer pass against out-of-axes
     annotations -> "did not call Figure.draw" errors on this matplotlib.
  3. The open-side chart send had no _force_paper guard (the close side did),
     so the sim and autopilot challengers each announced the main book's
     coin with their own photo.

Checks: renders a real PNG from the actual function on synthetic candles
(pixel-variance says "not blank"), renders TWO charts on racing threads and
requires both valid, and asserts the guard + fig.savefig + lock in source.
"""
import io
import os
import re
import struct
import sys
import threading
import zlib

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import bot_server as bs

bs.log = lambda *a, **k: None
SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_server.py")
src = io.open(SRC, encoding="utf-8").read()
FAILS = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name
          + (("   [" + str(detail) + "]") if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


# source contracts
check("open-side chart send is guarded for force_paper books",
      re.search(r"if not self\._force_paper:\s*\n\s*threading\.Thread\(target=_send_position_chart",
                src) is not None)
check("figure saved via fig.savefig, not the global current figure",
      "fig.savefig(buf" in src and "plt.savefig(buf" not in src)
check("chart build serialized by a lock", "_chart_lock = threading.Lock()" in src
      and "with _chart_lock:" in src)
check("fragile bbox_inches='tight' removed from the chart",
      'bbox_inches="tight"' not in src.split("def _make_price_chart")[1]
      .split("def _send_position_chart")[0])


def fake_klines(pair, interval=None, limit=60):
    import math
    n = limit or 60
    closes, highs, lows, volumes, opens = [], [], [], [], []
    px = 0.30
    for i in range(n):
        o = px
        px = px * (1 + 0.004 * math.sin(i / 4.0)) + (0.0004 if i % 7 == 0 else -0.0002)
        c = px
        closes.append(c); opens.append(o)
        highs.append(max(o, c) * 1.004); lows.append(min(o, c) * 0.996)
        volumes.append(10 + i % 5)
    return closes, highs, lows, volumes, opens


def png_ok(buf):
    """Valid PNG and visibly non-blank: decompress the image data and require
    real variance across bytes (a flat dark canvas is near-constant)."""
    if buf is None:
        return False, "None"
    data = buf.getvalue()
    if not data.startswith(b"\x89PNG"):
        return False, "not png"
    if len(data) < 8000:
        return False, f"suspiciously small ({len(data)}b)"
    try:
        idat = b""
        i = 8
        while i < len(data):
            ln = struct.unpack(">I", data[i:i+4])[0]
            typ = data[i+4:i+8]
            if typ == b"IDAT":
                idat += data[i+8:i+8+ln]
            i += 12 + ln
        raw = zlib.decompress(idat)
        sample = raw[::997][:4000]
        distinct = len(set(sample))
        if distinct < 24:
            return False, f"only {distinct} distinct sampled bytes — blank"
        return True, f"{len(data)}b, {distinct} distinct"
    except Exception as e:
        return False, f"decode: {e}"


orig = bs.get_klines
bs.get_klines = fake_klines
try:
    buf = bs._make_price_chart("CRVUSD", entry=0.301, entry_side="LONG",
                               trail_stop=0.292)
    ok, why = png_ok(buf)
    check("single chart renders a real, non-blank PNG", ok, why)

    # the race that produced the blank sends: two builds at once
    results = [None, None]
    def worker(ix, pair):
        results[ix] = bs._make_price_chart(pair, entry=0.301,
                                           entry_side="LONG", trail_stop=0.292)
    t1 = threading.Thread(target=worker, args=(0, "CRVUSD"))
    t2 = threading.Thread(target=worker, args=(1, "SOLUSD"))
    t1.start(); t2.start(); t1.join(30); t2.join(30)
    ok0, why0 = png_ok(results[0])
    ok1, why1 = png_ok(results[1])
    check("concurrent chart #1 valid and non-blank", ok0, why0)
    check("concurrent chart #2 valid and non-blank", ok1, why1)
finally:
    bs.get_klines = orig

print()
if FAILS:
    print(f"{len(FAILS)} FAILURES")
    sys.exit(1)
print("all telegram-chart checks pass")
