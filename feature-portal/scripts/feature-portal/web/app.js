/* ==========================================================
   功能移植工作台 — 前端逻辑
   步骤：评估 -> 移植 -> 编译烧录 -> 自测
   ========================================================== */
'use strict';

const S = {
  step: 1,
  boot: null,
  state: null,
  jobs: {},
  paths: { doc: false, code: false },
  conflictsOpen: false,
  selftest: [],
  dirty: false
};

/* ---------------- 基础工具 ---------------- */
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function normalizeWorkspacePath(p) {
  p = String(p || '').trim();
  const runtimePath = String((S.boot && S.boot.env && S.boot.env.workspace) || '');
  if (/^[A-Za-z]:[\\/]|^\\\\/.test(runtimePath)) return p;
  const m = p.match(/^\\\\wsl(?:\.localhost)?\\[^\\]+\\(.*)$/i);
  return m ? '/' + m[1].replace(/\\/g, '/') : p;
}

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

/* ---------------- 后端地址解析 ----------------
   直接以 http(s) 打开时同源请求；以 file:// 方式打开（IDE 预览面板）
   时自动探测本机 8770~8776 端口，找到哪个连上就用哪个。 */
let API_BASE = '';
const API_PORTS = [8770, 8771, 8772, 8773, 8774, 8775, 8776];

function withTimeout(promise, ms) {
  return Promise.race([
    promise,
    new Promise((_, rej) => setTimeout(() => rej(new Error('timeout')), ms))
  ]);
}

async function pingBase(base) {
  try {
    const r = await withTimeout(fetch(base + '/api/ping', { cache: 'no-store' }), 1500);
    if (!r.ok) return false;
    const d = await r.json();
    return !!(d && d.pong);
  } catch (e) { return false; }
}

async function resolveApiBase() {
  if (location.protocol !== 'file:') { API_BASE = ''; return; }
  const qs = new URLSearchParams(location.search).get('api');
  const cands = [];
  if (qs) cands.push(qs.replace(/\/$/, ''));
  const cached = localStorage.getItem('portalApiBase');
  if (cached && cands.indexOf(cached) < 0) cands.push(cached);
  API_PORTS.forEach(p => cands.push('http://127.0.0.1:' + p));
  for (const b of cands) {
    if (await pingBase(b)) {
      API_BASE = b;
      try { localStorage.setItem('portalApiBase', b); } catch (e) {}
      return;
    }
  }
  API_BASE = '';
}

async function api(path, body, method) {
  const url = /^https?:\/\//.test(path) ? path : API_BASE + path;
  const opt = { method: method || (body === undefined ? 'GET' : 'POST'), cache: 'no-store' };
  if (body !== undefined) {
    opt.headers = { 'Content-Type': 'application/json' };
    opt.body = JSON.stringify(body || {});
  }
  const res = await fetch(url, opt);
  let data;
  try { data = await res.json(); }
  catch (e) { throw new Error('服务返回非 JSON（HTTP ' + res.status + '）'); }
  return data;
}

function toast(msg, kind) {
  const el = document.createElement('div');
  el.className = 'alert ' + (kind || 'info');
  el.style.cssText = 'position:fixed;right:22px;bottom:96px;z-index:120;max-width:380px;box-shadow:0 16px 40px -14px #000';
  el.innerHTML = esc(msg);
  document.body.appendChild(el);
  setTimeout(() => { el.style.transition = 'opacity .4s'; el.style.opacity = '0'; }, 3200);
  setTimeout(() => el.remove(), 3700);
}

function fmtPct(v) { return (v == null ? '—' : (Math.round(v * 10) / 10) + '%'); }

/* ---------------- 初始化 ---------------- */
async function boot() {
  await resolveApiBase();
  try {
    S.boot = await api('/api/bootstrap');
  } catch (e) {
    renderOffline(e.message);
    return;
  }
  S.state = S.boot.state;
  startHeartbeat();
  renderEnv();
  renderProjects();
  restoreState();
  await loadBaseline();
  await loadSelftest();
  updateNext();
}

/* ---------------- 心跳 / 关闭登记 ----------------
   启动器给服务传了 --idle-exit，服务靠「还有几个页面开着」决定何时退出。
   每个标签页有唯一 id：关一个不影响其它；全关后服务自动停。
   注意：这里不能因为 document.hidden 就跳过心跳 —— 用户切到别的标签页时
   定时器会被浏览器节流，跳过反而会让服务误判没人了。 */
const PAGE_ID = Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
let heartbeatTimer = null;

function startHeartbeat() {
  if (heartbeatTimer) clearInterval(heartbeatTimer);

  const beat = () => {
    fetch(API_BASE + '/api/ping?id=' + PAGE_ID, { cache: 'no-store' }).catch(() => {});
  };

  beat();
  heartbeatTimer = setInterval(beat, 20000);

  // 页面重新可见时补一次，缩短节流造成的心跳空档
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) beat();
  });

  window.addEventListener('pagehide', () => {
    try {
      // text/plain 是简单请求，不会触发 preflight；sendBeacon 不支持预检
      const data = new Blob(['{}'], { type: 'text/plain;charset=UTF-8' });
      navigator.sendBeacon(API_BASE + '/api/bye?id=' + PAGE_ID, data);
    } catch (e) { /* 老浏览器忽略，服务那边会心跳超时兜底 */ }
  });
}

function renderOffline(msg) {
  const isFile = location.protocol === 'file:';
  const tried = isFile
    ? ('已自动尝试：' + API_PORTS.map(p => '127.0.0.1:' + p).join('、'))
    : ('当前源：' + location.origin);
  const box = document.createElement('div');
  box.id = 'offlineBox';
  box.className = 'offline';
  box.innerHTML =
    '<div class="offline-in">' +
    '<h2>后端未连接</h2>' +
    '<p class="offline-sub">浏览器报错：<code>' + esc(msg) + '</code></p>' +

    '<div class="offline-sec">' +
    '<h4>' + (isFile ? '原因：当前页面是「本地文件」方式打开的' : '原因：服务没起来，或端口/地址不对') + '</h4>' +
    '<p>' + (isFile
      ? 'IDE 预览面板把 <code>index.html</code> 当静态文件渲染，<code>/api/...</code> 请求会指向 <code>file:///api/...</code>，必然失败。'
      : '页面能打开只说明当前这个端口有静态服务，不代表后端在跑。') +
    '</p>' +
    '<p class="dim mono" style="font-size:11.5px">' + esc(tried) + '</p>' +
    '</div>' +

    '<div class="offline-sec">' +
    '<h4>1. 双击启动器（推荐，不用敲命令）</h4>' +
    '<pre class="offline-cmd">' +
    '\\\\wsl.localhost\\Ubuntu-20.04\\home\\hgw\\monaco\\tools\\feature-portal\\launch.vbs' +
    '</pre>' +
    '<p class="dim">它会自动在 WSL 里拉起后端、等就绪后打开浏览器。' +
    '关闭所有页面后服务会自动退出，不用手动停。' +
    '如果 Windows 提示“来自网络位置”，点“运行”即可。</p>' +
    '</div>' +

    '<div class="offline-sec">' +
    '<h4>2. 或者手动在 WSL 里启动</h4>' +
    '<pre class="offline-cmd">cd /home/hgw/monaco/tools/feature-portal\nbash run.sh</pre>' +
    '<p class="dim">不带 <code>--idle-exit</code> 启动时服务常驻，Ctrl+C 停止。' +
    '终端会打印实际端口（被占用时自动顺延）。</p>' +
    '</div>' +

    '<div class="offline-sec">' +
    '<h4>3. 再用系统浏览器打开那个地址</h4>' +
    '<pre class="offline-cmd">http://127.0.0.1:8770</pre>' +
    '<p class="dim">不要用 IDE 预览面板（file://），它连不了后端。</p>' +
    '</div>' +

    '<div class="offline-sec">' +
    '<h4>排查：后端到底起没起</h4>' +
    '<pre class="offline-cmd">curl -s http://127.0.0.1:8770/api/ping</pre>' +
    '<p class="dim">返回 <code>{"ok":true,"pong":true,...}</code> 说明后端正常，问题在浏览器地址；' +
    '报 <code>Connection refused</code> 说明服务没起来，看 <code>/tmp/feature-portal.log</code>。</p>' +
    '</div>' +

    '<div class="offline-actions">' +
    '<button class="btn" id="btnRetryConn">重新连接</button>' +
    '<span class="dim" id="retryNote"></span>' +
    '</div>' +
    '</div>';
  document.body.appendChild(box);
  $('#btnRetryConn').onclick = async () => {
    const note = $('#retryNote');
    note.textContent = '正在探测…';
    $('#btnRetryConn').disabled = true;
    try { localStorage.removeItem('portalApiBase'); } catch (e) {}
    await resolveApiBase();
    if (!API_BASE && location.protocol === 'file:') {
      // 最后再试同源
      API_BASE = '';
    }
    try {
      await api('/api/ping');
      location.reload();
    } catch (e2) {
      note.textContent = '仍无法连接：' + e2.message;
      $('#btnRetryConn').disabled = false;
    }
  };
}

