#!/usr/bin/env python3
"""HYPOTHESIS-LOOP SAFETY INVARIANTS — executed, not asserted in prose.

The research seam lets an LLM (and the owner) feed pre-registered hypotheses
into the paper tournament. Every path it adds must be provably unable to
reach money or to fool the scorer. Each invariant below is CHECKED BY RUNNING
CODE against the real modules on disk:

  1. NO ORDER PATH. The order-placing / private-endpoint function names are
     read from bot_server.py itself (regex over `def _..._place_order(` and
     `def _..._private(`), never hardcoded here — and none of them is
     referenced as a Name or Attribute anywhere in the AST of autopilot.py,
     research_loop.py, research_pass.py or research_evidence.py. AST, not
     grep: autopilot's docstring may NAME them while promising not to use them.
  2. PAPER_LOCK IS ENV-ONLY. Exactly one assignment exists in the whole tree,
     it reads os.environ, and no other module assigns / setattr()s / globals()-
     writes it. No file or HTTP path can flip it.
  3. HYPOTHESES ARE SANITIZED. The sanitizer (autopilot.sanitize_hypotheses,
     or whatever `sanitize_hyp*` autopilot exports) is FED hostile input:
     cf keys outside the per-kind whitelist are stripped or the entry dropped;
     origin outside the allowlist is dropped; born_ts earlier than intake is
     re-stamped to intake; cf_only is forced; the slot cap holds; garbage of
     every shape never raises.
  4. RESEARCH NEVER READS THE OWNER'S HAND-TRADE TABLE. No research_* module
     imports, names or embeds (in SQL strings) that table or its helpers.

Modules that do not exist yet (parallel work landing later) are reported as
SKIP with the invariant they will be held to; existing modules are held to it
now. Exit 1 on any FAIL.

    python test_hypothesis_safety.py
"""
import ast
import io
import os
import re
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import bot_server as bs          # noqa: E402
import autopilot as ap           # noqa: E402

bs.log = lambda *a, **k: None
ap.log = lambda *a, **k: None

