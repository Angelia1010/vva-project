"""主智能体调用知识库子智能体的工具。"""
from __future__ import annotations

from vva.agent.toolkit import ToolProvider, fn_schema as _fn
from vva.core.bus import EventBus
from vva.knowledge.agent import KnowledgeAgent
from vva.knowledge.retriever import Retriever


class KnowledgeTools(ToolProvider):
    def __init__(self, agent: KnowledgeAgent, retriever: Retriever, bus: EventBus) -> None:
        super().__init__(bus)
        self.agent, self.retriever = agent, retriever

    def schemas(self) -> list[dict]:
        if self.retriever.size == 0:      # 知识库为空时不提供这个工具
            return []
        return [_fn("ask_knowledge",
                    "查询领域知识库（规章制度、操作规程、设备手册、判断标准等），返回基于资料的结论和出处。"
                    "问到“是否正常/合规”“规定是什么”“怎么处理”时使用。",
                    {"question": {"type": "string", "description": "要查的问题，如“压力表读数多少算异常”"},
                     "context": {"type": "string",
                                 "description": "可选。已经从画面看到的现场情况，如“压力表读数 0.62MPa”，用于让知识库直接给出判断"}},
                    ["question"])]

    async def tool_ask_knowledge(self, question: str, context: str = "") -> dict:
        return await self.agent.answer(question, context or "")
