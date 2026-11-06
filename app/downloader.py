"""yt-dlp download session with per-request temp-folder isolation."""

import asyncio
import contextlib
import logging
import multiprocessing
import os
import re
import shutil
import signal
import sys
import threading
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import yt_dlp
from yt_dlp.globals import plugin_dirs

from app.config import settings

logger = logging.getLogger(__name__)

# Our own extractors, in yt-dlp's plugin layout (ytdlp_plugins/<pkg>/
# yt_dlp_plugins/extractor/*.py); plugins are tried before built-in extractors.
# Set before any YoutubeDL is created: every job process imports this module
# first, and yt-dlp loads plugins once per process on its first YoutubeDL.
plugin_dirs.value = [str(Path(__file__).parent / "ytdlp_plugins")]

# Every yt-dlp job runs in its own child process (see DownloadSession._run_child)
# so a timeout can really stop it: Python threads cannot be killed.  A thread
# from this pool waits on each child.  It is separate from asyncio's default
# executor, whose size depends on the host CPU count (Docker CPU limits do not
# change it) and which aiofiles needs for every streamed chunk.  Threads are
# created lazily, up to the limit.
ytdlp_executor = ThreadPoolExecutor(
    max_workers=settings.ytdlp_executor_workers,
    thread_name_prefix="yt-dlp",
)

# forkserver: never fork the multi-threaded server process itself; the fork
# server imports yt-dlp once so each job starts fast.
_mp = multiprocessing.get_context("forkserver")
_mp.set_forkserver_preload(["yt_dlp"])

# ---------------------------------------------------------------------------
# VK cookie routing
# ---------------------------------------------------------------------------

VK_DOMAINS = ("vk.com", "vkvideo.ru", "vk.ru", "vk-video.ru")


def _is_vk_url(url: str) -> bool:
    return any(d in url.lower() for d in VK_DOMAINS)


def _get_cookies_source_file(url: str | None = None) -> tuple[Path | None, str]:
    """Select the right cookies file (VK-specific vs general) for *url*."""
    if url and _is_vk_url(url) and settings.cookies_file_vk:
        return settings.cookies_file_vk, "VK"
    return settings.cookies_file, "general"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class DownloadError(Exception):
    pass


class DownloadTimeoutError(DownloadError):
    pass


class MetadataExtractionError(DownloadError):
    pass


class FileSizeExceededError(DownloadError):
    pass


class DownloadStoppedError(DownloadError):
    pass


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

_SKIP_EXTS = frozenset({".part", ".ytdl", ".tmp"})
_DISK_STEM = "output"


class _DownloadLogger:
    """yt-dlp logger that keeps the quiet/no_warnings output unchanged.

    yt-dlp's HTTP downloader skips a file larger than ``max_filesize`` without
    raising: it only prints "File is larger than max-filesize" via
    ``to_screen`` and the download still returns success.  With a logger set,
    that message arrives in ``debug()``, which is the only way to notice it.
    """

    _SIZE_SKIP_MARKER = "larger than max-filesize"

    def __init__(self) -> None:
        self.size_limit_skipped = False

    def debug(self, msg: str) -> None:
        # Screen/progress output: hidden by ``quiet`` before, still hidden.
        if self._SIZE_SKIP_MARKER in msg:
            self.size_limit_skipped = True

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        # Hidden by ``no_warnings`` before, still hidden.
        pass

    def error(self, msg: str) -> None:
        # yt-dlp wrote errors to stderr without a logger; keep doing so.
        sys.stderr.write(f"{msg}\n")


