"""钉钉入口的测试（用假的接口和消息，不需要 dingtalk-stream 和网络）。"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tests.test_channels import FakeHost, FakeReceiver  # noqa: E402
from vva.channels import build_channels  # noqa: E402
from vva.channels.dingtalk.api import DingTalkAPI, DingTalkError  # noqa: E402
from vva.channels.dingtalk.channel import DingTalkChannel  # noqa: E402
from vva.channels.dingtalk.message import parse_message  # noqa: E402
from vva.core.bus import TOPIC_ALERT  # noqa: E402


def _msg(text="画面里有几个人", group=False, mid="msg1", cid="cidGroup==", staff="u001", mtype="text",
         content=None, at=True):
    data = {"msgId": mid, "conversationId": cid, "conversationType": "2" if group else "1",
            "senderStaffId": staff, "senderId": "$:LWCP:x", "msgtype": mtype, "isInAtList": at,
            "conversationTitle": "值班群" if group else "",
            "sessionWebhook": "https://oapi.dingtalk.com/robot/sendBySession?session=s1",
            "sessionWebhookExpiredTime": int((time.time() + 3600) * 1000), "robotCode": "dingbot"}
    if mtype == "text":
        data["text"] = {"content": " " + text}
    if content is not None:
        data["content"] = content
    return data


class FakeDingAPI:
    def __init__(self):
        self.hooks, self.markdowns, self.images, self.uploads = [], [], [], 0

    async def reply_webhook(self, webhook, title, text, at_user=""):
        self.hooks.append((webhook, title, text, at_user))

    async def send_markdown(self, chat, title, text):
        self.markdowns.append((chat, title, text))

    async def send_image(self, chat, media_id):
        self.images.append((chat, media_id))

    async def upload_image(self, jpeg):
        self.uploads += 1
        return f"@media{self.uploads}"

    async def download_file(self, code, robot_code=""):
        return b"amr-bytes"

    async def close(self):
        pass


def _channel(**cfg):
    host, api = FakeHost(), FakeDingAPI()
    ch = DingTalkChannel({"client_id": "ding_a", "client_secret": "s", **cfg}, host, api=api, receiver=FakeReceiver())
    return ch, host, api


def test_parse_message_variants():
    m = parse_message(_msg(group=True))
    assert m.text == "画面里有几个人" and m.is_group and m.mentioned and m.chat_id == "cidGroup=="
    assert m.sender_id == "u001" and m.extra["webhook"].endswith("s1") and m.extra["chat_title"] == "值班群"
    p = parse_message(_msg())
    assert p.chat_type == "p2p" and p.mentioned                       # 单聊不需要 @
    rich = parse_message(_msg(mtype="richText", content={"richText": [{"text": "门口"}, {"pictureDownloadCode": "x"},
                                                                       {"text": "有人吗"}]}))
    assert rich.msg_type == "text" and rich.text == "门口有人吗"
    a = parse_message(_msg(mtype="audio", content={"duration": 2000, "downloadCode": "dc", "recognition": "几个人"}))
    assert a.msg_type == "audio" and a.text == "几个人" and a.file_key == "dc"
    assert parse_message(_msg(mtype="picture", content={"downloadCode": "pc"})).msg_type == "image"
    assert parse_message({"msgId": "x"}) is None


def test_dingtalk_question_reply_and_commands():
    async def run():
        ch, host, api = _channel()
        await ch.handle_event(_msg(group=True))
        assert host.asked == [("画面里有几个人", "dingtalk:cidGroup==", False)]
        hook, title, text, at_user = api.hooks[-1]
        assert hook.endswith("s1") and text.startswith("画面中有 2 人。") and at_user == "u001"
        assert api.images == [("cidGroup==", "@media1")]                   # 画面另发一张图片到群里
        await ch.handle_event(_msg(group=True))                             # 重复推送：忽略
        assert len(host.asked) == 1
        await ch.handle_event(_msg("/id", group=True, mid="m2"))
        assert "cidGroup==" in api.hooks[-1][2] and "userId：u001" in api.hooks[-1][2]
        await ch.handle_event(_msg("清空对话", mid="m3", cid="cidP2P"))
        assert host.resets == ["dingtalk:cidP2P"]
        await ch.handle_event(_msg("几个人", mid="m4", cid="cidP2P"))       # 单聊回答的图片发给本人
        assert api.images[-1] == ("u001", "@media2")
    asyncio.run(run())


def test_dingtalk_expired_webhook_falls_back_to_api():
    async def run():
        ch, host, api = _channel()
        data = _msg(group=True)
        data["sessionWebhookExpiredTime"] = int((time.time() - 10) * 1000)
        await ch.handle_event(data)
        assert api.hooks == [] and api.markdowns[-1][0] == "cidGroup=="
    asyncio.run(run())


def test_dingtalk_permissions_and_voice(monkeypatch):
    async def run():
        ch, host, api = _channel(allowed_users=["u_ok"], admin_users=["u_admin"])
        await ch.handle_event(_msg(staff="u_other"))
        assert host.asked == [] and "没有使用监控助手的权限" in api.hooks[-1][2]
        await ch.handle_event(_msg(mid="m2", staff="u_ok", mtype="audio",
                                   content={"downloadCode": "dc", "recognition": "门口有人吗"}))
        assert host.asked[-1] == ("门口有人吗", "dingtalk:cidGroup==", True)     # 用钉钉的识别结果；非管理员只读

        import vva.channels.dingtalk.channel as mod

        async def fake_decode(data, sr):
            assert data == b"amr-bytes"
            return b"\x00\x00" * 1600
        monkeypatch.setattr(mod, "decode_audio_ffmpeg", fake_decode)
        await ch.handle_event(_msg(mid="m3", staff="u_ok", mtype="audio", content={"downloadCode": "dc"}))
        assert host.asked[-1][0] == "门口有人吗"                                # 没有识别结果：下载后本机识别
    asyncio.run(run())


def test_dingtalk_alert_and_report():
    async def run():
        ch, host, api = _channel(alert_chats=["cidAlert"], report_chats=["cidRep"], report_times=["18:00"],
                                 web_url="https://x/browser")
        await ch.start()
        host.bus.bind_loop(asyncio.get_running_loop())
        host.bus.publish(TOPIC_ALERT, {"condition": "有人离开座位", "evidence": "工位空着", "ts": time.time()})
        await asyncio.sleep(0.05)
        chat, title, text = api.markdowns[0]
        assert chat == "cidAlert" and "有人离开座位" in title and "[查看现场](https://x/browser)" in text
        assert api.images == [("cidAlert", "@media1")] and host.notes[0][0] == "dingtalk:cidAlert"
        t = time.localtime()
        at = lambda h, m: time.mktime((t.tm_year, t.tm_mon, t.tm_mday, h, m, 0, 0, 0, -1))  # noqa: E731
        ch._last_report = at(17, 0)
        await ch.send_report(at(18, 1))
        assert api.markdowns[-1][0] == "cidRep" and "今天共 2 次提醒。" in api.markdowns[-1][2]
        assert ch.status()["alert_push"] and ch.status()["report"]
        await ch.stop()
    asyncio.run(run())


def test_build_dingtalk_from_config():
    chans = build_channels({"dingtalk": {"enabled": True, "client_id": "a", "client_secret": "b"}}, FakeHost())
    assert [c.name for c in chans] == ["dingtalk"]


class _Resp:
    def __init__(self, data=None, status=200, content=b""):
        self._data, self.status_code, self.content = data, status, content

    def json(self):
        return self._data


class FakeHTTP:
    def __init__(self):
        self.calls = []

    async def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        if url.endswith("/oauth2/accessToken"):
            return _Resp({"accessToken": "tok", "expireIn": 7200})
        if "/media/upload" in url:
            return _Resp({"errcode": 0, "media_id": "@lADmedia"})
        if url.endswith("/messageFiles/download"):
            return _Resp({"downloadUrl": "https://file/x"})
        if url == "https://file/x":
            return _Resp(content=b"voice")
        if "sendBySession" in url:
            return _Resp({"errcode": 310000, "errmsg": "sign not match"})
        if "oToMessages" in url:
            return _Resp({"code": "Forbidden.AccessDenied", "message": "没有权限"}, status=403)
        return _Resp({"processQueryKey": "k"})


def test_dingtalk_api_requests():
    async def run():
        http = FakeHTTP()
        api = DingTalkAPI("ding_a", "s", http=http)
        await api.send_markdown("cidG", "标题", "正文")
        assert await api.upload_image(b"jpg") == "@lADmedia"
        assert await api.download_file("dc") == b"voice"
        assert len([c for c in http.calls if c[1].endswith("accessToken")]) == 1        # 凭证会缓存
        method, url, kw = http.calls[1]
        assert url.endswith("/v1.0/robot/groupMessages/send") and kw["headers"]["x-acs-dingtalk-access-token"] == "tok"
        assert kw["json"]["robotCode"] == "ding_a" and kw["json"]["msgKey"] == "sampleMarkdown"
        assert json.loads(kw["json"]["msgParam"]) == {"title": "标题", "text": "正文"}
        assert "access_token=tok" in http.calls[2][1]
        for coro in (api.reply_webhook("https://oapi.dingtalk.com/robot/sendBySession?session=1", "t", "x"),
                     api.send_markdown("u001", "t", "x")):
            try:
                await coro
                raise AssertionError("应抛出 DingTalkError")
            except DingTalkError:
                pass
    asyncio.run(run())


def test_channel_settings_api(tmp_path):
    import pytest
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from vva.channels import ChannelManager
    from vva.web.channels_api import register_channel_routes

    app = FastAPI()
    mgr = ChannelManager({}, FakeHost(), str(tmp_path / "ch"))
    static = os.path.join(ROOT, "vva", "web", "static")
    register_channel_routes(app, mgr, "pw", static)
    c = TestClient(app)
    assert c.get("/api/channels/dingtalk").status_code == 401
    h = {"X-Token": "pw"}
    r = c.get("/api/channels/dingtalk", headers=h).json()
    assert r["settings"]["secret_set"] is False and r["status"]["started"] is False
    assert c.put("/api/channels/dingtalk", headers=h, json={"enabled": True}).status_code == 400   # 没有凭证不能开启
    r = c.put("/api/channels/dingtalk", headers=h,
              json={"client_id": "ding_a", "client_secret": "sec", "alert_chats": "cidA, cidB",
                    "report_times": ["08:00"]}).json()
    assert r["settings"]["secret_set"] and "client_secret" not in r["settings"]
    assert r["settings"]["alert_chats"] == ["cidA", "cidB"]
    assert c.put("/api/channels/dingtalk", headers=h, json={"report_times": ["25:00"]}).status_code == 400
    assert mgr.config("dingtalk")["client_secret"] == "sec"
    assert c.get("/api/channels/feishu", headers=h).json()["settings"]["domain"] == "https://open.feishu.cn"
    page = c.get("/dingtalk")
    assert page.status_code == 200 and 'name: "dingtalk"' in page.text
