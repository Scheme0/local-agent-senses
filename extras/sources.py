#!/usr/bin/env python3
"""Unified video-site resolution via yt-dlp.

All video-site page links (YouTube, X, TikTok, Bilibili, ...) are resolved
through one channel: yt-dlp extracts the direct media URL(s) plus the request
headers needed to play them. Direct media links (.mp4/.m3u8/.png, ...) never
reach this module; they go through the generic direct-link/buffer path in
media.py.

The Resolver registry is kept as a mechanism-based extension point (a future
resolver should cover a whole class of sites, not one specific site); no
per-site resolver is built in.
"""

import json
import shutil
import subprocess
from dataclasses import dataclass
from typing import Protocol

import config
import media
import media_formats as formats


@dataclass
class ResolvedStream:
    """Resolution result: direct links + request headers + extra metadata."""

    video_url: str = ""
    audio_url: str = ""
    headers: dict | None = None
    title: str | None = None
    duration: float = 0
    subtitle_text: str | None = None
    platform: str = ""


class ResolverError(RuntimeError):
    """A matched resolver failed and generic URL fallback must not hide it."""

    def __init__(self, resolver: str, message: str):
        super().__init__(f"{resolver} resolution failed: {message}")
        self.resolver = resolver


class Resolver(Protocol):
    """Resolver interface: match decides whether a URL is handled, resolve
    returns direct stream information."""

    name: str

    def match(self, url: str) -> bool: ...

    def resolve(self, url: str) -> ResolvedStream: ...


_REGISTRY: list[Resolver] = []


def register(resolver: Resolver) -> None:
    """Register a resolver; duplicates by name are ignored."""
    if not any(r.name == resolver.name for r in _REGISTRY):
        _REGISTRY.append(resolver)


def registered() -> list[str]:
    """Names of all registered resolvers."""
    return [r.name for r in _REGISTRY]


def try_resolve(url: str) -> ResolvedStream | None:
    """Try registered resolvers in order; None when none matches (the generic
    direct-link/buffer path is then used)."""
    for resolver in _REGISTRY:
        try:
            matched = resolver.match(url)
        except Exception as exc:
            raise ResolverError(resolver.name, f"could not inspect URL: {exc}") from exc
        if matched:
            try:
                return resolver.resolve(url)
            except ResolverError:
                raise
            except Exception as exc:
                raise ResolverError(resolver.name, str(exc)) from exc
    return None


def ytdlp_exe() -> str:
    """Locate yt-dlp: VISION_YTDLP / config yt_dlp > PATH."""
    explicit = config.env("VISION_YTDLP") or config.cfg_value("yt_dlp")
    if explicit:
        config.validate_external_tool(explicit, "VISION_YTDLP")
        return explicit
    found = shutil.which("yt-dlp")
    if found:
        return found
    raise RuntimeError("yt-dlp not found. Install it with `pip install yt-dlp`, "
                       "or set VISION_YTDLP / the yt_dlp config field")


def ytdlp_available() -> bool:
    try:
        ytdlp_exe()
        return True
    except Exception:
        return False


def _best_stream(formats_list: list[dict], want_video: bool) -> str:
    """Pick the best URL from a yt-dlp format list by (height, bitrate)."""
    norm = []
    for f in formats_list:
        url = f.get("url") or ""
        if not url:
            continue
        norm.append({
            "url": url,
            "height": f.get("height") or 0,
            "tbr": f.get("tbr") or 0,
            "vcodec": f.get("vcodec") or "none",
            "acodec": f.get("acodec") or "none",
        })
    if want_video:
        pool = [n for n in norm if n["vcodec"] != "none"]
    else:
        pool = [n for n in norm if n["acodec"] != "none"]
    if not pool:
        return ""
    return max(pool, key=lambda n: (n["height"], n["tbr"]))["url"]


def _pick_ytdlp_streams(info: dict) -> tuple[str, str]:
    """Pick (video URL, audio URL) from yt-dlp --dump-single-json formats."""
    formats_list = info.get("formats") or []
    if not formats_list and info.get("url"):
        return info["url"], ""
    combined = [f for f in formats_list
                if f.get("vcodec") != "none" and f.get("acodec") != "none"]
    if combined:
        return _best_stream(combined, want_video=True), ""
    return (_best_stream(formats_list, want_video=True),
            _best_stream(formats_list, want_video=False))


class YtDlpResolver:
    """Universal site resolver: yt-dlp covers most common video sites
    (YouTube / X / TikTok / Bilibili, ...). Only plain http(s) page links are
    handled; direct media links (.mp4/.m3u8/.png, ...) are skipped and go
    through the generic direct-link/buffer path. Without yt-dlp installed,
    match returns False and behavior is unchanged."""

    name = "yt-dlp"

    def match(self, url: str) -> bool:
        if not (url.startswith("http://") or url.startswith("https://")):
            return False
        clean = url.split("?", 1)[0].lower()
        if clean.endswith(formats.DIRECT_MEDIA_SUFFIXES):
            return False
        return ytdlp_available()

    def resolve(self, url: str) -> ResolvedStream:
        # yt-dlp is an independent network client (own DNS, own redirects), so
        # the original URL is re-checked here even when the caller already
        # checked it, and every returned stream URL is checked again.
        media.ensure_url_safe(url)
        proc = subprocess.run(
            [ytdlp_exe(), "--dump-single-json", "--no-playlist", "--no-warnings",
             "--no-download", url],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"yt-dlp resolution failed: "
                               f"{(proc.stderr or proc.stdout).strip()[:300]}")
        info = json.loads(proc.stdout)
        video_url, audio_url = _pick_ytdlp_streams(info)
        if video_url:
            media.ensure_url_safe(video_url)
        if audio_url:
            media.ensure_url_safe(audio_url)
        if not video_url:
            raise RuntimeError("yt-dlp found no usable video stream")
        headers = None
        first = (info.get("formats") or [{}])[0]
        hdrs = first.get("http_headers") or info.get("http_headers")
        if hdrs:
            headers = dict(hdrs)
        return ResolvedStream(
            video_url=video_url,
            audio_url=audio_url,
            headers=headers,
            title=info.get("title"),
            duration=info.get("duration") or 0,
            platform="yt-dlp",
        )


register(YtDlpResolver())
