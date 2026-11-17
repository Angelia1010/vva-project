"""职责与推送自检：看“发现了却没推到钉钉 / 飞书”卡在哪一环。服务运行时也可以执行（只读）。

用法：
    source .venv/bin/activate
    python scripts/check_duties.py -c config.yaml
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from vva.core.config import load_config  # noqa: E402
from vva.duties.model import Duty  # noqa: E402

LABEL = {"dingtalk": "钉钉", "feishu": "飞书"}
SDK = {"dingtalk": "dingtalk_stream", "feishu": "lark_oapi"}
STATUS = {"pending": "待处理", "handling": "处理中", "resolved": "已处理", "false": "误报", "trial": "试运行", "record": "记录"}


def ok(t: str) -> None:
    print(f"  [正常] {t}")


def bad(t: str, problems: list[str]) -> None:
    print(f"  [问题] {t}")
    problems.append(t)


def channel_cfg(cfg: dict, name: str) -> dict:
    import yaml
    c = dict((cfg.get("channels") or {}).get(name) or {})
    path = os.path.join("data/channels", f"{name}.yaml")
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            c.update(yaml.safe_load(f) or {})
    return c


def recent(name: str) -> list[dict]:
    try:
        with open(os.path.join("data/channels", f"{name}.recent.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default="config.yaml")
    args = ap.parse_args()
    cfg = load_config(args.config)
    problems: list[str] = []
    db_path = cfg["duties"]["db"]
    if not os.path.isfile(db_path):
        print(f"没有找到业务库 {db_path}：服务还没用新版本启动过，或者不是在项目目录下执行。")
        return
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row

    print("== 岗位")
    row = db.execute("SELECT data FROM station").fetchone()
    st = json.loads(row["data"]) if row else {}
    print(f"  {st.get('name', '?')}")
    if st.get("paused"):
        bad("岗位处于“暂停值守”状态，所有职责都不判断（设置页取消暂停）", problems)

    print("\n== 后台观察")
    if cfg["observer"].get("enabled", True):
        ok(f"已开启，约每 {cfg['observer'].get('rule_check_interval', 8)} 秒判断一次职责")
    else:
        bad("observer.enabled 为 false，后台不看画面，职责不会触发", problems)
    if cfg["video"].get("type") == "push":
        print("  [提示] 画面来自浏览器摄像头：值守台页面要一直开着并且“开启摄像头”，关掉页面就没有画面可看")

    print("\n== 通知渠道")
    chans = {}
    for name in ("dingtalk", "feishu"):
        c = channel_cfg(cfg, name)
        chans[name] = c
        lab = LABEL[name]
        if not c.get("enabled"):
            print(f"  [未开启] {lab}")
            continue
        ok(f"{lab}已开启")
        if importlib.util.find_spec(SDK[name]) is None:
            bad(f"{lab}：没有安装 {SDK[name]}，收不到群消息（推送不受影响）", problems)
        groups = [r for r in recent(name) if r.get("chat_type") == "group"]
        print(f"  {lab}默认告警群：{c.get('alert_chats') or '未设置'}")
        print(f"  {lab}最近发过消息的群：" + ("、".join(f"{r.get('title') or ''}({r['chat_id']})" for r in groups) or "无"))

    print("\n== 职责")
    now = time.time()
    duties = [Duty.from_record(json.loads(r["data"])) for r in db.execute("SELECT data FROM duties ORDER BY rowid")]
    if not duties:
        bad("没有任何职责", problems)
    for d in duties:
        flags = [d.kind_label, "启用" if d.enabled else "已暂停", "试运行" if d.mode == "trial" else "正式",
                 "复核" if d.verify else "不复核", f"冷却{int(d.cooldown_s // 60)}分钟"]
        print(f"- {d.id} {d.name}：{d.condition}")
        print(f"    {'，'.join(flags)}；生效 {d.schedule_text()}；通知 {d.effective_targets()}")
        if not d.enabled:
            bad(f"【{d.name}】已暂停", problems)
        if d.expired(now):
            bad(f"【{d.name}】临时任务已到期", problems)
        if d.kind == "watch" and not d.in_schedule(now):
            print("    [提示] 现在不在生效时间内")
        if d.mode == "trial":
            bad(f"【{d.name}】是试运行：只记录不通知（编辑职责关掉“试运行”）", problems)
        external = []
        for t in d.effective_targets():
            ch, _, chat = t.partition(":")
            if t == "default":
                for name, c in chans.items():
                    if c.get("enabled") and c.get("alert_chats"):
                        external.append(f"{LABEL[name]}默认告警群")
            elif ch in chans and chat:
                if chans[ch].get("enabled"):
                    external.append(f"{LABEL[ch]}·{chat}")
                else:
                    bad(f"【{d.name}】要发到{LABEL[ch]}，但{LABEL[ch]}没有开启", problems)
        if not external and d.kind != "inspect":
            bad(f"【{d.name}】的通知对象里没有钉钉 / 飞书群（只有 {d.effective_targets()}），所以不会推送到群。"
                "编辑这条职责，在“发现后通知谁”里选上钉钉的群", problems)

    print("\n== 最近 10 条告警")
    try:
        rows = db.execute("SELECT id, ts, status, data FROM alerts ORDER BY id DESC LIMIT 10").fetchall()
    except sqlite3.OperationalError:
        rows = []
    if not rows:
        print("  还没有任何告警：职责从没被判定命中。看服务日志里有没有“疑似命中”“放大复核不成立”")
    for r in rows:
        a = json.loads(r["data"])
        print(f"- #{r['id']} {time.strftime('%m-%d %H:%M:%S', time.localtime(r['ts']))} {a.get('duty_name')} "
              f"[{STATUS.get(r['status'], r['status'])}] {a.get('evidence', '')[:40]}")
        for e in db.execute("SELECT ts, text FROM alert_events WHERE alert_id = ? ORDER BY id", (r["id"],)):
            print(f"    {time.strftime('%H:%M:%S', time.localtime(e['ts']))} {e['text']}")

    print("\n== 结论")
    if problems:
        for p in dict.fromkeys(problems):
            print(f"  - {p}")
    else:
        print("  配置看起来没问题。如果还是没推送，看日志：")
    print("  实时日志：journalctl -u vva-app -f --no-pager | grep --line-buffered -E \"疑似命中|复核|告警 #|推送|钉钉|ERROR\"")


if __name__ == "__main__":
    main()
