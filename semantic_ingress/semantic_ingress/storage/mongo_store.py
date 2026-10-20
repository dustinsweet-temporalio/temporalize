# semantic_ingress/storage/mongo_store.py
"""
MongoDB persistence for the semantic index.

Talks to MongoDB directly (no proxy). Each repository gets its own database,
named ``{prefix}_{repo_id}``, with ``file_summaries``, ``folder_summaries`` and
``repo_summaries`` collections. The public methods keep the names and return
shapes the generation orchestrator already relies on.
"""
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from pymongo import DESCENDING, MongoClient, UpdateOne
from pymongo.errors import OperationFailure, PyMongoError

from semantic_ingress.config import MongoConfig
from semantic_ingress.models import DocumentType, IndexMeta, StoredDocument

_FILE_SUMMARY = DocumentType.FILE_SUMMARY

logger = structlog.get_logger()

COLLECTION_MAP = {
    "file_summary": "file_summaries",
    "folder_summary": "folder_summaries",
    "repo_summary": "repo_summaries",
    "dir_list": "repo_summaries",
}
INDEX_META_COLLECTION = "index_meta"
# Written by the Temporal report; each card records the blob ids it depends on.
PROCESS_CARDS_COLLECTION = "process_cards"
# The latest Temporal report, and cached results of its model calls (keyed on their inputs).
REPORTS_COLLECTION = "reports"
REPORT_CACHE_COLLECTION = "report_cache"
# Business-impact runs: each request (with the customer context it was given) and its
# per-process results. Removed IMPACT_REQUEST_TTL after the last write; the result
# itself is kept in REPORTS_COLLECTION.
IMPACT_REQUESTS_COLLECTION = "impact_requests"
IMPACT_REQUEST_TTL = timedelta(days=7)

# Text search covers the summary prose and the lens prose; summary text ranks higher.
TEXT_INDEX_NAME = "text_idx"
TEXT_INDEX_WEIGHTS = {"prose_summary": 2, "lens_prose": 1}


def _expire_at() -> datetime:
    return datetime.now(timezone.utc) + IMPACT_REQUEST_TTL


class MongoStoreError(Exception):
    """Base exception for the Mongo store."""
    pass


