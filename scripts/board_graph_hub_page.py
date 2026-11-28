#!/usr/bin/env python3
"""HTML/CSS/JS template for board-graph Hub including the 6-card Tool Self-Heal strip."""
from __future__ import annotations

HUB_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>board-graph Hub</title>
<style>
  :root {
    color-scheme: dark;
    --bg: #0c0c0e; --surface: #141418; --surface-2: #1b1b21; --surface-hover: #24242c;
    --ink-1: #f5f2eb; --ink-2: #c7c2b8; --ink-3: #8b867d;
    --hair: rgba(255,248,235,0.10); --hair-strong: rgba(255,248,235,0.20);
    --green: #22c55e; --green-soft: #4ade80; --green-bg: rgba(34,197,94,0.14);
    --copper: #e89149; --copper-soft: #f59e0b; --copper-bg: rgba(232,145,73,0.15);
    --amber: #facc15; --amber-bg: rgba(250,204,21,0.15);
    --cyan: #38bdf8; --cyan-bg: rgba(56,189,248,0.13);
    --red: #fb7185; --red-strong: #f43f5e; --red-bg: rgba(244,63,94,0.16);
    --font: "Segoe UI Variable Text", "Segoe UI", system-ui, -apple-system, sans-serif;
    --mono: ui-monospace, "Cascadia Mono", Menlo, Consolas, monospace;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--ink-1); font: 400 13px/1.45 var(--font); -webkit-font-smoothing: antialiased; }
  a { color: var(--copper-soft); text-decoration: none; }
  a:hover { text-decoration: underline; }
  button { font: inherit; color: inherit; cursor: pointer; background: none; border: none; }
  code, pre { font-family: var(--mono); }
  .header { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 10px;
            padding: 10px 16px; border-bottom: 1px solid var(--hair); background: var(--surface); position: sticky; top: 0; z-index: 20; }
  .header h1 { margin: 0; font-size: 16.5px; font-weight: 650; }
  .sub { font-size: 11.5px; color: var(--ink-3); }
  .hdr-right { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
  select.btn { background: var(--surface-2); color: var(--ink-1); cursor: pointer; }
  .pill { display: inline-flex; align-items: center; gap: 5px; padding: 2px 8px; border-radius: 999px;
          border: 1px solid var(--hair); background: var(--surface-2); font-size: 11.5px; color: var(--ink-2); }
  button.pill:hover { border-color: var(--hair-strong); background: var(--surface-hover); color: var(--ink-1); }
  .pill.warn { border-color: rgba(250,204,21,0.52); color: var(--amber); background: var(--amber-bg); }
  .pill.ok { border-color: rgba(34,197,94,0.48); color: var(--green-soft); background: var(--green-bg); }
  .pill.heal { border-color: rgba(56,189,248,0.48); color: var(--cyan); background: var(--cyan-bg); }
  .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--ink-3); }
  .dot.live { background: var(--green-soft); box-shadow: 0 0 7px var(--green); }
  .wrap { padding: 10px 16px 24px; display: flex; flex-direction: column; gap: 10px; }
  .run-row { background: var(--surface); border: 1px solid var(--hair); border-left-width: 4px; border-radius: 8px;
             padding: 9px 12px; display: flex; flex-direction: column; gap: 7px; }
  .run-row.heal-row { border-left-color: var(--cyan); background: linear-gradient(180deg, rgba(56,189,248,0.06), var(--surface)); }
  .run-top { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px; }
  .run-title { display: flex; flex-wrap: wrap; align-items: center; gap: 7px; }
  .run-title h2 { margin: 0; font-size: 15px; font-weight: 650; }
  .model-pill { padding: 1px 8px; border-radius: 999px; font-size: 11px; font-weight: 600; font-family: var(--mono); border: 1px solid; }
  .timer-pill { padding: 1px 8px; border-radius: 999px; font-size: 11px; font-weight: 600; font-family: var(--mono);
                border: 1px solid rgba(232,145,73,0.52); color: var(--copper-soft); background: var(--copper-bg); }
  .timer-pill.done { border-color: rgba(34,197,94,0.48); color: var(--green-soft); background: var(--green-bg); }
  .badge { padding: 1px 7px; border-radius: 999px; font-size: 11px; font-weight: 600; border: 1px solid var(--hair); color: var(--ink-2); }
  .badge.owner { color: var(--amber); border-color: rgba(250,204,21,0.58); background: var(--amber-bg); }
  .badge.lead { color: var(--red); border-color: rgba(244,63,94,0.58); background: var(--red-bg); }
  .badge.off { color: var(--red); border-color: rgba(244,63,94,0.45); background: var(--red-bg); }
  .btn { padding: 4px 9px; border: 1px solid var(--hair); border-radius: 6px; background: var(--surface-2); color: var(--ink-2); font-size: 11.5px; }
  .btn:hover { background: var(--surface-hover); color: var(--ink-1); border-color: var(--hair-strong); }
  .btn.pcb-btn { border-color: rgba(232,145,73,0.48); color: var(--copper); }
  .btn.pcb-btn:hover { background: var(--copper-bg); color: var(--ink-1); }
  .prog-track { height: 3px; width: 100%; border-radius: 999px; background: #101014; overflow: hidden; }
  .prog-fill { height: 100%; background: linear-gradient(90deg, var(--green), var(--copper)); transition: width .2s ease; }
  .prog-fill.heal-fill { background: linear-gradient(90deg, var(--cyan), var(--green)); }
  .strip { display: flex; flex-wrap: wrap; align-items: center; gap: 5px; }
  .heal-subrow { padding-top: 6px; border-top: 1px dashed var(--hair); display: flex; flex-direction: column; gap: 5px; }
  .heal-subhead { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px; font-size: 11.5px; color: var(--ink-2); }
  .chip { padding: 3px 8px; border-radius: 6px; border: 1px solid var(--hair); background: var(--surface-2);
          font-size: 11.5px; line-height: 1.3; white-space: nowrap; display: inline-flex; align-items: center; gap: 5px; color: var(--ink-1); }
  .chip:hover { border-color: var(--hair-strong); background: var(--surface-hover); }
  .chip.passed { border-color: rgba(34,197,94,0.52); background: var(--green-bg); color: var(--green-soft); }
  .chip.current { border-color: rgba(232,145,73,0.88); background: var(--copper-bg); color: #ffd7b3; font-weight: 600; }
  .chip.owner { border-color: rgba(250,204,21,0.85); background: var(--amber-bg); color: var(--amber); font-weight: 600; }
  .chip.lead { border-color: rgba(244,63,94,0.85); background: var(--red-bg); color: var(--red); font-weight: 600; }
  .chip.ahead { opacity: 0.65; color: var(--ink-2); }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.35; } }
  .chip-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--copper-soft); animation: pulse 1.4s infinite; }
  .later-note { font-size: 11px; color: var(--ink-3); font-family: var(--mono); padding: 0 4px; }
  .thumbs { display: flex; flex-wrap: nowrap; overflow-x: auto; gap: 10px; padding-top: 4px; }
  .thumb-btn { flex: none; display: flex; flex-direction: column; gap: 4px; padding: 6px; border-radius: 7px;
               border: 1px solid var(--hair); background: #0d0d11; }
  .thumb-btn:hover { border-color: var(--copper); }
  .thumb-btn img { height: 210px; width: auto; max-width: 380px; object-fit: contain; border-radius: 5px; background: #070709; display: block; }
  .thumb-btn span { font-size: 11px; color: var(--ink-2); font-family: var(--mono); max-width: 368px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; text-align: left; }
  .err-line { font-size: 11.5px; color: var(--red); background: var(--red-bg); border: 1px solid rgba(244,63,94,0.45); border-radius: 5px; padding: 5px 8px; }
  .drawer-backdrop { position: fixed; inset: 0; background: rgba(0,0,0,0.58); z-index: 30; display: none; }
  .drawer-backdrop.open { display: block; }
  .drawer { position: fixed; top: 0; right: 0; bottom: 0; width: min(680px, 100vw); background: var(--surface);
            border-left: 1px solid var(--hair-strong); z-index: 35; display: flex; flex-direction: column;
            transform: translateX(100%); transition: transform .15s ease-out; }
  .drawer.open { transform: translateX(0); }
  .drawer-head { display: flex; align-items: center; justify-content: space-between; padding: 12px 16px; border-bottom: 1px solid var(--hair); }
  .drawer-head h3 { margin: 0; font-size: 15.5px; }
  .drawer-body { flex: 1; overflow-y: auto; padding: 14px 16px 28px; display: flex; flex-direction: column; gap: 14px; }
  .sec-lbl { font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--ink-3); font-weight: 600; margin-bottom: 5px; }
  pre.box { margin: 0; padding: 9px 11px; border-radius: 6px; border: 1px solid var(--hair); background: #0f0f13;
            white-space: pre-wrap; word-break: break-word; font-size: 12px; color: var(--ink-1); }
  .doc-card { border: 1px solid var(--hair); border-radius: 7px; background: #101014; overflow: hidden; }
  .doc-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 6px 10px;
              border-bottom: 1px solid var(--hair); background: var(--surface-2); font-size: 11.5px; font-family: var(--mono); color: var(--ink-2); }
  .doc-body { padding: 10px 12px; font-size: 12.5px; color: var(--ink-1); overflow-x: auto; line-height: 1.5; }
  .doc-body h1, .doc-body h2, .doc-body h3 { margin: 10px 0 5px; font-size: 14px; color: var(--ink-1); }
  .doc-body h1:first-child, .doc-body h2:first-child { margin-top: 0; }
  .doc-body p { margin: 0 0 8px; }
  .doc-body ul { margin: 0 0 8px; padding-left: 18px; }
  .doc-body li + li { margin-top: 2px; }
  .doc-body code { background: rgba(255,255,255,0.06); padding: 1px 4px; border-radius: 4px; font-size: 11.5px; }
  table.rt { width: 100%; border-collapse: collapse; font-size: 11.5px; }
  table.rt th, table.rt td { padding: 5px 8px; border-bottom: 1px solid var(--hair); text-align: left; vertical-align: top; }
  table.rt th { color: var(--ink-2); font-weight: 600; background: rgba(255,255,255,0.03); }
  .img-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 8px; }
  .img-card { border: 1px solid var(--hair); border-radius: 6px; padding: 5px; background: #0d0d10; display: flex; flex-direction: column; gap: 4px; }
  .img-card img { width: 100%; max-height: 160px; object-fit: contain; border-radius: 4px; }
  .modal { position: fixed; inset: 0; z-index: 45; display: none; align-items: center; justify-content: center; padding: 20px; background: rgba(0,0,0,0.74); }
  .modal.open { display: flex; }
  .modal-card { width: min(1020px, 100%); max-height: 92vh; background: var(--surface); border: 1px solid var(--hair-strong);
                border-radius: 12px; display: flex; flex-direction: column; overflow: hidden; box-shadow: 0 24px 64px rgba(0,0,0,0.75); }
  .modal-head { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 10px; padding: 12px 16px; border-bottom: 1px solid var(--hair); }
  .modal-tabs { display: flex; flex-wrap: wrap; gap: 6px; padding: 8px 16px; border-bottom: 1px solid var(--hair); background: #101014; }
  .modal-tabs .btn.active { border-color: var(--copper); background: var(--copper-bg); color: var(--ink-1); }
  .modal-body { padding: 14px 16px; overflow: auto; display: flex; flex-direction: column; gap: 8px; align-items: center; background: #08080b; }
  .modal-body img { max-width: 100%; max-height: 68vh; border-radius: 8px; border: 1px solid var(--hair); }
</style>
</head>
<body>
<header class="header">
  <div style="display:flex;align-items:baseline;gap:10px;flex-wrap:wrap">
    <h1>board-graph Hub</h1>
    <span class="sub" id="sub">Loading sources...</span>
  </div>
  <div class="hdr-right">
    <select id="scope-select" class="btn" aria-label="Visible target scope">
      <option value="auto">Scope: Target (auto)</option>
      <option value="placement">Through Placement</option>
      <option value="board_checks">Through Board checks</option>
      <option value="verification">Through Verification</option>
      <option value="quote_files">Through Quote files</option>
      <option value="order">Through Order</option>
      <option value="all">Full lifecycle (all)</option>
    </select>
    <span class="pill"><span id="dot" class="dot"></span><span id="live-lbl">Connecting</span></span>
  </div>
</header>
<main class="wrap" id="rows"></main>
<div class="drawer-backdrop" id="backdrop"></div>
<aside class="drawer" id="drawer">
  <div class="drawer-head"><h3 id="d-title">Card</h3><button class="btn" id="d-close">Close</button></div>
  <div class="drawer-body" id="d-body"></div>
</aside>
<div class="modal" id="pcb-modal" aria-hidden="true">
  <div class="modal-card">
    <div class="modal-head">
      <div style="display:flex;align-items:center;gap:10px">
        <strong id="m-title">PCB / Routing Viewer</strong>
        <span class="sub" id="m-meta"></span>
      </div>
      <div style="display:flex;align-items:center;gap:8px">
        <label style="font-size:12px;color:var(--ink-2);display:inline-flex;align-items:center;gap:5px;cursor:pointer">
          <input type="checkbox" id="m-auto" checked> Auto-refresh (3s)
        </label>
        <button class="btn" id="m-refresh">Refresh</button>
        <a class="btn" id="m-raw" href="#" target="_blank" rel="noopener">Open raw</a>
        <button class="btn" id="m-close">Close</button>
      </div>
    </div>
    <div class="modal-tabs" id="m-tabs"></div>
    <div class="modal-body">
      <img id="m-img" src="" alt="PCB preview" style="display:none">
      <div id="m-empty" class="sub" style="padding:28px"></div>
    </div>
  </div>
</div>
<script>
(() => {
  const RUN_ACCENTS = ['#fb923c', '#34d399', '#c084fc', '#facc15', '#fb7185'];
  const READABLE_EXTS = ['.md', '.json', '.csv', '.txt'];
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const $ = (id) => document.getElementById(id);
  let lastSummary = null;
  let scopePref = null;
  let modalSrc = null;
  let modalIdx = 0;
  let modalTimer = null;

  function inlineMd(text) {
    return esc(text).replace(/`([^`]+)`/g, '<code>$1</code>').replace(/\\*\\*([^*]+)\\*\\*/g, '<strong>$1</strong>');
  }

  function renderMd(md) {
    const lines = String(md || '').split(/\\r?\\n/);
    const out = [];
    let para = [], code = null, inList = false, inTable = false;
    const flushPara = () => { if (para.length) { out.push(`<p>${inlineMd(para.join(' '))}</p>`); para = []; } };
    const closeList = () => { if (inList) { out.push('</ul>'); inList = false; } };
    const closeTable = () => { if (inTable) { out.push('</tbody></table>'); inTable = false; } };
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      if (/^```/.test(line.trim())) {
        flushPara(); closeList(); closeTable();
        if (code === null) code = [];
        else { out.push(`<pre class="box">${esc(code.join('\\n'))}</pre>`); code = null; }
        continue;
      }
      if (code !== null) { code.push(line); continue; }
      if (/^#{1,4}\\s+/.test(line)) {
        flushPara(); closeList(); closeTable();
        const lvl = Math.min(3, line.match(/^#+/)[0].length);
        out.push(`<h${lvl}>${inlineMd(line.replace(/^#+\\s+/, ''))}</h${lvl}>`);
      } else if (/^\\|.*\\|$/.test(line.trim())) {
        flushPara(); closeList();
        const cells = line.trim().slice(1, -1).split('|').map((c) => c.trim());
        if (cells.every((c) => /^[-: ]+$/.test(c))) continue;
        if (!inTable) {
          inTable = true;
          out.push(`<table class="rt"><thead><tr>${cells.map((c) => `<th>${inlineMd(c)}</th>`).join('')}</tr></thead><tbody>`);
        } else {
          out.push(`<tr>${cells.map((c) => `<td>${inlineMd(c)}</td>`).join('')}</tr>`);
        }
      } else if (/^[-*]\\s+/.test(line.trim())) {
        flushPara(); closeTable();
        if (!inList) { out.push('<ul>'); inList = true; }
        out.push(`<li>${inlineMd(line.trim().replace(/^[-*]\\s+/, ''))}</li>`);
      } else if (!line.trim()) {
        flushPara(); closeList(); closeTable();
      } else {
        closeList(); closeTable();
        para.push(line.trim());
      }
    }
    flushPara(); closeList(); closeTable();
    if (code !== null) out.push(`<pre class="box">${esc(code.join('\\n'))}</pre>`);
    return out.join('');
  }

  function renderCsv(text) {
    const rows = String(text || '').trim().split(/\\r?\\n/).filter(Boolean).slice(0, 40).map((r) => r.split(','));
    if (!rows.length) return '<div class="sub">(empty CSV)</div>';
    const head = rows[0].map((c) => `<th>${esc(c.replace(/^"|"$/g, ''))}</th>`).join('');
    const body = rows.slice(1).map((r) => `<tr>${r.map((c) => `<td>${esc(c.replace(/^"|"$/g, ''))}</td>`).join('')}</tr>`).join('');
    return `<table class="rt"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
  }

  function formatDocBody(path, rawText) {
    const low = path.toLowerCase();
    if (low.endsWith('.md')) return `<div class="doc-body">${renderMd(rawText)}</div>`;
    if (low.endsWith('.csv')) return `<div class="doc-body">${renderCsv(rawText)}</div>`;
    if (low.endsWith('.json')) {
      try { return `<pre class="box" style="border:0;border-radius:0">${esc(JSON.stringify(JSON.parse(rawText), null, 2))}</pre>`; } catch (_) {}
    }
    return `<pre class="box" style="border:0;border-radius:0">${esc(rawText)}</pre>`;
  }

  function closeDrawer() { $('drawer').classList.remove('open'); $('backdrop').classList.remove('open'); }
  $('d-close').addEventListener('click', closeDrawer);
  $('backdrop').addEventListener('click', closeDrawer);

  function galleryItems(src) {
    const items = [{ label: 'Live view', url: `/api/source/${encodeURIComponent(src.name)}/live-pcb.png` }];
    for (const p of (src.preview_images || [])) items.push({ label: `${p.card}: ${p.label}`, url: p.url });
    return items;
  }

  function renderModalImage() {
    if (!lastSummary || !modalSrc) return;
    const src = (lastSummary.sources || []).find((s) => s.name === modalSrc);
    if (!src) return;
    const items = galleryItems(src);
    if (modalIdx >= items.length) modalIdx = 0;
    const item = items[modalIdx];
    $('m-title').textContent = `${src.project || src.name}${src.model ? ' [' + src.model + ']' : ''}`;
    $('m-tabs').innerHTML = items.map((it, idx) =>
      `<button class="btn${idx === modalIdx ? ' active' : ''}" data-idx="${idx}">${esc(it.label)}</button>`
    ).join('');
    $('m-tabs').querySelectorAll('button').forEach((b) => b.addEventListener('click', () => {
      modalIdx = Number(b.dataset.idx); renderModalImage();
    }));
    $('m-raw').href = item.url;
    $('m-meta').textContent = `updated ${new Date().toLocaleTimeString()}`;
    const img = $('m-img');
    img.onload = () => { img.style.display = 'block'; $('m-empty').textContent = ''; };
    img.onerror = () => { img.style.display = 'none'; $('m-empty').textContent = 'No live PCB render or routing screenshot available yet.'; };
    img.src = `${item.url}${item.url.includes('?') ? '&' : '?'}t=${Date.now()}`;
  }

  function openModal(sourceName, startIdx = 0) {
    modalSrc = sourceName; modalIdx = startIdx;
    $('pcb-modal').classList.add('open'); $('pcb-modal').setAttribute('aria-hidden', 'false');
    renderModalImage();
    if (modalTimer) clearInterval(modalTimer);
    if ($('m-auto').checked) modalTimer = setInterval(renderModalImage, 3000);
  }

  function closeModal() {
    modalSrc = null;
    $('pcb-modal').classList.remove('open'); $('pcb-modal').setAttribute('aria-hidden', 'true');
    if (modalTimer) { clearInterval(modalTimer); modalTimer = null; }
  }

  $('m-close').addEventListener('click', closeModal);
  $('m-refresh').addEventListener('click', renderModalImage);
  $('m-auto').addEventListener('change', (e) => {
    if (modalTimer) { clearInterval(modalTimer); modalTimer = null; }
    if (e.target.checked && modalSrc) modalTimer = setInterval(renderModalImage, 3000);
  });
  $('pcb-modal').addEventListener('click', (e) => { if (e.target === $('pcb-modal')) closeModal(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') { closeDrawer(); closeModal(); } });

  function bomTablesHtml(view, nodeId) {
    if (!['bom', 'stock', 'datasheets', 'inventory'].includes(nodeId)) return '';
    const arts = view.artifacts || {};
    const rows = (arts.bom && arts.bom.rows) || [];
    const parts = (arts.datasheets && arts.datasheets.board_parts) || [];
    const partsHtml = [];
    if (rows.length) {
      partsHtml.push(`<div class="doc-card"><div class="doc-head"><span>BOM (${rows.length} line items)</span></div><div class="doc-body"><table class="rt"><thead><tr><th>Refs</th><th>Qty</th><th>Value</th><th>MPN</th><th>LCSC</th></tr></thead><tbody>${rows.map((r) => `<tr><td><code>${esc(r.refs)}</code></td><td>${esc(r.qty)}</td><td>${esc(r.value)}</td><td><code>${esc(r.mpn || '')}</code></td><td><code>${esc(r.lcsc || '')}</code></td></tr>`).join('')}</tbody></table></div></div>`);
    }
    if (parts.length && (nodeId === 'datasheets' || nodeId === 'stock')) {
      partsHtml.push(`<div class="doc-card"><div class="doc-head"><span>Datasheets & Heights</span></div><div class="doc-body"><table class="rt"><thead><tr><th>Refs</th><th>Maker / MPN</th><th>LCSC</th><th>Height</th></tr></thead><tbody>${parts.map((p) => `<tr><td><code>${esc(p.refs)}</code></td><td>${esc((p.maker || '') + ' ' + (p.mpn || ''))}</td><td><code>${esc(p.lcsc || '')}</code></td><td>${esc(p.height_mm != null ? p.height_mm + ' mm' : '')}</td></tr>`).join('')}</tbody></table></div></div>`);
    }
    return partsHtml.join('');
  }

  function inspectToolDetails(title, tool, heal) {
    $('d-title').textContent = `${title} / board-graph Engine`;
    const blocks = [];
    if (heal) {
      const modeLbl = heal.mode === 'heal' ? '6-card Self-Heal' : '6-card Release';
      const statusText = [
        `Mode: ${modeLbl} | Cycle: ${heal.version || 'none'} | Position: ${heal.position || 'none'} (${heal.passed_count}/${heal.total_count})`,
        heal.from_card ? `Triggered from PCB card: ${heal.from_card}` : null,
        `Gate status: ${heal.gate_ok ? 'PASS' : 'BLOCKED (' + (heal.gate_reason || 'unverified') + ')'}`,
      ].filter(Boolean).join('\\n');
      blocks.push(`<div><div class="sec-lbl">Self-Healing Gate</div><pre class="box">${esc(statusText)}</pre></div>`);
    }
    if (tool) {
      const commits = (tool.commits_ahead || []).map((c) => `${c.sha}  ${c.subject}`).join('\\n');
      const lines = [
        `Version: ${tool.version || 'unknown'} | Branch: ${tool.branch || 'detached'} | Commit: ${tool.commit || '?'} | Dirty: ${tool.dirty ? 'yes' : 'no'}`,
        tool.ahead ? `\\nCommits ahead of origin/main (${tool.ahead}):\\n${commits}` : '\\nClean on main (0 commits ahead).',
      ].join('');
      blocks.push(`<div><div class="sec-lbl">Engine Commits</div><pre class="box">${esc(lines)}</pre></div>`);
    }
    $('d-body').innerHTML = blocks.join('') || '<div class="sub">No engine metadata.</div>';
    $('drawer').classList.add('open'); $('backdrop').classList.add('open');
  }

  function inspectHealCard(title, heal, cardId) {
    const card = ((heal && heal.cards) || []).find((c) => c.id === cardId) || {};
    $('d-title').textContent = `${title} / ${card.label || cardId}`;
    const blocks = [
      `<div class="sub"><code>${esc(cardId)}</code> &middot; status <code>${esc(card.status || 'ahead')}</code> &middot; who <code>${esc(card.who || '')}</code>${heal.version ? ` &middot; cycle <code>${esc(heal.version)}</code>` : ''}</div>`,
    ];
    if (card.task) blocks.push(`<div><div class="sec-lbl">Task</div><pre class="box">${esc(card.task)}</pre></div>`);
    if (card.pass_when) blocks.push(`<div><div class="sec-lbl">Pass Condition</div><pre class="box">${esc(card.pass_when)}</pre></div>`);
    if (card.working) blocks.push(`<div><div class="sec-lbl">Active Attempt</div><pre class="box">Working: ${esc(card.working)}</pre></div>`);
    if (card.last_outcome || card.last_note) {
      blocks.push(`<div><div class="sec-lbl">Record (${esc(card.last_outcome || '')} by ${esc(card.last_actor || '')})</div><pre class="box">${esc(card.last_note || '')}</pre></div>`);
    }
    if ((heal.commits_ahead || []).length) {
      const rows = heal.commits_ahead.map((c) => `${c.sha}  ${c.subject}`).join('\\n');
      blocks.push(`<div><div class="sec-lbl">Commits in this cycle (${heal.ahead})</div><pre class="box">${esc(rows)}</pre></div>`);
    }
    $('d-body').innerHTML = blocks.join('');
    $('drawer').classList.add('open'); $('backdrop').classList.add('open');
  }

  async function inspectCard(sourceName, nodeId) {
    $('d-title').textContent = `${sourceName} / ${nodeId}`;
    $('d-body').innerHTML = '<div class="sub">Loading...</div>';
    $('drawer').classList.add('open'); $('backdrop').classList.add('open');
    try {
      const encSrc = encodeURIComponent(sourceName);
      const view = await (await fetch(`/api/source/${encSrc}/view.json`, { cache: 'no-store' })).json();
      const node = ((view.graph && view.graph.nodes) || []).find((n) => n.id === nodeId) || {};
      const st = (view.state && view.state.nodes && view.state.nodes[nodeId]) || {};
      const artItems = ((view.artifacts && view.artifacts.cards && view.artifacts.cards[nodeId] && view.artifacts.cards[nodeId].items) || []);
      $('d-title').textContent = `${view.project || sourceName} / ${node.label || nodeId}`;

      const blocks = [`<div class="sub"><code>${esc(nodeId)}</code> &middot; status <code>${esc(st.status || 'ahead')}</code> &middot; who <code>${esc(node.who || '')}</code></div>`];
      if (node.task) blocks.push(`<div><div class="sec-lbl">Task</div><pre class="box">${esc(node.task)}</pre></div>`);

      const imgItems = artItems.filter((it) => it.exists && (it.kind === 'image' || String(it.path || '').toLowerCase().endsWith('.png')));
      const tableHtml = bomTablesHtml(view, nodeId);
      const readablePaths = [];
      for (const p of (node.owns || []).concat(artItems.filter((it) => it.exists).map((it) => it.path))) {
        const clean = String(p || '').replace(/^\\/+/, '');
        if (!clean || readablePaths.includes(clean)) continue;
        if (READABLE_EXTS.some((ext) => clean.toLowerCase().endsWith(ext))) readablePaths.push(clean);
        if (readablePaths.length >= 3) break;
      }

      const realityParts = [];
      if (tableHtml) realityParts.push(tableHtml);
      if (imgItems.length) {
        realityParts.push(`<div class="img-grid">${imgItems.map((it) => {
          const u = `/api/source/${encSrc}/files/${it.path.split('/').map(encodeURIComponent).join('/')}`;
          return `<a class="img-card" href="${u}" target="_blank" rel="noopener"><img src="${u}" alt="${esc(it.label || it.path)}"><span class="sub">${esc(it.label || it.path)}</span></a>`;
        }).join('')}</div>`);
      }
      if (readablePaths.length) {
        const fetched = await Promise.all(readablePaths.map(async (rel) => {
          const fileUrl = `/api/source/${encSrc}/files/${rel.split('/').map(encodeURIComponent).join('/')}`;
          try {
            const r = await fetch(`${fileUrl}?raw=1`, { cache: 'no-store' });
            if (!r.ok) return null;
            return { rel, fileUrl, text: (await r.text()).slice(0, 12000) };
          } catch (_) { return null; }
        }));
        for (const doc of fetched.filter(Boolean)) {
          realityParts.push(`<div class="doc-card"><div class="doc-head"><span>${esc(doc.rel)}</span><a href="${doc.fileUrl}" target="_blank" rel="noopener">Open</a></div>${formatDocBody(doc.rel, doc.text)}</div>`);
        }
      }
      if (realityParts.length) {
        blocks.push(`<div><div class="sec-lbl">Project Reality</div><div style="display:flex;flex-direction:column;gap:8px">${realityParts.join('')}</div></div>`);
      }

      const wfParts = [];
      if (st.attempt) wfParts.push(`<pre class="box">Active: ${esc(st.attempt.by)} since ${esc(st.attempt.since)}\\n${esc(st.attempt.note || '')}</pre>`);
      if (st.last) wfParts.push(`<pre class="box">Last transition (${esc(st.last.outcome)} by ${esc(st.last.actor)}):\\n${esc(st.last.note || '')}</pre>`);
      const allLinks = (node.owns || []).concat(node.docs || []);
      if (allLinks.length) {
        wfParts.push(`<div class="sub">${allLinks.map((p) => `<a href="/api/source/${encSrc}/files/${String(p).split('/').map(encodeURIComponent).join('/')}" target="_blank" rel="noopener"><code>${esc(p)}</code></a>`).join(' &middot; ')}</div>`);
      }
      if (wfParts.length) blocks.push(`<div><div class="sec-lbl">Workflow Record</div><div style="display:flex;flex-direction:column;gap:6px">${wfParts.join('')}</div></div>`);

      $('d-body').innerHTML = blocks.join('');
    } catch (err) {
      $('d-body').innerHTML = `<pre class="box">${esc(err.message)}</pre>`;
    }
  }

  function formatDuration(sec) {
    const s = Math.max(0, Math.floor(sec));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), rem = s % 60;
    const pad = (n) => String(n).padStart(2, '0');
    return h > 0 ? `${h}:${pad(m)}:${pad(rem)}` : `${pad(m)}:${pad(rem)}`;
  }

  function tickTimers() {
    document.querySelectorAll('.timer-pill[data-start]').forEach((el) => {
      const startMs = Date.parse(el.dataset.start);
      if (Number.isNaN(startMs)) return;
      const endMs = el.dataset.end ? Date.parse(el.dataset.end) : Date.now();
      const text = formatDuration((endMs - startMs) / 1000);
      el.textContent = el.dataset.end ? `${text} done` : text;
    });
  }

  function chipState(c) {
    if (c.status === 'passed') return ['passed', 'passed', false];
    if (c.status === 'current') {
      if (c.working) return ['current', `working (${c.working})`, true];
      if (c.last_outcome === 'needs_owner') return ['owner', 'needs you', false];
      if (c.last_outcome === 'needs_lead' || c.last_outcome === 'needs_skill') return ['lead', c.last_outcome, false];
      return ['current', 'current', false];
    }
    return ['ahead', c.who || 'ahead', false];
  }

  function visibleCardsForSource(src) {
    const cards = src.cards || [];
    const sel = scopePref !== null ? scopePref : (lastSummary && lastSummary.default_scope) || 'auto';
    if (sel === 'all') return { visible: cards, hidden: 0 };
    const stopId = sel === 'auto' ? src.autonomy_until : sel;
    if (!stopId) return { visible: cards, hidden: 0 };
    const stopIdx = cards.findIndex((c) => c.id === stopId);
    if (stopIdx < 0) return { visible: cards, hidden: 0 };
    return { visible: cards.slice(0, stopIdx + 1), hidden: Math.max(0, cards.length - (stopIdx + 1)) };
  }

  function toolPillInfo(src) {
    const tool = src.tool || {};
    const heal = src.tool_heal;
    if (heal && !heal.gate_ok) {
      return { cls: 'pill warn', text: `Self-heal needed (${heal.passed_count}/${heal.total_count})` };
    }
    if (heal && heal.active && (heal.dirty || heal.ahead > 0 || !heal.done)) {
      return { cls: 'pill heal', text: `Tool Heal ${heal.version}: ${heal.passed_count}/${heal.total_count}` };
    }
    if (tool.dirty || (tool.ahead && tool.ahead > 0)) {
      return { cls: 'pill warn', text: `Engine edits: ${tool.ahead || 0} commit(s)${tool.dirty ? ' + dirty' : ''}` };
    }
    if (tool.version) return { cls: 'pill ok', text: `board-graph v${tool.version}` };
    return null;
  }

  function renderHealStripHtml(heal, keyPrefix) {
    const cards = heal.cards || [];
    const chips = cards.map((c) => {
      const [cls, stText, pulsing] = chipState(c);
      const dot = pulsing ? '<span class="chip-dot"></span>' : '';
      return `<button class="chip ${cls}" type="button" title="${esc(c.label + ': ' + stText)}" data-heal-key="${esc(keyPrefix)}" data-heal-card="${esc(c.id)}">${dot}<span>${esc(c.label)}</span></button>`;
    }).join('');
    return chips;
  }

  function render(data) {
    lastSummary = data;
    if (scopePref === null && data.default_scope) $('scope-select').value = data.default_scope;
    const sources = data.sources || [];
    $('sub').textContent = `${sources.length} source${sources.length === 1 ? '' : 's'} · ${new Date().toLocaleTimeString()}`;
    const wrap = $('rows');
    wrap.innerHTML = '';

    const localHeal = data.local_tool_heal;
    if (localHeal && (localHeal.active || localHeal.dirty || localHeal.ahead > 0)) {
      const total = localHeal.total_count || 6;
      const pct = total ? Math.round((localHeal.passed_count / total) * 100) : 0;
      const modeLbl = localHeal.mode === 'heal' ? 'Self-Heal' : 'Release';
      const statusBadge = !localHeal.gate_ok
        ? `<span class="badge owner">${esc(localHeal.working ? 'Working: ' + localHeal.working : (localHeal.blocked_outcome || 'Gate open'))}</span>`
        : `<span class="pill ok">Gate Verified</span>`;
      const row = document.createElement('section');
      row.className = 'run-row heal-row';
      row.innerHTML = `
        <div class="run-top">
          <div class="run-title">
            <h2>board-graph ${esc(modeLbl)}${localHeal.version ? ' (' + esc(localHeal.version) + ')' : ''}</h2>
            <span class="pill heal">${localHeal.passed_count}/${total} (${pct}%)</span>
            ${statusBadge}
            ${localHeal.dirty ? '<span class="pill warn">Uncommitted changes</span>' : ''}
            ${localHeal.ahead ? `<button class="pill" data-local-commits="1">${localHeal.ahead} commit(s) ahead</button>` : ''}
          </div>
          <span class="sub">${esc(localHeal.from_card ? 'From PCB card: ' + localHeal.from_card : '6-card tool workflow')}</span>
        </div>
        <div class="prog-track"><div class="prog-fill heal-fill" style="width:${pct}%"></div></div>
        <div class="strip">${renderHealStripHtml(localHeal, '__local__')}</div>
      `;
      row.querySelectorAll('[data-heal-card]').forEach((el) => el.addEventListener('click', () => inspectHealCard('board-graph Engine', localHeal, el.dataset.healCard)));
      const cb = row.querySelector('[data-local-commits]');
      if (cb) cb.addEventListener('click', () => inspectToolDetails('board-graph Engine', null, localHeal));
      wrap.appendChild(row);
    }

    sources.forEach((src, rowIdx) => {
      const accent = RUN_ACCENTS[rowIdx % RUN_ACCENTS.length];
      const card = document.createElement('section');
      card.className = 'run-row';
      card.style.borderLeftColor = accent;
      const alertBadge = !src.online
        ? `<span class="badge off">Offline</span>`
        : src.blocked_outcome === 'needs_owner'
          ? `<span class="badge owner">Needs you: ${esc(src.position)}</span>`
          : src.blocked_outcome
            ? `<span class="badge lead">${esc(src.blocked_outcome)}: ${esc(src.position)}</span>`
            : '';
      const modelHtml = src.model ? `<span class="model-pill" style="color:${accent};border-color:${accent}66;background:${accent}18">${esc(src.model)}</span>` : '';
      const tm = src.timing || {};
      const timerHtml = tm.started_at ? `<span class="timer-pill${tm.finished_at ? ' done' : ''}" data-start="${esc(tm.started_at)}" data-end="${esc(tm.finished_at || '')}">--:--</span>` : '';
      const tp = toolPillInfo(src);
      const toolPillHtml = tp ? `<button class="${tp.cls}" type="button" data-tool-src="${esc(src.name)}">${esc(tp.text)}</button>` : '';
      const { visible, hidden } = visibleCardsForSource(src);
      const passedCount = visible.filter((c) => c.status === 'passed').length;
      const pct = visible.length ? Math.round((passedCount / visible.length) * 100) : 0;
      const previewCount = (src.preview_images || []).length;
      const pcbLabel = previewCount ? `Watch PCB (${previewCount})` : 'Watch PCB';
      const openLink = src.url ? `<a class="btn" href="${esc(src.url)}" target="_blank" rel="noopener">Open</a>` : '';
      const chips = visible.map((c) => {
        const [cls, stText, pulsing] = chipState(c);
        const dot = pulsing ? '<span class="chip-dot"></span>' : '';
        return `<button class="chip ${cls}" type="button" title="${esc(c.label + ': ' + stText)}" data-src="${esc(src.name)}" data-node="${esc(c.id)}">${dot}<span>${esc(c.label)}</span></button>`;
      }).join('') + (hidden ? `<span class="later-note">+${hidden} later</span>` : '');
      const th = src.tool_heal;
      const showVmHeal = th && (th.active || th.dirty || th.ahead > 0);
      const vmHealHtml = showVmHeal
        ? `<div class="heal-subrow"><div class="heal-subhead"><span>Tool Self-Heal (${esc(th.version || 'untracked')}): ${th.passed_count}/${th.total_count}</span><span class="sub">${esc(th.gate_ok ? 'Verified' : (th.gate_reason || 'In progress'))}</span></div><div class="strip">${renderHealStripHtml(th, src.name)}</div></div>`
        : '';
      const allPreviews = src.preview_images || [];
      const greenViews = allPreviews.filter((p) => /(?:^|\\/)3d_(top|bottom)\\.png$/i.test(p.path || ''));
      const previews = greenViews.length ? greenViews : allPreviews.slice(0, 2);
      const thumbsHtml = previews.length
        ? `<div class="thumbs">${previews.map((p) => `<button class="thumb-btn" type="button" data-pcb-src="${esc(src.name)}" data-pcb-idx="${allPreviews.indexOf(p) + 1}"><img src="${esc(p.url)}" alt="${esc(p.label)}"><span>${esc(p.label)}</span></button>`).join('')}</div>`
        : '';
      const errHtml = (!src.online && src.error) ? `<div class="err-line">${esc(src.error)}</div>` : '';
      card.innerHTML = `
        <div class="run-top">
          <div class="run-title">
            <h2>${esc(src.project || src.name)}</h2>
            ${modelHtml}${timerHtml}${alertBadge}${toolPillHtml}
          </div>
          <div style="display:flex;gap:6px;align-items:center">
            <span class="sub">${passedCount}/${visible.length} (${pct}%)</span>
            <button class="btn pcb-btn" type="button" data-pcb-src="${esc(src.name)}" data-pcb-idx="0">${pcbLabel}</button>
            ${openLink}
          </div>
        </div>
        <div class="prog-track"><div class="prog-fill" style="width:${pct}%"></div></div>
        <div class="strip">${chips}</div>
        ${vmHealHtml}${thumbsHtml}${errHtml}
      `;
      card.querySelectorAll('.chip[data-src]').forEach((el) => el.addEventListener('click', () => inspectCard(el.dataset.src, el.dataset.node)));
      card.querySelectorAll('[data-heal-card]').forEach((el) => el.addEventListener('click', () => inspectHealCard(src.project || src.name, th, el.dataset.healCard)));
      card.querySelectorAll('[data-pcb-src]').forEach((el) => el.addEventListener('click', () => openModal(el.dataset.pcbSrc, Number(el.dataset.pcbIdx || 0))));
      const tpBtn = card.querySelector('[data-tool-src]');
      if (tpBtn) tpBtn.addEventListener('click', () => inspectToolDetails(src.project || src.name, src.tool, src.tool_heal));
      wrap.appendChild(card);
    });
    tickTimers();
  }

  $('scope-select').addEventListener('change', (e) => {
    scopePref = e.target.value;
    if (lastSummary) render(lastSummary);
  });

  async function refresh() {
    try {
      const resp = await fetch('/api/summary', { cache: 'no-store' });
      render(await resp.json());
    } catch (err) {
      $('sub').textContent = `Error: ${err.message}`;
    }
  }

  const es = new EventSource('/events');
  es.onopen = () => { $('dot').className = 'dot live'; $('live-lbl').textContent = 'Live'; };
  es.onmessage = () => refresh();
  es.onerror = () => { $('dot').className = 'dot'; $('live-lbl').textContent = 'Reconnecting'; };
  refresh();
  setInterval(tickTimers, 1000);
})();
</script>
</body>
</html>
"""
