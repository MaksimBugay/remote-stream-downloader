# yt-dlp FastAPI Microservice
# Multi-stage build for optimal image size

FROM python:3.14.3-slim AS builder

# Set build-time environment
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --target=/build/deps -r requirements.txt


# Production stage
FROM python:3.14.3-slim

# Labels for container metadata
LABEL org.opencontainers.image.title="yt-dlp Streaming Service" \
      org.opencontainers.image.description="FastAPI microservice for video downloads" \
      org.opencontainers.image.version="1.0.0" \
      org.opencontainers.image.vendor="Custom" \
      maintainer="developer@example.com"

# Runtime environment
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/deps \
    # UTF-8 locale: critical for non-ASCII filenames (Russian, CJK, etc.)
    # python:slim uses C locale (ASCII) by default — files with Cyrillic/Unicode
    # names would fail or produce garbled output without this
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONIOENCODING=utf-8 \
    # Deno cache directory (for yt-dlp JS runtime)
    DENO_DIR=/tmp/deno-cache \
    # Application settings (can be overridden)
    HOST=0.0.0.0 \
    PORT=8000 \
    METADATA_TIMEOUT_SECONDS=300 \
    DOWNLOAD_TIMEOUT_SECONDS=1800 \
    STREAM_CHUNK_SIZE=1048576 \
    TEMP_DOWNLOAD_DIR=/tmp/yt-dlp-downloads \
    MAX_CONCURRENT_DOWNLOADS=10 \
    MAX_FILE_SIZE_BYTES=1073741824

WORKDIR /app

# Install runtime dependencies
# - ffmpeg: required for yt-dlp merging video/audio streams
# - nodejs: required for yt-dlp YouTube extraction (JS runtime for signature decryption)
# - curl/unzip: needed for deno installation
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    unzip \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /tmp/yt-dlp-downloads \
    && chmod 777 /tmp/yt-dlp-downloads

# Install Deno (recommended JS runtime for yt-dlp)
# Using official install script and moving to system path
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh \
    && deno --version

# Copy Python dependencies from builder
COPY --from=builder /build/deps /app/deps

# Copy application code
COPY app/ /app/app/

# Create non-root user for security
RUN useradd --create-home --shell /bin/bash appuser \
    && mkdir -p /tmp/deno-cache \
    && chown -R appuser:appuser /app /tmp/yt-dlp-downloads /tmp/deno-cache

USER appuser

# Expose the application port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# Run the application
CMD ["python", "-m", "app.main"]
