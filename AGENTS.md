# AGENTS.md

Guidance for AI coding agents (and humans) working in this repo.

## Where the work is tracked

**GitHub Issues is the source of truth for tasks:**
https://github.com/sa-gomez/content_summarizer/issues

Check open issues before starting work, and file new work there rather than in TODO
comments, TODO files, or this file. Reference the issue number in branch names and PR
descriptions (e.g. `Closes #3`). If you finish a piece of work and spot follow-ups, open
an issue for each one and say so in the PR.

## What this project is

Turns links into transcripts: YouTube videos (captions first, Whisper fallback) and podcasts
(audio -> Whisper). GitHub: https://github.com/sa-gomez/content_summarizer

## Layout

| Path | Purpose |
|---|---|
| `transcribe.py` | CLI: URL(s) -> Markdown transcript in `transcripts/`. Runs via `uv` (PEP 723 inline deps, no venv). |
| `sources.py` | Pluggable non-YouTube sources (`@register`ed classes with `matches` / `resolve`). Apple Podcasts today. |
| `transcripts/` | Output, one Markdown file per link |

## Running things

```
uv run transcribe.py URL [URL ...]       # YouTube or Apple Podcasts link
uv run transcribe.py URL --model small   # override the Whisper model
uv run transcribe.py URL1 URL2 --caption-delay 5   # space out caption requests
uv run --with pytest --with "yt-dlp[default]" --with faster-whisper --with "av<17" pytest tests
```

Needs `uv` (https://docs.astral.sh/uv/). There is no venv to create; dependencies are the
`# /// script` block at the top of `transcribe.py`.

## How a link is handled

1. `sources.resolve(url)` asks each registered source whether it claims the URL.
2. Claimed (e.g. Apple Podcasts): the source returns a `Resolved` (audio URL + metadata);
   the audio is downloaded and transcribed with Whisper (default model `base`).
3. Not claimed: treated as YouTube. Captions first; on a missing or failed caption download
   (e.g. HTTP 429) fall back to Whisper (default model `small`).

To add a source type, add a class to `sources.py` with `matches(url)` and `resolve(url)`,
decorate it with `@register`, and raise `SourceError` for recognised links that can't be
resolved. Check open issues first; several source types are already filed.

## Gotchas

- `av` is pinned `<17`: `faster-whisper` 1.2.1 fails with newer PyAV
  (`open() got an unexpected keyword argument 'metadata_errors'`).
- YouTube rate-limits caption downloads per video (HTTP 429). `get_captions_with_retry()` retries 429/5xx
  (up to 4 retries, exponential backoff + jitter, honours `Retry-After`, 60s total cap) before falling back
  to Whisper; non-transient errors don't retry. Multi-URL runs wait `--caption-delay` seconds (default 3,
  plus jitter, growing after a 429) between caption requests. yt-dlp only *warns* on a failed subtitle
  download, so `get_captions()` captures warnings via a logger to detect it. Tests live in `tests/`.
- Spotify audio is DRM-protected and can't be downloaded.
- `.claude/worktrees/` is gitignored; don't commit it.

## Conventions

- Commit messages: short imperative summary, then a body explaining why.
- Verify changes end to end where you can (run the CLI on a real link) and state plainly
  what was not tested.
