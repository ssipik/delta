"""Dev/ops entrypoint: ``python -m rag_delta``.

Thin argparse wrapper over :func:`rag_delta.delta.run_delta` — the library
function remains the API that Airflow tasks import directly; keep all stage
logic out of this module.
"""

from __future__ import annotations

import argparse
import logging
import sys

from dotenv import load_dotenv

from rag_delta.delta import mark_ingested, run_delta


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m rag_delta",
        description="DELTA stage: diff a run's manifest against the ingested-docs "
        "ledger; write worklist + deletions.",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="run id whose manifest to diff (a rag_discover run). Omitted: "
        "diff the whole-table view — each document's most recent sighting, "
        "with detect_deletions sources scoped to their newest run so "
        "deletions are computed from that run's complete listing",
    )
    parser.add_argument(
        "--mark-ingested",
        action="store_true",
        help="instead of diffing: mark the run's worklist docs as ingested "
        "(dev stand-in for the embed stage's bookkeeping)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    if args.mark_ingested and args.run_id is None:
        parser.error("--mark-ingested requires --run-id (marks one run's worklist)")
    try:
        if args.mark_ingested:
            count = mark_ingested(args.run_id)
            print(f"run {args.run_id}: {count} docs marked ingested")
            return 0
        report = run_delta(run_id=args.run_id)
    except (ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for reason, count in report.counts.items():
        print(f"{reason}: {count}")
    print(
        f"run {report.run_id}: worklist={report.worklist_total} "
        f"deletions={report.counts['deleted']}"
        + (" — nothing to do (short-circuit)" if report.empty else "")
    )
    return 0


if __name__ == "__main__":
    # Only needed when running in development. In production the env variables are provided by Vault.
    load_dotenv()
    raise SystemExit(main())