function renderEnv() {
  const e = S.boot.env;
  const chips = [
    ['active-build', !!e.active_build],
    ['v3dl', !!e.v3dl],
    ['gerrit_dl.py', !!e.gerrit_script],
    ['sudo', !!e.sudo_ready]
  ];
  $('#envChips').innerHTML = chips.map(([n, ok]) =>
    `<span class="chip ${ok ? 'on' : 'off'}" title="${esc(ok ? '已就绪' : '未找到 / 未授权')}">
       <i></i>${esc(n)}</span>`).join('');
  $('#wsPath').textContent = e.workspace + '  ·  python ' + e.python;
}

function renderProjects() {
  const p = S.boot.projects;
  const famSel = $('#selFamily'), prodSel = $('#selProduct');
  const act = p.active || {};
  famSel.innerHTML = p.families.map(f =>
    `<option value="${esc(f)}">${esc(f)}</option>`).join('');
  if (act.family && p.families.includes(act.family)) famSel.value = act.family;
  fillProducts(act.product);
  $('#inpThreads').value = act.threads || '24';
  const savedMode = (S.state && S.state.build && S.state.build.mode) || 'debug';
  $('#selBuildMode').value = savedMode === 'release' ? 'release' : 'debug';
  $('#activeBadge').textContent = act.family
    ? '当前激活：' + act.family + ' / ' + act.product
    : '未检测到激活配置';
  updateBuildNote();
}

function fillProducts(selected) {
  const p = S.boot.projects;
  const fam = $('#selFamily').value;
  const list = (p.products && p.products[fam]) || [];
  $('#selProduct').innerHTML = list.map(x =>
    `<option value="${esc(x)}">${esc(x)}</option>`).join('');
  if (selected && list.includes(selected)) $('#selProduct').value = selected;
}

function updateBuildNote() {
  const fam = $('#selFamily').value, prod = $('#selProduct').value;
  const extra = $('#inpExtra').value.trim();
  const mode = $('#selBuildMode').value || 'debug';
  $('#buildCmdNote').textContent =
    '执行：cd build && active-build -m ' + mode + (extra ? ' ' + extra : '') +
    '    （family=' + fam + ', project=' + prod + '）';
}

/* --- 输入即存：目录 / topic 这些别因为没点按钮就丢了 --- */
const LS_KEY = 'feature-portal.form';
const FORM_FIELDS = ['featureName', 'evalTopic', 'gerritTopic', 'targetPath',
                     'basePath', 'baseBranch', 'baseXml'];
let saveTimer = null;

function formSnapshot() {
  const o = {};
  FORM_FIELDS.forEach(id => {
    const el = $('#' + id);
    if (el) o[id] = el.value;
  });
  return o;
}

function lsSaveLocal() {
  try { localStorage.setItem(LS_KEY, JSON.stringify(formSnapshot())); } catch (e) { /* 隐私模式 */ }
}

function lsLoadLocal() {
  try { return JSON.parse(localStorage.getItem(LS_KEY) || '{}') || {}; } catch (e) { return {}; }
}

/* 输入停下 700ms 后落盘；同时先写一份 localStorage，服务端挂了也还在 */
function saveSoon() {
  lsSaveLocal();
  if (saveTimer) clearTimeout(saveTimer);
  saveTimer = setTimeout(() => { saveTimer = null; saveState(); }, 700);
}

function pickSaved(serverVal, key, fb) {
  if (serverVal != null && String(serverVal).trim() !== '') return String(serverVal);
  const ls = lsLoadLocal();
  if (ls[key] != null && String(ls[key]).trim() !== '') return String(ls[key]);
  return fb || '';
}

function restoreState() {
  const st = S.state || {};
  $('#featureName').value = pickSaved(st.feature_name, 'featureName', '');
  const savedTopic = pickSaved((st.port && st.port.topic) || '', 'gerritTopic', '');
  $('#evalTopic').value = savedTopic;
  // 评估阶段填过的 topic 直接沿用，移植步骤不用再填一次
  $('#gerritTopic').value = savedTopic;
  $('#topicSource').hidden = !savedTopic;
  const p = normalizeWorkspacePath(pickSaved((st.port && st.port.path) || '', 'targetPath', (st.workspace || '')));
  $('#targetPath').value = p;
  $('#pathNote').textContent = p ? '上次选择：' + p : '还没选目录：点「浏览」或直接在框里粘贴路径';

  // 代码基线：默认沿用上次的目录
  const bl = st.baseline || {};
  $('#basePath').value = normalizeWorkspacePath(pickSaved(bl.path, 'basePath',
    st.workspace || (S.boot && S.boot.env.workspace) || ''));
  const br = pickSaved(bl.branch, 'baseBranch', '');
  if (br) $('#baseBranch').value = br;

  // 最近用过的目录，做成下拉候选
  fillRecentPaths((st.recent_paths || []).concat([p, $('#basePath').value]));

  S.paths.doc = !!(st.eval && st.eval.doc && st.eval.doc.done);
  S.paths.code = !!(st.eval && st.eval.code && st.eval.code.done);
  S.conflictsOpen = !!(st.port && (st.port.conflicts || []).length);
  syncChecks();

  if (st.build && st.build.ok) {
    goStep(3, true);
    $('#buildState').textContent = '上次编译成功';
    $('#buildState').className = 'run-state ok';
    showBuildOk({});
  }
}

function fillRecentPaths(paths) {
  const dl = $('#recentPaths');
  if (!dl) return;
  const seen = {};
  const items = [];
  (paths || []).forEach(p => {
    p = (p || '').trim();
    if (!p || seen[p]) return;
    seen[p] = 1;
    items.push(p);
  });
  dl.innerHTML = items.slice(0, 10).map(p => `<option value="${esc(p)}"></option>`).join('');
}

function syncChecks() {
  $('#chkDoc').checked = S.paths.doc;
  $('#chkCode').checked = S.paths.code;
  $('#cardDoc').classList.toggle('checked', S.paths.doc);
  $('#cardCode').classList.toggle('checked', S.paths.code);
  updateNext();
}

/* ---------------- 步骤切换 ---------------- */
function goStep(n, force) {
  if (!force && n > 1 && !(S.paths.doc && S.paths.code)) {
    toast('请先完成文档评估和代码评估并勾选', 'warn');
    return;
  }
  S.step = n;
  $$('.pane').forEach(p => p.classList.remove('active'));
  $('#pane' + n).classList.add('active');
  $$('.step').forEach(b => {
    const idx = +b.dataset.step;
    b.classList.toggle('active', idx === n);
    b.classList.toggle('done', idx < n);
    b.classList.toggle('locked', idx > 1 && !(S.paths.doc && S.paths.code));
  });
  if (n === 2) refreshPortState();
  if (n === 3) updateBuildNote();
  if (n === 4) loadSelftest();
  updateNext();
  window.scrollTo({ top: 0, behavior: 'smooth' });
  saveState();
}

function updateNext() {
  const btn = $('#btnNext');
  const hint = $('#hint');
  if (S.step === 1) {
    const ok = S.paths.doc && S.paths.code;
    btn.disabled = !ok;
    btn.textContent = '下一步：代码移植';
    hint.textContent = ok ? '两项评估已完成，可以进入移植'
      : '请完成文档评估和代码评估，并勾选两个多选框';
  } else if (S.step === 2) {
    const blocked = S.conflictsOpen;
    btn.disabled = blocked;
    btn.textContent = '下一步：编译';
    hint.textContent = blocked
      ? '存在未解决的冲突，解决后点「重新分析工作区冲突」'
      : '代码已落地，可以进入编译';
  } else if (S.step === 3) {
    btn.disabled = false;
    btn.textContent = '下一步：自测';
    hint.textContent = '编译烧录完成后进入自测';
  } else {
    btn.disabled = true;
    btn.textContent = '流程结束';
    hint.textContent = '自测完成后本次移植闭环';
  }
}

async function saveState() {
  try {
    const topic = ($('#gerritTopic').value || $('#evalTopic').value || '').trim();
    const port = { path: $('#targetPath').value };
    if (topic) port.topic = topic;  // 空值不要写回去，否则会覆盖评估阶段记下的 topic
    await api('/api/state', {
      feature_name: $('#featureName').value.trim(),
      eval: {
        doc: { done: S.paths.doc },
        code: { done: S.paths.code }
      },
      port: port,
      build: {
        family: $('#selFamily').value,
        product: $('#selProduct').value,
        mode: $('#selBuildMode').value,
        threads: $('#inpThreads').value
      },
      baseline: {
        path: $('#basePath').value,
        xml: $('#baseXml').value,
        branch: $('#baseBranch').value
      }
    });
    lsSaveLocal();
    rememberPath($('#targetPath').value);
  } catch (e) { /* 静默 */ }
}

/* 把用过的目录记进 recent_paths，下次输入框直接下拉出来 */
function rememberPath(p) {
  p = (p || '').trim();
  if (!p) return;
  const cur = ((S.state && S.state.recent_paths) || []).slice();
  if (cur[0] === p) return;
  const next = [p].concat(cur.filter(x => x !== p)).slice(0, 10);
  S.state = S.state || {};
  S.state.recent_paths = next;
  fillRecentPaths(next.concat([$('#basePath').value]));
  api('/api/state', { recent_paths: next }).catch(() => {});
}

