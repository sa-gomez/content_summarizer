#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["yt-dlp[default]", "faster-whisper", "av<17", "deno", "psycopg[binary]>=3.2"]
# ///
"""YouTube -> transcript.

Tries YouTube captions first; falls back to local Whisper (faster-whisper).

Usage:
  uv run transcribe.py URL [URL ...] [--lang en] [--model small]
                       [--force-whisper] [--timestamps] [--out transcripts]
"""
import argparse
import json
import re
import sys
import tempfile
from pathlib import Path

import yt_dlp


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
    from faster_whisper import WhisperModel

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
    print(f"  transcribing with Whisper '{model_name}' (first run downloads the model)...", file=sys.stderr)
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    seg_iter, _ = model.transcribe(audio, language=lang, vad_filter=True)
    return info, [(s.start, s.text.strip()) for s in seg_iter if s.text.strip()]


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
    ap.add_argument("--model", default="small", help="Whisper model: tiny/base/small/medium/large-v3/turbo")
    ap.add_argument("--force-whisper", action="store_true", help="skip captions, always use Whisper")
    ap.add_argument("--timestamps", action="store_true")
    ap.add_argument("--out", default="transcripts")
    ap.add_argument("--store", action="store_true",
                    help="save to Postgres (DATABASE_URL) and enqueue summarize/embed jobs")
    ap.add_argument("--no-md", action="store_true", help="with --store: skip writing the .md file")
    args = ap.parse_args()

    out_dir = Path(args.out)
    if not (args.store and args.no_md):
        out_dir.mkdir(parents=True, exist_ok=True)
    conn = None
    if args.store:
        import store  # lazy: only needed (and DB only touched) with --store

        conn = store.connect()

    for url in args.urls:
        print(f"==> {url}", file=sys.stderr)
        try:
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
                    info, segs = whisper_transcribe(url, args.lang, args.model, tmp)
                    source = f"whisper-{args.model}"
            segs = dedupe(segs)
            if not (args.store and args.no_md):
                path = out_dir / f"{slugify(info['title'])}_{info['id']}.md"
                header = f"# {info['title']}\n\n- URL: {info.get('webpage_url', url)}\n- Channel: {info.get('uploader', '?')}\n- Source: {source}\n\n"
                path.write_text(header + to_text(segs, args.timestamps) + "\n")
                print(f"  saved {path} ({source})", file=sys.stderr)
            if conn:
                doc_id, created = store.upsert_document(
                    conn,
                    source_type="youtube",
                    external_id=info["id"],
                    text=to_text(segs, False),  # plain prose; timestamps live in segments
                    segments=segs,
                    source_url=info.get("webpage_url", url),
                    title=info.get("title"),
                    author=info.get("uploader"),
                    published_at=store.parse_upload_date(info.get("upload_date")),
                    language=args.lang,
                    duration_s=info.get("duration"),
                    transcript_method=source,
                    metadata={"channel_id": info.get("channel_id"), "view_count": info.get("view_count")},
                )
                print(f"  stored doc#{doc_id} ({'new' if created else 'already present'})", file=sys.stderr)
        except Exception as e:  # keep going on batch runs
            print(f"  FAILED: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
