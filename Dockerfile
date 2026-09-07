FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot_server.py .
# Modules bot_server imports at boot — a missing one is a crash loop
# (sizing.py was committed but not COPY'd: ModuleNotFoundError on deploy).
COPY sizing.py .
COPY meta_lab.py .
COPY funding_carry.py .
# Analysis tools that must exist INSIDE the container, where DATABASE_URL
# lives. Only bot_server.py was shipped before, so learning_report.py was
# committed to the repo but missing from the image — "run it in the container"
# failed on a file that was never there.
COPY learning_report.py .
# Same reason: this one reads the owner's manual book from /data and the
# manual_lab table, so it only works from inside the container.
COPY manual_report.py .
# bot_server.py imports this at boot (the paper autopilot allocator). Committed
# to the repo but, like learning_report.py above, it MUST be copied in explicitly
# or `import autopilot` raises ModuleNotFoundError and autopilot silently disables.
COPY autopilot.py .
# research_loop.py (2026-09-06): the PURE research-side helpers autopilot.py
# imports (registration budget, N_eff clustering, family posteriors, the
# template fallback). The import is guarded — a missing file degrades every
# consumer to 'unknown' — but the image must carry the real thing.
COPY research_loop.py .
# Nightly research lab (paper-only sweeps): bot_server spawns it as a subprocess,
# so a missing file fails the cycle at spawn time — same footgun, new victim.
COPY research_lab.py .
# Imported by research_lab.py for signal evaluation — omit it and the lab dies on
# ModuleNotFoundError just like autopilot.py did before it was copied in above.
COPY find_signal.py .
# Imported by research_lab.py to top up {DATA_DIR}/history CSVs — same story:
# committed to the repo is not the same as present in the image.
COPY fetch_history.py .
# Evidence pack (2026-09-06): read-only SQL over DATABASE_URL, printed as one
# JSON document for the PC-side research pass. Only useful INSIDE the
# container (that is where the DSN lives) — so it has to be in the image.
COPY research_evidence.py .
# Point-in-time leakage check (2026-09-06): `docker exec <bot> python
# test_leakage.py --live 50` rebuilds shadow_signals features from archived
# bars strictly before each row's ts. Same reason: DB-only, so ship it.
COPY test_leakage.py .

RUN useradd -r -u 1001 -s /bin/false bot && mkdir -p /data && chown bot:bot /data

ENV DATA_DIR=/data
EXPOSE 8080

USER bot
CMD ["python", "bot_server.py"]