/* ---------------- 代码基线（目录 / manifest / 分支） ---------------- */
async function switchWorkspace(p) {
  if (!p) return;
  if (S.base && S.base.workspace === p) { await loadBaseline(true); return; }
  $('#baseBadge').textContent = '切换中…';
  let r;
  try {
    r = await api('/api/workspace/switch', { path: p });
  } catch (e) {
    toast('切换目录失败：' + e.message, 'danger');
    $('#baseBadge').textContent = '切换失败';
    return;
  }
  if (!r.ok) {
    toast(r.error || '该目录不是 repo 工作区', 'danger');
    $('#baseBadge').textContent = '非 repo 目录';
    $('#basePath').value = (S.base && S.base.workspace) || '';
    return;
  }
  $('#basePath').value = r.workspace;
  S.base = r;
  // 换了工作区，评估结果全部作废
  S.paths.code = false;
  if (S.state) {
    S.state.eval = S.state.eval || {};
    S.state.eval.code = { done: false };
    S.state.workspace = r.workspace;
  }
  syncChecks();
  try {
    S.boot = await api('/api/bootstrap');
    S.state = S.boot.state;
    renderEnv();
    renderProjects();
  } catch (e) { /* 忽略 */ }
  await loadBaseline();
  saveState();
  toast('工作区已切换到 ' + r.workspace + '，请确认基线后重新评估', 'ok');
}

async function loadBaseline(keepSel) {
  const btn = $('#btnBaseRefresh');
  if (btn) btn.disabled = true;
  $('#baseBadge').textContent = '读取中…';
  let r;
  try {
    r = await api('/api/repo/baseline');
  } catch (e) {
    $('#baseBadge').textContent = '读取失败';
    $('#baseStatus').innerHTML = '<span class="v warn">' + esc(e.message) + '</span>';
    if (btn) btn.disabled = false;
    return;
  }
  if (!r.ok) {
    $('#baseBadge').textContent = '不可用';
    $('#baseStatus').innerHTML = '<span class="v warn">' + esc(r.error || '读取失败') + '</span>';
    if (btn) btn.disabled = false;
    return;
  }
  S.base = r;
  // 后端是工作区的权威来源，直接对齐（避免 state 里的旧路径与真实根不一致）
  $('#basePath').value = r.workspace;

  const sel = $('#baseXml');
  const prev = keepSel ? sel.value
    : ((S.state && S.state.baseline && S.state.baseline.xml) || '');
  sel.innerHTML = '';
  const list = r.manifests || [];
  if (!list.length) {
    const o = document.createElement('option');
    o.value = '';
    o.textContent = '（该目录不是 repo 工作区）';
    sel.appendChild(o);
  }
  list.forEach(m => {
    const o = document.createElement('option');
    o.value = m.name;
    o.textContent = m.name
      + '  ·  ' + (m.revision || '未声明 revision')
      + (m.projects ? '  ·  ' + m.projects + ' 仓库' : '')
      + (m.current ? '  （当前）' : '');
    sel.appendChild(o);
  });
  if (prev && list.some(m => m.name === prev)) sel.value = prev;
  else {
    const cur = list.find(m => m.current);
    sel.value = cur ? cur.name : (list[0] ? list[0].name : '');
  }

  const dl = $('#baseBranchList');
  dl.innerHTML = '';
  (r.branches || []).forEach(b => {
    const o = document.createElement('option');
    o.value = b;
    dl.appendChild(o);
  });
  const sb = S.state && S.state.baseline;
  if (sb && sb.branch && !$('#baseBranch').value) $('#baseBranch').value = sb.branch;

  renderBaseStatus();
  if (btn) btn.disabled = false;
}

function renderBaseStatus() {
  const r = S.base || {};
  const sel = $('#baseXml').value || '';
  const m = (r.manifests || []).find(x => x.name === sel) || {};
  const parts = [];
  parts.push('<span><span class="k">当前 manifest</span> <span class="v">'
    + esc(r.current_manifest || '—') + '</span></span>');
  parts.push('<span><span class="k">实际 HEAD</span> <span class="v">'
    + esc(r.head || '—') + '</span>'
    + (r.head_repo ? ' <span class="k">@' + esc(r.head_repo) + '</span>' : '')
    + '</span>');
  parts.push('<span><span class="k">选中 xml</span> <span class="v">'
    + esc(sel || '—') + (m.revision ? ' → ' + esc(m.revision) : '') + '</span></span>');
  parts.push('<span><span class="k">切换脚本</span> <span class="v '
    + (r.checkout_script_ok ? 'ok' : 'warn') + '">'
    + (r.checkout_script_ok ? 'repo_force_checkout.sh 可用' : '未找到') + '</span></span>');
  const ab = r.active_build || {};
  if (ab.family || ab.project) {
    parts.push('<span><span class="k">编译配置</span> <span class="v">'
      + esc((ab.family || '?') + ' / ' + (ab.project || '?')) + '</span></span>');
  }
  $('#baseStatus').innerHTML = parts.join('');
  $('#baseBadge').textContent = r.has_repo
    ? ((r.manifests || []).length + ' 个 manifest')
    : '非 repo 目录';
}

function onBaseXmlChange() {
  const r = S.base || {};
  const m = (r.manifests || []).find(x => x.name === $('#baseXml').value);
  // 分支跟随所选 xml 的 revision，用户仍可手改
  $('#baseBranch').value = (m && m.revision) || '';
  renderBaseStatus();
}

async function doCheckout() {
  const path = ($('#basePath').value || '').trim();
  const xml = $('#baseXml').value || '';
  const branch = ($('#baseBranch').value || '').trim();
  const jobs = $('#baseJobs').value || '8';
  if (!path) { toast('请先选择代码目录', 'warn'); return; }
  if (!S.base || !S.base.checkout_script_ok) {
    toast('该目录下没有 repo_force_checkout.sh', 'danger');
    return;
  }
  const msg = '确认把代码更新到下面这套基线？\n\n'
    + '目录    : ' + path + '\n'
    + 'manifest: ' + (xml || '(当前)') + '\n'
    + '分支    : ' + (branch || '(按 manifest 的 revision)') + '\n'
    + '并发    : -j' + jobs + '\n\n'
    + '⚠ 会强制丢弃各仓库所有未提交改动（默认保留 build/sign.pem）。';
  if (!confirm(msg)) return;

  const log = $('#baseLog');
  log.hidden = false;
  log.textContent = '';
  $('#btnBaseCheckout').disabled = true;
  try {
    const r = await api('/api/repo/checkout', { path, xml, branch, jobs });
    if (!r.ok) { appendLog(log, ['!! ' + (r.error || '启动失败')]); return; }
    appendLog(log, ['$ ' + r.cmd, 'cwd: ' + r.cwd, '']);
    const job = await followJob(r.job.id, log, null);
    appendLog(log, ['', '=== 结束：' + (job.status === 'ok' ? '成功' : '失败 rc=' + job.rc)]);
    if (job.status === 'ok') {
      toast('代码已更新到指定基线，请重新做代码评估', 'ok');
      // 代码变了，之前的代码评估结果作废
      S.paths.code = false;
      if (S.state) {
        S.state.eval = S.state.eval || {};
        S.state.eval.code = { done: false };
        S.state.baseline = { path: path, xml: xml, branch: branch };
      }
      syncChecks();
      saveState();
      await loadBaseline(true);
    } else {
      toast('代码更新失败，看日志末尾', 'danger');
    }
  } catch (e) {
    appendLog(log, ['!! ' + e.message]);
  } finally {
    $('#btnBaseCheckout').disabled = false;
  }
}

async function refreshBaseCodegraph() {
  const path = ($('#basePath').value || '').trim();
  if (!path) { toast('请先选择代码目录', 'warn'); return; }
  const btn = $('#btnBaseCodegraph');
  const log = $('#baseLog');
  btn.disabled = true;
  log.hidden = false;
  log.textContent = '正在刷新 CodeGraph…\n';
  try {
    const r = await api('/api/repo/codegraph-refresh', { path });
    if (!r.ok) { appendLog(log, ['!! ' + (r.error || '启动失败')]); return; }
    appendLog(log, ['$ ' + r.cmd, 'cwd: ' + r.cwd, '']);
    const job = await followJob(r.job.id, log, null);
    if (job.status === 'ok') toast('CodeGraph 已刷新', 'ok');
    else toast('CodeGraph 刷新失败，看日志末尾', 'danger');
  } catch (e) {
    appendLog(log, ['!! ' + e.message]);
  } finally {
    btn.disabled = false;
  }
}

/* ---------------- 步骤 1：文档评估 ---------------- */
async function runDocEval() {
  const name = $('#featureName').value.trim();
  const card = $('#cardDoc');
  const body = $('#bodyDoc');
  card.classList.add('running');
  body.innerHTML = '<div class="dim">正在检索 docs/ 与 documentation/ …</div>';
  try {
    const r = await api('/api/eval/doc', { feature: name });
    body.innerHTML = renderDoc(r);
    if (!r.ok) { toast(r.error || '文档评估失败', 'danger'); }
    else {
      S.paths.doc = true;
      syncChecks();
      saveState();
    }
  } catch (e) {
    body.innerHTML = '<div class="alert danger">' + esc(e.message) + '</div>';
  } finally {
    card.classList.remove('running');
  }
}

