"""领域知识库子智能体：上传规章、规程、手册等文档，监控助手回答时据此判断“是否正常、怎么处理”。

  parsing.py    文档解析（txt / md / pdf / docx）与切分
  store.py      SQLite 存储（文档、段落、向量）
  retriever.py  关键词 + 向量混合检索
  agent.py      子智能体：检索 → 阅读片段 → 带出处的结论
  tools.py      主智能体调用的 ask_knowledge 工具

对外入口：
    kb = KnowledgeBase(cfg["knowledge"], llm, embedder, bus)
    tools = ToolSet(..., kb.tools)
    tasks = kb.background_tasks()
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Coroutine

from vva.core.bus import EventBus
from vva.knowledge.agent import KnowledgeAgent
from vva.knowledge.parsing import SUPPORTED, extract_sections, split_text
from vva.knowledge.retriever import Retriever
from vva.knowledge.store import Document, KnowledgeStore
from vva.knowledge.tools import KnowledgeTools

log = logging.getLogger(__name__)

__all__ = ["KnowledgeBase", "KnowledgeAgent", "Retriever", "KnowledgeStore", "SUPPORTED"]


class KnowledgeBase:
    def __init__(self, cfg: dict[str, Any], llm: Any, embedder: Any, bus: EventBus) -> None:
        root = cfg.get("dir") or "data/knowledge"
        os.makedirs(root, exist_ok=True)
        self.cfg = cfg
        self.embedder = embedder
        self.store = KnowledgeStore(root)
        self.retriever = Retriever(self.store, embedder)
        self.agent = KnowledgeAgent(self.retriever, llm, **cfg)
        self.tools = KnowledgeTools(self.agent, self.retriever, bus)
        self.chunk_chars = int(cfg.get("chunk_chars", 400))
        self.max_file_mb = float(cfg.get("max_file_mb", 50))
        self._tasks: set[asyncio.Task] = set()

    # ---------------------------------------------------------------- 文档管理
    def add_document(self, name: str, data: bytes) -> Document:
        """保存文档并在后台建立索引，立即返回（状态为 indexing）。"""
        name = os.path.basename(name or "").strip()
        if not name.lower().endswith(SUPPORTED):
            raise ValueError(f"不支持的文件格式（支持 {'、'.join(SUPPORTED)}）")
        if len(data) > self.max_file_mb * 1024 * 1024:
            raise ValueError(f"文件超过 {self.max_file_mb:g}MB")
        doc = self.store.add_document(name, data)
        task = asyncio.create_task(self.index(doc.id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return doc

    async def index(self, doc_id: int) -> None:
        doc = self.store.get_document(doc_id)
        if doc is None:
            return
        self.store.set_status(doc_id, "indexing")
        try:
            data = self.store.read_file(doc)
            sections = await asyncio.to_thread(extract_sections, doc.name, data)
            chunks = split_text(sections, self.chunk_chars)
            if not chunks:
                raise ValueError("没有提取到文字（扫描版 PDF 需要先做文字识别）")
            embs: list[Any] = [None] * len(chunks)
            if self.embedder is not None:
                try:
                    embs = await self.embedder.embed([c.position + "\n" + c.text for c in chunks])
                except Exception as e:      # 向量服务不可用时退回关键词检索，不影响入库
                    log.warning("文档 %s 向量化失败，仅使用关键词检索：%s", doc.name, e)
            self.store.replace_chunks(doc_id, [(c.text, c.position, e) for c, e in zip(chunks, embs, strict=True)])
            log.info("知识库已入库：%s，%d 段", doc.name, len(chunks))
        except Exception as e:
            log.warning("知识库文档 %s 处理失败：%s", doc.name, e)
            self.store.set_status(doc_id, "error", str(e))

    def delete_document(self, doc_id: int) -> bool:
        return self.store.delete_document(doc_id)

    def list_documents(self) -> list[Document]:
        return self.store.list_documents()

    async def wait_idle(self) -> None:
        """等待后台索引完成（测试和管理接口用）。"""
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # ---------------------------------------------------------------- 后台
    async def _resume(self) -> None:
        """启动时补做：上次中断的索引；新配置了向量模型后，给旧文档补向量。"""
        todo = {d.id for d in self.store.list_documents() if d.status == "indexing"}
        if self.embedder is not None:
            todo |= set(self.store.documents_missing_embeddings())
        for doc_id in sorted(todo):
            await self.index(doc_id)

    def background_tasks(self) -> list[Coroutine[Any, Any, None]]:
        return [self._resume()]

    def overview(self) -> dict[str, Any]:
        """管理页用：文档清单 + 统计。"""
        return {"documents": [d.to_dict() for d in self.list_documents()], "stats": self.stats(),
                "supported": list(SUPPORTED)}

    def stats(self) -> dict[str, Any]:
        docs = self.store.list_documents()
        return {"documents": len(docs), "chunks": sum(d.chunks for d in docs),
                "vector_search": self.embedder is not None}

    async def close(self) -> None:
        await self.wait_idle()
        await asyncio.to_thread(self.store.close)