FAILS, SKIPS = [], []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name
          + (("   [" + str(detail) + "]") if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def skip(name, why):
    print("SKIP  " + name + "   [" + why + "]")
    SKIPS.append(name)


def _src(name):
    p = os.path.join(HERE, name)
    if not os.path.exists(p):
        return None
    return io.open(p, encoding="utf-8").read()


def _tree(name):
    s = _src(name)
    return ast.parse(s, name) if s is not None else None


def _idents(tree):
    """Every Name id and Attribute attr referenced in code (docstrings and
    comments are NOT part of the AST's identifiers)."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                out.add(a.name)
                if a.asname:
                    out.add(a.asname)
    return out


def _strings(tree):
    return {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}


RESEARCH_FILES = ("autopilot.py", "research_loop.py", "research_pass.py", "research_evidence.py")

# ── 1. no order-placing / private-API symbol in the research side ────────────
BS_SRC = _src("bot_server.py")
ORDER_FUNCS = sorted(set(re.findall(r"^def (_[a-z0-9]+_(?:place_order|private))\(", BS_SRC, re.M)))
check("order/private function names were found in bot_server (not hardcoded here)",
      len(ORDER_FUNCS) >= 4, ORDER_FUNCS)
# the exchange session helpers those wrap — same rule, discovered the same way
PRIVATE_EXTRA = sorted(set(re.findall(r"^def (_[a-z0-9]+_(?:cancel_order|close_position|private_post))\(",
                                      BS_SRC, re.M)))
FORBIDDEN = set(ORDER_FUNCS) | set(PRIVATE_EXTRA)
for fname in RESEARCH_FILES:
    tree = _tree(fname)
    if tree is None:
        skip(f"{fname}: no order/private symbol referenced", "file not present yet")
        continue
    hits = sorted(_idents(tree) & FORBIDDEN)
    check(f"{fname}: no order/private symbol referenced in code (AST)", not hits, hits)
    # belt and braces: not even inside a string that could be getattr()'d
    str_hits = sorted(s for s in _strings(tree) if any(f in s for f in FORBIDDEN)
                      and not s.lstrip().startswith(("HARD SANDBOX", "\n")))
    # docstrings are Constant nodes too; only flag SHORT strings (a getattr key),
    # never a paragraph that merely names the symbol while forbidding it
    str_hits = [s for s in str_hits if len(s) < 60]
    check(f"{fname}: no order/private symbol as a getattr-able string literal", not str_hits, str_hits)
    check(f"{fname}: never getattr()s bot_server dynamically",
          not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                  and n.func.id == "getattr" and n.args
                  and isinstance(n.args[0], ast.Name) and n.args[0].id in ("bs", "bot_server")
                  for n in ast.walk(tree)))

# ── 2. PAPER_LOCK is read from the environment, once, and never written ──────
def _assigns_to(tree, name):
    out = []
    for n in ast.walk(tree):
        targets = []
        if isinstance(n, ast.Assign):
            targets = n.targets
        elif isinstance(n, (ast.AugAssign, ast.AnnAssign)):
            targets = [n.target]
        elif isinstance(n, ast.Global) and name in n.names:
            out.append(("global", n.lineno))
            continue
        for t in targets:
            if isinstance(t, ast.Name) and t.id == name:
                out.append(("assign", n.lineno, n))
            elif isinstance(t, ast.Attribute) and t.attr == name:
                out.append(("attr-assign", n.lineno, n))
    return out


bs_tree = ast.parse(BS_SRC, "bot_server.py")
pl = _assigns_to(bs_tree, "PAPER_LOCK")
check("bot_server assigns PAPER_LOCK exactly once", len(pl) == 1, [(k, ln) for k, ln, *_ in pl])
if pl:
    node = pl[0][2]
    val_src = ast.get_source_segment(BS_SRC, node.value) or ""
    check("that one assignment reads os.environ (env-only)",
          "os.environ" in val_src and "PAPER_LOCK" in val_src, val_src[:80])
    check("the assignment is module-level (not inside a route or loop)",
          any(isinstance(n, ast.Assign) and n is node for n in bs_tree.body))
check("bot_server never setattr()s / globals()-writes PAPER_LOCK",
      not re.search(r"setattr\([^)]*PAPER_LOCK|globals\(\)\s*\[\s*['\"]PAPER_LOCK", BS_SRC))
check("bot_server has no HTTP/JSON path that mentions setting PAPER_LOCK",
      not re.search(r"(request\.(json|args|form)[^\n]*PAPER_LOCK|PAPER_LOCK[^\n]*request\.(json|args|form))",
                    BS_SRC))
for fname in RESEARCH_FILES + ("research_lab.py", "find_signal.py", "learning_report.py"):
    tree = _tree(fname)
    if tree is None:
        skip(f"{fname}: never assigns PAPER_LOCK", "file not present yet")
        continue
    a = _assigns_to(tree, "PAPER_LOCK")
    check(f"{fname}: never assigns / globals PAPER_LOCK", not a, a)
    s = _src(fname)
    check(f"{fname}: no setattr on PAPER_LOCK", "setattr" not in s or "PAPER_LOCK" not in s
          or not re.search(r"setattr\([^)]*PAPER_LOCK", s))
check("is_live() honours PAPER_LOCK as a hard stop (existing contract kept)",
      "if PAPER_LOCK:" in BS_SRC)

# ── 3. hypotheses sanitizer under hostile input ──────────────────────────────
san = getattr(ap, "sanitize_hypotheses", None)
if san is None:
    cands = [n for n in dir(ap) if n.startswith("sanitize_hyp")]
    san = getattr(ap, cands[0], None) if cands else None

# per-kind cf whitelist: the scorer's EXISTING cf keys, derived from the
# registered configs (autopilot.HYP_CF_KEYS wins when the sanitizer publishes it)
derived = {}
for c in ap.CHALLENGER_CONFIGS:
    kind = c.get("kind") or "price"
    derived.setdefault(kind, set()).update((c.get("cf") or {}).keys())
WHITELIST = getattr(ap, "HYP_CF_KEYS", None) or getattr(ap, "HYP_CF_WHITELIST", None) or derived
WHITELIST = {k: set(v) for k, v in dict(WHITELIST).items()}
ORIGINS = set(getattr(ap, "HYP_ORIGINS", None) or getattr(ap, "HYP_ORIGIN_ALLOWLIST", None)
              or ("llm_prereg", "owner_idea", "human"))
MAX_SLOTS = int(getattr(ap, "HYP_MAX_SLOTS", 5))
ID_RE = re.compile(r"^hyp_[a-z0-9_]{1,24}\Z")


def _entries(out):
    """Normalize the sanitizer's return (dict id->entry, list of entries, or
    (entries, meta)) into a list of dicts carrying an 'id'."""
    if isinstance(out, tuple) and out:
        out = out[0]
    if isinstance(out, dict):
        if out and all(isinstance(v, dict) for v in out.values()):
            return [dict(v, id=v.get("id", k)) for k, v in out.items()]
        return []
    if isinstance(out, list):
        return [dict(e, id=e.get("id")) for e in out if isinstance(e, dict)]
    return []


def _call(raw, now=None):
    """Sanitizers differ in signature; try (raw), (raw, now)."""
    try:
        return san(raw, now) if now is not None else san(raw)
    except TypeError:
        return san(raw)


def _good(idx, **over):
    e = {"kind": "price", "family": "trend",
         "cf": {"conf": 0.55, "horizon": "fwd48"},
         "born_ts": time.time() - 5, "origin": "llm_prereg",
         "note": "HYPOTHESIS: a one-paragraph mechanism.",
         "prereg": {"mechanism": "m", "expected_decisions_per_month": 8,
                    "mintrl_estimate_months": 6, "kill_bar": "PSR<0.20", "cost_model": "spot RT"}}
    e.update(over)
    return {f"hyp_t{idx}": e}


if san is None:
    for nm in ("sanitizer exists in autopilot (sanitize_hypotheses)",
               "cf keys outside the per-kind whitelist never survive",
               "origin outside the allowlist is dropped",
               "born_ts earlier than intake is re-stamped to intake",
               "cf_only is forced on every survivor",
               "slot cap HYP_MAX_SLOTS holds",
               "sanitizer never raises on garbage"):
        skip(nm, "hypotheses sanitizer not present in autopilot yet")
else:
    check("sanitizer exists in autopilot", callable(san))
    now = time.time()

    # cf whitelist
    hostile = _good(1, cf={"conf": 0.55, "horizon": "fwd48", "leverage": 50,
                           "size_usd": 1e6, "order_type": "market", "symbol": "PF_XBTUSD"})
    ents = _entries(_call(hostile, now))
    bad = [k for e in ents for k in (e.get("cf") or {}) if k not in WHITELIST.get("price", set())]
    check("cf keys outside the per-kind whitelist never survive (stripped or dropped)", not bad, bad)
    for kind in ("trend", "carry", "switch"):
        wl = WHITELIST.get(kind, set())
        h = _good(2, kind=kind, family="carry" if kind == "carry" else "trend",
                  cf=dict({k: 1 for k in wl}, leverage=10, api_key="x"))
        ents = _entries(_call(h, now))
        bad = [k for e in ents for k in (e.get("cf") or {}) if k not in wl]
        check(f"{kind}: cf whitelist enforced", not bad, bad)

    # origin allowlist
    for o in ("exchange_bot", "", None, 42, "LLM_PREREG "):
        ents = _entries(_call(_good(3, origin=o), now))
        check(f"origin {o!r} outside {sorted(ORIGINS)} is dropped or rewritten into the allowlist",
              all(e.get("origin") in ORIGINS for e in ents)
              and not any(e.get("origin") == o for e in ents if o not in ORIGINS), ents)
    ents = _entries(_call(_good(4, origin="owner_idea"), now))
    check("a valid owner_idea entry survives", len(ents) == 1 and ents[0].get("origin") == "owner_idea", ents)

    # born_ts re-stamp
    ents = _entries(_call(_good(5, born_ts=now - 90 * 86400), now))
    check("born_ts 90 days before intake is re-stamped to >= intake",
          ents and all(float(e.get("born_ts") or 0) >= now - 1 for e in ents),
          [e.get("born_ts") for e in ents])
    ents = _entries(_call(_good(6, born_ts=0), now))
    check("born_ts 0 is re-stamped", ents and all(float(e.get("born_ts") or 0) >= now - 1 for e in ents))
    ents = _entries(_call(_good(7, born_ts="yesterday"), now))
    check("non-numeric born_ts is re-stamped, not crashed",
          all(isinstance(e.get("born_ts"), (int, float)) and e["born_ts"] >= now - 1 for e in ents))
    ents = _entries(_call(_good(8, born_ts=now + 365 * 86400), now))
    check("a FUTURE born_ts is not accepted as-is (clamped to intake or dropped)",
          all(float(e.get("born_ts") or 0) <= time.time() + 1 for e in ents),
          [e.get("born_ts") for e in ents])

    # cf_only forced
    ents = _entries(_call(_good(9, cf_only=False), now))
    check("cf_only is forced True even when the file says False",
          ents and all(e.get("cf_only") is True for e in ents), ents)

    # ids
    raw = {}
    raw.update(_good(10))
    raw["base"] = dict(_good(11)["hyp_t11"])
    raw["lab_x"] = dict(_good(12)["hyp_t12"])
    raw["hyp_UPPER"] = dict(_good(13)["hyp_t13"])
    raw["hyp_" + "x" * 40] = dict(_good(14)["hyp_t14"])
    ents = _entries(_call(raw, now))
    check("ids outside ^hyp_[a-z0-9_]{1,24}$ (built-in / lab_ / upper / long) are dropped",
          all(ID_RE.match(str(e.get("id"))) for e in ents) and len(ents) == 1,
          [e.get("id") for e in ents])

    # slot cap
    raw = {}
    for i in range(MAX_SLOTS + 4):
        raw.update(_good(20 + i))
    ents = _entries(_call(raw, now))
    check(f"slot cap holds ({MAX_SLOTS} of {MAX_SLOTS + 4} kept)", len(ents) <= MAX_SLOTS, len(ents))

    # garbage never raises
    garbage = [None, [], {}, "text", 3.5, b"bytes", {"hyp_a": 5}, {"hyp_a": {"cf": "no"}},
               {"hyp_a": {"kind": "price", "cf": []}}, {"hyp_a": {"kind": "nope", "cf": {}}},
               {"hyp_a": {"kind": "price", "cf": {"conf": "high"}, "origin": "human"}},
               {5: {}}, {"hyp_a": {"kind": "price", "cf": {"conf": float("nan")},
                                   "origin": "human", "born_ts": float("inf")}},
               {"hyp_a": {"kind": "price", "cf": {"conf": 0.5}, "origin": "human",
                          "prereg": "not a dict", "note": 123}},
               [{"id": "hyp_a"}], {"hyp_a": {"kind": ["price"], "cf": {"horizon": "fwd48"}}}]
    raised = []
    for g in garbage:
        try:
            _entries(_call(g, now))
        except Exception as e:
            raised.append((repr(g)[:40], type(e).__name__))
    check("sanitizer never raises on garbage of any shape", not raised, raised)
    ents = _entries(_call({"hyp_a": {"kind": "price", "cf": {"conf": float("nan")},
                                     "origin": "human", "born_ts": float("inf")}}, now))
    check("NaN/inf values never survive into a hypothesis",
          all(all(v == v and v not in (float("inf"), float("-inf"))
                  for v in (e.get("cf") or {}).values() if isinstance(v, float))
              and float(e.get("born_ts") or 0) != float("inf") for e in ents))

# the file the bot reloads: same drop-not-raise contract as the lab file
reader = next((getattr(ap, n) for n in ("_read_hyp_file", "_read_hypotheses_file",
                                        "read_hypotheses_file")
               if callable(getattr(ap, n, None))), None)
if reader is None:
    skip("hypotheses file reader never raises on a corrupt file", "reader not present yet")
else:
    import json
    import tempfile
    p = os.path.join(tempfile.gettempdir(), f"hyp_corrupt_{os.getpid()}.json")
    with open(p, "w", encoding="utf-8") as f:
        f.write("{not json")
    ok = True
    try:
        try:
            reader(p)
        except TypeError:
            saved = bs._DATA_DIR
            try:
                bs._DATA_DIR = os.path.dirname(p)
                os.replace(p, os.path.join(bs._DATA_DIR, "hypotheses.json"))
                p = os.path.join(bs._DATA_DIR, "hypotheses.json")
                reader()
            finally:
                bs._DATA_DIR = saved
    except Exception as e:
        ok = False
        print("   reader raised:", type(e).__name__, e)
    finally:
        try:
            os.remove(p)
        except OSError:
            pass
    check("hypotheses file reader never raises on a corrupt file", ok)

# ── 4. research side never touches the owner's hand-trade table ──────────────
HAND_TABLE = "manual" + "_lab"                      # assembled so THIS file never embeds the name
HAND_HELPERS = {n for n in dir(bs) if HAND_TABLE in n.lower() or n.lower().startswith("_web_manual")}
for fname in ("research_loop.py", "research_pass.py", "research_evidence.py", "research_lab.py"):
    tree = _tree(fname)
    if tree is None:
        skip(f"{fname}: never imports/names the hand-trade table", "file not present yet")
        continue
    ids = _idents(tree)
    hits = sorted(i for i in ids if HAND_TABLE in i.lower() or i in HAND_HELPERS)
    check(f"{fname}: no identifier names the hand-trade table or its helpers", not hits, hits)
    s_hits = sorted(s[:50] for s in _strings(tree) if HAND_TABLE in s.lower())
    check(f"{fname}: no SQL/string mentions the hand-trade table", not s_hits, s_hits)
    src = _src(fname)
    check(f"{fname}: never mentions the hand-trade table even in comments (grep)",
          HAND_TABLE not in src.lower())

# research_evidence stays read-only and secret-free
src_re = _src("research_evidence.py")
if src_re is None:
    skip("research_evidence.py is SELECT-only", "file not present yet")
else:
    body = src_re.split('"""', 2)[-1]        # skip the module docstring
    check("research_evidence.py: no INSERT/UPDATE/DELETE/ALTER/DROP/COPY in code",
          not re.search(r"\b(INSERT|UPDATE|DELETE|ALTER|DROP|TRUNCATE)\b", body))
    check("research_evidence.py: never prints the DSN value",
          "print(dsn" not in body and "print(os.environ" not in body
          and 'DATABASE_URL")' not in body.replace('os.environ.get("DATABASE_URL")', ""))
    check("research_evidence.py: reads DATABASE_URL from the environment only",
          'os.environ.get("DATABASE_URL")' in body and "postgres://" not in body.replace("postgres://<scrubbed>", ""))
    check("research_evidence.py: imports learning_report (not a re-implementation)",
          "import learning_report" in body)


