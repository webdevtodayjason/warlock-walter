FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

ENV MQTT_BROKER=mosquitto
ENV MQTT_PORT=1883
ENV BASECAMP_DB=/data/basecamp.db
EXPOSE 8000

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
