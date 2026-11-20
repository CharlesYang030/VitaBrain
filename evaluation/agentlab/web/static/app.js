// FEATURIZE_REMOTE_BANNER_V1: deployment identity, served only by Featurize.
(() => {
  const mount = () => {
    if (document.getElementById('featurize-remote-banner')) return;
    const style = document.createElement('style');
    style.textContent = `
      #featurize-remote-banner {position:sticky;top:0;z-index:1000;box-sizing:border-box;
        width:100%;display:flex;align-items:center;justify-content:center;flex-wrap:wrap;
        gap:4px 18px;padding:9px 16px;background:#fbbf24;color:#241900;
        border-bottom:2px solid #b77900;font:13px/1.5 system-ui,"PingFang SC",sans-serif;}
      #featurize-remote-banner strong {font-weight:800;letter-spacing:.3px;}
      #featurize-remote-banner span {overflow-wrap:anywhere;min-width:0;}
      body.featurize-remote > header {top:var(--featurize-banner-height,42px);}
      body.featurize-remote > .drawer {top:var(--featurize-banner-height,42px);}
      @media(min-width:701px) {
        body.featurize-remote .sidebar {top:calc(76px + var(--featurize-banner-height,42px));
          height:calc(100vh - 76px - var(--featurize-banner-height,42px));}
      }
      @media(min-width:851px) {
        body.featurize-remote .cal-library {top:calc(85px + var(--featurize-banner-height,42px));
          max-height:calc(100vh - 100px - var(--featurize-banner-height,42px));}
      }
      @media(max-width:600px) {
        #featurize-remote-banner {justify-content:flex-start;padding:8px 12px;font-size:12px;}
      }
    `;
    document.head.append(style);
    const banner = document.createElement('div');
    banner.id = 'featurize-remote-banner';
    banner.setAttribute('role', 'note');
    banner.setAttribute('aria-label', 'Featurize 远端服务器连接标识');
    const label = document.createElement('strong');
    label.textContent = 'FEATURIZE 服务器 · SSH 转发';
    const mapping = document.createElement('span');
    mapping.textContent = `本机 ${location.host} → Featurize :8852 · 智能体实验`;
    banner.append(label, mapping);
    document.body.classList.add('featurize-remote');
    document.body.prepend(banner);
    const resize = () => document.documentElement.style.setProperty(
      '--featurize-banner-height', `${banner.getBoundingClientRect().height}px`);
    resize();
    new ResizeObserver(resize).observe(banner);
    document.title = '[Featurize 远端] ' + document.title;
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount, {once:true});
  else mount();
})();
// END_FEATURIZE_REMOTE_BANNER_V1
const $ = id => document.getElementById(id);
// The running service may still have the old template cached. Move the existing
// controls, preserving their IDs and listeners, without restarting the service.
if (!$('replayControlsPanel')) {
  const viewportPanel = document.querySelector('.viewport-panel');
  const replayBar = document.querySelector('.replay-bar');
  if (viewportPanel && replayBar) {
    const card = document.createElement('article');
    card.id = 'replayControlsPanel';
    card.className = 'panel replay-controls-panel';
    card.setAttribute('aria-label', '回放与运行控制');
    viewportPanel.before(card);
    for (const node of [replayBar, $('replayLoadState'), $('stepBtn')?.closest('.actions'), $('autoError'), $('leakResult')]) {
      if (node) card.append(node);
    }
  }
}
const pretty = value => JSON.stringify(value, null, 2);
let backends = [], providers = [], scenarios = [];
let taskCatalog = {};
let runId = null, lastSeq = 0, polling = null, stepGroups = {}, stepInFlight = false;

// Per-step data reconstructed from audit events, for replay / IO cards.
// stepData[step] = {fpv, observer, context, modelResponded, parsed, executed, subtasks}
let stepData = {}, maxSeenStep = 0, followLive = true;
let latestRun = null, milestoneLog = [], lastSubtaskStatus = {};
let assistBySubtask = {};   // subtask_id -> earliest assist step (特权交接归因)
let modelAttemptEvents = new Map(); // seq -> event; retries can share an environment step
let loadingRunId = null;
let historyMetadata = {}, historyMetadataLoaded = false;
let attachmentVersion = 0;
const terminalStatuses = new Set(['completed', 'stopped', 'boot_failed']);

function stopPolling() { clearInterval(polling); polling = null; }
function startPolling() {
  if (polling === null) polling = setInterval(refreshAll, 2500);
}

async function api(path, options = {}) {
  const response = await fetch(path, {headers: {'Content-Type': 'application/json'}, ...options});
  let data = {};
  try { data = await response.json(); } catch (e) { /* non-json error body */ }
  if (!response.ok || data.ok === false) throw new Error(data.message || `HTTP ${response.status}`);
  return data;
}

let toastTimer = null;
function toast(message) {
  $('toast').textContent = message;
  $('toast').classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $('toast').classList.add('hidden'), 8000);
}
function note(message) { $('actionMessage').textContent = message; }
function escapeHtml(text) { const el = document.createElement('div'); el.textContent = text ?? ''; return el.innerHTML; }

// ---------------------------------------------------------------- drawer
function openDrawer(title, contentNode) {
  $('drawerTitle').textContent = title;
  const body = $('drawerBody');
  body.innerHTML = '';
  body.append(contentNode);
  $('drawer').classList.remove('hidden');
}
$('drawerClose').addEventListener('click', () => $('drawer').classList.add('hidden'));
$('drawerCopy').addEventListener('click', async () => {
  const text = $('drawerBody').innerText;
  try { await navigator.clipboard.writeText(text); toast('已复制到剪贴板'); }
  catch (e) { toast('复制失败：' + e.message); }
});
document.addEventListener('keydown', e => { if (e.key === 'Escape') $('drawer').classList.add('hidden'); });

function textDrawer(title, text) {
  const pre = document.createElement('pre');
  pre.className = 'code tall';
  pre.textContent = text;
  openDrawer(title, pre);
}

// ---- 可读的键值树渲染：折叠嵌套、直排多行文本、图像出缩略图 ----------
function imageThumb(src) {
  const img = document.createElement('img');
  img.loading = 'lazy';
  img.src = src;
  img.className = 'thumb';
  img.title = '点击放大';
  img.addEventListener('click', event => {
    event.preventDefault(); event.stopPropagation();
    const big = document.createElement('img');
    big.src = src; big.style.width = '100%';
    openDrawer('图像', big);
  });
  return img;
}

function valueNode(value, depth) {
  if (value === null || value === undefined) return textSpan('null', 'jt-null');
  if (typeof value === 'boolean' || typeof value === 'number') {
    return textSpan(String(value), 'jt-num');
  }
  if (typeof value === 'string') {
    if (value.startsWith('data:image/') || /^\/api\/runs\/[^/]+\/replay-images\//.test(value)) return imageThumb(value);
    // 嵌套 JSON 字符串（如上下文里的 body 文本）尝试解析成子树
    if (value.length > 2 && '[{'.includes(value[0])) {
      try { return jsonTree(JSON.parse(value), depth + 1); } catch (e) { /* 纯文本 */ }
    }
    if (value.length > 300) {
      const wrap = document.createElement('span');
      const pre = document.createElement('pre');
      pre.className = 'jt-str';
      pre.textContent = value.slice(0, 300) + ` …（共 ${value.length} 字符）`;
      const more = document.createElement('button');
      more.className = 'mini'; more.textContent = '全文';
      const full = value;
      more.addEventListener('click', event => {
        event.preventDefault(); event.stopPropagation();
        textDrawer('文本全文', full);
      });
      wrap.append(pre, more);
      return wrap;
    }
    if (value.includes('\n') || value.length > 80) {
      const pre = document.createElement('pre');
      pre.className = 'jt-str';
      pre.textContent = value;
      return pre;
    }
    return textSpan(value, 'jt-text');
  }
  return jsonTree(value, depth + 1);
}

function textSpan(text, cls) {
  const el = document.createElement('span');
  el.className = cls;
  el.textContent = text;
  return el;
}

function jsonTree(value, depth = 0) {
  const isArray = Array.isArray(value);
  const entries = isArray
    ? value.map((item, index) => [String(index), item])
    : Object.entries(value || {});
  const box = document.createElement('div');
  box.className = 'jt';
  for (const [key, item] of entries) {
    const isNested = item && typeof item === 'object'
      && (Array.isArray(item) ? item.length : Object.keys(item).length);
    if (isNested) {
      const details = document.createElement('details');
      details.className = 'jt-branch';
      if (depth < 1) details.open = true;
      const summary = document.createElement('summary');
      summary.innerHTML = `<span class="jt-key">${escapeHtml(key)}</span>
        <span class="jt-hint">${Array.isArray(item) ? `[${item.length}]` : `{${Object.keys(item).length}}`}</span>`;
      details.append(summary);
      let populated = false;
      const populate = () => {
        if (!populated && details.open) {
          populated = true;
          details.append(jsonTree(item, depth + 1));
        }
      };
      details.addEventListener('toggle', populate);
      populate();
      box.append(details);
    } else {
      const row = document.createElement('div');
      row.className = 'jt-row';
      const label = document.createElement('span');
      label.className = 'jt-key';
      label.textContent = key;
      row.append(label, valueNode(item, depth));
      box.append(row);
    }
  }
  if (!entries.length) box.append(textSpan(isArray ? '[]' : '{}', 'jt-null'));
  return box;
}

function smartNode(value) {
  const wrap = document.createElement('div');
  wrap.className = 'jt-wrap';
  wrap.append(jsonTree(value));
  const bar = document.createElement('div');
  bar.className = 'code-bar';
  const full = document.createElement('button');
  full.className = 'mini'; full.textContent = '原始 JSON';
  full.addEventListener('click', () => textDrawer('完整 JSON', pretty(value)));
  const copy = document.createElement('button');
  copy.className = 'mini'; copy.textContent = '复制';
  copy.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(pretty(value)); toast('已复制'); } catch (e) { toast(e.message); }
  });
  bar.append(full, copy);
  wrap.append(bar);
  return wrap;
}

// ------------------------------------------------------------------ init
async function initialize() {
  // New-run form default only; an attached run keeps its frozen config.
  $('contextPolicy').value = 'enhanced_v1';
  $('navigationAssist').value = 'recall';
  try {
    const [health, backendData, providerData] = await Promise.all([
      api('/api/health'), api('/api/backends'), api('/api/providers')]);
    $('healthDot').classList.add('ok');
    $('healthText').textContent = 'agentlab ready';
    backends = backendData.backends;
    $('backend').innerHTML = backends.map(b =>
      `<option value="${b.name}" ${b.default ? 'selected' : ''}>${b.title}</option>`).join('');
    if ($('backend').value !== 'development_inspector') { $('navigationAssist').value='off'; $('fogGtMap').value='off'; $('depthImage').value='off'; }
    providers = providerData.providers;
    $('provider').innerHTML = providers.map(p =>
      `<option value="${p.name}">${p.name} · ${p.model}</option>`).join('');
    // New runs follow the user's accepted STB_002 configuration (2026-09-17).
    if (backends.some(b => b.name === 'development_inspector')) {
      $('backend').value = 'development_inspector';
    }
    // New-run model requested on 2026-09-20; archived run configs stay frozen.
    if (providers.some(p => p.name === 'mentor_codex_astra')) {
      $('provider').value = 'mentor_codex_astra';
    } else if (providers.some(p => p.name === 'gptge_fable5')) {
      $('provider').value = 'gptge_fable5';
    } else if (providers.some(p => p.name === 'gptge_astra')) {
      $('provider').value = 'gptge_astra';
    }
    $('runMode').value = 'adaptive_probe';
    $('agentMode').value = 'arm';
    $('apcAuto').value = 'off';
    $('gtNavigation').value = $('backend').value === 'development_inspector' ? 'on' : 'off';
    $('fogGtMap').value = $('backend').value === 'development_inspector' ? 'on' : 'off';
    $('navigationAssist').value = $('backend').value === 'development_inspector' ? 'recall' : 'off';
    renderBackendMeta();
    renderProvider();
    taskCatalog = await api("/static/task_catalog.json");
    await loadScenarios();
    await refreshRuns();
    try {
      const savedId = sessionStorage.getItem('agentlab-selected-run');
      if (savedId && document.querySelector(`.run-row[data-run="${savedId}"]`)) attachRun(savedId);
    } catch (error) { /* Browsing history remains usable without storage. */ }
  } catch (error) { $('healthText').textContent = error.message; toast(`初始化失败：${error.message}`); }
}

