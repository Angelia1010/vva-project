"""飞书入口。

  channel.py   入口主体：问答、指令、告警推送、定时报告、权限
  message.py   解析“接收消息”事件
  api.py       飞书开放平台 HTTP 接口（发消息、回复、上传图片、下载语音）
  cards.py     消息卡片
  receiver.py  长连接接收消息（官方 SDK lark-oapi），无需公网 IP
"""
from vva.channels.feishu.channel import FeishuChannel

__all__ = ["FeishuChannel"]
