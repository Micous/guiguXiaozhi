"""追踪可视化页面 —— 纯 HTML + 原生 JS，不引前端框架。

回答"这次对话到底发生了什么"：
  * 左侧：追踪列表（最近的对话）
  * 右侧：选中对话的步骤树 + 时间线 + 每步的完整输入输出
"""

from __future__ import annotations


def render_trace_page() -> str:
    """返回追踪页面 HTML"""
    return """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>硅谷小智 · 链路追踪</title>
<style>
  :root {
    --bg: #f6f8fb; --panel: #ffffff; --border: #e3e8ef;
    --text: #1f2933; --muted: #6b7785; --accent: #2563eb;
    --ok: #16a34a; --warn: #d97706; --err: #dc2626; --agent: #7c3aed;
  }
  * { box-sizing: border-box; }
  body { margin: 0; font: 14px/1.6 -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         background: var(--bg); color: var(--text); }
  header { padding: 14px 20px; background: var(--panel); border-bottom: 1px solid var(--border);
           display: flex; align-items: center; gap: 14px; }
  header h1 { font-size: 16px; margin: 0; font-weight: 600; }
  header .muted { color: var(--muted); font-size: 13px; }
  .wrap { display: grid; grid-template-columns: 320px 1fr; gap: 14px; padding: 14px; align-items: start; }
  .panel { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; overflow: hidden; }
  .panel > h2 { font-size: 13px; margin: 0; padding: 10px 14px; border-bottom: 1px solid var(--border);
                color: var(--muted); font-weight: 600; }
  .list { max-height: calc(100vh - 110px); overflow: auto; }
  .item { padding: 10px 14px; border-bottom: 1px solid var(--border); cursor: pointer; }
  .item:hover { background: #f1f5f9; }
  .item.active { background: #eff6ff; border-left: 3px solid var(--accent); }
  .item .q { font-size: 13px; margin-bottom: 3px; word-break: break-all; }
  .item .meta { font-size: 12px; color: var(--muted); display: flex; gap: 8px; flex-wrap: wrap; }
  .badge { padding: 1px 6px; border-radius: 4px; font-size: 11px; background: #eef2f7; color: var(--muted); }
  .badge.err { background: #fee2e2; color: var(--err); }
  .badge.ok { background: #dcfce7; color: var(--ok); }
  .badge.tool { background: #ede9fe; color: var(--agent); }
  .node { border-left: 2px solid var(--border); margin-left: 10px; padding-left: 14px; position: relative; }
  .node > .head { padding: 8px 0; cursor: pointer; display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; }
  .node > .head:hover .nm { color: var(--accent); }
  .nm { font-weight: 600; font-size: 13px; }
  .dur { color: var(--muted); font-size: 12px; }
  .attrs { background: #f8fafc; border: 1px solid var(--border); border-radius: 6px; padding: 8px 10px;
           margin: 4px 0 8px; font: 12px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;
           white-space: pre-wrap; word-break: break-all; max-height: 320px; overflow: auto; }
  .bar { height: 6px; background: #eef2f7; border-radius: 3px; overflow: hidden; margin: 2px 0 6px; }
  .bar > i { display: block; height: 100%; background: var(--accent); }
  .empty { padding: 28px; color: var(--muted); text-align: center; }
</style>
</head>
<body>
<header>
  <h1>硅谷小智 · 链路追踪</h1>
  <span class="muted">点击左侧对话查看完整的 Agent 执行流程</span>
</header>
<div class="wrap">
  <div class="panel">
    <h2>最近的对话</h2>
    <div class="list" id="traces"><div class="empty">加载中…</div></div>
  </div>
  <div class="panel">
    <h2 id="detail-title">执行详情</h2>
    <div id="detail"><div class="empty">选择左侧的一条对话</div></div>
  </div>
</div>
<script>
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s == null ? '' : s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

async function loadTraces() {
  const res = await fetch('/traces?limit=50');
  const data = await res.json();
  const box = $('#traces');
  if (!data.traces || !data.traces.length) { box.innerHTML = '<div class="empty">暂无追踪记录</div>'; return; }
  box.innerHTML = data.traces.map((t, i) => `
    <div class="item" data-id="${t.trace_id}" data-i="${i}">
      <div class="q">${esc((t.input_preview || '(无输入预览)').slice(0, 60))}</div>
      <div class="meta">
        <span class="badge ${t.has_error ? 'err' : 'ok'}">${t.has_error ? '有错误' : '正常'}</span>
        <span class="badge">${t.span_count} 步</span>
        <span class="badge">${Math.round(t.total_duration_ms)} ms</span>
        ${(t.tools||[]).map(x => `<span class="badge tool">${esc(x)}</span>`).join('')}
      </div>
    </div>`).join('');
  box.querySelectorAll('.item').forEach(el => el.onclick = () => showTrace(el.dataset.id, el));
}

async function showTrace(id, el) {
  document.querySelectorAll('.item').forEach(x => x.classList.remove('active'));
  el.classList.add('active');
  $('#detail-title').textContent = '执行详情 · ' + id.slice(0, 12);
  const res = await fetch('/trace/' + id);
  if (!res.ok) { $('#detail').innerHTML = '<div class="empty">追踪不存在</div>'; return; }
  const tree = await res.json();
  const total = (tree.summary && tree.summary.total_duration_ms) || 1;
  const render = (nodes) => nodes.map(n => {
    const w = Math.max(2, Math.round((n.duration_ms / total) * 100));
    const attrs = Object.keys(n.attributes && n.attributes.length ? n.attributes : {}).length
      ? JSON.stringify(n.attributes, null, 2) : '{}';
    return `<div class="node">
      <div class="head"><span class="nm">${esc(n.name)}</span>
        <span class="dur">${Math.round(n.duration_ms)} ms</span>
        ${n.error ? '<span class="badge err">错误</span>' : ''}
        <span class="muted">${esc(n.start_time || '')}</span></div>
      <div class="bar"><i style="width:${w}%"></i></div>
      <div class="attrs">${esc(attrs)}</div>
      ${n.children && n.children.length ? render(n.children) : ''}
    </div>`;
  }).join('');
  $('#detail').innerHTML = render(tree.root_spans || []);
}

loadTraces();
setInterval(loadTraces, 5000);
</script>
</body>
</html>"""
