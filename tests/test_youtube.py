from __future__ import annotations

import json

from discord_bot.tools.youtube import (
    extract_player_response,
    format_duration,
    parse_transcript,
    parse_video_id,
    pick_caption_track,
    transcript_url,
    video_info,
)

VIDEO_ID = "dQw4w9WgXcQ"


def test_parse_video_id_supports_common_url_shapes() -> None:
    urls = [
        f"https://www.youtube.com/watch?v={VIDEO_ID}",
        f"https://youtu.be/{VIDEO_ID}?t=42",
        f"https://youtube.com/shorts/{VIDEO_ID}",
        f"https://www.youtube.com/embed/{VIDEO_ID}",
        f"https://www.youtube-nocookie.com/embed/{VIDEO_ID}",
        f"https://m.youtube.com/watch?v={VIDEO_ID}&feature=share",
    ]
    for url in urls:
        assert parse_video_id(url) == VIDEO_ID, url


def test_parse_video_id_rejects_other_hosts_and_bad_ids() -> None:
    assert parse_video_id(f"https://example.com/watch?v={VIDEO_ID}") is None
    assert parse_video_id("https://www.youtube.com/playlist?list=abc") is None
    assert parse_video_id("https://youtu.be/short") is None
    assert parse_video_id("not a url") is None


def test_extract_player_response_uses_raw_decode() -> None:
    player = {"videoDetails": {"title": "Test", "videoId": VIDEO_ID}}
    page = (
        "<html><script>var ytInitialPlayerResponse = "
        f"{json.dumps(player)};</script><script>var other = {{}};</script></html>"
    )

    assert extract_player_response(page) == player


def test_extract_player_response_returns_none_for_consent_pages() -> None:
    assert extract_player_response("<html><body>Before you continue</body></html>") is None
    assert extract_player_response("ytInitialPlayerResponse = {broken;") is None


def _player(tracks: list[dict]) -> dict:
    return {
        "videoDetails": {
            "videoId": VIDEO_ID,
            "title": "  Название   видео ",
            "author": "Автор",
            "lengthSeconds": "754",
            "shortDescription": "Описание",
        },
        "captions": {
            "playerCaptionsTracklistRenderer": {"captionTracks": tracks}
        },
    }


def test_pick_caption_track_prefers_russian_then_manual() -> None:
    russian_auto = {
        "baseUrl": "https://www.youtube.com/api/timedtext?v=x&lang=ru",
        "languageCode": "ru",
        "kind": "asr",
    }
    english_manual = {
        "baseUrl": "https://www.youtube.com/api/timedtext?v=x&lang=en",
        "languageCode": "en",
    }
    russian_manual = {
        "baseUrl": "https://www.youtube.com/api/timedtext?v=x&lang=ru",
        "languageCode": "ru",
    }

    assert pick_caption_track(_player([english_manual, russian_auto])) is russian_auto
    assert (
        pick_caption_track(_player([russian_auto, russian_manual]))
        is russian_manual
    )
    assert pick_caption_track({"videoDetails": {}}) is None


def test_transcript_url_adds_json3_and_rejects_foreign_hosts() -> None:
    track = {"baseUrl": "https://www.youtube.com/api/timedtext?v=x&lang=ru"}
    assert transcript_url(track) == (
        "https://www.youtube.com/api/timedtext?v=x&lang=ru&fmt=json3"
    )
    assert transcript_url({"baseUrl": "//www.youtube.com/api/timedtext?v=x"}) == (
        "https://www.youtube.com/api/timedtext?v=x&fmt=json3"
    )
    assert transcript_url({"baseUrl": "https://evil.example.com/x"}) is None
    assert transcript_url({"baseUrl": ""}) is None


def test_transcript_url_replaces_an_existing_format() -> None:
    track = {"baseUrl": "https://www.youtube.com/api/timedtext?v=x&fmt=srv3"}

    assert transcript_url(track) == (
        "https://www.youtube.com/api/timedtext?v=x&fmt=json3"
    )


def test_parse_transcript_joins_segments_and_unescapes() -> None:
    payload = json.dumps(
        {
            "events": [
                {"segs": [{"utf8": "Привет &amp; "}]},
                {"segs": [{"utf8": "мир"}, {"utf8": "!"}]},
                {"segs": None},
                {"no": "segs"},
            ]
        }
    )

    assert parse_transcript(payload) == "Привет & мир!"


def test_parse_transcript_handles_broken_payloads() -> None:
    assert parse_transcript("not json") == ""
    assert parse_transcript("[]") == ""
    assert parse_transcript('{"events": "nope"}') == ""


def test_video_info_cleans_and_formats_fields() -> None:
    track = {"languageCode": "ru", "kind": "asr"}

    info = video_info(
        VIDEO_ID, _player([track]), transcript="текст", track=track
    )

    assert info.title == "Название видео"
    assert info.author == "Автор"
    assert info.length_seconds == 754
    assert info.transcript == "текст"
    assert info.transcript_language == "ru"
    assert info.transcript_auto is True


def test_format_duration() -> None:
    assert format_duration(0) == ""
    assert format_duration(65) == "1:05"
    assert format_duration(754) == "12:34"
    assert format_duration(3725) == "1:02:05"
