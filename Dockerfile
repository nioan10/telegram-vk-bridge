FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
RUN pip install --no-cache-dir --only-binary=:all: -r requirements.txt
RUN useradd --system --uid 10001 bridge
RUN mkdir /app/state && chown bridge:bridge /app/state
COPY bridge ./bridge
COPY .env.example ./
USER bridge
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 CMD ["python", "-m", "bridge", "healthcheck"]
ENTRYPOINT ["python", "-m", "bridge"]
CMD ["run"]
