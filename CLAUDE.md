# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Stage 2 (DELTA) of the six-stage ingestion skeleton — `../ARCHITECTURE.md` block 11 is authoritative; read it before changing the diff semantics. Standalone stage module: `settings.py`, `sources.py`, `db.py` are **deliberately duplicated** from `rag_discover` (standalone-blocks convention, dedup refactor deferred) — fix bugs in both places.

## Commands

```bash
uv sync --frozen --extra dev                 # lock-exact venv
.venv/bin/python -m pytest                   # offline tests (pure diff logic, no DB)
python -m rag_delta --run-id <id>            # diff a discover run (needs ../postgres up)
python -m rag_delta --run-id <id> --mark-ingested   # dev stand-in for stage 5
```

## Invariants

- **Library-first, thin `__main__`** (project-wide convention): stage logic in `rag_delta.delta.run_delta()`; `__main__.py` is a logic-free argparse wrapper.
- **Delta READS `ingest_docs`, never writes it in the diff path.** Ledger writes happen only on completed ingestion (stage 5 later). `mark_ingested()` is a documented temporary stand-in — it moves to the embed stage when that exists; don't grow features onto it.
- **Never under-trigger**: `signals_changed()` returns True when no signal is comparable. Weakening this breaks the block-4 layer-1 guarantee (unchanged docs may be skipped forever).
- **Deletion evidence rules**: a source yields deletions only if config says `detect_deletions: true` (the default) AND it produced ≥1 manifest row this run. Rolling-window sources (RSS) are permanently excluded via `detect_deletions: false`. In whole-table mode (`run_id=None`), `latest_complete_listings` first scopes each `detect_deletions` source to its newest run (older runs would resurrect since-deleted docs); deletion rows are stamped with that evidencing run's id.
- **`unique_manifest_docs` collapses manifest rows to one per `(source_system, doc_url)`** — the document identity. Several rows describe one document when it was listed in several runs, or by several sources of one source_system in one run. Runs in **both** modes (it was whole-table-only as `latest_per_doc` until 2026-08-12). Ranked by `_sighting_order`: RSS by `(published_at, execution_ts)`, Confluence by `(version_number, execution_ts)`, everything else by `execution_ts`.
- **`compute_deletions` MUST run before `unique_manifest_docs` — don't reorder.** Eligibility comes from `source_id` on the manifest rows, and the collapse discards the losing row's `source_id`. Run it after, and a source that listed its documents but lost every tie looks identical to one that reported nothing: it drops out of `sources_seen_this_run`, and its deleted documents are silently never tombstoned — no error, no log, `deleted: 0` looks normal. (`listed` is unaffected — it keys on `(source_system, doc_url)`, which the collapse preserves.) No test catches a reorder while every source is alone in its source_system.
- **Empty run raises ValueError** (typo'd run_id ≠ "nothing changed"). Counts-only outputs (XCom contract).
- Schema: `../postgres/sql/002_delta.sql` is authoritative for `ingest_docs` / `ingest_worklist` / `ingest_deletions`; `db.py` mirrors it — change both together.
