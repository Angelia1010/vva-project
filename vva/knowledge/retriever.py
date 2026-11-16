"""检索：关键词（BM25，中文按字二元组切词，不需要分词库）+ 可选的向量检索，两路结果用排名融合（RRF）。

没有部署向量模型时只用关键词检索；对专业术语、型号、数值这类问题，关键词检索本身就比较可靠。
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

import numpy as np

from vva.knowledge.store import KnowledgeStore, StoredChunk

_WORD_RE = re.compile(r"[a-zA-Z]+|\d+(?:\.\d+)?|[一-鿿]")


def tokenize(text: str) -> list[str]:
    """英文按单词、数字按整数/小数、中文按单字和相邻二字组。"""
    toks = _WORD_RE.findall(text.lower())
    out: list[str] = []
    for i, t in enumerate(toks):
        out.append(t)
        if len(t) == 1 and "一" <= t <= "鿿" and i + 1 < len(toks) and "一" <= toks[i + 1] <= "鿿":
            out.append(t + toks[i + 1])
    return out


@dataclass
class Hit:
    chunk: StoredChunk
    score: float


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.tfs = [Counter(d) for d in docs]
        self.lens = [len(d) for d in docs]
        self.avg = sum(self.lens) / max(1, len(docs))
        df: Counter[str] = Counter()
        for tf in self.tfs:
            df.update(tf.keys())
        n = len(docs)
        self.idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

    def scores(self, query: list[str]) -> list[float]:
        out = []
        q = [t for t in set(query) if t in self.idf]
        for tf, ln in zip(self.tfs, self.lens, strict=True):
            s = 0.0
            for t in q:
                f = tf.get(t, 0)
                if f:
                    s += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * ln / self.avg))
            out.append(s)
        return out


class Retriever:
    def __init__(self, store: KnowledgeStore, embedder: Any = None, rrf_k: int = 60) -> None:
        self.store, self.embedder = store, embedder
        self.rrf_k = rrf_k
        self._version = -1
        self._chunks: list[StoredChunk] = []
        self._bm25: BM25 | None = None
        self._matrix: np.ndarray | None = None

    def _refresh(self) -> None:
        if self._version == self.store.version and self._bm25 is not None:
            return
        self._chunks = self.store.all_chunks()
        self._bm25 = BM25([tokenize(c.position + " " + c.text) for c in self._chunks])
        embs = [c.embedding for c in self._chunks]
        if self._chunks and all(e is not None for e in embs):
            m = np.stack(embs)
            self._matrix = m / (np.linalg.norm(m, axis=1, keepdims=True) + 1e-9)
        else:
            self._matrix = None
        self._version = self.store.version

    @property
    def size(self) -> int:
        self._refresh()
        return len(self._chunks)

    async def search(self, query: str, top_k: int = 4) -> list[Hit]:
        self._refresh()
        if not self._chunks or not query.strip():
            return []
        kw = self._bm25.scores(tokenize(query))
        ranked_kw = [i for i in sorted(range(len(kw)), key=lambda i: -kw[i]) if kw[i] > 0]
        ranked_vec: list[int] = []
        if self.embedder is not None and self._matrix is not None:
            q = np.asarray((await self.embedder.embed([query]))[0], np.float32)
            sims = self._matrix @ (q / (np.linalg.norm(q) + 1e-9))
            ranked_vec = [int(i) for i in np.argsort(-sims)[:max(top_k * 3, 10)]]
        fused: dict[int, float] = {}
        for ranking in (ranked_kw, ranked_vec):
            for r, i in enumerate(ranking):
                fused[i] = fused.get(i, 0.0) + 1.0 / (self.rrf_k + r + 1)
        best = sorted(fused, key=lambda i: -fused[i])[:top_k]
        return [Hit(self._chunks[i], fused[i]) for i in best]
