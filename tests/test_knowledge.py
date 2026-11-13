"""知识库子智能体的测试：解析、切分、检索、子智能体作答、工具接入、向量检索与降级。"""
from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import zipfile

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tests.fakes import FakeLLM, FakeVLM  # noqa: E402
from vva.agent.toolkit import ToolSet  # noqa: E402
from vva.core.bus import EventBus  # noqa: E402
from vva.knowledge import KnowledgeBase  # noqa: E402
from vva.knowledge.parsing import extract_sections, split_text  # noqa: E402
from vva.knowledge.retriever import tokenize  # noqa: E402

SOP = """# 设备间巡检规范
一、压力表
压力表读数正常范围为 0.2~0.5MPa。读数超过 0.6MPa 属于超压，应立即关闭进气阀并通知值班长。
二、人员进出
22:00 至次日 7:00 禁止无关人员进入设备间。进入须佩戴安全帽并登记。
三、消防
灭火器每月检查一次，压力指针应在绿区。
"""

MANUAL = """空压机使用手册
空压机启动前检查油位，油位应在视窗中线以上。
运行中出现异响应停机检查皮带张紧度。
"""


def _docx(paragraphs: list[str]) -> bytes:
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f'<w:p><w:r><w:t>{p}</w:t></w:r></w:p>' for p in paragraphs)
    xml = f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{ns}"><w:body>{body}</w:body></w:document>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


class FakeEmbedder:
    """按关键词出现与否生成向量，足以验证向量检索和融合逻辑。"""
    VOCAB = ["压力", "超压", "人员", "夜间", "灭火器", "空压机", "油位", "异响"]

    def __init__(self, fail=False):
        self.fail, self.calls = fail, 0

    async def embed(self, texts):
        self.calls += 1
        if self.fail:
            raise RuntimeError("embedding 服务不可用")
        return [[1.0 if w in t else 0.0 for w in self.VOCAB] + [0.01] for t in texts]


def _kb(tmp_path, llm=None, embedder=None, **cfg):
    return KnowledgeBase({"dir": str(tmp_path / "kb"), "chunk_chars": 120, **cfg}, llm or FakeLLM([]),
                         embedder, EventBus())


def test_tokenize_and_parsing():
    assert tokenize("压力表 0.6MPa") == ["压", "压力", "力", "力表", "表", "0.6", "mpa"]
    secs = extract_sections("规范.docx", _docx(["一、压力表", "读数正常范围为 0.2~0.5MPa。"]))
    assert "读数正常范围" in secs[0][1]
    assert extract_sections("a.txt", "中文".encode("gbk"))[0][1] == "中文"
    with pytest.raises(ValueError):
        extract_sections("a.xlsx", b"x")
    chunks = split_text(extract_sections("a.md", SOP.encode()), max_chars=120)
    assert [c.position for c in chunks] == ["一、压力表", "二、人员进出", "三、消防"]


def test_index_search_and_answer(tmp_path):
    async def run():
        llm = FakeLLM(["读数 0.62MPa 超过 0.6MPa，属于超压，应关闭进气阀并通知值班长 [1]。"])
        kb = _kb(tmp_path, llm)
        assert kb.tools.schemas() == []                                  # 知识库为空时不提供工具
        kb.add_document("巡检规范.md", SOP.encode())
        kb.add_document("空压机手册.docx", _docx(MANUAL.splitlines()))
        await kb.wait_idle()
        assert [d.status for d in kb.list_documents()] == ["ready", "ready"]
        hits = await kb.retriever.search("压力表多少算超压")
        assert hits[0].chunk.source == "《巡检规范.md》一、压力表"
        hits = await kb.retriever.search("空压机油位")
        assert hits[0].chunk.doc_name == "空压机手册.docx"
        tools = ToolSet(kb.tools)
        r = json.loads(await tools.call("ask_knowledge", {"question": "压力表读数正常吗", "context": "读数 0.62MPa"}))
        assert r["found"] and r["sources"] == ["《巡检规范.md》一、压力表"] and "[1]" not in r["answer"]
        prompt = llm.calls[0][1]["content"]
        assert "现场情况：读数 0.62MPa" in prompt and "[1] 《巡检规范.md》一、压力表" in prompt
        assert "不超过 150 个字" in llm.calls[0][0]["content"]
        assert kb.stats() == {"documents": 2, "chunks": kb.stats()["chunks"], "vector_search": False}
        await kb.close()
    asyncio.run(run())


