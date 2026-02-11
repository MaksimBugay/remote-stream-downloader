"""
yt-dlp FastAPI Microservice

A streaming video download service wrapping yt-dlp with:
- Configurable quality and format options
- Disk-buffered streaming with automatic cleanup
- Concurrent download isolation
- Execution timeout support
"""

import asyncio
import logging
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, AsyncIterator
from urllib.parse import quote, unquote

import aiofiles
import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from app.config import settings
from app.downloader import (
    DownloadError,
    DownloadSession,
    DownloadTimeoutError,
    FileSizeExceededError,
    MetadataExtractionError,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


class DownloadRequest(BaseModel):
    """Request body for POST /download endpoint."""
    
    url: str = Field(
        ...,
        description="Video URL (YouTube, Vimeo, LinkedIn, etc.)",
        examples=["https://www.youtube.com/watch?v=dQw4w9WgXcQ"],
    )
    quality: str = Field(
        default="high",
        description="Video quality: best, high (1080p), medium (720p), low (480p), audio",
    )
    noplaylist: bool = Field(
        default=True,
        description="Download only the video, not the playlist",
    )
    concurrent_fragment_downloads: int = Field(
        default=5,
        ge=1,
        le=16,
        description="Number of concurrent fragment downloads",
    )
    merge_output_format: str = Field(
        default="mp4",
        pattern="^(mp4|mkv|webm|m4a|mp3)$",
        description="Output container format",
    )
    chunk_size: int = Field(
        default=settings.stream_chunk_size,
        ge=1024,
        le=10485760,
        description="Streaming chunk size in bytes (default 1MB)",
    )
    metadata_timeout: int = Field(
        default=settings.metadata_timeout_seconds,
        ge=10,
        le=300,
        description="Metadata extraction timeout in seconds",
    )
    download_timeout: int = Field(
        default=settings.download_timeout_seconds,
        ge=30,
        le=7200,
        description="Download timeout in seconds",
    )
    max_file_size: int = Field(
        default=settings.max_file_size_bytes,
        ge=1024 * 1024,
        le=10 * 1024 * 1024 * 1024,
        description="Maximum file size in bytes (default 1GB)",
    )


class ThumbnailRequest(BaseModel):
    """Request body for POST /download/thumbnail endpoint."""

    url: str = Field(
        ...,
        description="Video URL (YouTube, Vimeo, LinkedIn, etc.)",
        examples=["https://www.youtube.com/watch?v=dQw4w9WgXcQ"],
    )
    metadata_timeout: int = Field(
        default=settings.metadata_timeout_seconds,
        ge=10,
        le=300,
        description="Metadata extraction timeout in seconds",
    )


# Semaphore for limiting concurrent downloads to prevent resource exhaustion
_download_semaphore: asyncio.Semaphore | None = None
_active_downloads: int = 0


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager for startup/shutdown tasks."""
    global _download_semaphore
    
    # Startup: initialize concurrency limiter and temp directory
    _download_semaphore = asyncio.Semaphore(settings.max_concurrent_downloads)
    settings.temp_download_dir.mkdir(parents=True, exist_ok=True)
    
    # Log cookies file status with detailed info
    def _log_cookies_file(file_path: Path | None, label: str):
        if file_path:
            logger.info(f"{label} cookies file path: {file_path}")
            if file_path.exists():
                cookie_size = file_path.stat().st_size
                logger.info(f"{label} cookies file found: {file_path} ({cookie_size} bytes)")
                # Log which domains have cookies (for debugging auth issues)
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        domains = set()
                        for line in f:
                            line = line.strip()
                            if line and not line.startswith("#"):
                                parts = line.split("\t")
                                if len(parts) >= 1:
                                    domains.add(parts[0])
                        if domains:
                            logger.info(f"{label} cookies file contains {len(domains)} unique domains")
                            # Check for key video platform domains
                            video_platforms = {
                                "youtube": [d for d in domains if "youtube" in d.lower()],
                                "vk": [d for d in domains if "vk" in d.lower()],
                                "vimeo": [d for d in domains if "vimeo" in d.lower()],
                                "rutube": [d for d in domains if "rutube" in d.lower()],
                                "dzen": [d for d in domains if "dzen" in d.lower()],
                            }
                            for platform, platform_domains in video_platforms.items():
                                if platform_domains:
                                    logger.info(f"{label} cookies for {platform}: {sorted(platform_domains)}")
                except Exception as e:
                    logger.warning(f"Could not parse {label} cookies file for domain info: {e}")
            else:
                logger.warning(f"{label} cookies file configured but NOT found: {file_path}")
        else:
            logger.info(f"No {label} cookies file configured")
    
    # Log general cookies (COOKIES_FILE)
    _log_cookies_file(settings.cookies_file, "General")
    
    # Log VK-specific cookies (COOKIES_FILE_VK)
    _log_cookies_file(settings.cookies_file_vk, "VK")
    
    logger.info(
        f"yt-dlp service started. Temp dir: {settings.temp_download_dir}, "
        f"max concurrent: {settings.max_concurrent_downloads}"
    )
    yield
    # Shutdown: cleanup could be added here if needed
    logger.info("yt-dlp service shutting down")


app = FastAPI(
    title="yt-dlp Streaming Service",
    description="Download and stream videos from YouTube and similar platforms",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS middleware with Access-Control-Allow-Origin: *
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=[
        "Content-Disposition",
        "X-Filename",
        "X-Filename-Encoded",
        "X-Thumbnail-Source",
        "X-Video-Title",
    ],
)


async def stream_file_with_cleanup(
    file_path: Path,
    chunk_size: int,
    cleanup_callback: Callable[[], None],
) -> AsyncIterator[bytes]:
    """
    Stream file contents in chunks, then cleanup after streaming completes.

    Args:
        file_path: Path to the file to stream.
        chunk_size: Size of each chunk in bytes.
        cleanup_callback: Function to call after streaming for cleanup.

    Yields:
        File content chunks.
    """
    try:
        async with aiofiles.open(file_path, "rb") as f:
            while chunk := await f.read(chunk_size):
                yield chunk
    except asyncio.CancelledError:
        logger.warning(f"Stream cancelled for {file_path}")
        raise
    except Exception as e:
        logger.error(f"Error streaming {file_path}: {e}")
        raise
    finally:
        # Cleanup after streaming (success or failure)
        try:
            cleanup_callback()
            logger.info("Cleaned up session after streaming")
        except Exception as e:
            logger.error(f"Cleanup error: {e}")


async def _deferred_cleanup(
    download_task: asyncio.Task,
    cleanup_callback: Callable[[], None],
    timeout: float = 300,
) -> None:
    """
    Run cleanup after download task finishes.
    
    Used when streaming is cancelled (e.g., client disconnect) and we can't
    await the download in the generator's finally block due to CancelledError.
    Runs as an independent task that won't be cancelled with the streaming.
    """
    try:
        if not download_task.done():
            logger.info("Deferred cleanup: waiting for download to finish...")
            await asyncio.wait_for(download_task, timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning(f"Deferred cleanup: download didn't finish within {timeout}s, proceeding")
        download_task.cancel()
    except (asyncio.CancelledError, Exception) as e:
        logger.debug(f"Deferred cleanup: download task ended: {type(e).__name__}: {e}")
    try:
        cleanup_callback()
        logger.info("Deferred cleanup completed")
    except Exception as e:
        logger.error(f"Deferred cleanup error: {e}")


async def stream_file_while_downloading(
    file_path: Path,
    chunk_size: int,
    download_task: asyncio.Task,
    cleanup_callback: Callable[[], None],
    max_file_size: int | None = None,
    poll_interval: float = 0.25,
) -> AsyncIterator[bytes]:
    """
    Stream a file while it is being downloaded, without Content-Length.

    Args:
        file_path: Path to the file to stream.
        chunk_size: Size of each chunk in bytes.
        download_task: Task that completes when download finishes or fails.
        cleanup_callback: Function to call after streaming for cleanup.
        max_file_size: Maximum allowed file size in bytes (None = no limit).
        poll_interval: Sleep interval while waiting for more data.
    
    Raises:
        FileSizeExceededError: If file size exceeds max_file_size during download.
        DownloadError: If download fails before file is created.
    """
    bytes_streamed = 0
    download_error: Exception | None = None
    
    try:
        # Wait for file to be created - errors here can be raised since no data sent yet
        # For videos with separate video+audio tracks, yt-dlp downloads both first,
        # then merges with ffmpeg - this can take a long time for big files
        wait_count = 0
        max_wait_seconds = settings.file_wait_timeout_seconds
        max_wait = int(max_wait_seconds / poll_interval)
        last_log_time = 0.0
        
        while not file_path.exists():
            if download_task.done():
                exc = download_task.exception()
                if exc:
                    raise exc
                # Check if file appeared right before task completed
                if file_path.exists():
                    break
                raise DownloadError("Download finished but output file not found")
            wait_count += 1
            elapsed = wait_count * poll_interval
            
            # Log progress every 30 seconds
            if elapsed - last_log_time >= 30:
                logger.info(f"Waiting for output file... ({elapsed:.0f}s elapsed, file: {file_path.name})")
                last_log_time = elapsed
            
            if wait_count > max_wait:
                raise DownloadError(f"Timeout waiting for download to start ({max_wait_seconds}s)")
            await asyncio.sleep(poll_interval)

        async with aiofiles.open(file_path, "rb") as f:
            while True:
                chunk = await f.read(chunk_size)
                if chunk:
                    bytes_streamed += len(chunk)
                    
                    # Check file size limit during streaming
                    if max_file_size and bytes_streamed > max_file_size:
                        download_task.cancel()
                        raise FileSizeExceededError(
                            f"File size exceeded {max_file_size / (1024*1024):.1f}MB limit "
                            f"(streamed {bytes_streamed / (1024*1024):.1f}MB)"
                        )
                    
                    yield chunk
                    continue
                    
                if download_task.done():
                    exc = download_task.exception()
                    if exc:
                        # Once we've started streaming, we can't raise an HTTP error.
                        # Log the error and end the stream gracefully.
                        # The client will receive an incomplete file.
                        logger.error(f"Download failed after streaming {bytes_streamed / (1024*1024):.1f}MB: {exc}")
                        download_error = exc
                    break
                await asyncio.sleep(poll_interval)
        
        if download_error:
            logger.warning(f"Stream ended due to download error (client received {bytes_streamed / (1024*1024):.1f}MB)")
        else:
            logger.info(f"Stream completed successfully ({bytes_streamed / (1024*1024):.1f}MB)")
            
    finally:
        # Wait for download task to complete before cleanup
        # This ensures ffmpeg post-processing finishes before we delete files
        # For big files, ffmpeg merge can take several minutes
        if not download_task.done():
            logger.info("Waiting for download/merge to complete before cleanup...")
            try:
                # Give it plenty of time for ffmpeg merge on large files
                await asyncio.wait_for(asyncio.shield(download_task), timeout=900)
                logger.debug("Download task completed normally")
            except asyncio.TimeoutError:
                logger.warning("Download task timed out (15min) during cleanup wait, proceeding with cleanup")
                download_task.cancel()
            except asyncio.CancelledError:
                # Streaming was cancelled (e.g., client disconnected).
                # asyncio.shield protects download_task from cancellation, but the
                # await itself raises CancelledError in this coroutine. We can't
                # wait here, so defer cleanup to an independent background task
                # that will properly wait for the download thread to finish before
                # deleting session files (avoiding FileNotFoundError in yt-dlp).
                logger.info("Stream cancelled, deferring cleanup until download finishes")
                asyncio.create_task(_deferred_cleanup(download_task, cleanup_callback))
                return
            except Exception as e:
                # Log but don't fail - we still need to cleanup
                logger.debug(f"Download task finished with error during cleanup wait: {type(e).__name__}: {e}")
        
        try:
            cleanup_callback()
            logger.info("Cleaned up session after streaming")
        except Exception as e:
            logger.error(f"Cleanup error: {e}")


async def _monitor_download(
    download_future: asyncio.Future,
    timeout: int,
    cleanup_callback: Callable[[], None] | None = None,
) -> None:
    """
    Await download completion with timeout enforcement.
    
    On timeout, triggers cleanup to prevent resource leaks from orphaned downloads.
    Note: This runs as a background task. Exceptions here are logged but may not
    propagate to the streaming response if streaming has already started.
    """
    try:
        await asyncio.wait_for(download_future, timeout=timeout)
    except asyncio.TimeoutError:
        # Cancel the future to stop background download
        download_future.cancel()
        # Cleanup resources immediately on timeout
        if cleanup_callback:
            try:
                cleanup_callback()
            except Exception as e:
                logger.error(f"Cleanup error on timeout: {e}")
        raise DownloadTimeoutError(f"Download exceeded {timeout}s timeout")
    except asyncio.CancelledError:
        # Task was cancelled (e.g., client disconnected)
        logger.info("Download monitor task was cancelled")
        raise
    except FileNotFoundError as e:
        # This can happen if cleanup runs before ffmpeg finishes merging.
        # If streaming completed successfully, this is not a problem.
        logger.warning(f"File not found during download (likely already cleaned up): {e}")
    except Exception as e:
        # Log unexpected errors to prevent "Task exception was never retrieved"
        logger.error(f"Unexpected error in download monitor: {type(e).__name__}: {e}")


@app.get("/health")
async def health_check() -> dict:
    """Health check endpoint with service status."""
    return {
        "status": "healthy",
        "service": "yt-dlp-streamer",
        "active_downloads": _active_downloads,
        "max_concurrent_downloads": settings.max_concurrent_downloads,
    }


@app.get("/debug/cookies")
async def debug_cookies() -> dict:
    """Debug endpoint to check cookies file status and content."""
    result = {
        "cookies_file_configured": str(settings.cookies_file) if settings.cookies_file else None,
        "cookies_file_exists": False,
        "cookies_file_size": 0,
        "total_domains": 0,
        "video_platform_domains": {},
        "vk_cookies_count": 0,
        "sample_vk_cookies": [],
    }
    
    if not settings.cookies_file:
        return result
    
    result["cookies_file_exists"] = settings.cookies_file.exists()
    
    if not settings.cookies_file.exists():
        return result
    
    result["cookies_file_size"] = settings.cookies_file.stat().st_size
    
    try:
        with open(settings.cookies_file, "r", encoding="utf-8") as f:
            domains = set()
            vk_cookies = []
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    parts = line.split("\t")
                    if len(parts) >= 7:
                        domain = parts[0]
                        cookie_name = parts[5]
                        domains.add(domain)
                        # Collect VK cookies info (without values for security)
                        if "vk" in domain.lower():
                            vk_cookies.append({
                                "domain": domain,
                                "name": cookie_name,
                                "path": parts[2],
                            })
            
            result["total_domains"] = len(domains)
            result["vk_cookies_count"] = len(vk_cookies)
            result["sample_vk_cookies"] = vk_cookies[:20]  # First 20 VK cookies
            
            # Categorize video platform domains
            result["video_platform_domains"] = {
                "youtube": sorted([d for d in domains if "youtube" in d.lower()]),
                "vk": sorted([d for d in domains if "vk" in d.lower()]),
                "vimeo": sorted([d for d in domains if "vimeo" in d.lower()]),
                "rutube": sorted([d for d in domains if "rutube" in d.lower()]),
            }
    except Exception as e:
        result["error"] = str(e)
    
    return result


@app.get("/download")
async def download_video(
    source_url: Annotated[
        str,
        Query(
            description="Video URL (YouTube, Vimeo, etc.)",
            examples=["https://www.youtube.com/watch?v=dQw4w9WgXcQ"],
        ),
    ],
    quality: Annotated[
        str,
        Query(
            description="Video quality: best, high (1080p), medium (720p), low (480p), audio",
        ),
    ] = "high",
    noplaylist: Annotated[
        bool,
        Query(description="Download only the video, not the playlist"),
    ] = True,
    concurrent_fragment_downloads: Annotated[
        int,
        Query(
            ge=1,
            le=16,
            description="Number of concurrent fragment downloads",
        ),
    ] = 5,
    merge_output_format: Annotated[
        str,
        Query(
            description="Output container format",
            pattern="^(mp4|mkv|webm|m4a|mp3)$",
        ),
    ] = "mp4",
    chunk_size: Annotated[
        int,
        Query(
            ge=1024,
            le=10485760,  # 10MB max chunk
            description="Streaming chunk size in bytes (default 1MB)",
        ),
    ] = settings.stream_chunk_size,
    metadata_timeout: Annotated[
        int,
        Query(
            ge=10,
            le=300,
            description="Metadata extraction timeout in seconds",
        ),
    ] = settings.metadata_timeout_seconds,
    download_timeout: Annotated[
        int,
        Query(
            ge=30,
            le=7200,
            description="Download timeout in seconds",
        ),
    ] = settings.download_timeout_seconds,
    max_file_size: Annotated[
        int,
        Query(
            ge=1024 * 1024,  # 1MB minimum
            le=10 * 1024 * 1024 * 1024,  # 10GB maximum
            description="Maximum file size in bytes (default 1GB)",
        ),
    ] = settings.max_file_size_bytes,
) -> StreamingResponse:
    """
    Download and stream a video from the provided URL.

    The video is downloaded to a temporary location, then streamed to the client.
    After streaming completes, the temporary file is automatically deleted.

    Returns:
        StreamingResponse with video content and appropriate headers.

    Raises:
        HTTPException 400: Invalid URL or parameters.
        HTTPException 408: Download timeout exceeded.
        HTTPException 500: Download or streaming error.
    """
    global _active_downloads
    
    logger.info(f"Download request: {source_url} (quality={quality})")
    
    # Acquire semaphore to limit concurrent downloads
    if _download_semaphore is None:
        raise HTTPException(status_code=503, detail="Service not ready")
    
    try:
        # Try to acquire immediately, fail fast if at capacity
        acquired = _download_semaphore.locked()
        if acquired and _active_downloads >= settings.max_concurrent_downloads:
            raise HTTPException(
                status_code=503,
                detail=f"Server at capacity ({settings.max_concurrent_downloads} concurrent downloads). Try again later.",
            )
    except HTTPException:
        raise
    
    await _download_semaphore.acquire()
    _active_downloads += 1
    logger.info(f"Active downloads: {_active_downloads}/{settings.max_concurrent_downloads}")

    # Create isolated download session (manual lifecycle to keep files during stream)
    session = DownloadSession(
        source_url=source_url,
        quality=quality,
        concurrent_fragments=concurrent_fragment_downloads,
        merge_format=merge_output_format,
        noplaylist=noplaylist,
        max_filesize=max_file_size,
    )
    
    def release_and_cleanup() -> None:
        """Release semaphore and cleanup session."""
        global _active_downloads
        try:
            session.cleanup()
        finally:
            _download_semaphore.release()
            _active_downloads -= 1
            logger.info(f"Active downloads: {_active_downloads}/{settings.max_concurrent_downloads}")
    
    cleanup_fn = release_and_cleanup
    
    try:
        # Extract metadata first to get proper filename
        try:
            metadata = await session.extract_metadata(timeout=metadata_timeout)
            
            # Format file size for logging
            size_info = ""
            if metadata.estimated_size:
                size_mb = metadata.estimated_size / (1024 * 1024)
                size_info = f", estimated_size={size_mb:.1f}MB"
            
            logger.info(
                f"Video metadata: title='{metadata.title}', "
                f"uploader='{metadata.uploader}', "
                f"duration={metadata.duration}s{size_info}"
            )
            
            # Early rejection if file size exceeds limit (when known from metadata)
            if metadata.exceeds_size_limit(max_file_size):
                estimated_mb = metadata.estimated_size / (1024 * 1024)
                limit_mb = max_file_size / (1024 * 1024)
                raise FileSizeExceededError(
                    f"Video size ({estimated_mb:.1f}MB) exceeds limit ({limit_mb:.1f}MB)"
                )
                
        except MetadataExtractionError as e:
            logger.error(f"Metadata extraction failed: {e}")
            raise HTTPException(
                status_code=400,
                detail=f"Failed to extract video info: {str(e)}",
            )

        # Start download in background and stream as it grows
        download_future = session.start_download()
        download_task = asyncio.create_task(
            _monitor_download(download_future, download_timeout, cleanup_fn)
        )

        filename = session.get_filename().strip()
        if not filename:
            filename = f"download.{merge_output_format}"
        logger.info(f"Download started: {filename}")

        # Determine media type
        media_type = _get_media_type(merge_output_format)

        # Stream the file with cleanup callback after streaming completes
        file_path = session.get_expected_output_path()

        # HTTP headers must be ASCII/latin-1, so we need:
        # - filename: ASCII fallback for older clients
        # - filename*: UTF-8 encoded (RFC 5987) for modern clients
        ascii_filename = filename.encode("ascii", "replace").decode("ascii")
        ascii_filename = ascii_filename.replace('"', "'")  # Escape quotes
        
        content_disposition = (
            f'attachment; filename="{ascii_filename}"; '
            f"filename*=UTF-8''{quote(filename, safe='')}"
        )

        return StreamingResponse(
            content=stream_file_while_downloading(
                file_path, chunk_size, download_task, cleanup_fn, max_file_size
            ),
            media_type=media_type,
            headers={
                "Content-Disposition": content_disposition,
                "X-Filename": ascii_filename,
                "X-Filename-Encoded": quote(filename, safe=""),  # UTF-8 URL-encoded
                "X-Content-Type": media_type,  # Expose MIME type in custom header
            },
        )
    except DownloadTimeoutError as e:
        logger.error(f"Download timeout: {e}")
        raise HTTPException(status_code=408, detail=str(e))
    except FileSizeExceededError as e:
        logger.warning(f"File size exceeded: {e}")
        raise HTTPException(status_code=413, detail=str(e))
    except HTTPException:
        # Cleanup before re-raising HTTP exceptions
        if settings.cleanup_on_error:
            cleanup_fn()
        raise
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        if settings.cleanup_on_error:
            cleanup_fn()
        raise HTTPException(
            status_code=500,
            detail=f"Internal server error: {str(e)}",
        )


def _get_media_type(format: str) -> str:
    """Map format to MIME type."""
    mime_types = {
        "mp4": "video/mp4",
        "mkv": "video/x-matroska",
        "webm": "video/webm",
        "m4a": "audio/mp4",
        "mp3": "audio/mpeg",
    }
    return mime_types.get(format, "application/octet-stream")


def _decode_url_if_needed(url: str) -> str:
    """
    Decode URL if it appears to be URL-encoded.
    
    Handles cases where URL was encoded before being added to JSON, e.g.:
    - "https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3DdQw4w9WgXcQ"
    - "https://www.youtube.com/watch?v=dQw4w9WgXcQ" (already decoded, returns as-is)
    """
    # Check if URL looks encoded (doesn't start with http:// or https://)
    if url and not url.startswith(("http://", "https://")):
        # Try to decode it
        decoded = unquote(url)
        if decoded.startswith(("http://", "https://")):
            logger.debug(f"URL was encoded, decoded: {decoded[:100]}...")
            return decoded
    
    # Also handle double-encoded or partially encoded URLs
    # by checking for common encoded patterns
    if "%3A" in url or "%2F" in url or "%3F" in url:
        decoded = unquote(url)
        logger.debug(f"URL contained encoded chars, decoded: {decoded[:100]}...")
        return decoded
    
    return url


@app.post("/download")
async def download_video_post(request: DownloadRequest) -> StreamingResponse:
    """
    Download and stream a video from the provided URL (POST version).
    
    Accepts JSON body with URL and optional parameters.
    Shares implementation with GET /download endpoint.
    
    The URL can be provided as-is or URL-encoded:
    - "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    - "https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3DdQw4w9WgXcQ"
    
    Example request body:
    ```json
    {
        "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "quality": "high",
        "merge_output_format": "mp4"
    }
    ```
    
    Returns:
        StreamingResponse with video content and appropriate headers.
    """
    # Decode URL if it was URL-encoded before being added to JSON
    source_url = _decode_url_if_needed(request.url)
    
    # Delegate to the GET endpoint implementation with extracted parameters
    return await download_video(
        source_url=source_url,
        quality=request.quality,
        noplaylist=request.noplaylist,
        concurrent_fragment_downloads=request.concurrent_fragment_downloads,
        merge_output_format=request.merge_output_format,
        chunk_size=request.chunk_size,
        metadata_timeout=request.metadata_timeout,
        download_timeout=request.download_timeout,
        max_file_size=request.max_file_size,
    )


def _guess_image_mime_type(content_type: str | None, url: str) -> tuple[str, str]:
    """
    Determine image MIME type and file extension from HTTP Content-Type or URL.

    Args:
        content_type: Content-Type header value from the HTTP response.
        url: The thumbnail URL (used as fallback for extension-based detection).

    Returns:
        Tuple of (mime_type, file_extension).
    """
    # Try Content-Type header first
    if content_type:
        ct = content_type.lower().split(";")[0].strip()
        mime_map = {
            "image/jpeg": ("image/jpeg", "jpg"),
            "image/jpg": ("image/jpeg", "jpg"),
            "image/png": ("image/png", "png"),
            "image/webp": ("image/webp", "webp"),
            "image/gif": ("image/gif", "gif"),
            "image/bmp": ("image/bmp", "bmp"),
            "image/svg+xml": ("image/svg+xml", "svg"),
        }
        if ct in mime_map:
            return mime_map[ct]

    # Fall back to URL extension
    url_path = url.lower().split("?")[0]
    ext_map = {
        ".jpg": ("image/jpeg", "jpg"),
        ".jpeg": ("image/jpeg", "jpg"),
        ".png": ("image/png", "png"),
        ".webp": ("image/webp", "webp"),
        ".gif": ("image/gif", "gif"),
    }
    for ext, result in ext_map.items():
        if url_path.endswith(ext):
            return result

    # Default to JPEG (most common for video thumbnails)
    return ("image/jpeg", "jpg")


async def _generate_thumbnail_from_stream(
    video_url: str,
    headers: dict[str, str] | None = None,
    seek_seconds: int = 3,
    timeout: float = 30.0,
) -> bytes:
    """
    Extract a single JPEG frame from a video stream URL using ffmpeg.

    Only downloads enough data to decode one frame at the seek position —
    does NOT download the entire video file.

    Args:
        video_url: Direct URL to the video stream.
        headers: Optional HTTP headers (User-Agent, Cookie, etc.) for the request.
        seek_seconds: Position to seek to before extracting the frame.
        timeout: Maximum time to wait for ffmpeg to finish.

    Returns:
        JPEG image bytes.

    Raises:
        RuntimeError: If ffmpeg fails or produces no output.
    """
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel", "error",
    ]

    # Pass HTTP headers to ffmpeg (User-Agent, Cookie, Referer, etc.)
    if headers:
        header_str = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        cmd += ["-headers", header_str]

    cmd += [
        # -ss before -i = fast seek (demuxer-level), minimal data downloaded
        "-ss", str(seek_seconds),
        "-i", video_url,
        "-frames:v", "1",
        "-vf", "scale='min(640,iw)':-2",
        "-f", "image2pipe",
        "-vcodec", "mjpeg",
        "-q:v", "3",
        "pipe:1",
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"ffmpeg thumbnail extraction timed out ({timeout}s)")

    if proc.returncode != 0 or not stdout:
        error_msg = stderr.decode(errors="replace")[:500] if stderr else "unknown error"
        raise RuntimeError(f"ffmpeg thumbnail extraction failed (rc={proc.returncode}): {error_msg}")

    return stdout


@app.get("/download/thumbnail")
async def download_thumbnail(
    source_url: Annotated[
        str,
        Query(
            description="Video URL (YouTube, Vimeo, etc.)",
            examples=["https://www.youtube.com/watch?v=dQw4w9WgXcQ"],
        ),
    ],
    metadata_timeout: Annotated[
        int,
        Query(
            ge=10,
            le=300,
            description="Metadata extraction timeout in seconds",
        ),
    ] = settings.metadata_timeout_seconds,
) -> Response:
    """
    Extract and return the video thumbnail from the provided URL.

    Tries two strategies in order:
    1. Platform-native thumbnail — downloads the thumbnail image URL provided
       by YouTube, Facebook, VK, etc. (fast, no video data needed).
    2. ffmpeg frame extraction — if no native thumbnail exists, extracts a
       single frame from the lowest-quality video stream. Only downloads
       enough bytes to decode one frame, NOT the entire video.

    Returns:
        Response with binary image content and appropriate headers.

    Raises:
        HTTPException 400: Invalid URL or metadata extraction failure.
        HTTPException 404: No thumbnail could be obtained by any strategy.
        HTTPException 502: Failed to download thumbnail from source platform.
    """
    logger.info(f"Thumbnail request: {source_url}")

    # Create a lightweight session for metadata extraction only.
    # No download directory is created — extract_metadata is network-only.
    session = DownloadSession(source_url=source_url)

    try:
        # Extract metadata to get thumbnail URL + video stream URL
        try:
            metadata = await session.extract_metadata(timeout=metadata_timeout)
        except MetadataExtractionError as e:
            logger.error(f"Metadata extraction failed for thumbnail: {e}")
            raise HTTPException(
                status_code=400,
                detail=f"Failed to extract video info: {str(e)}",
            )

        image_bytes: bytes | None = None
        mime_type: str = "image/jpeg"
        ext: str = "jpg"
        source: str = "unknown"

        # --- Strategy 1: Platform-native thumbnail URL ---
        thumbnail_url = metadata.thumbnail
        if thumbnail_url:
            logger.info(
                f"Trying platform thumbnail for '{metadata.title}': "
                f"{thumbnail_url[:120]}..."
            )
            try:
                async with httpx.AsyncClient(
                    follow_redirects=True,
                    timeout=30.0,
                ) as client:
                    resp = await client.get(thumbnail_url)
                    resp.raise_for_status()
                    if resp.content:
                        image_bytes = resp.content
                        content_type = resp.headers.get("content-type")
                        mime_type, ext = _guess_image_mime_type(
                            content_type, thumbnail_url
                        )
                        source = "platform"
                        logger.info(
                            f"Platform thumbnail fetched: {len(image_bytes)} bytes, "
                            f"type={mime_type}"
                        )
            except httpx.HTTPError as e:
                logger.warning(
                    f"Platform thumbnail download failed ({e}), "
                    "falling back to ffmpeg extraction"
                )

        # --- Strategy 2: Extract frame from video stream via ffmpeg ---
        if not image_bytes and metadata.video_stream_url:
            # Seek a few seconds in to skip black intro frames,
            # but clamp to video duration if known
            duration = metadata.duration or 60
            seek = min(3, max(0, duration - 1))

            logger.info(
                f"Generating thumbnail via ffmpeg (seek={seek}s) from "
                f"lowest-quality stream..."
            )
            try:
                image_bytes = await _generate_thumbnail_from_stream(
                    video_url=metadata.video_stream_url,
                    headers=metadata.video_stream_headers,
                    seek_seconds=seek,
                )
                mime_type, ext = "image/jpeg", "jpg"
                source = "ffmpeg"
                logger.info(
                    f"ffmpeg thumbnail generated: {len(image_bytes)} bytes"
                )
            except RuntimeError as e:
                # If seeking failed (e.g., video shorter than seek), retry at 0
                if seek > 0:
                    logger.warning(
                        f"ffmpeg extraction at {seek}s failed ({e}), "
                        "retrying at 0s..."
                    )
                    try:
                        image_bytes = await _generate_thumbnail_from_stream(
                            video_url=metadata.video_stream_url,
                            headers=metadata.video_stream_headers,
                            seek_seconds=0,
                        )
                        mime_type, ext = "image/jpeg", "jpg"
                        source = "ffmpeg"
                        logger.info(
                            f"ffmpeg thumbnail generated (0s): "
                            f"{len(image_bytes)} bytes"
                        )
                    except RuntimeError as retry_err:
                        logger.error(
                            f"ffmpeg thumbnail retry also failed: {retry_err}"
                        )
                else:
                    logger.error(f"ffmpeg thumbnail extraction failed: {e}")

        # --- No thumbnail obtained ---
        if not image_bytes:
            raise HTTPException(
                status_code=404,
                detail="No thumbnail could be obtained for this video",
            )

        # Generate unique random filename
        unique_name = f"{uuid.uuid4().hex}.{ext}"

        # URL-encode the video title for safe header transport
        safe_title = quote(metadata.safe_filename, safe="")

        logger.info(
            f"Returning thumbnail: {len(image_bytes)} bytes, source={source}, "
            f"type={mime_type}, filename={unique_name}"
        )

        return Response(
            content=image_bytes,
            media_type=mime_type,
            headers={
                "Content-Disposition": f'inline; filename="{unique_name}"',
                "X-Thumbnail-Source": source,
                "X-Video-Title": safe_title[:500],
                "Cache-Control": "public, max-age=86400",
            },
        )
    finally:
        # Cleanup session (no-op if no download directory was created)
        session.cleanup()


@app.post("/download/thumbnail")
async def download_thumbnail_post(request: ThumbnailRequest) -> Response:
    """
    Extract and return the video thumbnail (POST version).

    Accepts JSON body with URL and optional timeout.
    Shares implementation with GET /download/thumbnail.

    Example request body:
    ```json
    {
        "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    }
    ```

    Returns:
        Response with binary image content and appropriate headers.
    """
    source_url = _decode_url_if_needed(request.url)
    return await download_thumbnail(
        source_url=source_url,
        metadata_timeout=request.metadata_timeout,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level="info",
    )
