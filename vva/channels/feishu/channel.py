"""飞书入口：
  - 问答与指令：私聊机器人，或在群里 @机器人；支持文字和语音消息，回答附带当时看过的画面
  - 告警推送：监控规则触发时，向 alert_chats 发告警卡片（含画面），群里可以接着追问
  - 定时报告：在 report_times 的每个时间点，把上次报告以来的情况发到 report_chats
  - 权限：allowed_* 控制谁能用；admin_users 控制谁能修改提醒规则和备忘

会话：每个群、每个私聊各一个会话（feishu:<chat_id>），上下文互不干扰。
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from typing import Any

from vva.audio.utils import decode_audio_ffmpeg
from vva.channels.base import Channel, ChannelHost
from vva.channels.feishu import cards
from vva.channels.feishu.api import FeishuAPI
from vva.channels.feishu.message import IncomingMessage, parse_event
from vva.core.bus import TOPIC_ALERT

log = logging.getLogger(__name__)

HELP_TEXT = ("直接提问即可，例如：画面里有几个人？今天上午 9 点左右发生了什么？有人离开座位就提醒我。\n"
             "群聊里需要 @我。发送“清空对话”重新开始，发送“/id”查看本群和你的编号。")
RESET_WORDS = {"清空对话", "/reset", "重新开始"}


class FeishuChannel(Channel):
    name = "feishu"

    def __init__(self, cfg: dict[str, Any], host: ChannelHost, api: FeishuAPI | None = None,
                 receiver: Any = None) -> None:
        """api / receiver 可注入（测试用）；默认按配置创建。"""
        super().__init__(cfg, host)
        self.api = api
        self.receiver = receiver
        self.allowed_chats = set(cfg.get("allowed_chats") or [])
        self.allowed_users = set(cfg.get("allowed_users") or [])
        self.admin_users = set(cfg.get("admin_users") or [])
        self.require_mention = bool(cfg.get("require_mention", True))
        self.alert_chats = list(cfg.get("alert_chats") or [])
        self.report_chats = list(cfg.get("report_chats") or [])
        self.report_times = [str(t) for t in cfg.get("report_times") or []]
        self.web_url = cfg.get("web_url") or ""
        self.send_images = bool(cfg.get("send_images", True))
        self._seen: OrderedDict[str, None] = OrderedDict()      # 去重：飞书可能重复推送同一条消息
        self._tasks: list[asyncio.Task] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._last_report = time.time()

    # ---------------------------------------------------------------- 生命周期
    async def start(self) -> None:
        app_id, secret = self.cfg.get("app_id"), self.cfg.get("app_secret")
        if not (app_id and secret) and self.api is None:
            log.error("飞书入口未配置 app_id / app_secret，已跳过")
            return
        self._loop = asyncio.get_running_loop()
        domain = self.cfg.get("domain") or "https://open.feishu.cn"
        self.api = self.api or FeishuAPI(app_id, secret, domain)
        if self.receiver is None:
            from vva.channels.feishu.receiver import LongConnectionReceiver
            self.receiver = LongConnectionReceiver(app_id, secret, self._on_event_threadsafe,
                                                   "" if domain == "https://open.feishu.cn" else domain)
        try:
            self.receiver.start()
        except Exception as e:
            log.error("飞书消息接收启动失败（仍可推送告警）：%s", e)
        if self.alert_chats:     # 先订阅再启动任务，启动后立即发生的告警也不会漏
            q = self.host.bus.subscribe(TOPIC_ALERT)
            self._tasks.append(asyncio.create_task(self._alert_loop(q), name="feishu-alerts"))
        if self.report_chats and self.report_times:
            self._tasks.append(asyncio.create_task(self._report_loop(), name="feishu-reports"))
        log.info("飞书入口已启动：告警群 %d 个，报告群 %d 个，报告时间 %s", len(self.alert_chats),
                 len(self.report_chats), self.report_times or "无")

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        if self.api:
            await self.api.close()

    # ---------------------------------------------------------------- 接收消息
    def _on_event_threadsafe(self, event: dict[str, Any]) -> None:
        """在 SDK 线程里调用：切回主事件循环处理，立即返回。"""
        if self._loop:
            self._loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self.handle_event(event)))

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

    @staticmethod
    def session_of(m: IncomingMessage) -> str:
        return f"feishu:{m.chat_id}"

    async def handle_event(self, event: dict[str, Any]) -> None:
        m = parse_event(event)
        if m is None or self._seen_before(m.message_id):
            return
        log.info("收到飞书消息 chat_id=%s chat_type=%s open_id=%s 类型=%s：%s",
                 m.chat_id, m.chat_type, m.sender_id, m.msg_type, m.text[:50])
        if m.is_group and self.require_mention and not m.mentioned:
            return
        try:
            await self._handle(m)
        except Exception as e:
            log.exception("处理飞书消息失败")
            await self._reply_text(m, f"处理失败：{e}")

    async def _handle(self, m: IncomingMessage) -> None:
        if m.text == "/id":     # 不受权限限制，方便管理员获取编号填进配置
            await self._reply_text(m, f"chat_id：{m.chat_id}\n你的 open_id：{m.sender_id}")
            return
        if not self._allowed(m):
            await self._reply_text(m, "你没有使用监控助手的权限，请联系管理员。")
            return
        text = m.text
        if m.msg_type == "audio":
            text = await self._transcribe(m)
            if not text:
                return
        elif m.msg_type == "image":
            await self._reply_text(m, "暂不支持图片提问，请用文字或语音描述要看什么。")
            return
        if not text:
            return
        if text in ("/help", "帮助"):
            await self._reply_text(m, HELP_TEXT)
            return
        if text in RESET_WORDS:
            self.host.reset_session(self.session_of(m))
            await self._reply_text(m, "已清空对话。")
            return
        readonly = bool(self.admin_users) and m.sender_id not in self.admin_users
        reply = await self.host.ask(text, self.session_of(m), source="feishu", readonly=readonly)
        if reply.interrupted:
            return
        await self._reply_answer(m, reply)

    async def _transcribe(self, m: IncomingMessage) -> str:
        audio = await self.api.download_resource(m.message_id, m.file_key, "file")
        pcm = await decode_audio_ffmpeg(audio, 16000)
        text = await self.host.transcribe(pcm, 16000)
        if text is None:
            await self._reply_text(m, "没有配置语音识别，请发文字。")
            return ""
        if not text.strip():
            await self._reply_text(m, "没听清，请再说一遍。")
            return ""
        log.info("飞书语音识别：%s", text)
        return text.strip()

    # ---------------------------------------------------------------- 回复
    async def _reply_text(self, m: IncomingMessage, text: str) -> None:
        await self.api.reply(m.message_id, "text", {"text": text})

    async def _image_key(self, jpeg: bytes | None) -> str | None:
        if not (jpeg and self.send_images):
            return None
        try:
            return await self.api.upload_image(jpeg)
        except Exception as e:
            log.warning("上传图片到飞书失败：%s", e)
            return None

    async def _reply_answer(self, m: IncomingMessage, reply: Any) -> None:
        att = reply.attachments[-1] if reply.attachments else None
        key = await self._image_key(att.jpeg if att else None)
        await self.api.reply(m.message_id, "interactive",
                             cards.answer_card(reply.text, key, att.caption if att else ""))

    # ---------------------------------------------------------------- 告警
    async def _alert_loop(self, q: asyncio.Queue) -> None:
        try:
            while True:
                await self.push_alert(await q.get())
        finally:
            self.host.bus.unsubscribe(TOPIC_ALERT, q)

    async def push_alert(self, alert: dict[str, Any]) -> None:
        condition, evidence = alert.get("condition", ""), alert.get("evidence", "")
        time_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(alert.get("ts") or time.time())))
        key = await self._image_key(self.host.snapshot())
        card = cards.alert_card(condition, evidence, time_text, key, self.web_url)
        for chat in self.alert_chats:
            try:
                await self.api.send(chat, "interactive", card)
                self.host.add_note(f"feishu:{chat}", f"[{time_text}] 提醒：{condition}。{evidence}")
            except Exception as e:
                log.warning("推送飞书告警到 %s 失败：%s", chat, e)

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
        fmt = "%m-%d %H:%M"
        title = f"监控报告 {time.strftime(fmt, time.localtime(start))} ~ {time.strftime('%H:%M', time.localtime(now))}"
        for chat in self.report_chats:
            try:
                await self.api.send(chat, "interactive", cards.report_card(title, text, self.web_url))
            except Exception as e:
                log.warning("发送飞书报告到 %s 失败：%s", chat, e)

    async def _report_loop(self) -> None:
        while True:
            now = time.time()
            if self.due_report(now):
                try:
                    await self.send_report(now)
                except Exception as e:
                    log.warning("生成监控报告失败：%s", e)
            await asyncio.sleep(30)
