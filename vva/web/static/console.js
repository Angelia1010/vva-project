// 值班台公共脚本：时钟、管理口令（三个页面共用）
(function () {
  const clock = document.getElementById("clock");
  if (clock) {
    const tick = () => { clock.textContent = new Date().toLocaleTimeString("zh-CN", { hour12: false }); };
    tick(); setInterval(tick, 1000);
  }
})();

// 管理口令只在本页内存里保存；配置了 web.admin_token 时由页面显示输入框
window.adminToken = function () {
  const el = document.getElementById("token");
  return el ? el.value.trim() : "";
};