function renderDoc(r) {
  const score = r.score || 0;
  const tone = score >= 75 ? 'g' : score >= 40 ? 'a' : 'r';
  let html = '';
  html += '<div class="mini-grid">';
  html += `<div class="mini"><div class="k">文档评分</div><div class="v ${tone}">${score}</div></div>`;
  html += `<div class="mini"><div class="k">相关文档</div><div class="v">${(r.matched_files || []).length}</div></div>`;
  html += `<div class="mini"><div class="k">文档库总量</div><div class="v">${r.total_docs || 0}</div></div>`;
  html += '</div>';
  html += '<div class="doc-checks">';
  (r.checks || []).forEach(c => {
    const cls = c.matched ? 'ok' : c.found ? 'mid' : 'no';
    const mark = c.matched ? '✓' : c.found ? '!' : '×';
    html += `<div class="doc-check ${cls}">
      <span class="mark">${mark}</span>
      <span class="nm">${esc(c.label)}</span>
      <span class="ct">${c.matched ? c.matched + ' 份匹配' : c.found + ' 份同类'}</span>
    </div>`;
  });
  html += '</div>';
  html += `<div class="alert ${r.matched_files && r.matched_files.length ? 'ok' : 'warn'}" style="margin-top:12px">${esc(r.advice)}</div>`;
  if (r.matched_files && r.matched_files.length) {
    html += '<div class="tags" style="margin-top:10px">' + r.matched_files.slice(0, 8)
      .map(f => `<span class="tag" title="${esc(f.path)}">${esc(f.name)}</span>`).join('') + '</div>';
  }
  return html;
}

/* ---------------- 步骤 1：代码评估 ---------------- */
/* 评估的「本地基准」：代码基线目录优先，其次移植目录，最后用 state 里记的 */
function evalLocalPath() {
  const b = ($('#basePath') || {}).value || '';
  const t = ($('#targetPath') || {}).value || '';
  const st = S.state || {};
  return (b || t || (st.baseline && st.baseline.path) ||
    (st.port && st.port.path) || '').trim();
}

async function runCodeEval() {
  if (runCodeEval.busy) return;
  const name = $('#featureName').value.trim();
  const topic = $('#evalTopic').value.trim();
  if (!$('#basePath').value) {
    toast('请先在「0. 代码基线」里选择代码目录', 'warn');
    return;
  }
  if (!S.base || !S.base.has_repo) {
    toast('当前目录不是 repo 工作区，请重新选择', 'warn');
    return;
  }
  const card = $('#cardCode');
  const body = $('#bodyCode');
  runCodeEval.busy = true;
  document.body.classList.add('eval-busy-active');
  $('#evalBusy').hidden = false;
  card.classList.add('running');
  body.innerHTML = '<div class="dim">正在读 topic 补丁、和本地工作区代码比对、解析 .config 与 .map …</div>';
  $('#codeResult').innerHTML = '';
  try {
    const r = await api('/api/eval/code', {
      feature: name, topic: topic, paths: [], path: evalLocalPath()
    });
    if (!r.ok) { toast(r.error || '代码评估失败', 'danger'); return; }
    body.innerHTML = renderCodeBrief(r);
    $('#codeResult').innerHTML = renderCodeFull(r);
    S.paths.code = true;
    syncChecks();
    saveState();
    if (r.errors && r.errors.length) {
      toast(r.errors[0], 'warn');
    }
  } catch (e) {
    body.innerHTML = '<div class="alert danger">' + esc(e.message) + '</div>';
  } finally {
    card.classList.remove('running');
    $('#evalBusy').hidden = true;
    document.body.classList.remove('eval-busy-active');
    runCodeEval.busy = false;
  }
}

function renderCodeBrief(r) {
  const m = r.metrics || {};
  const ef = r.effort || {};
  const hw = r.hardware || {};
  const miss = (r.missing && r.missing.review && r.missing.review.missing) || 0;
  const rvc = (r.missing && r.missing.review) || {};
  let html = '<div class="mini-grid">';
  html += `<div class="mini"><div class="k">Review change</div><div class="v">${rvc.changes || 0}</div></div>`;
  html += `<div class="mini"><div class="k">涉及模块</div><div class="v">${m.modules || 0}</div></div>`;
  html += `<div class="mini"><div class="k">本地缺失接口</div><div class="v ${miss ? 'r' : 'g'}">${miss}</div></div>`;
  html += `<div class="mini"><div class="k">难度</div><div class="v ${ef.tone || ''}">${esc(ef.level || '—')}</div></div>`;
  html += `<div class="mini"><div class="k">预估工期</div><div class="v g">${ef.days_low || 0}–${ef.days_high || 0}<span style="font-size:11px">天</span></div></div>`;
  html += `<div class="mini"><div class="k">硬件</div><div class="v ${hw.ok ? 'g' : 'r'}">${hw.ok ? '支持' : '待确认'}</div></div>`;
  html += '</div>';
  html += `<div class="alert ${r.score >= 70 ? 'ok' : r.score >= 45 ? 'warn' : 'danger'}" style="margin-top:12px">
    评估完成度 <b>${r.score}</b> / 100 · 耗时 ${r.elapsed_ms} ms</div>`;
  return html;
}

