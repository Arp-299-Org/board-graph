#!/usr/bin/env python3
"""Multi-board / multi-run flat 2D board-graph Hub with live SSE updates."""
from __future__ import annotations

import argparse
import concurrent.futures
import http.server
import json
import mimetypes
import pathlib
import queue
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

KEEPALIVE = 15

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
  .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--ink-3); }
  .dot.live { background: var(--green-soft); box-shadow: 0 0 7px var(--green); }
  .wrap { padding: 10px 16px 24px; display: flex; flex-direction: column; gap: 10px; }
  .run-row { background: var(--surface); border: 1px solid var(--hair); border-left-width: 4px; border-radius: 8px;
             padding: 9px 12px; display: flex; flex-direction: column; gap: 7px; }
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
  .strip { display: flex; flex-wrap: wrap; align-items: center; gap: 5px; }
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
  .thumbs { display: flex; flex-wrap: nowrap; overflow-x: auto; gap: 8px; padding-top: 2px; }
  .thumb-btn { flex: none; display: flex; flex-direction: column; gap: 3px; padding: 4px; border-radius: 6px;
               border: 1px solid var(--hair); background: #0d0d11; }
  .thumb-btn:hover { border-color: var(--copper); }
  .thumb-btn img { height: 76px; width: auto; max-width: 150px; object-fit: contain; border-radius: 4px; background: #070709; display: block; }
  .thumb-btn span { font-size: 10px; color: var(--ink-3); font-family: var(--mono); max-width: 145px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; text-align: left; }
  .err-line { font-size: 11.5px; color: var(--red); background: var(--red-bg); border: 1px solid rgba(244,63,94,0.45); border-radius: 5px; padding: 5px 8px; }
  .tool-note { font-size: 11px; color: var(--ink-3); font-family: var(--mono); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
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
    return esc(text)
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  }

  function renderMd(md) {
    const lines = String(md || '').split(/\\r?\\n/);
    const out = [];
    let para = [];
    let code = null;
    let inList = false;
    let inTable = false;
    const flushPara = () => { if (para.length) { out.push(`<p>${inlineMd(para.join(' '))}</p>`); para = []; } };
    const closeList = () => { if (inList) { out.push('</ul>'); inList = false; } };
    const closeTable = () => { if (inTable) { out.push('</tbody></table>'); inTable = false; } };
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      if (/^```/.test(line.trim())) {
        flushPara(); closeList(); closeTable();
        if (code === null) { code = []; }
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
      try {
        return `<pre class="box" style="border:0;border-radius:0">${esc(JSON.stringify(JSON.parse(rawText), null, 2))}</pre>`;
      } catch (_) {}
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
      modalIdx = Number(b.dataset.idx);
      renderModalImage();
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
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const rem = s % 60;
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

  function render(data) {
    lastSummary = data;
    if (scopePref === null && data.default_scope) $('scope-select').value = data.default_scope;
    const sources = data.sources || [];
    $('sub').textContent = `${sources.length} source${sources.length === 1 ? '' : 's'} · ${new Date().toLocaleTimeString()}`;
    const wrap = $('rows');
    wrap.innerHTML = '';
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
      const modelHtml = src.model
        ? `<span class="model-pill" style="color:${accent};border-color:${accent}66;background:${accent}18">${esc(src.model)}</span>` : '';
      const tm = src.timing || {};
      const timerHtml = tm.started_at
        ? `<span class="timer-pill${tm.finished_at ? ' done' : ''}" data-start="${esc(tm.started_at)}" data-end="${esc(tm.finished_at || '')}">--:--</span>` : '';
      const tool = src.tool;
      const toolStr = tool ? `${tool.branch || ''}${tool.commit ? ' @' + tool.commit : ''}${tool.ahead ? ' +' + tool.ahead : ''}${tool.dirty ? ' dirty' : ''}` : '';
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
      const previews = (src.preview_images || []).slice(0, 5);
      const thumbsHtml = previews.length
        ? `<div class="thumbs">${previews.map((p, idx) => `<button class="thumb-btn" type="button" data-pcb-src="${esc(src.name)}" data-pcb-idx="${idx + 1}"><img src="${esc(p.url)}" alt="${esc(p.label)}"><span>${esc(p.label)}</span></button>`).join('')}</div>`
        : '';
      const errHtml = (!src.online && src.error) ? `<div class="err-line">${esc(src.error)}</div>` : '';
      card.innerHTML = `
        <div class="run-top">
          <div class="run-title">
            <h2>${esc(src.project || src.name)}</h2>
            ${modelHtml}
            ${timerHtml}
            ${alertBadge}
            ${toolStr ? `<span class="pill" style="font-family:var(--mono)">${esc(toolStr)}</span>` : ''}
          </div>
          <div style="display:flex;gap:6px;align-items:center">
            <span class="sub">${passedCount}/${visible.length} (${pct}%)</span>
            <button class="btn pcb-btn" type="button" data-pcb-src="${esc(src.name)}" data-pcb-idx="0">${pcbLabel}</button>
            ${openLink}
          </div>
        </div>
        <div class="prog-track"><div class="prog-fill" style="width:${pct}%"></div></div>
        <div class="strip">${chips}</div>
        ${thumbsHtml}
        ${errHtml}
      `;
      card.querySelectorAll('.chip').forEach((el) => el.addEventListener('click', () => inspectCard(el.dataset.src, el.dataset.node)));
      card.querySelectorAll('[data-pcb-src]').forEach((el) => el.addEventListener('click', () => openModal(el.dataset.pcbSrc, Number(el.dataset.pcbIdx || 0))));
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


class HubListeners:
    """Thread-safe set of SSE client queues."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queues: set[queue.Queue] = set()

    def add(self, q: queue.Queue) -> None:
        with self._lock:
            self._queues.add(q)

    def drop(self, q: queue.Queue) -> None:
        with self._lock:
            self._queues.discard(q)

    def wake(self) -> int:
        with self._lock:
            targets = list(self._queues)
        for q in targets:
            q.put(True)
        return len(targets)


def parse_source_spec(raw: str, *, is_project: bool = False) -> dict:
    """Parse NAME=URL or NAME=DIR into a normalized source record."""
    if "=" in raw:
        name, val = raw.split("=", 1)
        name, val = name.strip(), val.strip()
    else:
        val = raw.strip()
        name = pathlib.Path(val).name if is_project else urllib.parse.urlsplit(val).netloc
    if not name or not val:
        raise ValueError(f"invalid source specification {raw!r}; expected NAME=VALUE")
    if is_project:
        return {"name": name, "project": str(pathlib.Path(val).expanduser().resolve())}
    return {"name": name, "url": val.rstrip("/")}


def _parse_kv_flags(raw_list: list[str], flag_name: str, expected: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in raw_list or []:
        if "=" not in raw:
            raise ValueError(f"invalid {flag_name} {raw!r}; expected {expected}")
        name, val = raw.split("=", 1)
        if not name.strip() or not val.strip():
            raise ValueError(f"invalid {flag_name} {raw!r}; expected {expected}")
        out[name.strip()] = val.strip()
    return out


def load_sources(args: argparse.Namespace) -> list[dict]:
    """Load deduplicated Hub sources and optional --model / --started-at overrides from CLI or config."""
    sources: list[dict] = []
    if args.config:
        cfg = json.loads(pathlib.Path(args.config).read_text(encoding="utf-8"))
        for entry in cfg.get("sources") or []:
            if not isinstance(entry, dict) or not entry.get("name"):
                continue
            item = {"name": str(entry["name"])}
            if entry.get("model"):
                item["model"] = str(entry["model"])
            if entry.get("started_at"):
                item["started_at"] = str(entry["started_at"])
            if entry.get("project"):
                item["project"] = str(pathlib.Path(entry["project"]).expanduser().resolve())
            elif entry.get("url"):
                item["url"] = str(entry["url"]).rstrip("/")
            else:
                continue
            sources.append(item)
    for raw in args.source:
        sources.append(parse_source_spec(raw, is_project=False))
    for raw in args.project:
        sources.append(parse_source_spec(raw, is_project=True))
    models = _parse_kv_flags(getattr(args, "model", []), "--model", "NAME=MODEL")
    starts = _parse_kv_flags(getattr(args, "started_at", []), "--started-at", "NAME=ISO")
    deduped: dict[str, dict] = {}
    for src in sources:
        if src["name"] in models:
            src["model"] = models[src["name"]]
        if src["name"] in starts:
            src["started_at"] = starts[src["name"]]
        deduped[src["name"]] = src
    return list(deduped.values())


def fetch_source_view(src: dict, timeout: float = 3.0) -> dict:
    """Fetch full view.json dict for either an HTTP source or a local --project checkout."""
    if "project" in src:
        view_path = pathlib.Path(src["project"]) / "tools" / "board_graph_view" / "view.json"
        if not view_path.is_file():
            raise FileNotFoundError(f"missing {view_path}")
        return json.loads(view_path.read_text(encoding="utf-8"))
    with urllib.request.urlopen(f"{src['url']}/view.json", timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _collect_preview_images(src_name: str, view: dict) -> list[dict]:
    """Extract existing PNG artifacts from view.json, prioritizing routing_batch then placement."""
    cards = ((view.get("artifacts") or {}).get("cards") or {})
    priority = {"routing_batch": 0, "placement": 1}
    ordered_ids = sorted(cards.keys(), key=lambda cid: (priority.get(cid, 2), cid))
    encoded_src = urllib.parse.quote(src_name, safe="")
    out: list[dict] = []
    seen_paths: set[str] = set()
    for cid in ordered_ids:
        items = (cards.get(cid) or {}).get("items") or []
        for item in items:
            rel = str(item.get("path") or "").lstrip("/")
            if not rel or not rel.lower().endswith(".png") or not item.get("exists") or rel in seen_paths:
                continue
            seen_paths.add(rel)
            encoded_rel = "/".join(urllib.parse.quote(part, safe="") for part in rel.split("/"))
            out.append({
                "card": cid,
                "label": item.get("label") or pathlib.PurePosixPath(rel).name,
                "path": rel,
                "url": f"/api/source/{encoded_src}/files/{encoded_rel}",
            })
    return out


def _compute_timing(src: dict, view: dict, board_track: dict) -> dict:
    """Compute run start/finish timestamps from explicit override, view.timing, or transitions/log."""
    timing = view.get("timing") or {}
    transitions = board_track.get("transitions") or []
    log_rows = (view.get("log") or {}).get("rows") or []
    started_at = src.get("started_at") or timing.get("started_at")
    if not started_at:
        ats = [r["at"] for r in transitions if r.get("at")] + [r["at"] for r in log_rows if r.get("at")]
        started_at = min(ats) if ats else None
    finished_at = timing.get("finished_at")
    if not finished_at and transitions:
        last = transitions[-1]
        until = (view.get("autonomy") or {}).get("until")
        if board_track.get("done") or (until and last.get("node") == until and last.get("outcome") == "pass"):
            finished_at = last.get("at")
    return {"started_at": started_at, "finished_at": finished_at, "running": bool(started_at and not finished_at)}


def summarize_source(src: dict, timeout: float = 3.0) -> dict:
    """Build compact live status summary for one configured Hub source."""
    base = {"name": src["name"], "url": src.get("url"), "project_dir": src.get("project"), "model": src.get("model")}
    try:
        view = fetch_source_view(src, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return {**base, "online": False, "error": str(exc), "cards": [], "preview_images": []}

    graph_nodes = [n for n in (view.get("graph") or {}).get("nodes", []) if n.get("track") == "board"]
    node_states = (view.get("state") or {}).get("nodes") or {}
    board_track = next((t for t in (view.get("where") or {}).get("tracks", []) if t.get("id") == "board"), {})
    transitions = board_track.get("transitions") or []
    last = transitions[-1] if transitions else None
    position = board_track.get("position") or ((view.get("state") or {}).get("tracks") or {}).get("board", {}).get("position")
    model = src.get("model") or view.get("model") or (view.get("autonomy") or {}).get("model")

    cards = []
    passed_count = 0
    working = None
    blocked_outcome = None
    for n in graph_nodes:
        nid = n["id"]
        st = node_states.get(nid) or {}
        status = st.get("status", "ahead")
        if status == "passed":
            passed_count += 1
        attempt = st.get("attempt")
        last_node = st.get("last")
        last_out = last_node.get("outcome") if last_node else None
        if status == "current":
            if attempt:
                working = attempt.get("by")
            elif last_out in ("needs_owner", "needs_lead", "needs_skill"):
                blocked_outcome = last_out
        cards.append({
            "id": nid,
            "label": n.get("label") or nid,
            "who": n.get("who"),
            "status": status,
            "working": attempt.get("by") if attempt else None,
            "last_outcome": last_out,
        })

    return {
        **base,
        "online": True,
        "project": view.get("project") or src["name"],
        "model": model,
        "autonomy_until": (view.get("autonomy") or {}).get("until"),
        "position": position,
        "passed_count": passed_count,
        "working": working,
        "blocked_outcome": blocked_outcome,
        "tool": view.get("tool"),
        "timing": _compute_timing(src, view, board_track),
        "preview_images": _collect_preview_images(src["name"], view),
        "last": {
            "node": last.get("node"),
            "outcome": last.get("outcome"),
            "target": last.get("target"),
            "actor": last.get("actor"),
            "note": last.get("note"),
            "at": last.get("at"),
        } if last else None,
        "cards": cards,
    }


def _fetch_source_file(src: dict, rel: str, raw: bool = False, timeout: float = 5.0) -> tuple[bytes, str]:
    """Read raw file bytes and content-type from an HTTP or local project source."""
    parts = [urllib.parse.unquote(p) for p in rel.split("/") if p]
    if not parts or any(p in (".", "..") or "\\" in p for p in parts) or parts[0].startswith("."):
        raise PermissionError("forbidden path")
    if "project" in src:
        base = pathlib.Path(src["project"]).resolve()
        target = base.joinpath(*parts).resolve()
        target.relative_to(base)
        if not target.is_file():
            raise FileNotFoundError(rel)
        ctype = "text/plain; charset=utf-8" if raw else (mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        return target.read_bytes(), ctype
    encoded = "/".join(urllib.parse.quote(p, safe="") for p in parts)
    suffix = "?raw=1" if raw else ""
    with urllib.request.urlopen(f"{src['url']}/files/{encoded}{suffix}", timeout=timeout) as resp:
        return resp.read(), resp.headers.get_content_type() or "application/octet-stream"


def _fetch_live_pcb_bytes(src: dict, timeout: float = 5.0) -> tuple[bytes, str]:
    """Fetch /live-pcb.png or fall back to newest artifact PNG on older Cycle 1 servers."""
    if "project" in src:
        import board_graph_serve
        png, _, _ = board_graph_serve.latest_live_pcb(pathlib.Path(src["project"]))
        if png and png.is_file():
            return png.read_bytes(), "image/png"
    else:
        try:
            with urllib.request.urlopen(f"{src['url']}/live-pcb.png", timeout=timeout) as resp:
                return resp.read(), resp.headers.get_content_type() or "image/png"
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
    view = fetch_source_view(src, timeout=timeout)
    previews = _collect_preview_images(src["name"], view)
    if not previews:
        raise FileNotFoundError("no live PCB render or artifact PNG available")
    return _fetch_source_file(src, previews[0]["path"], timeout=timeout)


def _start_source_watchers(sources: list[dict], listeners: HubListeners, stop_event: threading.Event) -> None:
    def watch_http(src: dict) -> None:
        sse_url = f"{src['url']}/events"
        while not stop_event.is_set():
            try:
                req = urllib.request.Request(sse_url, headers={"Accept": "text/event-stream"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    while not stop_event.is_set():
                        raw = resp.readline()
                        if not raw:
                            break
                        if raw.startswith(b"data:"):
                            listeners.wake()
            except Exception:  # noqa: BLE001
                stop_event.wait(2.0)

    def watch_local(projects: list[dict]) -> None:
        mtimes: dict[str, int] = {}
        while not stop_event.is_set():
            changed = False
            for src in projects:
                p = pathlib.Path(src["project"]) / "tools" / "board_graph_view" / "view.json"
                try:
                    ns = p.stat().st_mtime_ns
                except OSError:
                    ns = 0
                if mtimes.get(src["name"]) != ns:
                    if src["name"] in mtimes:
                        changed = True
                    mtimes[src["name"]] = ns
            if changed:
                listeners.wake()
            stop_event.wait(1.0)

    locals_list = [s for s in sources if "project" in s]
    for src in sources:
        if "url" in src:
            threading.Thread(target=watch_http, args=(src,), daemon=True).start()
    if locals_list:
        threading.Thread(target=watch_local, args=(locals_list,), daemon=True).start()


class HubServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], sources: list[dict], listeners: HubListeners,
                 default_scope: str = "auto"):
        self.sources = sources
        self.source_map = {s["name"]: s for s in sources}
        self.listeners = listeners
        self.default_scope = default_scope
        super().__init__(server_address, HubHandler)


class HubHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass

    def _send(self, code: int, content_type: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _route_source(self, path: str, query: str) -> bool:
        rest = path[len("/api/source/"):]
        if "/" not in rest:
            return False
        raw_name, sub = rest.split("/", 1)
        src = self.server.source_map.get(urllib.parse.unquote(raw_name))
        if not src:
            self.send_error(404)
            return True
        if sub == "view.json":
            try:
                view = fetch_source_view(src, timeout=5.0)
                self._send(200, "application/json; charset=utf-8", json.dumps(view).encode("utf-8"))
            except Exception as exc:  # noqa: BLE001
                self._send(502, "application/json; charset=utf-8", json.dumps({"error": str(exc)}).encode("utf-8"))
            return True
        if sub == "live-pcb.png" or sub.startswith("files/"):
            raw = urllib.parse.parse_qs(query).get("raw") == ["1"]
            try:
                data, ctype = (_fetch_live_pcb_bytes(src) if sub == "live-pcb.png"
                               else _fetch_source_file(src, sub[len("files/"):], raw=raw))
                self._send(200, ctype, data)
            except (FileNotFoundError, urllib.error.HTTPError):
                self.send_error(404)
            except PermissionError:
                self.send_error(403)
            except Exception as exc:  # noqa: BLE001
                self._send(502, "application/json; charset=utf-8", json.dumps({"error": str(exc)}).encode("utf-8"))
            return True
        return False

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", HUB_HTML.encode("utf-8"))
        elif path == "/api/summary":
            with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(self.server.sources))) as pool:
                rows = list(pool.map(summarize_source, self.server.sources))
            payload = {
                "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "default_scope": self.server.default_scope,
                "sources": rows,
            }
            self._send(200, "application/json; charset=utf-8", json.dumps(payload).encode("utf-8"))
        elif path.startswith("/api/source/") and self._route_source(path, parsed.query):
            return
        elif path == "/events":
            self._serve_events()
        else:
            self.send_error(404)

    def _serve_events(self) -> None:
        turn: queue.Queue = queue.Queue()
        self.server.listeners.add(turn)
        try:
            self.send_response(200)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(f"data: {json.dumps({'ts': time.time()})}\n\n".encode("utf-8"))
            self.wfile.flush()
            while True:
                try:
                    turn.get(timeout=KEEPALIVE)
                    while not turn.empty():
                        turn.get_nowait()
                    self.wfile.write(f"data: {json.dumps({'ts': time.time()})}\n\n".encode("utf-8"))
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.server.listeners.drop(turn)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", action="append", default=[], help="NAME=URL of a live board-graph serve endpoint")
    parser.add_argument("--project", action="append", default=[], help="NAME=DIR of a local board-graph project")
    parser.add_argument("--model", action="append", default=[], help="NAME=MODEL label for a configured source")
    parser.add_argument("--started-at", action="append", default=[], help="NAME=ISO start timestamp for a configured source")
    parser.add_argument("--until", default="auto",
                        help="Stop card for visible pipeline scope ('auto' uses each run's autonomy.until, 'all' shows every card)")
    parser.add_argument("--config", help="JSON file with {\"sources\": [{\"name\": ..., \"url\"|\"project\": ...}]}")
    parser.add_argument("--bind", default="127.0.0.1", help="Address to bind (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Port to listen on (default: 8000)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        sources = load_sources(args)
    except (OSError, ValueError) as exc:
        print(f"board-graph hub: {exc}", file=sys.stderr)
        return 2
    if not sources:
        try:
            sources = [parse_source_spec(f"{pathlib.Path.cwd().name}={pathlib.Path.cwd()}", is_project=True)]
        except ValueError:
            pass
    listeners = HubListeners()
    stop_event = threading.Event()
    _start_source_watchers(sources, listeners, stop_event)
    httpd = HubServer((args.bind, args.port), sources, listeners, default_scope=args.until)
    print(f"board-graph hub serving {len(sources)} source(s) at http://{args.bind}:{httpd.server_port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
