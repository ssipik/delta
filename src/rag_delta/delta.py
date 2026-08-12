from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, insert, select, update
from sqlalchemy.engine import Engine

from rag_delta.db import (
    get_engine,
    ingest_deletions,
    ingest_docs,
    ingest_manifest,
    ingest_worklist,
    upsert_rows,
)
from rag_delta.sources import SourceConfig, load_sources

logger = logging.getLogger(__name__)

# Metadata fields the pre-parsing check compares to detect change.
_METADATA_FIELDS = ("doc_version",)

_KEY = ("manifest_id",)


@dataclass
class DeltaReport:
    run_id: str
    counts: dict[str, int] = field(
        default_factory=lambda: {
            "new": 0, "changed": 0, "reappeared": 0, "unchanged": 0, "deleted": 0
        }
    )

    @property
    def worklist_total(self) -> int:
        return sum(self.counts[r] for r in ("new", "changed", "reappeared"))

    @property
    def empty(self) -> bool:
        """True when nothing changed; the DAG uses this to skip the rest of the run."""
        return self.worklist_total == 0 and self.counts["deleted"] == 0


_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def _version_number(row: dict) -> int:
    """Confluence's doc_version as an int (it is the version number, bumped on
    every edit). 0 when missing or not a number, so such a row loses to any
    numbered one."""
    raw = row.get("doc_version")
    if raw is None:
        return 0
    try:
        return int(raw)
    except ValueError:
        return 0


def _sighting_order(row: dict, sources: dict[str, SourceConfig]) -> tuple:
    """Sort key for a document's manifest rows. Same shape everywhere
    (published_at, version number, execution_ts) so keys stay comparable:
    - RSS by (published_at, execution_ts)
    - Confluence by (version number, execution_ts)
    - every other source (and unknown ones) by execution_ts alone."""
    cfg = sources.get(row["source_id"])
    if cfg is not None:
        if cfg.type == "rss":
            return (row.get("published_at") or _EPOCH, 0, row["execution_ts"])
        elif cfg.type == "confluence":
            return (_EPOCH, _version_number(row), row["execution_ts"])
    return (_EPOCH, 0, row["execution_ts"])


def unique_manifest_docs(
    manifest_docs: list[dict], sources: dict[str, SourceConfig]
) -> list[dict]:
    """Select only unique manifest documents (source_system, doc_url), e.g.:
    - if the same doc, i.e., (doc_url, source_system) appears in multiple runs
    - if the same doc_url appears in multiple source_systems.
    Keep only the most recent sighting.
    """
    latest: dict[tuple[str, str], dict] = {}
    order: dict[tuple[str, str], tuple] = {}
    for row in manifest_docs:
        key = (row["source_system"], row["doc_url"])
        rank = _sighting_order(row, sources)
        if key not in latest or rank > order[key]:
            latest[key] = row
            order[key] = rank
    return list(latest.values())


def latest_complete_listings(
    manifest_docs: list[dict], sources: dict[str, SourceConfig]
) -> tuple[list[dict], dict[str, str]]:
    """Filter manifest documents down to the ones that describe what exists now.

    - detect_deletions == False (rolling-window sources, e.g. RSS feeds):
      keep all docs. No single run lists everything, so all runs together
      are the current set of documents.
    - detect_deletions == True: keep only the newest run's docs. That run
      lists everything the source has now, so a document missing from it
      was deleted at the source. Older runs would add those deleted
      documents back.

    Returns the selected rows, plus the run_id each detect_deletions
    source's deletion_docs are based on.
    """
    newest: dict[str, tuple[datetime, str]] = {}
    for doc in manifest_docs:
        cfg = sources.get(doc["source_id"])
        if cfg is None:
            continue
        if not cfg.detect_deletions:
            # detection of deletion_docs is not needed
            # all docs in the manifest are kept (see the second loop)
            continue
        else:
            # detection of deletion_docs is needed
            # only documents in the most recent run/execution date are considered
            current = newest.get(doc["source_id"])
            if current is None or doc["execution_ts"] > current[0]:
                # new/newer run found
                newest[doc["source_id"]] = (doc["execution_ts"], doc["run_id"])
            else:
                # older run, or same execution_ts (first doc seen wins ties)
                pass
    evidence_runs = {sid: run for sid, (_, run) in newest.items()}
    selected: list[dict] = []
    for doc in manifest_docs:
        cfg = sources.get(doc["source_id"])
        if cfg is None:
            continue
        if not cfg.detect_deletions:
            # detection of deletion_docs is not needed
            selected.append(doc)
        else:
            # detection of deletion_docs is needed
            if doc["run_id"] == evidence_runs[doc["source_id"]]:
                selected.append(doc)
    return selected, evidence_runs