# ── 5. the EVIDENCE PACK never raises, never writes, never leaks the DSN ─────
# [E] promises ONE JSON document on stdout whose failed sections become null
# with a line in "errors". A pack that can raise leaves the research pass with
# nothing; a pack that can WRITE is no longer read-only. Both are checked by
# RUNNING build_pack against fake connections — an empty database, a database
# that raises on every statement, and one that returns garbage shapes — with
# every executed statement recorded so "read-only" is a measurement, not a
# promise in a docstring.
import json as _json
import urllib.request as _urlreq

try:
    import research_evidence as rev
except Exception as _e:                          # noqa: BLE001
    rev = None
    skip("evidence pack build_pack never raises", f"import failed: {type(_e).__name__}: {_e}")

if rev is not None:
    EXECUTED = []

    class _FakeCur:
        def __init__(self, owner):
            self.o = owner

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            EXECUTED.append(str(sql))
            if self.o.mode == "boom":
                raise RuntimeError("relation does not exist")

        def fetchall(self):
            if self.o.mode == "garbage":
                return [("only-one",), (), (object(), float("nan"), None)]
            return []

    class _FakeConn:
        def __init__(self, mode):
            self.mode, self.closed = mode, False

        def cursor(self):
            return _FakeCur(self)

        def close(self):
            self.closed = True

    SECTION_KEYS = ("regime_table", "gate_table", "spread_map_summary",
                    "rejects_by_gate_week", "shadow_counts", "graveyard",
                    "funding_summary", "tca_summary", "goal")

    # the goal section must never touch the network from a test
    _saved_urlopen = _urlreq.urlopen
    GOAL_BODY = {"goal": {"proven": [], "alive": 2, "killed": 1, "trials_count": 9,
                          "n_eff": 4.0, "sd_sr": 0.3, "sr0": 0.61,
                          "nearest_verdict": {"id": "cf_trend", "months": 5.0},
                          "families": {}, "budget_remaining": 3,
                          "book_state": "flat"}}

    class _FakeResp:
        def __init__(self, body):
            self._b = _json.dumps(body).encode()

        def read(self):
            return self._b

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _urlopen_ok(url, timeout=None):
        return _FakeResp(GOAL_BODY)

    def _urlopen_404(url, timeout=None):
        raise OSError("connection refused")

    docs = {}
    try:
        _urlreq.urlopen = _urlopen_ok
        for mode in ("empty", "boom", "garbage"):
            EXECUTED.clear()
            conn = _FakeConn(mode)
            raised = None
            try:
                docs[mode] = rev.build_pack(conn)
            except Exception as e:                # noqa: BLE001
                raised = f"{type(e).__name__}: {e}"
                docs[mode] = None
            check(f"build_pack never raises against a {mode} database", raised is None, raised)
            if docs[mode] is None:
                continue
            check(f"[{mode}] the pack is ONE serialisable JSON document",
                  isinstance(_json.dumps(docs[mode], default=rev._json_default), str))
            check(f"[{mode}] every contracted section key is present",
                  all(k in docs[mode] for k in SECTION_KEYS),
                  sorted(set(SECTION_KEYS) - set(docs[mode])))
            check(f"[{mode}] 'generated' and 'errors' always ride along",
                  "generated" in docs[mode] and isinstance(docs[mode].get("errors"), list))
            check(f"[{mode}] every statement it executed was a SELECT (read-only, measured)",
                  all(s.strip().upper().startswith("SELECT") for s in EXECUTED),
                  [s[:60] for s in EXECUTED if not s.strip().upper().startswith("SELECT")])

        check("a database that raises on every statement -> null sections + one error each",
              all(docs["boom"].get(k) is None for k in SECTION_KEYS if k != "goal")
              and len(docs["boom"]["errors"]) >= len(SECTION_KEYS) - 1,
              docs["boom"]["errors"][:2])
        check("the live goal block is passed through verbatim when the bot answers",
              docs["empty"].get("goal") == GOAL_BODY["goal"], docs["empty"].get("goal"))

        # no connection at all: still a document, still no exception
        EXECUTED.clear()
        d_noconn = rev.build_pack(None, conn_error="no DATABASE_URL in the environment")
        check("no connection -> a document, not a crash",
              isinstance(d_noconn, dict) and d_noconn["errors"]
              and "no DATABASE_URL" in d_noconn["errors"][0], d_noconn.get("errors"))
        check("with no connection NOTHING is executed",
              EXECUTED == [], EXECUTED[:2])

        # the goal seam is 404-safe from this side too
        _urlreq.urlopen = _urlopen_404
        d_nogoal = rev.build_pack(_FakeConn("empty"))
        check("an unreachable /api/goal leaves goal null with a reason, never a fake block",
              d_nogoal.get("goal") is None
              and any(e.startswith("goal:") for e in d_nogoal["errors"]),
              d_nogoal.get("errors"))
    finally:
        _urlreq.urlopen = _saved_urlopen

    # the DSN value never reaches the document or an error line
    _FAKE_DSN = "postgresql://cryptobot:sup3rs3cr3t@db:5432/cryptobot"
    scrubbed = rev._scrub(f"could not connect to {_FAKE_DSN} (password=sup3rs3cr3t)")
    check("_scrub removes the DSN and any password from an error text",
          "sup3rs3cr3t" not in scrubbed and "cryptobot:" not in scrubbed, scrubbed)
    _saved_dsn = os.environ.get("DATABASE_URL")
    try:
        os.environ["DATABASE_URL"] = _FAKE_DSN
        _conn, _err = rev._connect()
        check("a failed connect reports the failure WITHOUT the DSN value",
              _conn is None and _err and "sup3rs3cr3t" not in _err, _err)
    finally:
        if _saved_dsn is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = _saved_dsn

    all_text = _json.dumps(docs, default=rev._json_default)
    check("no rendered pack contains a password or a dsn",
          "postgres://" not in all_text.replace("postgres://<scrubbed>", "")
          and "postgresql://" not in all_text)

    # the pack imports the report's own table builders — it cannot disagree
    # with the numbers the owner reads
    check("regime/gate tables come from learning_report, not a re-implementation",
          rev.lr.regime_table.__module__ == "learning_report"
          and rev.lr.gate_table.__module__ == "learning_report")
    # end to end: no DSN at all, the script still prints ONE valid JSON
    # document and exits 0 (the document IS the report)
    _saved_dsn = os.environ.get("DATABASE_URL")
    _saved_out = sys.stdout
    _cap = io.StringIO()
    try:
        os.environ.pop("DATABASE_URL", None)
        _urlreq.urlopen = _urlopen_404
        sys.stdout = _cap
        _rc = rev.main([])
    finally:
        sys.stdout = _saved_out
        _urlreq.urlopen = _saved_urlopen
        if _saved_dsn is not None:
            os.environ["DATABASE_URL"] = _saved_dsn
    printed = _cap.getvalue()
    check("with no DSN the pack still exits 0 (a partial document beats none)", _rc == 0, _rc)
    parsed = None
    try:
        parsed = _json.loads(printed)
    except Exception as e:                       # noqa: BLE001
        check("stdout is exactly ONE valid JSON document", False, f"{type(e).__name__}: {e}")
    if parsed is not None:
        check("stdout is exactly ONE valid JSON document", True)
        check("...whose sections are null with the reason in errors, never invented",
              all(parsed.get(k) is None for k in SECTION_KEYS)
              and any("DATABASE_URL" in e for e in parsed.get("errors", [])),
              parsed.get("errors", [])[:2])

print()
if SKIPS:
    print(f"{len(SKIPS)} checks SKIPPED (module not present yet — held to the invariant when it lands)")
if FAILS:
    print(f"{len(FAILS)} FAILURES")
    sys.exit(1)
print("all hypothesis-loop safety invariants hold")
