"""
yt-dlp FastAPI Streaming Service

High-performance video download service:
- Concurrent fragment downloads via yt-dlp
- Per-request temp-folder isolation
- Stream-while-downloading with automatic cleanup
- No ffmpeg re-encoding — streams whatever yt-dlp produces
"""

import asyncio
import logging
import time
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


# ===================================================================
# Request models
# ===================================================================


class DownloadRequest(BaseModel):
    """Request body for POST /download."""

    url: str = Field(
        ...,
        description="Video URL (YouTube, Vimeo, VK, etc.)",
        examples=["https://www.youtube.com/watch?v=dQw4w9WgXcQ"],
    )
    quality: str = Field(
        default="best",
        description="Video quality: best, high (1080p), medium (720p), low (480p), audio",
    )
    noplaylist: bool = Field(default=True)
    concurrent_fragment_downloads: int = Field(
        default=settings.default_concurrent_fragments, ge=1, le=16,
    )
    chunk_size: int = Field(
        default=settings.stream_chunk_size, ge=1024, le=10_485_760,
    )
    metadata_timeout: int = Field(
        default=settings.metadata_timeout_seconds, ge=10, le=300,
    )
    download_timeout: int = Field(
        default=settings.download_timeout_seconds, ge=30, le=7200,
    )
    max_file_size: int = Field(
        default=settings.max_file_size_bytes, ge=1_048_576, le=10_737_418_240,
    )


class DownloadForWebRequest(BaseModel):
    """Request body for POST /download-for-web."""

    url: str = Field(
        ...,
        description="Video URL (YouTube, Vimeo, VK, etc.)",
        examples=["https://www.youtube.com/watch?v=dQw4w9WgXcQ"],
    )
    quality: str = Field(
        default="best",
        description="Video quality: best, high (1080p), medium (720p), low (480p), audio",
    )
    noplaylist: bool = Field(default=True)
    concurrent_fragment_downloads: int = Field(
        default=settings.default_concurrent_fragments, ge=1, le=16,
    )
    chunk_size: int = Field(
        default=settings.stream_chunk_size, ge=1024, le=10_485_760,
    )
    metadata_timeout: int = Field(
        default=settings.metadata_timeout_seconds, ge=10, le=300,
    )
    download_timeout: int = Field(
        default=settings.download_timeout_seconds, ge=30, le=7200,
    )
    max_file_size: int = Field(
        default=settings.max_file_size_bytes, ge=1_048_576, le=10_737_418_240,
    )


class ThumbnailRequest(BaseModel):
    """Request body for POST /download/thumbnail."""

    url: str = Field(
        ...,
        description="Video URL (YouTube, Vimeo, VK, etc.)",
        examples=["https://www.youtube.com/watch?v=dQw4w9WgXcQ"],
    )
    metadata_timeout: int = Field(
        default=settings.metadata_timeout_seconds, ge=10, le=300,
    )


# ===================================================================
# Concurrency & lifespan
# ===================================================================

_semaphore: asyncio.Semaphore | None = None
_active_downloads: int = 0


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _semaphore
    _semaphore = asyncio.Semaphore(settings.max_concurrent_downloads)
    settings.temp_download_dir.mkdir(parents=True, exist_ok=True)

    for label, path in [
        ("General", settings.cookies_file),
        ("VK", settings.cookies_file_vk),
    ]:
        if path:
            if path.exists():
                logger.info(
                    "%s cookies: %s (%d bytes)", label, path, path.stat().st_size,
                )
            else:
                logger.warning(
                    "%s cookies configured but missing: %s", label, path,
                )
        else:
            logger.info("No %s cookies configured", label)

    logger.info(
        "Service started — temp=%s, max_concurrent=%d",
        settings.temp_download_dir,
        settings.max_concurrent_downloads,
    )
    yield
    logger.info("Service shutting down")


app = FastAPI(
    title="yt-dlp Streaming Service",
    description="Download and stream videos via yt-dlp",
    version="2.0.0",
    lifespan=lifespan,
)

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


# ===================================================================
# Helpers
# ===================================================================

