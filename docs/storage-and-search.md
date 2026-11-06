# Storage, summaries and search

How transcripts are stored, summarized and searched. Code lives in `store.py`,
`worker.py`, `search.py`; schema in `db/migrations/001_init.sql`.

## Design principle

**Raw text is the irreplaceable data; everything else is derived.** Whisper runs
are expensive and YouTube captions can vanish, so `documents.text` is written once
and never updated. Summaries, chunks and embeddings can always be regenerated
(new prompt, new model, new chunking) without touching the source.

## Architecture

```
transcribe.py --store ──► documents (+ segments)
                              │  enqueue jobs (idempotent via dedupe_key)
                              ▼
                            jobs  ◄── worker.py (N copies, FOR UPDATE SKIP LOCKED)
                              │         ├─ summarize: Claude ──► summaries ──► enqueues embed
                              │         └─ embed: chunk + vectorize ──► chunks
                              ▼
                           chunks ◄── search.py (keyword + vector, fused)
```

| Table | Role | Mutable? |
|---|---|---|
| `documents` | Raw transcript + metadata (`source_type`, `external_id` unique) | No |
| `segments` | `(start_s, text)` pairs for timestamped deep links | No |
| `summaries` | LLM output; `document_id` FK is the pointer to the source; unique per `(document_id, kind, model, prompt_version)` | Regenerate by bumping `PROMPT_VERSION` |
| `chunks` | Search units: slices of a transcript (`summary_id IS NULL`) or of a summary; hold `embedding` and `tsv` | Rebuildable |
| `jobs` | Postgres work queue | Yes |

### Job queue
Workers claim with `UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1)`,
so many workers never take the same job. A `running` job whose lock is older than 10
minutes is treated as orphaned and reclaimed. Failures retry with exponential backoff
(30s, 60s, 120s...) up to 5 attempts, then `failed` with the traceback in `jobs.error`.
`dedupe_key` makes re-ingesting a video a no-op.

### Summaries
Kinds: `short` (~50 words, Haiku), `medium` (~200) and `long` (~600) (Sonnet).
Transcripts over 60k characters are summarized map-reduce style (section summaries,
then a summary of those). Each summary row records `model`, `prompt_version` and
`metadata.source_document_id`.

## Vector embeddings

### The idea
An embedding model turns text into a list of numbers (here **384 floats**) so that
**texts with similar meaning land close together** in that 384-dimensional space.
"how to make loaves of bread" and "bake in a hot dutch oven, fold the dough" share
almost no words, but their vectors point in similar directions. Keyword search can't
see that; vector search can.

You never interpret individual numbers. Only distances and angles between vectors
mean anything.

### Pipeline in this project
1. **Chunk** (`worker.chunk_text`): split text into ~1500-character pieces with 200
   characters of overlap, breaking on whitespace. Why: a model compresses a whole
   input into one vector, so a 2-hour transcript would blur into mush. Small chunks
   give each vector one focused topic, and a hit points to a specific passage. Overlap
   keeps a sentence cut at a boundary retrievable from either side.
2. **Embed** (`worker.embed`): `fastembed` runs `BAAI/bge-small-en-v1.5` locally (ONNX,
   no API key, CPU-friendly) and returns one 384-dim vector per chunk.
3. **Store**: `chunks.embedding vector(384)` (pgvector). The dimension is fixed in the
   schema, so changing models means a new column or table plus re-embedding every chunk.
   This is why chunks are derived and disposable.
4. **Index**: `HNSW (embedding vector_cosine_ops)`. Exact nearest-neighbour search compares
   the query to every row. HNSW is a layered graph that finds *approximately* the nearest
   rows in roughly logarithmic time, trading a little recall for large speedups.
5. **Query** (`search.py`): embed the query with the same model, then order by
   `embedding <=> query` (cosine *distance*; smaller = more similar).

### Cosine similarity, intuitively
Cosine compares the *direction* of two vectors and ignores length: 1.0 means the same
direction (similar meaning), about 0 unrelated, negative opposed. pgvector's `<=>` returns
`1 - cosine_similarity`, hence "ORDER BY ... ASC".

### Hybrid search and rank fusion
Keyword and vector search fail in opposite ways:

| | Good at | Bad at |
|---|---|---|
| Keyword (`tsvector`, `ts_rank`) | exact names, rare terms, quotes, IDs | paraphrase, synonyms |
| Vector | meaning, paraphrase | exact strings, rare proper nouns |

`search.py` runs both, takes each top 50, and merges them with **reciprocal rank fusion**:
`score = sum(1 / (60 + rank))` across the lists a chunk appears in. It uses only ranks, so
no need to normalize incomparable scores (`ts_rank` vs. cosine). Chunks high in *both* lists
win; the constant 60 damps the gap between rank 1 and rank 5.

Reading scores: a chunk found by only one list tops out at about `1/61 = 0.0164`; one ranked
first by both scores about `0.0328`.

### Known limitations and tuning ideas
- **Keyword leg is strict.** `websearch_to_tsquery` ANDs terms and stems them, so
  "parallel agents git" finds no keyword match when "git" never appears, and only the vector
  leg contributes (visible as a `0.0164` top score). Options: OR the terms, or fall back to
  `plainto_tsquery`/trigram matching.
- **BGE query prefix.** BGE models can be given an instruction prefix on *queries only* for
  retrieval ("Represent this sentence for searching relevant passages: "). Not used here;
  `fastembed`'s `query_embed` supports it. Worth A/B testing.
- **Chunk size** (1500/200 chars) is a guess. Smaller is more precise, larger keeps context.
  Tune against real queries.
- **English only**: the model and the `'english'` text-search config are English.
- **Model changes**: any change requires re-embedding every chunk.
- **Approximate search**: HNSW can miss true neighbours; raise `hnsw.ef_search` if recall
  matters.

## Operating
See `README.md` for commands. Environment: `DATABASE_URL`, `ANTHROPIC_API_KEY`,
`SUMMARIZER=stub|claude`, `EMBEDDER=stub|fastembed`. The `stub` embedder is a hash bag of
words (plumbing tests only, not semantic).

### Moving to a host
Anything that provides Postgres with the `pgvector` extension works (RDS, Cloud SQL, Neon,
Supabase...). Apply `db/migrations/001_init.sql`, set `DATABASE_URL`, and run the worker as a
container. Scale workers horizontally; the queue is safe for concurrent consumers.

### Outgrowing Postgres
If search quality or scale demands it, add Elasticsearch/OpenSearch as a *derived* index fed
from `chunks`; Postgres stays the source of truth.
