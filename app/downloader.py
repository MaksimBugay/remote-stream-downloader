"""yt-dlp download service with isolation and timeout support."""

import asyncio
import re
import shutil
import unicodedata
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Optional

import yt_dlp

from app.config import settings


class DownloadError(Exception):
    """Custom exception for download failures."""

    pass


class DownloadTimeoutError(DownloadError):
    """Exception raised when download exceeds timeout."""

    pass


class MetadataExtractionError(DownloadError):
    """Exception raised when metadata extraction fails."""

    pass


class FileSizeExceededError(DownloadError):
    """Exception raised when file size exceeds the configured limit."""

    pass


@dataclass
class VideoMetadata:
    """Video metadata extracted from source URL."""

    title: str
    uploader: str | None
    duration: int | None  # seconds
    description: str | None
    thumbnail: str | None
    webpage_url: str
    extractor: str
    video_id: str
    filesize: int | None  # bytes (exact if known)
    filesize_approx: int | None  # bytes (approximate estimate)

    @property
    def estimated_size(self) -> int | None:
        """Get the best available file size estimate in bytes."""
        return self.filesize or self.filesize_approx

    def exceeds_size_limit(self, max_bytes: int) -> bool:
        """Check if the estimated file size exceeds the given limit."""
        size = self.estimated_size
        return size is not None and size > max_bytes

    @property
    def safe_filename(self) -> str:
        """
        Generate a filesystem-safe filename from metadata.
        
        Preserves Unicode characters (Cyrillic, CJK, etc.) while removing
        only characters that are problematic for filesystems.
        """
        # Start with title
        name = (self.title or "").strip()

        # Normalize unicode to NFC (composed form) - keeps characters intact
        # Unlike NFKD which decomposes, NFC preserves the visual representation
        name = unicodedata.normalize("NFC", name)

        # Remove only filesystem-unsafe characters (Windows is most restrictive)
        # < > : " / \ | ? * are not allowed in Windows filenames
        name = re.sub(r'[<>:"/\\|?*]', "_", name)
        
        # Remove control characters (0x00-0x1F and 0x7F)
        name = re.sub(r"[\x00-\x1f\x7f]", "", name)
        
        # Remove leading/trailing dots and spaces (problematic on Windows)
        name = name.strip(". ")
        
        # Replace multiple consecutive spaces/underscores with single space
        name = re.sub(r"[\s_]+", " ", name)
        name = name.strip()

        # Limit length in bytes (filesystem limits vary, 200 chars is safe)
        # Use bytes limit for UTF-8 encoded names (some FS have byte limits)
        max_bytes = 200
        while len(name.encode("utf-8")) > max_bytes and len(name) > 1:
            name = name[:-1].strip()

        # Fallback if empty or only punctuation/whitespace
        if not name or re.sub(r"[-_.\s]", "", name) == "":
            fallback_id = self.video_id or "download"
            name = f"video_{fallback_id}"

        return name

    def compose_filename(self, extension: str, include_uploader: bool = False) -> str:
        """
        Compose a complete filename with extension.

        Args:
            extension: File extension (without dot).
            include_uploader: Whether to include uploader name in filename.

        Returns:
            Complete filename like "Video Title.mp4" or "Uploader - Video Title.mp4"
        """
        if include_uploader and self.uploader:
            # Sanitize uploader name (same rules as title)
            uploader_safe = unicodedata.normalize("NFC", self.uploader)
            uploader_safe = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", uploader_safe)
            uploader_safe = uploader_safe.strip(". ")
            return f"{uploader_safe} - {self.safe_filename}.{extension}"
        return f"{self.safe_filename}.{extension}"


