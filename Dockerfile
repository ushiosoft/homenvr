FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg tzdata \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Vendored so the UI works without internet access.
ADD https://cdn.jsdelivr.net/npm/hls.js@1.5.15/dist/hls.min.js app/static/hls.min.js
COPY app app
RUN chmod 644 app/static/hls.min.js
ENV HOMENVR_CONFIG=/config/config.yml PYTHONUNBUFFERED=1
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz')"
CMD ["uvicorn", "app.main:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8080"]
