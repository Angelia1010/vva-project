// 检索结果面板：播放器（叠目标框、放大）+ 结果列表。存储页和值守台共用。
//   const panel = SearchPanel.mount(el, { onClose, openLink, wide })   wide：播放器在左、列表在右（存储页）
//   panel.show(result)   panel.select(i)   panel.zoom(i, on)   panel.progress(text)   panel.clear()
(function () {
  const esc = (s) => window.vva.esc(s);
  const css = `
  .sp { display: flex; flex-direction: column; gap: 10px; min-height: 0; height: 100%; }
  .sp-head { display: flex; align-items: center; gap: 8px; min-width: 0; }
  .sp-head .q { font-size: 14px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .sp-head .scope { font-size: 12px; color: var(--ink-2); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; }
  .sp-head .spacer { flex: 1; }
  .sp-head button.link { text-decoration: none; white-space: nowrap; } .sp-head button.link:hover, .sp-head a.link:hover { text-decoration: underline; }
  .sp-head a.link { text-decoration: none; white-space: nowrap; }
  .sp-screen { position: relative; aspect-ratio: 16 / 9; background: #070D17; border-radius: 6px; overflow: hidden; border: 1px solid var(--line); flex: 0 0 auto; }
  .sp-stage { position: absolute; inset: 0; transform-origin: 0 0; transition: transform .25s ease; }
  @media (prefers-reduced-motion: reduce) { .sp-stage { transition: none; } }
  .sp-stage video, .sp-stage img { display: block; width: 100%; height: 100%; object-fit: contain; background: #000; }
  .sp-stage .sp-box { position: absolute; border: 2px solid var(--lamp-alert); border-radius: 2px; pointer-events: none; }
  .sp-screen .sp-hint { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; text-align: center;
                        padding: 0 20px; color: var(--ink-2); font-size: 13px; background: rgba(7, 13, 23, .7); }
  .sp-screen .sp-tag { position: absolute; left: 8px; top: 8px; padding: 2px 8px; border-radius: 4px; background: rgba(8, 14, 24, .78);
                       font-size: 12px; color: #DCE6F1; pointer-events: none; }
  .sp-ctl { display: flex; align-items: center; gap: 14px; font-size: 13px; flex-wrap: wrap; }
  .sp-ctl button.link, .sp-ctl a.link { text-decoration: none; } .sp-ctl button.link:hover, .sp-ctl a.link:hover { text-decoration: underline; }
  .sp-ctl .spacer { flex: 1; }
  .sp-list { flex: 1 1 auto; min-height: 0; overflow-y: auto; display: flex; flex-direction: column; gap: 4px; }
  .sp.wide { display: grid; grid-template-columns: minmax(0, 1fr) 320px; grid-template-rows: auto auto auto minmax(0, 1fr); column-gap: 16px; }
  .sp.wide .sp-head { grid-column: 1 / -1; }
  .sp.wide .sp-screen, .sp.wide .sp-ctl, .sp.wide .sp-note { grid-column: 1; }
  .sp.wide .sp-screen { max-height: 100%; align-self: start; }
  .sp.wide .sp-list { grid-column: 2; grid-row: 2 / 5; }
  .sp-item { flex: 0 0 auto; display: grid; grid-template-columns: 76px minmax(0, 1fr); gap: 10px; align-items: center; padding: 6px; border-radius: 6px;
             border: 1px solid transparent; background: transparent; color: inherit; text-align: left; min-height: 0; cursor: pointer; }
  .sp-item:hover { background: rgba(255, 255, 255, .03); border-color: transparent; }
  .sp-item[aria-current="true"] { background: #152741; border-color: #2F4B72; }
  .sp-item .th { position: relative; width: 76px; height: 43px; border-radius: 4px; overflow: hidden; background: #0C1522; }
  .sp-item .th img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .sp-item .th b { position: absolute; left: 3px; top: 2px; font: 600 11px var(--digits); color: #fff; text-shadow: 0 0 3px #000; }
  .sp-item .l1 { font-size: 13px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .sp-item .l1 .tag { margin-left: 6px; font-weight: 400; }
  .sp-item .l2 { font-size: 12px; color: var(--ink-2); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .sp-empty { padding: 24px 8px; text-align: center; color: var(--ink-2); font-size: 13px; line-height: 1.8; }
  .sp-note { font-size: 12px; color: #F5D08A; }
  `;
  if (!document.getElementById("sp-style")) {
    const st = document.createElement("style"); st.id = "sp-style"; st.textContent = css; document.head.appendChild(st);
  }
  const clipUrl = (h) => `/api/playback/clip?cam=${encodeURIComponent(h.camera)}&start=${Math.floor(h.clip_start)}&end=${Math.ceil(h.clip_end)}`;

  window.SearchPanel = {
    mount(el, opts) {
      opts = opts || {};
      el.innerHTML = `<div class="sp${opts.wide ? " wide" : ""}">
        <div class="sp-head"><span class="q"></span><span class="scope"></span><span class="spacer"></span>
          <a class="link sp-open" href="#" hidden>在存储页打开</a>${opts.onClose ? '<button type="button" class="link sp-close">关闭</button>' : ""}</div>
        <div class="sp-screen"><div class="sp-stage"><video playsinline controls preload="metadata" hidden></video><img alt="" hidden></div>
          <span class="sp-tag" hidden></span><div class="sp-hint">输入要找的事，如“昨天下午有没有人抽烟”</div></div>
        <div class="sp-ctl"><button type="button" class="link sp-zoom" disabled>放大目标</button><span class="t"></span><span class="spacer"></span>
          <a class="link sp-dl" href="#" hidden>下载片段</a></div>
        <div class="sp-note" hidden></div>
        <div class="sp-list" role="list"></div></div>`;
      const $ = (s) => el.querySelector(s);
      const stage = $(".sp-stage"), video = $("video"), img = $(".sp-stage img"), hint = $(".sp-hint");
      let result = null, cur = -1, zoomed = false;
      if (opts.onClose) $(".sp-close").onclick = () => opts.onClose();

      function setHint(text) { hint.textContent = text; hint.hidden = !text; }
      function drawBox(h) {
        stage.querySelectorAll(".sp-box").forEach(b => b.remove());
        if (!h || !h.box) return;
        const b = document.createElement("div"); b.className = "sp-box";
        const [x1, y1, x2, y2] = h.box;
        b.style.cssText = `left:${x1 * 100}%;top:${y1 * 100}%;width:${(x2 - x1) * 100}%;height:${(y2 - y1) * 100}%`;
        stage.appendChild(b);
      }
      function applyZoom() {
        const h = result && result.hits[cur];
        if (!zoomed || !h || !h.box) { stage.style.transform = ""; $(".sp-zoom").textContent = "放大目标"; return; }
        const [x1, y1, x2, y2] = h.box, w = Math.max(.08, x2 - x1), hh = Math.max(.08, y2 - y1);
        const s = Math.min(4, Math.max(1.5, 0.8 / Math.max(w, hh)));
        const cx = (x1 + x2) / 2, cy = (y1 + y2) / 2;
        const tx = Math.min(0, Math.max(1 - s, 0.5 - cx * s)), ty = Math.min(0, Math.max(1 - s, 0.5 - cy * s));
        stage.style.transform = `translate(${tx * 100}%, ${ty * 100}%) scale(${s})`;
        $(".sp-zoom").textContent = "恢复原大小";
      }
      function select(i) {
        if (!result || !result.hits[i]) return;
        cur = i; zoomed = false; applyZoom();
        el.querySelectorAll(".sp-item").forEach((x, k) => x.setAttribute("aria-current", String(k === i)));
        const it = el.querySelectorAll(".sp-item")[i]; if (it) it.scrollIntoView({ block: "nearest" });
        const h = result.hits[i];
        $(".sp-tag").hidden = false; $(".sp-tag").textContent = `${i + 1} · ${h.camera_label} · ${h.time}`;
        $(".sp-zoom").disabled = !h.box;
        $(".t").textContent = h.confidence && h.confidence !== "高" ? `把握：${h.confidence}` : "";
        drawBox(h);
        video.pause();
        if (result.recording) {
          setHint("正在准备录像片段…");
          img.hidden = !h.thumb; if (h.thumb) img.src = h.thumb;
          video.hidden = true;
          video.onloadeddata = () => { setHint(""); img.hidden = true; video.hidden = false; video.currentTime = Math.max(0, h.start - h.clip_start); video.play().catch(() => {}); };
          video.onerror = () => { setHint(h.thumb ? "" : "这段时间没有录像"); video.hidden = true; img.hidden = !h.thumb;
                                  $(".sp-dl").hidden = true; if (h.thumb) $(".t").textContent = "没有录像，显示当时的画面"; };
          video.src = clipUrl(h);
          $(".sp-dl").href = clipUrl(h); $(".sp-dl").hidden = false;
        } else {
          video.hidden = true; img.hidden = !h.thumb; if (h.thumb) img.src = h.thumb;
          setHint(h.thumb ? "" : "没有开启录像，只能显示时间和依据"); $(".sp-dl").hidden = true;
        }
      }
      $(".sp-zoom").onclick = () => { zoomed = !zoomed; applyZoom(); };

      return {
        progress(text) {
          $(".q").textContent = "检索中"; $(".scope").textContent = "";
          setHint(text || "正在检索…"); video.hidden = true; img.hidden = true; $(".sp-tag").hidden = true;
          $(".sp-list").innerHTML = ""; $(".sp-note").hidden = true; $(".sp-dl").hidden = true; $(".sp-zoom").disabled = true;
        },
        show(r) {
          result = r; cur = -1;
          $(".q").textContent = r.look_for || r.question || "";
          $(".scope").textContent = `${(r.scope && r.scope.text) || ""} · ${r.hits.length} 处`;
          const open = $(".sp-open");
          open.hidden = !opts.openLink; if (opts.openLink) open.href = "/recording?search=" + encodeURIComponent(r.search_id);
          $(".sp-note").hidden = !r.note; $(".sp-note").textContent = r.note || "";
          const list = $(".sp-list"); list.innerHTML = "";
          if (!r.hits.length) {
            list.innerHTML = `<div class="sp-empty">${esc(r.summary)}<br>可以换个说法，或扩大时间范围再试。</div>`;
            setHint("没有找到"); video.hidden = true; img.hidden = true; $(".sp-tag").hidden = true; return;
          }
          r.hits.forEach((h, i) => {
            const b = document.createElement("button"); b.type = "button"; b.className = "sp-item"; b.setAttribute("role", "listitem");
            b.innerHTML = `<span class="th">${h.thumb ? `<img src="${esc(h.thumb)}" alt="" loading="lazy">` : ""}<b>${i + 1}</b></span>
              <span style="min-width:0"><span class="l1" style="display:block">${esc(h.time.slice(6))} · ${esc(h.camera_label)}${h.alert_id ? '<span class="tag urgent">告警</span>' : ""}</span>
              <span class="l2" style="display:block" title="${esc(h.evidence)}">${esc(h.evidence)}</span></span>`;
            b.onclick = () => select(i);
            list.appendChild(b);
          });
          select(0);
        },
        select, zoom(i, on) { if (i != null && i !== cur) select(i); zoomed = on !== false; applyZoom(); },
        clear() { result = null; video.pause(); video.removeAttribute("src"); }, get result() { return result; },
      };
    },
  };
})();
