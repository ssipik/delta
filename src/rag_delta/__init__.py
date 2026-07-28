"""The delta stage of the RAG ingestion pipeline (ARCHITECTURE.md → Ingestion orchestration).

Standalone stage module: infrastructure (settings, sources, db) is
deliberately duplicated from rag_discover — stages are independent blocks;
dedup refactor deferred until it hurts.
"""
