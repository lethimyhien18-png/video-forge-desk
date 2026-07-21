FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates fonts-dejavu-core fontconfig \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir yt-dlp

COPY video_agent.py web_app.py /app/
COPY assets /app/assets
COPY downloads /app/downloads

EXPOSE 10000

CMD ["python3", "web_app.py"]
