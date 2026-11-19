"""搜索子智能体的提示词。"""

PLAN_SYSTEM = """你是监控录像检索助手，负责把值班员的一句话变成检索条件。只输出 JSON，不要输出其他内容。"""

PLAN_PROMPT = """现在是 {now}（{weekday}）。
可选的摄像头：
{cameras}
{default_scope}
值班员说：“{question}”

请输出 JSON：
{{
  "start": "开始时间 YYYY-MM-DD HH:MM",
  "end": "结束时间 YYYY-MM-DD HH:MM",
  "cameras": ["摄像头编号"，没说具体哪一路就留空数组],
  "keywords": ["在画面文字记录里可能出现的词，3~8 个，包含同义说法，每个 2~6 个字"],
  "people_min": 至少几个人（只有问人数时才填整数，否则 null）,
  "look_for": "要在画面里确认的情况，写成能直接对着一张画面判断的一句话；只按人数等条件检索时也照样写"
}}
时间规则：“昨天下午”= 昨天 12:00~18:00；“上午”= 06:00~12:00；“晚上”= 18:00~24:00；“刚才”= 最近 30 分钟；
“今天”= 今天 00:00 到现在；没说时间 = 最近 24 小时。结束时间不要晚于现在。
关键词示例：吸烟 → ["吸烟", "抽烟", "香烟", "烟雾"]；叉车经过 → ["叉车", "搬运车", "车辆"]。"""

VERIFY_PROMPT = """{scene}要确认的情况：{look_for}
{hint}这张是监控画面（{when}）。画面中的人都是被监控对象。
判断画面里是否确实出现了这个情况，输出 JSON：
{{"match": true 或 false, "evidence": "画面中的具体依据，一句话", "confidence": "高/中/低",
  "box_2d": [x1, y1, x2, y2] 或 null}}
box_2d 框住最能说明情况的目标，{coord_rule}没有就填 null。看不清、只是可能时 match 为 false。"""

REFINE_PROMPT = """以下 {n} 张监控画面按时间先后排列，拍摄时间依次为：{times}。
要找的情况：{look_for}
从第几张开始出现这个情况？输出 JSON：{{"first": 张数（1~{n}），都没有出现填 0}}"""

SUMMARY_NONE = "{scope}没有找到{what}。"
SUMMARY_FOUND = "{scope}找到 {n} 处{what}：{items}。"