class MongoStore:
    """
    Client for the MongoDB semantic index.

    All operations are stateless, idempotent and deterministic.
    """

    def __init__(self, config: MongoConfig, prefix: str) -> None:
        """
        Initialize the store and verify the database is reachable.

        Args:
            config: Mongo configuration (connection URL)
            prefix: Database name prefix (must start with 'sem')
        """
        if not prefix.startswith("sem"):
            raise ValueError(f"Invalid prefix '{prefix}'. Prefix must start with 'sem'.")

        self.prefix = prefix
        self.log = logger.bind(component="mongo_store", prefix=prefix)
        self._ttl_ready: set[str] = set()  # databases whose impact request TTL index exists

        self.client: MongoClient = MongoClient(
            config.url,
            serverSelectionTimeoutMS=config.server_selection_timeout_ms,
        )
        try:
            self.client.admin.command("ping")
        except PyMongoError as e:
            raise MongoStoreError(
                f"Cannot connect to MongoDB at the configured MONGO_URL: {e}"
            ) from e

        self.log.info("mongo_store_initialized")

    # ------------------------------------------------------------------
    # Naming
    # ------------------------------------------------------------------

    def _get_database_name(self, repo_id: str) -> str:
        """
        Generate database name from repository ID and prefix.
        Each repository gets its own isolated database.

        Returns:
            Database name (e.g., "semv2_my-repo")
        """
        safe_repo_id = repo_id.replace("/", "_")
        return f"{self.prefix}_{safe_repo_id}"

    def _get_collection_name(self, document_type: Any) -> str:
        """Map a DocumentType enum value to its collection name."""
        return COLLECTION_MAP.get(document_type.value, "unknown")

    def _get_collection_name_from_string(self, doc_type_str: str) -> str:
        """Map a document type string to its collection name."""
        return COLLECTION_MAP.get(doc_type_str, "unknown")

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def query_documents(
        self,
        repo_id: str,
        collection: str,
        filter_query: dict[str, Any] | None = None,
        projection: dict[str, int] | None = None,
        limit: int | None = None,
        batch_size: int = 100,
    ) -> list[dict[str, Any]]:
        """
        Query documents from a collection.

        Args:
            repo_id: Repository identifier
            collection: Collection name
            filter_query: MongoDB filter query (optional)
            projection: Field projection (optional)
            limit: Maximum total documents to return (None = all)
            batch_size: Cursor batch size

        Returns:
            List of documents (empty if the collection does not exist)
        """
        database_name = self._get_database_name(repo_id)
        self.log.info(
            "query_documents_start",
            database=database_name,
            collection=collection,
            limit=limit,
        )

        try:
            cursor = self.client[database_name][collection].find(
                filter_query or {}, projection
            ).batch_size(batch_size)
            if limit is not None:
                cursor = cursor.limit(limit)
            documents = list(cursor)
        except PyMongoError as e:
            raise MongoStoreError(f"Query failed on {database_name}.{collection}: {e}") from e

        self.log.info("query_documents_complete", count=len(documents))
        return documents

    def get_tree_snapshot(self, repo_id: str) -> dict[str, str]:
        """
        Retrieve the flat tree snapshot (path -> blob OID) of what is indexed.

        Returns:
            Dictionary mapping file paths to blob OIDs (empty if not indexed yet)
        """
        self.log.info("retrieving_tree_snapshot", repo_id=repo_id)

        documents = self.query_documents(
            repo_id=repo_id,
            collection="file_summaries",
            projection={"path": 1, "blob_oid": 1},
        )

        tree: dict[str, str] = {}
        for doc in documents:
            path = doc.get("path")
            blob_oid = doc.get("blob_oid")
            if path and blob_oid:
                tree[path] = blob_oid

        self.log.info("tree_snapshot_retrieved", repo_id=repo_id, file_count=len(tree))
        return tree

    def get_index_meta(self, repo_id: str) -> dict[str, Any] | None:
        """Return the index_meta document, or None if the index predates it or is empty."""
        database_name = self._get_database_name(repo_id)
        try:
            return self.client[database_name][INDEX_META_COLLECTION].find_one(
                {"_id": IndexMeta.make_id(repo_id)}
            )
        except PyMongoError as e:
            raise MongoStoreError(f"Reading index_meta failed on {database_name}: {e}") from e

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def upsert_document(self, document: StoredDocument) -> None:
        """Insert or update one document (idempotent by deterministic ID)."""
        self.upsert_documents_bulk([document], batch_size=1)

    def upsert_documents_bulk(
        self,
        documents: list[StoredDocument],
        batch_size: int = 500,
        preserve_if_none: frozenset[str] = frozenset(),
    ) -> dict[str, Any]:
        """
        Bulk upsert documents, grouped by collection.

        Continues past failed batches and reports them in the summary. Fields named
        in `preserve_if_none` are left untouched in the stored document when the new
        value is None (e.g. repair-folders keeps a folder's existing lens).

        Returns:
            ``{"total_processed", "summary": {inserted, updated, no_change, errors}}``
        """
        empty = {
            "total_processed": 0,
            "summary": {"inserted": 0, "updated": 0, "no_change": 0, "errors": 0},
        }
        if not documents:
            self.log.warning("bulk_upsert_called_with_empty_list")
            return empty

        # The caller historically passed batch_size=1 (a limit of the old proxy
        # transport). Direct writes have no such limit, so use a sensible floor.
        batch_size = max(batch_size, 100)

        repo_id = documents[0].repo_id
        database_name = self._get_database_name(repo_id)

        by_collection: dict[str, list[StoredDocument]] = {}
        for doc in documents:
            if doc.repo_id != repo_id:
                self.log.warning(
                    "mismatched_repo_id_in_bulk",
                    expected=repo_id,
                    got=doc.repo_id,
                    doc_id=doc.id,
                )
                continue
            by_collection.setdefault(self._get_collection_name(doc.document_type), []).append(doc)

        inserted = updated = no_change = errors = processed = 0

        for collection_name, docs in by_collection.items():
            collection = self.client[database_name][collection_name]
            for start in range(0, len(docs), batch_size):
                batch = docs[start:start + batch_size]
                operations = []
                for doc in batch:
                    data = doc.model_dump(by_alias=True, mode="json")
                    data.pop("_id", None)
                    for key in preserve_if_none:
                        if data.get(key) is None:
                            data.pop(key, None)
                    operations.append(UpdateOne({"_id": doc.id}, {"$set": data}, upsert=True))

                try:
                    result = collection.bulk_write(operations, ordered=False)
                    inserted += result.upserted_count
                    updated += result.modified_count
                    no_change += result.matched_count - result.modified_count
                    processed += len(operations)
                except PyMongoError as e:
                    self.log.error(
                        "bulk_upsert_batch_failed",
                        database=database_name,
                        collection=collection_name,
                        batch_size=len(batch),
                        error=str(e),
                    )
                    errors += len(batch)

        summary = {
            "inserted": inserted,
            "updated": updated,
            "no_change": no_change,
            "errors": errors,
        }
        self.log.info("bulk_upsert_complete", processed=processed, **summary)

        if errors == len(documents):
            raise MongoStoreError("All bulk upsert batches failed")

        return {"total_processed": processed, "summary": summary}

    def delete_documents_bulk(self, doc_ids: list[str], repo_id: str) -> dict[str, Any]:
        """
        Bulk delete documents by ID (format ``{repo_id}::{doc_type}::{path}``).

        Returns:
            ``{"total_requested", "total_deleted"}``
        """
        if not doc_ids:
            self.log.warning("bulk_delete_called_with_empty_list")
            return {"total_requested": 0, "total_deleted": 0}

        database_name = self._get_database_name(repo_id)

        by_collection: dict[str, list[str]] = {}
        for doc_id in doc_ids:
            parts = doc_id.split("::")
            if len(parts) < 3:
                self.log.warning("invalid_doc_id_format", doc_id=doc_id)
                continue
            collection = self._get_collection_name_from_string(parts[1])
            by_collection.setdefault(collection, []).append(doc_id)

        total_deleted = 0
        for collection_name, ids in by_collection.items():
            try:
                result = self.client[database_name][collection_name].delete_many({"_id": {"$in": ids}})
            except PyMongoError as e:
                self.log.error(
                    "collection_delete_failed",
                    database=database_name,
                    collection=collection_name,
                    error=str(e),
                )
                raise MongoStoreError(f"Delete failed on {database_name}.{collection_name}: {e}") from e
            total_deleted += result.deleted_count

        result_summary = {"total_requested": len(doc_ids), "total_deleted": total_deleted}
        self.log.info("bulk_delete_complete", **result_summary)
        return result_summary

    def update_file_lenses(self, repo_id: str, updates: list[tuple[str, dict, str]]) -> int:
        """Set lens and lens_prose on existing file documents (path, lens, lens_prose). Returns modified count."""
        if not updates:
            return 0
        database_name = self._get_database_name(repo_id)
        ops = [
            UpdateOne(
                {"_id": StoredDocument.generate_id(repo_id, _FILE_SUMMARY, path)},
                {"$set": {"lens": lens, "lens_prose": prose}},
            )
            for path, lens, prose in updates
        ]
        try:
            result = self.client[database_name]["file_summaries"].bulk_write(ops, ordered=False)
        except PyMongoError as e:
            raise MongoStoreError(f"Lens update failed on {database_name}: {e}") from e
        self.log.info("file_lenses_updated", count=result.modified_count)
        return result.modified_count

    def lens_pack_versions(self, repo_id: str) -> list[dict[str, Any]]:
        """File counts per lens pack version, e.g. [{"version": "0.1.0", "files": 120}, {"version": None, "files": 3}]."""
        database_name = self._get_database_name(repo_id)
        try:
            rows = self.client[database_name]["file_summaries"].aggregate(
                [{"$group": {"_id": "$lens.pack_version", "files": {"$sum": 1}}}, {"$sort": {"_id": 1}}]
            )
            return [{"version": r["_id"], "files": r["files"]} for r in rows]
        except PyMongoError as e:
            raise MongoStoreError(f"Lens version count failed on {database_name}: {e}") from e

    def get_process_card(self, repo_id: str, key: str) -> dict[str, Any] | None:
        """A stored process card (written by the Temporal report), or None."""
        database_name = self._get_database_name(repo_id)
        try:
            return self.client[database_name][PROCESS_CARDS_COLLECTION].find_one({"_id": key})
        except PyMongoError as e:
            raise MongoStoreError(f"Reading a process card failed on {database_name}: {e}") from e

    def put_process_card(self, repo_id: str, doc: dict[str, Any]) -> None:
        """Insert or replace a process card document (must carry `_id`)."""
        database_name = self._get_database_name(repo_id)
        try:
            self.client[database_name][PROCESS_CARDS_COLLECTION].replace_one({"_id": doc["_id"]}, doc, upsert=True)
        except PyMongoError as e:
            raise MongoStoreError(f"Writing a process card failed on {database_name}: {e}") from e

    def get_report(self, repo_id: str) -> dict[str, Any] | None:
        """The stored Temporal report (latest), or None."""
        return self._get_one(repo_id, REPORTS_COLLECTION, f"{repo_id}::temporal_report")

    def put_report(self, repo_id: str, doc: dict[str, Any]) -> None:
        """Replace the stored Temporal report."""
        self._put_one(repo_id, REPORTS_COLLECTION, {**doc, "_id": f"{repo_id}::temporal_report"})

    def get_business_impact(self, repo_id: str) -> dict[str, Any] | None:
        """The stored business impact (latest), or None."""
        return self._get_one(repo_id, REPORTS_COLLECTION, f"{repo_id}::business_impact")

    def put_business_impact(self, repo_id: str, doc: dict[str, Any]) -> None:
        """Replace the stored business impact."""
        self._put_one(repo_id, REPORTS_COLLECTION, {**doc, "_id": f"{repo_id}::business_impact"})

    def put_impact_request(self, repo_id: str, doc: dict[str, Any]) -> None:
        """Insert or replace a business-impact request (must carry `_id`, the request id)."""
        self._ensure_request_ttl(repo_id)
        self._put_one(repo_id, IMPACT_REQUESTS_COLLECTION, {**doc, "expire_at": _expire_at()})

    def get_impact_request(self, repo_id: str, request_id: str) -> dict[str, Any] | None:
        return self._get_one(repo_id, IMPACT_REQUESTS_COLLECTION, request_id)

    def update_impact_request(self, repo_id: str, request_id: str, fields: dict[str, Any]) -> None:
        """Set fields on a request (dotted paths allowed, e.g. results.<key>); repeating it is harmless."""
        database_name = self._get_database_name(repo_id)
        try:
            self.client[database_name][IMPACT_REQUESTS_COLLECTION].update_one(
                {"_id": request_id}, {"$set": {**fields, "expire_at": _expire_at()}})
        except PyMongoError as e:
            raise MongoStoreError(f"Updating an impact request failed on {database_name}: {e}") from e

    def latest_impact_request(self, repo_id: str) -> dict[str, Any] | None:
        """The newest business-impact request, or None."""
        database_name = self._get_database_name(repo_id)
        try:
            return self.client[database_name][IMPACT_REQUESTS_COLLECTION].find_one(
                {}, sort=[("created_at", DESCENDING)])
        except PyMongoError as e:
            raise MongoStoreError(f"Reading impact requests failed on {database_name}: {e}") from e

    def _ensure_request_ttl(self, repo_id: str) -> None:
        database_name = self._get_database_name(repo_id)
        if database_name in self._ttl_ready:
            return
        try:
            self.client[database_name][IMPACT_REQUESTS_COLLECTION].create_index("expire_at", expireAfterSeconds=0)
        except PyMongoError as e:
            raise MongoStoreError(f"Creating the impact request TTL index failed on {database_name}: {e}") from e
        self._ttl_ready.add(database_name)

    def get_cached(self, repo_id: str, key: str) -> dict[str, Any] | None:
        """A cached report step result (see semantic_ingress.report.cache), or None."""
        return self._get_one(repo_id, REPORT_CACHE_COLLECTION, key)

    def put_cached(self, repo_id: str, key: str, value: dict[str, Any]) -> None:
        self._put_one(repo_id, REPORT_CACHE_COLLECTION, {"_id": key, **value})

    def _get_one(self, repo_id: str, collection: str, doc_id: str) -> dict[str, Any] | None:
        database_name = self._get_database_name(repo_id)
        try:
            return self.client[database_name][collection].find_one({"_id": doc_id})
        except PyMongoError as e:
            raise MongoStoreError(f"Reading {collection} failed on {database_name}: {e}") from e

    def _put_one(self, repo_id: str, collection: str, doc: dict[str, Any]) -> None:
        database_name = self._get_database_name(repo_id)
        try:
            self.client[database_name][collection].replace_one({"_id": doc["_id"]}, doc, upsert=True)
        except PyMongoError as e:
            raise MongoStoreError(f"Writing {collection} failed on {database_name}: {e}") from e

    def text_search_repo(self, repo_id: str, collection: str, text: str, limit: int = 8) -> list[dict[str, Any]]:
        """`$text` search in one of a repository's collections."""
        return self.text_search(self._get_database_name(repo_id), collection, text, limit)

    def prompt_versions(self, repo_id: str) -> list[dict[str, Any]]:
        """File counts per prompt version, e.g. [{"version": None, "files": 72}, {"version": 2, "files": 38}]."""
        database_name = self._get_database_name(repo_id)
        try:
            rows = self.client[database_name]["file_summaries"].aggregate(
                [{"$group": {"_id": "$prompt_version", "files": {"$sum": 1}}}, {"$sort": {"_id": 1}}]
            )
            return [{"version": r["_id"], "files": r["files"]} for r in rows]
        except PyMongoError as e:
            raise MongoStoreError(f"Prompt version count failed on {database_name}: {e}") from e

    def put_index_meta(self, meta: IndexMeta) -> None:
        """Insert or replace the index_meta document."""
        database_name = self._get_database_name(meta.repo_id)
        data = meta.model_dump(by_alias=True)
        try:
            self.client[database_name][INDEX_META_COLLECTION].replace_one(
                {"_id": data["_id"]}, data, upsert=True
            )
        except PyMongoError as e:
            raise MongoStoreError(f"Writing index_meta failed on {database_name}: {e}") from e
        self.log.info("index_meta_written", repo_id=meta.repo_id, schema_version=meta.schema_version)

    def reset_repository(self, repo_id: str) -> None:
        """
        Drop every index collection for a repository (summaries, dir list, index_meta).

        Used by a full re-index (--force or an outdated layout) right before the new
        documents are written, so paths removed from the repository do not linger.
        """
        database_name = self._get_database_name(repo_id)
        db = self.client[database_name]
        for name in ["file_summaries", "folder_summaries", "repo_summaries", INDEX_META_COLLECTION]:
            try:
                db.drop_collection(name)
            except PyMongoError as e:
                raise MongoStoreError(f"Dropping {database_name}.{name} failed: {e}") from e
        self.log.info("repository_index_reset", database=database_name)

    # ------------------------------------------------------------------
    # Indexes and search
    # ------------------------------------------------------------------

    def ensure_text_index(self, database: str, collection: str) -> str:
        """
        Make sure the collection's text index covers prose_summary and lens_prose.

        MongoDB allows one text index per collection. An existing text index with
        other fields (e.g. a prose_summary-only index created by an older indexer or
        by the web app) is dropped and replaced.

        Returns:
            "exists" if the right index was already there, otherwise "created"
        """
        coll = self.client[database][collection]
        try:
            for index in coll.list_indexes():
                if "_fts" not in index.get("key", {}):
                    continue
                if set(index.get("weights", {})) == set(TEXT_INDEX_WEIGHTS):
                    return "exists"
                self.log.info(
                    "replacing_text_index",
                    database=database,
                    collection=collection,
                    old=index["name"],
                    old_fields=sorted(index.get("weights", {})),
                )
                coll.drop_index(index["name"])
            coll.create_index(
                [(field, "text") for field in TEXT_INDEX_WEIGHTS],
                weights=TEXT_INDEX_WEIGHTS,
                name=TEXT_INDEX_NAME,
            )
        except (OperationFailure, PyMongoError) as e:
            raise MongoStoreError(f"Text index setup failed on {database}.{collection}: {e}") from e
        self.log.info("text_index_created", database=database, collection=collection)
        return "created"

    def text_search(
        self,
        database: str,
        collection: str,
        search_text: str,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Perform a ``$text`` search on a collection."""
        try:
            cursor = self.client[database][collection].find(
                {"$text": {"$search": search_text}}
            ).limit(limit)
            documents = list(cursor)
        except PyMongoError as e:
            raise MongoStoreError(f"Text search failed: {e}") from e

        self.log.info(
            "text_search_complete",
            database=database,
            collection=collection,
            result_count=len(documents),
        )
        return documents