_MIME: dict[str, str] = {
    "mp4": "video/mp4",
    "mkv": "video/x-matroska",
    "webm": "video/webm",
    "m4a": "audio/mp4",
    "mp3": "audio/mpeg",
    "ogg": "audio/ogg",
    "opus": "audio/opus",
}


def _media_type(ext: str) -> str:
    return _MIME.get(ext.lower(), "application/octet-stream")


def _decode_url(url: str) -> str:
    """Decode a URL that may have been fully percent-encoded (e.g. by a frontend).

    Only decodes when the entire URL appears to be encoded — i.e. it doesn't
    start with a scheme.  Already-valid URLs are returned as-is to avoid
    corrupting percent-encoded characters in their query strings.
    """
    if not url:
        return url
    if not url.startswith(("http://", "https://")):
        decoded = unquote(url)
        if decoded.startswith(("http://", "https://")):
            return decoded
    return url


def _replace_ext(filename: str, ext: str) -> str:
    ext = ext.lstrip(".").strip().lower()
    if not ext:
        return filename
    dot = filename.rfind(".")
    return f"{filename[:dot]}.{ext}" if dot > 0 else f"{filename}.{ext}"


# ===================================================================
# Streaming core
# ===================================================================


async def _wait_for_output(
    expected: Path,
    task: asyncio.Task,
    session: DownloadSession,
    poll: float = 0.25,
) -> Path:
    """Poll until yt-dlp produces a streamable output file."""
    deadline = time.monotonic() + settings.file_wait_timeout_seconds
    last_log = 0.0
    current_poll = poll

    while True:
        if expected.exists():
            return expected

        resolved = session.resolve_output()
        if resolved:
            if resolved != expected:
                logger.info(
                    "Resolved output %s (expected %s)",
                    resolved.name,
                    expected.name,
                )
            return resolved

        if task.done():
            exc = task.exception()
            if exc:
                raise exc
            final = session.resolve_output_recursive()
            if final:
                return final
            raise DownloadError("Download finished but no output file found")

        elapsed = settings.file_wait_timeout_seconds - (deadline - time.monotonic())
        if elapsed - last_log >= 30:
            logger.info("Waiting for output file… (%.0fs)", elapsed)
            last_log = elapsed
        if time.monotonic() >= deadline:
            raise DownloadError(
                f"Timeout waiting for output file ({settings.file_wait_timeout_seconds}s)"
            )
        await asyncio.sleep(current_poll)
        current_poll = min(2.0, current_poll * 1.5)


async def _stream_and_cleanup(
    path: Path,
    chunk_size: int,
    task: asyncio.Task,
    download_future: asyncio.Future,
    cleanup: Callable[[], None],
    max_size: int | None = None,
    poll: float = 0.25,
) -> AsyncIterator[bytes]:
    """Stream file bytes while the download continues, then clean up."""
    streamed = 0
    try:
        async with aiofiles.open(path, "rb") as f:
            while True:
                chunk = await f.read(chunk_size)
                if chunk:
                    streamed += len(chunk)
                    if max_size and streamed > max_size:
                        task.cancel()
                        raise FileSizeExceededError(
                            f"Streamed {streamed / 1_048_576:.1f}MB — exceeds "
                            f"{max_size / 1_048_576:.1f}MB limit"
                        )
                    yield chunk
                    continue

                if task.done():
                    exc = task.exception()
                    if exc:
                        logger.error(
                            "Download failed after streaming %.1fMB: %s",
                            streamed / 1_048_576,
                            exc,
                        )
                        raise DownloadError(
                            f"Download failed after streaming "
                            f"{streamed / 1_048_576:.1f}MB"
                        ) from exc
                    break

                await asyncio.sleep(poll)

        logger.info("Stream completed (%.1fMB)", streamed / 1_048_576)

    finally:
        deferred = False
        if not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=900)
            except asyncio.TimeoutError:
                logger.warning("Download didn't finish within 15 min of stream end")
                asyncio.create_task(
                    _deferred_cleanup(download_future, cleanup, reason="stream-timeout")
                )
                deferred = True
            except asyncio.CancelledError:
                asyncio.create_task(
                    _deferred_cleanup(download_future, cleanup, reason="stream-cancelled")
                )
                deferred = True
            except Exception:
                pass
        if not deferred:
            if not download_future.done():
                asyncio.create_task(
                    _deferred_cleanup(download_future, cleanup, reason="stream-finished")
                )
            else:
                try:
                    cleanup()
                    logger.info("Session cleaned up after streaming")
                except Exception as exc:
                    logger.error("Cleanup error: %s", exc)


