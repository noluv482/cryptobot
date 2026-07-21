FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot_server.py .

ENV DATA_DIR=/data
EXPOSE 8080

CMD ["python", "bot_server.py"]
