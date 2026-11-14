"""执行：给智能体配的“手”——带编号的现场动作，值班员说“执行 3 号”就能触发。

    actions = ActionService(db, bus, speak=..., notify=..., snapshot=..., station_name=..., problem=...)
    await actions.run(3, actor="值班员", source="web")        # 网页、语音、钉钉/飞书都走这里
    agent.extra_prompt → actions.prompt_block()              # 把编号清单写进系统提示词
    ToolSet(..., actions.tools)                              # list_actions / run_action

动作类型（kind）：
  speaker   现场喇叭播报一段话            notify    给钉钉/飞书默认告警群发一条通知
  snapshot  抓拍当前画面存证              webhook   调用现场设备网关的 HTTP 接口（报警器、电源、门禁、电话等）

高风险动作（danger）必须二次确认：网页上弹确认框；语音/聊天里先复述，值班员下一句明确确认后才执行。
智能体只在值班员明确要求时执行，不会自己决定去执行。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable

from vva.core.bus import TOPIC_UI, EventBus
from vva.core.sqlite import SqliteDB
from vva.core.types import fmt_ts

log = logging.getLogger(__name__)

KINDS = {"speaker": "现场喇叭", "notify": "钉钉/飞书通知", "snapshot": "抓拍存证", "webhook": "设备接口"}
CONFIRM_TTL_S = 60.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
  code INTEGER PRIMARY KEY, name TEXT NOT NULL, summary TEXT DEFAULT '', kind TEXT NOT NULL,
  config TEXT DEFAULT '{}', danger INTEGER DEFAULT 0, enabled INTEGER DEFAULT 1, created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS action_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, code INTEGER, name TEXT, actor TEXT, source TEXT,
  ok INTEGER, detail TEXT, image TEXT DEFAULT '');
CREATE INDEX IF NOT EXISTS idx_action_runs_ts ON action_runs(ts);
CREATE TABLE IF NOT EXISTS action_meta (k TEXT PRIMARY KEY, v TEXT);
"""

# 首次启动时预置的动作。设备接口类的没有填地址，页面上显示“未接入”，填上网关地址即可使用。
SEEDS: list[dict[str, Any]] = [
    {"code": 1, "name": "现场喇叭喊话", "kind": "speaker", "summary": "通过现场音箱播报一段提醒，可以临时指定要说的话",
     "config": {"text": "请注意，这里是监控区域，请遵守现场安全规定。"}},
    {"code": 2, "name": "通知值班群", "kind": "notify", "summary": "给钉钉/飞书的默认告警群发一条通知，附当前岗位和时间",
     "config": {"text": "值守台提醒：请值班人员关注现场情况。"}},
    {"code": 3, "name": "抓拍存证", "kind": "snapshot", "summary": "保存当前画面，留作证据"},
    {"code": 4, "name": "声光报警", "kind": "webhook", "summary": "打开现场的声光报警器"},
    {"code": 5, "name": "拨打值班电话", "kind": "webhook", "summary": "通过语音网关拨打值班负责人电话"},
    {"code": 6, "name": "打开现场照明", "kind": "webhook", "summary": "打开监控区域的补光灯"},
    {"code": 7, "name": "锁闭门禁", "kind": "webhook", "danger": True, "summary": "锁闭出入口门禁，人员暂时无法进出"},
    {"code": 8, "name": "关闭电源", "kind": "webhook", "danger": True, "summary": "切断指定回路的电源"},
    {"code": 9, "name": "设备急停", "kind": "webhook", "danger": True, "summary": "让产线设备立即停机"},
]


@dataclass
class Action:
    code: int
    name: str
    kind: str = "webhook"
    summary: str = ""
    config: dict[str, Any] = field(default_factory=dict)
    danger: bool = False
    enabled: bool = True
    created: float = 0.0
    updated: float = 0.0

    @classmethod
    def from_row(cls, r: dict[str, Any]) -> "Action":
        return cls(code=int(r["code"]), name=r["name"], kind=r["kind"], summary=r["summary"] or "",
                   config=json.loads(r["config"] or "{}"), danger=bool(r["danger"]), enabled=bool(r["enabled"]),
                   created=r["created"] or 0.0, updated=r["updated"] or 0.0)


