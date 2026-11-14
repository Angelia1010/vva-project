// 添加 / 编辑职责的右侧面板（值守台、职责页共用）。
// 用法：DutyEditor.open(duty 或 null, { onSaved }) ；DutyEditor.openTemplates({ onSaved })
(function () {
  const esc = (s) => vva.esc(s);
  const KINDS = [["watch", "盯守"], ["inspect", "巡检"], ["report", "汇报"]];
  const LEVELS = [["normal", "一般"], ["important", "重要"], ["urgent", "紧急"]];
  const WEEK = [["", "每天"], ["1,2,3,4,5", "工作日"], ["6,7", "周末"]];
  let state = null, onSaved = null, meta = { targets: [], objects: [] };

  function shell() {
    if (document.getElementById("duty-drawer")) return;
    const mask = document.createElement("div"); mask.className = "drawer-mask"; mask.id = "duty-mask";
    mask.onclick = close;
    const d = document.createElement("aside");
    d.className = "drawer"; d.id = "duty-drawer"; d.setAttribute("role", "dialog"); d.setAttribute("aria-modal", "true");
    d.setAttribute("aria-labelledby", "dd-title");
    d.innerHTML = `
      <header><h2 id="dd-title">添加职责</h2><span class="muted" id="dd-station"></span><span style="flex:1"></span>
        <button type="button" class="ghost" id="dd-close" aria-label="关闭"><svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="m3 3 10 10M13 3 3 13"/></svg></button></header>
      <div class="scroll" id="dd-body"></div>
      <footer id="dd-foot"></footer>`;
    document.body.append(mask, d);
    document.getElementById("dd-close").onclick = close;
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && d.classList.contains("open")) close(); });
  }
  function show(title) {
    shell();
    document.getElementById("dd-title").textContent = title;
    document.getElementById("duty-mask").classList.add("open");
    const d = document.getElementById("duty-drawer"); d.classList.add("open");
    setTimeout(() => { const f = d.querySelector("input, textarea, button.primary"); if (f) f.focus(); }, 50);
  }
  function close() {
    const d = document.getElementById("duty-drawer");
    if (!d) return;
    d.classList.remove("open"); document.getElementById("duty-mask").classList.remove("open");
  }
  async function loadMeta() {
    try {
      const r = await vva.api("GET", "/api/duties");
      meta = { targets: r.targets || [], objects: r.objects || [] };
    } catch (e) { meta = { targets: [{ id: "web", label: "值守台" }], objects: [] }; }
    try { document.getElementById("dd-station").textContent = (await vva.api("GET", "/api/station")).name; } catch (e) {}
  }

  // ---------------------------------------------------------------- 编辑表单
  const blank = () => ({ name: "", kind: "watch", condition: "", hours: "", weekdays: [], times: [], level: "normal",
    targets: meta.targets.some(t => t.id === "default") ? ["default", "web"] : ["web"], voice_text: "", verify: true,
    min_duration_s: 0, cooldown_s: 600, objects: [], rule_ref: "", mode: "live", enabled: true });

  function seg(name, options, value) {
    return `<div class="seg" role="group" aria-label="${name}">` + options.map(([v, t]) =>
      `<button type="button" data-seg="${name}" data-v="${v}" aria-pressed="${String(v) === String(value)}">${t}</button>`).join("") + `</div>`;
  }
  function chipList(name, options, selected) {
    return `<div class="chips">` + options.map(o =>
      `<button type="button" class="chip" data-chip="${name}" data-v="${esc(o.id)}" aria-pressed="${selected.includes(o.id)}">${esc(o.label)}</button>`).join("") + `</div>`;
  }

  function render() {
    const s = state.duty, isNew = !s.id;
    const body = document.getElementById("dd-body");
    const weekVal = (s.weekdays || []).join(",");
    const targets = meta.targets.slice();
    for (const t of s.targets || []) if (!targets.some(o => o.id === t)) targets.push({ id: t, label: t });
    body.innerHTML = `
      ${isNew ? `<div>
        <label class="lbl" for="dd-say">用一句话描述 <span class="muted">· 例如“仓库里有人吸烟就发钉钉安全群，紧急”</span></label>
        <div style="display:flex;gap:8px"><input id="dd-say" type="text" value="${esc(state.say || "")}" placeholder="发现什么、通知谁、什么时候生效">
          <button type="button" class="primary" id="dd-parse" style="flex:0 0 auto">生成</button></div>
        <div class="chips" style="margin-top:10px;align-items:center"><span class="muted" style="font-size:12px">或</span>
          <button type="button" class="chip" id="dd-tpl">从行业模板选择</button></div>
      </div>
      <div id="dd-questions"></div>
      <div class="divider"></div>` : ""}
      <div class="row2">
        <div><label class="lbl" for="dd-name">名称</label><input id="dd-name" type="text" value="${esc(s.name)}" placeholder="如：禁止吸烟"></div>
        <div><span class="lbl">类型</span>${seg("kind", KINDS, s.kind)}</div>
      </div>
      <div>
        <label class="lbl" for="dd-cond">${s.kind === "inspect" ? "检查什么 <span class='muted'>· 写上判断标准，如“超过 0.6MPa 为异常”</span>"
          : s.kind === "report" ? "报告内容" : "看什么 <span class='muted'>· 写成能从画面直接看出来的样子</span>"}</label>
        <textarea id="dd-cond" placeholder="如：画面中有人吸烟，或手里夹着点燃的香烟">${esc(s.condition)}</textarea>
        <div id="dd-hints"></div>
      </div>
      ${meta.objects.length && s.kind !== "report" ? `<div><span class="lbl">引用技能目标 <span class="muted">· 判断时带上它们的参考图</span></span>
        ${chipList("objects", meta.objects.map(o => ({ id: o, label: o })), s.objects || [])}</div>` : ""}
      ${s.kind === "watch" ? `<div class="row2">
        <div><label class="lbl" for="dd-hours">生效时间 <span class="muted">· 留空为全天</span></label><input id="dd-hours" type="text" value="${esc(s.hours)}" placeholder="08:00-18:00"></div>
        <div><span class="lbl">哪几天</span>${seg("week", WEEK, WEEK.some(w => w[0] === weekVal) ? weekVal : "")}</div>
      </div>` : `<div class="row2">
        <div><label class="lbl" for="dd-times">执行时间 <span class="muted">· 多个用逗号分开</span></label><input id="dd-times" type="text" value="${esc((s.times || []).join(", "))}" placeholder="09:00, 15:00"></div>
        <div><span class="lbl">哪几天</span>${seg("week", WEEK, WEEK.some(w => w[0] === weekVal) ? weekVal : "")}</div>
      </div>`}
      ${s.kind !== "report" ? `<div><span class="lbl">级别</span>${seg("level", LEVELS, s.level)}</div>` : ""}
      <div><span class="lbl">${s.kind === "report" ? "发给谁" : "发现后通知谁"}</span>${chipList("targets", targets, s.targets || [])}
        ${targets.some(t => t.id === "default" || t.id.includes(":")) ? `<p class="muted" style="margin:6px 0 0;font-size:12px">要推到钉钉 / 飞书，记得选上对应的群；只选“值守台”时只在网页上提醒。</p>`
          : `<div class="hintbox" style="margin-top:8px">还没有可选的钉钉 / 飞书群，所以现在只能在值守台提醒。先在“设置 → 钉钉”接入，把机器人拉进群并 @它 说一句话，回到这里刷新就能选到这个群。</div>`}</div>
      ${s.kind === "watch" ? `<div class="box">
        <label class="switch"><input type="checkbox" id="dd-verify" ${s.verify ? "checked" : ""}>放大复核后再通知</label>
        <label class="switch"><input type="checkbox" id="dd-trial" ${s.mode === "trial" ? "checked" : ""}>试运行（只记录不通知）</label>
        <div class="inline">同一件事<input id="dd-cool" type="number" min="1" value="${Math.round((s.cooldown_s || 600) / 60)}" aria-label="冷却分钟">分钟内只报一次</div>
        <div class="inline">持续<input id="dd-dur" type="number" min="0" value="${Math.round(s.min_duration_s || 0)}" aria-label="持续秒数">秒以上才算</div>
      </div>
      <div><label class="lbl" for="dd-voice">现场播报 <span class="muted">· 通知对象勾选“现场播报”时说这句，留空则播报提醒内容</span></label>
        <input id="dd-voice" type="text" value="${esc(s.voice_text)}" placeholder="如：仓库内禁止吸烟"></div>` : ""}
      ${s.kind !== "report" ? `<div><label class="lbl" for="dd-ref">规章依据 <span class="muted">· 可选，写进告警和通知</span></label>
        <input id="dd-ref" type="text" value="${esc(s.rule_ref)}" placeholder="如：《仓库安全管理规定》第 5 条"></div>
      <div><span class="lbl">试一下 <span class="muted">· 用当前画面检查一次，不会发通知</span></span>
        <div id="dd-test"><button type="button" id="dd-run-test">用当前画面检查</button></div></div>` : ""}
    `;
    const foot = document.getElementById("dd-foot");
    foot.innerHTML = `${!isNew ? `<button type="button" class="danger" id="dd-del">删除</button>` : ""}
      <span class="toast" id="dd-msg" role="status"></span><span style="flex:1"></span>
      <button type="button" id="dd-cancel">取消</button>
      <button type="button" class="primary" id="dd-save" style="min-width:140px">${isNew ? "保存并开启" : "保存修改"}</button>`;
    bind();
    renderHints(state.hints || []);
    renderQuestions(state.questions || []);
  }

  function renderHints(hints) {
    const el = document.getElementById("dd-hints");
    if (el) el.innerHTML = hints.map(h => `<div class="hintbox" style="margin-top:8px">${esc(h)}</div>`).join("");
  }
  function renderQuestions(qs) {
    const el = document.getElementById("dd-questions");
    if (el) el.innerHTML = qs.length ? `<div class="hintbox info">${qs.map(esc).join("<br>")}</div>` : "";
  }
  function msg(text, cls) { const m = document.getElementById("dd-msg"); if (m) { m.textContent = text; m.className = "toast " + (cls || ""); } }

  function collect() {
    const s = state.duty, val = (id) => { const e = document.getElementById(id); return e ? e.value.trim() : undefined; };
    const out = { ...s, name: val("dd-name"), condition: val("dd-cond") };
    if (document.getElementById("dd-hours")) out.hours = val("dd-hours");
    if (document.getElementById("dd-times")) out.times = val("dd-times").split(/[\s,，、]+/).filter(Boolean);
    if (document.getElementById("dd-voice")) out.voice_text = val("dd-voice");
    if (document.getElementById("dd-ref")) out.rule_ref = val("dd-ref");
    const v = document.getElementById("dd-verify"); if (v) out.verify = v.checked;
    const t = document.getElementById("dd-trial"); if (t) out.mode = t.checked ? "trial" : "live";
    const c = document.getElementById("dd-cool"); if (c) out.cooldown_s = Math.max(1, Number(c.value || 10)) * 60;
    const d = document.getElementById("dd-dur"); if (d) out.min_duration_s = Math.max(0, Number(d.value || 0));
    return out;
  }

  function bind() {
    const body = document.getElementById("dd-body");
    body.querySelectorAll("[data-seg]").forEach(b => b.onclick = () => {
      state.duty = collect();
      const k = b.dataset.seg, v = b.dataset.v;
      if (k === "kind") state.duty.kind = v;
      if (k === "level") state.duty.level = v;
      if (k === "week") state.duty.weekdays = v ? v.split(",").map(Number) : [];
      render();
    });
    body.querySelectorAll("[data-chip]").forEach(b => b.onclick = () => {
      const k = b.dataset.chip, v = b.dataset.v, list = state.duty[k] || (state.duty[k] = []);
      const i = list.indexOf(v); if (i >= 0) list.splice(i, 1); else list.push(v);
      b.setAttribute("aria-pressed", String(i < 0));
    });
    const parse = document.getElementById("dd-parse");
    if (parse) {
      const run = async () => {
        const text = document.getElementById("dd-say").value.trim();
        if (!text) return;
        parse.disabled = true; parse.textContent = "生成中…";
        try {
          const r = await vva.api("POST", "/api/duties/parse", { text });
          state = { ...state, say: text, duty: { ...state.duty, ...r.draft }, hints: r.hints, questions: r.questions };
          render(); msg("已生成，请检查后保存", "ok");
        } catch (e) { parse.disabled = false; parse.textContent = "生成"; msg("生成失败：" + e.message, "err"); }
      };
      parse.onclick = run;
      document.getElementById("dd-say").onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); run(); } };
    }
    const tpl = document.getElementById("dd-tpl"); if (tpl) tpl.onclick = () => openTemplates({ onSaved });
    const test = document.getElementById("dd-run-test"); if (test) test.onclick = runTest;
    document.getElementById("dd-cancel").onclick = close;
    document.getElementById("dd-save").onclick = save;
    const del = document.getElementById("dd-del"); if (del) del.onclick = remove;
  }

  async function runTest() {
    const box = document.getElementById("dd-test");
    const duty = collect();
    if (!duty.condition) { msg("请先填写要看什么", "err"); return; }
    box.innerHTML = `<div class="testbox"><div class="res"><span class="muted">正在检查当前画面…</span></div></div>`;
    try {
      const r = await vva.api("POST", "/api/duties/test", { duty });
      const boxStyle = r.box ? `left:${r.box[0] * 100}%;top:${r.box[1] * 100}%;width:${(r.box[2] - r.box[0]) * 100}%;height:${(r.box[3] - r.box[1]) * 100}%` : "";
      box.innerHTML = `<div class="testbox">
        <div class="shot">${r.image ? `<img src="${r.image}" alt="检查时的画面">` : ""}${boxStyle ? `<div class="box" style="${boxStyle}"></div>` : ""}</div>
        <div class="res"><span class="${r.match ? "hit" : "miss"}">${r.match ? (duty.kind === "inspect" ? "异常" : "命中") : (duty.kind === "inspect" ? "正常" : "当前未命中")}</span>
          <span>${esc(r.evidence || "")}</span>
          <span><button type="button" class="ghost" id="dd-retest" style="min-height:28px;padding:2px 10px">重新检查</button></span></div></div>`;
      document.getElementById("dd-retest").onclick = runTest;
      renderHints(r.hints || []);
    } catch (e) { box.innerHTML = `<div class="hintbox">检查失败：${esc(e.message)}</div>`; }
  }

  async function save() {
    const duty = collect(), btn = document.getElementById("dd-save");
    if (!duty.condition) { msg("请填写要看什么", "err"); return; }
    btn.disabled = true;
    try {
      const saved = duty.id ? await vva.api("PUT", "/api/duties/" + duty.id, duty) : await vva.api("POST", "/api/duties", duty);
      close(); onSaved && onSaved(saved);
    } catch (e) { msg("没有保存：" + e.message, "err"); btn.disabled = false; }
  }
  async function remove() {
    if (!confirm(`删除“${state.duty.name}”？删除后不再值守这项，已有的告警记录会保留。`)) return;
    try { await vva.api("DELETE", "/api/duties/" + state.duty.id); close(); onSaved && onSaved(null); }
    catch (e) { msg("删除失败：" + e.message, "err"); }
  }

  // ---------------------------------------------------------------- 行业模板
  async function openTemplates(opts) {
    onSaved = (opts && opts.onSaved) || onSaved;
    show("从行业模板添加");
    await loadMeta();
    const body = document.getElementById("dd-body"), foot = document.getElementById("dd-foot");
    body.innerHTML = `<p class="muted" style="margin:0">加载中…</p>`;
    let list = [];
    try { list = (await vva.api("GET", "/api/duties/templates")).templates; } catch (e) { body.innerHTML = `<div class="hintbox">${esc(e.message)}</div>`; return; }
    let cur = list[0].id;
    const draw = () => {
      const tpl = list.find(t => t.id === cur);
      body.innerHTML = `<div><span class="lbl">行业</span>${seg("tpl", list.map(t => [t.id, t.name]), cur)}</div>
        <div><span class="lbl">勾选要添加的职责 <span class="muted">· 已有同名职责的会跳过</span></span><div class="tpl">
        ${tpl.items.map(i => `<label><input type="checkbox" value="${esc(i.name)}" checked>
          <span><span class="t">${esc(i.name)}</span> <span class="tag">${{ watch: "盯守", inspect: "巡检", report: "汇报" }[i.kind || "watch"]}</span>
          ${i.level === "urgent" ? '<span class="tag urgent">紧急</span>' : ""}
          <div class="d">${esc(i.condition)}${i.hours ? "；" + esc(i.hours) : ""}${i.times ? "；每天 " + esc(i.times.join("、")) : ""}</div>
          ${i.hint ? `<div class="d" style="color:#F5D08A">${esc(i.hint)}</div>` : ""}</span></label>`).join("")}
        </div></div>
        <div><span class="lbl">统一通知到</span>${chipList("tpl-targets", meta.targets, meta.targets.some(t => t.id === "default") ? ["default", "web"] : ["web"])}</div>
        <label class="switch"><input type="checkbox" id="tpl-trial" checked>盯守类先试运行（只记录不通知，确认准确后再正式开启）</label>`;
      body.querySelectorAll("[data-seg]").forEach(b => b.onclick = () => { cur = b.dataset.v; draw(); });
      body.querySelectorAll("[data-chip]").forEach(b => b.onclick = () => b.setAttribute("aria-pressed", String(b.getAttribute("aria-pressed") !== "true")));
    };
    draw();
    foot.innerHTML = `<span class="toast" id="dd-msg" role="status"></span><span style="flex:1"></span>
      <button type="button" id="dd-cancel">取消</button><button type="button" class="primary" id="dd-save" style="min-width:140px">添加所选职责</button>`;
    document.getElementById("dd-cancel").onclick = close;
    document.getElementById("dd-save").onclick = async () => {
      const names = [...body.querySelectorAll(".tpl input:checked")].map(i => i.value);
      const targets = [...body.querySelectorAll('[data-chip="tpl-targets"][aria-pressed="true"]')].map(b => b.dataset.v);
      if (!names.length) { msg("请至少勾选一项", "err"); return; }
      try {
        const r = await vva.api("POST", "/api/duties/templates/" + cur, { names, targets, mode: document.getElementById("tpl-trial").checked ? "trial" : "live" });
        close(); onSaved && onSaved(r.created);
      } catch (e) { msg("没有添加：" + e.message, "err"); }
    };
  }

  async function open(duty, opts) {
    onSaved = (opts && opts.onSaved) || null;
    show(duty ? "编辑职责" : "添加职责");
    await loadMeta();
    state = { duty: duty ? JSON.parse(JSON.stringify(duty)) : blank(), hints: [], questions: [], say: "" };
    if (duty && duty.targets) state.duty.targets = duty.targets.slice();
    render();
  }

  window.DutyEditor = { open, openTemplates, close };
})();