function currentBackend() { return backends.find(b => b.name === $('backend').value); }

function renderBackendMeta() {
  const b = currentBackend();
  if (!b) return;
  $('backendMeta').textContent =
    `backend=${b.name} · 水印=${b.watermark} · 正式SR资格=${b.formal_sr_eligible ? 'YES' : 'NO'} · ${b.reason}`;
}

async function loadScenarios() {
  const backend = $('backend').value;
  $('scenario').innerHTML = '<option>载入场景目录…</option>';
  $('scenarioMeta').textContent = '正在从环境侧读取场景目录（Inspector 目录可能需要数秒）…';
  try {
    const data = await api(`/api/scenarios?backend=${encodeURIComponent(backend)}`);
    scenarios = data.scenarios;
    if (!scenarios.length) throw new Error('该 backend 未返回任何场景');
    $('scenario').innerHTML = scenarios.map(s =>
      `<option value="${s.scenario_id}">${s.scenario_id} · ${escapeHtml(s.title || '')}</option>`).join('');
    const preferred = scenarios.find(s => s.scenario_id === 'STB_002');
    if (preferred) $('scenario').value = 'STB_002';
    renderScenarioMeta();
  } catch (error) {
    $('scenario').innerHTML = '<option value="">（不可用）</option>';
    $('scenarioMeta').textContent = `场景目录获取失败：${error.message}`;
  }
}

function renderScenarioMeta() {
  const s = scenarios.find(item => item.scenario_id === $('scenario').value);
  if (!s) { $('scenarioMeta').textContent = ''; return; }
  $('scenarioMeta').textContent =
    `channel=${s.channel} · readiness=${s.readiness ?? 'n/a'} · digest=${s.digest}`;
}

function renderProvider() {
  const p = providers.find(item => item.name === $('provider').value);
  if (!p) return;
  $('providerMeta').textContent =
    `${p.protocol} · ${p.base_url || 'local'} · model=${p.model} · secret_env=${p.secret_env || 'none'}` +
    ` · ${p.configured ? 'configured ✓' : 'NOT CONFIGURED（请在服务端导出环境变量）'} · omit=[${p.omit_parameters.join(', ')}]`;
}

// ------------------------------------------------------------------ run
async function createRun(isRetry) {
  try {
    note('正在创建运行（配置将被冻结写入 manifest）…');
    $('createBtn').disabled = true;
    const data = await api('/api/runs', {method: 'POST', body: JSON.stringify({
      environment_backend: $('backend').value,
      scenario_id: $('scenario').value,
      provider: $('provider').value,
      interface_mode: $('interfaceMode').value,
      run_mode: $('runMode').value,
      seed: Number($('seed').value),
      house_index: $('houseIndex').value === '' ? null : Number($('houseIndex').value),
      agent_mode: $('agentMode').value || null,
      apc_auto: $('apcAuto').value !== 'off',
      spatial_memory_enabled: $('spatialMemory').value === 'on',
      fog_gt_enabled: $('fogGtMap').value === 'on',
      depth_image_enabled: $('depthImage').value === 'on',
      visual_frame_count: Number($('visualFrames').value),
      memory_compaction_enabled: $('memoryCompaction').value === 'on',
      context_policy: $('contextPolicy').value,
      navigation_enabled: $('navigationAssist').value === 'on',
      gt_navigation_enabled: $('gtNavigation').value === 'on',
      path_recall_enabled: $('navigationAssist').value === 'recall',
      reasoning_effort: $('reasoningEffort').value || null,
      focused_timeout_seconds: Number($('timeout').value),
    })});
    loadingRunId = data.run.run_id;
    attachRun(data.run.run_id);
    note(`已创建 ${data.run.run_id}，环境加载中…`);
    renderRun(data.run);
    await refreshRuns();
  } catch (error) {
    // Single-session guard: automatically terminate the blocking run and
    // retry once — clicking 创建 already expresses intent to take over.
    const blocked = /单会话环境.*(run_[0-9a-f]+)/.exec(error.message);
    if (blocked && !isRetry) {
      try {
        note(`8851 被 ${blocked[1]} 占用，正在自动终止并重建…`);
        await api(`/api/runs/${blocked[1]}/stop`,
          {method: 'POST', body: JSON.stringify({terminate: true})});
        toast(`已自动终止占用的 ${blocked[1]}（其审计文件保留），正在重新创建`);
        $('createBtn').disabled = false;
        return createRun(true);
      } catch (stopError) {
        note(stopError.message);
        toast(`自动接管失败：${stopError.message}`);
      }
    } else {
      note(error.message);
      toast(`创建运行失败：${error.message}`);
    }
  }
  finally { $('createBtn').disabled = false; }
}

function attachRun(id) {
  attachmentVersion++;
  safetyReceipts.clear();
  try { sessionStorage.setItem('agentlab-selected-run', id); } catch (error) { /* optional */ }
  runId = id; lastSeq = 0; stepGroups = {};
  stepData = {}; maxSeenStep = 0; followLive = true;
  milestoneLog = []; lastSubtaskStatus = {}; assistBySubtask = {};
  modelAttemptEvents = new Map();
  $('timeline').innerHTML = '';
  $('decision').innerHTML = '<div class="empty">暂无模型输出</div>';
  $('milestones').innerHTML = '<div class="empty">子任务状态变化会记录在这里</div>';
  $('leakResult').classList.add('hidden');
  $('replaySlider').value = 0; $('replaySlider').max = 0; $('replaySlider').disabled = true;
  setLive(true);
  $('leakScanBtn').disabled = false;
  $('exportBtn').classList.remove('disabled');
  $('exportBtn').href = `/api/runs/${id}/export`;
  stopPolling();
  startPolling();
  refreshAll();
}

let refreshInFlight = false;
async function refreshAll() {
  if (!runId || refreshInFlight || document.hidden) return;
  refreshInFlight = true;
  const requestedRunId = runId;
  const generation = attachmentVersion;
  try {
    const run = await api(`/api/runs/${requestedRunId}?view=replay`);
    if (requestedRunId !== runId || generation !== attachmentVersion) return;
    latestRun = run;
    renderRun(run);
    await refreshEvents(requestedRunId, generation);
    if (requestedRunId !== runId || generation !== attachmentVersion) return;
    // Drain the final audit first. Completion must not discard the last
    // action, model explanation or observation. Replay remains entirely local.
    if (terminalStatuses.has(run.status) && !run.auto_running && !run.model_waiting) {
      stopPolling();
      $('replayLoadState').textContent = '历史已载入 · 自动刷新已停止 · 拖动滑条回顾画面与决策';
    }
  } catch (error) { note(error.message); }
  finally { refreshInFlight = false; }
}

async function refreshEvents(requestedRunId = runId, generation = attachmentVersion) {
  if (!runId) return;
  let more = true;
  while (more) {
    const data = await api(`/api/runs/${requestedRunId}/events?after=${lastSeq}&view=replay`);
    if (requestedRunId !== runId || generation !== attachmentVersion) return;
    $('hashState').textContent = data.hash_chain_valid ? 'Hash chain ✓' : `Hash chain ✕ ${data.hash_chain_problem || ''}`;
    const before = lastSeq;
    for (const event of data.events) { lastSeq = Math.max(lastSeq, event.seq); ingestEvent(event, false); appendEvent(event); }
    if (followLive) $('timeline').scrollTop = $('timeline').scrollHeight;
    more = data.has_more === true;
    if (more && before === lastSeq) throw new Error('历史分页未推进，请重新加载记录');
    $('replayLoadState').textContent = more ? `正在载入历史 · 已读取 ${lastSeq} 条审计记录…` : '历史已同步 · 可拖动滑条回顾';
    if (more) await new Promise(resolve => setTimeout(resolve, 0));
  }
  if (latestRun) renderSafetyPanel(latestRun);
  if (latestRun) renderStepStats(latestRun.step_stats || []);
  if (followLive) renderSelectedStep(maxSeenStep);
}

// -------------------------------------------------------- event ingestion
function stepSlot(step) {
  if (!(step in stepData)) stepData[step] = {};
  if (step > maxSeenStep) { maxSeenStep = step; onMaxStepGrew(); }
  return stepData[step];
}

function ingestEvent(event, renderNow = true) {
  if (event.stage === 'ACTION_EXECUTED') {
    const p = event.payload || {};
    if (p.safety_event || p.safety_warning) safetyReceipts.set(event.seq, {
      seq:event.seq, step:event.step + 1, time:event.timestamp,
      action:p.action?.verb, damage:p.safety_event, warning:p.safety_warning});
  }
  if (event.seq === 1) {
    const meta = historyMetadata[event.run_id] ||= {};
    meta.started_at ||= event.timestamp;
    const row = document.querySelector(`.run-row[data-run="${event.run_id}"] .history-time`);
    if (row) row.textContent = `启动记录（北京时间）：${beijingTime(meta.started_at)}`;
  }
  if (['API_REQUESTED', 'MODEL_RESPONDED', 'MODEL_OUTPUT_REPAIRED', 'ACTION_VALIDATED'].includes(event.stage)) {
    modelAttemptEvents.set(event.seq, event);
  }
  const step = event.step ?? 0;
  const payload = event.payload || {};
  const slot = stepSlot(step);
  if (event.stage === 'OBSERVED' && event.visibility === 'agent_visible') {
    if (payload.navigation_substep != null) {
      (slot.navigationObservations ||= []).push(payload);
    } else {
      slot.fpv = (payload.rgb || {}).data_url || slot.fpv;
      slot.observation = payload;
    }
    // New runs keep task truth only in evaluator-private observations.
    if (payload.subtask_progress?.length) trackMilestones(step, payload.subtask_progress);
  } else if (event.stage === 'OBSERVED' && event.visibility === 'evaluator_private' && payload.observer_views) {
    slot.observer = payload.observer_views;
    trackMilestones(step, slot.observer.subtask_progress_full || []);
  } else if (event.stage === 'API_REQUESTED') {
    slot.apiRequested = true;
  } else if (event.stage.startsWith('MEMORY_COMPA')) {
    (slot.compactionEvents ||= []).push(event);
  } else if (event.stage === 'CONTEXT_BUILT') {
    slot.context = payload;
    slot.spatial_memory = payload.spatial_memory;
  } else if (event.stage === 'MODEL_RESPONDED') {
    slot.modelResponded = payload;
  } else if (event.stage === 'CONTROLLER_DECIDED') {
    slot.controller = payload;
  } else if (event.stage === 'RESPONSE_PARSED') {
    slot.parsed = payload;
  } else if (event.stage === 'ACTION_EXECUTED') {
    slot.executed = payload;
    (slot.executions ||= []).push(payload);
  } else if (event.stage === 'PATH_RECALL_COMPLETED' && event.visibility === 'agent_visible') {
    slot.recallSummary = payload;
    slot.executed = payload;
  } else if (event.stage.startsWith('GT_NAVIGATION_')) {
    (slot.gtNavigationEvents ||= []).push(event);
  } else if (event.stage === 'NAVIGATION_COMPLETED') {
    slot.navigationSummary = payload;
  } else if (event.stage === 'ASSIST_TRIGGERED' && payload.subtask_id) {
    const id = payload.subtask_id;
    if (!(id in assistBySubtask) || step < assistBySubtask[id]) {
      assistBySubtask[id] = step;
    }
  }
  if (followLive && renderNow) renderSelectedStep(maxSeenStep);
}

function assistTag(subtaskId, atStep) {
  const assistStep = assistBySubtask[subtaskId];
  if (assistStep !== undefined && (atStep === undefined || assistStep <= atStep)) {
    return `<span class="assist-tag" title="步 ${assistStep} 触发特权交接（APC/手动）——该环节不算模型自解">⚡ 特权协助</span>`;
  }
  return '';
}

function trackMilestones(step, rows) {
  for (const row of rows) {
    if (row.masked) continue;
    const prev = lastSubtaskStatus[row.id];
    if (prev !== undefined && prev !== row.status) {
      milestoneLog.push({step, id: row.id, description: row.description, from: prev, to: row.status});
    } else if (prev === undefined && row.status !== 'unready') {
      milestoneLog.push({step, id: row.id, description: row.description, from: '（首次观测）', to: row.status, initial: true});
    }
    lastSubtaskStatus[row.id] = row.status;
  }
  if (milestoneLog.length) {
    $('milestones').innerHTML = milestoneLog.map(m => {
      const tag = m.to === 'done'
        ? (m.initial ? '<span class="self-tag">初始／首次观测已满足</span>' : (assistTag(m.id, m.step) || '<span class="self-tag">已完成</span>'))
        : '';
      return `
      <div class="milestone"><span class="chip">步 ${m.step}</span>
        <b>${escapeHtml(m.description || m.id)}</b> <span class="arrow">${escapeHtml(taskStatusLabel(m.from))} → ${escapeHtml(taskStatusLabel(m.to))}</span> ${tag}</div>`;
    }).join('');
  }
}

