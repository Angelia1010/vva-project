"""技能（Skill）的测试：SKILL.md 读写、按需加载、专用工具可见性、参考图对照、观察器、旧场景包迁移、App 接入。"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import numpy as np
import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tests.fakes import FakeLLM, FakeVLM  # noqa: E402
from vva.agent.agent import Agent  # noqa: E402
from vva.agent.toolkit import ToolSet  # noqa: E402
from vva.core.bus import EventBus  # noqa: E402
from vva.core.types import Frame  # noqa: E402
from vva.perception.imaging import encode_jpeg  # noqa: E402
from vva.perception.observer import Observer  # noqa: E402
from vva.perception.timeline import Timeline, WatchRules  # noqa: E402
from vva.skills import SkillManager, SkillTools, parse_skill_md  # noqa: E402
from vva.video.frame_buffer import FrameBuffer  # noqa: E402

PLUGIN = '''
from vva.skills.plugin import SkillToolProvider, fn_schema

class Tools(SkillToolProvider):
    def schemas(self):
        return [fn_schema("pressure_status", "判断压力", {"mpa": {"type": "number"}}, ["mpa"])]

    async def tool_pressure_status(self, mpa):
        return {"status": "超压" if float(mpa) > 0.6 else "正常", "skill": self.ctx.skill}
'''


def _img(v=100):
    return np.full((360, 640, 3), v, np.uint8)


def _jpeg(v=120):
    return encode_jpeg(_img(v))


def _buf():
    b = FrameBuffer()
    b.add(Frame(image=_img(), ts=time.time()))
    return b


def _mgr(tmp_path, **cfg):
    return SkillManager({"dir": str(tmp_path / "skills"), **cfg})


def _seed(mgr):
    st = mgr.store
    st.create("现场概况", "现场基本情况", "设备间，左侧空压机，右上方压力表。", always=True)
    st.create("设备间读表", "问到压力表读数、是否超压时使用", "## 方法\n先对照参考图，再读数，再调用 pressure_status。",
              hotwords=["兆帕"], rules=[{"condition": "压力表超过 0.6 兆帕", "cooldown_s": 600}])
    st.save_object("设备间读表", "压力表", ["表盘"], "白色圆表", "0.2~0.5", "超过 0.6")
    st.add_image("设备间读表", "压力表", _jpeg(10))
    st.create("访客管理", "问到访客、陌生人时使用", "访客应佩戴红色挂绳工牌。", enabled=False)
    with open(os.path.join(st.root, st.get("设备间读表").dirname, "tools.py"), "w", encoding="utf-8") as f:
        f.write(PLUGIN)
    st.reload()


def test_skill_md_roundtrip_and_store(tmp_path):
    mgr = _mgr(tmp_path)
    _seed(mgr)
    with open(os.path.join(mgr.store.root, "设备间读表", "SKILL.md"), encoding="utf-8") as f:
        meta, body = parse_skill_md(f.read())
    assert meta["description"] == "问到压力表读数、是否超压时使用" and meta["rules"][0]["cooldown_s"] == 600
    assert body.startswith("## 方法")
    s = SkillManager({"dir": str(tmp_path / "skills")}).store.get("设备间读表")     # 重新读取
    assert s.objects["压力表"].images == ["ref_01.jpg"] and s.has_tools and s.hotwords == ["兆帕"]
    mgr.store.update("访客管理", enabled=True, cameras=["cam9"])
    assert [x.name for x in mgr.active()] == ["现场概况", "设备间读表"]          # cam9 不是当前摄像头
    with pytest.raises(ValueError):
        mgr.store.create("访客管理")
    assert mgr.store.delete("访客管理") and "访客管理" not in mgr.store.skills


def test_prompt_shows_catalog_and_always_body(tmp_path):
    mgr = _mgr(tmp_path)
    _seed(mgr)
    block = mgr.prompt_block()
    assert "### 现场概况\n设备间，左侧空压机" in block                            # 常驻：正文直接带上
    assert "- 设备间读表：问到压力表读数、是否超压时使用（目标：压力表）" in block    # 其他：只有目录
    assert "先对照参考图" not in block and "访客管理" not in block               # 未加载的正文、停用的技能不出现
    assert mgr.asr_prompt() == "兆帕、压力表、表盘"
    rules = WatchRules()
    assert mgr.apply_rules(rules) == 1 and mgr.apply_rules(rules) == 0
    assert mgr.observer_hint() == "设备间，左侧空压机，右上方压力表。"


def test_load_skill_reveals_plugin_tools(tmp_path):
    async def run():
        mgr = _mgr(tmp_path)
        _seed(mgr)
        bus = EventBus()
        mgr.load_plugins(_buf(), FakeVLM(), bus)
        tools = ToolSet(SkillTools(mgr, _buf(), FakeVLM(), bus))
        llm = FakeLLM([[("load_skill", {"name": "设备间读表"})],
                       [("pressure_status", {"mpa": 0.62})], "读数 0.62 兆帕，超压。",
                       "好。"])
        agent = Agent(llm, tools)
        agent.tool_visible = mgr.tool_visible
        visible = lambda: {s["function"]["name"] for s in agent._schemas(agent.default)}  # noqa: E731
        assert "pressure_status" not in visible() and {"load_skill", "compare_with_reference"} <= visible()
        await agent.run("压力表正常吗", lambda _: None)
        msgs = agent.history
        loaded = json.loads(next(m["content"] for m in msgs if m["role"] == "tool"))
        assert loaded["loaded_skill"] == "设备间读表" and "pressure_status" in loaded["tools"]
        assert "先对照参考图" in loaded["instructions"] and loaded["objects"][0].startswith("【压力表】")
        result = json.loads([m for m in msgs if m["role"] == "tool"][1]["content"])
        assert result == {"status": "超压", "skill": "设备间读表"}           # 同一轮里加载后即可调用
        tools_in_round2 = {t["function"]["name"] for t in llm_tools(llm, 1)}
        assert "pressure_status" in tools_in_round2
        assert "pressure_status" in visible()                                # 之后的提问也可用
        agent.reset()
        assert "pressure_status" not in visible()                            # 清空对话后需要重新加载
        r = json.loads(await tools.call("load_skill", {"name": "访客管理"}))
        assert "没有已启用的技能" in r["error"]
    asyncio.run(run())


def llm_tools(llm, i):
    return llm.tool_lists[i]


def test_unloaded_plugin_tool_is_refused(tmp_path):
    async def run():
        mgr = _mgr(tmp_path)
        _seed(mgr)
        bus = EventBus()
        mgr.load_plugins(_buf(), FakeVLM(), bus)
        agent = Agent(FakeLLM([[("pressure_status", {"mpa": 1})], "好。"]), ToolSet(SkillTools(mgr, _buf(), FakeVLM(), bus)))
        agent.tool_visible = mgr.tool_visible
        await agent.run("x", lambda _: None)
        tool_msg = next(m for m in agent.history if m["role"] == "tool")
        assert "需要先用 load_skill" in tool_msg["content"]
    asyncio.run(run())


def test_compare_with_reference_and_observer(tmp_path):
    async def run():
        mgr = _mgr(tmp_path)
        _seed(mgr)
        vlm = FakeVLM(default="依据：x\n结论：读数约 0.3 兆帕\n把握：中")
        tools = SkillTools(mgr, _buf(), vlm, EventBus())
        r = json.loads(await tools.call("compare_with_reference", {"target": "表盘", "question": "读数"}))
        assert r["skill"] == "设备间读表" and r["reference_images"] == 1 and r["answer"] == "读数约 0.3 兆帕"
        assert vlm.requests[-1][0] == 2
        rules = WatchRules()
        rules.add("压力表超过 0.6 兆帕")
        ovlm = FakeVLM(default=json.dumps({"summary": "设备间无人", "people_count": 0}, ensure_ascii=False))
        obs = Observer(_buf(), ovlm, Timeline(), rules, EventBus(), hint_provider=mgr.observer_hint,
                       references=mgr.references_for_text)
        await obs.step()
        n, prompt = ovlm.requests[0]
        assert n == 2 and "图1是“压力表”的参考图1" in prompt and "设备间，左侧空压机" in prompt
    asyncio.run(run())


def test_migrate_legacy_scene(tmp_path):
    scene = tmp_path / "scenes" / "default"
    (scene / "objects" / "保温杯").mkdir(parents=True)
    (scene / "scene.yaml").write_text(yaml.safe_dump({
        "description": "办公室测试环境", "areas": {"左侧": "工位"}, "standards": ["杯子放桌面内侧"],
        "hotwords": ["保温杯"], "rules": [{"condition": "没有人", "cooldown_s": 60}]}, allow_unicode=True), encoding="utf-8")
    (scene / "objects" / "保温杯" / "object.yaml").write_text("name: 保温杯\ndescription: 粉色\n", encoding="utf-8")
    (scene / "objects" / "保温杯" / "ref_01.jpg").write_bytes(_jpeg())
    (tmp_path / "skills" / "示例").mkdir(parents=True)          # 已有示例技能也照样迁移
    (tmp_path / "skills" / "示例" / "SKILL.md").write_text("---\nname: 示例\nenabled: false\n---\n正文", encoding="utf-8")
    mgr = _mgr(tmp_path, legacy_scene_dir=str(scene))
    s = mgr.store.get("现场概况")
    assert s.always and "办公室测试环境" in s.body and "- 左侧：工位" in s.body and "- 杯子放桌面内侧" in s.body
    assert s.objects["保温杯"].images == ["ref_01.jpg"] and s.rules[0].condition == "没有人"
    mgr.store.delete("现场概况")
    assert "现场概况" not in _mgr(tmp_path, legacy_scene_dir=str(scene)).store.skills   # 只迁移一次


def test_example_skill_in_repo_loads(tmp_path):
    async def run():
        mgr = SkillManager({"dir": os.path.join(ROOT, "skills")})
        ex = mgr.store.get("示例-设备间读表")
        assert not ex.enabled and ex.has_tools and "压力表" in ex.objects
        mgr.load_plugins(_buf(), FakeVLM(), EventBus())
        r = json.loads(await mgr.plugins["示例-设备间读表"].call("pressure_status", {"mpa": 0.55}))
        assert r["status"] == "偏高"
    asyncio.run(run())


def test_app_wires_skills(tmp_path):
    async def run():
        from vva.app import App
        from vva.core.config import load_config
        cfg = load_config(None)
        cfg["video"]["type"] = "push"
        cfg["observer"]["enabled"] = False
        for k in ("memory", "skills", "knowledge"):
            cfg[k]["dir"] = str(tmp_path / k)
        os.makedirs(cfg["skills"]["dir"])
        mgr = SkillManager({"dir": cfg["skills"]["dir"]})
        _seed(mgr)
        llm = FakeLLM(["好。", "好。"])
        app = App(cfg, llm=llm, vlm=FakeVLM(), enable_audio=False)
        assert "设备间读表" in app.skills.plugins
        assert [r.condition for r in app.rules.list()] == ["压力表超过 0.6 兆帕"]
        app.buffer.add(Frame(image=_img(), ts=time.time()))
        await app.start()
        await app.ask("看看", "web")
        system = llm.calls[0][0]["content"]
        assert "## 技能" in system and "设备间读表" in system
        app.skills.store.update("访客管理", enabled=True, rules=[{"condition": "出现陌生人"}])
        app.skills.changed()
        assert "出现陌生人" in [r.condition for r in app.rules.list()]
        await app.stop()
    asyncio.run(run())
