from datetime import datetime, timezone

from rag_delta.delta import (
    classify,
    compute_deletions,
    latest_complete_listings,
    unique_manifest_docs,
    metadata_changed,
)
from rag_config import SourceConfig


def ts(day: int) -> datetime:
    return datetime(2026, 7, day, tzinfo=timezone.utc)


def manifest_row(**overrides) -> dict:
    row = {
        "run_id": "run-1",
        "source_system": "web_rss",
        "doc_url": "https://example.com/doc1",
        "source_id": "feed_a",
        "doc_version": "guid-1|2026-07-01",
        "size_bytes": None,
        "published_at": None,
        "execution_ts": ts(1),
    }
    row.update(overrides)
    return row


def doc_row(**overrides) -> dict:
    row = {
        "source_system": "web_rss",
        "doc_url": "https://example.com/doc1",
        "source_id": "feed_a",
        "doc_version": "guid-1|2026-07-01",
        "size_bytes": None,
        "status": "active",
    }
    row.update(overrides)
    return row


def source(
    sid: str, detect_deletions: bool = True, type: str = "rss"
) -> SourceConfig:
    return SourceConfig(
        id=sid,
        type=type,
        source_system="web_rss",
        url=f"https://example.com/{sid}",
        detect_deletions=detect_deletions,
    )


RSS_SOURCES = {"feed_a": source("feed_a"), "feed_b": source("feed_b")}


class TestUniqueManifestDocs:
    def test_most_recent_sighting_wins(self):
        old = manifest_row(run_id="run-1", execution_ts=ts(1), doc_version="v1")
        new = manifest_row(run_id="run-2", execution_ts=ts(9), doc_version="v2")
        result = unique_manifest_docs([old, new], RSS_SOURCES)
        assert len(result) == 1
        assert result[0]["doc_version"] == "v2"

    def test_distinct_docs_all_kept(self):
        a = manifest_row(doc_url="https://example.com/a")
        b = manifest_row(doc_url="https://example.com/b")
        assert len(unique_manifest_docs([a, b], RSS_SOURCES)) == 2

    def test_ts_tie_keeps_first_row(self):
        first = manifest_row(run_id="run-1", doc_version="v1")
        second = manifest_row(run_id="run-2", doc_version="v2")  # same execution_ts
        assert unique_manifest_docs([first, second], RSS_SOURCES)[0]["doc_version"] == "v1"

    def test_rss_published_at_outranks_execution_ts(self):
        # feed_b re-listed the doc later, but with an older pubDate: the
        # sighting with the newest published_at must win, not the newest run
        newer_item = manifest_row(
            run_id="run-1", source_id="feed_a",
            published_at=ts(8), execution_ts=ts(1), doc_version="v-new",
        )
        stale_relisting = manifest_row(
            run_id="run-2", source_id="feed_b",
            published_at=ts(5), execution_ts=ts(9), doc_version="v-old",
        )
        result = unique_manifest_docs([newer_item, stale_relisting], RSS_SOURCES)
        assert result[0]["doc_version"] == "v-new"

    def test_rss_missing_published_at_loses_to_dated_sighting(self):
        undated = manifest_row(
            run_id="run-1", published_at=None, execution_ts=ts(9), doc_version="v1"
        )
        dated = manifest_row(
            run_id="run-2", published_at=ts(2), execution_ts=ts(1), doc_version="v2"
        )
        result = unique_manifest_docs([undated, dated], RSS_SOURCES)
        assert result[0]["doc_version"] == "v2"

    def test_non_rss_source_ignores_published_at(self):
        sources = {"feed_a": source("feed_a", type="local_files")}
        old_pub_new_run = manifest_row(
            run_id="run-2", published_at=ts(1), execution_ts=ts(9), doc_version="v2"
        )
        new_pub_old_run = manifest_row(
            run_id="run-1", published_at=ts(8), execution_ts=ts(1), doc_version="v1"
        )
        result = unique_manifest_docs([old_pub_new_run, new_pub_old_run], sources)
        assert result[0]["doc_version"] == "v2"

    def test_confluence_highest_version_wins(self):
        sources = {"wiki": source("wiki", type="confluence")}
        # version 10 > version 9: compared as numbers, not as text
        old = manifest_row(
            source_id="wiki", run_id="run-2", execution_ts=ts(9), doc_version="9"
        )
        new = manifest_row(
            source_id="wiki", run_id="run-1", execution_ts=ts(1), doc_version="10"
        )
        result = unique_manifest_docs([old, new], sources)
        assert len(result) == 1
        assert result[0]["doc_version"] == "10"

    def test_confluence_missing_version_loses(self):
        sources = {"wiki": source("wiki", type="confluence")}
        unversioned = manifest_row(
            source_id="wiki", run_id="run-2", execution_ts=ts(9), doc_version=None
        )
        versioned = manifest_row(
            source_id="wiki", run_id="run-1", execution_ts=ts(1), doc_version="3"
        )
        result = unique_manifest_docs([unversioned, versioned], sources)
        assert result[0]["doc_version"] == "3"

    def test_confluence_same_version_keeps_newest_run(self):
        sources = {"wiki": source("wiki", type="confluence")}
        old = manifest_row(
            source_id="wiki", run_id="run-1", execution_ts=ts(1), doc_version="4"
        )
        new = manifest_row(
            source_id="wiki", run_id="run-2", execution_ts=ts(9), doc_version="4"
        )
        result = unique_manifest_docs([old, new], sources)
        assert len(result) == 1
        assert result[0]["run_id"] == "run-2"

    def test_unknown_source_id_falls_back_to_execution_ts(self):
        # source_id no longer in sources.yaml → observation order
        removed = manifest_row(
            source_id="gone_feed", published_at=ts(8), execution_ts=ts(1),
            doc_version="v1",
        )
        current = manifest_row(
            source_id="gone_feed", published_at=ts(2), execution_ts=ts(9),
            doc_version="v2",
        )
        result = unique_manifest_docs([removed, current], RSS_SOURCES)
        assert result[0]["doc_version"] == "v2"


