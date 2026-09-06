#!/usr/bin/env python3
"""Assistant event sink contract (2026-09-06).

The bot forwards its moments to the noluv-assistant spine
(POST ASSISTANT_EVENT_URL, body {system, kind, text, data, ref}). Everything is
mocked: requests.post is replaced by a recorder, the sink thread is driven
inline through the injectable _evt_send(post=, now=, sleep=). No network.

What must hold:
  1. OFF BY DEFAULT — ASSISTANT_EVENT_URL unset -> emit_event/_push_sse/tg()
     enqueue nothing and touch requests.post never.
  2. PAYLOADS — each SSE type maps to the contract kind with the contract
     data keys: trade_open -> cryptobot.trade.open {pair, side, size, price,
     conf}; trade_close -> cryptobot.trade.close {pair, pnl, reason};
     autopilot_kill -> cryptobot.tournament.kill {entrant, reason};
     control -> cryptobot.control; backtest_done -> nothing. The body has
     exactly the contract keys and no id/ts/iso (the server fills those).
  3. NEVER BLOCKS — with the queue full and no worker, 100 emits return in
     well under a second, are counted as dropped, and raise nothing.
  4. DE-DUPE — same kind twice inside 1 s posts once; different kinds both
     post; the tg() mirror yields to a typed event on the same moment in
     EITHER order; identical mirror text inside the window is sent once.
  5. NEVER RAISES — a post that throws is swallowed and counted as failed;
     a non-ok response likewise.
  6. WIRED (from the SOURCE) — tg() calls the mirror after the Discord block,
     _push_sse forwards through _sse_to_event inside a try, and the direct
     emitters exist where the contract says: watchdog pause/resume,
     dd_circuit open/close, max-session-dd control, autopilot kill hook.
  7. WORKER — a real daemon thread delivers a queued typed event to the
     mocked post within a short wait.
"""
import ast
import os
import re
import sys
import threading
import time

if hasattr(sys.stdout, "reconfigure"):   # Windows consoles default to cp1252
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.pop("ASSISTANT_EVENT_URL", None)   # 1. must be off before import
import bot_server as bs

