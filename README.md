# rag-delta

Stage 2 (DELTA) of the RAG ingestion pipeline — see [`../ARCHITECTURE.md`](../ARCHITECTURE.md) (block 11). Diffs a discovery run's `ingest_manifest` against the `ingest_docs` ledger (block 4, layer-1 pre-parse change detection) and writes:

- **`ingest_worklist`** — documents needing (re-)ingestion, with a reason: `new`, `changed` (any cheap signal differs: version hint, ETag, last-modified, size), or `reappeared` (was deleted, listed again).
- **`ingest_deletions`** — active ledger docs no longer listed by their source. Only for sources with `detect_deletions: true` (the default) that actually produced rows this run; rolling-window feeds (`false`, e.g. ECB RSS) never yield deletions.

Returns counts only (the stage contract); `worklist=0 deletions=0` is the DAG's short-circuit signal. Standalone module by convention: infrastructure (`settings.py`, `sources.py`, `db.py`) is duplicated from `rag_discover`, dedup deferred.

## Usage

```bash
uv sync --frozen --extra dev        # lock-exact venv from uv.lock
.venv/bin/python -m pytest          # offline unit tests (pure diff logic)

# Diff a discovery run (Postgres from ../postgres must be up)
python -m rag_delta --run-id <id>

# Dev stand-in for stage 5: mark the run's worklist as ingested into the ledger
python -m rag_delta --run-id <id> --mark-ingested

# Containerized
docker compose run --rm --build pipeline python -m rag_delta --run-id <id>
```

Typical dev cycle: `rag_discover --run-id X` → `rag_delta --run-id X` (worklist counts) → `--mark-ingested` → next discover+delta run reports `unchanged`.

## Design notes

- **Delta only reads `ingest_docs`** — ledger rows are written when ingestion actually completes (stage 5 later; `mark_ingested()` is the temporary stand-in). Crash-safety: a document keeps reappearing on the worklist until its content has truly landed.
- **Layer-1 semantics**: signals are compared only where both sides have them; if *no* signal is comparable the document counts as changed — layer 1 may over-trigger (a wasted parse) but never under-triggers. Layer 2 (`text_sha` post-parse) catches false positives before embedding.
- **Empty run fails loudly**: `run_delta` raises on a run with zero manifest rows — a typo'd `--run-id` must not masquerade as "nothing changed".
- Re-runs are idempotent: delta reads a fixed manifest, and worklist/deletions upsert on `manifest_id` (delta fully owns both tables — rows it no longer computes are deleted, not left behind).
