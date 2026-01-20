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

    # Server settings
    host: str = "0.0.0.0"
    port: int = 8000

    # Download settings
    metadata_timeout_seconds: int = 300  # 5 minutes max
    download_timeout_seconds: int = 1800  # 30 minutes max
    default_quality: str = "high"
    default_concurrent_fragments: int = 5
    default_merge_format: str = "mp4"

    # Streaming settings
    stream_chunk_size: int = 1024 * 1024  # 1MB chunks by default

    # Storage settings
    temp_download_dir: Path = Path("/tmp/yt-dlp-downloads")
    cleanup_on_error: bool = True
    
    # Concurrency settings
    max_concurrent_downloads: int = 10  # Prevent server overload
    
    # File size limits
    max_file_size_bytes: int = 1024 * 1024 * 1024  # 1GB default limit
    
    # Cookie file for YouTube authentication (to bypass bot detection)
    # Export cookies from browser using: yt-dlp --cookies-from-browser chrome --cookies cookies.txt
    cookies_file: Path | None = None

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Ensure temp directory exists
        self.temp_download_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
