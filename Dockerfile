FROM python:3.12-slim-bookworm
# libGL/libglib: OpenCV runtime deps even for the headless wheel's imdecode/dnn.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg tzdata libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Object detection for person/vehicle labels: YOLOX-tiny (Megvii, Apache-2.0) is the
# default; MobileNet-SSD (Pascal VOC) is the fallback if the YOLOX file is missing.
ADD https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_tiny.onnx /models/yolox_tiny.onnx
ADD https://raw.githubusercontent.com/djmv/MobilNet_SSD_opencv/master/MobileNetSSD_deploy.prototxt /models/MobileNetSSD_deploy.prototxt
ADD https://raw.githubusercontent.com/djmv/MobilNet_SSD_opencv/master/MobileNetSSD_deploy.caffemodel /models/MobileNetSSD_deploy.caffemodel
# Vendored so the UI works without internet access.
ADD https://cdn.jsdelivr.net/npm/hls.js@1.5.15/dist/hls.min.js app/static/hls.min.js
RUN chmod 644 /models/*
COPY app app
RUN chmod 644 app/static/hls.min.js
ENV HOMENVR_CONFIG=/config/config.yml PYTHONUNBUFFERED=1
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz')"
CMD ["uvicorn", "app.main:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8080"]