// 职责卡（值守台左栏、职责页共用）。actions：{ test, toggle, edit, alerts, remove } 回调

// 删除职责（带确认），成功返回 true
window.removeDuty = async function (d) {
  const tip = d.temporary ? `取消临时任务“${d.name}”？` : `删除职责“${d.name}”？删除后不再值守这一项，已有的告警记录会保留。`;
  if (!confirm(tip)) return false;
  await vva.api("DELETE", "/api/duties/" + d.id);
  return true;
};
window.dutyCard = function (d, actions, opts) {
  const esc = vva.esc, o = opts || {};
  const el = document.createElement("details");
  const hot = d.today_count > 0 && d.kind === "watch" && d.state !== "trial";
  el.className = "dcard" + (d.temporary ? " temp" : "") + (d.state === "error" ? " error" : "") + (hot ? " hot" : "");
  if (o.open) el.open = true;
  const dot = d.temporary ? "temp" : (d.state === "running" ? "running" : d.state);
  const count = d.kind === "inspect" ? "" : (d.kind === "report" ? "" : String(d.today_count));
  el.innerHTML = `<summary>
      <div class="top"><span class="dot ${dot}" aria-hidden="true"></span><span class="name">${esc(d.name)}</span>
        ${d.temporary ? "" : `<span class="tag">${esc(d.kind_label)}</span>`}
        ${d.level === "urgent" ? '<span class="tag urgent">紧急</span>' : d.level === "important" ? '<span class="tag important">重要</span>' : ""}
        ${count !== "" ? `<span class="count ${hot ? "hot" : ""}" title="今天的告警次数">${count}</span>` : ""}</div>
      <div class="meta"><span>${d.temporary ? "到 " + esc(d.expires_text.slice(-8, -3) || d.expires_text) + " 为止" : esc(d.schedule)}</span><span aria-hidden="true">→</span><span class="to">${esc(d.target_text)}</span></div>
      <div class="state ${d.state}">${esc(d.state_text)}${d.temporary && d.created_by ? " · " + esc(d.created_by) + "下达" : ""}</div>
    </summary>
    <div class="acts"></div>`;
  const acts = el.querySelector(".acts");
  const add = (label, fn, cls) => { const b = document.createElement("button"); b.type = "button"; b.textContent = label; if (cls) b.className = cls; b.onclick = (e) => { e.preventDefault(); fn(d, b); }; acts.appendChild(b); };
  if (actions.test && d.kind !== "report") add("试一下", actions.test);
  if (actions.toggle) add(d.enabled ? "暂停" : "恢复", actions.toggle);
  if (actions.edit) add(d.temporary ? "转为长期职责" : "编辑", actions.edit);
  if (actions.alerts) add("告警记录", actions.alerts, "ghost");
  if (actions.remove) add(d.temporary ? "取消任务" : "删除", actions.remove, "danger");
  return el;
};