async def _deferred_cleanup(
    download_future: asyncio.Future,
    cleanup: Callable[[], None],
    reason: str = "unknown",
) -> None:
    """Background cleanup that waits for executor download to actually finish."""
    try:
        if not download_future.done():
            logger.info("Deferred cleanup waiting for download completion (%s)", reason)
            await asyncio.shield(download_future)
    except asyncio.CancelledError:
        # Keep cleanup task resilient to ambient cancellation.
        logger.warning("Deferred cleanup task cancelled (%s)", reason)
        return
    except Exception as exc:
        logger.debug("Download finished with error before deferred cleanup: %s", exc)
    try:
        cleanup()
        logger.info("Deferred cleanup done")
    except Exception as exc:
        logger.error("Deferred cleanup error: %s", exc)


async def _monitor_download(
    future: asyncio.Future,
    timeout: int,
) -> None:
    """Await download with timeout enforcement."""
    try:
        await asyncio.wait_for(future, timeout=timeout)
    except asyncio.TimeoutError:
        # Do NOT cleanup here. run_in_executor work may still be writing;
        # cleanup must happen only after the executor future truly finishes.
        raise DownloadTimeoutError(f"Download exceeded {timeout}s timeout")
    except asyncio.CancelledError:
        raise
    except Exception:
        raise


# ===================================================================
# Core download handler (shared by /download and /download-for-web)
# ===================================================================


