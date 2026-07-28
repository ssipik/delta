from __future__ import annotations

from typing import Iterable, Sequence

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.engine import Engine

from rag_delta.settings import get_settings

metadata = MetaData()

# Mirrors ../postgres/sql/001_discover.sql (authoritative for the schema).
# Duplicated from rag_discover.db by the standalone-stage convention:
# this is the read side of the discover → delta contract.
ingest_manifest = Table(
    "ingest_manifest",
    metadata,
    # GENERATED ALWAYS AS IDENTITY in the DDL: never supplied on insert.
    Column("manifest_id", BigInteger, primary_key=True),
    Column("run_id", Text, nullable=False),
    Column("source_system", Text, nullable=False),
    Column("doc_url", Text, nullable=False),
    Column("source_id", Text, nullable=False),
    Column("title", Text),
    Column("published_at", DateTime(timezone=True)),
    Column("doc_version", Text),
    Column("size_bytes", BigInteger),
    Column("extra", JSONB, nullable=False),
    Column("execution_ts", DateTime(timezone=True), nullable=False),
    # the natural key; discover upserts on it so a re-run of one run updates
    # its sighting instead of minting a second manifest_id
    UniqueConstraint("run_id", "source_system", "doc_url"),
)

# Mirrors ../postgres/sql/002_delta.sql — the ledger of successfully
# ingested documents. Delta only READS it; rows are written when ingestion
# completes (the embed stage later; mark_ingested() as dev stand-in until then).
ingest_docs = Table(
    "ingest_docs",
    metadata,
    # the sighting whose points are live in Qdrant
    Column("manifest_id", BigInteger, primary_key=True),
    Column("text_sha", Text),
    Column("parser_version", Text),
    Column("status", Text, nullable=False),
)

ingest_worklist = Table(
    "ingest_worklist",
    metadata,
    Column("manifest_id", BigInteger, primary_key=True),
    Column("run_id", Text, nullable=False),
    Column("reason", Text, nullable=False),
    # copied from the sighting; plan orders on it to find the newest run
    Column("execution_ts", DateTime(timezone=True), nullable=False),
)

ingest_deletions = Table(
    "ingest_deletions",
    metadata,
    Column("manifest_id", BigInteger, primary_key=True),
    # the run that DETECTED the deletion, not the sighting's run
    Column("run_id", Text, nullable=False),
)


def get_engine(dsn: str | None = None) -> Engine:
    return create_engine(dsn or get_settings().pg_dsn)


def upsert_rows(
    engine: Engine,
    table: Table,
    rows: Sequence[dict],
    key: Iterable[str],
) -> int:
    """Idempotent batch upsert (INSERT … ON CONFLICT DO UPDATE on `key`)."""
    key = list(key)
    deduped = list({tuple(r[k] for k in key): r for r in rows}.values())
    if not deduped:
        return 0
    stmt = insert(table)
    stmt = stmt.on_conflict_do_update(
        index_elements=key,
        # only columns actually supplied: an identity column (manifest_id,
        # batch_id) is absent from the rows and must not be overwritten
        set_={
            c.name: stmt.excluded[c.name]
            for c in table.columns
            if c.name not in key and c.name in deduped[0]
        },
    )
    with engine.begin() as conn:
        conn.execute(stmt, deduped)
    return len(deduped)