// -------------------------------------------------------------- replay
function onMaxStepGrew() {
  const slider = $('replaySlider');
  slider.max = maxSeenStep;
  slider.disabled = maxSeenStep === 0;
  if (followLive) slider.value = maxSeenStep;
}

function setLive(on) {
  followLive = on;
  $('liveBtn').classList.toggle('active', on);
  $('replayBadge').classList.toggle('hidden', on);
  if (on) { $('replaySlider').value = maxSeenStep; renderSelectedStep(maxSeenStep); }
}

$('replaySlider').addEventListener('input', () => {
  const step = Number($('replaySlider').value);
  setLiveSilently(step >= maxSeenStep);
  renderSelectedStep(step);
});
function setLiveSilently(on) {
  followLive = on;
  $('liveBtn').classList.toggle('active', on);
  $('replayBadge').classList.toggle('hidden', on);
}
$('liveBtn').addEventListener('click', () => setLive(true));

function renderSelectedStep(step) {
  const slot = stepData[step] || {};
  $('replayLabel').textContent = maxSeenStep ? `步 ${step} / ${maxSeenStep}` : '—';
  $('replayStepNo').textContent = step;
  renderViewport(slot);
  // The latest observation may not have a model call yet (it becomes the
  // NEXT step's input) — fall back to the most recent step with model I/O.
  let ioStep = step;
  while (ioStep > 0 && !(stepData[ioStep] || {}).context
         && !(stepData[ioStep] || {}).modelResponded) ioStep--;
  const ioSlot = stepData[ioStep] || {};
  $('ioStepBadge').textContent = ioStep === step
    ? `步 ${step}` : `步 ${ioStep}（最近一次模型调用）`;
  renderIO(ioSlot, ioStep);
  if (slot.parsed) renderDecision(slot.parsed, slot.modelResponded, step);
  if (slot.observation && !followLive) {
    renderTask(slot.observation, (slot.observer || {}).subtask_progress_full);
  }
}

// -------------------------------------------------------------- viewport
// FPV (model-facing) and the two observer views (TPV/BEV) render side by
// side and update together on every step / replay position.
function renderImageGallery(id, cards) {
  const box = $(id);
  while (box.children.length > cards.length) box.lastElementChild.remove();
  cards.forEach((card, index) => {
    let figure = box.children[index];
    if (!figure) {
      figure = document.createElement('figure'); figure.className = 'image-card';
      const caption = document.createElement('figcaption');
      const img = document.createElement('img'); img.loading = 'lazy'; img.draggable = false;
      img.addEventListener('click', () => { const big = document.createElement('img'); big.src = img.src;
        big.style.maxWidth = '100%'; openDrawer(caption.textContent, big); });
      const details = document.createElement('details'); const summary = document.createElement('summary');
      summary.textContent = '关联文字说明'; const pre = document.createElement('pre'); pre.className = 'image-caption-text';
      details.append(summary, pre); figure.append(caption, img, details); box.append(figure);
    }
    const [caption, img, details] = figure.children;
    caption.textContent = card.title; img.alt = card.title;
    if (img.getAttribute('src') !== card.url) img.src = card.url;
    if (card.imageId) img.id = card.imageId; else img.removeAttribute('id');
    figure.dataset.viewId = card.viewId || ''; figure.dataset.floor = card.floor ?? '';
    details.classList.toggle('hidden', !card.text);
    details.lastElementChild.textContent = card.text || '';
  });
}

function renderModelImages(slot) {
  const obs = slot.observation || {}, ctx = slot.context;
  const exact = ctx && (!obs.observation_id || ctx.observation_id === obs.observation_id);
  const cards = []; let history = 0, current = 0, maps = 0, depths = 0;
  if (exact) {
    for (const msg of ctx.messages || []) {
      let caption = '';
      for (const part of Array.isArray(msg.content) ? msg.content : []) {
        if (part.type === 'text') caption = part.text || '';
        if (part.type !== 'image_url') continue;
        const first = caption.split('\n')[0];
        const historical = first.startsWith('HISTORICAL_FPV');
        const map = first.startsWith('EXPLORED_GT_MAP') || first.startsWith('MAP_IMAGE');
        const depth = first.startsWith('CURRENT_FRONT_DEPTH');
        if (historical) history++; else if (map) maps++; else if (depth) depths++; else current++;
        cards.push({title: `${cards.length + 1}. ${historical ? '历史正视图' : map ? '模型地图' : depth ? '正视米制深度' : '当前视图'} · ${first || '旧版原始输入'}`,
          url: part.image_url?.url || '', text: caption});
      }
    }
    const v = ctx.visual_input;
    if (v) { maps = v.map_image_count || 0; history = v.history_fpv_count ?? v.history?.length ?? 0;
      current = v.current_fpv_count ?? (v.actual_fpv_count || 0) - history; }
  } else {
    for (const frame of slot.history_frames || []) {
      const {rgb, ...caption} = frame; history++;
      cards.push({title: `${cards.length+1}. 历史正视图 · ${frame.observation_id}`, url:rgb, text:pretty(caption)});
    }
    const views = obs.current_views?.views || (slot.fpv ? [{view_id:'front',label:'正视图（旧版）',data_url:slot.fpv}] : []);
    for (const view of views) {
      const {data_url, ...caption} = view; current++;
      cards.push({title: `${cards.length+1}. 当前 ${view.label} · ${obs.observation_id || ''}`,
        url:data_url, viewId:view.view_id, text:pretty({...caption, observation_id:obs.observation_id})});
    }
    if (obs.front_depth) {
      const {data_url, ...caption} = obs.front_depth; depths++;
      cards.push({title:`${cards.length+1}. 当前正视深度 · 固定0–6m`, url:data_url, text:pretty(caption)});
    }
    for (const im of slot.spatial_memory?.images || (slot.spatial_memory?.image ? [slot.spatial_memory.image] : [])) {
      maps++; cards.push({title:`${cards.length+1}. 模型地图 · F${im.floor ?? '?'} ${im.current ? '当前层' : '历史层'}`,url:im.data_url});
    }
  }
  let selectionImages=0;
  for (const event of slot.gtNavigationEvents || []) {
    if (event.stage!=='GT_NAVIGATION_INPUT') continue;
    let viewIndex=0;
    for (const message of event.payload.messages || []) for (const part of Array.isArray(message.content)?message.content:[]) {
      if(part.type==='image_url') {selectionImages++;cards.push({title:`独立选点窗口 ${selectionImages} · ${event.payload.views?.[viewIndex++]?.view_id || 'front（旧版）'} · GT绿色可通行区域`,url:part.image_url.url,text:'此图单独发送给选点响应，不混入主决策；选点输出和路径见I/O。'});}
    }
  }
  renderImageGallery('modelImageGallery', cards);
  $('modelImageSummary').textContent = `历史正 ${history} ＋ 当前 ${current} ＋ 深度 ${depths} ＋ 地图 ${maps}${selectionImages ? ` ＋ 独立选点 ${selectionImages}` : ''} ＝ ${cards.length} 张`;
  $('modelImageStatus').textContent = exact
    ? `${slot.apiRequested ? '已发出模型请求' : '已构建上下文'} · ${ctx.observation_id || ''}；以下图片及相邻文字直接来自审计中的 messages。`
    : `当前观察的输入预览 · ${obs.observation_id || '等待观察'}；尚未构建本步模型请求，历史不足时不补图。`;
}

function renderViewport(slot) {
  const spatial = slot.spatial_memory?.image ? slot.spatial_memory : null;
  $('spatialMapPanel').classList.toggle('hidden', !spatial);
  if (spatial) {
    const images = spatial.images || [spatial.image];
    renderImageGallery('spatialMapImages', images.map((im,i) => ({title:`F${im.floor ?? '?'} · ${im.current ? '当前层' : '已访问历史层'} · 模型可见`,
      url:im.data_url,floor:im.floor,imageId:i===0?'spatialMapImage':undefined})));
    const floorBox = $('spatialMapFloors');
    const wanted = floorBox.dataset.run === String(runId) ? Number(floorBox.dataset.floor) : NaN;
    const chosen = images.find(im => im.floor === wanted) || images[0];
    floorBox.dataset.run = String(runId); floorBox.replaceChildren();
    images.forEach(im => {
      if (im.floor === undefined) return;
      const button = document.createElement('button'); button.className='mini';
      button.textContent=`F${im.floor} 节点说明`;
      button.onclick=()=>{floorBox.dataset.floor=im.floor;renderMapNodes(spatial.state,im.floor);};
      floorBox.append(button);
    });
    renderMapNodes(spatial.state,chosen.floor);
    $('spatialMapLegend').textContent = spatial.version === 'explored_gt_bev_v5'
      ? '三视图探索式 GT 辅助：左 −90°、正 0°、右 +90° 各自按深度遮挡检查，再取可见表面并集；最远4m，不是整片270°揭开。黑色未知，原色当前可见，暗色上次所见。固定画布，红色方位/朝向/路线，各楼层独立。'
      : spatial.version === 'explored_gt_bev_v4'
      ? '旧版单正视图探索式 GT：当前正视图可见表面揭雾，最远4m；黑色未知、暗色历史，各楼层独立。'
      : spatial.version === 'explored_gt_bev_v3'
      ? '旧版固定画布 GT：4m / 360°结构遮挡揭雾；黑色未知、暗色历史。红色方位/朝向/路线。'
      : ['explored_gt_fog_v1','explored_gt_fog_v2'].includes(spatial.version)
      ? '旧版结构 GT：4m / 360°结构遮挡揭雾；各层独立。'
      : 'RGB 估计地图：由正视图及公开动作形成，存在感知误差。';
    $('spatialMapState').textContent=pretty(spatial.state);
  } else {
    renderImageGallery('spatialMapImages',[]); $('spatialMapFloors').replaceChildren();
    $('spatialMapState').textContent=''; $('spatialMapNodes').replaceChildren();
  }
  const observer=slot.observer || {}, obs=slot.observation || {};
  const views=obs.current_views?.views || [];
  for (const [key,id,cell,empty] of [['left','fpvLeft','leftViewCell','leftViewEmpty'],['right','fpvRight','rightViewCell','rightViewEmpty']]) {
    const view=views.find(v=>v.view_id===key); $(cell).classList.toggle('hidden',!view);
    if (view) {if ($(id).getAttribute('src')!==view.data_url) $(id).src=view.data_url;}
    else $(id).removeAttribute('src');
    $(empty).classList.toggle('hidden',Boolean(view));
  }
  document.querySelector('.multiview').classList.toggle('single-view',views.length!==3);
  const fpv=views.find(v=>v.view_id==='front')?.data_url || slot.fpv;
  if (fpv) {if ($('fpv').getAttribute('src')!==fpv) $('fpv').src=fpv;} else $('fpv').removeAttribute('src');
  const tpvUrl=observer.tpv?.data_url;
  if (tpvUrl) {if ($('tpv').getAttribute('src')!==tpvUrl) $('tpv').src=tpvUrl;} else $('tpv').removeAttribute('src');
  $('tpvEmpty').classList.toggle('hidden',Boolean(tpvUrl));
  const bevs=observer.bev || [];
  renderImageGallery('observerBevGallery',bevs.map((im,i)=>({title:`F${(im.floor ?? i)+1} · 完整 Inspector BEV · 仅观察者`,
    floor:(im.floor ?? i)+1,url:im.data_url,imageId:i===0?'bev':undefined})));
  $('bevEmpty').classList.toggle('hidden',bevs.length>0);
  renderModelImages(slot);
}