class ActionStore:
    def __init__(self, db: SqliteDB) -> None:
        self.db = db
        db.script(SCHEMA)
        if not db.query("SELECT v FROM action_meta WHERE k='seeded'"):
            if not db.query("SELECT code FROM actions LIMIT 1"):
                for s in SEEDS:
                    self.save(Action(**s))
            db.execute("INSERT OR REPLACE INTO action_meta(k, v) VALUES('seeded', '1')")

    def list(self) -> list[Action]:
        return [Action.from_row(r) for r in self.db.query("SELECT * FROM actions ORDER BY code")]

    def get(self, code: int) -> Action | None:
        rows = self.db.query("SELECT * FROM actions WHERE code=?", (int(code),))
        return Action.from_row(rows[0]) if rows else None

    def next_code(self) -> int:
        used = {a.code for a in self.list()}
        return next(i for i in range(1, 1000) if i not in used)

    def save(self, a: Action) -> Action:
        now = time.time()
        a.created = a.created or now
        a.updated = now
        self.db.execute("INSERT OR REPLACE INTO actions(code, name, summary, kind, config, danger, enabled, created, updated) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (a.code, a.name, a.summary, a.kind, json.dumps(a.config, ensure_ascii=False), int(a.danger),
                         int(a.enabled), a.created, a.updated))
        return a

    def remove(self, code: int) -> bool:
        if self.get(code) is None:
            return False
        self.db.execute("DELETE FROM actions WHERE code=?", (int(code),))
        return True

    def log(self, a: Action, actor: str, source: str, ok: bool, detail: str, image: str = "") -> int:
        return self.db.execute("INSERT INTO action_runs(ts, code, name, actor, source, ok, detail, image) VALUES(?,?,?,?,?,?,?,?)",
                               (time.time(), a.code, a.name, actor, source, int(ok), detail[:300], image))

    def runs(self, limit: int = 30, code: int | None = None) -> list[dict[str, Any]]:
        where, params = ("WHERE code=?", [int(code)]) if code is not None else ("", [])
        rows = self.db.query(f"SELECT * FROM action_runs {where} ORDER BY id DESC LIMIT ?", [*params, int(limit)])
        return [{**r, "ok": bool(r["ok"]), "time": fmt_ts(r["ts"])} for r in rows]

    def last_runs(self) -> dict[int, dict[str, Any]]:
        rows = self.db.query("SELECT r.* FROM action_runs r JOIN (SELECT code, MAX(id) AS id FROM action_runs GROUP BY code) m "
                             "ON r.id = m.id")
        return {int(r["code"]): r for r in rows}


def http_call(url: str, method: str, payload: dict[str, Any], headers: dict[str, str], timeout: float) -> str:
    """调用设备网关。返回简短的结果说明；失败抛异常。"""
    data = None if method == "GET" else json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(300).decode("utf-8", "replace").strip()
            return f"设备已响应（{resp.status}）" + (f"：{body[:80]}" if body else "")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"设备接口返回 {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RuntimeError(f"连不上设备接口：{getattr(e, 'reason', e)}") from e


