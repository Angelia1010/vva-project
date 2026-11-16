"""知识库管理接口（网页 /knowledge 页面使用）。

  GET    /api/knowledge                  文档列表与统计
  POST   /api/knowledge?name=<文件名>     上传文档（请求体为文件内容），后台建立索引
  DELETE /api/knowledge/{id}             删除文档
  POST   /api/knowledge/{id}/reindex     重新建立索引
  GET    /api/knowledge/search?q=        只检索，查看命中的段落（调试用）
  POST   /api/knowledge/ask              {"question", "context"} 直接问子智能体（调试用）

配置了 web.admin_token 时，修改类接口需要在请求头 X-Token 中携带。
"""
from __future__ import annotations

import asyncio
import os

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from vva.knowledge import KnowledgeBase


def register_knowledge_routes(api: FastAPI, kb: KnowledgeBase, admin_token: str, static_dir: str) -> None:
    def check(request: Request) -> None:
        if admin_token and request.headers.get("X-Token") != admin_token:
            raise HTTPException(401, "需要管理口令")

    def listing() -> JSONResponse:
        return JSONResponse({**kb.overview(), "token_required": bool(admin_token)})

    @api.get("/knowledge")
    async def knowledge_page() -> FileResponse:
        return FileResponse(os.path.join(static_dir, "knowledge.html"))

    @api.get("/api/knowledge")
    async def list_docs() -> JSONResponse:
        return listing()

    @api.post("/api/knowledge")
    async def upload(request: Request) -> JSONResponse:
        check(request)
        name = request.query_params.get("name") or ""
        try:
            kb.add_document(name, await request.body())
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return listing()

    @api.delete("/api/knowledge/{doc_id}")
    async def delete(request: Request, doc_id: int) -> JSONResponse:
        check(request)
        if not kb.delete_document(doc_id):
            raise HTTPException(404, "没有这个文档")
        return listing()

    @api.post("/api/knowledge/{doc_id}/reindex")
    async def reindex(request: Request, doc_id: int) -> JSONResponse:
        check(request)
        if kb.store.get_document(doc_id) is None:
            raise HTTPException(404, "没有这个文档")
        asyncio.create_task(kb.index(doc_id))
        return listing()

    @api.get("/api/knowledge/search")
    async def search(q: str = "") -> JSONResponse:
        hits = await kb.retriever.search(q, kb.agent.top_k)
        return JSONResponse([{"source": h.chunk.source, "score": round(h.score, 4), "text": h.chunk.text}
                             for h in hits])

    @api.post("/api/knowledge/ask")
    async def ask(body: dict) -> JSONResponse:
        question = str(body.get("question") or "").strip()
        if not question:
            raise HTTPException(400, "问题不能为空")
        return JSONResponse(await kb.agent.answer(question, str(body.get("context") or "")))
