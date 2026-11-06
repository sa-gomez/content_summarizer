# YouTube & podcast transcripts

    uv run transcribe.py URL [URL ...]            # YouTube: captions first, Whisper fallback
    uv run transcribe.py URL --force-whisper --model small
    uv run transcribe.py URL --timestamps --lang en --out transcripts

Supported links:

| Link | How |
|---|---|
| YouTube video | YouTube captions; falls back to Whisper if missing or rate-limited |
| Apple Podcasts episode (`podcasts.apple.com/.../id123?i=456`) | Audio downloaded, transcribed with Whisper |
| Apple Podcasts show (no `?i=`) | Latest episode |

Output: one Markdown file per link in `transcripts/`.

Models: tiny/base/small/medium/large-v3/turbo (bigger = slower, more accurate).
Defaults: `small` for YouTube, `base` for podcasts (episodes are long). Override
with `--model`, e.g. `--model small` for better accuracy on a podcast.

Dependencies are declared at the top of `transcribe.py` (PEP 723); `uv run`
builds a cached environment from them on first use, so there is no venv to
create or activate. Install uv: https://docs.astral.sh/uv/

Adding a new kind of link: see `sources.py` (and `TODO.md` for planned ones).
