"""钉钉入口。

  channel.py   入口主体：收发消息（通用的权限、指令、告警、报告在 vva.channels.chat）
  message.py   解析机器人收到的消息
  api.py       钉钉开放平台 HTTP 接口（访问凭证、群发、单聊、上传图片、下载语音、会话回复）
  markdown.py  消息内容（Markdown）
  receiver.py  Stream 模式接收消息（官方 SDK dingtalk-stream），无需公网 IP
"""
from vva.channels.dingtalk.channel import DingTalkChannel

__all__ = ["DingTalkChannel"]
