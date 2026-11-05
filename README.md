# YouTube transcripts

    uv run transcribe.py URL [URL ...]            # captions first, Whisper fallback
    uv run transcribe.py URL --force-whisper --model small
    uv run transcribe.py URL --timestamps --lang en --out transcripts

Output: one Markdown file per video in `transcripts/`.
Models: tiny/base/small/medium/large-v3/turbo (bigger = slower, more accurate).

Dependencies are declared at the top of `transcribe.py` (PEP 723); `uv run`
builds a cached environment from them on first use, so there is no venv to
create or activate. Install uv: https://docs.astral.sh/uv/