@dataclass
class VideoMetadata:
    """Video metadata extracted from the source URL."""

    title: str
    uploader: str | None
    duration: int | None
    description: str | None
    thumbnail: str | None
    webpage_url: str
    extractor: str
    video_id: str
    filesize: int | None
    filesize_approx: int | None
    video_stream_url: str | None = None
    video_stream_headers: dict[str, str] | None = field(default=None, repr=False)

    @property
    def estimated_size(self) -> int | None:
        return self.filesize or self.filesize_approx

    def exceeds_size_limit(self, max_bytes: int) -> bool:
        est = self.estimated_size
        return est is not None and est > max_bytes

    @property
    def safe_filename(self) -> str:
        name = unicodedata.normalize("NFC", (self.title or "").strip())
        name = re.sub(r'[<>:"/\\|?*]', "_", name)
        name = re.sub(r"[\x00-\x1f\x7f]", "", name)
        name = name.strip(". ")
        name = re.sub(r"[\s_]+", " ", name).strip()
        while len(name.encode("utf-8")) > 200 and len(name) > 1:
            name = name[:-1].strip()
        if not name or not re.sub(r"[-_.\s]", "", name):
            name = f"video_{self.video_id or 'download'}"
        return name

    def compose_filename(self, ext: str) -> str:
        return f"{self.safe_filename}.{ext}"


# ---------------------------------------------------------------------------
# Download session
# ---------------------------------------------------------------------------


