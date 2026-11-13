"""示例：技能专用工具。加载“示例-设备间读表”技能后，智能体才能看到并调用 pressure_status。

规则性的判断（阈值比较）写成代码，比让模型自己比大小更稳定。
"""
from vva.skills.plugin import SkillToolProvider, fn_schema

NORMAL = (0.2, 0.5)
OVER = 0.6


class Tools(SkillToolProvider):
    def schemas(self):
        return [fn_schema("pressure_status", "按本技能的标准判断压力表读数（兆帕）是否正常",
                          {"mpa": {"type": "number", "description": "读数，单位兆帕"}}, ["mpa"])]

    async def tool_pressure_status(self, mpa: float) -> dict:
        mpa = float(mpa)
        if mpa > OVER:
            status = "超压"
        elif mpa > NORMAL[1]:
            status = "偏高"
        elif mpa < NORMAL[0]:
            status = "偏低"
        else:
            status = "正常"
        return {"mpa": mpa, "status": status, "standard": f"正常 {NORMAL[0]}~{NORMAL[1]}，超过 {OVER} 为超压"}
