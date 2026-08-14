FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot_server.py .
# Analysis tools that must exist INSIDE the container, where DATABASE_URL
# lives. Only bot_server.py was shipped before, so learning_report.py was
# committed to the repo but missing from the image — "run it in the container"
# failed on a file that was never there.
COPY learning_report.py .

RUN useradd -r -u 1001 -s /bin/false bot && mkdir -p /data && chown bot:bot /data

ENV DATA_DIR=/data
EXPOSE 8080

USER bot
CMD ["python", "bot_server.py"]
