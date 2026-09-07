#!/usr/bin/env python3
"""
Deploy preflight gate — static, dependency-free, runs BEFORE the image is built and
the bot is (re)started.

WHY THIS EXISTS
---------------
The bot runs under docker-compose `restart: unless-stopped`. A syntax error, or a
top-level import that blows up at runtime, turns every boot into an immediate crash
that Docker silently restarts forever — a crash-loop with the previous good code
already gone. This gate catches those cheaply and statically so the CI job aborts
BEFORE the running bot is ever touched.

WHAT IT DOES (and deliberately does NOT do)
-------------------------------------------
It is meant to run inside a bare `python:3.11-slim` container with NO project
dependencies installed — a full `pip install -r requirements.txt` is heavy and is
not needed to prove the code compiles and its imports are structurally sound.

  1. COMPILE EVERY .py in the tree with SyntaxWarning escalated to an error.
     Uses the builtin compile() (not py_compile) so it writes NO .pyc files into the
     mounted repo. A SyntaxError / IndentationError / escalated SyntaxWarning in any
     file fails the gate. This is the primary, fully-reliable check and is exactly
     the class of bug that produces the silent crash-loop above.

  2. GUARDED IMPORT SMOKE of the modules the bot cannot boot without:
     bot_server, autopilot, research_lab — plus research_evidence, which the
     research pass runs in-container and which must import cleanly there. Because project deps are absent, a real
     import stops at the first third-party module (e.g. `requests`) raising
     ModuleNotFoundError — that is EXPECTED and reported as SKIPPED, never a failure.
     Any OTHER exception raised while importing (a SyntaxError, or a genuine
     import-time bug such as a NameError in top-level code that runs before the first
     third-party import) fails the gate. This tolerance is why the check never
     false-fails a legitimate deploy just because numpy/pandas/flask aren't installed.

Exit code 0 = safe to deploy. Non-zero = abort the deploy.
"""

import importlib
import os
import sys
import warnings

# Directories that never contain first-party source and may be large or
# permission-restricted (postgres data dir, git internals, byte-code caches).
SKIP_DIRS = {"__pycache__", ".git", "pgdata", ".venv", "venv", "node_modules", ".mypy_cache"}

# The modules the bot process cannot start without.
CRITICAL_MODULES = ["bot_server", "autopilot", "research_lab", "research_evidence"]


def compile_all(root):
    """Compile every .py under root. Returns (count, [(path, message), ...])."""
    warnings.simplefilter("error", SyntaxWarning)
    failures = []
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        # prune skip-dirs in place so os.walk does not descend into them
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            count += 1
            try:
                with open(path, "rb") as fh:
                    source = fh.read()
                # builtin compile honours the PEP 263 encoding cookie and writes no
                # bytecode; SyntaxWarning is escalated to an exception by the filter.
                compile(source, path, "exec")
            except SyntaxWarning as exc:
                failures.append((path, "SyntaxWarning (escalated): %s" % (exc,)))
            except SyntaxError as exc:
                failures.append((path, "SyntaxError: %s" % (exc,)))
            except Exception as exc:  # e.g. undecodable source
                failures.append((path, "%s: %s" % (type(exc).__name__, exc)))
    return count, failures


def import_smoke(root, modules):
    """Guarded import of each critical module. Returns [(module, status, detail)].

    status is one of: "ok", "skipped" (a project dependency is not installed in this
    bare image — expected), or "failed" (a real import-time defect).
    """
    sys.path.insert(0, os.path.abspath(root))
    results = []
    for mod in modules:
        # drop any partially/previously loaded critical modules so each import is fresh
        for cached in CRITICAL_MODULES:
            sys.modules.pop(cached, None)
        try:
            importlib.import_module(mod)
            results.append((mod, "ok", "imported cleanly"))
        except ModuleNotFoundError as exc:
            # Expected in a deps-free image: the import chain hit an uninstalled
            # third-party package. Not a defect in our code.
            results.append((mod, "skipped", "needs uninstalled dependency: %s" % (exc.name,)))
        except SyntaxError as exc:
            results.append((mod, "failed", "SyntaxError: %s" % (exc,)))
        except Exception as exc:
            # A non-dependency import-time error (NameError, bad decorator, top-level
            # blow-up, ...). This is a real problem regardless of missing deps.
            results.append((mod, "failed", "%s: %s" % (type(exc).__name__, exc)))
    return results


def main():
    root = os.environ.get("PREFLIGHT_ROOT", ".")
    print("== Preflight: %s (python %s) ==" % (os.path.abspath(root), sys.version.split()[0]))

    count, compile_failures = compile_all(root)
    print("[compile] %d .py file(s) compiled with SyntaxWarning=error" % count)
    for path, message in compile_failures:
        print("[compile] FAIL  %s -> %s" % (path, message))

    import_failures = []
    for mod, status, detail in import_smoke(root, CRITICAL_MODULES):
        tag = {"ok": "OK  ", "skipped": "SKIP", "failed": "FAIL"}[status]
        print("[import]  %s %-13s %s" % (tag, mod, detail))
        if status == "failed":
            import_failures.append((mod, detail))

    ok = not compile_failures and not import_failures
    print("== Preflight %s ==" % ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