def _ledger_query():
    """Ledger rows with their sighting's document attributes joined in.

    ingest_docs holds only manifest_id + state; source_system, doc_url,
    source_id and doc_version live on the sighting it points at. Joining here
    keeps the row shape the pure functions below expect.
    """
    return (
        select(
            ingest_docs.c.manifest_id,
            ingest_docs.c.status,
            ingest_docs.c.text_sha,
            ingest_docs.c.parser_version,
            ingest_manifest.c.source_system,
            ingest_manifest.c.doc_url,
            ingest_manifest.c.source_id,
            ingest_manifest.c.doc_version,
        )
        .select_from(ingest_docs)
        .join(
            ingest_manifest,
            ingest_docs.c.manifest_id == ingest_manifest.c.manifest_id,
        )
    )


def metadata_changed(manifest_doc: dict, ledger_doc: dict) -> bool:
    """Return True if the change token (doc_version) differs between manifest
    and ledger, or if it cannot be compared (NULL on either side) — the pre-parsing check
    must never under-trigger. (size_bytes is a cost proxy, not compared here.)"""
    comparable = False
    for field_name in _METADATA_FIELDS:
        ours, theirs = manifest_doc.get(field_name), ledger_doc.get(field_name)
        if ours is None or theirs is None:
            continue
        comparable = True
        if ours != theirs:
            return True
    return not comparable


def classify(manifest_docs: list[dict], ledger_docs: list[dict]) -> dict[str, list[dict]]:
    """Split a run's manifest into new / changed / reappeared / unchanged."""
    docs = {(d["source_system"], d["doc_url"]): d for d in ledger_docs}
    result: dict[str, list[dict]] = {"new": [], "changed": [], "reappeared": [], "unchanged": []}
    for doc in manifest_docs:
        document = docs.get((doc["source_system"], doc["doc_url"]))
        if document is None:
            result["new"].append(doc)
        elif document["status"] == "deleted":
            result["reappeared"].append(doc)
        elif metadata_changed(doc, document):
            result["changed"].append(doc)
        else:
            result["unchanged"].append(doc)
    return result


def compute_deletions(
    manifest_docs: list[dict],
    ledger_docs: list[dict],
    sources: dict[str, SourceConfig],
) -> list[dict]:
    """Active ledger docs no longer listed by their source this run.

    A source is included only if (a) its config sets detect_deletions —
    rolling-window feeds (RSS) do not — and (b) it produced at least one
    manifest row this run. A source with no rows this run gives no evidence
    about its documents.
    """
    listed = {(r["source_system"], r["doc_url"]) for r in manifest_docs}
    sources_seen_this_run = {r["source_id"] for r in manifest_docs}
    eligible = {
        sid for sid, cfg in sources.items()
        if cfg is not None and cfg.detect_deletions and sid in sources_seen_this_run
    }
    deletion_docs = [
        doc for doc in ledger_docs
        if doc["status"] == "active"
        and doc["source_id"] in eligible
        and (doc["source_system"], doc["doc_url"]) not in listed
    ]
    return deletion_docs


def _replace_rows(engine: Engine, table, rows: list[dict]) -> None:
    """Make the table hold exactly `rows` — delta fully owns worklist and
    deletion_docs.

    Upserting alone would only ever add: re-running a run after a document
    stopped being 'changed' — or came back, so it is no longer deleted — would
    leave the stale row behind for plan or finalize to act on. Deleting the
    rows delta no longer computes fixes that.

    These are work queues, not history: plan and finalize each read one run's
    rows (WHERE run_id = ...), so any other run's rows are stale and would only
    accumulate. Hence a full replace, not a per-run one — this is also how
    whole-table mode (run_id=None) has always behaved.
    """
    keep = {r["manifest_id"] for r in rows}
    with engine.begin() as conn:
        stmt = delete(table)
        if keep:
            stmt = stmt.where(table.c.manifest_id.notin_(keep))
        conn.execute(stmt)
    upsert_rows(engine, table, rows, _KEY)