// ------------------------------------------------------------ IO cards
function renderIO(slot, step) {
  const input = $('ioInput');
  const output = $('ioOutput');
  if (!slot.context) {
    input.innerHTML = '<div class="empty">该步尚未构建模型上下文</div>';
  } else {
    input.innerHTML = '';
    const ctx = slot.context;
    const head = document.createElement('div');
    head.className = 'io-meta';
    head.innerHTML = `字符 <b>${ctx.character_count ?? '—'}</b> · 记忆事实 <b>${ctx.memory_fact_count ?? 0}</b>
      · obs <b>${escapeHtml(ctx.observation_id || '')}</b><br>
      prompt sha256 <code class="sha" title="点击复制">${escapeHtml((ctx.prompt_sha256 || '').slice(0, 16))}…</code>`;
    if (ctx.text_projection) {
      const p = ctx.text_projection, b = ctx.input_budget_breakdown || {};
      head.insertAdjacentHTML('beforeend', `<br>框架无损编码节省 <b>${p.saved_text_tokens || 0}</b> 文本 tokens · 图片 <b>${b.image_count ?? '—'}</b> 张 · 完整输入估算 <b>${b.estimated_total_tokens ?? '—'}</b> tokens（非API账单）`);
    }
    head.querySelector('.sha').addEventListener('click', async () => {
      try { await navigator.clipboard.writeText(ctx.prompt_sha256); toast('sha256 已复制'); } catch (e) {}
    });
    input.append(head);
    for (const message of ctx.messages || []) {
      const section = document.createElement('details');
      section.className = 'io-msg';
      if (message.role !== 'system') section.open = true;
      const summary = document.createElement('summary');
      summary.innerHTML = `<span class="role role-${message.role}">${message.role}</span>`;
      section.append(summary);
      const parts = Array.isArray(message.content) ? message.content : [{type: 'text', text: String(message.content)}];
      for (const part of parts) {
        if (part.type === 'image_url') {
          const img = document.createElement('img');
          img.src = (part.image_url || {}).url || '';
          img.className = 'thumb io-img';
          img.title = '模型收到的原始画面（点击放大）';
          img.addEventListener('click', () => {
            const big = document.createElement('img');
            big.src = img.src; big.style.width = '100%';
            openDrawer(`步 ${step} · 模型输入画面`, big);
          });
          section.append(img);
        } else {
          const pre = document.createElement('pre');
          pre.className = 'code prose';
          const text = part.text || '';
          pre.textContent = text.length > 3000 ? text.slice(0, 3000) + `\n…（共 ${text.length} 字符）` : text;
          section.append(pre);
          if (text.length > 3000) {
            const btn = document.createElement('button');
            btn.className = 'mini';
            btn.textContent = '查看全文';
            btn.addEventListener('click', () => textDrawer(`步 ${step} · ${message.role} 消息全文`, text));
            section.append(btn);
          }
        }
      }
      input.append(section);
    }
  }
  for (const event of slot.gtNavigationEvents || []) {
    const section=document.createElement('details');section.open=true;
    const title=document.createElement('summary');
    title.textContent=({GT_NAVIGATION_PREPARED:'通道生成诊断 · 选点前检查',GT_NAVIGATION_INPUT:'独立导航窗口 · 模型实际输入',GT_NAVIGATION_MODEL_OUTPUT:'独立导航窗口 · 模型选点响应',GT_NAVIGATION_OUTPUT_RETRY:'选点输出截断 · 保留原输入重试',GT_NAVIGATION_PLANNED:'目标向下箭头与规划路径'})[event.stage] + (event.visibility==='server_private'?'（仅研究者可见）':'');
    section.append(title);
    const p=event.payload;
    for (const value of [p.image,...(p.views || []).map(v=>v.image),p.target_image,p.bev_image]) {
      if (value?.data_url) {const im=document.createElement('img');im.src=value.data_url;im.style.maxWidth='100%';section.append(im);}
    }
    const pre=document.createElement('pre');pre.className='code prose';
    if(p.messages) pre.textContent=p.messages.map(m=>typeof m.content==='string'?m.content:m.content.filter(c=>c.type==='text').map(c=>c.text).join('\n')).join('\n');
    else pre.textContent=pretty(Object.fromEntries(Object.entries(p).filter(([k])=>!['image','target_image','bev_image'].includes(k))));
    section.append(pre);input.append(section);
  }
  for (const event of slot.compactionEvents || []) {
    const detail=document.createElement('details');
    const title=document.createElement('summary');title.textContent=`模型记忆压缩 · ${event.stage}`;
    const pre=document.createElement('pre');pre.className='code prose';pre.textContent=pretty(event.payload);
    detail.append(title,pre);input.append(detail);
  }
  if (!slot.modelResponded && !slot.parsed && !slot.controller) {
    output.innerHTML = '<div class="empty">该步尚无模型输出</div>';
  } else {
    output.innerHTML = '';
    if (slot.controller) {
      const meta = document.createElement('div');
      meta.className = 'io-meta';
      meta.textContent = ['front_rgb_navigation_v2','visible_gt_waypoint_v1','visible_gt_waypoint_v2_triplet'].includes(slot.controller.version)
        ? `本次模型导航决策 · 已执行 ${slot.executions?.length || 0} 个内部动作 · 共计1决策步`
        : '旧版导航控制器：原始动作逐步计数。';
      output.append(meta);
    }
    if (slot.modelResponded) {
      const meta = document.createElement('div');
      meta.className = 'io-meta';
      const usage = slot.modelResponded.usage || {};
      meta.innerHTML = `延迟 <b>${((slot.modelResponded.latency_ms || 0) / 1000).toFixed(1)}s</b>
        · tokens 入 <b>${usage.prompt_tokens ?? '—'}</b> / 出 <b>${usage.completion_tokens ?? '—'}</b>`;
      output.append(meta);
      const pre = document.createElement('pre');
      pre.className = 'code prose';
      pre.textContent = slot.modelResponded.content || '';
      output.append(pre);
    }
    if (slot.executions?.some(p => p.navigation_substep != null) || slot.navigationSummary) {
      const details = document.createElement('details');
      const title = document.createElement('summary');
      title.textContent = `导航执行明细（${slot.executions?.length || 0} 个动作）· ${slot.navigationSummary?.feedback_code || '执行中'}`;
      details.append(title);
      for (const receipt of slot.executions || []) {
        const pre = document.createElement('pre'); pre.className = 'code';
        pre.textContent = JSON.stringify(receipt, null, 2); details.append(pre);
        const obs = (slot.navigationObservations || []).find(o => o.navigation_substep === receipt.navigation_substep);
        if (obs?.rgb?.data_url) { const img=document.createElement('img'); img.src=obs.rgb.data_url; img.className='thumb io-img'; details.append(img); }
      }
      if (slot.navigationSummary) { const pre=document.createElement('pre'); pre.className='code'; pre.textContent=JSON.stringify(slot.navigationSummary,null,2); details.append(pre); }
      output.append(details);
    }
    if (slot.recallSummary) {
      const details=document.createElement('details');
      const title=document.createElement('summary');
      title.textContent=`已探索路径回溯 · ${slot.recallSummary.node?.id || ''} · ${slot.recallSummary.success ? '成功' : '拒绝'} · 1个决策`;
      const pre=document.createElement('pre'); pre.className='code';
      pre.textContent=JSON.stringify(slot.recallSummary,null,2);details.append(title,pre);output.append(details);
    }
    if (slot.parsed) {
      const chip = document.createElement('div');
      chip.className = 'io-meta';
      const act = slot.parsed.action || (slot.parsed.decision_type === 'navigate' ? {verb:'navigate', parameters:slot.parsed.navigation_request} : slot.parsed.decision_type === 'return_to_visited' ? {verb:'return_to_visited',parameters:slot.parsed.return_request} : slot.parsed.decision_type === 'gt_navigate' ? {verb:'gt_navigate',parameters:slot.parsed.gt_navigation_request} : {});
      chip.innerHTML = `解析动作 <b>${escapeHtml(act.verb || '')}</b> ${escapeHtml(JSON.stringify(act.parameters || {}))}`;
      output.append(chip);
    }
  }
}

// ------------------------------------------------------------- rendering
function renderRun(run) {
  renderSafetyPanel(run);
  const diagnosticBox = $("behaviorWarnings");
  if (diagnosticBox) {
    const diagnostics = run.behavior_diagnostics || [];
    diagnosticBox.textContent = diagnostics.map(d => d.message + " " + d.suggestion).join("\n");
    diagnosticBox.classList.toggle("hidden", diagnostics.length === 0);
  }
  const config = run.config || {};
  const profile = run.environment_profile || {};
  const wm = $('watermark');
  wm.textContent = profile.watermark || 'UNKNOWN';
  wm.className = 'watermark' + (profile.formal_sr_eligible ? ' formal' : '');

  const autoPaused = run.status === 'running' && !run.auto_running && run.auto_stop_reason;
  const statusText = autoPaused ? '自动运行已暂停' : run.status;
  $('runStatus').textContent = statusText;
  const autoError = $('autoError');
  if (autoError) {
    autoError.classList.toggle('hidden', !run.auto_error);
    autoError.textContent = run.auto_error
      ? `完成 ${run.step} 步后暂停：${run.auto_error.type} — ${run.auto_error.message}。现场已保留，可点击“单步”或“自动运行”重试。`
      : '';
  }
  $('runStep').textContent = run.step;
  $('srEligible').textContent = run.formal_sr_eligible ? 'YES' : 'NO';
  $('assistCount').textContent = run.assist_count;
  const summary = run.stats_summary || {};
  const mc = run.memory_compaction || {};
  if ($('compactionStatus')) {
    const b = mc.budget;
    $('compactionStatus').textContent = mc.enabled
      ? `模型记忆压缩：${mc.status}${mc.status === 'over_budget' ? '（超预算，未丢弃新增记录）' : ''} · 摘要第${mc.revision || 0}版 · 调用${summary.compaction_model_calls || 0}次 · ${summary.compaction_latency_seconds || 0}s` + (b ? ` · 触发/目标/超额提示 ${b.trigger}/${b.target}/${b.hard} · 本步估算 ${mc.estimated_input_tokens ?? '—'}` : '')
      : '模型记忆压缩：关闭（历史运行保持原配置）';
    $('compactionDetail').textContent = JSON.stringify(mc, null, 2);
  }
  $('tokenTotal').textContent = summary.total_prompt_tokens != null
    ? `${summary.total_prompt_tokens} / ${summary.total_completion_tokens}` : '—';
  $('latencyAvg').textContent = summary.avg_latency_ms != null
    ? `${(summary.avg_latency_ms / 1000).toFixed(1)}s` : '—';
  $('runMeta').textContent =
    `${run.run_id} · ${config.environment_backend} · ${config.scenario_id} · seed=${config.seed}` +
    ` · ${config.provider_name}/${config.provider_model} · ${config.interface_mode} · ${config.run_mode}` +
    ` · 深度=${config.depth_image_enabled ? 'RGB-D 开' : '关'} · BEV=${config.fog_gt_enabled ? 'GT 揭雾 4m（特权）' : (config.spatial_memory_enabled ? 'RGB 估计' : '关')} · FPV=${config.current_view_protocol === 'left_front_right_v1' ? `当前3＋历史正最多${(config.visual_frame_count || 1)-1}` : (config.visual_frame_count || 1)}` +
    ` · GT通道点选=${config.gt_navigation_enabled ? '开（两阶段+A*）' : '关'} · 上下文=${config.context_policy || 'legacy'} · 移动辅助=${config.path_recall_enabled ? '已探索路径回溯' : config.navigation_enabled ? '旧版点选导航' : '关'}` +
    ` · digest=${config.scenario_digest}`;
  $('modeBadge').textContent = `${config.interface_mode || ''} · ${statusText}`;

  const overlay = $('viewportOverlay');
  if (run.status === 'loading' || run.status === 'created') {
    loadingRunId = run.run_id;
    overlay.classList.remove('hidden');
    const boot = run.boot || {};
    $('overlayText').textContent = `环境加载中（${boot.phase || 'starting'}）— Unity 场景绑定可能需要数分钟，请勿关闭`;
    $('bootLog').textContent = (boot.log || []).join('\n');
    $('bootLog').scrollTop = $('bootLog').scrollHeight;
  } else if (run.status === 'boot_failed') {
    if (loadingRunId === run.run_id) {
      note(`环境加载失败：${(run.boot || {}).error || '未知错误'}`);
      loadingRunId = null;
    }
    overlay.classList.remove('hidden');
    $('overlayText').textContent = `环境加载失败：${(run.boot || {}).error || '未知错误'}`;
    toast(`环境加载失败：${(run.boot || {}).error || ''}`);
  } else {
    overlay.classList.add('hidden');
    if (loadingRunId === run.run_id) {
      if (run.status === 'running') note('场景已就绪，点击“单步”或“自动运行”开始。');
      loadingRunId = null;
    }
  }
  $('modelWaiting').classList.toggle('hidden', !(run.model_waiting || stepInFlight));
  const waiting = $('modelWaiting');
  let waitingText = waiting.querySelector('.request-status');
  if (!waitingText) {
    [...waiting.childNodes].filter(n => n.nodeType === Node.TEXT_NODE).forEach(n => n.remove());
    waitingText = document.createElement('span'); waitingText.className = 'request-status'; waiting.append(waitingText);
  }
  const retry = run.provider_retry;
  if (retry) {
    const seconds = Math.max(0, Math.ceil(retry.retry_at_unix - Date.now()/1000));
    waitingText.textContent = `API ${retry.http_status || retry.error_type || '连接错误'} · 重试 ${retry.retry}/${retry.max_retries} · ${seconds > 0 ? `${seconds} 秒后重试` : '重试请求中…'}`;
  } else waitingText.textContent = '模型思考中…';

  const observation = run.observation || {};
  $('observation').textContent = pretty(observation);
  if (followLive) {
    const rgb = observation.rgb || {};
    const slot = {...stepSlot(run.step)}; // Live execution view must not overwrite the decision input stored for replay.
    if (rgb.data_url) slot.fpv = rgb.data_url;
    slot.observation = observation; slot.history_frames = run.history_frames || [];
    slot.observer = run.observer_views || {};
    slot.spatial_memory = run.spatial_memory;
    renderViewport(slot);
    renderBboxes(observation, config.interface_mode);
    renderTask(observation, (run.observer_views || {}).subtask_progress_full);
  }
  renderInteractables(observation, config.interface_mode, run.observer_views);
  renderStepStats(run.step_stats || []);

  $('focus').textContent = (run.apc || {}).focused_subtask || '—';
  const apc = run.apc || {active_seconds: 0, timeout_seconds: 300};
  $('apcText').textContent = `${(apc.active_seconds || 0).toFixed(1)} / ${apc.timeout_seconds} s active wall time`;
  $('apcBar').style.width = `${Math.min(100, (apc.active_seconds || 0) / apc.timeout_seconds * 100)}%`;
  $('episodeClock').textContent = `${(run.episode_active_seconds || 0).toFixed(1)} s`;
  const isAdaptive = (config.run_mode === 'adaptive_probe');
  $('apcAutoState').textContent = isAdaptive
    ? (run.apc_auto ? 'APC：超时自动交接（开）' : 'APC：仅计时，交接靠人工「立即交接」（关）')
    : 'Strict：不交接，APC 仅作诊断显示';

  renderMemory(run.memory);
  renderCapabilities(run.capabilities, run.tainted_capabilities || []);
  const historyBadge = document.querySelector(`.history-result[data-result="${run.run_id}"]`);
  if (historyBadge) setHistoryResult(historyBadge, run);

  const running = run.status === 'running';
  $('stepBtn').disabled = !running || stepInFlight || !!run.auto_running || !!run.checkpoint_reconciliation_required || !!run.maintenance_hold;
  $('autoBtn').disabled = !running || !!run.auto_running || stepInFlight || !!run.checkpoint_reconciliation_required || !!run.maintenance_hold;
  $('stopBtn').disabled = !running;
  if ($('checkpointBtn')) $('checkpointBtn').disabled = !running || !!run.auto_running || !!run.model_waiting || stepInFlight || !run.checkpoint || (!!run.checkpoint_reconciliation_required && !run.checkpoint_save_pending);
  if ($('checkpointStatus')) $('checkpointStatus').textContent = run.checkpoint_reconciliation_required
    ? '最后动作/检查点待核对：禁止盲目续跑'
    : run.checkpoint?.error ? `保存失败：${run.checkpoint.error}`
    : run.checkpoint ? `已保存第 ${run.checkpoint.step} 步 · 保留 Unity 才能恢复`
    : '本轮未启用检查点（旧服务仍保留现场）';
  $('terminateBtn').disabled = !(running || run.status === 'loading');
  $('handoffBtn').disabled = !(running && config.run_mode === 'adaptive_probe');
}

