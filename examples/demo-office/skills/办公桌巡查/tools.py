"""“办公桌巡查”技能的专用工具：按规定判断桌面物品是否合规。

规定写在代码里，判断结果稳定，不受模型发挥影响。加载“办公桌巡查”技能后才可用。
"""
from vva.skills.plugin import SkillToolProvider, fn_schema

# 规定：(关键词, 违反的规定)
FORBIDDEN = [
    (("打火机", "蜡烛", "火柴", "香薰"), "工位禁止放置明火物品"),
    (("食物", "零食", "外卖", "饭盒", "面包", "饼干", "水果"), "工位禁止进食，食物应放在茶水间"),
    (("易拉罐", "饮料罐", "可乐", "啤酒"), "工位不放开口的罐装饮料，避免倾倒"),
]
NEAR_COMPUTER = ("水杯", "保温杯", "杯子", "茶杯", "咖啡")


class Tools(SkillToolProvider):
    def schemas(self):
        return [fn_schema(
            "desk_compliance", "按《办公区管理规范》判断桌面物品是否合规，返回违规项",
            {"items": {"type": "array", "items": {"type": "string"},
                       "description": "看到的桌面物品，如 [\"笔记本电脑\", \"粉色保温杯\", \"手机\"]"},
             "cup_next_to_computer": {"type": "boolean",
                                      "description": "水杯是否紧挨着电脑或键盘（看不清时不填）"}},
            ["items"])]

    async def tool_desk_compliance(self, items, cup_next_to_computer=None):
        items = [str(x) for x in items or []]
        problems = []
        for item in items:
            for keywords, rule in FORBIDDEN:
                if any(k in item for k in keywords):
                    problems.append(f"{item}：{rule}")
        has_cup = any(any(k in item for k in NEAR_COMPUTER) for item in items)
        if has_cup and cup_next_to_computer:
            problems.append("水杯紧挨电脑：水杯应放在桌面内侧，离电脑 20 厘米以上")
        return {"compliant": not problems, "problems": problems, "checked_items": items}
