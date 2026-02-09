FROM python:3.11-slim

WORKDIR /app

# Ensure pip is installed and up to date
RUN apt-get update && apt-get install -y python3-pip && pip install --upgrade pip

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1

CMD uvicorn main:app --host 0.0.0.0 --port $PORT
