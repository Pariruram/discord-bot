"""Best-effort YouTube metadata and transcripts without extra dependencies.

YouTube renders its watch pages with JavaScript, so a plain HTML reader only
sees the title and meta description. The initial player response embedded in
the page still carries the video details and caption track URLs, which is
enough to answer "what is this video about" without a headless browser.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

YOUTUBE_HOSTS = {
    "youtu.be",
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}

TRANSCRIPT_HOSTS = {"youtube.com", "www.youtube.com"}


@dataclass(frozen=True, slots=True)
class VideoInfo:
    video_id: str
    title: str = ""
    author: str = ""
    length_seconds: int = 0
    description: str = ""
    transcript: str = ""
    transcript_language: str = ""
    transcript_auto: bool = False


def parse_video_id(url: str) -> str | None:
    """Return the 11-character video id for a YouTube URL, else None."""

    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host not in YOUTUBE_HOSTS:
        return None

    if host == "youtu.be":
        candidate = parts.path.strip("/").split("/")[0]
    elif parts.path == "/watch":
        candidate = parse_qs(parts.query).get("v", [""])[0]
    elif parts.path.startswith(("/shorts/", "/embed/", "/live/")):
        candidate = parts.path.split("/")[2] if len(parts.path.split("/")) > 2 else ""
    else:
        candidate = ""

    return candidate if VIDEO_ID_RE.fullmatch(candidate) else None


def extract_player_response(page: str) -> dict[str, Any] | None:
    """Pull ``ytInitialPlayerResponse`` out of the raw watch page."""

    match = re.search(r"ytInitialPlayerResponse\s*=\s*\{", page)
    if match is None:
        return None
    start = match.end() - 1
    try:
        data, _ = json.JSONDecoder().raw_decode(page[start:])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _video_details(player: dict[str, Any]) -> dict[str, Any]:
    details = player.get("videoDetails")
    return details if isinstance(details, dict) else {}


def pick_caption_track(player: dict[str, Any]) -> dict[str, Any] | None:
    """Prefer Russian, then English; manual captions beat auto-generated."""

    captions = player.get("captions")
    if not isinstance(captions, dict):
        return None
    renderer = captions.get("playerCaptionsTracklistRenderer")
    if not isinstance(renderer, dict):
        return None
    tracks = renderer.get("captionTracks")
    if not isinstance(tracks, list):
        return None

    candidates = [track for track in tracks if isinstance(track, dict)]
    if not candidates:
        return None

    def score(track: dict[str, Any]) -> tuple[int, int]:
        language = str(track.get("languageCode") or "")
        priority = {"ru": 0, "en": 1}.get(language, 5)
        manual = 0 if track.get("kind") != "asr" else 1
        return priority, manual

    return min(candidates, key=score)


def transcript_url(track: dict[str, Any]) -> str | None:
    url = str(track.get("baseUrl") or "").strip()
    if not url:
        return None
    if url.startswith("//"):
        url = f"https:{url}"
    host = (urlsplit(url).hostname or "").lower()
    if host not in TRANSCRIPT_HOSTS:
        return None
    if "fmt=" in url:
        url = re.sub(r"fmt=[^&]*", "fmt=json3", url)
    else:
        url = f"{url}{'&' if '?' in url else '?'}fmt=json3"
    return url


def parse_transcript(payload: str) -> str:
    """Join a YouTube ``json3`` transcript into readable text."""

    try:
        data = json.loads(payload)
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""

    events = data.get("events")
    if not isinstance(events, list):
        return ""

    parts: list[str] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        segments = event.get("segs")
        if not isinstance(segments, list):
            continue
        text = "".join(
            str(segment.get("utf8") or "")
            for segment in segments
            if isinstance(segment, dict)
        )
        if text.strip():
            parts.append(text)
    joined = html.unescape("".join(parts))
    return " ".join(joined.split())


def video_info(
    video_id: str,
    player: dict[str, Any],
    transcript: str = "",
    track: dict[str, Any] | None = None,
) -> VideoInfo:
    details = _video_details(player)
    try:
        length = int(str(details.get("lengthSeconds") or 0))
    except (TypeError, ValueError):
        length = 0
    language = str((track or {}).get("languageCode") or "")
    return VideoInfo(
        video_id=video_id,
        title=_clean(details.get("title")),
        author=_clean(details.get("author")),
        length_seconds=length,
        description=_clean(details.get("shortDescription")),
        transcript=transcript.strip(),
        transcript_language=language,
        transcript_auto=str((track or {}).get("kind") or "") == "asr",
    )


def format_duration(seconds: int) -> str:
    if seconds <= 0:
        return ""
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())
