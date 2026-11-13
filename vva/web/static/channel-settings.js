// 接入入口配置页（飞书、钉钉）的公共脚本。页面需先定义 window.CHANNEL。
const $ = (id) => document.getElementById(id);
const CH = window.CHANNEL;                  // 页面里定义：name, label, idField, secretField, texts, defaults, sdkInstall
const API_URL = "/api/channels/" + CH.name;
const LISTS = ["alert_chats", "report_chats", "allowed_chats", "allowed_users", "admin_users"];
const BOOLS = ["enabled", "send_images", "require_mention"];
let dirty = false, msgTimer = null;

function msg(text, cls) {
  const m = $("msg"); m.textContent = text; m.className = "toast " + (cls || "");
  clearTimeout(msgTimer); msgTimer = setTimeout(() => { m.textContent = ""; }, 5000);
}
function setDirty(v) { dirty = v; $("dirty").classList.toggle("on", v); }
function lines(id) { return $(id).value.split(/[\s,，、]+/).map(x => x.trim()).filter(Boolean); }

async function call(method, url, body) {
  const headers = { "X-Token": adminToken() };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(url, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  if (!res.ok) {
    let d = String(res.status); try { d = (await res.json()).detail || d; } catch (e) {}
    if (res.status === 401) { $("token").style.display = "block"; $("token").focus(); }
    throw new Error(d);
  }
  return res.json();
}

function renderState(data) {
  const s = data.settings, st = data.status, el = $("state");
  let cls = "", title = "", detail = "";
  if (!s.enabled) { title = "未开启"; detail = `填写应用凭证后打开“开启${CH.label}接入”并保存。`; }
  else if (!st.started) { cls = "warn"; title = "已开启，等待启动"; detail = "重启服务后生效。"; }
  else if (st.receiver === "error") { cls = "err"; title = "消息接收出错"; detail = st.error || "请查看服务日志。仍可推送告警。"; }
  else {
    cls = "ok"; title = "运行中";
    const parts = [st.last_message ? `最近收到消息 ${st.last_message}` : "还没有收到消息",
                   st.alert_push ? "告警推送已开启" : "未设置告警群", st.report ? "定时报告已开启" : "未设置定时报告"];
    detail = parts.join("，");
  }
  el.className = "state " + cls; $("state-title").textContent = title; $("state-detail").textContent = detail;

  const notes = $("notes"); notes.innerHTML = "";
  const add = (html, extraCls) => { const d = document.createElement("div"); d.className = "note " + (extraCls || ""); d.innerHTML = html; notes.appendChild(d); return d; };
  if (data.restart_required) {
    const d = add(`<span style="flex:1">连接参数已修改，需要重启服务才能生效。</span>`, "restart");
    if (data.env.restart_supported) {
      const b = document.createElement("button"); b.className = "primary"; b.textContent = "重启服务"; b.onclick = restart; d.appendChild(b);
    } else { d.querySelector("span").textContent += "请在服务器上执行 sudo systemctl restart vva-app。"; }
  }
  if (!data.env.sdk) add(`服务器还没有安装接收${CH.label}消息的组件，收不到消息（推送不受影响）。在服务器上执行：<code>source .venv/bin/activate && ${CH.sdkInstall}</code>，然后重启服务。`);
  if (!data.env.ffmpeg && CH.needsFfmpeg) add(`服务器没有安装 ffmpeg，${CH.label}里的语音消息无法识别（文字不受影响）。安装：<code>sudo apt install -y ffmpeg</code>`);
}

function renderRecent(st) {
  const box = $("recent"); box.innerHTML = "";
  if (!st.recent || !st.recent.length) {
    box.innerHTML = '<p class="empty">还没有收到消息。把机器人拉进群并 @它 说句话，或私聊它，这里会列出会话，可以一键设为告警群、报告群。</p>';
    return;
  }
  for (const c of st.recent) {
    const el = document.createElement("div"); el.className = "chat";
    el.innerHTML = `<div class="head"><span class="kind">${c.chat_type === "group" ? "群聊" : "私聊"}</span><span class="id"></span><span class="when">${c.time}</span></div>
      <div class="said"></div><div class="acts"></div>`;
    el.querySelector(".id").textContent = c.title ? `${c.title}  ${c.chat_id}` : c.chat_id;
    el.querySelector(".said").textContent = "“" + c.text + "”";
    const acts = el.querySelector(".acts");
    const btn = (label, field, value) => {
      const b = document.createElement("button"); b.type = "button"; b.textContent = label;
      b.onclick = () => {
        const cur = lines(field);
        if (!cur.includes(value)) { $(field).value = [...cur, value].join("\n"); setDirty(true); msg(`已加入“${$(field).labels[0].firstChild.textContent.trim()}”，记得保存`, "ok"); }
        else msg("已经在列表里了", "ok");
      };
      acts.appendChild(b);
    };
    if (c.chat_type === "group") { btn("设为告警群", "alert_chats", c.chat_id); btn("设为报告群", "report_chats", c.chat_id); btn("允许此群使用", "allowed_chats", c.chat_id); }
    btn("发消息的人设为管理员", "admin_users", c.sender_id);
    const t = document.createElement("button"); t.type = "button"; t.textContent = "发测试消息";
    t.onclick = () => { $("test_chat").value = c.chat_id; sendTest(); };
    acts.appendChild(t);
    box.appendChild(el);
  }
}

function render(data, keepForm) {
  const s = data.settings;
  if (!keepForm) {
    for (const k of BOOLS) $(k).checked = !!s[k];
    for (const k of [CH.idField, ...CH.texts]) $(k).value = s[k] || (CH.defaults || {})[k] || "";
    $(CH.secretField).value = "";
    $(CH.secretField).placeholder = s.secret_set ? "已设置，留空表示不修改" : "在开放平台复制";
    $("web_url").value = s.web_url || (location.origin + "/browser");
    $("report_times").value = (s.report_times || []).join(", ");
    for (const k of LISTS) $(k).value = (s[k] || []).join("\n");
    setDirty(false);
  }
  renderState(data); renderRecent(data.status);
  if (!s.enabled && !s[CH.idField]) $("steps").open = true;
}

async function refresh(keepForm) {
  try { render(await call("GET", API_URL), keepForm); }
  catch (e) { $("state-title").textContent = "无法读取配置"; $("state-detail").textContent = e.message; }
}

function credentials() {
  const body = {};
  for (const k of [CH.idField, CH.secretField, ...CH.texts]) body[k] = $(k).value.trim();
  return body;
}

function formBody() {
  const body = { ...credentials(), web_url: $("web_url").value.trim(), report_times: lines("report_times") };
  for (const k of BOOLS) body[k] = $(k).checked;
  for (const k of LISTS) body[k] = lines(k);
  return body;
}

$("btn-save").onclick = async () => {
  try { render(await call("PUT", API_URL, formBody())); msg("已保存", "ok"); }
  catch (e) { msg("没有保存：" + e.message, "err"); }
};
$("btn-check").onclick = async () => {
  const r = $("check-result"); r.className = "result"; r.textContent = "正在连接…";
  try { const x = await call("POST", API_URL + "/check", credentials());
        r.className = "result " + (x.ok ? "ok" : "err"); r.textContent = x.message; }
  catch (e) { r.className = "result err"; r.textContent = e.message; }
};
async function sendTest() {
  const r = $("test-result"); r.className = "result"; r.textContent = "正在发送…";
  try { const x = await call("POST", API_URL + "/send_test", { chat_id: $("test_chat").value.trim() });
        r.className = "result " + (x.ok ? "ok" : "err"); r.textContent = x.message; }
  catch (e) { r.className = "result err"; r.textContent = e.message; }
}
$("btn-test").onclick = sendTest;
async function restart() {
  if (!confirm("重启服务会中断正在进行的对话，大约 10 秒后恢复。继续？")) return;
  try { const x = await call("POST", "/api/system/restart"); msg(x.message, "ok"); setTimeout(() => location.reload(), 10000); }
  catch (e) { msg("重启失败：" + e.message, "err"); }
}
document.querySelectorAll("input, textarea, select").forEach(el => {
  if (el.id !== "test_chat" && el.id !== "token") el.addEventListener("input", () => setDirty(true));
});
window.addEventListener("beforeunload", (e) => { if (dirty) { e.preventDefault(); e.returnValue = ""; } });
refresh(false);
setInterval(() => refresh(true), 15000);     // 定时刷新状态和最近的消息，不覆盖正在编辑的内容