bs.log = lambda *a, **k: None
SRC = open(os.path.join(HERE, "bot_server.py"), encoding="utf-8").read()
AP_SRC = open(os.path.join(HERE, "autopilot.py"), encoding="utf-8").read()
FAILS = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name
          + (f"   [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


class Recorder:
    """Stand-in for requests.post."""
    def __init__(self, ok=True, raise_exc=None):
        self.calls, self.ok, self.raise_exc = [], ok, raise_exc

    def __call__(self, url, json=None, timeout=None, **kw):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if self.raise_exc:
            raise self.raise_exc
        return type("R", (), {"ok": self.ok, "status_code": 200 if self.ok else 500})()


def reset(url=""):
    """Fresh sink state; url '' = disabled. Worker start is stubbed so the
    queue can be inspected deterministically."""
    bs.ASSISTANT_EVENT_URL = url
    with bs._evt_lock:
        bs._evt_state.update({"last_kind": {}, "last_typed_ts": -1e9,
                              "last_mirror": ("", -1e9), "dropped": 0,
                              "posted": 0, "failed": 0, "worker": None})
    while True:
        try: bs._evt_q.get_nowait()
        except Exception: break
    bs._evt_start_worker = lambda: None


def drain(post, now=None):
    """Drive every queued item through _evt_send inline (no sleeping)."""
    out = []
    while True:
        try: item = bs._evt_q.get_nowait()
        except Exception: break
        out.append(bs._evt_send(item, post=post, now=now or time.time, sleep=lambda s: None))
    return out


_orig_start = bs._evt_start_worker
_orig_post = bs.requests.post
bs.TG_TOKEN = ""            # tg() must never reach Telegram here
bs.TG_CHAT_ID = ""
bs.DISCORD_WEBHOOK = ""

# ── 1. off by default ────────────────────────────────────────────────────────
rec = Recorder()
bs.requests.post = rec
reset("")
check("env unset -> ASSISTANT_EVENT_URL empty", bs.ASSISTANT_EVENT_URL == "")
check("emit_event disabled returns False", bs.emit_event("cryptobot.control", "x", {}) is False)
bs._push_sse("trade_open", {"name": "BTC", "side": "LONG", "entry": 1.0, "pair": "XBTUSD",
                            "size": 10, "confidence": 70})
bs.tg("hello *world*")
check("disabled: queue stays empty", bs._evt_q.qsize() == 0, str(bs._evt_q.qsize()))
check("disabled: requests.post never called", rec.calls == [], str(len(rec.calls)))
check("disabled: _evt_send posts nothing",
      bs._evt_send((time.time(), True, {"kind": "k"}), post=rec) is False and rec.calls == [])

# ── 2. payload shapes ────────────────────────────────────────────────────────
URL = "http://127.0.0.1:8420/api/event"
CONTRACT_KEYS = {"system", "kind", "text", "data", "ref"}

reset(URL)
rec = Recorder()
bs._push_sse("trade_open", {"name": "BTC", "side": "LONG", "entry": 61000.5, "pair": "XBTUSD",
                            "size": 250.0, "confidence": 72})
res = drain(rec)
check("trade_open posts one event", len(rec.calls) == 1 and res == [True], str(res))
body = rec.calls[0]["json"] if rec.calls else {}
check("trade_open body has exactly the contract keys", set(body) == CONTRACT_KEYS, str(sorted(body)))
check("trade_open system=cryptobot kind=cryptobot.trade.open",
      body.get("system") == "cryptobot" and body.get("kind") == "cryptobot.trade.open", str(body.get("kind")))
check("trade_open data {pair, side, size, price, conf}",
      {k: body["data"].get(k) for k in ("pair", "side", "size", "price", "conf")}
      == {"pair": "XBTUSD", "side": "LONG", "size": 250.0, "price": 61000.5, "conf": 72}, str(body.get("data")))
check("trade_open text is a human sentence with the numbers",
      "LONG" in body["text"] and "61000.5" in body["text"] and len(body["text"]) <= 300, body.get("text"))
check("POST url + 2 s timeout", rec.calls[0]["url"] == URL and rec.calls[0]["timeout"] == 2, str(rec.calls[0]))
check("body carries no id/ts/iso (server fills them)",
      not ({"id", "ts", "iso"} & set(body)))

reset(URL); rec = Recorder()
bs._push_sse("trade_close", {"name": "ETH", "side": "SHORT", "pair": "ETHUSD", "pnl": -3.25,
                             "reason": "stop_loss", "balance": 96.75, "win": False})
drain(rec)
body = rec.calls[0]["json"] if rec.calls else {}
check("trade_close -> cryptobot.trade.close", body.get("kind") == "cryptobot.trade.close", str(body.get("kind")))
check("trade_close data {pair, pnl, reason}",
      {k: body["data"].get(k) for k in ("pair", "pnl", "reason")}
      == {"pair": "ETHUSD", "pnl": -3.25, "reason": "stop_loss"}, str(body.get("data")))
check("trade_close text shows -$3.25", "-$3.25" in body.get("text", ""), body.get("text"))

reset(URL); rec = Recorder()
bs._push_sse("autopilot_kill", {"entrant": "lab_abc", "reason": "past MinTRL with PSR 0.1 < 0.2",
                                "was_champion": False})
drain(rec)
body = rec.calls[0]["json"] if rec.calls else {}
check("autopilot_kill -> cryptobot.tournament.kill", body.get("kind") == "cryptobot.tournament.kill", str(body.get("kind")))
check("kill data {entrant, reason}",
      body.get("data", {}).get("entrant") == "lab_abc" and "MinTRL" in body.get("data", {}).get("reason", ""),
      str(body.get("data")))

reset(URL); rec = Recorder()
bs._push_sse("control", {"paused": True, "paper_mode": True, "sim_enabled": False})
drain(rec)
body = rec.calls[0]["json"] if rec.calls else {}
check("control -> cryptobot.control", body.get("kind") == "cryptobot.control", str(body.get("kind")))
check("control data {paused, paper_mode, sim_enabled}",
      body.get("data") == {"paused": True, "paper_mode": True, "sim_enabled": False}, str(body.get("data")))
check("control text says PAUSED", "PAUSED" in body.get("text", ""), body.get("text"))

reset(URL); rec = Recorder()
bs._push_sse("autopilot", {"enabled": False})
drain(rec)
body = rec.calls[0]["json"] if rec.calls else {}
check("autopilot toggle -> cryptobot.control {autopilot: False}",
      body.get("kind") == "cryptobot.control" and body.get("data", {}).get("autopilot") is False, str(body))

reset(URL); rec = Recorder()
bs._push_sse("backtest_done", {"result": 1})
check("backtest_done maps to nothing", bs._evt_q.qsize() == 0 and bs._sse_to_event("backtest_done", {}) is None)
check("_sse_to_event survives a non-dict payload", bs._sse_to_event("trade_open", None)[0] == "cryptobot.trade.open")

reset(URL); rec = Recorder()
bs.emit_event("cryptobot.dd_circuit.open", "x" * 1000, {"balance": 1})
drain(rec)
check("text capped at 300 chars", len(rec.calls[0]["json"]["text"]) == 300 if rec.calls else False)

# tg() mirror alone (no typed event nearby) -> cryptobot.tg with markdown stripped
reset(URL); rec = Recorder()
bs.tg("⚠️ *Database disconnected* — learning paused")
t0 = time.time()
check("mirror queued once", bs._evt_q.qsize() == 1, str(bs._evt_q.qsize()))
res = drain(rec, now=lambda: t0 + 5)   # hold window elapsed, no typed event
body = rec.calls[0]["json"] if rec.calls else {}
check("tg mirror -> cryptobot.tg, markdown stripped",
      body.get("kind") == "cryptobot.tg" and body.get("text") == "⚠️ Database disconnected — learning paused",
      str(body))

# ── 3. never blocks ──────────────────────────────────────────────────────────
reset(URL)
for i in range(bs._EVT_QUEUE_MAX):
    bs._evt_q.put_nowait((time.time(), True, {"kind": f"fill{i}"}))
check("queue is bounded", bs._evt_q.full() and bs._evt_q.maxsize == bs._EVT_QUEUE_MAX)
t0 = time.perf_counter(); raised = None; results = []
try:
    for i in range(100):
        results.append(bs.emit_event(f"cryptobot.test.{i}", "overflow", {"i": i}))
except Exception as e:
    raised = e
dt = time.perf_counter() - t0
check("100 emits into a full queue return fast", dt < 0.5, f"{dt:.3f}s")
check("full queue: emits return False, nothing raised", raised is None and results == [False] * 100)
check("full queue: drops are counted", bs._evt_state["dropped"] == 100, str(bs._evt_state["dropped"]))
with bs._evt_lock:
    bs._evt_state["last_typed_ts"] = -1e9   # no typed event claiming the moment
check("full queue: tg() mirror also drops without raising", bs.tg("overflow line") is False and bs._evt_state["dropped"] == 101, str(bs._evt_state["dropped"]))
check("sink thread is a daemon (never keeps the process alive)",
      "daemon=True" in SRC.split("def _evt_start_worker")[1].split("def _evt_worker")[0])

# ── 4. de-dupe ───────────────────────────────────────────────────────────────
reset(URL); rec = Recorder()
a = bs.emit_event("cryptobot.trade.open", "one", {"pair": "A"})
b = bs.emit_event("cryptobot.trade.open", "two", {"pair": "B"})
c = bs.emit_event("cryptobot.trade.close", "three", {"pair": "A"})
drain(rec)
kinds = [x["json"]["kind"] for x in rec.calls]
check("same kind twice inside 1 s -> posted once", a is True and b is False and kinds.count("cryptobot.trade.open") == 1, str(kinds))
check("different kind inside 1 s -> posted", c is True and "cryptobot.trade.close" in kinds, str(kinds))
with bs._evt_lock:
    bs._evt_state["last_kind"]["cryptobot.trade.open"] -= 1.5   # window elapsed
check("same kind after the window -> posted again", bs.emit_event("cryptobot.trade.open", "four") is True)

# typed FIRST, then the tg() mirror (the trade_open code path order)
reset(URL); rec = Recorder()
bs._push_sse("trade_open", {"name": "BTC", "side": "LONG", "entry": 1, "pair": "XBTUSD", "size": 1, "confidence": 50})
bs.tg("🟢 *LONG BTC* opened @ 1")
check("typed first: mirror not even queued", bs._evt_q.qsize() == 1, str(bs._evt_q.qsize()))
drain(rec)
check("typed first: exactly the typed event posts", [x["json"]["kind"] for x in rec.calls] == ["cryptobot.trade.open"])

# mirror FIRST, then typed (the /control route order: tg thread, then _push_sse)
reset(URL); rec = Recorder()
t0 = time.time()
bs.tg("⏸ *Trading PAUSED* via dashboard")
bs._push_sse("control", {"paused": True, "paper_mode": True, "sim_enabled": False})
check("mirror first: both queued", bs._evt_q.qsize() == 2, str(bs._evt_q.qsize()))
slept = []
items = []
while True:
    try: items.append(bs._evt_q.get_nowait())
    except Exception: break
res = [bs._evt_send(it, post=rec, now=lambda: t0 + 0.2, sleep=slept.append) for it in items]
check("mirror first: mirror waits out the hold window then yields", res[0] is False and slept and 0.5 < slept[0] <= 1.0, str((res, slept)))
check("mirror first: only the typed control event posts", [x["json"]["kind"] for x in rec.calls] == ["cryptobot.control"], str([x["json"]["kind"] for x in rec.calls]))

# identical mirror text inside the window -> once; different text -> both
reset(URL); rec = Recorder()
bs.tg("same line"); bs.tg("same line"); bs.tg("other line")
check("identical mirror text inside 1 s sent once, distinct text kept", bs._evt_q.qsize() == 2, str(bs._evt_q.qsize()))

# ── 5. never raises ──────────────────────────────────────────────────────────
reset(URL)
bad = Recorder(raise_exc=ConnectionError("assistant down"))
bs.emit_event("cryptobot.control", "x", {})
res = drain(bad)
check("post exception swallowed -> False, counted failed", res == [False] and bs._evt_state["failed"] == 1 and len(bad.calls) == 1)
reset(URL)
nok = Recorder(ok=False)
bs.emit_event("cryptobot.control", "x", {})
res = drain(nok)
check("non-ok response -> False, counted failed", res == [False] and bs._evt_state["failed"] == 1)
reset(URL)
check("emit_event with unserialisable data does not raise",
      bs.emit_event("cryptobot.control", "x", data=object()) is True and bs._evt_q.get_nowait()[2]["data"] == {})
check("_push_sse survives a broken mapper",
      (lambda orig: (setattr(bs, "_sse_to_event", lambda *a: 1 / 0), bs._push_sse("control", {}), setattr(bs, "_sse_to_event", orig))[1])(bs._sse_to_event) is None)

# ── 6. wired, from the source ────────────────────────────────────────────────
tree = ast.parse(SRC)
funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}