function renderCodeFull(r) {
  let html = '';

  /* ---- 1. 依赖缺失接口（topic 代码 ↔ 本地代码） ---- */
  const ms = r.missing || {};
  const rv = ms.review || {};
  const mItems = ms.items || [];
  const mHeads = ms.missing_headers || [];
  const mini = (k, v, tone) =>
    `<div class="mini"><div class="k">${esc(k)}</div><div class="v ${tone || ''}">${v}</div></div>`;

  html += sect('1', '本次功能依赖代码情况（topic 代码 ↔ 本地代码）',
    `<span class="badge ${(rv.missing || 0) > 0 ? 'danger' : ''}">`
    + `${(rv.missing || 0) > 0 ? '本地缺 ' + rv.missing + ' 个接口' : '本地无缺口'}</span>`,
    (() => {
      if (!ms.topic) {
        return '<div class="alert info">没填 gerrit topic，没法做 topic ↔ 本地对照。'
          + '填入 topic 链接后，这里只列「topic 用到、本地还没有」的接口；'
          + '改动文件和本地已有的接口都不再输出。</div>';
      }
      let b = '<div class="mini-grid">';
      b += mini('Review change', rv.changes || 0);
      b += mini('topic 修改文件', rv.files || 0);
      b += mini('比对外部符号', rv.symbols || 0);
      b += mini('本地已有（已跳过）', rv.local_hit || 0, 'dim');
      b += mini('本地缺失', rv.missing || 0, (rv.missing || 0) ? 'r' : 'g');
      b += mini('缺失头文件', rv.headers_missing || 0, (rv.headers_missing || 0) ? 'r' : 'g');
      b += '</div>';

      b += `<div class="alert info" style="margin-top:12px;display:block">`
        + `topic 侧只读取各 change 的 <b>diff 新增行</b>，未修改的旧代码不会参与接口分析；`
        + `随后在本地工作区搜索这些修改实际引用的接口。`
        + `</div>`;

      /* 比对基准：让人确认比的是本地这棵树，不是线上 */
      const baseRoot = ms.local_root || (ms.baseline && ms.baseline.root) || '';
      b += `<div class="alert ${r.local_root_fallback ? 'warn' : 'info'}" style="margin-top:12px;display:block">`
        + `<div><b>比对基准：本地工作区</b> <code>${esc(baseRoot || '—')}</code>`
        + (ms.repos_scanned ? `（${ms.repos_scanned} 个仓库）` : '')
        + `</div>`
        + (r.local_root_fallback
          ? `<div style="margin-top:6px;color:#ffcc80">注意：你选的目录里没有可用仓库，`
            + `这次退回到上面这个目录比对，结论不代表你选的那棵树。</div>` : '')
        + `<div style="margin-top:6px;font-size:11.8px" class="dim">`
        + `topic 一侧只读 <b>diff 新增行</b>；本地一侧只读上面这棵工作区的文件，`
        + `不查 origin/分支/任何远端版本。头文件里有定义或声明、或者别处调用过，都算「本地已有」。`
        + `</div></div>`;

      if (ms.note) b += `<div class="alert warn" style="margin-top:12px">${esc(ms.note)}</div>`;
      if (ms.failed && ms.failed.length) {
        b += '<table class="tbl" style="margin-top:10px"><thead><tr>'
          + '<th>读不到补丁的 change</th><th>仓库</th><th>原因</th></tr></thead><tbody>';
        ms.failed.slice(0, 8).forEach(f => {
          b += `<tr><td class="mono">#${esc(f.number)}</td>
            <td class="mono dim">${esc(f.repo || '—')}</td>
            <td class="mono dim">${esc(f.error || '')}</td></tr>`;
        });
        b += '</tbody></table>';
      }

      if (mHeads.length) {
        b += '<div style="margin-top:16px"><div class="dim" style="margin-bottom:8px">'
          + '① topic 里 include 了、本地找不到的头文件（要先合入，否则下面的接口也补不上）</div>';
        b += '<table class="tbl"><thead><tr><th>头文件</th><th>被谁 include</th></tr></thead><tbody>';
        mHeads.slice(0, 20).forEach(h => {
          b += `<tr><td class="mono t-strong">${esc(h.name)}</td>
            <td class="mono dim">${esc(h.used_in || '—')}</td></tr>`;
        });
        b += '</tbody></table>';
        if (mHeads.length > 20) {
          b += `<div class="dim" style="margin-top:8px">… 其余 ${mHeads.length - 20} 个略</div>`;
        }
        b += '</div>';
      }

      /* 判定为「本地已有」的抽样证据：可展开核对 */
      const hits = ms.hit_sample || [];
      if (hits.length) {
        const hd = rv.headers_hit || 0;
        b += `<details style="margin-top:14px"><summary class="dim" style="cursor:pointer">`
          + `本地已有接口的证据（抽样 ${hits.length} 条 / 共 ${rv.local_hit || 0} 条，`
          + `其中 ${hd} 条落在头文件里）</summary>`;
        b += '<table class="tbl" style="margin-top:10px"><thead><tr>'
          + '<th>接口</th><th>本地证据</th><th>形态</th></tr></thead><tbody>';
        hits.forEach(h => {
          b += `<tr><td class="mono">${esc(h.name)}</td>
            <td class="mono dim">${esc(h.file || '—')}${h.line ? ':' + h.line : ''}</td>
            <td><span class="tag muted">${esc(h.kind || '')}</span></td></tr>`;
        });
        b += '</tbody></table></details>';
      }

      if (!mItems.length) {
        b += '<div class="alert ok" style="margin-top:14px">topic 代码引用的外部接口，本地代码里全部已有 —— 不需要额外补依赖，直接移植即可。</div>';
        return b;
      }

      const kinds = {};
      mItems.forEach(i => { kinds[i.kind] = (kinds[i.kind] || 0) + 1; });
      const kindTxt = Object.keys(kinds).map(k => k + ' ' + kinds[k]).join(' · ');
      b += `<div style="margin-top:16px"><div class="dim" style="margin-bottom:8px">`
        + `② 本地没有的接口（${esc(kindTxt)}）—— 移植时必须一起带过来或自己实现</div>`;
      b += '<table class="tbl"><thead><tr>'
        + '<th>接口</th><th>类型</th><th>首次出现</th><th>模块</th><th>提示</th>'
        + '</tr></thead><tbody>';
      mItems.slice(0, 80).forEach(i => {
        b += `<tr>
          <td class="mono t-strong">${esc(i.name)}${i.kind === '函数' ? '()' : ''}</td>
          <td><span class="tag ${i.kind === '函数' ? '' : 'muted'}">${esc(i.kind)}</span></td>
          <td class="mono dim">${esc(i.used_in || '—')}${i.line ? ':' + i.line : ''}</td>
          <td class="mono dim">${esc(i.module || '—')}</td>
          <td class="dim" style="font-size:11.5px">${esc(i.hint || '本地头文件中无声明')}</td></tr>`;
      });
      b += '</tbody></table>';
      if (mItems.length > 80) {
        b += `<div class="dim" style="margin-top:8px">… 其余 ${mItems.length - 80} 个接口略</div>`;
      }
      b += `<div class="dim" style="margin-top:10px;font-size:11.5px">`
        + `判定方式：从 topic diff 新增行提取用到的函数 / 宏 / 类型 → `
        + `在本地工作区 <code>${esc(baseRoot || '')}</code> 里逐词查找（${esc(ms.how || 'git grep')}）。`
        + `头文件里有定义/声明、或任何源码里引用过，都算「本地已有」，不列出。</div>`;
      return b;
    })());

  /* ---- 2. 硬件支持 ---- */
  const hw = r.hardware || { groups: [], board: {}, key_items: [] };
  html += sect('2', '硬件是否支持',
    `<span class="badge ${hw.ok ? '' : 'danger'}">${hw.ok ? '能力齐备' : '存在缺口'}</span>`,
    (() => {
      let b = `<div class="alert ${hw.ok ? 'ok' : 'warn'}">${esc(hw.note || '')}</div>`;
      const board = hw.board || {};
      b += '<div class="mini-grid">';
      b += `<div class="mini"><div class="k">代码区上限</div><div class="v">${esc(board.code_cap || '—')}</div></div>`;
      b += `<div class="mini"><div class="k">PSRAM</div><div class="v">${esc(board.psram || '—')}</div></div>`;
      b += `<div class="mini"><div class="k">EXBIN 上限</div><div class="v">${esc(board.exbin_cap || '—')}</div></div>`;
      b += `<div class="mini"><div class="k">主堆</div><div class="v">${esc(board.main_heap || '—')}</div></div>`;
      b += '</div>';
      if (hw.groups && hw.groups.length) {
        b += '<table class="tbl" style="margin-top:14px"><thead><tr><th>能力</th><th>结论</th><th>命中的配置项</th></tr></thead><tbody>';
        hw.groups.forEach(g => {
          b += `<tr><td class="t-strong">${esc(g.label)}</td>
            <td><span class="tag ${g.supported ? 'g' : 'r'}">${g.supported ? '已启用' : '未启用'}</span></td>
            <td class="mono dim">${g.items.slice(0, 4).map(i => esc(i.key) + '=' + esc(i.value)).join('<br>')}</td></tr>`;
        });
        b += '</tbody></table>';
      }
      return b;
    })());

  /* ---- 3. 存储状态 ---- */
  const st = r.storage;
  html += sect('3', '项目存储状态（build/*.map + .config）',
    st && st.target ? `<span class="badge muted">${esc(st.target)}</span>` : '',
    (() => {
      if (!st) return '<div class="alert warn">未找到 .map 文件，先完成一次编译再评估。</div>';
      let b = '';
      (st.warnings || []).forEach(w => {
        b += `<div class="alert ${w.level === 'danger' ? 'danger' : w.level === 'warn' ? 'warn' : 'ok'}">${esc(w.text)}</div>`;
      });
      b += '<table class="tbl" style="margin-top:12px"><thead><tr>'
        + '<th>区域</th><th class="t-right">已用</th><th class="t-right">总量</th>'
        + '<th class="t-right">余量</th><th style="width:190px">占用</th></tr></thead><tbody>';
      (st.regions || []).forEach(g => {
        const cls = g.pct >= 90 ? 'danger' : g.pct >= 75 ? 'warn' : '';
        const note = g.note
          ? `<div class="dim" style="font-size:10.5px;font-weight:400">${esc(g.note)}</div>` : '';
        b += `<tr>
          <td class="t-strong mono">${esc(g.name)}${note}</td>
          <td class="mono t-right">${esc(g.used_h)}</td>
          <td class="mono t-right dim">${esc(g.total_h)}</td>
          <td class="mono t-right ${g.pct >= 90 ? '' : 'dim'}">${esc(g.free_h)}</td>
          <td><div class="bar ${cls}"><span style="width:${Math.min(g.pct, 100)}%"></span></div>
              <span class="mono dim" style="font-size:10.5px">${fmtPct(g.pct)}</span></td></tr>`;
      });
      b += '</tbody></table>';
      if (st.exbin) {
        b += `<div class="alert ${st.exbin.pct >= 95 ? 'danger' : st.exbin.pct >= 85 ? 'warn' : 'info'}" style="margin-top:12px">
          <b>EXBIN</b> ${esc(st.exbin.file)}：${esc(st.exbin.size_h)} / ${esc(st.exbin.limit_h)}
          （${fmtPct(st.exbin.pct)}）</div>`;
      }
      b += `<div class="dim" style="margin-top:10px;font-family:var(--mono);font-size:11.5px">
        map: ${esc(st.map)} · 解析耗时 ${st.parse_ms} ms · ${esc(st.at)}</div>`;
      return b;
    })());

  /* ---- 4. 工期预测 ---- */
  const ef = r.effort;
  html += sect('4', '预测移植时间（结合难度）',
    ef ? `<span class="badge ${ef.tone === 'danger' ? 'danger' : ef.tone === 'warn' ? 'warn' : ''}">${esc(ef.level)}</span>` : '',
    (() => {
      if (!ef) return '<div class="dim">—</div>';
      let b = '<div class="effort"><div class="effort-big">'
        + `<div class="num">${ef.days_low}–${ef.days_high}</div>`
        + '<div class="unit">人天（含自测联调）</div>'
        + `<div class="lv ${ef.tone}">难度 ${ef.difficulty} · ${esc(ef.level)}</div>`
        + '</div><div>';
      b += '<table class="tbl"><thead><tr><th>因子</th><th class="t-right">数量</th>'
        + '<th class="t-right">折算人天</th></tr></thead><tbody>';
      (ef.breakdown || []).forEach(x => {
        b += `<tr><td>${esc(x.label)}</td>
          <td class="mono t-right">${x.value} ${esc(x.unit)}</td>
          <td class="mono t-right t-strong">${x.weight}</td></tr>`;
      });
      b += `<tr><td class="t-strong">合计（基准）</td><td></td>
        <td class="mono t-right t-strong">${ef.days}</td></tr>`;
      b += '</tbody></table>';
      b += `<div class="dim" style="margin-top:10px">${esc(ef.assume)}</div>`;
      b += '</div></div>';
      return b;
    })());

  if (r.errors && r.errors.length) {
    html += `<div class="alert warn" style="margin-top:14px">评估告警：${r.errors.map(esc).join('；')}</div>`;
  }
  return html;
}

function sect(num, title, right, body) {
  return `<div class="sect">
    <div class="sect-head"><h3><span class="num">${num}</span>${esc(title)}</h3>
      <div>${right || ''}</div></div>
    <div class="sect-body">${body}</div>
  </div>`;
}

