"""聊天机器人类入口（飞书、钉钉……）的公共部分。

各入口只需要实现“怎么收、怎么发”：
  parse()          把平台推来的原始消息转成 IncomingMessage
  connect()        创建接口客户端和消息接收器（receiver，需有 start()）
  reply_text() / reply_answer()      回复一条消息
  upload_image()   上传画面截图，返回平台的图片标识
  send_alert() / send_report() / send_test()   主动发到群
  voice_text()     语音消息转文字

公共部分负责：权限、消息去重、指令（/id、帮助、清空对话）、提问、告警推送、定时报告、配置页状态。
会话：每个群、每个私聊各一个会话（<入口名>:<会话编号>），上下文互不干扰。
"""
from __future__ import annotations

import asyncio
import logging
import time
from abc import abstractmethod
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from vva.channels.base import Channel, ChannelHost
from vva.core.bus import TOPIC_ALERT

log = logging.getLogger(__name__)

RESET_WORDS = {"清空对话", "/reset", "重新开始"}


@dataclass
class IncomingMessage:
    message_id: str
    chat_id: str
    chat_type: str              # p2p 私聊 | group 群聊
    sender_id: str              # 发送者编号（飞书 open_id，钉钉 staffId）
    msg_type: str               # text | audio | image | ...
    text: str = ""
    file_key: str = ""          # 语音 / 图片的资源编号
    mentioned: bool = False     # 群消息中是否 @ 了机器人
    extra: dict[str, Any] = field(default_factory=dict)    # 各平台特有的字段（如钉钉的回复地址）

    @property
    def is_group(self) -> bool:
        return self.chat_type == "group"


