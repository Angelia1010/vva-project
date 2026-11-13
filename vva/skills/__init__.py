"""技能（Skill）：用“说明 + 参考图 + 判断标准 + 规则模板 + 热词 + 可选专用工具”适配具体现场和专项任务，
不需要训练模型。每个技能是 skills/ 下的一个目录（见 store.py），可以同时启用多个，按需加载。

  model.py    数据结构
  store.py    目录读写（SKILL.md、目标与参考图）、旧版场景包迁移
  manager.py  启用与摄像头过滤、提示词（目录 + 常驻正文）、加载状态、专用工具、参考图、热词、规则
  tools.py    load_skill / compare_with_reference / 各技能的专用工具
  plugin.py   技能专用工具的基类（tools.py 里继承它）

对外入口：
    skills = SkillManager(cfg["skills"], camera_id)
    skills.load_plugins(buffer, vlm, bus)
    tools = ToolSet(..., SkillTools(skills, buffer, vlm, bus))
    agent.extra_prompt = skills.prompt_block
    agent.tool_visible = skills.tool_visible
"""
from vva.skills.manager import LOADED_KEY, SkillManager
from vva.skills.model import RuleTemplate, Skill, SkillObject
from vva.skills.plugin import SkillContext, SkillToolProvider
from vva.skills.store import SkillStore, parse_skill_md, render_skill_md, safe_name
from vva.skills.tools import SkillTools

__all__ = ["SkillManager", "SkillStore", "SkillTools", "Skill", "SkillObject", "RuleTemplate", "SkillContext",
           "SkillToolProvider", "LOADED_KEY", "parse_skill_md", "render_skill_md", "safe_name"]
