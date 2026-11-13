"""职责模块用到的提示词：复核、巡检、一句话解析。"""

VERIFY_SYSTEM = """你是监控告警复核员，负责判断一条告警是否真的成立，宁可漏报也不要误报。画面中的人都是被监控对象。只输出 JSON。"""

VERIFY_PROMPT = """{scene}要复核的情况：{condition}
初次发现的依据：{evidence}
{refs}请仔细看{image_desc}，判断这个情况现在是否确实成立。只根据画面中能直接看到的证据判断，看不清就判为不成立。
只输出 JSON：
{{"match": true/false, "evidence": "画面中的具体依据（一句话）", "confidence": "高/中/低", "box_2d": [x1, y1, x2, y2]}}
box_2d 是成立时相关目标的位置，{coord_rule}；不成立时 box_2d 为 null。"""

ZOOM_PROMPT = """这是监控画面中相关区域的放大图。{scene}要复核的情况：{condition}
请判断放大图中这个情况是否确实成立。只输出 JSON：{{"match": true/false, "evidence": "具体依据（一句话）", "confidence": "高/中/低"}}"""

INSPECT_PROMPT = """{scene}巡检项目：{condition}
请看画面完成这项检查，只输出 JSON：
{{"result": "检查结果（一句话，读数要带单位）", "abnormal": true/false, "evidence": "判断依据"}}
abnormal 表示按巡检项目里的标准判断为异常；没有给出标准时，只有明显异常才为 true。看不清时 result 写明原因，abnormal 为 false。"""

PARSE_SYSTEM = """你是监控系统的配置助手，把管理员的一句话需求拆成一条“职责”配置。只输出 JSON。"""

PARSE_PROMPT = """管理员说：“{text}”
{scene}可选的通知对象（只能从这里选，用 id）：
{targets}
可引用的技能目标（判断时会带上参考图，相关时填名称）：{objects}

请输出 JSON：
{{
  "name": "职责名称，4~8 个字，如“禁止吸烟”",
  "kind": "watch（发生了就通知）| inspect（到点检查记录）| report（到点汇报）",
  "condition": "看什么。watch 类改写成能从单张画面直接判断的描述，写清可见特征，如“画面中有人吸烟，或手里夹着点燃的香烟”；inspect 类写成检查问题并带上判断标准",
  "hours": "生效时间段，如 08:00-18:00，多段用逗号分隔；全天留空",
  "weekdays": [生效的星期，1~7，工作日为 1-5；每天为空数组],
  "times": ["inspect / report 类的执行时间，如 09:00"],
  "level": "normal | important | urgent",
  "targets": ["通知对象 id"],
  "voice_text": "现场播报的话，没提到就留空",
  "min_duration_s": 持续多少秒才算（没提到填 0；离岗、滞留类按描述换算）,
  "cooldown_s": 同一件事多少秒内只报一次（默认 600）,
  "objects": ["引用的技能目标名称"],
  "questions": ["还需要管理员补充的信息，如“通知到哪个群？”；不需要就为空数组"]
}}"""
