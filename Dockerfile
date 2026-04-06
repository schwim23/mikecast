FROM python:3.11-slim

# ffmpeg for audio loudness normalization
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Secrets are injected as env vars at runtime (ECS task definition → SSM Parameter Store).
# No secrets are baked into the image.

ENTRYPOINT ["python", "mikecast_briefing.py"]