def calls_named(node, name):
    return [c for c in ast.walk(node) if isinstance(c, ast.Call)
            and ((isinstance(c.func, ast.Name) and c.func.id == name)
                 or (isinstance(c.func, ast.Attribute) and c.func.attr == name))]


def emit_kinds(node):
    return [c.args[0].value for c in calls_named(node, "emit_event")
            if c.args and isinstance(c.args[0], ast.Constant)]


tg_src = ast.get_source_segment(SRC, funcs["tg"])
check("tg() mirrors after the Discord block", tg_src.find("DISCORD_WEBHOOK") < tg_src.find("_evt_mirror_tg(msg)") < tg_src.find("if not TG_TOKEN"))
push = funcs["_push_sse"]
in_try = any(isinstance(n, ast.Try) and calls_named(n, "_sse_to_event") for n in ast.walk(push))
check("_push_sse forwards through _sse_to_event inside try/except", in_try and bool(calls_named(push, "emit_event")))
check("_push_sse still broadcasts to SSE clients", "_sse_clients" in ast.get_source_segment(SRC, push))
check("env read: ASSISTANT_EVENT_URL default ''",
      re.search(r'ASSISTANT_EVENT_URL\s*=\s*_clean_env\(os\.environ\.get\("ASSISTANT_EVENT_URL",\s*""\)\)', SRC) is not None)


