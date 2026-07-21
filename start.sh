#!/usr/bin/env bash
set -e

# ── CryptoBot quick-start script (Linux / macOS) ──────────────────────────────

command -v docker >/dev/null 2>&1 || { echo "Error: Docker is not installed. Get it at https://docs.docker.com/get-docker/"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "Error: docker compose plugin not found. Update Docker Desktop or install the compose plugin."; exit 1; }

# Copy example env file if .env doesn't exist
if [ ! -f .env ]; then
    cp .env.example .env
    echo ""
    echo "  .env file created from .env.example"
    echo "  Open .env in a text editor, fill in your Telegram token, chat ID, and PIN, then re-run this script."
    echo ""
    exit 0
fi

# Create data dirs
mkdir -p data pgdata

echo "Starting CryptoBot..."
docker compose up -d --build

echo ""
echo "  Bot is running!"
echo "  Open your browser at:  http://localhost:8081"
echo ""
echo "  To view logs:   docker compose logs -f bot"
echo "  To stop:        docker compose down"
