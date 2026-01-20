# yt-dlp Streaming Microservice

A FastAPI-based microservice that wraps yt-dlp for downloading and streaming videos from YouTube and similar platforms.

## Features

- 🎥 **Video Downloads** - Download from YouTube, Vimeo, and 1000+ supported sites
- 🌊 **Streaming Response** - Files streamed directly to client, not loaded in memory
- ⏱️ **Execution Timeout** - Configurable timeout for downloads
- 🔒 **Concurrent Isolation** - Each download runs in isolated temporary directory
- 🧹 **Auto Cleanup** - Temporary files deleted after streaming completes
- 🌐 **CORS Enabled** - `Access-Control-Allow-Origin: *` for frontend integration
- 🐳 **Docker Ready** - Production-ready Docker and Docker Compose configuration

## Quick Start

### Using Docker Compose (Recommended)

```bash
# Clone and start
docker compose up -d

# View logs
docker compose logs -f

# Stop
docker compose down
```

### Using Docker

```bash
# Build
docker build -t yt-dlp-service .

# Run
docker run -d -p 8000:8000 --name yt-dlp-streamer yt-dlp-service
```

### Local Development

```bash
# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # Linux/Mac
# .venv\Scripts\activate   # Windows

# Install dependencies
pip install -r requirements.txt

# Install ffmpeg (required for merging)
# Ubuntu/Debian: sudo apt install ffmpeg
# macOS: brew install ffmpeg
# Windows: Download from ffmpeg.org

# Run
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## API Usage

### Endpoint: GET `/download`

Download and stream a video.

#### Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `source_url` | string | *required* | Video URL (YouTube, Vimeo, etc.) |
| `quality` | string | `high` | Quality: `best`, `high` (1080p), `medium` (720p), `low` (480p), `audio` |
| `noplaylist` | boolean | `true` | Download only the video, not entire playlist |
| `concurrent_fragment_downloads` | int | `5` | Concurrent fragment downloads (1-16) |
| `merge_output_format` | string | `mp4` | Output format: `mp4`, `mkv`, `webm`, `m4a`, `mp3` |
| `chunk_size` | int | `1048576` | Streaming chunk size in bytes (1KB - 10MB) |
| `metadata_timeout` | int | `300` | Metadata extraction timeout in seconds (10-300) |
| `download_timeout` | int | `1800` | Download timeout in seconds (30-7200) |

#### Example Requests

```bash
# Basic download (URL must be encoded - the '?' and '=' in YouTube URL need encoding)
curl -L "http://localhost:8000/download?source_url=https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3DVIDEO_ID" -o video.mp4

# High quality with custom settings
curl -L "http://localhost:8000/download?source_url=https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3DVIDEO_ID&quality=best&concurrent_fragment_downloads=10" -o video.mp4

# Audio only
curl -L "http://localhost:8000/download?source_url=https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3DVIDEO_ID&quality=audio&merge_output_format=m4a" -o audio.m4a

# Using --data-urlencode for automatic encoding (recommended)
curl -L -G "http://localhost:8000/download" \
  --data-urlencode "source_url=https://www.youtube.com/watch?v=VIDEO_ID" \
  --data-urlencode "quality=best" \
  -o video.mp4
```

> **Note:** The `source_url` parameter must be URL-encoded. Use `--data-urlencode` with `-G` flag for automatic encoding, or manually encode special characters:
> - `:` → `%3A`
> - `/` → `%2F`
> - `?` → `%3F`
> - `=` → `%3D`
> - `&` → `%26`

#### Response Headers

| Header | Description |
|--------|-------------|
| `Content-Disposition` | Filename for download |
| `Content-Type` | MIME type (`video/mp4`, etc.) |
| `X-Filename` | Original filename |

### Health Check: GET `/health`

```bash
curl http://localhost:8000/health
# {"status":"healthy","service":"yt-dlp-streamer"}
```

## Configuration

Configuration via environment variables (see `env.example`):

```bash
# Copy example config
cp env.example .env

# Edit as needed
nano .env
```

### Key Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `METADATA_TIMEOUT_SECONDS` | `300` | Max metadata extraction time (5 min) |
| `DOWNLOAD_TIMEOUT_SECONDS` | `1800` | Max download time (30 min) |
| `STREAM_CHUNK_SIZE` | `1048576` | 1MB streaming chunks |
| `DEFAULT_QUALITY` | `high` | Default video quality |
| `DEFAULT_CONCURRENT_FRAGMENTS` | `5` | Parallel fragment downloads |

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    FastAPI Application                       │
├─────────────────────────────────────────────────────────────┤
│  GET /download                                               │
│    ├── Create isolated session (UUID directory)             │
│    ├── Download via yt-dlp (with timeout)                   │
│    ├── Stream file to client as it grows (chunked)          │
│    └── Cleanup session directory                            │
├─────────────────────────────────────────────────────────────┤
│  Disk Buffer: /tmp/yt-dlp-downloads/{session-uuid}/         │
│    └── Automatic cleanup after streaming                    │
└─────────────────────────────────────────────────────────────┘
```

## Error Handling

| HTTP Status | Description |
|-------------|-------------|
| `200` | Success - streaming response (chunked transfer) |
| `400` | Invalid parameters |
| `408` | Download timeout exceeded |
| `500` | Download or server error |

## Resource Management

The Docker Compose configuration includes:

- **CPU Limit**: 2 cores (configurable via `CPU_LIMIT`)
- **Memory Limit**: 2GB (configurable via `MEMORY_LIMIT`)
- **tmpfs**: disabled by default (volume-backed temp storage)
- **Log Rotation**: Max 3 files, 10MB each

## Security Considerations

- Runs as non-root user in Docker
- Read-only security options enabled
- No new privileges allowed
- Automatic cleanup prevents disk exhaustion

## License

MIT License
