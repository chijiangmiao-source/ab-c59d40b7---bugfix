"""Review page markup (vanilla JS, no external resources)."""

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>第三方诊断类复核 · 飞控地面工具</title>
<style>
  :root { --bg:#0f1419; --panel:#171e26; --line:#27313d; --fg:#d7e0ea;
          --muted:#8a99a8; --ok:#3fb950; --bad:#f85149; --accent:#58a6ff; }
  * { box-sizing: border-box; }
  body { margin:0; font:14px/1.5 -apple-system,"Segoe UI",Roboto,
         "PingFang SC","Microsoft YaHei",monospace; background:var(--bg);
         color:var(--fg); }
  header { padding:16px 24px; border-bottom:1px solid var(--line);
           background:var(--panel); }
  header h1 { margin:0; font-size:17px; }
  header p { margin:4px 0 0; color:var(--muted); font-size:12.5px; }
  main { max-width:1080px; margin:0 auto; padding:20px 24px 60px; }
  textarea { width:100%; height:150px; background:#0b0f14; color:var(--fg);
             border:1px solid var(--line); border-radius:6px; padding:10px;
             font-family:ui-monospace,Menlo,Consolas,monospace;
             font-size:12px; resize:vertical; }
  .row { display:flex; gap:12px; align-items:center; margin:10px 0;
         flex-wrap:wrap; }
  input[type=text] { background:#0b0f14; color:var(--fg);
         border:1px solid var(--line); border-radius:6px; padding:7px 10px;
         font-family:ui-monospace,monospace; }
  button { background:var(--accent); color:#0b0f14; border:0;
           border-radius:6px; padding:8px 18px; font-weight:600;
           cursor:pointer; }
  button:disabled { opacity:.5; cursor:wait; }
  .hint { color:var(--muted); font-size:12px; }
  .verdict { margin:16px 0; padding:12px 14px; border-radius:6px;
             font-weight:600; display:none; }
  .verdict.pass { display:block; background:rgba(63,185,80,.12);
             border:1px solid var(--ok); color:var(--ok); }
  .verdict.fail { display:block; background:rgba(248,81,73,.12);
             border:1px solid var(--bad); color:var(--bad); }
  section.panel { background:var(--panel); border:1px solid var(--line);
             border-radius:8px; margin-top:16px; overflow:hidden; }
  section.panel > h2 { margin:0; padding:10px 14px; font-size:13.5px;
             border-bottom:1px solid var(--line); color:var(--accent); }
  .pad { padding:12px 14px; }
  table { width:100%; border-collapse:collapse; font-size:12px;
          font-family:ui-monospace,Menlo,Consolas,monospace; }
  th, td { text-align:left; padding:6px 10px; border-bottom:1px solid
           var(--line); vertical-align:top; }
  th { color:var(--muted); font-weight:600; position:sticky; top:0;
       background:var(--panel); }
  td.pc { color:var(--accent); white-space:nowrap; }
  .types { color:#a5d6ff; word-break:break-all; }
  .empty { color:var(--muted); font-style:italic; }
  details { margin-top:14px; }
  summary { cursor:pointer; color:var(--muted); }
  pre.raw { background:#0b0f14; border:1px solid var(--line);
            border-radius:6px; padding:10px; overflow:auto;
            max-height:360px; font-size:11.5px; }
  .meta { color:var(--muted); font-size:12px; }
  code.k { color:#79c0ff; }
</style>
</head>
<body>
<header>
  <h1>第三方诊断类字节码复核</h1>
  <p>单个 Base64 编码的 JVM class 文件（≤ 64 KiB）· 范围：静态
     <code class="k">()V</code> 方法 · 输出逐偏移入栈状态、异常处理器入口状态与通过/首个拒绝证据</p>
</header>
<main>
  <label class="hint" for="b64">class 文件（Base64，单个）</label>
  <textarea id="b64" spellcheck="false" autocomplete="off"
    placeholder="粘贴 Base64…"></textarea>
  <div class="row">
    <label class="hint">目标静态方法名 <input type="text" id="method"
      value="verify" size="16"></label>
    <button id="go">提交复核</button>
    <span class="hint" id="sizeinfo"></span>
  </div>
  <div id="verdict" class="verdict"></div>

  <section class="panel" id="reportPanel" style="display:none">
    <h2>复核报告</h2>
    <div class="pad" id="meta"></div>
    <div id="extra"></div>
  </section>

  <details id="rawDetails">
    <summary>原始 JSON 响应</summary>
    <pre class="raw" id="raw"></pre>
  </details>
</main>
<script>
const b64 = document.getElementById('b64');
const methodInput = document.getElementById('method');
const goBtn = document.getElementById('go');
const verdictEl = document.getElementById('verdict');
const reportPanel = document.getElementById('reportPanel');
const metaEl = document.getElementById('meta');
const rawEl = document.getElementById('raw');
const sizeInfo = document.getElementById('sizeinfo');

b64.addEventListener('input', () => {
  const n = b64.value.replace(/\s/g, '').length;
  sizeInfo.textContent = n
    ? ('base64 字符数: ' + n + ' · 解码上限 ' + (64*1024) + ' 字节') : '';
});

function esc(s) {
  return String(s).replace(/[&<>"]/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}
function typeList(arr) {
  if (!arr || !arr.length) return '<span class="empty">∅</span>';
  return '<span class="types">' + arr.map(esc).join(' · ') + '</span>';
}

function showRejection(ev) {
  verdictEl.className = 'verdict fail';
  const d = ev.detail ? ' — ' + esc(JSON.stringify(ev.detail)) : '';
  verdictEl.textContent = '拒绝：[' + esc(ev.stage) + '] 字节偏移 '
    + esc(ev.offset) + '：' + esc(ev.reason) + d;
}

function renderReport(r) {
  metaEl.innerHTML =
    '<div>类：<code class="k">' + esc(r.class_name) + '</code> · 父类：'
    + esc(r.super_class || '—')
    + ' · 方法：<code class="k">' + esc(r.method) + '</code></div>'
    + '<div class="meta">major/minor=' + esc(r.class_file_version[0])
    + '.' + esc(r.class_file_version[1])
    + ' · max_stack=' + r.max_stack + ' · max_locals=' + r.max_locals
    + ' · code_length=' + r.code_length
    + ' · 指令数=' + r.instruction_count + '</div>';

  let html = '<table><thead><tr>'
    + '<th>pc</th><th>指令</th><th>入栈（底→顶）</th><th>高</th>'
    + '<th>局部变量</th><th>后继</th><th>异常边</th></tr></thead><tbody>';
  for (const o of r.offsets) {
    const exc = (o.exception_edges || []).map(e =>
      '@' + e.handler_pc + ' catch ' + esc(e.catch_class)
      + ' [' + e.start_pc + ',' + e.end_pc + ')').join('<br>')
      || '<span class="empty">—</span>';
    const succ = o.normal_successors.map(s => '@' + s).join(', ')
      || '<span class="empty">终</span>';
    const stackCell = o.reachable
      ? typeList(o.stack_in)
      : '<span class="empty">不可达</span>';
    const localsCell = o.reachable
      ? typeList(o.locals_in)
      : '<span class="empty">—</span>';
    const heightCell = o.reachable ? o.stack_height : '—';
    html += '<tr><td class="pc">' + o.pc + '</td><td>' + esc(o.mnemonic)
      + '</td><td>' + stackCell
      + '</td><td>' + heightCell + '</td><td>'
      + localsCell + '<td>' + succ + '</td><td>' + exc
      + '</td></tr>';
  }
  html += '</tbody></table>';

  const extra = document.getElementById('extra');
  extra.innerHTML =
    '<section class="panel" style="border:0;border-radius:0">'
    + '<h2>逐偏移入栈状态</h2><div class="pad">' + html + '</div></section>';

  let hh = '<h2 style="border-top:1px solid var(--line)">异常处理器入口状态</h2>';
  hh += '<div class="pad"><table><thead><tr><th>range</th><th>handler pc</th>'
      + '<th>catch</th><th>可达</th><th>入口栈</th><th>入口局部变量</th>'
      + '</tr></thead><tbody>';
  if (!r.exception_handlers.length)
    hh += '<tr><td colspan="6" class="empty">无异常表项</td></tr>';
  for (const h of r.exception_handlers) {
    hh += '<tr><td>[' + h.start_pc + ',' + h.end_pc + ')</td><td class="pc">'
      + h.handler_pc + '</td><td>' + esc(h.catch_class)
      + '</td><td>' + (h.reachable ? '是' : '否（无边进入）') + '</td><td>'
      + (h.reachable ? typeList(h.entry_stack) : '—') + '</td><td>'
      + (h.reachable ? typeList(h.entry_locals) : '—') + '</td></tr>';
  }
  hh += '</tbody></table></div>';
  extra.insertAdjacentHTML('beforeend', hh);

  if (r.stack_map_frames && r.stack_map_frames.length) {
    let sf = '<h2 style="border-top:1px solid var(--line)">StackMapTable 帧（已核对）</h2>';
    sf += '<div class="pad"><table><thead><tr><th>pc</th>'
        + '<th>声明局部变量</th><th>声明栈</th></tr></thead><tbody>';
    for (const f of r.stack_map_frames) {
      sf += '<tr><td class="pc">' + f.offset + '</td><td>'
        + typeList(f.locals) + '</td><td>' + typeList(f.stack)
        + '</td></tr>';
    }
    sf += '</tbody></table></div>';
    extra.insertAdjacentHTML('beforeend', sf);
  }
}

goBtn.addEventListener('click', async () => {
  // Clear prior conclusions before each run.
  verdictEl.className = 'verdict';
  verdictEl.textContent = '';
  reportPanel.style.display = 'none';
  metaEl.innerHTML = '';
  document.getElementById('extra').innerHTML = '';
  rawEl.textContent = '';
  goBtn.disabled = true;
  try {
    const resp = await fetch('/api/verify', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        class_base64: b64.value,
        method: methodInput.value || 'verify'
      })
    });
    const data = await resp.json();
    rawEl.textContent = JSON.stringify(data, null, 2);
    if (data.result === 'pass') {
      verdictEl.className = 'verdict pass';
      verdictEl.textContent = '通过：类型状态在全部正常边与异常边收敛，无拒绝证据。';
      reportPanel.style.display = '';
      renderReport(data.report);
    } else {
      showRejection(data.first_rejection);
    }
  } catch (e) {
    verdictEl.className = 'verdict fail';
    verdictEl.textContent = '请求失败：' + esc(e.message);
  } finally {
    goBtn.disabled = false;
  }
});
</script>
</body>
</html>
"""
