"""岗位、职责、告警的接口（值守台、职责页、告警中心使用）。

  GET  /api/overview                     值守台首屏：岗位、职责清单（含状态和今日次数）、统计、待处理告警
  GET  /api/station   PUT /api/station   岗位信息（名称、看哪里、场景说明、负责人、暂停值守）
  GET  /api/duties                       职责清单 + 可选的通知对象、技能目标
  POST /api/duties                       新增职责
  PUT  /api/duties/{id}                  修改（含暂停 / 启用：{"enabled": false}）
  DELETE /api/duties/{id}                删除
  POST /api/duties/parse                 一句话 → 表单草稿 {"text"}
  POST /api/duties/test                  试一下（草稿或已保存的职责）{"duty": {...}} 或 {"id"}，可加 "notify": true 发测试通知
  GET  /api/duties/templates             行业模板
  POST /api/duties/templates/{tpl}       按模板批量添加 {"names": [...], "targets": [...], "mode": "live|trial"}
  GET  /api/duties/changes               最近的职责变更（全部）
  GET  /api/duties/{id}/changes          某条职责的变更记录
  GET  /api/alerts                       告警列表 ?status=pending&duty=d1&days=1
  GET  /api/alerts/{id}                  告警详情（含处理经过）
  POST /api/alerts/{id}/action           处理 {"action": "ack|handling|resolve|false|trial_ok", "actor", "note", "add_ref"}
  GET  /api/alerts/image/{path}          告警截图
  DELETE /api/alerts/{id}                删除一条告警（含截图和处理经过）
  POST /api/alerts/delete                批量删除 {"ids": [...]}

配置了 web.admin_token 时，职责和岗位的修改、删除告警需要在请求头 X-Token 中携带；处理告警不需要（值班员操作）。
"""
from __future__ import annotations

import base64
import os
import re
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from vva.alerts import ACTIONS, day_start
from vva.core.bus import TOPIC_ALERT, TOPIC_UI
from vva.duties.model import KINDS, LEVELS, MODES, Duty
from vva.duties.store import EDITABLE
from vva.duties.templates import TEMPLATES, capability_hints, template_list

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")
PAGES = {"/duties": "duties.html", "/alerts": "alerts.html", "/settings": "settings.html"}


def clean_duty(body: dict[str, Any], partial: bool = False) -> dict[str, Any]:
    """校验网页提交的职责字段。"""
    out: dict[str, Any] = {}
    for k in EDITABLE:
        if k in body:
            out[k] = body[k]
    for k in ("name", "condition", "hours", "voice_text", "rule_ref"):
        if k in out:
            out[k] = str(out[k] or "").strip()
    if not partial and not out.get("condition"):
        raise HTTPException(400, "请填写要看什么")
    if "kind" in out and out["kind"] not in KINDS:
        raise HTTPException(400, "类型不对")
    if "level" in out and out["level"] not in LEVELS:
        raise HTTPException(400, "级别不对")
    if "mode" in out and out["mode"] not in MODES:
        raise HTTPException(400, "开启方式不对")
    for k in ("enabled", "temporary", "verify"):
        if k in out:
            out[k] = bool(out[k])
    for k in ("min_duration_s", "cooldown_s", "expires"):
        if k in out:
            try:
                out[k] = max(0.0, float(out[k] or 0))
            except (TypeError, ValueError) as e:
                raise HTTPException(400, f"{k} 应为数字") from e
    if "cooldown_s" in out:
        out["cooldown_s"] = max(5.0, out["cooldown_s"])
    for k in ("targets", "objects", "times"):
        if k in out:
            out[k] = [str(x).strip() for x in out[k] or [] if str(x).strip()]
    for t in out.get("times", []):
        if not _TIME_RE.match(t):
            raise HTTPException(400, f"时间格式不对：{t}（应为 HH:MM）")
    if "weekdays" in out:
        out["weekdays"] = sorted({int(d) for d in out["weekdays"] or [] if str(d).isdigit() and 1 <= int(d) <= 7})
    if out.get("kind") in ("inspect", "report") and not out.get("times") and not partial:
        raise HTTPException(400, "巡检、汇报类职责需要设置执行时间")
    return out


