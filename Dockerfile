# syntax=docker/dockerfile:1.7
FROM python:3.12-slim-bookworm
# libGL/libglib: OpenCV runtime deps even for the headless wheel's imdecode/dnn.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg tzdata libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Downloads are pinned by checksum: a changed upstream file fails the build.
# Object detection (person / vehicle / animal labels): YOLOX-tiny (Megvii, Apache-2.0).
ADD --checksum=sha256:427cc366d34e27ff7a03e2899b5e3671425c262ea2291f88bb942bc1cc70b0f7 \
    https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_tiny.onnx /models/yolox_tiny.onnx
# Vendored so the UI works without internet access.
ADD --checksum=sha256:a91c218fd92b39c2c929b1a08400bc8e85df34a5d474dece920103a2c51675df \
    https://cdn.jsdelivr.net/npm/hls.js@1.5.15/dist/hls.min.js app/static/hls.min.js
RUN chmod 644 /models/*
COPY app app
RUN chmod 644 app/static/hls.min.js
ENV HOMENVR_CONFIG=/config/config.yml PYTHONUNBUFFERED=1
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz')"
# Graceful-shutdown timeout: open video streams would otherwise hold a restart for
# as long as a viewer keeps the page open.
CMD ["uvicorn", "app.main:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8080", "--timeout-graceful-shutdown", "5"]