async def _handle_download(
    source_url: str,
    quality: str,
    noplaylist: bool,
    concurrent_fragments: int,
    chunk_size: int,
    metadata_timeout: int,
    download_timeout: int,
    max_file_size: int,
    merge_output_format: str | None = None,
) -> StreamingResponse:
    global _active_downloads

    if _semaphore is None:
        raise HTTPException(status_code=503, detail="Service not ready")

    try:
        await asyncio.wait_for(
            _semaphore.acquire(),
            timeout=settings.download_slot_acquire_timeout_seconds,
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=503,
            detail=(
                f"Server at capacity ({settings.max_concurrent_downloads} "
                "concurrent downloads). Try again later."
            ),
        )

    _active_downloads += 1
    logger.info(
        "Active downloads: %d/%d",
        _active_downloads,
        settings.max_concurrent_downloads,
    )

    session = DownloadSession(
        source_url=source_url,
        quality=quality,
        concurrent_fragments=concurrent_fragments,
        noplaylist=noplaylist,
        max_filesize=max_file_size,
        merge_output_format=merge_output_format,
    )

    cleanup_done = False
    download_future: asyncio.Future | None = None

    def release_and_cleanup() -> None:
        global _active_downloads
        nonlocal cleanup_done
        if cleanup_done:
            return
        cleanup_done = True
        try:
            session.cleanup()
        finally:
            _semaphore.release()
            _active_downloads = max(0, _active_downloads - 1)
            logger.info(
                "Active downloads: %d/%d",
                _active_downloads,
                settings.max_concurrent_downloads,
            )

    try:
        # ---- metadata ----
        try:
            meta = await session.extract_metadata(timeout=metadata_timeout)
        except MetadataExtractionError as exc:
            raise HTTPException(
                status_code=400, detail=f"Failed to extract video info: {exc}",
            )

        size_info = ""
        if meta.estimated_size:
            size_info = f", est={meta.estimated_size / 1_048_576:.1f}MB"
        logger.info(
            "Metadata: title='%s', uploader='%s', duration=%ss%s",
            meta.title,
            meta.uploader,
            meta.duration,
            size_info,
        )

        if meta.exceeds_size_limit(max_file_size):
            raise FileSizeExceededError(
                f"Estimated size ({meta.estimated_size / 1_048_576:.1f}MB) "
                f"exceeds {max_file_size / 1_048_576:.1f}MB limit"
            )

        # ---- start background download ----
        dl_future = session.start_download()
        download_future = dl_future
        dl_task = asyncio.create_task(
            _monitor_download(dl_future, download_timeout),
        )

        # ---- wait for file to appear ----
        expected = session.expected_output()
        stream_path = await _wait_for_output(expected, dl_task, session)

        # ---- build response ----
        actual_ext = stream_path.suffix.lstrip(".").lower() or "mp4"
        filename = _replace_ext(session.get_filename(), actual_ext)
        ascii_name = (
            filename.encode("ascii", "replace").decode("ascii").replace('"', "'")
        )
        disposition = (
            f'attachment; filename="{ascii_name}"; '
            f"filename*=UTF-8''{quote(filename, safe='')}"
        )
        mime = _media_type(actual_ext)

        return StreamingResponse(
            content=_stream_and_cleanup(
                stream_path,
                chunk_size,
                dl_task,
                dl_future,
                release_and_cleanup,
                max_file_size,
            ),
            media_type=mime,
            headers={
                "Content-Disposition": disposition,
                "X-Filename": ascii_name,
                "X-Filename-Encoded": quote(filename, safe=""),
                "X-Content-Type": mime,
            },
        )

    except DownloadTimeoutError as exc:
        if settings.cleanup_on_error and download_future is not None:
            asyncio.create_task(
                _deferred_cleanup(download_future, release_and_cleanup, reason="request-timeout")
            )
        raise HTTPException(status_code=408, detail=str(exc))
    except FileSizeExceededError as exc:
        release_and_cleanup()
        raise HTTPException(status_code=413, detail=str(exc))
    except HTTPException:
        if settings.cleanup_on_error:
            if download_future is not None and not download_future.done():
                asyncio.create_task(
                    _deferred_cleanup(
                        download_future, release_and_cleanup, reason="http-exception"
                    )
                )
            else:
                release_and_cleanup()
        raise
    except Exception as exc:
        logger.exception("Unexpected error: %s", exc)
        if settings.cleanup_on_error:
            if download_future is not None and not download_future.done():
                asyncio.create_task(
                    _deferred_cleanup(
                        download_future, release_and_cleanup, reason="unexpected-error"
                    )
                )
            else:
                release_and_cleanup()
        raise HTTPException(
            status_code=500, detail=f"Internal server error: {exc}",
        )


# ===================================================================
# /health
# ===================================================================


@app.get("/health")
async def health_check() -> dict:
    return {
        "status": "healthy",
        "service": "yt-dlp-streamer",
        "active_downloads": _active_downloads,
        "max_concurrent_downloads": settings.max_concurrent_downloads,
    }


# ===================================================================
# /debug/cookies
# ===================================================================


