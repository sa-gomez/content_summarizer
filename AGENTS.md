# AGENTS.md

Guidance for AI coding agents (and humans) working in this repo.

## Where the work is tracked

**GitHub Issues is the source of truth for tasks:**
https://github.com/sa-gomez/content_summarizer/issues

Check open issues before starting work, and file new work there rather than in TODO 
comments or this file. Reference the issue number in branch names and PR descriptions
(e.g. `Closes #3`).

If you finish a piece of work and spot follow-ups, open
an issue for each one and say so in the PR.

## What this project is

Ingests transcripts (YouTube today; podcasts and others planned), stores the raw text in
Postgres with metadata, generates LLM summaries of several lengths via background workers,
and supports hybrid keyword + semantic search. GitHub: https://github.com/sa-gomez/content_summarizer

Designs in docs/ directory


## Layout

| Path | Purpose |
|---|---|
| `transcribe.py` | CLI: URL(s) -> Markdown transcript in `transcripts/`. Runs via `uv` (PEP 723 inline deps, no venv). |
| `sources.py` | Pluggable non-YouTube sources (`@register`ed classes with `matches` / `resolve`). Apple Podcasts today. |
| `transcripts/` | Output, one Markdown file per link |
| `store.py` | Postgres helpers: `connect`, `upsert_document`, `enqueue_job` |
| `worker.py` | Job worker (`summarize`, `embed`) using `FOR UPDATE SKIP LOCKED` |
| `search.py` | Hybrid search CLI (keyword + pgvector, reciprocal rank fusion) |
| `db/migrations/001_init.sql` | Schema; mounted into the Postgres container's init dir |
| `docker-compose.yml`, `Dockerfile` | `db` (pgvector), `worker`, and `app` (one-off CLI, profile `tools`) |
| `tests/ingest_fixture.py` | Fixture ingest used for manual end-to-end checks |

## Running things

```
uv run transcribe.py URL [URL ...]       # YouTube or Apple Podcasts link
uv run transcribe.py URL --model small   # override the Whisper model
uv run transcribe.py URL1 URL2 --caption-delay 5   # space out caption requests
uv run --with pytest --with "yt-dlp[default]" --with faster-whisper --with "av<17" pytest tests

docker compose up -d db worker                      # Postgres + worker
uv run transcribe.py URL --store                    # ingest (needs uv on the host)
docker compose run --rm app python search.py "q"    # search
docker compose run --rm app python worker.py --once # drain the queue and exit
docker compose down -v
```

Env vars: `DATABASE_URL`, `ANTHROPIC_API_KEY`, `SUMMARIZER=stub|claude`,
`EMBEDDER=stub|fastembed`. Use the stubs for offline plumbing tests; the stub embedder is
not semantic.


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

## Invariants (do not break)

- **`documents.text` is immutable.** Raw transcripts are the irreplaceable data. Anything
  else (summaries, chunks, embeddings) must stay regenerable from it.
- **Every summary points to its source** via `summaries.document_id` (and
  `metadata.source_document_id`).
- **Jobs must be idempotent.** Enqueue with a `dedupe_key`; handlers may run more than once
  (stale-lock reclaim, retries).
- **The embedding dimension (384) is fixed in the schema.** Changing the embedding model means
  a new column/table and re-embedding all chunks.
- Schema changes go in a new numbered file under `db/migrations/`. The init directory only
  runs on a fresh database, so existing databases need the file applied by hand until a
  migration runner exists.

## Conventions

- Keep `transcribe.py` importing `store` lazily so it works without a database.
- Commit messages: short imperative summary, then a body explaining why.
- Verify changes end to end where you can (ingest -> worker -> search) and state plainly what
  was not tested.

