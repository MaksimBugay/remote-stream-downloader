"""Configuration settings for yt-dlp microservice."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings with environment variable support."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Server
    host: str = "0.0.0.0"
    port: int = 8000

    # Download
    metadata_timeout_seconds: int = 300
    download_timeout_seconds: int = 900
    default_quality: str = "best"
    default_concurrent_fragments: int = 8

    # Streaming
    stream_chunk_size: int = 1024 * 1024
    file_wait_timeout_seconds: int = 900

    # Storage
    temp_download_dir: Path = Path("/tmp/yt-dlp-downloads")
    cleanup_on_error: bool = True

    # Concurrency
    max_concurrent_downloads: int = 10
    download_slot_acquire_timeout_seconds: float = 1.0
    # Threads reserved for yt-dlp (metadata + downloads), separate from the
    # default pool that aiofiles uses for streaming reads.
    ytdlp_executor_workers: int = 100

    # File size
    max_file_size_bytes: int = 1024 * 1024 * 1024

    # Cookie file for general authentication (YouTube, etc.)
    cookies_file: Path | None = None

    # Cookie file specifically for VK/VKVideo (vk.com, vkvideo.ru)
    cookies_file_vk: Path | None = None

    # Proxy for yt-dlp (to bypass IP-based bot detection)
    proxy: str | None = None

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.temp_download_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
