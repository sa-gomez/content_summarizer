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


Adding a new kind of link: see `sources.py` and `AGENTS.md`. Planned work (more
source types, benchmarks, etc.) is tracked in
[GitHub Issues](https://github.com/sa-gomez/content_summarizer/issues).

## Storing, summarizing and searching (Postgres)

Transcripts can be stored in Postgres (pgvector) with LLM summaries and hybrid
keyword + semantic search. Raw text is immutable; summaries/chunks/embeddings are derived.

    docker compose up -d db worker          # Postgres + job worker (scale: --scale worker=3)
    export ANTHROPIC_API_KEY=...            # workers use Claude for summaries
    uv run transcribe.py URL --store        # also writes .md; add --no-md to skip it
    docker compose run --rm app python search.py "query" [--kind summary|transcript|short|medium|long] [--mode hybrid|keyword|semantic]

Schema: `db/migrations/001_init.sql` (documents, segments, summaries -> document_id,
chunks, jobs). `--store` reads `DATABASE_URL` (default: the compose db on localhost:5432);
point it at any managed Postgres with the pgvector extension to host it.
Set `SUMMARIZER=stub` / `EMBEDDER=stub` for offline plumbing tests.
Fixture test: `docker compose run --rm app python tests/ingest_fixture.py`, then `python worker.py --once`.

Full design notes, including how embeddings and hybrid search work: [docs/storage-and-search.md](docs/storage-and-search.md).

