"""通知路由：把告警、巡检异常、报告按职责的通知对象分发出去。

不直接调用钉钉、飞书：向事件总线发一条 TOPIC_ALERT（带 targets），由各接入入口按自己的前缀取用，
值守台和现场播报由应用处理。通知对象的写法见 vva.duties.model。
"""
from vva.notify.router import NotifyRouter, describe_target

__all__ = ["NotifyRouter", "describe_target"]
