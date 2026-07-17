# Stateless YouTube stream-URL resolver, deployed on Cloud Run's always-free tier.
# No cookies, no persistent state — yt-dlp's own extractor works without login,
# confirmed by direct testing on 2026-07-17.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

ENV PORT=8080
EXPOSE 8080

CMD exec gunicorn --bind :$PORT --workers 2 --threads 4 --timeout 60 main:app