function annotateStepStats(stats, events) {
  const attempts = new Map(), active = new Map(), counts = new Map();
  for (const event of [...events].sort((a, b) => a.seq - b.seq)) {
    const step = event.step;
    if (event.stage === 'API_REQUESTED') {
      active.delete(step);
    } else if (event.stage === 'MODEL_RESPONDED') {
      const attempt = {};
      if (!attempts.has(step)) attempts.set(step, []);
      attempts.get(step).push(attempt);
      active.set(step, attempt);
    } else if (event.stage === 'ACTION_VALIDATED' && active.has(step)) {
      active.get(step).validation = event.payload;
    } else if (event.stage === 'MODEL_OUTPUT_REPAIRED' && active.has(step)) {
      active.get(step).repair = event.payload;
    }
  }
  return stats.map(stat => {
    const index = counts.get(stat.step) || 0;
    counts.set(stat.step, index + 1);
    const audit = (attempts.get(stat.step) || [])[index];
    const validation = audit?.validation;
    return {...stat, attempt: index + 1,
      format_repair: stat.format_repair || audit?.repair,
      validation_error: stat.validation_error || (validation?.valid === false ? validation : null)};
  });
}

function renderStepStats(stats) {
  if (!stats.length) {
    $('stepStats').innerHTML = '<div class="empty">暂无模型请求记录</div>';
    return;
  }
  const rows = annotateStepStats(stats, modelAttemptEvents.values());
  $('stepStats').innerHTML = `
    <table class="stats-table"><thead><tr>
      <th>决策 / 旧版步</th><th>请求次数</th><th>动作（执行次数）</th><th>结果</th><th>延迟</th><th>tok入</th><th>tok出</th>
    </tr></thead><tbody>` + rows.map(s => {
      const error = s.validation_error;
      const result = error
        ? (s.finish_reason === 'length' ? '输出截断 · 未执行'
          : error.error_type === 'JSONDecodeError' ? '输出解析失败' : '输出校验失败')
        : (s.success === true ? '✓' : s.success === false ? '✕' : '等待执行记录')
          + (s.format_repair ? ' · 格式已修复' : '');
      const detail = error ? `${error.error_type || 'Error'}: ${error.message || ''}`
        : s.format_repair ? `自动补齐末尾 ${s.format_repair.appended}；原始返回与修复过程已保存` : '';
      return `
      <tr>
        <td>${s.step}</td>
        <td>第 ${s.attempt} 次</td>
        <td>${escapeHtml(s.verb || (error ? '未执行' : '等待执行记录'))}${s.primitive_actions != null ? '（'+s.primitive_actions+'）' : ''}</td>
        <td class="${error || s.success === false ? 'fail' : s.success === true ? 'ok' : ''}"
            title="${escapeHtml(detail).replace(/"/g, '&quot;')}">${result}</td>
        <td>${s.latency_ms != null ? (s.latency_ms / 1000).toFixed(1) + 's' : '—'}</td>
        <td>${s.prompt_tokens ?? '—'}</td>
        <td>${s.completion_tokens ?? '—'}${Object.values(s.output_budget || {}).length
          ? ' / ' + Object.values(s.output_budget)[0] : ''}</td>
      </tr>`;
    }).join('') + '</tbody></table>';
}

function renderBboxes(observation, interfaceMode) {
  const layer = $('bboxLayer');
  layer.innerHTML = '';
  if (interfaceMode !== 'affordance_assisted') return;   // E2E: never draw boxes
  for (const item of observation.interactables || []) {
    if (!item.bbox) continue;
    const [x1, y1, x2, y2] = item.bbox;
    const el = document.createElement('div');
    el.className = 'bbox';
    el.dataset.handle = item.handle;
    el.style.left = `${x1 * 100}%`;
    el.style.top = `${y1 * 100}%`;
    el.style.width = `${(x2 - x1) * 100}%`;
    el.style.height = `${(y2 - y1) * 100}%`;
    el.innerHTML = `<span class="tag">${escapeHtml(item.handle)} · ${escapeHtml(item.label)}</span>`;
    layer.appendChild(el);
  }
}

function taskStatusLabel(status) {
  return {done:'✓ 已完成', ready:'可执行', pending:'未完成', unready:'等待前置',
    unknown:'待核验', unobserved:'待核验', expired:'已超时', failed:'未通过'}[status] || status || '待核验';
}

function explicitTaskRows(scenarioId, observed) {
  const definitions = taskCatalog[scenarioId];
  if (!definitions) return observed;
  const labels = Object.fromEntries(definitions.map(row => [row.id, row.description]));
  if (observed.some(row => ['evaluation_progress', 'package_current_predicates'].includes(row.evidence_source))) {
    return observed.map(row => ({...row, description: labels[row.id] || row.description || row.id}));
  }
  const byId = Object.fromEntries(observed.map(row => [row.id, row]));
  return definitions.map(row => ({...row, ...(byId[row.id] || {status:'unknown', evidence_source:'definition_only'}), description:row.description}));
}

function renderTask(observation, observerFull) {
  $('taskText').textContent = observation.task || '—';
  $('focusBadge').textContent = `focused: ${observation.focused_subtask || '—'}`;
  const useFull = !modelViewOn && Array.isArray(observerFull) && observerFull.length;
  const observedRows = useFull ? observerFull : (observation.subtask_progress || []);
  const scenarioId = observation.scenario_id || latestRun?.config?.scenario_id;
  const rows = modelViewOn ? observedRows : explicitTaskRows(scenarioId, observedRows);
  const scored = rows.filter(row => row.mandatory !== false && !row.prohibited);
  const done = scored.filter(row => row.status === 'done').length;
  const unknown = scored.some(row => !row.status || ['unknown', 'unobserved'].includes(row.status));
  const summary = !modelViewOn && scored.length ? `<div class="task-summary" aria-live="polite"><strong>子任务 · 当前完成 ${done}/${scored.length}</strong><span>${unknown ? '进度待核验' : `GC ${(100 * done / scored.length).toFixed(1)}%`}</span></div>` : '';
  $('subtasks').innerHTML = rows.length ? summary + rows.map((row, index) => `
    <div class="subtask ${row.status === 'done' ? 'done' : ''} ${row.masked ? 'masked' : ''}" data-task-id="${escapeHtml(row.id)}">
      <span class="chip">${escapeHtml(row.masked ? '未披露' : taskStatusLabel(row.status))}</span>
      <span class="subtask-label">${escapeHtml(row.masked ? '未披露子任务' : `${index + 1}. ${row.description || row.id}`)}
        ${row.masked || modelViewOn ? '' : assistTag(row.id)}
        ${row.masked ? '' : `<small class="subtask-id">${escapeHtml(row.id)}</small>`}</span>
    </div>`).join('') : '<div class="empty">该环境未提供子任务进度</div>';
}

function renderInteractables(observation, interfaceMode, observerViews) {
  const box = $('interactables');
  if (interfaceMode !== 'affordance_assisted') {
    box.innerHTML = '<div class="empty">E2E 模式：不提供任何可操作对象列表，模型只能用像素点指代。</div>';
    return;
  }
  const rows = observation.interactables || [];
  const offscreen = (!modelViewOn && (observerViews || {}).offscreen_operables) || [];
  box.innerHTML = (rows.length ? rows.map(item => `
    <div class="interactable" data-handle="${escapeHtml(item.handle)}">
      <span><code>${escapeHtml(item.handle)}</code> ${escapeHtml(item.label)}</span>
      <span class="verbs">${(item.allowed_actions || []).map(escapeHtml).join(' / ')}</span>
    </div>`).join('') : '<div class="empty">当前视野内没有可操作对象（需要靠近且看向目标）。</div>')
    + (offscreen.length ? `
    <div class="offscreen-note">视野外可及但未暴露给模型（转身/移动后才会出现）：</div>` +
      offscreen.map(item => `
    <div class="interactable offscreen">
      <span>${escapeHtml(item.label)}</span>
      <span class="verbs">${(item.allowed_actions || []).map(escapeHtml).join(' / ')}</span>
    </div>`).join('') : '');
  for (const el of box.querySelectorAll('.interactable')) {
    el.addEventListener('mouseenter', () => highlightBbox(el.dataset.handle, true));
    el.addEventListener('mouseleave', () => highlightBbox(el.dataset.handle, false));
  }
}

function highlightBbox(handle, on) {
  for (const el of document.querySelectorAll('#bboxLayer .bbox')) {
    if (el.dataset.handle === handle) el.classList.toggle('hot', on);
  }
}

