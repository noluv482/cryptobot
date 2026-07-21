@echo off
setlocal

REM ── CryptoBot quick-start script (Windows) ───────────────────────────────────

where docker >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo Error: Docker is not installed. Get it at https://docs.docker.com/get-docker/
    pause
    exit /b 1
)

docker compose version >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo Error: docker compose plugin not found. Update Docker Desktop.
    pause
    exit /b 1
)

if not exist .env (
    copy .env.example .env
    echo.
    echo   .env file created from .env.example
    echo   Open .env in Notepad, fill in your Telegram token, chat ID, and PIN, then re-run this script.
    echo.
    pause
    exit /b 0
)

if not exist data mkdir data
if not exist pgdata mkdir pgdata

echo Starting CryptoBot...
docker compose up -d --build

echo.
echo   Bot is running!
echo   Open your browser at:  http://localhost:8081
echo.
echo   To view logs:   docker compose logs -f bot
echo   To stop:        docker compose down
echo.
pause