class ChatChannel(Channel):
    name = "chat"
    label = "聊天机器人"              # 显示给用户的平台名
    user_id_label = "编号"            # 发送者编号的叫法（飞书 open_id，钉钉 userId）
    conn_fields: tuple[str, ...] = ()   # 这些配置变化后需要重启服务（长连接无法热切换）

    def __init__(self, cfg: dict[str, Any], host: ChannelHost, api: Any = None, receiver: Any = None) -> None:
        """api / receiver 可注入（测试用）；默认在 connect() 里按配置创建。"""
        super().__init__(cfg, host)
        self.api = api
        self.receiver = receiver
        self._apply_options(cfg)
        self._conn = self._conn_key(cfg)
        self._seen: OrderedDict[str, None] = OrderedDict()                 # 去重：平台可能重复推送同一条消息
        self.recent: OrderedDict[str, dict[str, Any]] = OrderedDict()      # 最近发来消息的会话，供配置页一键选用
        self._alert_task: asyncio.Task | None = None
        self._report_task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._last_report = time.time()
        self._stopped = False
        self.started = False
        self.start_error = ""

    # ---------------------------------------------------------------- 各入口实现
    @abstractmethod
    def ready(self) -> bool:
        """必需的凭证是否已配置。"""

    @abstractmethod
    async def connect(self) -> None:
        """创建 self.api 和 self.receiver（已注入的保持不变）。"""

    @abstractmethod
    def parse(self, raw: dict[str, Any]) -> IncomingMessage | None: ...

    @abstractmethod
    async def reply_text(self, m: IncomingMessage, text: str) -> None: ...

    @abstractmethod
    async def reply_answer(self, m: IncomingMessage, text: str, image: Any, caption: str) -> None:
        """回答；image 为 upload_image() 的结果，没有图时为 None。"""

    @abstractmethod
    async def upload_image(self, jpeg: bytes) -> Any: ...

    @abstractmethod
    async def send_alert(self, chat: str, condition: str, evidence: str, time_text: str, image: Any) -> None: ...

    @abstractmethod
    async def send_report_to(self, chat: str, title: str, text: str) -> None: ...

    @abstractmethod
    async def voice_text(self, m: IncomingMessage) -> str:
        """语音消息转文字；识别不了时自行回复提示并返回空串。"""

    async def send_test(self, chat: str) -> None:
        await self.send_report_to(chat, "监控助手测试消息", f"{self.label}配置正常，告警和报告会发到这里。")

    async def close(self) -> None:
        if self.api is not None and hasattr(self.api, "close"):
            await self.api.close()

    # ---------------------------------------------------------------- 配置
    def _conn_key(self, cfg: dict[str, Any]) -> tuple:
        return tuple(cfg.get(k) for k in self.conn_fields)

    def _apply_options(self, cfg: dict[str, Any]) -> None:
        """可以随时修改、立即生效的选项。"""
        self.allowed_chats = set(cfg.get("allowed_chats") or [])
        self.allowed_users = set(cfg.get("allowed_users") or [])
        self.admin_users = set(cfg.get("admin_users") or [])
        self.require_mention = bool(cfg.get("require_mention", True))
        self.alert_chats = list(cfg.get("alert_chats") or [])
        self.report_chats = list(cfg.get("report_chats") or [])
        self.report_times = [str(t) for t in cfg.get("report_times") or []]
        self.web_url = cfg.get("web_url") or ""
        self.send_images = bool(cfg.get("send_images", True))

    def apply(self, cfg: dict[str, Any]) -> bool:
        """网页修改配置后调用：选项立即生效；返回 True 表示连接参数变了，需要重启服务。"""
        self.cfg = cfg
        self._apply_options(cfg)
        if self.started:
            self._sync_tasks()
        return self._conn_key(cfg) != self._conn and self.started

    # ---------------------------------------------------------------- 生命周期
    async def start(self) -> None:
        if not self.ready() and self.api is None:
            log.error("%s入口未配置应用凭证，已跳过", self.label)
            return
        self._loop = asyncio.get_running_loop()
        await self.connect()
        self.started = True
        try:
            self.receiver.start()
        except Exception as e:
            self.start_error = str(e)
            log.error("%s消息接收启动失败（仍可推送告警）：%s", self.label, e)
        self._sync_tasks()
        log.info("%s入口已启动：告警群 %d 个，报告群 %d 个，报告时间 %s", self.label, len(self.alert_chats),
                 len(self.report_chats), self.report_times or "无")

    def _sync_tasks(self) -> None:
        """启动告警推送（一直运行：职责可能指定发到本入口的某个群），按配置启停入口自带的定时报告。"""
        if self._alert_task is None:
            q = self.host.bus.subscribe(TOPIC_ALERT)     # 先订阅再启动任务，启动后立即发生的告警也不会漏
            self._alert_task = asyncio.create_task(self._alert_loop(q), name=f"{self.name}-alerts")
        if self.report_chats and self.report_times and self._report_task is None:
            self._report_task = asyncio.create_task(self._report_loop(), name=f"{self.name}-reports")
        elif not (self.report_chats and self.report_times) and self._report_task is not None:
            self._report_task.cancel()
            self._report_task = None

    async def stop(self) -> None:
        self._stopped = True      # 长连接线程无法彻底关闭：之后收到的消息一律忽略
        for t in (self._alert_task, self._report_task):
            if t is not None:
                t.cancel()
        self._alert_task = self._report_task = None
        await self.close()

    def status(self) -> dict[str, Any]:
        """配置页显示的运行状态。"""
        rec = self.receiver
        state = getattr(rec, "state", "running" if self.started else "not_started")
        error = self.start_error or getattr(rec, "error", "")
        if error and state != "running":
            state = "error"
        last = getattr(rec, "last_event", 0.0)
        return {"started": self.started, "receiver": state, "error": error,
                "last_message": time.strftime("%m-%d %H:%M:%S", time.localtime(last)) if last else "",
                "alert_push": bool(self.alert_chats) and self._alert_task is not None,
                "report": self._report_task is not None,
                "recent": list(reversed(self.recent.values()))}

    # ---------------------------------------------------------------- 接收消息
    def _on_event_threadsafe(self, raw: dict[str, Any]) -> None:
        """在 SDK 线程里调用：切回主事件循环处理，立即返回。"""
        if self._loop and not self._stopped:
            self._loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self.handle_event(raw)))

    def _allowed(self, m: IncomingMessage) -> bool:
        """没有配置名单时所有人可用；配置了就需要群在 allowed_chats 或人在 allowed_users 里。"""
        if not (self.allowed_chats or self.allowed_users):
            return True
        return m.chat_id in self.allowed_chats or m.sender_id in self.allowed_users

    def _seen_before(self, message_id: str) -> bool:
        if message_id in self._seen:
            return True
        self._seen[message_id] = None
        while len(self._seen) > 500:
            self._seen.popitem(last=False)
        return False

    def session_of(self, m: IncomingMessage) -> str:
        return f"{self.name}:{m.chat_id}"

    def help_text(self) -> str:
        return ("直接提问即可，例如：画面里有几个人？今天上午 9 点左右发生了什么？有人离开座位就提醒我。\n"
                "群聊里需要 @我。发送“清空对话”重新开始，发送“/id”查看本群和你的编号。")

    def _remember_chat(self, m: IncomingMessage) -> None:
        self.recent.pop(m.chat_id, None)
        self.recent[m.chat_id] = {"chat_id": m.chat_id, "chat_type": m.chat_type, "sender_id": m.sender_id,
                                  "title": m.extra.get("chat_title", ""),
                                  "text": (m.text or f"[{m.msg_type}]")[:40], "time": time.strftime("%m-%d %H:%M")}
        while len(self.recent) > 20:
            self.recent.popitem(last=False)

    async def handle_event(self, raw: dict[str, Any]) -> None:
        m = self.parse(raw)
        if m is None or self._seen_before(m.message_id):
            return
        log.info("收到%s消息 chat_id=%s chat_type=%s sender=%s 类型=%s：%s",
                 self.label, m.chat_id, m.chat_type, m.sender_id, m.msg_type, m.text[:50])
        self._remember_chat(m)
        if m.is_group and self.require_mention and not m.mentioned:
            return
        try:
            await self._handle(m)
        except Exception as e:
            log.exception("处理%s消息失败", self.label)
            await self.reply_text(m, f"处理失败：{e}")

    async def _handle(self, m: IncomingMessage) -> None:
        if m.text == "/id":     # 不受权限限制，方便管理员获取编号填进配置
            await self.reply_text(m, f"chat_id：{m.chat_id}\n你的 {self.user_id_label}：{m.sender_id}")
            return
        if not self._allowed(m):
            await self.reply_text(m, "你没有使用监控助手的权限，请联系管理员。")
            return
        text = m.text
        if m.msg_type == "audio":
            text = await self.voice_text(m)
            if not text:
                return
        elif m.msg_type == "image":
            await self.reply_text(m, "暂不支持图片提问，请用文字或语音描述要看什么。")
            return
        if not text:
            return
        if text in ("/help", "帮助"):
            await self.reply_text(m, self.help_text())
            return
        if text in RESET_WORDS:
            self.host.reset_session(self.session_of(m))
            await self.reply_text(m, "已清空对话。")
            return
        readonly = bool(self.admin_users) and m.sender_id not in self.admin_users
        reply = await self.host.ask(text, self.session_of(m), source=self.name, readonly=readonly)
        if reply.interrupted:
            return
        att = reply.attachments[-1] if reply.attachments else None
        image = await self._image(att.jpeg if att else None)
        await self.reply_answer(m, reply.text, image, att.caption if att else "")

    async def recognize(self, m: IncomingMessage, pcm: bytes, sample_rate: int = 16000) -> str:
        """用本机语音识别把 PCM 转成文字，识别不了时回复提示。"""
        text = await self.host.transcribe(pcm, sample_rate)
        if text is None:
            await self.reply_text(m, "没有配置语音识别，请发文字。")
            return ""
        if not text.strip():
            await self.reply_text(m, "没听清，请再说一遍。")
            return ""
        log.info("%s语音识别：%s", self.label, text)
        return text.strip()

    async def _image(self, jpeg: bytes | None) -> Any:
        if not (jpeg and self.send_images):
            return None
        try:
            return await self.upload_image(jpeg)
        except Exception as e:
            log.warning("上传图片到%s失败：%s", self.label, e)
            return None

    # ---------------------------------------------------------------- 告警
    async def _alert_loop(self, q: asyncio.Queue) -> None:
        try:
            while True:
                msg = await q.get()
                try:
                    if msg.get("kind") == "report":
                        await self.push_report(msg)
                    else:
                        await self.push_alert(msg)
                except Exception:
                    log.exception("%s推送失败", self.label)
        finally:
            self.host.bus.unsubscribe(TOPIC_ALERT, q)

    def chats_for(self, msg: dict[str, Any], default: list[str]) -> list[str]:
        """这条消息要发到本入口的哪些群：
        没有 targets（旧版提醒）→ 默认群；targets 里有 "default" 或本入口名 → 默认群；"<入口名>:<群>" → 指定的群。"""
        targets = msg.get("targets")
        if targets is None:
            return list(default)
        chats: list[str] = []
        for t in targets:
            if t in ("default", self.name):
                chats += default
            elif t.startswith(self.name + ":"):
                chats.append(t.split(":", 1)[1])
        return list(dict.fromkeys(c for c in chats if c))

    def _report_result(self, msg: dict[str, Any], chat: str, ok: bool, error: str = "") -> None:
        fn = getattr(self.host, "notify_result", None)
        if fn and msg.get("alert_id"):
            fn(msg["alert_id"], f"{self.label}·{chat[:12]}", ok, error)

    async def push_alert(self, alert: dict[str, Any]) -> None:
        chats = self.chats_for(alert, self.alert_chats)
        if not chats:
            return
        condition, evidence = alert.get("condition", ""), alert.get("evidence", "")
        if alert.get("station"):
            condition = f"{condition}（{alert['station']}）"
        time_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(alert.get("ts") or time.time())))
        jpeg = None
        if alert.get("image") and hasattr(self.host, "alert_image"):     # 优先用复核时保存的画面
            jpeg = self.host.alert_image(alert["image"])
        image = await self._image(jpeg or self.host.snapshot())
        for chat in chats:
            try:
                await self.send_alert(chat, condition, evidence, time_text, image)
                self.host.add_note(f"{self.name}:{chat}", f"[{time_text}] 提醒：{condition}。{evidence}")
                self._report_result(alert, chat, True)
            except Exception as e:
                log.warning("推送%s告警到 %s 失败：%s", self.label, chat, e)
                self._report_result(alert, chat, False, str(e))

    async def push_report(self, msg: dict[str, Any]) -> None:
        """汇报类职责生成的报告。"""
        for chat in self.chats_for(msg, self.report_chats):
            try:
                await self.send_report_to(chat, msg.get("title") or "监控报告", msg.get("text") or "")
            except Exception as e:
                log.warning("发送%s报告到 %s 失败：%s", self.label, chat, e)

    # ---------------------------------------------------------------- 定时报告
    def due_report(self, now: float) -> bool:
        """当前时间是否刚过某个报告时间点（且本时间点还没发过）。"""
        t = time.localtime(now)
        for hm in self.report_times:
            try:
                h, mi = (int(x) for x in hm.split(":"))
            except ValueError:
                continue
            target = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, h, mi, 0, 0, 0, -1))
            if self._last_report < target <= now:
                return True
        return False

    async def send_report(self, now: float) -> None:
        start = self._last_report
        self._last_report = now
        text = await self.host.report(start, now)
        title = (f"监控报告 {time.strftime('%m-%d %H:%M', time.localtime(start))} ~ "
                 f"{time.strftime('%H:%M', time.localtime(now))}")
        for chat in self.report_chats:
            try:
                await self.send_report_to(chat, title, text)
            except Exception as e:
                log.warning("发送%s报告到 %s 失败：%s", self.label, chat, e)

    async def _report_loop(self) -> None:
        while True:
            now = time.time()
            if self.due_report(now):
                try:
                    await self.send_report(now)
                except Exception as e:
                    log.warning("生成监控报告失败：%s", e)
            await asyncio.sleep(30)