class DownloadSession:
    """
    One-request-one-folder download session.

    Every call — even for the same URL — gets its own UUID-named temp
    directory.  The folder is deleted only when ``cleanup()`` is called
    (i.e. after the client has fully consumed the streamed file).
    """

    def __init__(
        self,
        source_url: str,
        quality: str = settings.default_quality,
        concurrent_fragments: int = settings.default_concurrent_fragments,
        noplaylist: bool = True,
        max_filesize: int | None = None,
        merge_output_format: str | None = None,
    ):
        self.source_url = source_url
        self.quality = quality
        self.concurrent_fragments = concurrent_fragments
        self.noplaylist = noplaylist
        self.max_filesize = max_filesize or settings.max_file_size_bytes
        self.merge_output_format = merge_output_format

        self.session_id = uuid.uuid4().hex
        self.session_dir = settings.temp_download_dir / self.session_id
        self.metadata: VideoMetadata | None = None
        self._filename: str | None = None
        self._cookies_path: Path | None = None
        self._cookies_mtime: float | None = None
        self._reported_output_path: Path | None = None
        # Child process running the current yt-dlp job (see _run_child).
        self._proc_lock = threading.Lock()
        self._proc: multiprocessing.process.BaseProcess | None = None
        self._stopped = False

    def __getstate__(self) -> dict:
        # The session is pickled into the child; process handles stay here.
        state = self.__dict__.copy()
        for key in ("_proc_lock", "_proc"):
            state.pop(key, None)
        return state

    # ------------------------------------------------------- child process

    def kill(self) -> None:
        """Stop the running yt-dlp job and its subprocesses (e.g. ffmpeg).

        Safe to call from any thread, repeatedly, or before a job started
        (the next job then refuses to start).  Wait for the job's future to
        finish before deleting the session dir.
        """
        with self._proc_lock:
            self._stopped = True
            proc = self._proc
        if proc is None or proc.pid is None:
            return
        try:
            # The child leads its own process group (os.setsid in _child_main).
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            # Not yet a group leader, or already gone.
            with contextlib.suppress(Exception):
                proc.kill()
        logger.info("Stopped yt-dlp job (pid %d)", proc.pid)

    def _run_child(self, kind: str, opts: dict):
        """Run one yt-dlp job in a child process; blocks until it ends."""
        recv, send = _mp.Pipe(duplex=False)
        proc = _mp.Process(
            target=_child_main, args=(send, self, kind, opts), daemon=True,
        )
        with self._proc_lock:
            if self._stopped:
                recv.close()
                send.close()
                raise DownloadStoppedError("yt-dlp job stopped")
            proc.start()
            self._proc = proc
        send.close()
        try:
            while True:
                try:
                    msg = recv.recv()
                except EOFError:
                    break
                if msg[0] == "path":
                    self._reported_output_path = Path(msg[1])
                elif msg[0] == "ok":
                    return msg[1]
                elif msg[0] == "error":
                    raise msg[1]
            if self._stopped:
                raise DownloadStoppedError("yt-dlp job stopped")
            raise DownloadError(f"yt-dlp process exited (code {proc.exitcode})")
        finally:
            recv.close()
            proc.join()
            with self._proc_lock:
                self._proc = None
            proc.close()

    async def _stop_and_wait(self, fut: asyncio.Future) -> None:
        """Kill the job behind *fut* and wait until it has really ended."""
        self.kill()
        await asyncio.wait({fut})
        if not fut.cancelled():
            fut.exception()  # mark retrieved; the job was stopped on purpose

    # -------------------------------------------------------------- cookies

    def _writable_cookies(self) -> Path | None:
        """Return a per-session writable copy of the correct cookies file."""
        source, label = _get_cookies_source_file(self.source_url)
        if not source:
            return None
        if not source.exists():
            if (
                label == "VK"
                and settings.cookies_file
                and settings.cookies_file.exists()
            ):
                logger.info("VK cookies missing — falling back to general cookies")
                source = settings.cookies_file
                label = "general"
            else:
                logger.warning("%s cookies not found: %s", label, source)
                return None

        mtime = source.stat().st_mtime
        if (
            self._cookies_path
            and self._cookies_path.exists()
            and self._cookies_mtime is not None
            and mtime <= self._cookies_mtime
        ):
            return self._cookies_path

        self.session_dir.mkdir(parents=True, exist_ok=True)
        dst = self.session_dir / f"cookies-{label.lower()}.txt"
        try:
            shutil.copy2(source, dst)
            self._cookies_path = dst
            self._cookies_mtime = mtime
            logger.info(
                "Copied %s cookies (%d B) → %s",
                label,
                source.stat().st_size,
                dst,
            )
            return dst
        except Exception as exc:
            logger.error("Failed to copy %s cookies: %s", label, exc)
            return None

    # --------------------------------------------------------- yt-dlp opts

    def _common_opts(self) -> dict:
        opts: dict = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": self.noplaylist,
            "extract_flat": False,
            "remote_components": ["ejs:github"],
        }
        cookies = self._writable_cookies()
        if cookies:
            opts["cookiefile"] = str(cookies)
        if settings.proxy:
            opts["proxy"] = settings.proxy
        return opts

    def _extract_opts(self) -> dict:
        opts = self._common_opts()
        opts |= {
            "skip_download": True,
            "format": None,
            "ignore_no_formats_error": True,
            "allow_unplayable_formats": False,
        }
        return opts

    @staticmethod
    def _format_selector(quality: str) -> str:
        """Permissive format selector — no container/codec restrictions.

        Uses ``bv*`` (best video, may include audio) + ``ba`` (best audio)
        so yt-dlp can always find a valid combination even when separate
        video-only / audio-only streams are not available.
        """
        table = {
            "best": "bv*+ba/b",
            "high": (
                "bv*[height<=1080]+ba/"
                "b[height<=1080]/"
                "bv*+ba/b"
            ),
            "medium": (
                "bv*[height<=720]+ba/"
                "b[height<=720]/"
                "bv*+ba/b"
            ),
            "low": (
                "bv*[height<=480]+ba/"
                "b[height<=480]/"
                "bv*+ba/b"
            ),
            "audio": "ba/b",
        }
        return table.get(quality, table["best"])

    def _download_opts(self) -> dict:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        opts = self._common_opts()
        opts |= {
            "format": self._format_selector(self.quality),
            "outtmpl": str(self.session_dir / f"{_DISK_STEM}.%(ext)s"),
            "concurrent_fragment_downloads": self.concurrent_fragments,
            "nopart": True,
            "writeinfojson": False,
            "writethumbnail": False,
            "progress_hooks": [self._progress_hook],
            "postprocessor_hooks": [self._postprocessor_hook],
        }
        if self.max_filesize:
            opts["max_filesize"] = self.max_filesize
        if self.merge_output_format:
            opts["merge_output_format"] = self.merge_output_format
        return opts

    def _session_path(self, candidate: str) -> Path | None:
        """Resolve a path reported by yt-dlp relative to the session dir."""
        try:
            path = Path(candidate)
        except Exception:
            return None
        return path if path.is_absolute() else self.session_dir / path

    def _progress_hook(self, data: dict) -> None:
        """Capture output path from yt-dlp progress events.

        Only accepts paths whose stem matches ``_DISK_STEM`` exactly so that
        intermediate per-format files (e.g. ``output.f137.mp4``) produced
        during bestvideo+bestaudio downloads are ignored.
        """
        status = data.get("status")
        if status not in {"downloading", "finished"}:
            return
        info = data.get("info_dict") or {}
        candidate = info.get("filepath") or data.get("filename")
        if not candidate:
            return
        path = self._session_path(candidate)
        if path and path.stem == _DISK_STEM:
            self._reported_output_path = path

    def _postprocessor_hook(self, data: dict) -> None:
        """Capture final output path after postprocessors (e.g. ffmpeg merge)."""
        if data.get("status") != "finished":
            return
        info = data.get("info_dict") or {}
        filepath = info.get("filepath")
        if not filepath:
            return
        path = self._session_path(filepath)
        if path:
            self._reported_output_path = path

    # ------------------------------------------------------------- metadata

    async def extract_metadata(self, timeout: int | None = None) -> VideoMetadata:
        opts = self._extract_opts()
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(ytdlp_executor, self._run_child, "extract", opts)
        try:
            # asyncio.wait never cancels fut: on timeout the job is killed and
            # awaited, not abandoned.
            done, _ = await asyncio.wait({fut}, timeout=timeout)
            if not done:
                await self._stop_and_wait(fut)
                raise MetadataExtractionError(
                    f"Metadata extraction timed out ({timeout}s)"
                )
            info = fut.result()
        except asyncio.CancelledError:
            # Request cancelled (e.g. client gone): don't leave the job running.
            await self._stop_and_wait(fut)
            raise
        except MetadataExtractionError:
            raise
        except Exception as exc:
            raise MetadataExtractionError(str(exc)) from exc

        if not info:
            raise MetadataExtractionError("No metadata returned")

        filesize = info.get("filesize")
        filesize_approx = info.get("filesize_approx")
        if not filesize and not filesize_approx:
            total = sum(
                f.get("filesize") or f.get("filesize_approx") or 0
                for f in info.get("requested_formats", [])
            )
            if total:
                filesize_approx = total

        vs_url, vs_hdrs = self._pick_thumbnail_stream(info)

        ext = info.get("ext") or "mp4"
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
            video_stream_url=vs_url,
            video_stream_headers=vs_hdrs,
        )
        self._filename = self.metadata.compose_filename(ext)
        return self.metadata

    @staticmethod
    def _pick_thumbnail_stream(info: dict) -> tuple[str | None, dict | None]:
        """Pick the lowest-quality video stream URL for thumbnail extraction."""
        vfmts = [
            f
            for f in (info.get("formats") or [])
            if f.get("vcodec") and f["vcodec"] != "none" and f.get("url")
        ]
        if vfmts:
            vfmts.sort(
                key=lambda f: (
                    0 if f.get("protocol", "") in ("https", "http", "") else 1,
                    f.get("height") or 9999,
                ),
            )
            return vfmts[0].get("url"), vfmts[0].get("http_headers")
        if info.get("url"):
            return info["url"], info.get("http_headers")
        return None, None

    def _do_extract(self, opts: dict) -> dict:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(self.source_url, download=False)

    # -------------------------------------------------------------- download

    def start_download(self) -> asyncio.Future:
        """Kick off the download in a child process (see ``kill()``)."""
        opts = self._download_opts()
        return asyncio.get_running_loop().run_in_executor(
            ytdlp_executor, self._run_child, "download", opts
        )

    def _do_download(self, opts: dict) -> None:
        dl_logger = _DownloadLogger()
        with yt_dlp.YoutubeDL(opts | {"logger": dl_logger}) as ydl:
            rc = ydl.download([self.source_url])
            if rc:
                raise DownloadError(f"yt-dlp error code {rc}")
        if dl_logger.size_limit_skipped:
            raise FileSizeExceededError(
                f"File exceeds {self.max_filesize / 1_048_576:.1f}MB limit"
            )

    # -------------------------------------------------------- output helpers

    def expected_output(self) -> Path:
        """Best-guess output path (actual extension may differ)."""
        ext = "mp4"
        if self._filename:
            ext = self._filename.rsplit(".", 1)[-1]
        return self.session_dir / f"{_DISK_STEM}.{ext}"

    def resolve_output(self) -> Path | None:
        """Find the actual output file produced by yt-dlp (cheap checks only).

        Filters out intermediate per-format files (stem != ``_DISK_STEM``) so
        that bestvideo+bestaudio downloads don't prematurely stream the
        video-only fragment before the ffmpeg merge completes.
        """
        if not self.session_dir.exists():
            return None
        if self._reported_output_path and self._reported_output_path.exists():
            return self._reported_output_path
        matches = [
            p
            for p in self.session_dir.glob(f"{_DISK_STEM}.*")
            if p.is_file()
            and p.suffix.lower() not in _SKIP_EXTS
            and p.stem == _DISK_STEM
        ]
        if matches:
            return max(matches, key=lambda p: p.stat().st_size)
        return None

    def resolve_output_recursive(self) -> Path | None:
        """Slow fallback used only once after download has completed."""
        if not self.session_dir.exists():
            return None
        direct = self.resolve_output()
        if direct:
            return direct
        candidates = [
            p
            for p in self.session_dir.rglob("*")
            if p.is_file() and p.suffix.lower() not in _SKIP_EXTS
        ]
        return max(candidates, key=lambda p: p.stat().st_size) if candidates else None

    def get_filename(self) -> str:
        """User-facing filename (Unicode-safe, for HTTP headers only)."""
        return self._filename or "download.mp4"

    # --------------------------------------------------------------- cleanup

    def cleanup(self) -> None:
        """Remove the session temp folder and all its contents."""
        if self.session_dir.exists():
            shutil.rmtree(self.session_dir, ignore_errors=True)
            logger.debug("Removed session dir %s", self.session_dir)