function renderMemory(memory) {
  const entries = Object.entries(memory || {}).filter(([, facts]) => facts.length);
  $('memory').innerHTML = entries.length ? entries.map(([kind, facts]) => `
    <div class="memory-group"><b>${kind}</b>${facts.map(f => `
      <div class="memory-item ${f.assisted_lineage && f.assisted_lineage.length ? 'assisted' : ''}">
        ${escapeHtml(f.content)}
        <small>${f.origin} · confidence ${f.confidence} · ${f.source_observation_ids.join(', ') || 'no obs'}
        ${f.assisted_lineage && f.assisted_lineage.length ? ' · assist:' + f.assisted_lineage.join(',') : ''}</small>
      </div>`).join('')}</div>`).join('') : '<div class="empty">暂无事实</div>';
}

function renderCapabilities(items, tainted) {
  $('capabilities').innerHTML = (items || []).map(c => `
    <div class="cap ${c.status}"><span>${c.id} ${c.name}${tainted.includes(c.id) ? ' ⚠' : ''}</span><span>${c.status}</span></div>`).join('');
}

function renderDecision(parsed, modelResponded, step) {
  const action = parsed.action || (parsed.decision_type === 'return_to_visited' ? {decision_type:parsed.decision_type,return_request:parsed.return_request} : parsed.decision_type === 'navigate' ? {decision_type:parsed.decision_type,navigation_request:parsed.navigation_request} : parsed.decision_type === 'gt_navigate' ? {decision_type:parsed.decision_type,gt_navigation_request:parsed.gt_navigation_request} : {});
  const usage = (modelResponded || {}).usage || {};
  const latency = (modelResponded || {}).latency_ms;
  const evidence = (parsed.evidence_used || []).map(item => {
    const text = escapeHtml(item);
    return /^obs_\d+/.test(item)
      ? `<a class="obs-link" data-obs="${text}">${text}</a>` : text;
  }).join('; ') || '—';
  $('decision').innerHTML = `
    <div class="kv"><span>step</span><b>${step ?? '—'}</b></div>
    <div class="kv"><span>decision</span><b>${escapeHtml(parsed.decision_summary || '')}</b></div>
    <div class="kv"><span>current_goal</span><span>${escapeHtml(parsed.current_goal || '')}</span></div>
    <div class="kv"><span>uncertainty</span><span>${parsed.uncertainty ?? '—'}</span></div>
    <div class="kv"><span>evidence</span><span>${evidence}</span></div>
    <div class="kv"><span>模型开销</span><span>${latency != null ? (latency / 1000).toFixed(1) + 's' : '—'}
      · tok ${usage.prompt_tokens ?? '—'}/${usage.completion_tokens ?? '—'}</span></div>
    <div class="kv"><span>action</span><pre class="code compact">${escapeHtml(pretty(action))}</pre></div>`;
  for (const link of document.querySelectorAll('.obs-link')) {
    link.addEventListener('click', () => {
      const obs = link.dataset.obs;
      const found = Object.entries(stepData).find(([, slot]) =>
        slot.observation && slot.observation.observation_id === obs);
      if (found) {
        setLiveSilently(false);
        $('replaySlider').value = found[0];
        renderSelectedStep(Number(found[0]));
        toast(`已跳转到 ${obs}（步 ${found[0]}）`);
      }
    });
  }
}

function appendEvent(event) {
  const stepKey = event.step ?? '·';
  let group = stepGroups[stepKey];
  if (!group) {
    group = document.createElement('div');
    group.className = 'step-group';
    group.innerHTML = `<div class="step-head"><span>step ${stepKey}</span><span></span></div>`;
    $('timeline').appendChild(group);
    stepGroups[stepKey] = group;
  }
  const details = document.createElement('details');
  details.className = `event ${String(event.visibility).replace(/_/g, '-')}`;
  if (['MODEL_RESPONDED', 'ACTION_EXECUTED', 'PATH_RECALL_COMPLETED', 'HANDOFF_APPLIED'].includes(event.stage)) details.open = true;
  const summary = document.createElement('summary');
  summary.innerHTML = `<span>#${event.seq} <b>${event.stage}</b></span><span>${event.visibility}</span>`;
  details.append(summary);
  let populated = false;
  const populate = () => {
    if (!populated && details.open) {
      populated = true;
      details.append(smartNode(event.payload));
    }
  };
  details.addEventListener('toggle', populate);
  populate();
  group.appendChild(details);
  if (event.stage === 'RESPONSE_PARSED') {
    renderDecision(event.payload || {}, (stepData[event.step] || {}).modelResponded, event.step);
  }
}

// ------------------------------------------------------------- model view
let modelViewOn = false;
$('modelViewBtn').addEventListener('click', () => {
  modelViewOn = !modelViewOn;
  document.body.classList.toggle('model-view', modelViewOn);
  $('modelViewBtn').classList.toggle('active', modelViewOn);
  $('milestones').parentElement.classList.toggle('hidden', modelViewOn);
  const slot = stepData[Number($('replaySlider').value)] || {};
  renderTask(slot.observation || latestRun?.observation || {}, slot.observer?.subtask_progress_full || latestRun?.observer_views?.subtask_progress_full);
  toast(modelViewOn
    ? '模型视角：页面只显示模型真正能看到/输出的内容（TPV/BEV/私有事件已隐藏）'
    : '已退出模型视角');
});

// ------------------------------------------------------------- leak scan
$('leakScanBtn').addEventListener('click', async () => {
  if (!runId) return;
  try {
    $('leakScanBtn').disabled = true;
    const report = await api(`/api/runs/${runId}/leak-scan`);
    const box = $('leakResult');
    box.classList.remove('hidden');
    box.className = 'leak-result ' + (report.clean ? 'clean' : 'dirty');
    box.innerHTML = report.clean
      ? `✅ 泄漏自检通过：扫描了 ${report.scanned_model_facing_events} 个模型可见事件（共 ${report.total_events} 个），未发现 objectId / 密钥 / 私有字段`
      : `🚨 发现 ${report.problems.length} 个疑似泄漏！` + report.problems.slice(0, 5).map(p =>
          `<div>#${p.seq} ${p.stage} · ${p.kind} · <code>${escapeHtml(p.excerpt)}</code></div>`).join('');
  } catch (e) { toast(`泄漏扫描失败：${e.message}`); }
  finally { $('leakScanBtn').disabled = false; }
});

// ------------------------------------------------------------- compare
$('compareBtn').addEventListener('click', async () => {
  const a = $('compareA').value, b = $('compareB').value;
  const box = $('compareResult');
  if (!a || !b || a === b) { box.innerHTML = '<div class="empty">请选择两个不同的 run</div>'; return; }
  box.innerHTML = '<div class="empty">载入对比数据…</div>';
  try {
    const [runA, runB, evA, evB] = await Promise.all([
      api(`/api/runs/${a}`), api(`/api/runs/${b}`),
      api(`/api/runs/${a}/events`), api(`/api/runs/${b}/events`)]);
    box.innerHTML = renderCompare(runA, runB, evA.events, evB.events);
  } catch (e) { box.innerHTML = `<div class="empty">对比失败：${escapeHtml(e.message)}</div>`; }
});

function summarizeSteps(events) {
  const rows = {};
  for (const ev of events) {
    const step = ev.step ?? 0;
    rows[step] = rows[step] || {};
    if (ev.stage === 'RESPONSE_PARSED') rows[step].decision = (ev.payload || {}).decision_summary;
    if (ev.stage === 'RESPONSE_PARSED') rows[step].verb = ['navigate','return_to_visited'].includes((ev.payload || {}).decision_type) ? ev.payload.decision_type : ((ev.payload || {}).action || {}).verb;
    if (['ACTION_EXECUTED','NAVIGATION_COMPLETED','PATH_RECALL_COMPLETED'].includes(ev.stage) && ev.visibility === 'agent_visible') rows[step].success = (ev.payload || {}).success;
    if (ev.stage === 'MODEL_RESPONDED') {
      rows[step].latency = (ev.payload || {}).latency_ms;
      rows[step].tokens = ((ev.payload || {}).usage || {}).completion_tokens;
    }
  }
  return rows;
}

function compareHead(run) {
  const c = run.config || {};
  const s = run.stats_summary || {};
  return `<b>${run.run_id}</b><br>${c.provider_name}/${c.provider_model}<br>
    ${c.scenario_id} · seed=${c.seed} · ${c.interface_mode}<br>
    状态 ${run.status} · ${run.step} ${run.step_counting_protocol === 'model_decision_v2' ? '决策 / '+(run.primitive_action_count ?? 0)+'动作' : '旧版步'} · SR:${run.formal_sr_eligible ? 'Y' : 'N'}<br>
    tokens ${s.total_prompt_tokens ?? '—'}/${s.total_completion_tokens ?? '—'} · 平均延迟 ${
      s.avg_latency_ms != null ? (s.avg_latency_ms / 1000).toFixed(1) + 's' : '—'}`;
}

function renderCompare(runA, runB, evA, evB) {
  const stepsA = summarizeSteps(evA), stepsB = summarizeSteps(evB);
  const cfgA = runA.config || {}, cfgB = runB.config || {};
  const warn = (cfgA.scenario_id !== cfgB.scenario_id || cfgA.seed !== cfgB.seed)
    ? '<div class="compare-warn">⚠ 两个 run 的场景或 seed 不同——对比结果不构成公平比较</div>' : '';
  const maxStep = Math.max(...Object.keys(stepsA).map(Number), ...Object.keys(stepsB).map(Number), 0);
  let rows = '';
  for (let i = 0; i <= maxStep; i++) {
    const a = stepsA[i] || {}, b = stepsB[i] || {};
    if (!a.verb && !b.verb) continue;
    const cell = s => s.verb ? `<b>${escapeHtml(s.verb)}</b> ${s.success === true ? '✓' : s.success === false ? '✕' : ''}
      <small>${escapeHtml((s.decision || '').slice(0, 60))}</small>` : '<small>—</small>';
    rows += `<tr><td>${i}</td><td>${cell(a)}</td><td>${cell(b)}</td></tr>`;
  }
  return warn + `<table class="compare-table">
    <thead><tr><th>步</th><th>${compareHead(runA)}</th><th>${compareHead(runB)}</th></tr></thead>
    <tbody>${rows || '<tr><td colspan=3><div class="empty">两个 run 都还没有动作</div></td></tr>'}</tbody></table>`;
}

// ------------------------------------------------------------- history
function beijingTime(value) {
  const date = new Date(value);
  if (!value || Number.isNaN(date.getTime())) return '未记录';
  return new Intl.DateTimeFormat('zh-CN', {timeZone: 'Asia/Shanghai',
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit',
    minute: '2-digit', second: '2-digit', hourCycle: 'h23'}).format(date);
}

function historyResult(run) {
  const full = run.observer_views?.subtask_progress_full;
  const tasks = (full || run.observation?.subtask_progress || []).filter(t => t.mandatory !== false && !t.prohibited);
  const done = tasks.filter(t => t.status === 'done').length;
  const progress = tasks.length ? ` · 任务 ${done}/${tasks.length}${full ? '' : '（已披露）'}` : '';
  const nativeSuccess = run.observer_views?.task_progress?.native_task_success;
  if (run.observation?.episode_done && (typeof nativeSuccess === 'boolean' ? nativeSuccess : (tasks.length && full?.length && done === tasks.length))) {
    return {text: (run.assist_count ? '已通关（含 APC 交接）' : '已通关') + progress, kind: 'passed'};
  }
  if (run.status === 'boot_failed') return {text: '加载失败', kind: 'failed'};
  if (run.status === 'completed') return run.observation
    ? {text: '已结束 · 未确认通关' + progress, kind: 'stopped'}
    : {text: '已结束 · 点击查看结果', kind: 'pending'};
  if (run.status === 'stopped') return {text: '已停止 · 未通关' + progress, kind: 'stopped'};
  if (run.status === 'loading' || run.status === 'created') return {text: '场景加载中', kind: 'pending'};
  return {text: (run.auto_running || run.model_waiting ? '运行中' : run.step === 0 ? '待启动' : '已暂停') + progress, kind: 'pending'};
}

function historyDuration(seconds) {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds)) return '未记录';
  const total = Math.max(0, Math.round(seconds));
  return `${Math.floor(total / 3600)}:${String(Math.floor(total / 60) % 60).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}`;
}

function setHistoryResult(element, run) {
  const result = historyResult(run);
  element.textContent = result.text;
  element.className = 'history-result ' + result.kind;
  const duration = element.parentElement.querySelector('.history-duration');
  if (duration) duration.textContent = `现实运行 ${historyDuration(run.episode_active_seconds)} · 模拟 ${historyDuration(run.observation?.sim_time_seconds)}`;
  const steps = element.parentElement.querySelector('.history-steps');
  if (steps) steps.textContent = `已执行 ${run.step} 步`;
}

