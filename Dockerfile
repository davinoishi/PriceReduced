# Single-service image. Builds on the Pi (aarch64) and locally (arm64/amd64).
FROM python:3.12-slim

# extruct/lxml and selectolax ship wheels for common platforms, but keep a
# compiler around in case a source build is needed on the Pi.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Data (SQLite) lives on a mounted volume in compose.
RUN mkdir -p /app/data
ENV DATABASE_URL=sqlite:///./data/prices.db

EXPOSE 8000

# Becomes the full server at M4; M1 ships a minimal health app.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