/* ---------------- 步骤 2：移植 ---------------- */
function refreshPortState() {
  const p = ($('#targetPath').value || '').trim();
  $('#pathBadge').textContent = p ? '当前目录' : '未选择';
  if (p) $('#pathNote').textContent = '代码将落在此工作区下的各 git 仓库；上次选择：' + p;
  // 评估阶段填的 topic 自动带到本步骤，不用再填一次
  const et = ($('#evalTopic').value || '').trim();
  if (et && !($('#gerritTopic').value || '').trim()) {
    $('#gerritTopic').value = et;
  }
  $('#topicSource').hidden = !($('#gerritTopic').value || '').trim();
  // 上次遗留了冲突未处理 -> 自动重新扫一遍现场
  if (S.conflictsOpen && $('#conflictCard').hidden) reanalyzeConflicts();
}

/* --- 目录选择弹窗 --- */
let dirState = { path: '', pick: '', target: 'port' };

async function openDirModal(start, target) {
  dirState.target = target || 'port';
  $('#dirModal').hidden = false;
  const cur = dirState.target === 'base' ? $('#basePath').value : $('#targetPath').value;
  await loadDir(start || cur || S.boot.env.workspace);
}

async function loadDir(path) {
  const list = $('#dirList');
  list.innerHTML = '<div class="dir-hint">读取中…</div>';
  let r;
  try {
    r = await api('/api/fs/list?path=' + encodeURIComponent(path || ''));
  } catch (e) {
    list.innerHTML = '<div class="dir-hint err">读不到这个目录：' + esc(e.message) +
      '<br>可能是路径不存在或没有权限。点上方「工作区」回到默认目录。</div>';
    return;
  }
  if (!r.ok) {
    list.innerHTML = '<div class="dir-hint err">读不到这个目录：' + esc(r.error || '未知错误') +
      '<br>当前尝试的路径：<code>' + esc(path || '') + '</code></div>';
    return;
  }
  dirState.path = r.path;
  dirState.pick = r.path;
  $('#dirJump').value = r.path;
  $('#dirPick').textContent = '已选：' + r.path;
  $('#dirShortcuts').innerHTML = (r.shortcuts || []).map(s =>
    `<button data-p="${esc(s.path)}">${esc(s.name)}</button>`).join('');
  $('#dirShortcuts').querySelectorAll('button').forEach(b => {
    b.onclick = () => loadDir(b.dataset.p);
  });
  const rows = [];
  if (r.parent && r.parent !== r.path) {
    rows.push(`<div class="dir-row" data-p="${esc(r.parent)}" data-up="1">
      <span class="ico">↑</span><span>返回上级</span></div>`);
  }
  (r.dirs || []).forEach(d => {
    const nm = esc(d.name);
    rows.push(`<div class="dir-row" data-p="${esc(d.path)}" title="${nm}">
      <span class="ico">▸</span><span class="nm">${nm}</span></div>`);
  });
  if (!(r.dirs || []).length) {
    rows.push('<div class="dir-hint">（这个目录下没有子目录，可以直接点「选定此目录」）</div>');
  }
  list.innerHTML = rows.join('');
  list.scrollTop = 0;
  list.querySelectorAll('.dir-row[data-p]').forEach(row => {
    // 单击即进入：浏览到哪一层，「选定此目录」就选哪一层，最不容易误解
    row.onclick = () => loadDir(row.dataset.p);
  });
  $('#dirUp').onclick = () => loadDir(r.parent || r.path);
}

/* --- gerrit 预检（只读） --- */
async function gerritPlan() {
  const url = $('#gerritTopic').value.trim();
  if (!url) { toast('先填 gerrit topic 链接', 'warn'); return; }
  const path = $('#targetPath').value.trim();
  if (!path) { toast('先选目标目录', 'warn'); return; }
  const card = $('#planCard');
  card.hidden = false;
  $('#planList').innerHTML = '<div class="dim">正在定位仓库、读各仓库当前分支与工作区状态…</div>';
  $('#planCount').textContent = '';
  try {
    const r = await api('/api/port/precheck', { topic: url, path: path });
    const p = r.precheck || {};
    if (!r.ok) {
      $('#planList').innerHTML = `<div class="alert danger">${esc(p.message || '预检失败')}</div>`;
      return;
    }
    const chs = p.changes || [];
    const blocked = p.blocked || 0;
    $('#planCount').textContent = chs.length + ' 个 change' + (blocked ? ` · ${blocked} 个会失败` : ' · 无阻塞');
    let html = '';
    if (blocked) {
      html += `<div class="alert danger">有 ${blocked} 个仓库当前状态会让 download 直接失败，
        先按「阻塞原因」处理再下载。</div>`;
    } else {
      html += '<div class="alert ok">预检通过：没有发现会让 download 失败的阻塞项。</div>';
    }
    html += '<table class="tbl"><thead><tr><th>#</th><th>主题</th><th>仓库</th>'
      + '<th>本地分支</th><th>结论</th></tr></thead><tbody>';
    chs.forEach(c => {
      const f = c.facts || {};
      const br = f.branch ? esc(f.branch) : (f.detached ? 'detached' : '—');
      let verdict;
      if ((c.blockers || []).length) {
        verdict = `<span style="color:#ffb4b6">会失败</span>`;
      } else if ((c.warnings || []).length) {
        verdict = `<span style="color:#ffd479">有风险</span>`;
      } else {
        verdict = `<span style="color:#7ee0a6">可以</span>`;
      }
      html += `<tr><td class="mono">${esc(c.number)}</td>
        <td>${esc(c.subject)}</td>
        <td class="mono dim">${esc(c.repo || ('未定位 ' + c.project))}</td>
        <td class="mono">${br}</td>
        <td>${verdict}</td></tr>`;
      const notes = []
        .concat((c.blockers || []).map(x => ({ t: x, cls: 'danger' })))
        .concat((c.warnings || []).map(x => ({ t: x, cls: 'warn' })))
        .concat(c.note ? [{ t: c.note, cls: 'info' }] : []);
      if (notes.length) {
        html += `<tr><td></td><td colspan="4">${notes.map(n =>
          `<div class="alert ${n.cls}" style="margin:4px 0;padding:6px 10px">${esc(n.t)}</div>`).join('')}</td></tr>`;
      }
    });
    html += '</tbody></table>';
    $('#planList').innerHTML = html;
  } catch (e) {
    $('#planList').innerHTML = `<div class="alert danger">${esc(e.message)}</div>`;
  }
}

/* --- 下载代码 --- */
async function gerritDownload() {
  const url = $('#gerritTopic').value.trim();
  if (!url) { toast('先填 gerrit topic 链接', 'warn'); return; }
  const path = $('#targetPath').value.trim();
  const busy = $('#downloadBusy');
  busy.hidden = false;
  const logEl = $('#dlLog');
  logEl.textContent = '';
  $('#conflictCard').hidden = true;
  $('#dlState').textContent = '执行中';
  $('#dlState').className = 'run-state run';

  try {
    const r = await api('/api/port/download', { topic: url, path: path });
    if (!r.ok) {
      logEl.textContent = '启动失败：' + (r.error || '');
      $('#dlState').textContent = '失败';
      $('#dlState').className = 'run-state fail';
      return;
    }
    const job = await followJob(r.job.id, logEl, $('#dlState'));
    const output = (job.lines || []).join('\n');
    await handlePortResult(output);
  } catch (e) {
    logEl.textContent += '\n' + e.message;
    $('#dlState').textContent = '失败';
    $('#dlState').className = 'run-state fail';
  } finally {
    busy.hidden = true;
  }
}

async function handlePortResult(output) {
  const r = await api('/api/port/result', { output: output });
  if (!r.ok && !r.payload) {
    $('#dlState').textContent = '需要人工确认';
    $('#dlState').className = 'run-state fail';
    if (r.raw_tail) {
      $('#dlLog').textContent += '\n\n—— 输出尾部 ——\n' + r.raw_tail;
    }
    return;
  }
  if (r.status === 'ok') {
    $('#dlState').textContent = '全部合入';
    $('#dlState').className = 'run-state ok';
    S.conflictsOpen = false;
    $('#conflictCard').hidden = true;
    updateNext();
    toast('代码已全部 cherry-pick 完成', 'ok');
    const st = await api('/api/state', {
      port: { done: true, conflicts: [] }
    });
    return;
  }
  if (r.status === 'partial') {
    $('#dlState').textContent = '部分完成';
    $('#dlState').className = 'run-state run';
    renderConflicts(r);
    toast('部分 change 已落地，剩下的看结果表', 'warn');
    return;
  }
  renderConflicts(r);
}

