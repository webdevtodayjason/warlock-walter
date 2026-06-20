FROM python:3.12-slim

WORKDIR /app

RUN mkdir -p /data

COPY basecamp/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY basecamp/server.py .

ENV MQTT_BROKER=broker.emqx.io
ENV MQTT_PORT=1883
ENV BASECAMP_DB=/data/basecamp.db
EXPOSE 8000

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