def enclosing_kinds(marker):
    """emit_event kinds inside whichever function body contains `marker`."""
    for n in funcs.values():
        seg = ast.get_source_segment(SRC, n) or ""
        if marker in seg:
            return emit_kinds(n)
    return None


wd = enclosing_kinds("entries PAUSED by watchdog")
check("watchdog pause/resume emit typed events", wd is not None and {"cryptobot.watchdog.pause", "cryptobot.watchdog.resume"} <= set(wd), str(wd))
dd = enclosing_kinds("DD CIRCUIT OPEN")
check("dd circuit open/close emit typed events", dd is not None and {"cryptobot.dd_circuit.open", "cryptobot.dd_circuit.close"} <= set(dd), str(dd))
mx = enclosing_kinds("Max drawdown hit")
check("max-session-dd pause emits cryptobot.control", mx is not None and "cryptobot.control" in mx, str(mx))
for marker in ("Scan stalled >10m — new entries PAUSED", "Drawdown circuit OPEN", "Max drawdown hit"):
    for n in funcs.values():
        seg = ast.get_source_segment(SRC, n) or ""
        if marker in seg:
            # the typed emit sits right BEFORE its tg() line so the mirror yields
            i_tg = seg.find(marker)
            i_emit = seg.rfind("emit_event(", 0, i_tg)
            check(f"typed emit precedes tg() near '{marker}'", 0 < i_emit and 0 < i_tg - i_emit < 900, str((i_emit, i_tg)))
            break