function renderConflicts(r) {
  const card = $('#conflictCard');
  card.hidden = false;
  // 只有工作区里真的还留着冲突标记才卡住下一步；已撤销 / 未执行的不锁流程
  S.conflictsOpen = (r.conflicts || []).length > 0;
  updateNext();

  let html = '';
  // ---- 失败原因：把每条 change 的 state/message 摊开 ----
  const chs = r.changes || [];
  if (chs.length) {
    html += '<div class="sect" style="margin-bottom:14px"><div class="sect-head">'
      + '<h3>每个 change 的处理结果</h3></div><div class="sect-body">'
      + '<table class="tbl"><thead><tr><th>#</th><th>主题</th><th>仓库</th>'
      + '<th>状态</th><th>原因</th></tr></thead><tbody>';
    const label = {
      applied: ['已合入', '#7ee0a6'],
      applied_auto: ['已合入（自动解冲突）', '#ffd479'],
      already_applied: ['已存在，跳过', '#7ee0a6'],
      conflict: ['冲突（现场保留）', '#ffb4b6'],
      conflict_aborted: ['冲突（已撤销，后续继续）', '#ffb4b6'],
      error: ['失败', '#ffb4b6'],
      pending: ['未执行', '#ffd479'],
    };
    chs.forEach(c => {
      const [txt, color] = label[c.state] || [c.state || '—', ''];
      html += `<tr><td class="mono">${esc(c.number)}</td>
        <td>${esc(c.subject || '')}</td>
        <td class="mono dim">${esc(c.repo || c.project || '')}</td>
        <td style="color:${color};white-space:nowrap">${esc(txt)}</td>
        <td style="font-size:12px">${esc(c.message || '')}</td></tr>`;
      if (c.auto_resolved && c.auto_resolved.length) {
        html += `<tr><td></td><td colspan="4" class="mono" style="font-size:11.8px;color:#ffd479">`
          + '自动解掉的文件：' + c.auto_resolved.map(esc).join(', ') + '</td></tr>';
      }
      if (c.files && c.files.length) {
        html += `<tr><td></td><td colspan="4" class="mono" style="font-size:11.8px;color:#ffb4b6">`
          + c.files.map(esc).join('<br>') + '</td></tr>';
      }
    });
    html += '</tbody></table>';
    if (r.status !== 'ok') {
      html += '<div class="alert info" style="margin-top:8px">按上面「原因」列逐条处理，'
        + '处理完再点「下载代码」——已经合入的 change 会自动跳过。'
        + '完整结果已存到 <code>data/last-download.json</code>。</div>';
    }
    html += '</div></div>';
  }

  if (r.status === 'error' && !chs.length) {
    html += '<div class="alert danger">下载过程出错：' +
      esc((r.payload && r.payload.message) || r.message || '') + '</div>';
  }
  const list = r.conflicts || [];
  if (!list.length && r.status === 'conflict') {
    html += '<div class="alert warn">检测到冲突但未解析到具体文件，请执行「重新分析工作区冲突」。</div>';
  }
  list.forEach(c => {
    html += `<div class="sect" style="margin-bottom:12px">
      <div class="sect-head"><h3>${esc(c.change)}</h3>
        <span class="badge danger">${c.files.length} 个冲突文件</span></div>
      <div class="sect-body">
        <div class="path-note" style="margin-top:0;margin-bottom:10px">仓库：<b>${esc(c.repo_rel || c.repo)}</b></div>`;
    c.files.forEach(f => {
      html += `<div style="margin-bottom:12px">
        <div class="mono" style="color:#ffb4b6">${esc(f.file)}</div>
        <div class="dim" style="font-size:11.8px">冲突块 ${f.regions} 处`;
      if (f.ours && f.ours.length) html += ` · 本分支侧：<span class="mono">${f.ours.slice(0, 4).map(esc).join(', ')}</span>`;
      if (f.theirs && f.theirs.length) html += ` · change 侧：<span class="mono">${f.theirs.slice(0, 4).map(esc).join(', ')}</span>`;
      html += '</div>';
      if (f.hotspots && f.hotspots.length) {
        html += '<div class="tags" style="margin-top:6px">' + f.hotspots.slice(0, 5)
          .map(h => `<span class="tag">${esc(h.file)}:${h.line}</span>`).join('') + '</div>';
      }
      html += '</div>';
    });
    html += '<div class="alert info" style="margin-top:6px;flex-direction:column;align-items:stretch">'
      + (c.advice || []).map(a => `<div>${esc(a)}</div>`).join('') + '</div>';
    html += '</div></div>';
  });
  if (list.length) {
    html += `<div class="alert info">把冲突上下文交给 CodeGraph / AI 继续分析：
    <code>data/conflict-report.md</code> 已生成，或在终端执行
    <code>codegraph</code> 查看调用关系后再改。</div>`;
  }
  $('#conflictBody').innerHTML = html;
}

async function reanalyzeConflicts() {
  const repo = $('#targetPath').value.trim() || S.boot.env.workspace;
  $('#conflictCard').hidden = false;
  $('#conflictBody').innerHTML = '<div class="dim">扫描未解决冲突…</div>';
  try {
    const r = await api('/api/port/conflict-analysis', { repo: repo });
    if (!r.ok) {
      $('#conflictBody').innerHTML = `<div class="alert danger">${esc(r.error)}</div>`;
      return;
    }
    if (!r.files || !r.files.length) {
      const n = (r.scanned || []).length;
      $('#conflictBody').innerHTML = '<div class="alert ok">扫描了 ' + n
        + ' 个 git 仓库，没有发现未解决的冲突，可以继续。</div>';
      S.conflictsOpen = false;
      updateNext();
      return;
    }
    renderConflicts({
      ok: true, status: 'conflict',
      conflicts: r.reports,
      payload: { message: '工作区当前未解决冲突' }
    });
  } catch (e) {
    $('#conflictBody').innerHTML = `<div class="alert danger">${esc(e.message)}</div>`;
  }
}

async function runToolCmd() {
  const cmd = $('#toolCmd').value.trim();
  if (!cmd) { toast('先输入要执行的命令', 'warn'); return; }
  const logEl = $('#toolLog');
  logEl.hidden = false;
  logEl.textContent = '';
  const cwd = $('#targetPath').value.trim() || S.boot.env.workspace;
  $('#btnToolRun').disabled = true;
  try {
    const r = await api('/api/tool/run', { cmd: cmd, cwd: cwd });
    if (!r.ok) {
      appendLog(logEl, ['$ ' + cmd, r.error || '执行失败']);
      return;
    }
    appendLog(logEl, ['$ ' + cmd + '   (cwd: ' + cwd + ')']);
    await followJob(r.job.id, logEl, null);
  } catch (e) {
    appendLog(logEl, [e.message]);
  } finally {
    $('#btnToolRun').disabled = false;
  }
}

/* ---------------- 任务日志跟随 ---------------- */
async function followJob(jobId, logEl, stateEl) {
  let cursor = 0;
  let idle = 0;
  while (true) {
    let r;
    try { r = await api('/api/job?id=' + jobId + '&since=' + cursor); }
    catch (e) { await sleep(1200); idle++; if (idle > 900) break; continue; }
    if (!r.ok) break;
    const j = r.job;
    if (j.lines && j.lines.length) {
      appendLog(logEl, j.lines);
    }
    cursor = j.cursor;
    if (j.status !== 'running') {
      if (stateEl) {
        stateEl.textContent = j.status === 'ok' ? '成功 (rc=0)' : '失败 (rc=' + j.rc + ')';
        stateEl.className = 'run-state ' + (j.status === 'ok' ? 'ok' : 'fail');
      }
      return j;
    }
    idle++;
    if (idle > 1500) {          // 约 20 分钟，防止异常时无限轮询
      if (stateEl) { stateEl.textContent = '超时未结束'; stateEl.className = 'run-state fail'; }
      break;
    }
    await sleep(idle > 60 ? 1600 : 800);
  }
  return { lines: [], status: 'unknown' };
}

function appendLog(el, lines) {
  if (el.textContent === '等待执行…') el.textContent = '';
  const frag = document.createDocumentFragment();
  lines.forEach(ln => {
    const span = document.createElement('span');
    let cls = '';
    if (/error|Error|ERROR|失败|fatal|denied|冲突|CONFLICT/.test(ln)) cls = 'err';
    else if (/success|Success|完成|OK\b|生成|finished/.test(ln)) cls = 'okl';
    if (cls) span.className = cls;
    span.textContent = ln + '\n';
    frag.appendChild(span);
  });
  el.appendChild(frag);
  el.scrollTop = el.scrollHeight;
}

/* ---------------- 步骤 3：编译 ---------------- */
async function startBuild() {
  const fam = $('#selFamily').value;
  const prod = $('#selProduct').value;
  if (!fam || !prod) { toast('先选择编译目标', 'warn'); return; }
  document.body.classList.remove('stage-success');
  $('#buildOkCard').hidden = true;
  const logEl = $('#buildLog');
  logEl.textContent = '';
  $('#buildState').textContent = '编译中';
  $('#buildState').className = 'run-state run';
  $('#btnBuild').disabled = true;

  try {
    const r = await api('/api/build/start', {
      family: fam, product: prod,
      threads: $('#inpThreads').value,
      mode: $('#selBuildMode').value || 'debug',
      extra: $('#inpExtra').value
    });
    if (!r.ok) {
      appendLog(logEl, ['启动失败：' + (r.error || '')]);
      $('#buildState').textContent = '失败';
      $('#buildState').className = 'run-state fail';
      $('#btnBuild').disabled = false;
      return;
    }
    appendLog(logEl, ['$ cd ' + r.cwd + '\n$ ' + r.cmd + '\n']);
    const job = await followJob(r.job.id, logEl, $('#buildState'));
    $('#btnBuild').disabled = false;
    const ok = job.status === 'ok';
    const res = await api('/api/build/result', { ok: ok });
    if (ok) {
      showBuildOk(res.artifacts || {});
      openFlashModal(res.artifacts || {});
    } else {
      toast('编译失败，看日志定位', 'danger');
    }
  } catch (e) {
    appendLog(logEl, [e.message]);
    $('#buildState').textContent = '失败';
    $('#buildState').className = 'run-state fail';
    $('#btnBuild').disabled = false;
  }
}

