"""Raw-evidence register (Phase 2.1).

Every payload Phase 2 fetches is stored once, byte for byte, and registered:

  * content-addressed: the object key is derived from the SHA-256 of the
    original bytes, so identical content is stored once and a key can never
    point at different bytes;
  * gzip with mtime=0, so the stored object is itself deterministic;
  * uploaded BEFORE its row is inserted, so a row never points at nothing
    (a crash in between leaves an unreferenced object, which is harmless);
  * one row per (source, source_record_id, sha256): when a source changes a
    file, the new bytes become a new row - a new version - and the old one
    stays. Rows are immutable at the database level (migration 0003).

read() re-hashes what comes back, so corrupted or substituted storage is
detected rather than trusted.
"""
from __future__ import annotations

import gzip
import hashlib
import re
from dataclasses import dataclass

MAX_DOCUMENT_BYTES = 25 * 1024 * 1024
SOURCE_SHAPE = re.compile(r"^[a-z0-9_]+$")


class EvidenceError(RuntimeError):
    """A document could not be stored or did not verify."""


@dataclass(frozen=True)
class StoredDocument:
    doc_id: int
    sha256: str
    storage_key: str
    new: bool


def storage_key(source: str, sha256: str) -> str:
    return f"{source}/{sha256[:2]}/{sha256}.gz"


class EvidenceStore:
    def __init__(self, conn, blobs):
        self.conn = conn
        self.blobs = blobs

    def put(self, *, source: str, source_record_id: str, body: bytes, url: str | None = None,
            content_type: str | None = None, source_last_modified: str | None = None,
            run_id: int | None = None) -> StoredDocument:
        if not SOURCE_SHAPE.match(source or ""):
            raise EvidenceError(f"invalid source name {source!r}")
        if not source_record_id or len(source_record_id) > 200:
            raise EvidenceError("source_record_id must be 1-200 characters")
        if len(body) > MAX_DOCUMENT_BYTES:
            raise EvidenceError(f"document larger than {MAX_DOCUMENT_BYTES} bytes refused")

        sha = hashlib.sha256(body).hexdigest()
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT doc_id, storage_key FROM source_documents"
                " WHERE source = %s AND source_record_id = %s AND sha256 = %s",
                (source, source_record_id, sha),
            )
            row = cur.fetchone()
        if row:
            self.conn.rollback()
            return StoredDocument(row[0], sha, row[1], new=False)

        key = storage_key(source, sha)
        self.blobs.put_if_absent(key, gzip.compress(body, mtime=0))
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO source_documents (source, source_record_id, url, sha256, bytes,
                    content_type, source_last_modified, storage_key, run_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (source, source_record_id, sha256) DO NOTHING
                RETURNING doc_id
                """,
                (source, source_record_id, url, sha, len(body), content_type,
                 source_last_modified, key, run_id),
            )
            inserted = cur.fetchone()
            if inserted is None:  # a concurrent writer registered it first
                cur.execute(
                    "SELECT doc_id FROM source_documents"
                    " WHERE source = %s AND source_record_id = %s AND sha256 = %s",
                    (source, source_record_id, sha),
                )
                inserted = cur.fetchone()
        self.conn.commit()
        return StoredDocument(inserted[0], sha, key, new=True)

    def read(self, doc_id: int) -> bytes:
        """Original bytes of a registered document, verified against its hash."""
        with self.conn.cursor() as cur:
            cur.execute("SELECT sha256, storage_key FROM source_documents WHERE doc_id = %s", (doc_id,))
            row = cur.fetchone()
        self.conn.rollback()
        if row is None:
            raise EvidenceError(f"no document {doc_id}")
        sha, key = row
        body = gzip.decompress(self.blobs.get(key))
        if hashlib.sha256(body).hexdigest() != sha:
            raise EvidenceError(f"document {doc_id}: stored bytes do not match their SHA-256")
        return body

    def versions(self, source: str, source_record_id: str) -> list[tuple[int, str]]:
        """(doc_id, sha256) for every version seen, oldest first."""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT doc_id, sha256 FROM source_documents"
                " WHERE source = %s AND source_record_id = %s ORDER BY fetched_at, doc_id",
                (source, source_record_id),
            )
            rows = cur.fetchall()
        self.conn.rollback()
        return rows