def test_not_found_and_errors(tmp_path):
    async def run():
        llm = FakeLLM(["知识库中没有相关规定。"])
        kb = _kb(tmp_path, llm)
        kb.add_document("巡检规范.md", SOP.encode())
        kb.add_document("空.txt", b"   ")
        await kb.wait_idle()
        assert kb.list_documents()[1].status == "error"
        r = await kb.agent.answer("食堂几点开饭")
        assert r == {"found": False, "answer": "知识库中没有相关规定", "sources": []}   # 关键词完全不命中：不调用模型
        assert llm.calls == []
        r = await kb.agent.answer("压力表要多久校验一次")
        assert not r["found"] and r["sources"] == []
        with pytest.raises(ValueError):
            kb.add_document("表格.xlsx", b"x")
        assert kb.delete_document(1) and (await kb.retriever.search("压力表")) == []
        await kb.close()
    asyncio.run(run())


def test_vector_search_and_fallback(tmp_path):
    async def run():
        emb = FakeEmbedder()
        kb = _kb(tmp_path, embedder=emb)
        kb.add_document("巡检规范.md", SOP.encode())
        await kb.wait_idle()
        assert kb.stats()["vector_search"] and all(c.embedding is not None for c in kb.store.all_chunks())
        hits = await kb.retriever.search("超压怎么办")                     # 向量与关键词融合
        assert hits[0].chunk.position == "一、压力表"
        await kb.close()
        # 向量服务不可用：照常入库，只用关键词检索；之后服务恢复，重启时自动补向量
        bad = _kb(tmp_path / "b", embedder=FakeEmbedder(fail=True))
        bad.add_document("巡检规范.md", SOP.encode())
        await bad.wait_idle()
        assert bad.list_documents()[0].status == "ready"
        assert (await bad.retriever.search("压力表"))[0].chunk.position == "一、压力表"
        await bad.close()
        good = _kb(tmp_path / "b", embedder=FakeEmbedder())
        await good.background_tasks()[0]
        assert all(c.embedding is not None for c in good.store.all_chunks())
        await good.close()
    asyncio.run(run())


def test_app_exposes_knowledge_tool(tmp_path):
    async def run():
        from vva.app import App
        from vva.core.config import load_config
        cfg = load_config(None)
        cfg["video"]["type"] = "push"
        cfg["observer"]["enabled"] = False
        for k in ("memory", "scene", "knowledge"):
            cfg[k]["dir"] = str(tmp_path / k)
        llm = FakeLLM([[("ask_knowledge", {"question": "夜间能进设备间吗"})], "夜间禁止进入 [1]。", "按《巡检规范》，夜间禁止进入。"])
        app = App(cfg, llm=llm, vlm=FakeVLM(), enable_audio=False)
        app.knowledge.add_document("巡检规范.md", SOP.encode())
        await app.knowledge.wait_idle()
        await app.start()
        reply = await app.ask("晚上能进设备间吗", "web")
        assert reply.text == "按《巡检规范》，夜间禁止进入。"
        system = llm.calls[0][0]["content"]
        assert "ask_knowledge" in system
        tool_msg = next(m for m in app.conversations.get("web").messages if m["role"] == "tool")
        assert json.loads(tool_msg["content"])["sources"] == ["《巡检规范.md》二、人员进出"]
        assert app.status()["knowledge"]["documents"] == 1
        await app.stop()
    asyncio.run(run())


def test_embedding_vectors_are_float32(tmp_path):
    async def run():
        kb = _kb(tmp_path, embedder=FakeEmbedder())
        kb.add_document("手册.txt", MANUAL.encode())
        await kb.wait_idle()
        assert all(c.embedding.dtype == np.float32 for c in kb.store.all_chunks())
        await kb.close()
    asyncio.run(run())


def test_overview_payload_keeps_document_list(tmp_path):
    async def run():
        kb = _kb(tmp_path)
        kb.add_document("巡检规范.md", SOP.encode())
        await kb.wait_idle()
        data = kb.overview()
        assert isinstance(data["documents"], list) and data["documents"][0]["name"] == "巡检规范.md"
        assert data["stats"]["documents"] == 1 and ".pdf" in data["supported"]
        json.dumps(data, ensure_ascii=False)                                # 可直接作为接口返回
        await kb.close()
    asyncio.run(run())