function showBuildOk(art) {
  document.body.classList.add('stage-success');
  const card = $('#buildOkCard');
  card.hidden = false;
  const info = [];
  if (art.target) info.push('target=' + art.target);
  if (art.mtime) info.push('产物时间 ' + art.mtime);
  if (art.package) info.push('包 ' + art.package.split('/').pop() + ' (' + art.package_size + ')');
  $('#buildOkInfo').textContent = info.join('  ·  ') || '固件已生成';
  $$('.step').forEach(b => {
    if (+b.dataset.step <= 3) b.classList.add('done');
  });
}

function openFlashModal(art) {
  $('#flashModal').hidden = false;
  const bits = [];
  if (art.target) bits.push('目标 ' + art.target);
  if (art.package) bits.push(art.package.split('/').pop());
  $('#flashModalInfo').innerHTML =
    '固件已就绪' + (bits.length ? '（' + esc(bits.join(' · ')) + '）' : '') +
    '。是否立即刷机？将调用 <code>v3dl app</code>。';
}

async function doFlash() {
  $('#flashModal').hidden = true;
  $('#flashCard').hidden = false;
  const logEl = $('#flashLog');
  logEl.textContent = '';
  $('#flashState').textContent = '烧录中';
  $('#flashState').className = 'run-state run';
  try {
    const r = await api('/api/flash/start', { mode: 'app' });
    if (!r.ok) {
      appendLog(logEl, [r.error || '启动失败']);
      $('#flashState').textContent = '无法开始';
      $('#flashState').className = 'run-state fail';
      if (r.need_sudo) {
        appendLog(logEl, ['\n提示：在启动本服务的 WSL 终端执行  sudo -v  之后再点刷机。']);
      }
      return;
    }
    $('#flashCmd').textContent = '$ ' + r.cmd;
    const job = await followJob(r.job.id, logEl, $('#flashState'));
    const ok = job.status === 'ok';
    await api('/api/flash/result', { ok: ok, mode: 'app' });
    if (ok) {
      toast('刷机完成，进入自测', 'ok');
      goStep(4, true);
    } else {
      toast('刷机失败，请检查设备连接', 'danger');
    }
  } catch (e) {
    appendLog(logEl, [e.message]);
    $('#flashState').textContent = '失败';
    $('#flashState').className = 'run-state fail';
  }
}

/* ---------------- 步骤 4：自测 ---------------- */
async function loadSelftest() {
  try {
    const r = await api('/api/selftest/items');
    if (!r.ok) return;
    S.selftest = r.items || [];
    renderProbe(r.probe || {});
    renderSelftest();
  } catch (e) { /* 忽略 */ }
}

function renderProbe(p) {
  const el = $('#probeBox');
  if (p.device) {
    el.className = 'probe ok';
    el.textContent = '设备已连接：' + (p.ports || []).join(', ');
  } else if (p.error) {
    el.className = 'probe fail';
    el.textContent = '探测失败：' + p.error;
  } else {
    el.className = 'probe fail';
    el.textContent = '未检测到 Watch-Link 串口（v3dl com 无输出）';
  }
  if (p.raw) el.textContent += '\n' + p.raw;
}

function renderSelftest() {
  const box = $('#selftestList');
  box.innerHTML = S.selftest.map((it, i) => `
    <div class="check-item">
      <div class="ci-txt"><b>${esc(it.label)}</b><i>${esc(it.hint || '')}</i></div>
      <div class="seg" data-i="${i}">
        <button data-v="ok"   class="${it.state === 'ok' ? 'on-ok' : ''}">OK</button>
        <button data-v="ng"   class="${it.state === 'ng' ? 'on-ng' : ''}">NG</button>
        <button data-v="skip" class="${it.state === 'skip' ? 'on-skip' : ''}">跳过</button>
      </div>
    </div>`).join('');

  box.querySelectorAll('.seg button').forEach(btn => {
    btn.onclick = async () => {
      const i = +btn.parentElement.dataset.i;
      const v = btn.dataset.v;
      S.selftest[i].state = (S.selftest[i].state === v) ? 'pending' : v;
      renderSelftest();
      await saveSelftest();
    };
  });

  const done = S.selftest.filter(x => x.state === 'ok').length;
  const ng = S.selftest.filter(x => x.state === 'ng').length;
  $('#stState').textContent = `OK ${done} / ${S.selftest.length}` + (ng ? ` · NG ${ng}` : '');
  $('#stState').className = 'run-state ' + (ng ? 'fail' : done === S.selftest.length ? 'ok' : '');
}

async function saveSelftest() {
  const items = S.selftest.map(x => ({ id: x.id, state: x.state, note: '' }));
  const r = await api('/api/selftest/save', { items: items });
  const card = $('#finalCard');
  if (r.done) {
    card.hidden = false;
    card.classList.remove('bad');
    $('#finalTitle').textContent = '自测全部通过';
    $('#finalText').textContent = '本次功能移植闭环完成，可以提交评审 / 送测。';
  } else {
    const ng = S.selftest.filter(x => x.state === 'ng').length;
    if (ng) {
      card.hidden = false;
      card.classList.add('bad');
      $('#finalTitle').textContent = '存在未通过项（' + ng + ' 项 NG）';
      $('#finalText').textContent = '请在下方记录问题现象，修复后重新编译刷机再验证。';
    } else {
      card.hidden = true;
    }
  }
}

/* ---------------- 事件绑定 ---------------- */
function bind() {
  $$('.step').forEach(b => {
    b.onclick = () => {
      const n = +b.dataset.step;
      if (n > 1 && !(S.paths.doc && S.paths.code)) {
        toast('完成两项评估后才能解锁后续步骤', 'warn');
        return;
      }
      goStep(n);
    };
  });

  $('#btnNext').onclick = () => {
    if (S.step < 4) goStep(S.step + 1);
  };

  // 代码基线
  $('#btnBasePick').onclick = () => openDirModal(null, 'base');
  $('#basePath').onkeydown = (e) => {
    if (e.key !== 'Enter') return;
    const p = (e.target.value || '').trim();
    if (!p) return;
    e.target.blur();
    switchWorkspace(p);
  };
  $('#btnBaseRefresh').onclick = () => loadBaseline(true);
  $('#btnBaseCodegraph').onclick = refreshBaseCodegraph;
  $('#baseXml').onchange = onBaseXmlChange;
  $('#btnBaseCheckout').onclick = doCheckout;

  // 步骤 1
  $('#btnDoc').onclick = runDocEval;
  $('#btnCode').onclick = runCodeEval;
  $('#chkDoc').onchange = (e) => {
    S.paths.doc = e.target.checked; syncChecks(); saveState();
  };
  $('#chkCode').onchange = (e) => {
    S.paths.code = e.target.checked; syncChecks(); saveState();
  };
  $('#featureName').onchange = saveState;

  // 步骤 2
  $('#btnBrowse').onclick = () => openDirModal(null, 'port');
  $('#targetPath').onchange = () => { refreshPortState(); saveState(); };
  $('#dirJump').onkeydown = (e) => {
    if (e.key !== 'Enter') return;
    const p = (e.target.value || '').trim();
    if (!p) return;
    e.target.blur();
    loadDir(p);
  };
  $('#dirClose').onclick = $('#dirCancel').onclick = () => { $('#dirModal').hidden = true; };
  $('#dirOk').onclick = () => {
    const picked = dirState.pick;
    $('#dirModal').hidden = true;
    if (!picked) { toast('还没选目录', 'warn'); return; }
    if (dirState.target === 'base') {
      switchWorkspace(picked);
    } else {
      $('#targetPath').value = picked;
      refreshPortState();
      saveState();
      toast('代码目录已设为 ' + picked, 'ok');
    }
  };
  $('#btnPlan').onclick = gerritPlan;
  $('#btnDownload').onclick = gerritDownload;
  $('#btnReanalyze').onclick = reanalyzeConflicts;
  $('#btnToolRun').onclick = runToolCmd;
  $('#toolCmd').onkeydown = (e) => { if (e.key === 'Enter') runToolCmd(); };

  // 步骤 3
  $('#selFamily').onchange = () => { fillProducts(); updateBuildNote(); saveState(); };
  $('#selProduct').onchange = () => { updateBuildNote(); saveState(); };
  $('#inpExtra').oninput = updateBuildNote;
  $('#btnBuild').onclick = startBuild;
  $('#selBuildMode').onchange = updateBuildNote;
  $('#btnGoFlash').onclick = () => openFlashModal({});
  $('#flashConfirm').onclick = doFlash;
  $('#flashCancel').onclick = () => { $('#flashModal').hidden = true; };

  // 步骤 4
  $('#btnProbe').onclick = loadSelftest;
  $('#stNote').onchange = saveSelftest;

  // 全局
  $('#btnReset').onclick = async () => {
    if (!confirm('重置整个流程状态？评估勾选与步骤进度都会清空。')) return;
    await api('/api/reset', {});
    try { localStorage.removeItem(LS_KEY); } catch (e) { }
    location.reload();
  };

  // 所有输入框：边打字边存（localStorage 立即 + 服务端 700ms 防抖），
  // 目录/topic 不会因为「没点保存」就丢了
  FORM_FIELDS.forEach(id => {
    const el = $('#' + id);
    if (!el) return;
    el.addEventListener('input', saveSoon);
    el.addEventListener('change', saveSoon);
  });
}

/* ---------------- 启动 ---------------- */
document.addEventListener('DOMContentLoaded', () => {
  bind();
  boot();
});