class ActionService:
    def __init__(self, db: SqliteDB, bus: EventBus, *, shots_dir: str = "data/actions",
                 speak: Callable[[str], None] | None = None,
                 notify: Callable[[str, str], Awaitable[list[str]]] | None = None,
                 snapshot: Callable[[], bytes | None] | None = None,
                 station_name: Callable[[], str] = lambda: "",
                 problem: Callable[[str], str] = lambda kind: "",
                 http: Callable[..., str] = http_call) -> None:
        self.store = ActionStore(db)
        self.bus = bus
        self.shots_dir = shots_dir
        self._speak, self._notify, self._snapshot = speak, notify, snapshot
        self.station_name, self._problem, self._http = station_name, problem, http
        self._pending: dict[tuple[str, int], tuple[float, int]] = {}    # (会话, 编号) → (时间, 提出确认时的对话轮次)
        from vva.actions.tools import ActionTools
        self.tools = ActionTools(self, bus)

    # ---------------------------------------------------------------- 查询
    def problem(self, a: Action) -> str:
        """这个动作现在执行不了的原因；可以执行返回空串。"""
        if not a.enabled:
            return "已停用"
        if a.kind == "webhook":
            return "" if str(a.config.get("url") or "").startswith(("http://", "https://")) else "未接入设备接口"
        if a.kind == "speaker" and self._speak is None:
            return "现场播报未开启"
        if a.kind == "notify" and self._notify is None:
            return "钉钉/飞书未开启"
        return self._problem(a.kind)

    def to_dict(self, a: Action, last: dict[str, Any] | None = None) -> dict[str, Any]:
        problem = self.problem(a)
        d = asdict(a)
        d.update(kind_label=KINDS.get(a.kind, a.kind), ready=not problem, problem=problem, code_text=f"{a.code:02d}",
                 last=({"time": fmt_ts(last["ts"]), "ts": last["ts"], "ok": bool(last["ok"]), "detail": last["detail"],
                        "actor": last["actor"]} if last else None))
        return d

    def list(self) -> list[dict[str, Any]]:
        last = self.store.last_runs()
        return [self.to_dict(a, last.get(a.code)) for a in self.store.list()]

    def find(self, key: Any) -> Action | None:
        """按编号或名称找动作：3、"3"、"3号"、"03"、"抓拍"。"""
        s = str(key).strip().rstrip("号").strip()
        if s.isdigit():
            return self.store.get(int(s))
        return next((a for a in self.store.list() if a.name == s or (s and s in a.name)), None)

    def prompt_block(self) -> str:
        items = [a for a in self.store.list() if a.enabled]
        if not items:
            return ""
        lines = [f"- {a.code} 号 {a.name}（{a.summary or KINDS.get(a.kind, '')}"
                 f"{'；高风险，需确认' if a.danger else ''}{'；' + self.problem(a) if self.problem(a) else ''}）" for a in items]
        return ("\n## 可以执行的动作（实时，以这里为准）\n" + "\n".join(lines) +
                "\n用户说“执行 3 号”“3 号”或直接说动作名称时，调用 run_action 执行对应编号，然后一句话说明结果。"
                "只在用户明确要求时执行，不要自己决定去执行。标了“需确认”的动作：先调用一次 run_action，"
                "按返回的提示向用户复述动作并请对方确认；用户下一句明确确认后，再带 confirm=true 调用。\n")

    # ---------------------------------------------------------------- 管理
    def create(self, fields: dict[str, Any]) -> Action:
        code = int(fields.get("code") or self.store.next_code())
        if self.store.get(code) is not None:
            raise ValueError(f"{code} 号已经被占用")
        return self.store.save(Action(code=code, name=fields["name"], kind=fields.get("kind", "webhook"),
                                      summary=fields.get("summary", ""), config=fields.get("config") or {},
                                      danger=bool(fields.get("danger")), enabled=bool(fields.get("enabled", True))))

    def update(self, code: int, fields: dict[str, Any]) -> Action:
        a = self.store.get(code)
        if a is None:
            raise KeyError(code)
        for k in ("name", "kind", "summary", "config", "danger", "enabled"):
            if k in fields:
                setattr(a, k, fields[k])
        return self.store.save(a)

    # ---------------------------------------------------------------- 执行
    def needs_confirm(self, a: Action, session: str, turn_no: int, confirm: bool) -> bool:
        """高风险动作：第一次调用只登记；同一会话在之后的对话轮次里、60 秒内带 confirm 再调用才放行。"""
        if not a.danger:
            return False
        key, now = (session, a.code), time.time()
        asked = self._pending.get(key)
        if confirm and asked and now - asked[0] <= CONFIRM_TTL_S and turn_no != asked[1]:
            del self._pending[key]
            return False
        self._pending[key] = (now, turn_no)
        return True

    async def run(self, code: Any, *, actor: str = "值班员", source: str = "web", text: str = "") -> dict[str, Any]:
        a = self.find(code)
        if a is None:
            return {"ok": False, "error": f"没有 {code} 号动作"}
        problem = self.problem(a)
        if problem:
            self.store.log(a, actor, source, False, problem)
            return {"ok": False, "code": a.code, "name": a.name, "error": f"{a.code} 号“{a.name}”现在执行不了：{problem}"}
        image = ""
        try:
            detail, image = await self._execute(a, text.strip())
            ok = True
        except Exception as e:
            log.warning("执行 %s 号 %s 失败：%s", a.code, a.name, e)
            detail, ok = str(e) or "执行失败", False
        run_id = self.store.log(a, actor, source, ok, detail, image)
        log.info("执行 %s 号 %s（%s，%s）：%s", a.code, a.name, actor, source, detail)
        self.bus.publish(TOPIC_UI, {"type": "action", "code": a.code, "name": a.name, "ok": ok, "detail": detail,
                                    "actor": actor, "session": "web"})
        out = {"ok": ok, "code": a.code, "name": a.name, "detail": detail, "run_id": run_id}
        if not ok:
            out["error"] = detail
        return out

    async def _execute(self, a: Action, text: str) -> tuple[str, str]:
        cfg = a.config
        say = text or str(cfg.get("text") or "")
        if a.kind == "speaker":
            if not say:
                raise RuntimeError("没有要播报的内容")
            self._speak(say)
            return f"已播报：{say[:60]}", ""
        if a.kind == "notify":
            if not say:
                raise RuntimeError("没有要发送的内容")
            station = self.station_name()
            sent = await self._notify(f"{station + ' · ' if station else ''}值守台通知", say)
            return f"已发到 {'、'.join(sent)}", ""
        if a.kind == "snapshot":
            jpeg = self._snapshot() if self._snapshot else None
            if not jpeg:
                raise RuntimeError("现在没有画面")
            os.makedirs(self.shots_dir, exist_ok=True)
            name = time.strftime("%Y%m%d-%H%M%S") + f"-{a.code}.jpg"
            with open(os.path.join(self.shots_dir, name), "wb") as f:
                f.write(jpeg)
            return "已保存当前画面", name
        if a.kind == "webhook":
            payload = {"code": a.code, "name": a.name, "text": text, "station": self.station_name(), "ts": int(time.time()),
                       **(cfg.get("body") if isinstance(cfg.get("body"), dict) else {})}
            headers = {str(k): str(v) for k, v in (cfg.get("headers") or {}).items()}
            method = str(cfg.get("method") or "POST").upper()
            return await asyncio.to_thread(self._http, cfg["url"], method, payload, headers,
                                           float(cfg.get("timeout") or 5)), ""
        raise RuntimeError(f"不认识的动作类型 {a.kind}")

    def shot_path(self, name: str) -> str | None:
        path = os.path.join(self.shots_dir, os.path.basename(name))
        return path if os.path.isfile(path) else None
