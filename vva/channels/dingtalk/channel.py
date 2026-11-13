"""钉钉入口：私聊机器人或在群里 @机器人 提问、下指令（文字和语音），告警和定时报告推送到指定的群。

通用的权限、指令、告警、报告逻辑在 vva.channels.chat.ChatChannel，这里只负责钉钉的收发：
  - 回复：用消息里带的会话回复地址（sessionWebhook），回答附带的画面用机器人接口另发一张图片
  - 主动推送（告警、报告）：机器人群发接口，chat 为群的 openConversationId（cid 开头）
  - 语音：优先用钉钉自带的识别结果；没有时下载语音，用本机语音识别
"""
from __future__ import annotations

import logging
import time
from typing import Any

from vva.audio.utils import decode_audio_ffmpeg
from vva.channels.chat import ChatChannel, IncomingMessage
from vva.channels.dingtalk import markdown
from vva.channels.dingtalk.api import DingTalkAPI
from vva.channels.dingtalk.message import parse_message

log = logging.getLogger(__name__)


class DingTalkChannel(ChatChannel):
    name = "dingtalk"
    label = "钉钉"
    user_id_label = "userId"
    conn_fields = ("client_id", "client_secret", "robot_code")

    def ready(self) -> bool:
        return bool(self.cfg.get("client_id") and self.cfg.get("client_secret"))

    async def connect(self) -> None:
        cid, secret = self.cfg.get("client_id"), self.cfg.get("client_secret")
        self.api = self.api or DingTalkAPI(cid, secret, self.cfg.get("robot_code") or "")
        if self.receiver is None:
            from vva.channels.dingtalk.receiver import StreamReceiver
            self.receiver = StreamReceiver(cid, secret, self._on_event_threadsafe)

    def parse(self, raw: dict[str, Any]) -> IncomingMessage | None:
        return parse_message(raw)

    # ---------------------------------------------------------------- 回复
    def _reply_target(self, m: IncomingMessage) -> str:
        """主动发送时的目标：群用 openConversationId，单聊用员工 userId。"""
        return m.chat_id if m.is_group else (m.extra.get("staff_id") or m.sender_id)

    async def _reply_markdown(self, m: IncomingMessage, title: str, text: str) -> None:
        hook = m.extra.get("webhook") or ""
        if hook and time.time() < (m.extra.get("webhook_expire") or float("inf")):
            await self.api.reply_webhook(hook, title, text, m.extra.get("staff_id", "") if m.is_group else "")
        else:
            await self.api.send_markdown(self._reply_target(m), title, text)

    async def reply_text(self, m: IncomingMessage, text: str) -> None:
        await self._reply_markdown(m, text[:30], text)

    async def reply_answer(self, m: IncomingMessage, text: str, image: Any, caption: str) -> None:
        msg = markdown.answer(text, caption if image else "")
        await self._reply_markdown(m, msg["title"], msg["text"])
        if image:
            try:
                await self.api.send_image(self._reply_target(m), image)
            except Exception as e:
                log.warning("发送钉钉画面失败：%s", e)

    async def upload_image(self, jpeg: bytes) -> str:
        return await self.api.upload_image(jpeg)

    # ---------------------------------------------------------------- 主动推送
    async def send_alert(self, chat: str, condition: str, evidence: str, time_text: str, image: Any) -> None:
        msg = markdown.alert(condition, evidence, time_text, self.web_url)
        await self.api.send_markdown(chat, msg["title"], msg["text"])
        if image:
            try:
                await self.api.send_image(chat, image)
            except Exception as e:
                log.warning("发送钉钉告警画面失败：%s", e)

    async def send_report_to(self, chat: str, title: str, text: str) -> None:
        msg = markdown.report(title, text, self.web_url)
        await self.api.send_markdown(chat, msg["title"], msg["text"])

    # ---------------------------------------------------------------- 语音
    async def voice_text(self, m: IncomingMessage) -> str:
        if m.text:                                  # 钉钉已经识别好了
            log.info("钉钉语音识别：%s", m.text)
            return m.text
        if not m.file_key:
            await self.reply_text(m, "没听清，请再说一遍。")
            return ""
        audio = await self.api.download_file(m.file_key, m.extra.get("robot_code") or "")
        pcm = await decode_audio_ffmpeg(audio, 16000)
        return await self.recognize(m, pcm, 16000)