class TestLatestCompleteListings:
    def test_older_run_rows_dropped_for_diffable_source(self):
        # doc "gone" was listed in run-1 only: scoping must not resurrect it
        sources = {"wiki": source("wiki", type="local_files")}
        old_gone = manifest_row(
            source_id="wiki", run_id="run-1", execution_ts=ts(1),
            doc_url="https://example.com/gone",
        )
        old_kept = manifest_row(source_id="wiki", run_id="run-1", execution_ts=ts(1))
        new_kept = manifest_row(source_id="wiki", run_id="run-2", execution_ts=ts(9))
        scoped, evidence = latest_complete_listings(
            [old_gone, old_kept, new_kept], sources
        )
        assert scoped == [new_kept]
        assert evidence == {"wiki": "run-2"}

    def test_rolling_window_source_keeps_all_runs(self):
        rows = [
            manifest_row(run_id="run-1", execution_ts=ts(1),
                         doc_url="https://example.com/a"),
            manifest_row(run_id="run-2", execution_ts=ts(9),
                         doc_url="https://example.com/b"),
        ]
        scoped, evidence = latest_complete_listings(
            rows, {"feed_a": source("feed_a", detect_deletions=False)}
        )
        assert scoped == rows
        assert evidence == {}

    def test_unknown_source_rows_dropped(self):
        # source_id no longer in sources.yaml → cannot be processed, excluded
        rows = [
            manifest_row(source_id="gone_feed", run_id="run-1", execution_ts=ts(1)),
            manifest_row(source_id="gone_feed", run_id="run-2", execution_ts=ts(9)),
        ]
        scoped, evidence = latest_complete_listings(rows, RSS_SOURCES)
        assert scoped == []
        assert evidence == {}

    def test_sources_scoped_independently(self):
        # wiki_b's latest run (run-2) must not discard wiki_a's run-1 rows
        sources = {
            "wiki_a": source("wiki_a", type="local_files"),
            "wiki_b": source("wiki_b", type="local_files"),
        }
        a_row = manifest_row(source_id="wiki_a", run_id="run-1", execution_ts=ts(1))
        b_row = manifest_row(
            source_id="wiki_b", run_id="run-2", execution_ts=ts(9),
            doc_url="https://example.com/b",
        )
        scoped, evidence = latest_complete_listings([a_row, b_row], sources)
        assert scoped == [a_row, b_row]
        assert evidence == {"wiki_a": "run-1", "wiki_b": "run-2"}


class TestMetadataChanged:
    def test_equal_field_is_unchanged(self):
        assert metadata_changed(manifest_row(), doc_row()) is False

    def test_differing_field_is_changed(self):
        assert metadata_changed(
            manifest_row(doc_version="guid-1|2026-07-02"), doc_row()
        )

    def test_no_comparable_field_means_changed(self):
        # the pre-parsing check must never under-trigger
        assert metadata_changed(
            manifest_row(doc_version=None), doc_row(doc_version=None)
        )

class TestClassify:
    def test_unknown_doc_is_new(self):
        classes = classify([manifest_row()], [])
        assert len(classes["new"]) == 1
        assert not classes["changed"] and not classes["unchanged"]

    def test_known_unchanged_doc(self):
        classes = classify([manifest_row()], [doc_row()])
        assert len(classes["unchanged"]) == 1

    def test_known_changed_doc(self):
        classes = classify(
            [manifest_row(doc_version="guid-1|2026-07-09")], [doc_row()]
        )
        assert len(classes["changed"]) == 1

    def test_deleted_doc_reappears(self):
        classes = classify([manifest_row()], [doc_row(status="deleted")])
        assert len(classes["reappeared"]) == 1


class TestComputeDeletions:
    def test_absent_doc_of_diffable_source_is_deleted(self):
        gone = doc_row(doc_url="https://example.com/gone")
        deletions = compute_deletions(
            [manifest_row()], [gone], {"feed_a": source("feed_a")}
        )
        assert [d["doc_url"] for d in deletions] == ["https://example.com/gone"]

    def test_rolling_window_source_excluded(self):
        gone = doc_row(doc_url="https://example.com/gone")
        deletions = compute_deletions(
            [manifest_row()],
            [gone],
            {"feed_a": source("feed_a", detect_deletions=False)},
        )
        assert deletions == []

    def test_source_absent_from_run_gives_no_evidence(self):
        # feed_b produced no manifest rows this run → its docs are not deletions
        gone = doc_row(doc_url="https://example.com/gone", source_id="feed_b")
        deletions = compute_deletions(
            [manifest_row()],  # only feed_a listed anything
            [gone],
            {"feed_a": source("feed_a"), "feed_b": source("feed_b")},
        )
        assert deletions == []

    def test_already_deleted_doc_not_reported_again(self):
        gone = doc_row(doc_url="https://example.com/gone", status="deleted")
        deletions = compute_deletions(
            [manifest_row()], [gone], {"feed_a": source("feed_a")}
        )
        assert deletions == []
