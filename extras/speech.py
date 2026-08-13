#!/usr/bin/env python3
"""Local speech transcription (FunASR / SenseVoice) for the vision skill.

Audio is decoded to memory via ffmpeg pipes; nothing is written to disk except
FunASR's own model cache. Output lines carry timestamps:

    [00:03 - 00:18] Let me show you how this button works

Usage:
    python speech.py <video_or_audio> [--lang auto|zh|en|yue|ja|ko] [--model sensevoice|paraformer]
"""

import argparse
import re
import sys
from pathlib import Path

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR.parent))
import config  # noqa: E402
import media  # noqa: E402
import video_plans  # noqa: E402

_STDIN_BYTES: bytes | None = None


def media_spec(media: str):
    """ffmpeg input args for a file/URL or an in-memory byte stream (stdin)."""
    if media == "-":
        return ["-i", "pipe:0"], _STDIN_BYTES
    return ["-i", media], None


def probe_has_audio(media: str) -> bool:
    return bool(video_plans.probe(
        media, input=_STDIN_BYTES if media == "-" else None,
    ).get("has_audio"))


def speech_segments(media: str) -> list[tuple[float, float]]:
    """Find speech intervals using ffmpeg silencedetect (whole file in one pass)."""
    args, data = media_spec(media)
    code, _, stderr = video_plans.run_ffmpeg(
        [*args, "-af", "silencedetect=noise=-35dB:d=0.5", "-f", "null", "-"],
        timeout=1800, input=data,
    )
    if code != 0:
        raise RuntimeError(f"Audio analysis failed: {stderr.strip()[:300]}")
    starts = [float(m) for m in re.findall(r"silence_start:\s*([\d.]+)", stderr)]
    ends = [float(m) for m in re.findall(r"silence_end:\s*([\d.]+)", stderr)]
    if not starts:
        return [(0.0, 1e9)]
    # speech chunks: [0..silence1] [silence1end..silence2] ...
    chunks = []
    if starts[0] > 0.5:
        chunks.append((0.0, starts[0]))
    for i in range(len(ends)):
        if i < len(starts) - 1:
            chunks.append((ends[i], starts[i + 1]))
    if ends and ends[-1] < 1e8:
        chunks.append((ends[-1], 1e9))
    if not chunks:
        chunks = [(0.0, 1e9)]
    padded = []
    for s, e in chunks:
        s = max(0.0, s - 0.3)
        e = e + 0.3
        if e - s >= 0.3:
            padded.append((s, e))
    return padded


def decode_pcm(media: str, start: float, end: float) -> "numpy.ndarray":  # noqa: F821
    import numpy as np

    args, data = media_spec(media)
    ss = ["-ss", f"{start:.3f}", "-to", f"{end:.3f}"]
    if data is None:
        # HLS cannot seek before opening the input; files use fast input seek.
        prefer_output = str(media).lower().endswith(".m3u8")
        commands = [ss + args] if prefer_output else [ss + args, args + ss]
    else:
        # Anonymous pipes are not seekable: decode from the start and discard.
        commands = [args + ss]
    out = b""
    for cmd in commands:
        code, out, stderr = video_plans.run_ffmpeg(
            [*cmd, "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
            timeout=1800, input=data,
        )
        if code == 0 and out:
            break
        out = b""
    if not out:
        return np.zeros(1600, dtype=np.float32)
    samples = np.frombuffer(out, dtype=np.int16).astype(np.float32) / 32768.0
    return samples


def fmt(seconds: float) -> str:
    seconds = max(0.0, seconds)
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m:02d}:{s:02d}"


def load_model(model_name: str, lang: str):
    import torch
    from funasr import AutoModel
    from funasr.utils.postprocess_utils import rich_transcription_postprocess

    device = "cuda:0" if torch.cuda.is_available() else "cpu"
    if model_name == "paraformer":
        model = AutoModel(
            model="iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
            vad_model="fsmn-vad",
            punc_model="ct-punc",
            device=device,
            disable_update=True,
            hub="modelscope",
        )
    else:
        model = AutoModel(
            model="iic/SenseVoiceSmall",
            vad_model="fsmn-vad",
            vad_kwargs={"max_single_segment_time": 30000},
            trust_remote_code=True,
            device=device,
            disable_update=True,
            hub="modelscope",
        )
    return model, rich_transcription_postprocess


def transcribe(media: str, lang: str, model_name: str) -> None:
    if not probe_has_audio(media):
        raise RuntimeError("No audio track found in this file (muted or video-only).")
    try:
        model, postprocess = load_model(model_name, lang)
    except ImportError as e:
        raise RuntimeError(
            f"FunASR is not installed ({e}). Install it in the speech environment "
            "with: pip install funasr (users in China may add "
            "-i https://pypi.tuna.tsinghua.edu.cn/simple). Set VISION_SPEECH_PYTHON "
            "or speech_env if the environment is not auto-detected."
        ) from e

    segments = speech_segments(media)
    for start, end in segments:
        samples = decode_pcm(media, start, end)
        if len(samples) < 1600:
            continue
        # SenseVoice: pass the requested language through (default "auto").
        # Paraformer is Mandarin-only, so it always uses "zh".
        language = lang if model_name == "sensevoice" else "zh"
        try:
            res = model.generate(
                input=samples,
                cache={},
                language=language,
                use_itn=True,
                batch_size_s=60,
            )
        except TypeError:
            res = model.generate(input=samples, cache={}, language=language, use_itn=True)
        except Exception:
            res = model.generate(input=samples)
        text = ""
        if res:
            raw = res[0].get("text", "")
            try:
                text = postprocess(raw)
            except Exception:
                text = raw
        text = re.sub(r"<\|[^|]+\|>", "", text).strip()
        if text:
            end_label = "..." if end >= 1e8 else fmt(end)
            print(f"[{fmt(start)} - {end_label}] {text}", flush=True)


def main() -> None:
    global _STDIN_BYTES
    parser = argparse.ArgumentParser(
        description="Local speech transcription (FunASR)")
    parser.add_argument("media", help="video or audio file path / URL")
    parser.add_argument("--lang", default="auto", choices=list(config.SPEECH_LANGS))
    parser.add_argument("--model", default="sensevoice", choices=["sensevoice", "paraformer"])
    args = parser.parse_args()
    if args.media == "-":
        try:
            _STDIN_BYTES = media.read_stdin()
        except RuntimeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
    elif args.media.startswith(("http://", "https://")):
        # ffmpeg reads the URL directly (headers from VISION_FFMPEG_HEADERS),
        # but the URL is still untrusted input: apply the same SSRF guard as
        # the main vision path before handing it to ffmpeg.
        try:
            media.ensure_url_safe(args.media)
        except RuntimeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
    elif not Path(args.media).exists():
        print(f"Error: file not found: {args.media}", file=sys.stderr)
        sys.exit(1)
    try:
        transcribe(args.media, args.lang, args.model)
    except Exception as e:
        print(f"Transcription failed: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
