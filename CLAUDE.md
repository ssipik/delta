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
- **Empty run raises ValueError** (typo'd run_id ≠ "nothing changed"). Counts-only outputs (XCom contract).
- Schema: `../postgres/sql/002_delta.sql` is authoritative for `ingest_docs` / `ingest_worklist` / `ingest_deletions`; `db.py` mirrors it — change both together.
