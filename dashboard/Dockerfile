FROM python:3.12-slim
# ffmpeg: converts downloaded Twitch clips (HEVC) to H.264 for the BRB
# player's browser source - see transcode_clip() in app.py.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY static/ static/
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
