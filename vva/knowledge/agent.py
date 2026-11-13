"""知识库子智能体：接到主智能体的问题后，自己检索、阅读资料片段、给出带出处的结论。

主智能体只拿到一句结论和出处，不需要把大段资料放进自己的上下文：
既省上下文（8B 模型只有 16K），也避免主智能体被资料原文带偏。
"""
from __future__ import annotations

import re
from typing import Any

from vva.agent import prompts
from vva.knowledge.retriever import Hit, Retriever
from vva.memory.summarizer import complete_text

_CITE_RE = re.compile(r"\[(\d+)\]")
NOT_FOUND = "知识库中没有相关规定"


class KnowledgeAgent:
    def __init__(self, retriever: Retriever, llm: Any, top_k: int = 4, max_answer_chars: int = 150,
                 excerpt_chars: int = 600, **_: Any) -> None:
        self.retriever, self.llm = retriever, llm
        self.top_k = int(top_k)
        self.max_chars = int(max_answer_chars)
        self.excerpt_chars = int(excerpt_chars)

    async def retrieve(self, question: str, context: str = "") -> list[Hit]:
        hits = await self.retriever.search(question, self.top_k)
        if context:     # 再用“问题 + 现场情况”检索一次，补充与现场相关的条款
            seen = {h.chunk.id for h in hits}
            hits += [h for h in await self.retriever.search(f"{question} {context}", self.top_k)
                     if h.chunk.id not in seen]
        return hits[:self.top_k + 2]

    async def answer(self, question: str, context: str = "") -> dict[str, Any]:
        hits = await self.retrieve(question, context)
        if not hits:
            return {"found": False, "answer": NOT_FOUND, "sources": []}
        excerpts = "\n\n".join(f"[{i}] {h.chunk.source}\n{h.chunk.text[:self.excerpt_chars]}"
                               for i, h in enumerate(hits, 1))
        ctx = f"现场情况：{context}\n" if context else ""
        text = await complete_text(self.llm, [
            {"role": "system", "content": prompts.KNOWLEDGE_SYSTEM.format(max_chars=self.max_chars)},
            {"role": "user", "content": prompts.KNOWLEDGE_PROMPT.format(excerpts=excerpts, context=ctx,
                                                                         question=question)}])
        cited = sorted({int(n) for n in _CITE_RE.findall(text) if 1 <= int(n) <= len(hits)})
        found = bool(text) and NOT_FOUND not in text
        return {"found": found, "answer": _CITE_RE.sub("", text).strip() or NOT_FOUND,
                "sources": list(dict.fromkeys(hits[n - 1].chunk.source for n in cited)) if found else []}
