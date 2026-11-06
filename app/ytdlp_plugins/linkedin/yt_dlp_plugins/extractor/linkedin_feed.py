"""yt-dlp extractor for LinkedIn feed posts opened with login cookies.

Loaded as a yt-dlp plugin (see ``app/downloader.py``), so it is tried before
the built-in extractors.  yt-dlp's own ``LinkedInIE`` parses the logged-out
page (``<video data-sources=...>``) and does not accept group posts at all.
With cookies LinkedIn serves its app page instead, which embeds the post as a
React Server Components stream in ``window.__como_rehydration__``; the video
is the ``"playlist"`` object in that stream.
"""

import json
import re

from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.extractor.linkedin import LinkedInIE
from yt_dlp.utils import ExtractorError, int_or_none, url_or_none

_REHYDRATION_RE = r"window\.__como_rehydration__\s*=\s*(\[.*?\])\s*</script>"
# RSC rows look like ``<hex id>:<json>``; the post text row is tagged so.
_COMMENTARY_ROW_RE = r'(?m)^[0-9a-f]+:(\[.*"legacyControlName":"commentary_text".*\])$'


def _rsc_text(node) -> str:
    """Concatenate the text of an RSC element tree (``["$", type, key, props]``)."""
    if isinstance(node, str):
        # "$..." strings are references to other rows; literal "$" is "$$".
        if node.startswith("$"):
            return node[1:] if node.startswith("$$") else ""
        return node
    if not isinstance(node, list):
        return ""
    if len(node) == 4 and node[0] == "$" and isinstance(node[3], dict | None):
        if node[1] == "br":
            return "\n"
        props = node[3] or {}
        # LinkedIn's text component nests its content under textProps.
        children = props.get("children", (props.get("textProps") or {}).get("children"))
        return _rsc_text(children)
    return "".join(_rsc_text(child) for child in node)


class LinkedInFeedPostIE(InfoExtractor):
    IE_NAME = "linkedin:feedpost"
    _VALID_URL = [
        r"https?://(?:www\.)?linkedin\.com/feed/update/"
        r"urn:li:(?:groupPost|activity|ugcPost|share):(?P<id>[\d-]+)",
        # Shared post links: /posts/<author>_<slug>-ugcPost-<id>-<4 chars>
        r"https?://(?:www\.)?linkedin\.com/posts/[^/?#]+-"
        r"(?:ugcPost|activity|share)-(?P<id>\d+)-\w{4}/?(?:[?#]|$)",
    ]

    def _real_extract(self, url):
        video_id = self._match_id(url)
        # Group posts answer 404 to logged-out visitors; keep the body to
        # report "login required" instead of a bare HTTP error.
        webpage = self._download_webpage(url, video_id, expected_status=404)

        rsc = self._rehydration_stream(webpage, video_id)
        playlist = self._playlist(rsc, video_id) if rsc else None
        if not playlist:
            if LinkedInIE.suitable(url) and "<video" in webpage:
                # Logged-out public page: yt-dlp's own extractor handles it.
                return self.url_result(url, LinkedInIE)
            if not self._get_cookies("https://www.linkedin.com").get("li_at"):
                self.raise_login_required(
                    "LinkedIn posts need login cookies (li_at)", method="cookies",
                )
            raise ExtractorError("No video found in this LinkedIn post", expected=True)

        formats = []
        for source in playlist.get("sources") or []:
            src = url_or_none(source.get("src"))  # "$<row>" refs are inline data
            if not src:
                continue
            if source.get("type") == "application/x-mpegURL":
                formats.extend(self._extract_m3u8_formats(
                    src, video_id, "mp4", m3u8_id="hls", fatal=False,
                ))
            elif source.get("type") == "video/mp4":
                # e.g. .../mp4-720p-30fp-crf28/...; the label is not the height
                # (a "720p" file can be 640x800), so it only names the format.
                label = self._search_regex(
                    r"/(mp4-[\w-]+)/", src, "format id", default="mp4",
                )
                formats.append({"url": src, "format_id": label, "ext": "mp4"})
        if not formats:
            raise ExtractorError("LinkedIn post has no playable video sources")

        text = self._commentary(rsc, video_id)
        title = next((line.strip() for line in text.splitlines() if line.strip()), None)
        actors = re.findall(r'"actorName":"((?:[^"\\]|\\.)+)"', rsc)
        uploader = json.loads(f'"{actors[0]}"') if len(set(actors)) == 1 else None

        thumbnail = None
        thumb = playlist.get("thumbnail") or {}
        renditions = [r for r in thumb.get("imageRenditions") or [] if r.get("suffixUrl")]
        if thumb.get("rootUrl") and renditions:
            best = max(renditions, key=lambda r: r.get("width") or 0)
            thumbnail = url_or_none(thumb["rootUrl"] + best["suffixUrl"])

        subtitles = {}
        for track in playlist.get("textTracks") or []:
            if track.get("kind") == "captions" and url_or_none(track.get("src")):
                subtitles.setdefault("en", []).append({"url": track["src"], "ext": "vtt"})

        return {
            "id": video_id,
            "title": title or f"LinkedIn post {video_id}",
            "description": text or None,
            "uploader": uploader,
            "duration": int_or_none(playlist.get("duration")),
            "thumbnail": thumbnail,
            "formats": formats,
            "subtitles": subtitles,
            "webpage_url": url,
        }

    def _rehydration_stream(self, webpage, video_id) -> str | None:
        data = self._search_regex(
            _REHYDRATION_RE, webpage, "rehydration data", flags=re.DOTALL, default=None,
        )
        chunks = self._parse_json(data, video_id, fatal=False) if data else None
        if not isinstance(chunks, list):
            return None
        return "".join(chunk for chunk in chunks if isinstance(chunk, str))

    def _playlist(self, rsc, video_id) -> dict | None:
        start = rsc.find('"playlist":[')
        if start < 0:
            return None
        try:
            items, _ = json.JSONDecoder().raw_decode(rsc, start + len('"playlist":'))
        except json.JSONDecodeError:
            self.report_warning("Could not parse LinkedIn video playlist", video_id)
            return None
        return next((i for i in items if isinstance(i, dict) and i.get("sources")), None)

    def _commentary(self, rsc, video_id) -> str:
        row = self._search_regex(_COMMENTARY_ROW_RE, rsc, "post text", default=None)
        if not row:
            return ""
        try:
            return _rsc_text(json.loads(row)).strip()
        except json.JSONDecodeError:
            self.report_warning("Could not parse LinkedIn post text", video_id)
            return ""
