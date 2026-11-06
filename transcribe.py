#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["yt-dlp[default]", "faster-whisper", "av<17", "deno"]
# ///
"""YouTube / podcast link -> transcript.

YouTube: tries captions first; falls back to local Whisper (faster-whisper).
Podcasts (see sources.py): downloads the audio and runs Whisper.

Usage:
  uv run transcribe.py URL [URL ...] [--lang en] [--model NAME]
                       [--force-whisper] [--timestamps] [--out transcripts]

Default Whisper model: 'small' for YouTube, 'base' for podcasts (faster; long audio).
"""
import argparse
import json
import re
import shutil
import sys
import tempfile
import urllib.request
from pathlib import Path

import yt_dlp

import sources

DEFAULT_MODEL_YOUTUBE = "small"
DEFAULT_MODEL_PODCAST = "base"


def slugify(s):
    return re.sub(r"[^\w\-]+", "_", s).strip("_")[:80] or "video"


def fmt_ts(sec):
    sec = int(sec)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


def get_captions(url, lang, tmp):
    """Return (info, segments|None). segments = [(start_sec, text)]."""
    opts = {
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": [lang, f"{lang}-orig"],
        "subtitlesformat": "json3",
        "outtmpl": str(tmp / "%(id)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    files = sorted(tmp.glob(f"{info['id']}*.json3"))
    if not files:
        return info, None
    # prefer manually uploaded captions over auto-generated ones
    manual = set((info.get("subtitles") or {}).keys())
    files.sort(key=lambda f: not any(f.name.endswith(f".{k}.json3") for k in manual))
    data = json.loads(files[0].read_text())
    segs = []
    for ev in data.get("events", []):
        text = "".join(s.get("utf8", "") for s in ev.get("segs", []) or []).replace("\n", " ").strip()
        if text:
            segs.append((ev.get("tStartMs", 0) / 1000, text))
    return info, segs or None


def whisper_transcribe(url, lang, model_name, tmp):
    opts = {
        "format": "bestaudio[ext=m4a]/bestaudio/18/worst",
        "outtmpl": str(tmp / "%(id)s.%(ext)s"),
        "noprogress": True,
        "quiet": True,
        "no_warnings": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    audio = info["requested_downloads"][0]["filepath"]
    return info, whisper_file(audio, lang, model_name)


def whisper_file(audio, lang, model_name):
    """Transcribe a local audio file -> [(start_sec, text)]."""
    from faster_whisper import WhisperModel

    print(f"  transcribing with Whisper '{model_name}' (first run downloads the model)...", file=sys.stderr)
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    seg_iter, _ = model.transcribe(audio, language=lang, vad_filter=True)
    return [(s.start, s.text.strip()) for s in seg_iter if s.text.strip()]


def download_audio(audio_url, tmp):
    """Stream a direct audio URL to a file in tmp; return its path."""
    ext = Path(audio_url.split("?")[0]).suffix or ".mp3"
    dest = tmp / f"episode{ext}"
    req = urllib.request.Request(audio_url, headers={"User-Agent": sources.USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)
    return dest


def to_text(segs, timestamps):
    if timestamps:
        return "\n".join(f"[{fmt_ts(t)}] {txt}" for t, txt in segs)
    # plain prose: join, then break into paragraphs every ~6 caption chunks
    out, buf = [], []
    for _, txt in segs:
        buf.append(txt)
        if (len(buf) >= 6 and re.search(r"[.?!]$", txt)) or len(buf) >= 14:
            out.append(" ".join(buf))
            buf = []
    if buf:
        out.append(" ".join(buf))
    return "\n\n".join(out)


def dedupe(segs):
    """Auto-captions often repeat the previous line; drop exact repeats."""
    out = []
    for t, txt in segs:
        if not out or out[-1][1] != txt:
            out.append((t, txt))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("urls", nargs="+")
    ap.add_argument("--lang", default="en")
    ap.add_argument(
        "--model",
        default=None,
        help=f"Whisper model: tiny/base/small/medium/large-v3/turbo "
        f"(default: {DEFAULT_MODEL_YOUTUBE} for YouTube, {DEFAULT_MODEL_PODCAST} for podcasts)",
    )
    ap.add_argument("--force-whisper", action="store_true", help="skip captions, always use Whisper")
    ap.add_argument("--timestamps", action="store_true")
    ap.add_argument("--out", default="transcripts")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    for url in args.urls:
        print(f"==> {url}", file=sys.stderr)
        try:
            ep = sources.resolve(url)
            if ep is not None:  # podcast-style source: audio file -> Whisper
                model_name = args.model or DEFAULT_MODEL_PODCAST
                print(f"  {ep.show}: {ep.title}", file=sys.stderr)
                with tempfile.TemporaryDirectory() as t:
                    print("  downloading audio...", file=sys.stderr)
                    audio = download_audio(ep.audio_url, Path(t))
                    segs = whisper_file(str(audio), args.lang, model_name)
                source = f"whisper-{model_name}"
                path = out_dir / f"{slugify(ep.title)}_{ep.id}.md"
                header = (
                    f"# {ep.title}\n\n- URL: {ep.page_url}\n- Show: {ep.show}\n"
                    f"- Date: {ep.date or '?'}\n- Source: {source}\n\n"
                )
                path.write_text(header + to_text(segs, args.timestamps) + "\n")
                print(f"  saved {path} ({source})", file=sys.stderr)
                continue

            model_name = args.model or DEFAULT_MODEL_YOUTUBE
            with tempfile.TemporaryDirectory() as t:
                tmp = Path(t)
                info, segs, source = None, None, None
                if not args.force_whisper:
                    try:
                        info, segs = get_captions(url, args.lang, tmp)
                    except Exception as e:  # e.g. HTTP 429 on the caption endpoint
                        print(f"  caption fetch failed ({e})", file=sys.stderr)
                        segs = None
                    source = "youtube-captions"
                    if segs is None:
                        print("  no captions found, falling back to Whisper", file=sys.stderr)
                if segs is None:
                    info, segs = whisper_transcribe(url, args.lang, model_name, tmp)
                    source = f"whisper-{model_name}"
            segs = dedupe(segs)
            path = out_dir / f"{slugify(info['title'])}_{info['id']}.md"
            header = f"# {info['title']}\n\n- URL: {info.get('webpage_url', url)}\n- Channel: {info.get('uploader', '?')}\n- Source: {source}\n\n"
            path.write_text(header + to_text(segs, args.timestamps) + "\n")
            print(f"  saved {path} ({source})", file=sys.stderr)
        except Exception as e:  # keep going on batch runs
            print(f"  FAILED: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