def run_delta(
    config_path: str | Path,
    run_id: str | None = None,
    engine: Engine | None = None,
) -> DeltaReport:
    """DELTA stage: diff a manifest against the ingested-docs ledger.

    With run_id: diff exactly that discovery run's manifest (the DAG path).
    Without run_id: diff the whole-table view, handled per source type.
    Rolling-window sources keep each document's latest row across all runs —
    their history is the full set and gives no deletion evidence.
    detect_deletions sources are first cut down to their most recent run:
    that run lists everything the source has now, so deletion_docs are computed
    from it the same way as in run mode. Each deletion row records the
    run_id of the run it was based on — as recent as that source's last
    discovery.

    Writes ingest_worklist + ingest_deletions rows (idempotent upserts;
    each worklist row keeps the run_id of the manifest row it came from) and
    returns counts only, per the stage contract. Raises ValueError when no
    manifest rows match — a mistyped run_id must raise, not report
    "nothing changed".
    """
    sources = load_sources(config_path)
    engine = engine or get_engine()

    query = select(ingest_manifest)
    if run_id is not None:
        query = query.where(ingest_manifest.c.run_id == run_id)
    with engine.connect() as conn:
        manifest_docs = [dict(r) for r in conn.execute(query).mappings()]
        ledger_docs = [dict(r) for r in conn.execute(_ledger_query()).mappings()]

    if not manifest_docs:
        raise ValueError(
            f"no manifest rows for run {run_id!r} — did discover run?"
            if run_id is not None
            else "ingest_manifest is empty — did discover ever run?"
        )

    evidence_runs: dict[str, str] = {}
    if run_id is None:
        manifest_docs, evidence_runs = latest_complete_listings(manifest_docs, sources)
        logger.info(
            "whole-table mode: diffing %s documents; "
            "%s source(s) eligible for deletion detection",
            len(manifest_docs),
            len(evidence_runs),
        )
    deletion_docs = compute_deletions(manifest_docs, ledger_docs, sources)
    
    # Deduplicate manifest docs
    manifest_docs = unique_manifest_docs(manifest_docs, sources)

    classes = classify(manifest_docs, ledger_docs)

    worklist_docs = [
        {
            "manifest_id": row["manifest_id"],
            "run_id": row["run_id"],
            "reason": reason,
            # the sighting's own timestamp, not now(): plan orders on it
            "execution_ts": row["execution_ts"],
        }
        for reason in ("new", "changed", "reappeared")
        for row in classes[reason]
    ]
    # manifest_id here is the ledger's — the sighting whose points are live in
    # Qdrant, which is what finalize must delete. run_id is the detecting run.
    deletion_docs = [
        {
            "manifest_id": doc["manifest_id"],
            "run_id": run_id if run_id is not None else evidence_runs[doc["source_id"]],
        }
        for doc in deletion_docs
    ]
    _replace_rows(engine, ingest_worklist, worklist_docs)
    _replace_rows(engine, ingest_deletions, deletion_docs)

    report = DeltaReport(run_id=run_id if run_id is not None else "<all-runs>")
    for reason, rows in classes.items():
        report.counts[reason] = len(rows)
    report.counts["deleted"] = len(deletion_docs)
    logger.info("delta %s: %s", report.run_id, report.counts)
    return report


def mark_ingested(run_id: str, engine: Engine | None = None) -> int:
    """TEMPORARY stand-in for the embed stage's ledger writes.

    Marks this run's worklist documents as ingested by copying their
    manifest signals into the ingest_docs ledger. In the full pipeline this
    happens in EMBED (the embed stage) after the vectors are written to
    Qdrant — this helper lets the discover→delta cycle run start to finish
    before the plan, parse_chunk and embed stages exist. Remove when the embed stage owns it.
    """
    engine = engine or get_engine()
    with engine.connect() as conn:
        work_ids = {
            r["manifest_id"]
            for r in conn.execute(
                select(ingest_worklist).where(ingest_worklist.c.run_id == run_id)
            ).mappings()
        }
        manifest_docs = [
            dict(r) for r in conn.execute(
                select(ingest_manifest).where(ingest_manifest.c.run_id == run_id)
            ).mappings()
        ]
        ledger = {
            (r["source_system"], r["doc_url"]): r["manifest_id"]
            for r in conn.execute(_ledger_query()).mappings()
        }

    count = 0
    with engine.begin() as conn:
        for row in manifest_docs:
            if row["manifest_id"] not in work_ids:
                continue
            # the ledger's key IS manifest_id, so re-ingesting a document
            # moves the existing row onto the new sighting rather than
            # inserting a second one (see embed for the real thing)
            existing = ledger.get((row["source_system"], row["doc_url"]))
            if existing is None:
                conn.execute(
                    insert(ingest_docs).values(
                        manifest_id=row["manifest_id"], status="active"
                    )
                )
            else:
                conn.execute(
                    update(ingest_docs)
                    .where(ingest_docs.c.manifest_id == existing)
                    .values(manifest_id=row["manifest_id"], status="active")
                )
            count += 1
    logger.info("mark_ingested %s: %s docs", run_id, count)
    return count
