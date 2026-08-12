"""
Video analysis plans for the local-agent-senses toolkit.

Pure standard library. All intermediate data stays in memory (no temp files),
including remote media: the caller may pass raw bytes via the `input` argument
and they are fed to ffmpeg through an anonymous pipe (pipe:0).

Schemes:
  skim      - uniform low-fps sampling over the whole video
  scenes    - scene-change keyframes (pixel-difference based)
  window    - detailed sampling of a time range
  segments  - N equal segments, one representative frame each
  burst     - short window, high fps (animation / transient details)
  contact   - one tiled contact sheet of the whole video
  text      - dense sampling of a text band (subtitles / lyrics / captions)

Post-filter:
  dedupe    - drop frames too similar to the last kept frame
              (pixel difference + minimum time interval)
"""

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

SCRIPT_DIR = Path(__file__).resolve().parent
import config  # noqa: E402


@dataclass
class Frame:
    t: float
    jpeg: bytes
    w: int
    h: int


@dataclass
class FrameSet:
    frames: list
    duration: float
    width: int
    height: int
    mode: str
    meta: dict = field(default_factory=dict)


def ffmpeg_bin() -> str:
    candidates = [
        os.environ.get("VISION_FFMPEG", ""),
        config.cfg_value("ffmpeg"),
        str(SCRIPT_DIR / "ffmpeg" / "bin" / "ffmpeg.exe"),
        str(SCRIPT_DIR / "bin" / "ffmpeg.exe"),
        str(SCRIPT_DIR / "ffmpeg" / "bin" / "ffmpeg"),
        str(SCRIPT_DIR / "bin" / "ffmpeg"),
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    found = shutil.which("ffmpeg")
    if found:
        return found
    raise RuntimeError(
        "ffmpeg not found. Install ffmpeg and add it to PATH, or place it under "
        "the repo's ffmpeg\\bin directory (ffmpeg.exe on Windows)."
    )


def run_ffmpeg(args, timeout: int = 3600, input: bytes | None = None):
    args = _inject_http_opts(args)
    cmd = [ffmpeg_bin(), *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, input=input, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"ffmpeg exceeded its {timeout}s time limit (the media may be too "
            "large or corrupted; try a smaller clip, a lower --fps, or fewer "
            "--max-frames).") from None
    return proc.returncode, proc.stdout, proc.stderr.decode("utf-8", errors="replace")


def _inject_http_opts(args: list) -> list:
    """Add reconnect options and request headers before each http(s) ffmpeg input."""
    hdr = ""
    raw = os.environ.get("VISION_FFMPEG_HEADERS", "")
    if raw:
        try:
            headers = json.loads(raw)
        except ValueError:
            headers = {}
        if headers:
            hdr = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    out = []
    for i, a in enumerate(args):
        if a == "-i" and i + 1 < len(args) and str(args[i + 1]).startswith(("http://", "https://")):
            out += ["-reconnect", "1", "-reconnect_streamed", "1",
                    "-reconnect_delay_max", "5", "-rw_timeout", "30000000",
                    "-timeout", "20000000"]
            if hdr:
                out += ["-headers", hdr]
        out.append(a)
    return out


def _media_input(media: str, input: bytes | None = None):
    """ffmpeg input args for a file/URL or an in-memory byte stream."""
    if input is None:
        return ["-i", media], None
    return ["-i", "pipe:0"], input


def probe(path: str, input: bytes | None = None) -> dict:
    """Duration / resolution / audio / subtitle info from `ffmpeg -i` stderr."""
    args, data = _media_input(path, input)
    _, _, err = run_ffmpeg([*args], timeout=120, input=data)
    info = {"duration": 0.0, "width": 0, "height": 0, "has_audio": False, "has_subtitle": False}
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", err)
    if m:
        info["duration"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", err)
    if m:
        info["width"], info["height"] = int(m.group(1)), int(m.group(2))
    if re.search(r"Stream #\d+:\d+.*Audio:", err):
        info["has_audio"] = True
    if re.search(r"Stream #\d+:\d+.*Subtitle:", err):
        info["has_subtitle"] = True
    return info


def extract_subtitle_text(path: str, input: bytes | None = None) -> str | None:
    """Extract the first subtitle track as compact text (SRT -> lines)."""
    args, data = _media_input(path, input)
    code, out, err = run_ffmpeg([*args, "-map", "0:s:0", "-f", "srt", "-"],
                                timeout=600, input=data)
    if code != 0 or not out:
        return None
    text = out.decode("utf-8", errors="replace")
    blocks = re.split(r"\n\s*\n", text.strip())
    lines = []
    for block in blocks:
        lines_b = block.strip().splitlines()
        if len(lines_b) < 3:
            continue
        timing = lines_b[1].strip()
        content = " ".join(l.strip() for l in lines_b[2:])
        if content:
            lines.append(f"[{timing}] {content}")
    return "\n".join(lines) if lines else None


def _split_mjpeg(blob: bytes):
    """Split a concatenated MJPEG stream (image2pipe) into individual JPEGs.

    Walks JPEG markers instead of scanning for a raw FF D9, so a FF D9 byte
    inside a length-delimited segment (e.g. EXIF data in APP1) cannot
    truncate a frame early.
    """
    frames = []
    pos = 0
    n = len(blob)
    while True:
        start = blob.find(b"\xff\xd8", pos)
        if start < 0:
            break
        i = start + 2
        end = -1
        while i + 1 < n:
            if blob[i] != 0xFF:
                i += 1
                continue
            marker = blob[i + 1]
            if marker == 0xFF:          # fill byte before a marker
                i += 1
                continue
            if marker == 0xD9:          # EOI
                end = i + 2
                break
            if marker == 0xD8:          # next image starts without EOI
                end = i
                break
            if 0xD0 <= marker <= 0xD7 or marker == 0x00:
                # restart marker / byte-stuffed FF inside entropy data
                i += 2
                continue
            if marker == 0x01:          # TEM has no length field
                i += 2
                continue
            if i + 4 > n:
                break
            seg_len = int.from_bytes(blob[i + 2:i + 4], "big")
            i += 2 + seg_len
        if end < 0:
            break
        frames.append(blob[start:end])
        pos = end
    return frames


def _scale_dims(width: int, height: int, max_side: int) -> tuple[int, int]:
    if width <= 0 or height <= 0:
        return max_side, max_side
    if width <= max_side:
        return width, height
    ratio = max_side / width
    return max_side, max(2, int(round(height * ratio)))


def _crop_dims(width: int, height: int, crop: str | None) -> tuple[int, int]:
    """Dimensions after an ffmpeg crop=W:H:X:Y filter (or original)."""
    if not crop:
        return width, height
    m = re.fullmatch(r"(\d+):(\d+):[+-]?\d+:[+-]?\d+", crop)
    if not m:
        return width, height
    return int(m.group(1)), int(m.group(2))


def _band_crop(width: int, height: int, band: str = "bottom") -> str | None:
    """ffmpeg crop=W:H:X:Y for the bottom/top/middle text band."""
    if band == "full" or width <= 0 or height <= 0:
        return None
    if band == "top":
        return f"{width}:{max(1, height // 4)}:0:0"
    if band == "middle":
        return f"{width}:{max(1, height // 3)}:0:{max(0, height // 3)}"
    y = height * 3 // 4
    return f"{width}:{max(1, height - y)}:0:{y}"


def extract_all_jpegs(path: str, fps: float, max_side: int, start: float | None = None,
                      end: float | None = None, crop: str | None = None,
                      input: bytes | None = None) -> list[Frame]:
    """One decode pass -> all sampled frames as JPEGs in memory with timestamps."""
    info = probe(path, input=input)
    cw, ch = _crop_dims(info["width"], info["height"], crop)
    w, h = _scale_dims(cw, ch, max_side)
    args, data = _media_input(path, input)
    if start is not None:
        ss = ["-ss", f"{start:.3f}"]
        args = ss + args if data is None else args + ss
    if end is not None:
        args = [*args, "-t", f"{end - (start or 0.0):.3f}"]
    parts = [f"fps={fps}"]
    if crop:
        parts.append(f"crop={crop}")
    parts.append(f"scale={w}:{h}")
    vf = ",".join(parts)
    args += ["-vf", vf, "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "2", "-"]
    code, out, err = run_ffmpeg(args, input=data)
    if code != 0 or not out:
        raise RuntimeError(f"ffmpeg frame extraction failed: {err.strip()[:300]}")
    jpegs = _split_mjpeg(out)
    t0 = start if start is not None else 0.0
    return [Frame(t=t0 + i / fps, jpeg=j, w=w, h=h) for i, j in enumerate(jpegs)]


def extract_frame_seek(path: str, t: float, max_side: int, crop: str | None = None,
                       input: bytes | None = None,
                       keyframes: bool = False) -> Frame | None:
    """Extract one frame near timestamp t (fast seek for files, decode-seek for
    pipes). With keyframes=True, only keyframes are decoded (-skip_frame nokey):
    much faster for long videos, at the cost of sub-second accuracy."""
    info = probe(path, input=input)
    cw, ch = _crop_dims(info["width"], info["height"], crop)
    w, h = _scale_dims(cw, ch, max_side)
    args, data = _media_input(path, input)
    ss = ["-ss", f"{t:.3f}"]
    skip = ["-skip_frame", "nokey"] if keyframes else []
    # HLS (m3u8) cannot seek before opening the input; use output seek instead.
    prefer_output_seek = data is not None or str(path).lower().endswith(".m3u8")
    orders = [skip + args + ss] if prefer_output_seek else [skip + ss + args]
    if data is None:
        orders.append(skip + (ss + args if prefer_output_seek else args + ss))
    out = b""
    for ordered in orders:
        vf = f"crop={crop}," if crop else ""
        vf += f"scale={w}:{h}"
        code, out, err = run_ffmpeg(
            [*ordered, "-frames:v", "1", "-vf", vf,
             "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "2", "-"],
            timeout=180, input=data)
        if code == 0 and out:
            break
        out = b""
    if not out:
        return None
    jpegs = _split_mjpeg(out)
    return Frame(t=t, jpeg=jpegs[0], w=w, h=h) if jpegs else None


def extract_thumbnails(path: str, fps: float = 1.0, width: int = 160, height: int = 90,
                       start: float | None = None, end: float | None = None,
                       crop: str | None = None, input: bytes | None = None):
    """Tiny grayscale frames for dHash-based similarity (cheap, all in memory)."""
    args, data = _media_input(path, input)
    if start is not None:
        ss = ["-ss", f"{start:.3f}"]
        args = ss + args if data is None else args + ss
    if end is not None:
        args = [*args, "-t", f"{end - (start or 0.0):.3f}"]
    vf = f"fps={fps}"
    if crop:
        vf += f",crop={crop}"
    vf += f",scale={width}:{height}"
    args += ["-vf", vf, "-pix_fmt", "gray",
             "-f", "rawvideo", "-"]
    code, out, err = run_ffmpeg(args, input=data)
    if code != 0:
        raise RuntimeError(f"ffmpeg thumbnail extraction failed: {err.strip()[:300]}")
    frame_size = width * height
    frames = []
    t0 = start if start is not None else 0.0
    for i in range(len(out) // frame_size):
        chunk = out[i * frame_size : (i + 1) * frame_size]
        frames.append((t0 + i / fps, chunk))
    return frames


def frame_diff(a: bytes, b: bytes) -> float:
    """Mean absolute pixel difference (0..255) between two gray thumbnails."""
    if len(a) != len(b):
        return 255.0
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def _select_by_dedupe(times, frames, threshold: float, min_interval: float):
    """Greedy dedupe: keep a frame when it differs from the last kept frame
    or when the time gap exceeds min_interval."""
    if not times:
        return []
    kept = [0]
    last_f, last_t = frames[0], times[0]
    for i in range(1, len(times)):
        if frame_diff(frames[i], last_f) >= threshold or times[i] - last_t >= min_interval:
            kept.append(i)
            last_f, last_t = frames[i], times[i]
    return kept


def _pick_uniform(times, max_frames: int):
    if len(times) <= max_frames:
        return list(range(len(times)))
    step = len(times) / max_frames
    return sorted({min(len(times) - 1, int(i * step)) for i in range(max_frames)})


def _extract_selected(path, times, max_side, input=None, crop=None, fast_threshold=1800):
    """Extract JPEGs for selected timestamps. Uses a single pass for normal
    videos (exact timestamps) and per-frame seek for very long ones."""
    is_remote = str(path).startswith(("http://", "https://"))
    fast = bool(times) and not is_remote and times[-1] <= fast_threshold
    if fast:
        try:
            all_frames = extract_all_jpegs(path, fps=1.0, max_side=max_side,
                                           input=input, crop=crop)
            by_time = {round(f.t, 2): f for f in all_frames}
            return [by_time.get(round(t, 2))
                    or extract_frame_seek(path, t, max_side, input=input, crop=crop)
                    for t in times]
        except Exception:
            pass
    frames = []
    for t in times:
        # Long or remote videos decode only keyframes: dramatically faster,
        # and the sampled frame is still within one GOP of the target time.
        f = extract_frame_seek(path, t, max_side, input=input, crop=crop,
                               keyframes=not fast)
        if f:
            frames.append(f)
    return frames


def _finalize(path, info, times, max_side, mode, meta=None, input=None, crop=None) -> FrameSet:
    frames = [f for f in _extract_selected(path, times, max_side, input=input, crop=crop) if f]
    if not frames:
        raise RuntimeError("No usable frames extracted; make sure the video decodes correctly.")
    return FrameSet(
        frames=frames,
        duration=info["duration"],
        width=frames[0].w,
        height=frames[0].h,
        mode=mode,
        meta=meta or {},
    )


def scheme_skim(path, info, fps=1.0, max_frames=24, max_side=1600,
                dedupe=True, dedupe_threshold=3.0, min_interval=10.0,
                input=None) -> FrameSet:
    thumbs = extract_thumbnails(path, fps=fps, input=input)
    times = [t for t, _ in thumbs]
    frames = [g for _, g in thumbs]
    if dedupe:
        keep = _select_by_dedupe(times, frames, dedupe_threshold, min_interval)
    else:
        keep = list(range(len(times)))
    selected = [times[i] for i in keep]
    chosen = _pick_uniform(selected, max_frames)
    times = [selected[i] for i in chosen]
    return _finalize(path, info, times, max_side, "skim",
                     {"sampled": len(thumbs), "kept": len(times)}, input=input)


def scheme_scenes(path, info, threshold=4.0, max_frames=24, max_side=1600,
                  input=None, max_gap=30.0) -> FrameSet:
    """Scene-change keyframes, with a max-gap safety net so a long static
    segment still gets sampled instead of silently dropping out."""
    thumbs = extract_thumbnails(path, fps=1.0, input=input)
    times = [t for t, _ in thumbs]
    frames = [g for _, g in thumbs]
    dists = [frame_diff(frames[i], frames[i - 1]) for i in range(1, len(frames))]
    used_threshold = threshold
    indices = None
    for t in (threshold, threshold / 2):
        idx = [0] + [i + 1 for i, d in enumerate(dists) if d >= t]
        if 2 <= len(idx) <= max_frames:
            indices = idx
            used_threshold = t
            break
    if indices is None:
        # fallback: uniform segments (standard keyframe-extraction fallback)
        indices = _pick_uniform(list(range(len(times))), max_frames)
    scene_times = [times[i] for i in indices]
    selected = _fill_gaps(scene_times, times, max_gap, max_frames)
    return _finalize(path, info, selected, max_side, "scenes",
                     {"threshold": round(used_threshold, 2),
                      "scene_frames": len(scene_times),
                      "max_gap": max_gap},
                     input=input)


def _fill_gaps(times, all_times, max_gap, max_frames):
    """Fill the largest silent gap with its midpoint until no gap exceeds
    max_gap or the frame cap is reached."""
    if max_gap <= 0 or len(times) < 2:
        return list(times)
    out = list(times)
    for _ in range(max_frames - len(out)):
        gap_i, gap = max(
            ((i, out[i + 1] - out[i]) for i in range(len(out) - 1)),
            key=lambda p: p[1], default=(-1, 0.0),
        )
        if gap <= max_gap:
            break
        mid = (out[gap_i] + out[gap_i + 1]) / 2
        pick = min(all_times, key=lambda t: abs(t - mid))
        if pick in out:
            break
        out.insert(gap_i + 1, pick)
    return out


def scheme_window(path, info, start, end, fps=1.0, max_frames=24, max_side=1600,
                  dedupe=True, dedupe_threshold=3.0, min_interval=5.0,
                  input=None) -> FrameSet:
    start = max(0.0, start)
    end = min(info["duration"], end) if info["duration"] else end
    if end <= start:
        raise ValueError(f"Invalid time window: from={start:.1f}s to={end:.1f}s")
    thumbs = extract_thumbnails(path, fps=fps, start=start, end=end, input=input)
    times = [t for t, _ in thumbs]
    frames = [g for _, g in thumbs]
    if dedupe:
        keep = _select_by_dedupe(times, frames, dedupe_threshold, min_interval)
    else:
        keep = list(range(len(times)))
    selected = [times[i] for i in keep]
    chosen = _pick_uniform(selected, max_frames)
    times = [selected[i] for i in chosen]
    return _finalize(path, info, times, max_side, "window",
                     {"window": [round(start, 1), round(end, 1)]}, input=input)


def scheme_segments(path, info, n=6, max_side=1600, input=None) -> FrameSet:
    n = max(1, min(n, 24))
    duration = info["duration"] or 1.0
    times = [(i + 0.5) * duration / n for i in range(n)]
    return _finalize(path, info, times, max_side, "segments", {"n": n}, input=input)


def scheme_burst(path, info, start, duration=3.0, fps=5.0, max_frames=24,
                 max_side=1600, input=None) -> FrameSet:
    end = start + duration
    fs = scheme_window(path, info, start, end, fps=fps, max_frames=max_frames,
                       max_side=max_side, dedupe=False, input=input)
    fs.mode = "burst"
    return fs


def scheme_contact(path, info, n=24, max_side=640, input=None) -> FrameSet:
    """One tiled contact sheet (ffmpeg tile filter) + per-cell timestamps."""
    duration = info["duration"] or 1.0
    n = max(1, min(n, 100))
    fps = max(1.0, n / duration)
    cols = int(round(n ** 0.5))
    rows = (n + cols - 1) // cols
    args, data = _media_input(path, input)
    args += ["-vf", f"fps={fps},scale=160:90,tile={cols}x{rows}",
             "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "3", "-"]
    code, out, err = run_ffmpeg(args, input=data)
    if code != 0 or not out:
        raise RuntimeError(f"Contact sheet generation failed: {err.strip()[:300]}")
    jpegs = _split_mjpeg(out)
    if not jpegs:
        raise RuntimeError("Contact sheet generation failed: no output.")
    sheet_w, sheet_h = _scale_dims(info["width"], info["height"], max_side)
    timestamps = [round(i / fps, 1) for i in range(n)]
    return FrameSet(
        frames=[Frame(t=0.0, jpeg=jpegs[0], w=sheet_w, h=sheet_h)],
        duration=duration,
        width=sheet_w,
        height=sheet_h,
        mode="contact",
        meta={"cells": n, "cols": cols, "rows": rows, "timestamps": timestamps},
    )


def scheme_text(path, info, fps=1.0, max_frames=48, max_side=1600, dedupe=True,
                band="bottom", dedupe_threshold=2.0, min_interval=1.0,
                input=None) -> FrameSet:
    """Dense sampling of the subtitle/lyric band (bottom by default).

    Keeps every visibly changed frame so text that only changes in a small
    region (karaoke lines, captions) is not dropped by scene/dedupe filters.
    """
    crop = _band_crop(info["width"], info["height"], band)
    thumbs = extract_thumbnails(path, fps=fps, crop=crop, input=input)
    times = [t for t, _ in thumbs]
    frames = [g for _, g in thumbs]
    if dedupe:
        keep = _select_by_dedupe(times, frames, dedupe_threshold, min_interval)
    else:
        keep = list(range(len(times)))
    selected = [times[i] for i in keep]
    chosen = _pick_uniform(selected, max_frames)
    times = [selected[i] for i in chosen]
    return _finalize(path, info, times, max_side, "text",
                     {"band": band, "crop": crop, "sampled": len(thumbs),
                      "kept": len(times)},
                     input=input, crop=crop)


SCHEMES = {
    "skim": scheme_skim,
    "scenes": scheme_scenes,
    "window": scheme_window,
    "segments": scheme_segments,
    "burst": scheme_burst,
    "contact": scheme_contact,
    "text": scheme_text,
}
