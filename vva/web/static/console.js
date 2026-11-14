// AI 值守台：所有页面共用的脚本（时钟、管理口令、接口请求）
(function () {
  const clock = document.getElementById("clock");
  if (clock) {
    const tick = () => { clock.textContent = new Date().toLocaleTimeString("zh-CN", { hour12: false }); };
    tick(); setInterval(tick, 1000);
  }
  // 管理口令：在本标签页内记住（关闭标签页即清除），切换页面不用重新输入
  const tokenInput = document.getElementById("token");
  if (tokenInput) {
    try { tokenInput.value = sessionStorage.getItem("vva-token") || ""; } catch (e) {}
    if (tokenInput.value) tokenInput.style.display = "block";
    tokenInput.addEventListener("input", () => { try { sessionStorage.setItem("vva-token", tokenInput.value.trim()); } catch (e) {} });
  }
})();

window.adminToken = function () {
  const el = document.getElementById("token");
  if (el && el.value.trim()) return el.value.trim();
  try { return sessionStorage.getItem("vva-token") || ""; } catch (e) { return ""; }
};

window.vva = {
  // 调用后端接口：返回 JSON；失败时抛出带中文说明的错误；需要口令时显示口令输入框
  async api(method, url, body) {
    const headers = { "X-Token": window.adminToken() };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const res = await fetch(url, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
    if (!res.ok) {
      let detail = String(res.status);
      try { detail = (await res.json()).detail || detail; } catch (e) {}
      if (res.status === 401) {
        const t = document.getElementById("token");
        if (t) { t.style.display = "block"; t.focus(); }
        detail = "需要管理口令：请在右上角输入后重试";
      }
      throw new Error(detail);
    }
    return res.json();
  },
  esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  },
  // 告警截图地址
  img(file) { return file ? "/api/alerts/image/" + file.split("/").map(encodeURIComponent).join("/") : ""; },
  ago(ts) {
    const s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 60) return "刚刚";
    if (s < 3600) return Math.floor(s / 60) + " 分钟前";
    if (s < 86400) return Math.floor(s / 3600) + " 小时前";
    return Math.floor(s / 86400) + " 天前";
  },
  hm(ts) { return new Date(ts * 1000).toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit" }); },
};
