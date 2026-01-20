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
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, AsyncIterator
from urllib.parse import quote

import aiofiles
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

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
    
    # Log cookies file status
    if settings.cookies_file:
        if settings.cookies_file.exists():
            logger.info(f"Cookies file configured and found: {settings.cookies_file}")
        else:
            logger.warning(f"Cookies file configured but NOT found: {settings.cookies_file}")
    else:
        logger.info("No cookies file configured (COOKIES_FILE env var not set)")
    
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
    expose_headers=["Content-Disposition", "X-Filename", "X-Filename-Encoded"],
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
    """
    bytes_streamed = 0
    
    try:
        while not file_path.exists():
            if download_task.done():
                exc = download_task.exception()
                if exc:
                    raise exc
                raise DownloadError("Download finished but output file not found")
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
                        raise exc
                    break
                await asyncio.sleep(poll_interval)
    finally:
        try:
            cleanup_callback()
            logger.info(f"Cleaned up session after streaming ({bytes_streamed / (1024*1024):.1f}MB)")
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


@app.get("/health")
async def health_check() -> dict:
    """Health check endpoint with service status."""
    return {
        "status": "healthy",
        "service": "yt-dlp-streamer",
        "active_downloads": _active_downloads,
        "max_concurrent_downloads": settings.max_concurrent_downloads,
    }


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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level="info",
    )