check("trade_open SSE payload carries size", '"size": round(margin * leverage, 4)' in SRC)
check("trade_close SSE payload carries pair", '"pair": pair,\n' in SRC.split('_push_sse("trade_close"')[1][:200].replace("\r\n", "\n"))
check("autopilot.py kill -> bs._push_sse('autopilot_kill', {entrant, reason})",
      re.search(r'bs\._push_sse\("autopilot_kill",\s*\{"entrant": cid,\s*"reason": reason', AP_SRC) is not None)
check("autopilot kill hook is wrapped in try/except", "try:\n                bs._push_sse(\"autopilot_kill\"" in AP_SRC.replace("\r\n", "\n"))
check("no secret values in any payload builder",
      not any(tok in ast.get_source_segment(SRC, funcs[f]) for f in ("_evt_payload", "_sse_to_event", "_evt_mirror_tg")
              for tok in ("TG_TOKEN", "KRAKEN_API", "BINANCE_API", "ANTHROPIC_API_KEY", "DISCORD_WEBHOOK")))

# ── 7. real worker thread delivers ───────────────────────────────────────────
reset(URL)
bs._evt_start_worker = _orig_start
got = threading.Event()
class ThreadRec(Recorder):
    def __call__(self, *a, **k):
        r = super().__call__(*a, **k); got.set(); return r
trec = ThreadRec()
bs.requests.post = trec
ok = bs.emit_event("cryptobot.control", "worker check", {"paused": False})
delivered = got.wait(3.0)
check("daemon worker delivers a typed event to requests.post", ok and delivered and trec.calls[0]["json"]["kind"] == "cryptobot.control", str((ok, delivered)))
w = bs._evt_state["worker"]
check("worker thread is daemon + alive", w is not None and w.daemon and w.is_alive())
bs.requests.post = _orig_post
bs.ASSISTANT_EVENT_URL = ""

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: " + "; ".join(FAILS))
    sys.exit(1)
print("ALL PASS")