# ---------------------------------------------------------------------------
# Child process entry point
# ---------------------------------------------------------------------------


def _child_main(conn, session: DownloadSession, kind: str, opts: dict) -> None:
    """Run one yt-dlp job and report back over *conn*.

    Messages: ``("path", str)`` whenever a hook reports a new output path,
    then ``("ok", result)`` or ``("error", DownloadError)``.
    """
    # Own process group, so kill() also stops ffmpeg & co. started by yt-dlp.
    os.setsid()
    try:
        if kind == "extract":
            result = yt_dlp.YoutubeDL.sanitize_info(session._do_extract(opts))
        else:
            sent: Path | None = None

            def report_path(_data: dict) -> None:
                nonlocal sent
                path = session._reported_output_path
                if path is not None and path != sent:
                    sent = path
                    conn.send(("path", str(path)))

            # Hooks must update the child's session copy, then forward it.
            opts = opts | {
                "progress_hooks": [session._progress_hook, report_path],
                "postprocessor_hooks": [session._postprocessor_hook, report_path],
            }
            session._do_download(opts)
            result = None
        conn.send(("ok", result))
    except DownloadError as exc:
        conn.send(("error", exc))
    except BaseException as exc:
        # yt-dlp's own exceptions may hold tracebacks that cannot be pickled.
        conn.send(("error", DownloadError(str(exc))))
    finally:
        conn.close()
