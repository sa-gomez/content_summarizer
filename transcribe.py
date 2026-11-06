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
import random
import re
import shutil
import sys
import tempfile
import time
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


# Caption retry budget (issue #24). Delays: 2s, 4s, 8s, 16s (+ jitter), capped in total.
CAPTION_RETRIES = 4
CAPTION_BACKOFF_BASE = 2.0
CAPTION_MAX_TOTAL_WAIT = 60.0
DEFAULT_CAPTION_DELAY = 3.0

_TRANSIENT_RE = re.compile(r"HTTP Error (429|5\d\d)|Too Many Requests", re.I)


class CaptionTransientError(Exception):
    """A caption download failed in a way worth retrying (HTTP 429 / 5xx)."""

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


def _is_429(exc):
    return bool(re.search(r"429|Too Many Requests", str(exc), re.I))


def classify_error(exc):
    """Return (transient, retry_after_seconds|None) for an exception from yt-dlp."""
    if isinstance(exc, CaptionTransientError):
        return True, exc.retry_after
    # Walk the chain: yt-dlp wraps urllib/HTTP errors in DownloadError.
    seen, cur = set(), exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        candidates = [cur]
        exc_info = getattr(cur, "exc_info", None)
        if exc_info and len(exc_info) > 1 and isinstance(exc_info[1], BaseException):
            candidates.append(exc_info[1])
        for c in candidates:
            status = getattr(c, "status", None) or getattr(c, "code", None)
            if isinstance(status, int) and (status == 429 or 500 <= status < 600):
                headers = getattr(c, "headers", None) or getattr(getattr(c, "response", None), "headers", None)
                ra = headers.get("Retry-After") if headers is not None and hasattr(headers, "get") else None
                try:
                    return True, float(ra) if ra is not None else None
                except (TypeError, ValueError):
                    return True, None
        cur = cur.__cause__ or cur.__context__
    if _TRANSIENT_RE.search(str(exc)):
        return True, None
    return False, None


class _WarningLog:
    """yt-dlp logger that remembers warnings. Failed subtitle downloads are only
    warnings in yt-dlp, so this is how we see an HTTP 429 on the caption endpoint."""

    def __init__(self):
        self.warnings = []

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        self.warnings.append(str(msg))

    def error(self, msg):
        self.warnings.append(str(msg))


class Throttle:
    """Minimum jittered delay between caption requests; grows after a 429."""

    def __init__(self, delay, sleep=time.sleep):
        self.delay, self.penalty, self.sleep = delay, 0.0, sleep
        self.started = False

    def wait(self):
        if self.started and (self.delay > 0 or self.penalty > 0):
            d = (self.delay + self.penalty) * random.uniform(1.0, 1.5)
            print(f"  waiting {d:.1f}s before caption request", file=sys.stderr)
            self.sleep(d)
        self.started = True

    def rate_limited(self):
        self.penalty = min(max(self.penalty * 2, self.delay or 2.0), 60.0)


def get_captions_with_retry(url, lang, tmp, throttle=None, fetch=None, sleep=time.sleep,
                            retries=CAPTION_RETRIES, max_total_wait=CAPTION_MAX_TOTAL_WAIT):
    """get_captions() with backoff on transient errors (429/5xx).

    Non-transient errors propagate immediately. If the retry budget (attempts or
    total wait) is spent, the last transient error is raised so the caller can
    fall back to Whisper.
    """
    fetch = fetch or get_captions
    if throttle:
        throttle.wait()
    waited = 0.0
    for attempt in range(retries + 1):
        try:
            return fetch(url, lang, tmp)
        except Exception as e:
            transient, retry_after = classify_error(e)
            if not transient:
                raise
            if throttle and _is_429(e):
                throttle.rate_limited()
            if attempt >= retries:
                raise
            delay = retry_after if retry_after is not None else CAPTION_BACKOFF_BASE * 2**attempt
            delay = delay * random.uniform(1.0, 1.25)
            if waited + delay > max_total_wait:
                raise
            status = "429" if _is_429(e) else "a transient error"
            print(f"  caption fetch got {status}, retrying in {delay:.0f}s ({attempt + 1}/{retries})", file=sys.stderr)
            sleep(delay)
            waited += delay


def get_captions(url, lang, tmp):
    """Return (info, segments|None). segments = [(start_sec, text)].

    Raises CaptionTransientError when no caption file was written because the
    download hit HTTP 429 / 5xx (yt-dlp only warns about that).
    """
    log = _WarningLog()
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
        "logger": log,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    files = sorted(tmp.glob(f"{info['id']}*.json3"))
    if not files:
        bad = [w for w in log.warnings if _TRANSIENT_RE.search(w)]
        if bad:
            raise CaptionTransientError(bad[-1])
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
    ap.add_argument(
        "--caption-delay",
        type=float,
        default=DEFAULT_CAPTION_DELAY,
        metavar="SECONDS",
        help="minimum delay (plus jitter) between caption requests on multi-URL runs "
        f"(default: {DEFAULT_CAPTION_DELAY:g}; grows after a 429)",
    )
    ap.add_argument("--out", default="transcripts")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    throttle = Throttle(args.caption_delay)

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
                        info, segs = get_captions_with_retry(url, args.lang, tmp, throttle)
                    except Exception as e:  # e.g. HTTP 429 on the caption endpoint
                        print(f"  caption fetch failed ({e}), falling back to Whisper", file=sys.stderr)
                        segs = None
                    source = "youtube-captions"
                    if segs is None and info is not None:
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
