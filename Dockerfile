FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot_server.py .

RUN useradd -r -u 1001 -s /bin/false bot && mkdir -p /data && chown bot:bot /data

ENV DATA_DIR=/data
EXPOSE 8080

USER bot
CMD ["python", "bot_server.py"]