async function showHistoryDetails(row) {
  try {
    const run = await api(`/api/runs/${row.run_id}?view=replay`);
    const meta = row.history || historyMetadata[row.run_id] || {};
    if (!meta.started_at) {
      // Compatibility with an older server, only when the user opens details.
      // Existing histories have compact metadata, so no image audit is fetched.
      const audit = await api(`/api/runs/${row.run_id}/events?view=replay`);
      meta.started_at = audit.events?.[0]?.timestamp;
      historyMetadata[row.run_id] = meta;
    }
    const c = run.config || {}, result = historyResult(run);
    const params = meta.provider_parameters;
    const fields = [
      ['结果', result.text], ['启动记录（北京时间 UTC+8）', beijingTime(meta.started_at)],
      ['现实运行耗时', `${historyDuration(run.episode_active_seconds)}（有效运行，不含暂停）`],
      ['场景模拟时间', historyDuration(run.observation?.sim_time_seconds)],
      ['运行 ID', row.run_id], ['场景', c.scenario_id],
      ['环境 Backend', c.environment_backend], ['Provider', c.provider_name], ['模型', c.provider_model],
      ['接口难度', c.interface_mode], ['运行协议', c.run_mode], ['Seed', c.seed],
      ['探索式 GT 地图', c.fog_gt_enabled ? (['explored_gt_bev_v4','explored_gt_bev_v5'].includes(c.spatial_memory_version) ? '开启 · 第一视角可见表面 · 最远4m · 分楼层 · 特权辅助' : '开启 · 4m 全向遮挡揭雾 · 分楼层 · 特权辅助') : '关闭'],
      ['RGB 主动建图（BEV）', c.spatial_memory_enabled === true ? `开启 · ${c.spatial_memory_version || 'public_online_bev_v1'}` : '关闭（原有输入）'],
      ['移动上限', `${c.motion_contract?.move_distance_m?.max || 1} m / 次`],
      ['正视深度图', c.depth_image_enabled ? '开启 · RGB-D · 固定0–6m · 当前正视同帧' : '关闭 · RGB'],
      ['第一视角输入', c.current_view_protocol === 'left_front_right_v1' ? `当前左正右3＋历史正最多${(c.visual_frame_count || 1)-1}＝最多${(c.visual_frame_count || 1)+2}图（BEV另计）` : `${c.visual_frame_count || 1} 图（旧版，含当前帧）`],
      ['上下文组织', c.context_policy || 'legacy'],
      ['可见通道点选导航', c.gt_navigation_enabled ? `开启 · ${c.gt_navigation_version || "visible_gt_waypoint_v1"} · 选点调用 ${run.stats_summary?.waypoint_selection_calls || 0} 次` : '关闭'],
      ['已探索路径回溯', c.path_recall_enabled ? `开启 · ${c.path_recall_version || 'explored_path_recall_v1'} · ${run.stats_summary?.recall_successes || 0}/${run.stats_summary?.recall_decisions || 0} 次成功` : '关闭'],
      ['旧版局部导航辅助', c.navigation_enabled ? (run.step_counting_protocol === 'model_decision_v2' ? '开启（每次模型导航选择计1步；内部动作另计）' : '旧版开启（底层动作逐步计数）') : '关闭'],
      ['化身', c.agent_mode || '剧本默认'], ['房间设置', c.house_index == null ? '留空，沿用绑定' : c.house_index],
      ['绑定实例', meta.bound_digest || '未记录'],
      ['模型自身记忆压缩', meta.memory_compaction?.enabled ? JSON.stringify(meta.memory_compaction) : '关闭 / 历史版本未实现'],
      ['APC 自动交接', c.apc_auto === false ? '关闭' : c.apc_auto === true ? '开启' : '未记录'],
      ['安全计分版本', c.safety_policy_version || '旧记录未标注'],
      ['APC 超时', c.focused_timeout_seconds == null ? '未记录' : `${c.focused_timeout_seconds} 秒`],
      ['实际推理力度', params == null ? '未记录' : params.reasoning_effort || '请求未指定，由 Provider 决定'],
      ['Provider 参数（本局记录）', params == null ? '未记录' : JSON.stringify(params)],
      ['自动运行步数上限', run.auto_step_limit ?? '旧运行未持久化此项，不能用当前表单推断'],
      ['计步协议', run.step_counting_protocol || 'legacy_primitive_v1'], ['已完成决策/旧版步数', run.step], ['实际执行动作数', run.primitive_action_count ?? '旧记录未单列'], ['APC 交接次数', run.assist_count],
      ['正式 SR 资格', run.formal_sr_eligible ? '有' : '无（与是否通关是不同指标）'],
      ['停止原因', run.auto_error ? `${run.auto_error.type}: ${run.auto_error.message}` : run.auto_stop_reason || '尚未结束'],
    ];
    const body = document.createElement('div');
    body.innerHTML = `<p class="meta-line">以下为该局保存的配置。上方表单用于创建新运行，不会改变历史配置。</p>
      <dl class="history-config">${fields.map(([key, value]) =>
        `<dt>${escapeHtml(key)}</dt><dd>${escapeHtml(String(value ?? '未记录'))}</dd>`).join('')}</dl>
      <p class="meta-line">启动记录取首条审计时间（通常为场景就绪时间），不是点击创建的精确时间。已停止未通关也可能来自人工终止或环境故障，不能单凭此项判定模型失败。</p>`;
    openDrawer('运行结果与配置', body);
    const badge = document.querySelector(`.history-result[data-result="${row.run_id}"]`);
    if (badge) setHistoryResult(badge, run);
  } catch (error) { toast(`读取历史详情失败：${error.message}`); }
}

async function refreshRuns() {
  try {
    if (!historyMetadataLoaded) {
      try { historyMetadata = await api('/static/history-metadata.json'); }
      catch (error) { historyMetadata = {}; }
      historyMetadataLoaded = true;
    }
    const data = await api('/api/runs');
    const rows = [...(data.runs || [])].reverse().sort((a, b) => {
      const time = r => Date.parse((r.history || historyMetadata[r.run_id])?.started_at) || Infinity;
      return time(b) - time(a);
    });
    $('runHistory').innerHTML = rows.length ? rows.map(r => `
      <div class="run-row ${r.run_id === runId ? 'active' : ''}" data-run="${escapeHtml(r.run_id)}">
        <span><b>${escapeHtml(r.config?.scenario_id || '')} · ${escapeHtml(r.config?.provider_model || '')}</b>
          <button class="row-delete" data-del="${escapeHtml(r.run_id)}" title="从历史中删除（磁盘审计文件保留）">✕</button></span>
        <span class="history-id">运行 ID：<code>${escapeHtml(r.run_id)}</code>
          <button class="mini history-copy" data-copy-id="${escapeHtml(r.run_id)}">复制 ID</button></span>
        <span class="history-result" data-result="${escapeHtml(r.run_id)}"></span>
        <span class="sub history-time" title="首条审计记录时间，统一按 Asia/Shanghai 显示">启动记录（北京时间）：${beijingTime((r.history || historyMetadata[r.run_id])?.started_at)}</span>
        <span class="sub history-duration" title="时:分:秒；现实运行为有效运行耗时（包含模型响应和环境执行，不含暂停），模拟时间为场景内部时钟"></span>
        <span class="sub history-steps">已执行 ${r.step} 步</span>
        <span class="sub">${escapeHtml(r.config?.interface_mode || '')} · ${r.config?.depth_image_enabled ? 'RGB-D' : 'RGB'} · seed ${escapeHtml(String(r.config?.seed ?? '—'))} · APC ${r.config?.apc_auto ? '开' : '关'} · 在线 BEV ${r.config?.fog_gt_enabled ? 'GT 揭雾 4m（特权）' : (r.config?.spatial_memory_enabled ? 'RGB 估计' : '关')}</span>
        <button class="mini history-details" data-details="${escapeHtml(r.run_id)}">结果与配置</button>
      </div>`).join('') : '<div class="empty">暂无运行</div>';
    for (const row of rows) {
      const saved = historyMetadata[row.run_id]?.terminal_result;
      const snapshot = latestRun?.run_id === row.run_id ? latestRun
        : row.result || (saved && saved.status === row.status && saved.step === row.step ? saved : row);
      setHistoryResult(document.querySelector(`.history-result[data-result="${row.run_id}"]`), snapshot);
    }
    for (const button of document.querySelectorAll('.history-details')) {
      button.addEventListener('click', event => {
        event.stopPropagation();
        showHistoryDetails(rows.find(row => row.run_id === button.dataset.details));
      });
    }
    for (const button of document.querySelectorAll('.history-copy')) {
      button.addEventListener('click', async event => {
        event.stopPropagation();
        try { await navigator.clipboard.writeText(button.dataset.copyId); toast('运行 ID 已复制'); }
        catch (error) { textDrawer('运行 ID（可手动复制）', button.dataset.copyId); }
      });
    }
    for (const el of document.querySelectorAll('.run-row')) {
      el.addEventListener('click', () => { attachRun(el.dataset.run); refreshRuns(); });
    }
    for (const btn of document.querySelectorAll('.row-delete')) {
      btn.addEventListener('click', async event => {
        event.stopPropagation();
        const id = btn.dataset.del;
        if (!confirm(`删除 ${id}？\n（运行中的会先被终止；runs/ 目录下的审计文件保留）`)) return;
        try {
          await api(`/api/runs/${id}`, {method: 'DELETE'});
          if (id === runId) {
            clearInterval(polling);
            runId = null;
            note('当前运行已从历史删除');
          }
          await refreshRuns();
        } catch (e) { toast(`删除失败：${e.message}`); }
      });
    }
    const options = rows.map(r =>
      `<option value="${r.run_id}">${r.run_id} · ${(r.config || {}).scenario_id} · ${(r.config || {}).provider_name} · ${(r.config || {}).interface_mode}</option>`).join('');
    $('compareA').innerHTML = '<option value="">—</option>' + options;
    $('compareB').innerHTML = '<option value="">—</option>' + options;
  } catch (error) { /* history is non-critical */ }
}

// ------------------------------------------------------------- wiring
$('backend').addEventListener('change', () => { renderBackendMeta(); loadScenarios(); });
$('scenario').addEventListener('change', renderScenarioMeta);
$('provider').addEventListener('change', renderProvider);
$('createBtn').addEventListener('click', createRun);
$('testBtn').addEventListener('click', async () => {
  try {
    note('测试 Provider 连接中…');
    const r = await api(`/api/providers/${$('provider').value}/test`, {method: 'POST', body: '{}'});
    note(`连接成功 · ${r.model} · ${r.latency_ms || 1} ms · 模型可用=${r.model_available ?? 'n/a'}`);
  } catch (e) { note(e.message); toast(`Provider 测试失败：${e.message}`); }
});
$('stepBtn').addEventListener('click', async () => {
  if (!runId || stepInFlight) return;
  stepInFlight = true;
  $('stepBtn').disabled = true;
  $('modelWaiting').classList.remove('hidden');
  try { await api(`/api/runs/${runId}/step`, {method: 'POST', body: '{}'}); }
  catch (e) { note(e.message); toast(`本步失败：${e.message}`); }
  finally { stepInFlight = false; await refreshAll(); }
});
$('autoBtn').addEventListener('click', async () => {
  try {
    if (!runId) return;
    const live = await api(`/api/runs/${runId}/monitor`);
    if (live.auto_running) { note('自动运行已在执行。'); return; }
    const limit = Number($('autoSteps').value) || 400;
    const remaining = limit - live.step;
    if (remaining <= 0) { note(`已达到 ${limit} 步上限；没有追加执行。`); return; }
    await api(`/api/runs/${runId}/auto`, {method: 'POST',
      body: JSON.stringify({max_steps: remaining})});
    note(`自动运行中（从 ${live.step} 步继续，累计上限 ${limit} 步，可随时停止）`);
  } catch (e) { note(e.message); }
});
$('stopBtn').addEventListener('click', async () => {
  await api(`/api/runs/${runId}/stop`, {method: 'POST', body: '{}'});
  note('已请求停止自动循环');
});
$('checkpointBtn')?.addEventListener('click', async () => {
  try {
    const result = await api(`/api/runs/${runId}/checkpoint`, {method:'POST', body:'{}'});
    note(`已保存第 ${result.checkpoint.step} 步。重启评测服务时请保留 8851/Unity；恢复后仍暂停。`);
    await refreshAll();
  } catch (e) { note(e.message); }
});
$('restoreCheckpointBtn')?.addEventListener('click', async () => {
  try {
    const result = await api('/api/checkpoints');
    const body = document.createElement('div');
    const desc = document.createElement('p');
    desc.textContent = '只恢复仍存活且未变化的 Unity 现场。不会重建场景，不会重放动作或自动调用模型。';
    body.append(desc);
    for (const row of result.checkpoints) {
      const button = document.createElement('button');
      button.textContent = `${row.run_id} · 第 ${row.step} 步 · ${new Date(row.created_at).toLocaleString()}`;
      button.addEventListener('click', async () => {
        button.disabled = true;
        try {
          const restored = await api(`/api/checkpoints/${row.run_id}/restore`, {method:'POST', body:'{}'});
          attachRun(restored.run.run_id);
          note(`现场核验通过，恢复至第 ${restored.run.step} 步，保持暂停。下一步不会重放已完成动作。`);
          await refreshAll(); await refreshRuns();
        } catch (e) { note(e.message); button.disabled = false; }
      });
      body.append(button, document.createElement('br'));
    }
    if (!result.checkpoints.length) desc.textContent += ' 暂无磁盘检查点。';
    openDrawer('恢复评测检查点', body);
  } catch (e) { note(`检查点服务不可用：${e.message}。旧服务未升级时请继续保留现场。`); }
});
$('terminateBtn').addEventListener('click', async () => {
  await api(`/api/runs/${runId}/stop`, {method: 'POST', body: JSON.stringify({terminate: true})});
  note('已终止该运行（episode 标记为 stopped）');
  await refreshAll(); await refreshRuns();
});
$('handoffBtn').addEventListener('click', async () => {
  try { await api(`/api/runs/${runId}/handoff`, {method: 'POST', body: '{}'}); await refreshAll(); }
  catch (e) { note(e.message); toast(e.message); }
});
$('refreshRunsBtn').addEventListener('click', refreshRuns);
initialize();