@app.get("/debug/cookies")
async def debug_cookies() -> dict:
    result: dict = {
        "cookies_file_configured": (
            str(settings.cookies_file) if settings.cookies_file else None
        ),
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
        with open(settings.cookies_file, encoding="utf-8") as f:
            domains: set[str] = set()
            vk_cookies: list[dict] = []
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    parts = line.split("\t")
                    if len(parts) >= 7:
                        domain, cookie_name = parts[0], parts[5]
                        domains.add(domain)
                        if "vk" in domain.lower():
                            vk_cookies.append(
                                {"domain": domain, "name": cookie_name, "path": parts[2]}
                            )
            result["total_domains"] = len(domains)
            result["vk_cookies_count"] = len(vk_cookies)
            result["sample_vk_cookies"] = vk_cookies[:20]
            result["video_platform_domains"] = {
                p: sorted(d for d in domains if p in d.lower())
                for p in ("youtube", "vk", "vimeo", "rutube")
            }
    except Exception as exc:
        result["error"] = str(exc)

    return result


# ===================================================================
# /download  (GET + POST)
# ===================================================================


@app.get("/download")
async def download_video(
    source_url: Annotated[
        str, Query(description="Video URL (YouTube, Vimeo, VK, etc.)"),
    ],
    quality: Annotated[
        str, Query(description="best / high / medium / low / audio"),
    ] = "best",
    noplaylist: Annotated[bool, Query()] = True,
    concurrent_fragment_downloads: Annotated[
        int, Query(ge=1, le=16),
    ] = settings.default_concurrent_fragments,
    chunk_size: Annotated[
        int, Query(ge=1024, le=10_485_760),
    ] = settings.stream_chunk_size,
    metadata_timeout: Annotated[
        int, Query(ge=10, le=300),
    ] = settings.metadata_timeout_seconds,
    download_timeout: Annotated[
        int, Query(ge=30, le=7200),
    ] = settings.download_timeout_seconds,
    max_file_size: Annotated[
        int, Query(ge=1_048_576, le=10_737_418_240),
    ] = settings.max_file_size_bytes,
) -> StreamingResponse:
    """Download and stream a video from the provided URL."""
    return await _handle_download(
        source_url,
        quality,
        noplaylist,
        concurrent_fragment_downloads,
        chunk_size,
        metadata_timeout,
        download_timeout,
        max_file_size,
    )


@app.post("/download")
async def download_video_post(request: DownloadRequest) -> StreamingResponse:
    """Download and stream a video (POST version with JSON body)."""
    return await _handle_download(
        _decode_url(request.url),
        request.quality,
        request.noplaylist,
        request.concurrent_fragment_downloads,
        request.chunk_size,
        request.metadata_timeout,
        request.download_timeout,
        request.max_file_size,
    )


# ===================================================================
# /download-for-web  (GET + POST) — same pipeline, no transcoding
# ===================================================================


@app.get("/download-for-web")
async def download_for_web(
    source_url: Annotated[
        str, Query(description="Video URL (YouTube, Vimeo, VK, etc.)"),
    ],
    quality: Annotated[
        str, Query(description="best / high / medium / low / audio"),
    ] = "best",
    noplaylist: Annotated[bool, Query()] = True,
    concurrent_fragment_downloads: Annotated[
        int, Query(ge=1, le=16),
    ] = settings.default_concurrent_fragments,
    chunk_size: Annotated[
        int, Query(ge=1024, le=10_485_760),
    ] = settings.stream_chunk_size,
    metadata_timeout: Annotated[
        int, Query(ge=10, le=300),
    ] = settings.metadata_timeout_seconds,
    download_timeout: Annotated[
        int, Query(ge=30, le=7200),
    ] = settings.download_timeout_seconds,
    max_file_size: Annotated[
        int, Query(ge=1_048_576, le=10_737_418_240),
    ] = settings.max_file_size_bytes,
) -> StreamingResponse:
    """Download and stream a video in a browser-compatible container."""
    return await _handle_download(
        source_url,
        quality,
        noplaylist,
        concurrent_fragment_downloads,
        chunk_size,
        metadata_timeout,
        download_timeout,
        max_file_size,
        merge_output_format="mp4",
    )


@app.post("/download-for-web")
async def download_for_web_post(
    request: DownloadForWebRequest,
) -> StreamingResponse:
    """Download and stream a video for web in a browser-compatible container."""
    return await _handle_download(
        _decode_url(request.url),
        request.quality,
        request.noplaylist,
        request.concurrent_fragment_downloads,
        request.chunk_size,
        request.metadata_timeout,
        request.download_timeout,
        request.max_file_size,
        merge_output_format="mp4",
    )


# ===================================================================
# /download/thumbnail  (GET + POST)
# ===================================================================

_IMG_MIME: dict[str, tuple[str, str]] = {
    "image/jpeg": ("image/jpeg", "jpg"),
    "image/jpg": ("image/jpeg", "jpg"),
    "image/png": ("image/png", "png"),
    "image/webp": ("image/webp", "webp"),
    "image/gif": ("image/gif", "gif"),
}

_IMG_EXT: dict[str, tuple[str, str]] = {
    ".jpg": ("image/jpeg", "jpg"),
    ".jpeg": ("image/jpeg", "jpg"),
    ".png": ("image/png", "png"),
    ".webp": ("image/webp", "webp"),
    ".gif": ("image/gif", "gif"),
}


def _guess_image_mime(ct: str | None, url: str) -> tuple[str, str]:
    if ct:
        key = ct.lower().split(";")[0].strip()
        if key in _IMG_MIME:
            return _IMG_MIME[key]
    path = url.lower().split("?")[0]
    for ext, val in _IMG_EXT.items():
        if path.endswith(ext):
            return val
    return ("image/jpeg", "jpg")


async def _ffmpeg_thumbnail(
    video_url: str,
    headers: dict[str, str] | None = None,
    seek: int = 3,
    timeout: float = 30.0,
) -> bytes:
    """Extract a single JPEG frame from a video stream (no full download)."""
    cmd: list[str] = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error"]
    if headers:
        cmd += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items())]
    cmd += [
        "-ss", str(seek),
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
        raise RuntimeError(f"ffmpeg thumbnail timed out ({timeout}s)")
    if proc.returncode or not stdout:
        err = stderr.decode(errors="replace")[:500] if stderr else "unknown"
        raise RuntimeError(
            f"ffmpeg thumbnail failed (rc={proc.returncode}): {err}"
        )
    return stdout


@app.get("/download/thumbnail")
async def download_thumbnail(
    source_url: Annotated[
        str, Query(description="Video URL (YouTube, Vimeo, VK, etc.)"),
    ],
    metadata_timeout: Annotated[
        int, Query(ge=10, le=300),
    ] = settings.metadata_timeout_seconds,
) -> Response:
    """
    Return the video thumbnail.

    Strategy 1: platform-native thumbnail URL (fast).
    Strategy 2: extract a single frame via ffmpeg from the lowest-quality
    video stream (only downloads enough bytes for one frame).
    """
    session = DownloadSession(source_url=source_url)
    try:
        try:
            meta = await session.extract_metadata(timeout=metadata_timeout)
        except MetadataExtractionError as exc:
            raise HTTPException(
                status_code=400, detail=f"Failed to extract video info: {exc}",
            )

        image_bytes: bytes | None = None
        mime, ext, source = "image/jpeg", "jpg", "unknown"

        # Strategy 1 — platform thumbnail
        if meta.thumbnail:
            try:
                async with httpx.AsyncClient(
                    follow_redirects=True, timeout=30.0,
                ) as client:
                    resp = await client.get(meta.thumbnail)
                    resp.raise_for_status()
                    if resp.content:
                        image_bytes = resp.content
                        mime, ext = _guess_image_mime(
                            resp.headers.get("content-type"), meta.thumbnail,
                        )
                        source = "platform"
            except httpx.HTTPError:
                pass

        # Strategy 2 — ffmpeg single-frame extraction
        if not image_bytes and meta.video_stream_url:
            dur = meta.duration or 60
            seek = min(3, max(0, dur - 1))
            try:
                image_bytes = await _ffmpeg_thumbnail(
                    meta.video_stream_url, meta.video_stream_headers, seek,
                )
                mime, ext, source = "image/jpeg", "jpg", "ffmpeg"
            except RuntimeError:
                if seek > 0:
                    try:
                        image_bytes = await _ffmpeg_thumbnail(
                            meta.video_stream_url, meta.video_stream_headers, 0,
                        )
                        mime, ext, source = "image/jpeg", "jpg", "ffmpeg"
                    except RuntimeError:
                        pass

        if not image_bytes:
            raise HTTPException(
                status_code=404, detail="No thumbnail could be obtained",
            )

        return Response(
            content=image_bytes,
            media_type=mime,
            headers={
                "Content-Disposition": f'inline; filename="{uuid.uuid4().hex}.{ext}"',
                "X-Thumbnail-Source": source,
                "X-Video-Title": quote(meta.safe_filename, safe=""),
                "Cache-Control": "public, max-age=86400",
            },
        )
    finally:
        session.cleanup()


@app.post("/download/thumbnail")
async def download_thumbnail_post(request: ThumbnailRequest) -> Response:
    """Return video thumbnail (POST version with JSON body)."""
    return await download_thumbnail(
        source_url=_decode_url(request.url),
        metadata_timeout=request.metadata_timeout,
    )


# ===================================================================
# Entry point
# ===================================================================

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level="info",
    )