def register_duty_routes(api: FastAPI, app: Any, admin_token: str, static_dir: str) -> None:
    m = app.duties

    def check(request: Request) -> str:
        if admin_token and request.headers.get("X-Token") != admin_token:
            raise HTTPException(401, "需要管理口令")
        return "管理员"

    def objects() -> list[str]:
        if not app.skills:
            return []
        return [o.name for s in app.skills.active() for o in s.objects.values()]

    def page_route(file: str):
        async def page() -> FileResponse:
            return FileResponse(os.path.join(static_dir, file))
        return page

    for path, file in PAGES.items():
        api.add_api_route(path, page_route(file), methods=["GET"])

    # ---------------------------------------------------------------- 首屏
    @api.get("/api/overview")
    async def overview() -> JSONResponse:
        return JSONResponse({"station": app.station.to_dict(), "duties": m.status_list(), "stats": m.alerts.stats(),
                             "open_alerts": m.alerts.open_alerts(5), "token_required": bool(admin_token)})

    @api.get("/api/station")
    async def get_station() -> JSONResponse:
        return JSONResponse(app.station.to_dict())

    @api.put("/api/station")
    async def put_station(request: Request, body: dict) -> JSONResponse:
        check(request)
        changes = {k: body[k] for k in ("name", "area", "scene", "owner", "paused") if k in body}
        if "name" in changes and not str(changes["name"]).strip():
            raise HTTPException(400, "请填写岗位名称")
        st = app.station.update(**changes)
        app.rules.paused = st.paused
        app.bus.publish(TOPIC_UI, {"type": "duties_changed", "session": None})
        return JSONResponse(app.station.to_dict())

    # ---------------------------------------------------------------- 职责
    @api.get("/api/duties")
    async def list_duties() -> JSONResponse:
        return JSONResponse({"duties": m.status_list(), "targets": m.target_options(), "objects": objects(),
                             "token_required": bool(admin_token)})

    @api.get("/api/duties/templates")
    async def templates() -> JSONResponse:
        return JSONResponse({"templates": template_list()})

    @api.post("/api/duties/parse")
    async def parse(body: dict) -> JSONResponse:
        text = str(body.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "请先用一句话描述")
        return JSONResponse(await m.parse(text, objects()))

    @api.post("/api/duties/test")
    async def test(body: dict) -> JSONResponse:
        if body.get("id"):
            duty = m.book.get(str(body["id"]))
            if duty is None:
                raise HTTPException(404, "没有这条职责")
        else:
            fields = clean_duty(body.get("duty") or {}, partial=True)
            if not fields.get("condition"):
                raise HTTPException(400, "请先填写要看什么")
            duty = Duty(id="test", name=fields.pop("name", "") or "试一下", condition=fields.pop("condition"), **fields)
        r = await m.test(duty)
        image = r.pop("image", None)
        r["image"] = "data:image/jpeg;base64," + base64.b64encode(image).decode() if image else ""
        if body.get("notify"):
            m.router.bus.publish(TOPIC_ALERT, {"kind": "alert", "name": duty.name, "condition": f"【测试】{duty.name}",
                                           "evidence": r.get("evidence") or "这是一条测试通知", "level": duty.level,
                                           "station": app.station.station.name, "ts": time.time(),
                                           "targets": [t for t in duty.effective_targets() if t != "voice"]})
            r["notified"] = m.target_labels([t for t in duty.effective_targets() if t != "voice"])
        r["hints"] = capability_hints(duty.condition, duty.kind)
        return JSONResponse(r)

    @api.post("/api/duties")
    async def create(request: Request, body: dict) -> JSONResponse:
        actor = check(request)
        try:
            duty = m.book.create(actor=actor, source=str(body.get("source") or "web"), **clean_duty(body))
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return JSONResponse(duty.to_dict())

    @api.post("/api/duties/templates/{tpl}")
    async def apply_template(tpl: str, request: Request, body: dict) -> JSONResponse:
        actor = check(request)
        t = TEMPLATES.get(tpl)
        if t is None:
            raise HTTPException(404, "没有这个模板")
        names = set(body.get("names") or [i["name"] for i in t["items"]])
        existing = {d.name for d in m.book.list()}
        created = []
        for item in t["items"]:
            if item["name"] not in names or item["name"] in existing:
                continue
            fields = {k: v for k, v in item.items() if k != "hint"}
            if body.get("targets"):
                fields["targets"] = list(body["targets"])
            if body.get("mode") in MODES and fields.get("kind", "watch") == "watch":
                fields["mode"] = body["mode"]
            created.append(m.book.create(actor=actor, source="template", **clean_duty(fields)).to_dict())
        return JSONResponse({"created": created})

    @api.put("/api/duties/{duty_id}")
    async def update(duty_id: str, request: Request, body: dict) -> JSONResponse:
        actor = check(request)
        try:
            duty = m.book.update(duty_id, actor=actor, **clean_duty(body, partial=True))
        except KeyError as e:
            raise HTTPException(404, "没有这条职责") from e
        return JSONResponse(duty.to_dict())

    @api.delete("/api/duties/{duty_id}")
    async def delete(duty_id: str, request: Request) -> JSONResponse:
        actor = check(request)
        if not m.book.remove(duty_id, actor):
            raise HTTPException(404, "没有这条职责")
        return JSONResponse({"ok": True})

    @api.get("/api/duties/changes")
    async def all_changes() -> JSONResponse:
        rows = m.book.store.changes(limit=30) if m.book.store else []
        names = {d.id: d.name for d in m.book.list()}
        return JSONResponse({"changes": [{**r, "name": names.get(r["duty_id"], ""),
                                          "time": time.strftime("%m-%d %H:%M", time.localtime(r["ts"]))} for r in rows]})

    @api.get("/api/duties/{duty_id}/changes")
    async def changes(duty_id: str) -> JSONResponse:
        rows = m.book.store.changes(duty_id) if m.book.store else []
        return JSONResponse({"changes": [{**r, "time": time.strftime("%m-%d %H:%M", time.localtime(r["ts"]))} for r in rows]})

    # ---------------------------------------------------------------- 告警
    @api.get("/api/alerts")
    async def alerts(status: str = "", duty: str = "", days: float = 1) -> JSONResponse:
        start = day_start() - (max(1.0, days) - 1) * 86400
        statuses = tuple(s for s in status.split(",") if s) if status else ()
        items = [a for a in m.alerts.list(start=start, duty_id=duty, statuses=statuses, limit=500)
                 if status or a["status"] != "record"]
        return JSONResponse({"alerts": items, "stats": m.alerts.stats(),
                             "duties": [{"id": d.id, "name": d.name} for d in m.book.list()]})

    @api.get("/api/alerts/image/{rel:path}")
    async def alert_image(rel: str) -> FileResponse:
        path = m.alerts.store.image_path(rel)
        if not path:
            raise HTTPException(404, "截图不存在或已过期清理")
        return FileResponse(path, media_type="image/jpeg")

    @api.get("/api/alerts/{alert_id}")
    async def alert_detail(alert_id: int) -> JSONResponse:
        d = m.alerts.detail(alert_id)
        if d is None:
            raise HTTPException(404, "没有这条告警")
        return JSONResponse(d)

    @api.delete("/api/alerts/{alert_id}")
    async def alert_delete(alert_id: int, request: Request) -> JSONResponse:
        check(request)
        if not m.alerts.delete([alert_id]):
            raise HTTPException(404, "没有这条告警")
        return JSONResponse({"ok": True, "deleted": 1})

    @api.post("/api/alerts/delete")
    async def alerts_delete(request: Request, body: dict) -> JSONResponse:
        check(request)
        ids = [int(i) for i in body.get("ids") or [] if str(i).isdigit()]
        if not ids:
            raise HTTPException(400, "没有选择要删除的告警")
        return JSONResponse({"ok": True, "deleted": m.alerts.delete(ids)})

    @api.post("/api/alerts/{alert_id}/action")
    async def alert_action(alert_id: int, body: dict) -> JSONResponse:
        action = str(body.get("action") or "")
        if action not in ACTIONS:
            raise HTTPException(400, "不支持的操作")
        try:
            alert = m.alerts.act(alert_id, action, str(body.get("actor") or "值班员")[:20], str(body.get("note") or "")[:200])
        except KeyError as e:
            raise HTTPException(404, "没有这条告警") from e
        added = action == "false" and bool(body.get("add_ref")) and m.add_negative(alert)
        return JSONResponse({**alert.to_dict(), "ref_added": added})