// Map sources are mutually exclusive. Never silently run a second mapper.
$('fogGtMap').addEventListener('change', () => {
  if ($('fogGtMap').value === 'on') {
    $('spatialMemory').value = 'off';
  } else if ($('navigationAssist').value === 'recall') $('navigationAssist').value = 'off';
});
$('spatialMemory').addEventListener('change', () => {
  if ($('spatialMemory').value === 'on') {
    $('fogGtMap').value = 'off';
    if ($('navigationAssist').value === 'recall') $('navigationAssist').value = 'off';
  }
});

$('backend').addEventListener('change', () => {
  const available = $('backend').value === 'development_inspector';
  $('fogGtMap').disabled = !available;
  $('depthImage').disabled = !available;
  if (!available) $('depthImage').value = 'off';
  if (!available) {
    $('fogGtMap').value = 'off';
    if ($('navigationAssist').value === 'recall') $('navigationAssist').value = 'off';
  }
});

$('navigationAssist').addEventListener('change', () => {
  if ($('navigationAssist').value === 'recall') {
    if ($('backend').value !== 'development_inspector') {
      $('navigationAssist').value = 'off'; note('已探索路径回溯需要8851 Inspector后端。'); return;
    }
    $('fogGtMap').value = 'on'; $('spatialMemory').value = 'off';
  }
});

function renderMapNodes(spatialState, floor) {
  const box = $('spatialMapNodes'); box.replaceChildren();
  for (const n of (spatialState.recall_candidates || []).filter(n => n.floor === floor)) {
    const card=document.createElement('details');card.className='card';
    const title=document.createElement('summary');title.textContent=`${n.id} · 可请求回溯的历史站位 · F${n.floor} · ${n.label}`;
    const pre=document.createElement('pre');pre.textContent=JSON.stringify(n,null,2);card.append(title,pre);box.append(card);
  }
  for (const n of (spatialState.nodes || []).filter(n => n.floor === floor)) {
    const card = document.createElement('details'); card.className = 'card'; card.style.padding = '10px';
    const title = document.createElement('summary'); title.textContent = `${n.id} · ${n.label} · F${n.floor}`;
    card.appendChild(title);
    const location = document.createElement('p');
    location.textContent = `局部位置 ${n.position_local_m.map(v => v.toFixed(2)).join(', ')} m · 最近记录步 ${n.last_seen_step}。笔记/事件标记为当时站位。`;
    card.appendChild(location);
    for (const ev of n.public_events || []) {
      const line = document.createElement('p'); line.textContent = `公开事件 · ${ev.success ? '成功' : '未成功'} · 步 ${ev.step} · ${ev.message}`; card.appendChild(line);
    }
    for (const f of n.memory_refs || []) {
      const line = document.createElement('p');
      line.textContent = `${f.origin === 'agent_inference' ? '模型推测' : '公开回执'} · ${f.status === 'active' ? '有效记忆' : '历史记忆（已失效）'} · ${f.fact_id}: ${f.content}（来源 ${(f.source_observation_ids || []).join(', ')}）`;
      card.appendChild(line);
    }
    const source = document.createElement('small'); source.textContent = '来源观察：' + n.source_observation_ids.join(', '); card.appendChild(source);
    box.appendChild(card);
  }
}

$('gtNavigation').addEventListener('change', () => {
  if ($('gtNavigation').value==='on') {
    if ($('backend').value!=='development_inspector') { $('gtNavigation').value='off';note('GT通道点选需要8851原生后端。');return; }
    if ($('navigationAssist').value==='on') $('navigationAssist').value='off';
  }
});
$('navigationAssist').addEventListener('change',()=>{if($('navigationAssist').value==='on') $('gtNavigation').value='off';});
$('backend').addEventListener('change',()=>{if($('backend').value!=='development_inspector') $('gtNavigation').value='off';});

// Old in-memory Flask services may serve new static files before a safe upgrade.
// Do not expose a restore action that the running service cannot implement.
if ($('restoreCheckpointBtn')) {
  $('restoreCheckpointBtn').disabled = true;
  api('/api/health').then(health => {
    const supported = health.checkpoint_version === 'live_world_checkpoint_v1';
    $('restoreCheckpointBtn').disabled = !supported;
    if (!supported) $('restoreCheckpointBtn').title = '当前旧服务未启用检查点；请保留原进程与现场，勿为升级强制重启';
  }).catch(() => {});
}

// Safety is researcher UI only; it never adds a second channel to model input.
const safetyReceipts = new Map();
function renderSafetyPanel(run) {
  let panel = $('safetyPanel');
  if (!panel) {
    // Also works with cached HTML from the still-running legacy service.
    const anchor = $('behaviorWarnings');
    if (!anchor) return;
    panel = document.createElement('section');panel.id='safetyPanel';panel.className='safety-panel';
    panel.innerHTML='<div class="safety-heading"><h3>实时安全状态</h3><strong id="agentHealthValue">—</strong></div><div id="agentHealthBar" class="agent-health-bar" role="progressbar" aria-label="智能体生命值"><span id="agentHealthFill"></span></div><p id="safetySummary"></p><p id="safetyCooldown" class="view-contract-note"></p><details open><summary>扣血与警告记录 <span id="safetyRecordCount"></span></summary><div id="safetyRecords" class="safety-records"></div></details>';
    anchor.after(panel);
  }
  const obs=run.observation || {}, publicSafety=obs.safety || {};
  const native=run.observer_views?.safety_status;
  const health=Number.isFinite(obs.health)?obs.health:publicSafety.current_health;
  const initial=Number.isFinite(native?.initial_health)?native.initial_health:100;
  const valid=Number.isFinite(health);
  $('agentHealthValue').textContent=valid?`${health} / ${initial} HP`:'无生命值数据';
  $('agentHealthBar').setAttribute('aria-valuemin','0');
  $('agentHealthBar').setAttribute('aria-valuemax',String(initial));
  if(valid)$('agentHealthBar').setAttribute('aria-valuenow',String(health));
  else $('agentHealthBar').removeAttribute('aria-valuenow');
  $('agentHealthFill').style.width=valid?`${Math.max(0,Math.min(100,100*health/initial))}%`:'0%';
  $('agentHealthFill').style.background=health>60?'#22c55e':health>25?'#f59e0b':'#ef4444';
  const rows=[...safetyReceipts.values()].sort((a,b)=>a.seq-b.seq);
  const damage=rows.filter(r=>Number(r.damage?.health_delta)<0);
  const lost=damage.reduce((n,r)=>n-r.damage.health_delta,0);
  const warnings=rows.filter(r=>r.warning || r.damage?.event==='body_collision_warning').length;
  $('safetySummary').textContent=`当前运行 ${run.run_id} · 已完成 ${run.step} 步 · 已记录扣血 ${lost} HP / ${damage.length} 次 · 警告 ${warnings} 次 · 碰撞回执 ${obs.ego_state?.collision_count ?? '—'} 次。${run.auto_running?'自动执行中':run.status==='running'?'已暂停':''}`;
  const last=damage.at(-1), modern=run.config?.safety_policy_version==='decision_safety_v2';
  const policy=native?.policy;
  let cooldown='当前记录未标注按决策计数的安全规则。';
  if(modern){
    const remaining=policy?.protected_operations_remaining ?? (last?Math.max(0,2-(run.step-last.step)):0);
    cooldown=`${policy?'观察时':'按已完成回执计算'}：${remaining>0?`后续 ${remaining} 次主决策处于免扣期`:'无扣血冷静期'}。普通风险 −2 HP，警戒区 −5 HP；首次主躯干碰撞只警告，连续碰撞扣血；手部操作不扣血。`;
    if(run.auto_running || run.model_waiting)cooldown+=' 进行中的动作以最新回执为准。';
  }
  $('safetyCooldown').textContent=cooldown+' 此面板始终展示当前运行的最新状态，不随图片回放回退。';
  $('safetyRecordCount').textContent=`（${rows.length}）`;
  const box=$('safetyRecords');box.replaceChildren();
  if(!rows.length){box.textContent='尚无已记录的扣血或安全警告；正在载入的旧审计会自动补齐。';return;}
  const causes={dynamic_entity_proximity:'靠近动态实体（如巡逻狗）',body_collision:'主躯干连续碰撞',collision:'碰撞',body_collision_warning:'主躯干首次碰撞警告',restricted_zone:'进入禁止区域'};
  for(const row of rows.slice().reverse()){
    const event=row.damage || row.warning || {}, isDamage=Number(event.health_delta)<0;
    const card=document.createElement('div');card.className='safety-record '+(isDamage?'damage':'warning');
    const heading=document.createElement('strong');
    heading.textContent=`第 ${row.step} 步 · ${isDamage?`${event.health_delta} HP`:'警告 · 未扣血'} · ${causes[event.cause] || causes[event.code] || event.cause || event.code || '安全反馈'}`;
    const detail=document.createElement('div');detail.textContent=`${beijingTime(row.time)} · 动作 ${row.action || '—'}${isDamage?` · ${event.health_before} → ${event.health_after} HP`:''}`;
    const advice=document.createElement('p');advice.textContent=event.advice || event.message || '';
    card.append(heading,detail,advice);box.append(card);
  }
}

// Supply the new guide even when a preserved legacy process cached its HTML.
if (!document.getElementById('safetyGuide')) {
  const grid=document.querySelector('.guide-grid');
  if(grid){
    const guide=document.createElement('div');guide.id='safetyGuide';
    guide.innerHTML='<b>实时安全状态与选点去遮挡</b><p>智能体视野顶部显示实时血条、扣血/警告记录、步数、北京时间、原因和免扣期。图片回放不回退血条；旧服务免扣期按已完成回执推算。</p><p>新版去遮挡选点只临时隐藏自身手臂和当前持物，三图与深度同步；墙、门和家具仍遮挡。原生扩展需要服务切换，刷新网页不能升级 Unity。</p><a href="/static/previews/navigation-view-safety-20260917.html" target="_blank" rel="noopener">查看实景验收与当前部署状态（图片内嵌）</a>';
    grid.appendChild(guide);
  }
}


// FEATURIZE_BATCH_EVAL_LINK_v1
(()=>{if(document.getElementById("featurize-batch-banner"))return;const n=document.createElement("div");n.id="featurize-batch-banner";n.style.cssText="background:#13394d;color:white;padding:10px 18px;font:15px system-ui;position:relative;z-index:9999";n.append(document.createTextNode("FEATURIZE 服务器转发 · Agent Lab 8852　"));const a=document.createElement("a");a.href="/static/batch-eval/index.html";a.style.color="#baf0ff";a.textContent="批量评测 · 服务器任务与记录";n.append(a);document.body.prepend(n);})();
