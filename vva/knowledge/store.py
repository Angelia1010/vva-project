"""知识库存储（SQLite）：文档清单 + 切分后的段落（含可选的向量）。原文件另存一份，便于重建索引。"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any

import numpy as np

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT NOT NULL,
    filename TEXT NOT NULL,
    size     INTEGER NOT NULL,
    chunks   INTEGER NOT NULL DEFAULT 0,
    status   TEXT NOT NULL,          -- indexing | ready | error
    error    TEXT,
    created  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id    INTEGER NOT NULL,
    idx       INTEGER NOT NULL,
    text      TEXT NOT NULL,
    position  TEXT,
    embedding BLOB
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
"""


@dataclass
class Document:
    id: int
    name: str
    filename: str
    size: int
    chunks: int
    status: str
    error: str | None
    created: float

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "created": time.strftime("%Y-%m-%d %H:%M", time.localtime(self.created))}


@dataclass
class StoredChunk:
    id: int
    doc_id: int
    doc_name: str
    text: str
    position: str
    embedding: np.ndarray | None = None

    @property
    def source(self) -> str:
        return f"《{self.doc_name}》" + (self.position or "")


class KnowledgeStore:
    def __init__(self, root: str) -> None:
        self.root = root
        self.files_dir = os.path.join(root, "files")
        os.makedirs(self.files_dir, exist_ok=True)
        self.path = os.path.join(root, "knowledge.db")
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(_SCHEMA)
            self._db.commit()
        self.version = 0            # 每次内容变化加一，检索器据此重建索引

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur

    def _query(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    # ---------------------------------------------------------------- 文档
    def add_document(self, name: str, data: bytes) -> Document:
        cur = self._exec("INSERT INTO documents(name, filename, size, status, created) VALUES (?,?,?,?,?)",
                         (name, "", len(data), "indexing", time.time()))
        doc_id = int(cur.lastrowid)
        filename = f"{doc_id}_{os.path.basename(name)}"
        with open(os.path.join(self.files_dir, filename), "wb") as f:
            f.write(data)
        self._exec("UPDATE documents SET filename = ? WHERE id = ?", (filename, doc_id))
        return self.get_document(doc_id)

    def get_document(self, doc_id: int) -> Document | None:
        rows = self._query("SELECT * FROM documents WHERE id = ?", (doc_id,))
        return Document(**dict(rows[0])) if rows else None

    def list_documents(self) -> list[Document]:
        return [Document(**dict(r)) for r in self._query("SELECT * FROM documents ORDER BY id")]

    def read_file(self, doc: Document) -> bytes:
        with open(os.path.join(self.files_dir, doc.filename), "rb") as f:
            return f.read()

    def set_status(self, doc_id: int, status: str, error: str | None = None) -> None:
        self._exec("UPDATE documents SET status = ?, error = ? WHERE id = ?", (status, error, doc_id))

    def delete_document(self, doc_id: int) -> bool:
        doc = self.get_document(doc_id)
        if doc is None:
            return False
        with self._lock:
            self._db.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
            self._db.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
            self._db.commit()
        try:
            os.remove(os.path.join(self.files_dir, doc.filename))
        except OSError:
            pass
        self.version += 1
        return True

    # ---------------------------------------------------------------- 段落
    def replace_chunks(self, doc_id: int, items: list[tuple[str, str, np.ndarray | None]]) -> None:
        """写入一个文档的全部段落：[(文本, 出处位置, 向量或 None)]。"""
        rows = [(doc_id, i, text, pos, None if emb is None else np.asarray(emb, np.float32).tobytes())
                for i, (text, pos, emb) in enumerate(items)]
        with self._lock:
            self._db.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
            self._db.executemany("INSERT INTO chunks(doc_id, idx, text, position, embedding) VALUES (?,?,?,?,?)", rows)
            self._db.execute("UPDATE documents SET chunks = ?, status = 'ready', error = NULL WHERE id = ?",
                             (len(rows), doc_id))
            self._db.commit()
        self.version += 1

    def all_chunks(self) -> list[StoredChunk]:
        rows = self._query("SELECT c.id, c.doc_id, d.name, c.text, c.position, c.embedding FROM chunks c "
                           "JOIN documents d ON d.id = c.doc_id WHERE d.status = 'ready' ORDER BY c.doc_id, c.idx")
        return [StoredChunk(r["id"], r["doc_id"], r["name"], r["text"], r["position"] or "",
                            np.frombuffer(r["embedding"], np.float32) if r["embedding"] else None) for r in rows]

    def documents_missing_embeddings(self) -> list[int]:
        rows = self._query("SELECT DISTINCT doc_id FROM chunks WHERE embedding IS NULL")
        return [r["doc_id"] for r in rows]
