#!/usr/bin/env bash
set -e

# ── CryptoBot quick-start script (Linux / macOS) ──────────────────────────────

command -v docker >/dev/null 2>&1 || { echo "Error: Docker is not installed. Get it at https://docs.docker.com/get-docker/"; exit 1; }

# Support both docker compose (v2 plugin) and docker-compose (v1 standalone)
if docker compose version >/dev/null 2>&1; then
    DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    DC="docker-compose"
else
    echo "Error: neither 'docker compose' nor 'docker-compose' found."
    echo "Update Docker Desktop or install the compose plugin."
    exit 1
fi

# Copy example env file if .env doesn't exist
if [ ! -f .env ]; then
    cp .env.example .env
    echo ""
    echo "  .env file created from .env.example"
    echo "  Open .env in a text editor, fill in your Telegram token, chat ID, and PIN, then re-run this script."
    echo ""
    exit 0
fi

# Warn if required fields are still blank
if grep -qE "^(TG_TOKEN|TG_CHAT_ID|DASHBOARD_PIN)=$" .env 2>/dev/null; then
    echo "Warning: some required fields in .env are still blank (TG_TOKEN, TG_CHAT_ID, DASHBOARD_PIN)."
    echo "Fill them in before starting."
    exit 1
fi

# Create data dirs
mkdir -p data pgdata

echo "Starting CryptoBot..."
$DC up -d --build

echo ""
echo "  Bot is running!"
echo "  Open your browser at:  http://localhost:8081"
echo ""
echo "  To view logs:   $DC logs -f bot"
echo "  To stop:        $DC down"