class DownloadSession:
    """
    Manages an isolated download session with its own temporary directory.
    Ensures cleanup after use, even on errors.
    """

    def __init__(
        self,
        source_url: str,
        quality: str = settings.default_quality,
        concurrent_fragments: int = settings.default_concurrent_fragments,
        merge_format: str = settings.default_merge_format,
        noplaylist: bool = True,
        max_filesize: int | None = None,
    ):
        self.source_url = source_url
        self.quality = quality
        self.concurrent_fragments = concurrent_fragments
        self.merge_format = merge_format
        self.noplaylist = noplaylist
        self.max_filesize = max_filesize or settings.max_file_size_bytes

        # Create unique session directory for isolation
        self.session_id = str(uuid.uuid4())
        self.session_dir = settings.temp_download_dir / self.session_id
        self.output_file: Path | None = None
        self.metadata: VideoMetadata | None = None
        self._composed_filename: str | None = None

    def _get_extract_opts(self) -> dict:
        """Build yt-dlp options for metadata extraction only."""
        opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
            "noplaylist": self.noplaylist,
            "skip_download": True,
        }
        # Add cookies if configured (required for YouTube bot detection bypass)
        if settings.cookies_file and settings.cookies_file.exists():
            opts["cookiefile"] = str(settings.cookies_file)
        return opts

    def _get_ydl_opts(self) -> dict:
        """Build yt-dlp options dictionary for download."""
        format_selector = self._get_format_selector()

        # Use composed filename if metadata was extracted
        if self._composed_filename:
            output_template = str(self.session_dir / self._composed_filename)
            # Remove extension as yt-dlp will add it
            if output_template.endswith(f".{self.merge_format}"):
                output_template = output_template[: -(len(self.merge_format) + 1)]
            output_template += ".%(ext)s"
        else:
            output_template = str(self.session_dir / "%(title)s.%(ext)s")

        opts = {
            "format": format_selector,
            "outtmpl": output_template,
            "noplaylist": self.noplaylist,
            "concurrent_fragment_downloads": self.concurrent_fragments,
            "merge_output_format": self.merge_format,
            "nopart": True,
            "quiet": True,
            "no_warnings": True,
            "extract_flat": False,
            "writeinfojson": False,
            "writethumbnail": False,
            # No postprocessors - merge_output_format handles container format
            # during download, enabling true streaming-while-downloading
        }
        
        # Add cookies if configured (required for YouTube bot detection bypass)
        if settings.cookies_file and settings.cookies_file.exists():
            opts["cookiefile"] = str(settings.cookies_file)
        
        # Add file size limit if configured
        if self.max_filesize:
            opts["max_filesize"] = self.max_filesize
        
        return opts

    def _get_format_selector(self) -> str:
        """Map quality setting to yt-dlp format selector."""
        quality_map = {
            "best": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "high": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080]/best",
            "medium": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720]/best",
            "low": "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480]/best",
            "audio": "bestaudio[ext=m4a]/bestaudio",
        }
        return quality_map.get(self.quality, quality_map["best"])

    async def extract_metadata(
        self, timeout: Optional[int] = None
    ) -> VideoMetadata:
        """
        Extract video metadata without downloading.

        Returns:
            VideoMetadata object with video information.

        Raises:
            MetadataExtractionError: If extraction fails.
        """
        opts = self._get_extract_opts()

        try:
            loop = asyncio.get_running_loop()
            extract_task = loop.run_in_executor(None, self._sync_extract_info, opts)
            if timeout:
                info = await asyncio.wait_for(extract_task, timeout=timeout)
            else:
                info = await extract_task
        except asyncio.TimeoutError as e:
            raise MetadataExtractionError(
                f"Metadata extraction exceeded {timeout}s timeout"
            ) from e
        except Exception as e:
            raise MetadataExtractionError(f"Failed to extract metadata: {e}") from e

        if not info:
            raise MetadataExtractionError("No metadata returned from extraction")

        # Extract filesize - check multiple possible fields
        # yt-dlp provides filesize for direct downloads, filesize_approx for estimated
        filesize = info.get("filesize")
        filesize_approx = info.get("filesize_approx")
        
        # For format selection, check requested_formats for combined size
        if not filesize and not filesize_approx:
            requested_formats = info.get("requested_formats", [])
            if requested_formats:
                total_size = sum(
                    f.get("filesize") or f.get("filesize_approx") or 0
                    for f in requested_formats
                )
                if total_size > 0:
                    filesize_approx = total_size
        
        self.metadata = VideoMetadata(
            title=info.get("title", "Unknown Title"),
            uploader=info.get("uploader") or info.get("channel"),
            duration=info.get("duration"),
            description=info.get("description"),
            thumbnail=info.get("thumbnail"),
            webpage_url=info.get("webpage_url", self.source_url),
            extractor=info.get("extractor", "unknown"),
            video_id=info.get("id", self.session_id),
            filesize=filesize,
            filesize_approx=filesize_approx,
        )

        # Pre-compose the filename
        self._composed_filename = self.metadata.compose_filename(self.merge_format)

        return self.metadata

    def _sync_extract_info(self, opts: dict) -> dict:
        """Synchronously extract video info."""
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(self.source_url, download=False)

    async def download(self, timeout: int = settings.download_timeout_seconds) -> Path:
        """
        Execute the download with timeout.

        If metadata hasn't been extracted yet, it will be extracted first.

        Args:
            timeout: Maximum seconds to wait for download completion.

        Returns:
            Path to the downloaded file.

        Raises:
            DownloadTimeoutError: If download exceeds timeout.
            DownloadError: If download fails for any other reason.
        """
        # Extract metadata first if not already done
        if self.metadata is None:
            await self.extract_metadata(timeout=timeout)

        # Ensure session directory exists
        self.session_dir.mkdir(parents=True, exist_ok=True)

        opts = self._get_ydl_opts()

        try:
            loop = asyncio.get_running_loop()
            await asyncio.wait_for(
                loop.run_in_executor(None, self._sync_download, opts),
                timeout=timeout,
            )
        except asyncio.TimeoutError as e:
            raise DownloadTimeoutError(
                f"Download exceeded {timeout}s timeout"
            ) from e

        # Find the output file
        self.output_file = self._find_output_file()
        if not self.output_file:
            raise DownloadError("Download completed but output file not found")

        return self.output_file

    def start_download(self) -> asyncio.Future:
        """
        Start the download in a background executor and return the future.

        Returns:
            asyncio.Future that resolves when download completes or fails.
        """
        # Ensure session directory exists
        self.session_dir.mkdir(parents=True, exist_ok=True)
        opts = self._get_ydl_opts()
        loop = asyncio.get_running_loop()
        return loop.run_in_executor(None, self._sync_download, opts)

    def _sync_download(self, opts: dict) -> None:
        """Synchronous download using yt-dlp."""
        with yt_dlp.YoutubeDL(opts) as ydl:
            error_code = ydl.download([self.source_url])
            if error_code != 0:
                raise DownloadError(f"yt-dlp returned error code: {error_code}")

    def _find_output_file(self) -> Path | None:
        """Find the downloaded file in session directory."""
        if not self.session_dir.exists():
            return None

        # Look for video files with expected extensions
        extensions = [self.merge_format, "mp4", "mkv", "webm", "m4a", "mp3"]
        for ext in extensions:
            files = list(self.session_dir.glob(f"*.{ext}"))
            if files:
                # Return the largest file (in case of temp files)
                return max(files, key=lambda f: f.stat().st_size)

        # Fallback: return any file
        files = list(self.session_dir.iterdir())
        if files:
            return files[0]

        return None

    def get_file_size(self) -> int:
        """Get the size of the output file in bytes."""
        if self.output_file and self.output_file.exists():
            return self.output_file.stat().st_size
        return 0

    def get_expected_output_path(self) -> Path:
        """Get the expected output file path for streaming."""
        filename = self.get_filename()
        return self.session_dir / filename

    def get_filename(self) -> str:
        """
        Get the filename of the downloaded file.

        Returns filename from metadata if available, otherwise from actual file.
        """
        # Use pre-composed filename from metadata
        if self._composed_filename:
            return self._sanitize_filename(self._composed_filename)

        # Fallback to actual file name
        if self.output_file:
            return self._sanitize_filename(self.output_file.name)

        return f"download.{self.merge_format}"

    def _sanitize_filename(self, filename: str) -> str:
        """Normalize edge-case filenames like '-' or empty values."""
        name = (filename or "").strip()
        if not name:
            return f"download.{self.merge_format}"
        stem, dot, ext = name.rpartition(".")
        base = stem if dot else name
        if re.sub(r"[-_.\s]", "", base) == "":
            return f"download.{self.merge_format}"
        return name

    def cleanup(self) -> None:
        """Remove session directory and all contents."""
        if self.session_dir.exists():
            shutil.rmtree(self.session_dir, ignore_errors=True)


@asynccontextmanager
async def create_download_session(
    source_url: str,
    quality: str = settings.default_quality,
    concurrent_fragments: int = settings.default_concurrent_fragments,
    merge_format: str = settings.default_merge_format,
    noplaylist: bool = True,
    max_filesize: int | None = None,
) -> AsyncIterator[DownloadSession]:
    """
    Context manager for download sessions with automatic cleanup.

    Usage:
        async with create_download_session(url) as session:
            metadata = await session.extract_metadata()
            print(f"Downloading: {metadata.title}")
            await session.download()
            # Stream file...
        # Cleanup happens automatically
    """
    session = DownloadSession(
        source_url=source_url,
        quality=quality,
        concurrent_fragments=concurrent_fragments,
        merge_format=merge_format,
        noplaylist=noplaylist,
        max_filesize=max_filesize,
    )
    try:
        yield session
    finally:
        # Always cleanup, even on errors
        session.cleanup()
