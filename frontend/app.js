const root = document.getElementById('app');
const toastNode = document.getElementById('toast');
const headers = { 'X-Principal-Id': 'local-owner', 'Content-Type': 'application/json' };
const state = {
  page: 'home', modal: false, prompt: '', models: [], modelError: '', providerErrors: {}, sessions: [],
  scene: null, sessionId: null, branchId: 'main', snapshot: null, events: [], report: null,
  perspective: 'GOD', selectedEvent: null, replaySeq: null, activeTab: 'threads',
  configSection: 'world', advanced: false, stageMode: 'table', loading: false, playTimer: null,
  pollTimer: null, autoModels: true, visual: {theme: 'council'}, validationWarnings: [], latestSeq: 0,
  replayQueue: [], playSpeed: 1, playBusy: false, validating: false,
  validationStatus: '', validationFingerprint: '',
  createMode: 'ai', quickMode: 'cooperative',
};

const h = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const nice = value => typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value ?? '');
const qs = params => new URLSearchParams(params).toString();
function visiblePaths(value, prefix = '') {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return [prefix];
  return Object.entries(value).flatMap(([key, child]) => visiblePaths(child, prefix ? `${prefix}.${key}` : key)).sort();
}
function pathValue(value, path) {
  return path.split('.').reduce((current, key) => current && typeof current === 'object' ? current[key] : undefined, value);
}
function setPath(value, path, next) {
  const parts = path.split('.');
  let target = value;
  for (const part of parts.slice(0, -1)) target = target[part];
  target[parts.at(-1)] = next;
}
function syncSimpleRule(action, fields) {
  const effect = action.effects[0];
  const field = fields.find(item => item.path === effect.path);
  const maximum = field?.max ?? 100;
  action.preconditions = [{expr:`world['${effect.path}'] + ${effect.value} <= ${maximum}`,
    reason_code:'METRIC_AT_LIMIT', public_message:'该指标已达上限'}];
}

async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { ...headers, ...(options.headers || {}) } });
  let data;
  try { data = await response.json(); } catch { data = {}; }
  if (!response.ok) {
    const detail = data.detail;
    const message = typeof detail === 'string' && /[\u4e00-\u9fff]/.test(detail) ? detail
      : String(detail || '').includes('401') ? '模型服务拒绝了当前密钥，请检查连接。'
      : Array.isArray(detail) ? '填写内容未通过校验，请检查必填项和数值范围。'
      : `请求未完成（错误码 ${response.status}），请稍后重试。`;
    throw new Error(message);
  }
  return data;
}
function post(path, body) { return api(path, { method: 'POST', body: JSON.stringify(body) }); }
function preferredModel() { return state.models.find(item => item.model_id.toLowerCase().includes('flash'))?.model_id || state.models[0]?.model_id || ''; }
function modelLabel(model) {
  return `${model.provider || 'Model'} · ${model.native_model_id || model.model_id}`;
}
function modelDisplay(modelId) {
  const model = state.models.find(item => item.model_id === modelId);
  return model ? modelLabel(model) : modelId.replace(/^google:/, 'Google · ').replace(/^mimo:/, 'MiMo · ');
}
const statusNames = {RUNNING: '推演中', PAUSING: '正在暂停', PAUSED: '已暂停', STOPPING: '正在结束', FINISHED: '已结束', ERROR: '出现错误', CREATED: '已创建'};
const commandNames = {PAUSE: '暂停', RESUME: '继续', STEP: '单步推进', STOP: '结束'};
function statusName(value) { return statusNames[value] || '状态待更新'; }
function connectionMessage(error) { return error?.includes('401') ? '当前密钥未通过模型服务验证，请在本机更新密钥并重启服务。' : '模型服务暂时不可用，请检查连接后重试。'; }
function notify(message, kind = 'info') {
  toastNode.textContent = message;
  toastNode.dataset.kind = kind;
  toastNode.classList.add('show');
  clearTimeout(notify.timer);
  notify.timer = setTimeout(() => toastNode.classList.remove('show'), 4400);
}
function busy(value) { state.loading = value; render(); }
function nav(page) {
  state.page = page;
  if (['home', 'config', 'collection'].includes(page) && location.hash.startsWith('#/session/'))
    history.replaceState(null, '', location.pathname + location.search);
  if (page !== 'live') stopPolling();
  if (page === 'live') startPolling();
  render();
  window.scrollTo({ top: 0, behavior: 'smooth' });
}
async function init() {
  try {
    const [models, sessions] = await Promise.all([
      api('/models'), api('/sessions')
    ]);
    state.models = models.models || [];
    state.modelError = models.error || '';
    state.providerErrors = models.provider_errors || {};
    state.sessions = sessions.sessions || [];
  } catch (error) { notify(error.message, 'error'); }
  const hash = location.hash.match(/^#\/session\/([^/]+)(?:\/(live|results))?/);
  if (hash) {
    try { await openSession(hash[1], hash[2] || 'live'); } catch (error) { notify(error.message, 'error'); render(); }
  } else render();
}

function icon(name, size = 20) {
  const icons = {
    spark: '<path d="m12 2 1.8 6.2L20 10l-6.2 1.8L12 18l-1.8-6.2L4 10l6.2-1.8z"/><path d="m19 17 .6 1.4L21 19l-1.4.6L19 21l-.6-1.4L17 19l1.4-.6z"/>',
    arrow: '<path d="M4 12h15m-6-6 6 6-6 6"/>',
    play: '<path d="m8 5 11 7-11 7z"/>', pause: '<path d="M8 5v14m8-14v14"/>',
    check: '<path d="m4 12 5 5L20 6"/>', close: '<path d="M5 5 19 19M19 5 5 19"/>',
    chevron: '<path d="m9 5 7 7-7 7"/>', eye: '<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/>',
    globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c3 3 3 15 0 18M12 3c-3 3-3 15 0 18"/>',
    list: '<path d="M8 6h13M8 12h13M8 18h13M3 6h1M3 12h1M3 18h1"/>',
    bars: '<path d="M5 20v-7m7 7V4m7 16v-11"/>',
    scale: '<path d="M12 3v18M5 7h14M4 7l-3 6h6l-3-6zm16 0-3 6h6l-3-6zM7 21h10"/>',
    refresh: '<path d="M20 7v5h-5M4 17v-5h5"/><path d="M5 9a8 8 0 0 1 14-2l1 5M4 12l1 5a8 8 0 0 0 14-2"/>',
    layers: '<path d="m12 2 9 5-9 5-9-5 9-5zm-9 10 9 5 9-5m-18 5 9 5 9-5"/>',
  };
  return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[name] || icons.spark}</svg>`;
}

function header() {
  return `<header class="site-header ${state.page === 'home' ? 'home-header' : ''}">
    <button class="brand" data-action="home" aria-label="返回拟界首页"><span class="brand-symbol">◌</span><span>拟界</span></button>
    ${state.page === 'home' ? `<button class="header-collection" data-action="collection">我的世界 <span class="account-mark" aria-hidden="true">♙</span></button>` : `
    <nav class="primary-nav" aria-label="主导航">
      <button data-action="home">首页</button>
      <button class="${state.page === 'config' ? 'active' : ''}" data-action="config" ${!state.scene ? 'disabled title="先进入一个拟界，世界草案才会可用"' : ''}>世界草案</button>
      <button class="${state.page === 'live' ? 'active' : ''}" data-action="live" ${!state.sessionId ? 'disabled title="先进入一个拟界"' : ''}>推演现场</button>
      <button class="${state.page === 'results' ? 'active' : ''}" data-action="results" ${!state.sessionId ? 'disabled title="先进入一个拟界"' : ''}>结果与回放</button>
    </nav>
    <button class="header-collection" data-action="collection">我的世界 <span class="count-badge">${state.sessions.length}</span></button>`}
  </header>`;
}

function home() {
  return `<main class="world-home" aria-label="拟界首页">
    <div class="world-home-art" role="img" aria-label="帷幕中的幻想世界，角色、城市和议事桌由轨迹相连"></div>
    <div class="world-home-shade"></div>
    <div class="world-home-explain"><strong>让一个世界自行推演</strong><p>描述情境 → 检查角色与规则 → 观察多名角色行动、世界变化和结局。你是观察者，可以暂停、切换视角并回放。</p></div>
    <div class="world-home-bottom"><span class="home-rule" aria-hidden="true"></span>
      <button class="world-create" data-action="open-create">${icon('spark', 17)}<span>创建世界</span></button>
      <span class="home-rule" aria-hidden="true"></span></div>
  </main>
  <section class="home-features" aria-label="拟界介绍">
    <div class="feature-grid">
      <article class="feature-card"><span class="feature-icon">${icon('layers', 22)}</span><div><h3>多角色推演</h3><p>为每个角色设定身份与目标，看他们自行发言、行动、结盟或竞争。</p></div></article>
      <article class="feature-card"><span class="feature-icon">${icon('eye', 22)}</span><div><h3>视角隔离</h3><p>上帝视角纵览全局，也可以只看某个角色知道的信息。</p></div></article>
      <article class="feature-card"><span class="feature-icon">${icon('scale', 22)}</span><div><h3>可回放裁判</h3><p>每次裁定都附带证据，随时回到任意时刻复盘。</p></div></article>
    </div>
    <div class="sample-showcase">
      <div class="sample-showcase-head"><span class="eyebrow">先看一个跑起来的世界</span><h2>不用写一个字，直接体验</h2><p>两个示例场景，一键载入，可单步推进也可以继续推演。</p></div>
      <div class="sample-cards">
        <button class="sample-card" data-action="try-sample" data-sample="cooperative"><span class="sample-tag">合作</span><strong>共同修桥</strong><small>2 名角色 · 把修复进度推到 3</small><span class="card-link">立即体验 ${icon('arrow', 16)}</span></button>
        <button class="sample-card" data-action="try-sample" data-sample="competitive"><span class="sample-tag">竞争</span><strong>双人竞价</strong><small>2 名角色 · 秘密预算博弈</small><span class="card-link">立即体验 ${icon('arrow', 16)}</span></button>
      </div>
    </div>
    <p class="home-footnote">无需配置，开箱即玩。接上模型密钥后，可以用一句话生成属于自己的世界。</p>
  </section>${state.modal ? createModal() : ''}`;
}

function createModal() {
  const quick = state.createMode === 'quick';
  return `<div class="modal-backdrop" data-action="close-create"><section class="create-modal" role="dialog" aria-modal="true" aria-labelledby="create-title">
    <button class="icon-button modal-close" data-action="close-create" aria-label="关闭">${icon('close')}</button>
    <div class="eyebrow">创建拟界</div>
    <div class="create-modes"><div class="segmented" role="tablist" aria-label="创建方式">
      <button type="button" role="tab" class="${!quick ? 'on' : ''}" data-action="create-mode" data-mode="ai">AI 生成初稿</button>
      <button type="button" role="tab" class="${quick ? 'on' : ''}" data-action="create-mode" data-mode="quick">快速创建</button>
    </div></div>
    ${quick ? quickCreateForm() : aiCreateForm()}
    <div class="modal-alt" aria-hidden="true"><span></span><em>或者</em><span></span></div>
    <button type="button" class="sample-button" data-action="try-sample" ${state.loading ? 'disabled' : ''}>${icon('play', 18)}<span><strong>试玩示例世界</strong><small>不用写一个字，先看一个跑起来的推演</small></span>${icon('arrow', 16)}</button>
  </section></div>`;
}

function aiCreateForm() {
  return `<h1 id="create-title">先说说你想推演什么</h1>
    <p>描述场景、角色与目标。系统会生成可逐项修改的世界初稿。</p>
    ${!state.models.length ? `<div class="create-connection" role="status"><strong>暂时无法生成世界初稿</strong><span>${connectionMessage(state.modelError)}</span><button type="button" data-action="refresh-models">重新检查连接</button></div>` : ''}
    <form id="draft-form"><label class="field-label" for="world-prompt">你的世界设想</label>
      <textarea id="world-prompt" name="prompt" rows="7" maxlength="4000" required minlength="12" placeholder="描述你想推演的场景、参与角色及目标。">${h(state.prompt)}</textarea>
       <div class="modal-foot"><span id="draft-hint">至少输入 12 个字，最多 4000 个字；已输入 ${state.prompt.length} 个字。</span><button id="draft-submit" class="primary-button" type="submit" ${state.loading || !state.models.length || state.prompt.trim().length < 12 || state.prompt.length > 4000 ? 'disabled' : ''}>${state.loading ? '正在生成…' : state.models.length ? '生成世界初稿' : '等待模型连接'} ${icon('arrow', 17)}</button></div>
    </form>`;
}

function quickCreateForm() {
  const coop = state.quickMode !== 'competitive';
  return `<h1 id="create-title">填四个空，跑起一个世界</h1>
    <p>按模板生成完整配置，可直接启动推演；启动后仍可在「世界草案」中逐项微调。</p>
    <div class="quick-form">
      <label class="form-field">世界名称<input id="quick-name" maxlength="24" placeholder="未命名世界"></label>
      <label class="form-field">一句话场景<textarea id="quick-story" rows="3" maxlength="200" placeholder="例如：两名参与者共同修复桥梁"></textarea></label>
      <div class="form-field"><span class="field-label">关系模式</span><div class="segmented">
        <button type="button" class="${coop ? 'on' : ''}" data-action="quick-mode" data-mode="cooperative">合作</button>
        <button type="button" class="${!coop ? 'on' : ''}" data-action="quick-mode" data-mode="competitive">竞争</button>
      </div>${!coop ? '<small>竞争模式为双人竞速，固定 2 名角色，率先达成目标者获胜。</small>' : ''}</div>
      <label class="form-field">角色数量<input id="quick-agents" type="number" min="2" max="4" value="2" ${!coop ? 'disabled' : ''}></label>
      <div class="modal-foot"><span>${coop ? '将按合作模板生成：1 项进展指标 · 1 种行动 · 20 回合上限' : '将按竞争模板生成：双人进度条 · 各 1 种行动 · 20 回合上限'}</span><button type="button" class="primary-button" data-action="quick-create" ${state.loading ? 'disabled' : ''}>${state.loading ? '正在启动…' : '开始推演'} ${icon('arrow', 17)}</button></div>
    </div>`;
}

function buildQuickScene({ name, story, mode, agentCount }) {
  const stamp = Date.now().toString(36);
  const modelId = preferredModel() || 'deepseek-flash';
  const seed = Math.floor(Math.random() * 1000000);
  const target = 5;
  const base = {
    scene_id: `quick_${stamp}`, scene_version: '1',
    scenario_name: name, background_story: story, public_story: story,
    interaction_mode: mode, random_seed: seed, scheduler_policy: 'fixed_round_robin',
    judging_policy: { enabled: false },
    leaderboard_config: { enabled: false, leaderboard_type: mode === 'cooperative' ? 'contribution' : 'competitive', metrics: [] },
    early_semantic_trigger_policy: { enabled: false },
  };
  if (mode === 'cooperative') {
    const personas = ['务实协作，优先推进共同目标', '谨慎细致，注重计划与执行', '善于沟通，乐于配合他人', '行动力强，专注落实方案'];
    return { ...base,
      shared_goals: [`将进展提升至 ${target}`],
      agents: Array.from({ length: agentCount }, (_, i) => ({
        agent_id: `agent_${stamp}_${i}`, display_name: `角色 ${i + 1}`,
        public_role: '参与者', persona: personas[i % personas.length],
        personal_goal: '推进共同目标', model_id: modelId, allowed_tools: ['advance'],
      })),
      initial_world_state: { progress: 0 },
      world_state_schema: { schema_version: '1',
        fields: [{ path: 'progress', type: 'int', min: 0, max: target, description: '进展', visibility: { audience: 'PUBLIC' }, preference: 'HIGHER' }],
        invariants: [`working['progress'] <= ${target}`] },
      action_specs: [{
        action_type: 'advance', kind: 'WRITE', description: '推进一次',
        intent: '让世界向共同目标前进一步',
        params_schema: { type: 'object', properties: {}, additionalProperties: false },
        preconditions: [{ expr: `world['progress'] < ${target}`, reason_code: 'ALREADY_DONE', public_message: '目标已达成，无需继续推进。' }],
        effects: [{ op: 'add', path: 'progress', value: 1 }],
        visibility: { proposal: { audience: 'ALL_AGENTS' }, result: { audience: 'ALL_AGENTS' } } }],
      termination_policy: { max_turns: 20,
        hard_conditions: [{ condition_id: 'target_reached', expr: `world['progress'] >= ${target}`, outcome: 'GOAL_REACHED', reason: '目标已达成' }] },
    };
  }
  const mkAction = (tag, owner) => ({
    action_type: `advance_${tag}`, kind: 'WRITE', description: `${owner}推进一次`,
    intent: '提升自己的进度，率先达成目标',
    params_schema: { type: 'object', properties: {}, additionalProperties: false },
    preconditions: [{ expr: `world['score_${tag}'] < ${target}`, reason_code: 'ALREADY_DONE', public_message: '已达成目标，无需继续推进。' }],
    effects: [{ op: 'add', path: `score_${tag}`, value: 1 }],
    visibility: { proposal: { audience: 'ALL_AGENTS' }, result: { audience: 'ALL_AGENTS' } } });
  return { ...base,
    agents: [
      { agent_id: `a_${stamp}`, display_name: '角色 1', public_role: '竞争者', persona: '积极进取，争取率先达成目标', personal_goal: '率先达成目标', model_id: modelId, allowed_tools: ['advance_a'] },
      { agent_id: `b_${stamp}`, display_name: '角色 2', public_role: '竞争者', persona: '沉稳冷静，善于把握时机反超', personal_goal: '率先达成目标', model_id: modelId, allowed_tools: ['advance_b'] },
    ],
    initial_world_state: { score_a: 0, score_b: 0 },
    world_state_schema: { schema_version: '1',
      fields: [
        { path: 'score_a', type: 'int', min: 0, max: target, description: '角色 1 的进度', visibility: { audience: 'PUBLIC' }, preference: 'HIGHER' },
        { path: 'score_b', type: 'int', min: 0, max: target, description: '角色 2 的进度', visibility: { audience: 'PUBLIC' }, preference: 'HIGHER' } ],
      invariants: [] },
    action_specs: [mkAction('a', '角色 1'), mkAction('b', '角色 2')],
    termination_policy: { max_turns: 20,
      hard_conditions: [
        { condition_id: 'a_wins', expr: `world['score_a'] >= ${target}`, outcome: 'WINNER', reason: '角色 1 率先达成目标' },
        { condition_id: 'b_wins', expr: `world['score_b'] >= ${target}`, outcome: 'WINNER', reason: '角色 2 率先达成目标' } ] },
  };
}

async function quickCreate() {
  const name = (document.getElementById('quick-name')?.value || '').trim() || '未命名世界';
  const story = (document.getElementById('quick-story')?.value || '').trim();
  if (story.length < 4) { notify('请用一句话描述你想推演的场景', 'error'); return; }
  const mode = state.quickMode === 'competitive' ? 'competitive' : 'cooperative';
  const agentCount = mode === 'cooperative'
    ? Math.min(4, Math.max(2, Number(document.getElementById('quick-agents')?.value) || 2)) : 2;
  const scene = buildQuickScene({ name, story, mode, agentCount });
  busy(true);
  try {
    await post('/scenes/validate', { scene });
    const result = await post('/sessions', { scene, paused: false });
    state.modal = false; state.prompt = '';
    await openSession(result.session_id, 'live', result.branch_id);
    location.hash = `#/session/${state.sessionId}/live`;
    notify(`世界「${name}」已启动，可在右上角进入「世界草案」微调`, 'success');
    const sessions = await api('/sessions');
    state.sessions = sessions.sessions || [];
  } catch (error) {
    notify(error.message, 'error');
  } finally { busy(false); }
}

async function playSample(sampleName) {
  busy(true);
  try {
    const { examples } = await api('/examples');
    const sample = examples.find(e => e.name === (sampleName || 'cooperative')) || examples[0];
    if (!sample?.scene) throw new Error('暂无可用示例场景');
    const result = await post('/sessions', { scene: sample.scene, paused: true });
    state.modal = false; state.prompt = '';
    await openSession(result.session_id, 'live', result.branch_id);
    location.hash = `#/session/${state.sessionId}/live`;
    notify(`已载入示例世界「${sample.scene.scenario_name}」，可单步推进或继续推演`, 'success');
    const sessions = await api('/sessions');
    state.sessions = sessions.sessions || [];
  } catch (error) {
    notify(error.message, 'error');
  } finally { busy(false); }
}

const configSections = [
  ['world', '世界与目标'], ['agents', '角色与模型'], ['state', '初始世界'],
  ['rules', '行动与规则'], ['judge', '裁判与排行'], ['finish', '终止与启动'], ['advanced', '完整配置']
];
function modelOptions(selected) {
  if (!state.models.length) return `<option value="${h(selected || '')}">暂无已验证模型</option>`;
  return state.models.map(m => `<option value="${h(m.model_id)}" ${selected === m.model_id ? 'selected' : ''}>${h(modelLabel(m))}</option>`).join('');
}
function config() {
  if (!state.scene) return `<main class="empty-page"><h1>还没有世界草案</h1><button class="primary-button" data-action="open-create">开始创建</button></main>`;
  const scene = state.scene;
  const section = state.configSection;
  const fields = scene.world_state_schema.fields || [];
  const selectedModel = scene.agents[0]?.model_id || '';
  let content = '';
  if (section === 'world') content = `<div class="section-heading"><span class="eyebrow">第一步 · 世界</span><h2>世界与共同目标</h2><p>先确认正在推演的问题，以及角色之间的主要关系。</p></div>
    <div class="form-grid"><label class="form-field wide">世界名称<input data-field="scenario_name" value="${h(scene.scenario_name)}"></label>
      <label class="form-field wide">场景设定<textarea data-field="background_story" rows="6">${h(scene.background_story)}</textarea></label>
      <label class="form-field">关系模式<select data-field="interaction_mode"><option value="cooperative" ${scene.interaction_mode === 'cooperative' ? 'selected' : ''}>合作</option><option value="competitive" ${scene.interaction_mode === 'competitive' ? 'selected' : ''}>竞争</option><option value="mixed" ${scene.interaction_mode === 'mixed' ? 'selected' : ''}>混合</option></select></label>
      <label class="form-field wide">共同目标<textarea data-field="shared_goals" rows="3">${h((scene.shared_goals || []).join('\n'))}</textarea><small>每行一个目标</small></label>
      <label class="form-field">舞台表现<select id="visual-theme"><option value="council" ${state.visual.theme === 'council' ? 'selected' : ''}>议事桌 · 方案与关系</option><option value="garden" ${state.visual.theme === 'garden' ? 'selected' : ''}>浮岛 · 空间与状态</option><option value="market" ${state.visual.theme === 'market' ? 'selected' : ''}>棋盘 · 资源与竞争</option></select><small>只改变舞台表达，不改变世界规则。</small></label></div>`;
  if (section === 'agents') content = `<div class="section-heading"><span class="eyebrow">第二步 · 角色</span><h2>角色与模型</h2><p>2～7 名参与者。默认所有角色使用同一个模型，也可以逐个指定。</p></div>
    <div class="model-toolbar"><div><strong>模型分配方式</strong><span>${state.autoModels ? '所有角色使用默认模型' : '每个角色单独选择模型'}</span></div><div class="segmented"><button class="${state.autoModels ? 'on' : ''}" data-action="model-auto">自动</button><button class="${!state.autoModels ? 'on' : ''}" data-action="model-manual">手动</button></div></div>
    ${state.modelError || Object.keys(state.providerErrors).length ? `<div class="notice warning">${state.modelError ? connectionMessage(state.modelError) : `部分供应商未连接：${h(Object.entries(state.providerErrors).map(([name, reason]) => `${name}（${reason}）`).join('、'))}`}<button data-action="refresh-models">重新检查</button></div>` : ''}
    <div class="agent-grid">${scene.agents.map((agent, index) => `<article class="agent-edit-card"><div class="agent-card-top"><span class="avatar avatar-${index % 6}">${h(agent.display_name.slice(0, 1))}</span><span>角色 ${String(index + 1).padStart(2, '0')}</span><button class="icon-button" data-action="remove-agent" data-index="${index}" ${scene.agents.length <= 2 ? 'disabled' : ''} aria-label="删除角色">${icon('close', 16)}</button></div>
      <label class="form-field">名称<input data-agent-index="${index}" data-key="display_name" value="${h(agent.display_name)}"></label>
      <label class="form-field">公开身份<input data-agent-index="${index}" data-key="public_role" value="${h(agent.public_role)}"></label>
      <label class="form-field">性格与工作方式<textarea rows="2" data-agent-index="${index}" data-key="persona">${h(agent.persona)}</textarea></label>
      <label class="form-field">个人目标<input data-agent-index="${index}" data-key="personal_goal" value="${h(agent.personal_goal || '')}"></label>
      <label class="form-field">运行模型<select data-agent-index="${index}" data-key="model_id" ${state.autoModels ? 'disabled' : ''}>${modelOptions(agent.model_id)}</select><small>${state.models.length ? `当前：${h(modelDisplay(agent.model_id))}` : `已配置 ${h(agent.model_id || '默认模型')}，尚未验证连通性`}</small></label>
      <div class="tool-permissions"><strong>当前已授权 ${agent.allowed_tools.length} 项</strong><small>未勾选的是可添加行动</small>${scene.action_specs.map(action => `<label><input type="checkbox" data-agent-tool="${index}" value="${h(action.action_type)}" ${agent.allowed_tools.includes(action.action_type) ? 'checked' : ''}>${h(action.description)}</label>`).join('')}</div></article>`).join('')}</div>
    <button class="outline-button" data-action="add-agent" ${scene.agents.length >= 7 ? 'disabled' : ''}>＋ 添加角色</button>`;
  if (section === 'state') content = `<div class="section-heading"><span class="eyebrow">第三步 · 初始状态</span><h2>初始世界事实</h2><p>这些数值是推演开始时的初始状态。"可见范围"决定哪些角色能看到这个数字。</p></div>
    <div class="state-table"><div class="state-table-head"><span>指标与改善方向</span><span>起始值</span><span>上限</span><span>可见范围</span></div>
    ${fields.map((field, i) => `<div class="state-row"><div><strong>${h(field.description || '未命名指标')}</strong>${['int','float'].includes(field.type) ? `<select data-field-preference="${i}"><option value="HIGHER" ${field.preference === 'HIGHER' ? 'selected' : ''}>越高越好</option><option value="LOWER" ${field.preference === 'LOWER' ? 'selected' : ''}>越低越好</option><option value="NEUTRAL" ${field.preference === 'NEUTRAL' ? 'selected' : ''}>不计排行</option>${field.preference === 'TARGET_RANGE' ? '<option value="TARGET_RANGE" selected>目标区间</option>' : ''}</select>` : ''}</div><input type="${['int','float'].includes(field.type) ? 'number' : 'text'}" data-state-index="${i}" value="${h(pathValue(scene.initial_world_state, field.path))}" ${field.type !== 'int' && field.type !== 'float' ? 'disabled' : ''}><span>${h(field.max ?? '—')}</span><select data-field-visibility="${i}" ${!['PUBLIC','OPERATOR_ONLY'].includes(field.visibility?.audience) ? 'disabled' : ''}><option value="PUBLIC" ${field.visibility?.audience === 'PUBLIC' ? 'selected' : ''}>公共</option><option value="OPERATOR_ONLY" ${field.visibility?.audience === 'OPERATOR_ONLY' ? 'selected' : ''}>仅管理员</option>${!['PUBLIC','OPERATOR_ONLY'].includes(field.visibility?.audience) ? `<option selected>受限可见</option>` : ''}</select></div>`).join('')}</div>
    <p class="help-note">想让某些数字只给特定角色看，可以在「完整配置」中设置可见范围。</p>`;
  if (section === 'rules') content = `<div class="section-heading"><span class="eyebrow">第四步 · 规则</span><h2>行动与规则</h2><p>发言可以提出和修订方案；只有通过规则裁定的动作会改变世界事实。</p></div>
    <div class="rule-list">${scene.action_specs.map((action, i) => `<article class="rule-card"><div class="rule-icon">${icon('layers')}</div><div class="rule-content"><span class="eyebrow">行动 ${String(i + 1).padStart(2, '0')}</span><label class="form-field">可执行说明<input data-rule-index="${i}" data-rule-key="description" value="${h(action.description)}"></label>${action.intent ? `<p>行动意图：${h(action.intent)}</p>` : ''}${action.action_type.startsWith('action_') && action.effects.length === 1 && action.effects[0].op === 'add' ? `<div class="rule-edit-grid"><label class="form-field">改变的世界指标<select data-rule-index="${i}" data-rule-key="path">${fields.filter(field => ['int','float'].includes(field.type)).map(field => `<option value="${h(field.path)}" ${action.effects[0].path === field.path ? 'selected' : ''}>${h(field.description || '未命名指标')}</option>`).join('')}</select></label><label class="form-field">每次增加<input type="number" min="1" max="5" data-rule-index="${i}" data-rule-key="delta" value="${h(action.effects[0].value)}"></label></div>` : `<p>实际效果：${h(action.effects.map(x => `${fieldName(x.path)} ${x.op === 'reveal' ? '改变知情范围' : x.op === 'add' ? `+${x.value}` : x.op === 'sub' ? `-${x.value}` : x.op === 'set' ? `设为 ${x.value}` : x.op}`).join('、') || '无世界变更')}</p>`}<small>前置条件：${h(action.preconditions.map(x => x.public_message || '需满足行动条件').join('；') || '无')}</small></div><button class="icon-button" data-action="remove-rule" data-index="${i}" aria-label="删除行动">${icon('close', 16)}</button></article>`).join('')}</div>
    <button class="outline-button rule-add" data-action="add-rule" ${scene.world_state_schema.invariants.length ? 'disabled' : ''}>＋ 添加行动</button><p class="help-note">${scene.world_state_schema.invariants.length ? '新增行动的效果需要在「完整配置」中定义。' : '复杂参数、多个效果与精确表达式可在「完整配置」中编辑。'}</p>`;
  if (section === 'judge') content = `<div class="section-heading"><span class="eyebrow">第五步 · 裁判</span><h2>裁判与排行</h2><p>动作合法性、语义判断和排行各自独立，结果会附上可回看的证据。</p></div>
    <div class="eval-grid"><article class="eval-card"><span class="eval-icon">${icon('check')}</span><h3>规则裁定</h3><p>校验动作是否合法，并决定世界事实是否改变。</p><span class="tag">始终运行</span></article>
    <article class="eval-card"><span class="eval-icon">${icon('scale')}</span><h3>语义裁判</h3><p>根据目标、证据判断完成度与共识。</p><label class="toggle-line"><input type="checkbox" data-toggle="judging_policy.enabled" ${scene.judging_policy.enabled ? 'checked' : ''}> 启用语义裁判</label></article>
    <article class="eval-card"><span class="eval-icon">${icon('bars')}</span><h3>排行榜</h3><p>依据配置中的确定性指标计算贡献，不按发言量计分。</p><label class="toggle-line"><input type="checkbox" data-toggle="leaderboard_config.enabled" ${scene.leaderboard_config.enabled ? 'checked' : ''}> 启用排行</label></article></div>
    <div class="metric-list"><h3>排行指标</h3>${scene.leaderboard_config.metrics.map((metric, i) => `<div><span>${h(metric.name)}</span><label>权重 <input type="number" min="0" max="100" data-metric-index="${i}" value="${Math.round(metric.weight * 100)}">%</label><small>按世界事实计算</small></div>`).join('')}</div>
    <label class="form-field judging-dimensions">语义裁判维度<textarea data-field="judging_policy.judging_dimensions" rows="3">${h((scene.judging_policy.judging_dimensions || []).join('\n'))}</textarea><small>每行一个维度；隐藏评估不会进入角色提示。</small></label>`;
  if (section === 'finish') content = `<div class="section-heading"><span class="eyebrow">第六步 · 启动</span><h2>终止条件与启动检查</h2><p>启动前确认条件可达成，模型已连接，所有角色的配置有效。</p></div>
    <div class="form-grid"><label class="form-field">最多角色决策机会（Turn）<input type="number" min="1" max="200" data-field="termination_policy.max_turns" value="${h(scene.termination_policy.max_turns ?? '')}" placeholder="不单独限制"></label><label class="form-field">最多完整轮数（Round）<input type="number" min="1" max="100" data-field="termination_policy.max_rounds" value="${h(scene.termination_policy.max_rounds ?? '')}" placeholder="不单独限制"></label>
      <label class="form-field">到轮数上限仍未完成时<select data-field="termination_policy.round_limit_outcome"><option value="MAX_TURNS" ${scene.termination_policy.round_limit_outcome !== 'FAILED' ? 'selected' : ''}>记录为达到回合上限</option><option value="FAILED" ${scene.termination_policy.round_limit_outcome === 'FAILED' ? 'selected' : ''}>记录为推演失败</option></select></label>
      <div class="form-field wide"><span>硬终止条件</span>${scene.termination_policy.hard_conditions.map((item, i) => item.condition_id === 'target_reached' ? `<div class="target-editor"><label class="form-field">目标指标<select data-target-index="${i}" data-target-key="path">${fields.filter(field => ['int','float'].includes(field.type)).map(field => `<option value="${h(field.path)}" ${item.expr.includes(`['${field.path}']`) ? 'selected' : ''}>${h(field.description || '未命名指标')}</option>`).join('')}</select></label><label class="form-field">达到数值<input type="number" data-target-index="${i}" data-target-key="value" value="${h(item.expr.match(/\d+$/)?.[0] || 1)}"></label><label class="form-field wide">达到后说明<input data-target-index="${i}" data-target-key="reason" value="${h(item.reason)}"></label></div>` : `<div class="condition"><code>${h(item.reason || '满足设定条件')}</code><small>${h(item.expr.replace(/world\['([^']+)'\]/g, (_, path) => fieldName(path)).replace(/>=/g, '≥').replace(/<=/g, '≤').replace(/==/g, '=').replace(/\band\b/g, ' 且 ').replace(/\bor\b/g, ' 或 '))}</small></div>`).join('') || '<div class="condition">达到回合上限时结束</div>'}</div></div>
    <div class="launch-checklist"><div>${icon('check', 18)} ${scene.agents.length} 名角色已配置</div><div>${icon('check', 18)} ${fields.length} 项世界事实</div><div>${icon('check', 18)} ${scene.action_specs.length} 种行动规则</div><div>${icon('check', 18)} 裁判与排行将按当前设置执行</div></div>
    <p class="help-note">「检查配置」会在服务端验证结构、初始世界事实及已接入模型；通过后才可启动。</p>`;
  if (section === 'advanced') content = `<div class="section-heading"><span class="eyebrow">完整设置</span><h2>完整配置</h2><p>所有规则、权限、模型和终止表达式均可编辑。应用前会进行完整校验。</p></div>
    <label class="form-field wide"><span>完整配置数据</span><textarea id="advanced-json" class="code-editor" spellcheck="false" rows="25">${h(JSON.stringify(scene, null, 2))}</textarea></label>
    <button class="outline-button" data-action="apply-json">应用完整配置修改</button>`;
  return `<main class="config-page"><div class="page-topline"><span class="eyebrow">世界草案</span><span class="draft-pill">可编辑初稿</span></div>
    <div class="config-title"><div><h1>${h(scene.scenario_name)}</h1><p>从故事到规则，让每个细节都经得起推演。</p></div><button class="text-button" data-action="home">返回首页</button></div>
    <div class="config-layout"><aside class="config-sidebar"><div class="sidebar-title">世界设置 <span>01—07</span></div>${configSections.map(([key, name], i) => `<button class="step-link ${section === key ? 'active' : ''}" data-action="section" data-section="${key}"><span>${String(i + 1).padStart(2, '0')}</span>${name}${icon('chevron', 15)}</button>`).join('')}</aside>
    <section class="config-main">${content}${state.validationWarnings.length ? `<div class="notice warning config-warnings"><strong>启动提醒</strong>${state.validationWarnings.map(item => `<p>${h(item)}</p>`).join('')}</div>` : ''}</section><aside class="config-aside"><div class="preview-card"><div class="preview-top"><span class="eyebrow">世界预览</span><span>◎</span></div><div class="preview-visual"><div class="preview-disc"></div><div class="preview-disc small"></div><span class="preview-orb">✦</span></div><h3>${h(scene.scenario_name)}</h3><p>${h(scene.background_story.slice(0, 110))}${scene.background_story.length > 110 ? '…' : ''}</p><div class="preview-stats"><span>${scene.agents.length} 个角色</span><span>${fields.length} 项事实</span><span>${scene.action_specs.length} 种行动</span></div></div>
    <div class="aside-note"><span class="eyebrow">设计提示</span><p>这是由文字生成的起点。检查每一条可执行规则，再让世界开始运行。</p></div></aside></div>
    <div class="config-footer"><span id="check-status">${h(state.validationFingerprint === JSON.stringify(scene) ? state.validationStatus : '修改后请重新检查配置')}</span><div><button class="outline-button" data-action="validate" ${state.validating ? 'disabled' : ''}>${state.validating ? '检查中…' : '检查配置'}</button><button class="primary-button" data-action="start" ${state.loading ? 'disabled' : ''}>${state.loading ? '处理中…' : '启动推演'} ${icon('arrow', 17)}</button></div></div></main>`;
}

const eventNames = {
  round_started: '新回合开始', turn_started: '角色行动', speech_committed: '公开发言',
  action_proposed: '提出行动', arbiter_accepted: '行动采纳', arbiter_rejected: '行动驳回',
  world_state_delta: '世界变化', semantic_termination_decided: '裁判判断',
  leaderboard_updated: '排行更新', simulation_terminated: '推演结束',
  simulation_paused: '推演暂停', simulation_resumed: '继续推演', model_error: '模型异常',
  budget_exhausted: '预算耗尽', rule_evaluation_error: '规则异常',
  speech_pending: '发言待提交', speech_discarded: '发言未提交', private_observation_added: '私有反馈已记录',
  agent_status_changed: '角色状态变化', permission_denied: '权限拒绝', retry_attempted: '重试行动',
  round_settlement_committed: '回合结算', visibility_revealed: '信息揭示',
  command_accepted: '控制指令已执行', command_rejected: '控制指令被拒绝', command_conflict: '控制版本冲突',
  checkpoint_created: '保存检查点', branch_created: '创建分支', semantic_check: '语义检查',
  early_trigger: '提前检查请求', judging_policy_updated: '裁判配置更新',
  budget_warning: '预算提醒', budget_updated: '预算更新', model_call_cancelled: '模型调用取消',
  fallback_model_used: '启用备用模型', simulation_pausing: '正在暂停',
  simulation_stopping: '正在结束', simulation_error: '推演异常',
};
const outcomes = {
  GOAL_REACHED: '目标达成', CONSENSUS: '达成共识', WINNER: '产生胜者', DRAW: '平局',
  STALEMATE: '陷入僵局', HUMAN_TERMINATED: '由用户结束', FAILED: '推演失败', MAX_TURNS: '达到回合上限',
};
function actionName(id) { return state.scene?.action_specs.find(a => a.action_type === id)?.description || '未命名行动'; }
function fieldName(id) { return state.scene?.world_state_schema.fields.find(f => f.path === id)?.description || '未命名指标'; }
function eventText(e) {
  const p = e.payload || {};
  if (e.type === 'speech_committed') return p.message?.content || '';
  if (e.type === 'world_state_delta') return Object.entries(p.changes || {}).map(([k, v]) =>
    `${fieldName(k)} ${Object.hasOwn(p.before || {}, k) ? `${nice(p.before[k])} → ` : '变为 '}${nice(v)}`).join(' · ');
  if (e.type === 'action_proposed') return actionName(p.action?.action_type);
  if (e.type === 'arbiter_accepted') return `${actionName(p.action_type)} 已采纳`;
  if (e.type === 'arbiter_rejected') return `${p.action_type ? `${actionName(p.action_type)}：` : ''}${p.message || '行动未通过规则裁定'}`;
  if (e.type === 'simulation_terminated') return p.reason || outcomes[p.outcome] || p.outcome || '推演结束';
  if (e.type === 'semantic_termination_decided') return p.reason || '语义判断完成';
  if (e.type === 'model_error') return p.source === 'SEMANTIC_JUDGE' || p.source === 'FINAL_JUDGE'
    ? '裁判模型未能完成本次判断。' : '角色模型回复格式异常，本次行动未提交。';
  if (e.type === 'turn_started') return `${agentName(p.actor_id || e.actor_id)} 开始行动`;
  if (e.type === 'speech_pending') return '关联行动完成裁定后再公开发言';
  if (e.type === 'private_observation_added') return '已记录该角色可见的执行反馈';
  if (e.type === 'checkpoint_created') return `已保存检查点`;
  if (e.type === 'visibility_revealed') return `${fieldName(p.field_path)} 的知情范围已变化`;
  if (e.type === 'command_accepted') return `${commandNames[p.name] || '控制指令'}已执行`;
  if (e.type === 'semantic_check') return `根据目标和证据进行${p.source === 'FINAL' ? '最终' : '阶段'}判断`;
  return typeof p.reason === 'string' && /[\u4e00-\u9fff]/.test(p.reason) ? p.reason : '';
}
function agentName(id) { return state.scene?.agents.find(a => a.agent_id === id)?.display_name || '系统'; }
function relevantEvents() { return state.events.filter(e => ['speech_committed', 'action_proposed', 'arbiter_accepted', 'arbiter_rejected', 'world_state_delta', 'semantic_termination_decided', 'simulation_terminated', 'round_started', 'model_error'].includes(e.type)); }
function currentEvent() {
  const seq = state.replaySeq ?? state.snapshot?.event_seq ?? 0;
  return relevantEvents().filter(e => e.event_seq <= seq).at(-1);
}
function stageMarkup() {
  const world = state.snapshot?.world_state || {};
  const visible = new Set(visiblePaths(world));
  const fields = (state.scene?.world_state_schema?.fields || []).filter(field => visible.has(field.path));
  const e = currentEvent();
  const isMap = state.stageMode === 'map';
  const activeActor = e?.actor_id;
  const worldChange = e?.type === 'world_state_delta';
  const targetCheck = state.scene?.goal_checks?.find(check => check.group === 'SUCCESS' && check.op === 'GE');
  const legacyTarget = !targetCheck ? state.scene?.termination_policy?.hard_conditions?.find(item => item.outcome === 'GOAL_REACHED')?.expr?.match(/world\['([^']+)'\]\s*>=\s*(\d+)/) : null;
  const targetField = targetCheck?.path || legacyTarget?.[1];
  const targetValue = Number(targetCheck?.target || legacyTarget?.[2] || 0);
  const targetCurrent = Number(pathValue(world, targetField || '') ?? 0);
  const progress = targetValue ? Math.min(100, Math.round(targetCurrent / targetValue * 100)) : null;
  return `<div class="world-stage theme-${h(state.visual.theme)} ${isMap ? 'map-mode' : 'table-mode'}">
    <div class="stage-head"><div><span class="eyebrow">${isMap ? '世界状态图' : '角色议事台'}</span><h2>${h(state.scene?.scenario_name || '拟界')}</h2></div><div class="segmented small"><button data-action="stage-table" class="${!isMap ? 'on' : ''}">议事台</button><button data-action="stage-map" class="${isMap ? 'on' : ''}">指标图</button></div></div>
    <div class="stage-canvas"><div class="stage-grid"></div><div class="stage-core ${worldChange ? 'changed' : ''}"><span>拟界</span><strong>${h(state.scene?.scenario_name || '')}</strong><small>${h(state.scene?.shared_goals?.[0] || '')}</small></div>
      ${state.scene?.agents.map((agent, i) => `<button class="stage-agent stage-agent-${i % 7} ${state.perspective === `AGENT:${agent.agent_id}` ? 'focused' : ''} ${activeActor === agent.agent_id ? 'acting' : ''}" data-action="perspective" data-perspective="AGENT:${h(agent.agent_id)}"><span class="stage-agent-avatar avatar-${i % 6}">${h(agent.display_name.slice(0, 1))}</span><span>${h(agent.display_name)}</span></button>`).join('') || ''}
      <div class="stage-metrics">${fields.filter(field => ['int','float'].includes(field.type)).slice(0, 4).map((field, i) => `<div class="stage-metric metric-${i} ${worldChange && field.path in (e.payload?.changes || {}) ? 'changed' : ''}"><span>${h(field.description || '未命名指标')}</span><strong>${h(pathValue(world, field.path))}</strong><small>当前事实</small></div>`).join('')}</div>
      ${progress !== null && visible.has(targetField) ? `<div class="stage-progress"><span>${h(fieldName(targetField))} · 单项条件 ${h(targetCurrent)} / ${h(targetValue)}</span><strong>${progress}%</strong><i><b style="width:${progress}%"></b></i></div>` : ''}
    </div><div class="world-facts"><span class="eyebrow">此视角的世界事实</span><div>${fields.map(field => `<article class="world-fact ${worldChange && field.path in (e.payload?.changes || {}) ? 'changed' : ''}"><small>${h(field.description || field.path)}</small><strong>${h(nice(pathValue(world, field.path)))}</strong></article>`).join('') || '<p>当前没有可见的世界事实</p>'}</div></div><div class="stage-caption"><span class="caption-label">当前事件 · ${e ? String(e.event_seq).padStart(3, '0') : '000'}</span><p>${e ? h(eventText(e) || eventNames[e.type] || e.type) : '世界已就绪，等待第一条事件。'}</p></div>
  </div>`;
}
function eventList() {
  const list = state.events;
  return `<div class="events-list">${list.length ? list.map(e => `<button class="event-item ${state.selectedEvent === e.event_id ? 'selected' : ''}" data-action="event" data-seq="${e.event_seq}" data-event-id="${h(e.event_id)}"><span class="event-rail ${e.type === 'arbiter_rejected' || e.type === 'model_error' ? 'alert' : e.type === 'arbiter_accepted' ? 'success' : ''}"></span><span class="event-main"><span class="event-meta"><b>${h(agentName(e.actor_id))}</b><em>#${String(e.event_seq).padStart(3, '0')} · ${h(eventNames[e.type] || e.type)}</em></span><span class="event-body">${h(eventText(e) || eventNames[e.type] || e.type)}</span></span></button>`).join('') : '<div class="empty-list">还没有可见事件</div>'}</div>`;
}
function threadList() {
  const items = state.events.filter(e => ['speech_committed', 'action_proposed', 'arbiter_accepted',
    'arbiter_rejected', 'world_state_delta', 'visibility_revealed', 'model_error'].includes(e.type));
  const turns = new Map();
  for (const event of items) {
    const key = `${event.round_index}:${event.turn_index}:${event.actor_id}`;
    if (!turns.has(key)) turns.set(key, []);
    turns.get(key).push(event);
  }
  return `<div class="thread-list">${[...turns.values()].map((group, i) => `<article class="thread-card"><div class="thread-heading"><span class="thread-number">${String(i + 1).padStart(2, '0')}</span><strong>${h(group[0].actor_id ? agentName(group[0].actor_id) : '世界演化')}</strong><small>第 ${group[0].round_index + 1} 轮</small></div>${group.map(event => `<button class="thread-beat ${event.type === 'arbiter_rejected' ? 'rejected' : ''}" data-action="event" data-seq="${event.event_seq}" data-event-id="${h(event.event_id)}"><span>${h(eventNames[event.type])}</span><p>${h(eventText(event))}</p><small>#${event.event_seq}</small></button>`).join('')}</article>`).join('') || '<div class="empty-list">还没有可见的行动或世界变化</div>'}</div>`;
}
function live() {
  if (!state.scene || !state.snapshot) return `<main class="empty-page"><h1>正在载入推演…</h1></main>`;
  const snap = state.snapshot;
  const isHistory = state.replaySeq !== null;
  const last = Math.max(state.latestSeq, snap.event_seq || 0);
  const playAction = isHistory ? 'play-replay' : snap.status === 'RUNNING' ? 'pause' : 'resume';
  const playLabel = isHistory ? (state.playTimer ? '暂停回放' : '播放回放')
    : snap.status === 'RUNNING' ? '暂停推演' : snap.status === 'FINISHED' ? '已结束' : '继续推演';
  const playIconName = isHistory ? (state.playTimer ? 'pause' : 'play') : snap.status === 'RUNNING' ? 'pause' : 'play';
  const activeAgent = state.perspective.startsWith('AGENT:')
    ? state.scene.agents.find(agent => agent.agent_id === state.perspective.slice(6)) : null;
  return `<main class="live-page"><div class="live-topline"><div><span class="eyebrow">推演记录 · ${h(state.sessionId?.slice(0, 8))}</span><h1>${h(state.scene.scenario_name)}</h1></div><div class="live-actions"><span class="status-pill status-${h(snap.status || 'HISTORY')}">${isHistory ? '历史回放' : statusName(snap.status)}</span><button class="outline-button compact" data-action="results">查看结果 ${icon('arrow', 16)}</button></div></div>
    <div class="live-toolbar"><div class="perspective-wrap">${icon('eye', 19)}<label for="perspective-select">观察视角</label><select id="perspective-select"><option value="GOD" ${state.perspective === 'GOD' ? 'selected' : ''}>上帝视角</option><option value="OBSERVER" ${state.perspective === 'OBSERVER' ? 'selected' : ''}>公共视角</option>${state.scene.agents.map(agent => `<option value="AGENT:${h(agent.agent_id)}" ${state.perspective === `AGENT:${agent.agent_id}` ? 'selected' : ''}>${h(agent.display_name)} 的视角</option>`).join('')}</select></div><span class="toolbar-divider"></span><span>第 ${Number(snap.scheduler_state?.round_index ?? 0) + 1} 轮</span><span>全局事件 #${snap.event_seq} · 此视角可见 ${state.events.length} 条</span>${activeAgent ? `<span class="active-model">${h(activeAgent.display_name)} · ${h(modelDisplay(activeAgent.model_id))}</span>` : ''}<div class="toolbar-spacer"></div><button class="text-button" data-action="latest">返回最新</button></div>
    <div class="live-layout"><section class="stage-column">${stageMarkup()}<div class="playback-bar"><div class="playback-group"><span class="playback-label">实时控制</span><button class="play-button has-label" data-action="${playAction}" aria-label="${playLabel}" ${!isHistory && snap.status === 'FINISHED' ? 'disabled' : ''}>${icon(playIconName, 18)}<span>${playLabel}</span></button><button class="step-button has-label" data-action="step" title="推进一个角色回合" ${snap.status !== 'PAUSED' || isHistory ? 'disabled' : ''}>${icon('chevron', 16)}<span>单步</span></button><button class="text-button stop-button" data-action="stop" ${snap.status === 'FINISHED' || isHistory ? 'disabled' : ''}>结束</button></div><div class="playback-group replay-group"><span class="playback-label">回放</span><div class="timeline-wrap"><div class="timeline-label"><span>推演时间线</span><span>${isHistory ? `#${state.replaySeq}` : '实时'} / #${last}</span></div><input id="timeline" type="range" min="0" max="${Math.max(last, 1)}" value="${isHistory ? state.replaySeq : last}" aria-label="回放事件位置"><div class="timeline-marks">${relevantEvents().filter(e => ['world_state_delta','semantic_termination_decided','simulation_terminated'].includes(e.type)).slice(-18).map(e => `<button style="left:${Math.min(100, e.event_seq / Math.max(last, 1) * 100)}%" title="${h(eventNames[e.type])} #${e.event_seq}" data-action="event" data-seq="${e.event_seq}" data-event-id="${h(e.event_id)}"></button>`).join('')}</div></div><label class="speed-select">回放速度 <select id="replay-speed"><option value="0.5" ${state.playSpeed === 0.5 ? 'selected' : ''}>×0.5</option><option value="1" ${state.playSpeed === 1 ? 'selected' : ''}>×1</option><option value="2" ${state.playSpeed === 2 ? 'selected' : ''}>×2</option></select></label></div></div></section>
    <aside class="live-sidebar"><div class="sidebar-tabs"><button class="${state.activeTab === 'events' ? 'active' : ''}" data-action="tab-events">完整事件</button><button class="${state.activeTab === 'threads' ? 'active' : ''}" data-action="tab-threads">方案脉络</button></div><div class="sidebar-summary"><span class="eyebrow">${state.activeTab === 'events' ? '事件记录' : '方案与行动'}</span><p>${state.activeTab === 'events' ? '每条事件都可定位舞台和回放。' : '按角色回合串起提出、发言、行动与裁定。'}</p></div>${state.activeTab === 'events' ? eventList() : threadList()}</aside></div>
    ${snap.status === 'FINISHED' && !isHistory ? '<div class="finish-banner"><span>本次推演已结束，证据与排行已就绪。</span><button class="primary-button" data-action="results">查看结果</button></div>' : ''}</main>`;
}

function resultCard(title, value, body, iconName) { return `<article class="result-stat"><span class="result-stat-icon">${icon(iconName)}</span><span>${title}</span><strong>${h(value)}</strong><small>${body}</small></article>`; }
function results() {
  if (!state.report || !state.scene) return `<main class="empty-page"><h1>正在载入结果…</h1></main>`;
  const report = state.report;
  const judge = report.judger;
  const board = report.leaderboard;
  const complete = report.status === 'FINISHED';
  const refs = id => `<button class="evidence-link" data-action="evidence" data-ref="${h(id)}">查看证据 ${icon('arrow', 15)}</button>`;
  const checks = report.goal_checks || [];
  const worldFields = state.scene.world_state_schema.fields.filter(field =>
    pathValue(report.final_world_state || {}, field.path) !== undefined);
  return `<main class="results-page"><div class="results-hero"><div class="results-hero-copy"><span class="eyebrow">推演结果</span><h1>${complete ? report.outcome === 'GOAL_REACHED' ? '已满足设定成功条件。' : '本轮推演结束。' : '推演仍在进行。'}</h1><p>${h(state.scene.scenario_name)} · ${report.outcome ? h(outcomes[report.outcome] || report.outcome) : '结果将在结束后更新'}</p><p>${h(report.outcome_reason || '')}</p><span class="integrity ${report.run_integrity?.pure_agent_run ? '' : 'intervened'}">${report.run_integrity?.pure_agent_run ? '纯角色推演' : '含人工介入'}</span></div><div class="result-orbit"><span>◎</span><small>${complete ? '已结束' : '进行中'}</small></div></div>
    <section class="result-panel result-world"><div class="panel-heading"><span class="eyebrow">最终世界</span><h2>世界变成了什么</h2><p>按真实世界状态与设定条件展示；数值达标仅代表对应条件满足。</p></div><div class="result-world-grid">${worldFields.map(field => `<div class="result-world-item"><span>${h(field.description || field.path)}</span><strong>${h(nice(pathValue(report.final_world_state, field.path)))}</strong></div>`).join('')}</div>${checks.length ? `<div class="goal-checks">${checks.map(check => `<div class="goal-check ${check.group === 'FAILURE' ? check.met ? 'unmet' : 'met' : check.met ? 'met' : 'unmet'}"><b>${check.met ? '✓' : '○'} ${h(check.label)}</b><span>${h(check.current)} ${h({GE:'≥',LE:'≤',EQ:'='}[check.op])} ${h(check.target)} · ${check.group === 'SUCCESS' ? '成功条件' : '失败条件'}</span></div>`).join('')}</div>` : ''}</section>
    <div class="result-stats">${resultCard('动作采纳', report.arbiter.accepted, '规则裁定已确认', 'check')}${resultCard('动作驳回', report.arbiter.rejected, '可追溯裁定', 'scale')}${resultCard('实际行动角色', report.participating_agents?.length ?? 0, '至少开始一次行动', 'globe')}</div>
    <div class="results-grid"><section class="result-panel"><div class="panel-heading"><span class="eyebrow">一 · 规则裁定</span><h2>动作裁定</h2><p>只有合法且被采纳的动作改变世界事实。</p></div><div class="decision-summary"><strong>${report.arbiter.accepted} 次采纳</strong><span>·</span><strong>${report.arbiter.rejected} 次驳回</strong></div><div class="evidence-list">${report.arbiter.evidence_refs.slice(-7).map(ref => refs(ref)).join('') || '<p class="muted">暂无动作裁定证据</p>'}</div></section>
    <section class="result-panel"><div class="panel-heading"><span class="eyebrow">二 · 语义裁判</span><h2>语义判断</h2><p>目标、贡献与共识，依据证据作出判断。</p></div>${judge.status === 'READY' || judge.status === 'INTERIM' ? `${judge.status === 'INTERIM' ? '<span class="tag interim">阶段性判断，最终裁判尚未完成</span>' : ''}<div class="judge-score"><strong>${Math.round(judge.result.goal_completion * 100)}<small>%</small></strong><span>目标完成度</span></div><p class="judge-reason">${h(judge.result.reason)}</p><div class="judge-details"><span>共识 ${Math.round(judge.result.consensus_score * 100)}%</span><span>僵局 ${Math.round(judge.result.stalemate_score * 100)}%</span></div>${judge.judge_bias_risk ? '<p class="judge-caveat">裁判与参与者使用同一模型来源，判断可能有偏差。</p>' : ''}<div class="evidence-list">${judge.result.evidence_refs.map(ref => refs(ref)).join('') || '<p class="muted">此判断未提供可定位的事件证据</p>'}</div>` : `<div class="unavailable">${h(judge.reason || '尚无裁判结果')}</div>`}</section></div>
    <section class="ranking-panel"><div class="panel-heading"><span class="eyebrow">三 · 排行</span><h2>本拟界排行</h2><p>${board.status === 'READY' ? h(board.method) : h(board.reason || '排行将在推演结束后生成')}</p></div>${board.status === 'READY' ? `<div class="ranking-list">${board.rows.map((row, i) => `<div class="ranking-row"><span class="ranking-number">${String(row.rank).padStart(2, '0')}</span><span class="avatar avatar-${i % 6}">${h(row.name.slice(0, 1))}</span><div><strong>${h(row.name)}</strong><small>${h(Object.entries(row.breakdown).map(([key, value]) => `${fieldName(key)} ${value}`).join(' · ') || '暂无指标贡献')}</small></div><span class="ranking-bar"><i style="width:${board.rows[0].score > 0 ? Math.max(0, Math.min(100, row.score / board.rows[0].score * 100)) : 0}%"></i></span><b>${h(row.score)}</b><button class="text-button" data-action="evidence" data-ref="${h(row.evidence_refs[0] || '')}" ${row.evidence_refs.length ? '' : 'disabled'}>证据 ${icon('arrow', 15)}</button></div>`).join('')}</div>` : `<div class="unavailable">${h(board.reason || '暂无排行结果')}</div>`}</section>
    <div class="results-bottom"><span>证据将跳回授权回放的对应事件。</span><button class="outline-button" data-action="live">打开完整回放 ${icon('arrow', 17)}</button></div></main>`;
}

function collection() {
  return `<main class="collection-page"><div class="section-heading"><span class="eyebrow">我的世界</span><h1>我的拟界</h1><p>继续推演，或回看已经发生的选择。</p></div><div class="collection-grid">${state.sessions.map((item, i) => `<button class="collection-card" data-action="open-session" data-session="${h(item.session_id)}" data-branch="${h(item.branch_id)}"><span class="collection-art art-${i % 3}">◌</span><span class="eyebrow">${statusName(item.status)}</span><strong>${h(item.scenario_name)}</strong><small>${item.agent_count} 名角色 · ${item.event_seq} 条事件</small><span class="card-link">进入拟界 ${icon('arrow', 16)}</span></button>`).join('') || '<p class="muted">还没有已创建的拟界。</p>'}</div><button class="primary-button" data-action="open-create">创造新拟界 ${icon('arrow', 17)}</button></main>`;
}

function render() {
  root.innerHTML = `${header()}${({home, config, live, results, collection})[state.page]?.() || home()}${state.page === 'home' ? '' : '<footer class="site-footer"><span>拟界 · 世界推演</span><span>每一个判断，都有其来处。</span></footer>'}`;
}

async function refreshModels() {
  const response = await api('/models');
  state.models = response.models || [];
  state.modelError = response.error || '';
  state.providerErrors = response.provider_errors || {};
  render();
}
async function generateDraft(prompt) {
  state.prompt = prompt;
  if (!state.models.length) throw new Error('请先在服务端配置并验证模型连接');
  busy(true);
  try {
    const result = await post('/scenes/draft', { description: prompt, model_id: preferredModel() });
    state.scene = result.scene;
    state.validationStatus = '';
    state.validationFingerprint = '';
    state.visual = result.visual || {theme: 'council'};
    state.validationWarnings = result.warnings || [];
    state.modal = false;
    state.configSection = 'world';
    state.autoModels = true;
    nav('config');
    notify(state.validationWarnings.length ? `世界初稿已生成，有 ${state.validationWarnings.length} 项需检查` : '世界初稿已生成，请逐项检查');
  } finally { busy(false); }
}
async function validateScene() {
  if (!state.scene) throw new Error('缺少场景配置');
  state.validating = true; render();
  try {
    const result = await post('/scenes/validate', { scene: state.scene });
    state.validationWarnings = result.warnings || [];
    state.validationStatus = `检查通过 · ${result.agent_count} 名角色 · ${result.action_count} 种行动${state.validationWarnings.length ? ` · ${state.validationWarnings.length} 项提醒` : ''}`;
    state.validationFingerprint = JSON.stringify(state.scene);
  } finally {
    state.validating = false; render();
  }
  if (state.validationWarnings.length) {
    notify(`配置检查通过，有 ${state.validationWarnings.length} 项启动提醒`, 'info');
    document.querySelector('.config-warnings')?.scrollIntoView({ behavior: 'smooth', block: 'center' });
  } else {
    notify('配置检查通过，可以启动推演', 'success');
  }
}
async function startSession() {
  busy(true);
  try {
    await validateScene();
    const result = await post('/sessions', { scene: state.scene, paused: false });
    state.sessionId = result.session_id;
    state.branchId = result.branch_id;
    state.perspective = 'GOD';
    state.events = [];
    state.replaySeq = null;
    location.hash = `#/session/${state.sessionId}/live`;
    await loadLive(true);
    nav('live');
    const sessions = await api('/sessions');
    state.sessions = sessions.sessions || [];
  } finally { busy(false); }
}
async function openSession(id, page = 'live', branch = 'main') {
  stopPolling();
  state.sessionId = id;
  state.branchId = branch;
  state.perspective = 'GOD';
  state.replaySeq = null;
  state.events = [];
  const overview = await api(`/sessions/${encodeURIComponent(id)}/overview?${qs({branch_id:branch})}`);
  state.scene = overview.scene;
  state.visual = await api(`/scenes/${encodeURIComponent(state.scene.scene_id)}/visual`);
  if (page === 'results') await loadResults(); else await loadLive(true);
  nav(page);
}
async function loadLive(reset = false) {
  if (!state.sessionId) return;
  const id = encodeURIComponent(state.sessionId);
  const params = { branch_id: state.branchId, perspective: state.perspective };
  const snapshot = await api(`/sessions/${id}/snapshot?${qs(params)}`);
  state.latestSeq = snapshot.event_seq;
  const changed = reset || !state.snapshot || snapshot.event_seq !== state.snapshot.event_seq || snapshot.status !== state.snapshot.status;
  if (!changed) return;
  const newlyVisible = !reset && state.snapshot &&
    visiblePaths(snapshot.world_state).some(path => !visiblePaths(state.snapshot.world_state).includes(path));
  const after = reset || newlyVisible ? 0 : state.events.at(-1)?.event_seq || 0;
  const record = await api(`/sessions/${id}/events?${qs({...params, after_seq: after})}`);
  if (reset || newlyVisible) state.events = record.events;
  else state.events = [...state.events, ...record.events];
  state.replayQueue = state.events.filter(e => ['speech_committed', 'action_proposed', 'arbiter_accepted',
    'arbiter_rejected', 'world_state_delta', 'visibility_revealed', 'semantic_termination_decided',
    'simulation_terminated', 'round_started', 'model_error'].includes(e.type)).map(e => e.event_seq);
  if (state.replaySeq === null) state.snapshot = snapshot;
  if (state.page === 'live') {
    const oldScroll = document.querySelector('.events-list')?.scrollTop || 0;
    render();
    const list = document.querySelector('.events-list');
    if (list) list.scrollTop = oldScroll;
  }
}
function startPolling() {
  stopPolling();
  state.pollTimer = setInterval(async () => {
    if (!state.sessionId || state.replaySeq !== null || document.hidden) return;
    try { await loadLive(); } catch (error) { stopPolling(); notify(error.message, 'error'); }
  }, 1800);
}
function stopPolling() {
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = null;
  if (state.playTimer) clearInterval(state.playTimer);
  state.playTimer = null;
}
async function jumpTo(seq, eventId = null) {
  if (!state.sessionId) return;
  const id = encodeURIComponent(state.sessionId);
  const [replay, record] = await Promise.all([
    api(`/sessions/${id}/replay?${qs({at_seq:seq, perspective:state.perspective, branch_id:state.branchId})}`),
    api(`/sessions/${id}/events?${qs({at_seq:seq, perspective:state.perspective, branch_id:state.branchId})}`)]);
  state.replaySeq = seq;
  state.snapshot = replay;
  state.events = record.events;
  state.selectedEvent = eventId;
  render();
}
async function switchPerspective(value) {
  stopPolling();
  state.perspective = value;
  state.events = [];
  state.snapshot = null;
  state.selectedEvent = null;
  const seq = state.replaySeq;
  state.replaySeq = null;
  render();
  await loadLive(true);
  if (seq !== null) await jumpTo(seq);
  if (state.page === 'live' && seq === null) startPolling();
  notify('已切换为授权视角');
}
async function command(name) {
  if (!state.sessionId) return;
  await post(`/sessions/${encodeURIComponent(state.sessionId)}/commands?${qs({branch_id:state.branchId})}`, {name, expected_version:state.snapshot?.version});
  await loadLive();
}
async function resumeSession() {
  const beforeSeq = state.snapshot?.event_seq || 0;
  await command('RESUME');
  // 无模型时服务端会迅速回落到 PAUSED；轮询确认并给出明确反馈，避免"点了没反应"
  for (let i = 0; i < 3; i++) {
    await new Promise(resolve => setTimeout(resolve, 900));
    await loadLive();
    if (state.snapshot?.status === 'RUNNING') return;
  }
  if (state.snapshot?.status === 'PAUSED') {
    const modelFailed = state.events.some(e => e.type === 'model_error' && e.event_seq > beforeSeq);
    notify(modelFailed ? '模型服务不可用，推演无法继续，请检查模型连接' : '推演已暂停', modelFailed ? 'error' : 'info');
  }
}
async function loadResults() {
  if (!state.sessionId) return;
  state.report = await api(`/sessions/${encodeURIComponent(state.sessionId)}/report?${qs({branch_id:state.branchId})}`);
  render();
}
function nextReplayEvent() {
  const current = state.replaySeq || 0;
  return state.replayQueue.find(seq => seq > current);
}
function startReplayPlayback() {
  state.playTimer = setInterval(async () => {
    if (state.playBusy) return;
    const next = nextReplayEvent();
    if (!next) { clearInterval(state.playTimer); state.playTimer = null; render(); return; }
    state.playBusy = true;
    try { await jumpTo(next); }
    catch (error) { clearInterval(state.playTimer); state.playTimer = null; notify(error.message, 'error'); }
    finally { state.playBusy = false; }
  }, 950 / state.playSpeed);
}

root.addEventListener('submit', async event => {
  if (event.target.id !== 'draft-form') return;
  event.preventDefault();
  try { await generateDraft(new FormData(event.target).get('prompt').trim()); }
  catch (error) { notify(error.message, 'error'); }
});
root.addEventListener('change', async event => {
  const el = event.target;
  if (el.id === 'perspective-select') { try { await switchPerspective(el.value); } catch (error) { notify(error.message, 'error'); } return; }
  if (el.id === 'replay-speed') {
    state.playSpeed = Number(el.value);
    if (state.playTimer) { clearInterval(state.playTimer); startReplayPlayback(); }
    render(); return;
  }
  if (el.id === 'timeline') { try { await jumpTo(Number(el.value)); } catch (error) { notify(error.message, 'error'); } return; }
  if (el.id === 'visual-theme') {
    try {
      state.visual = await api(`/scenes/${encodeURIComponent(state.scene.scene_id)}/visual`, {method:'PUT',body:JSON.stringify({theme:el.value})});
      render(); notify('舞台样式已保存', 'success');
    } catch (error) { notify(error.message, 'error'); }
    return;
  }
  if (!state.scene) return;
  if (el.dataset.field) {
    const key = el.dataset.field;
    if (key === 'shared_goals') state.scene.shared_goals = el.value.split('\n').map(x => x.trim()).filter(Boolean);
    else if (key.includes('.')) {
      const [parent, prop] = key.split('.');
      state.scene[parent][prop] = key === 'judging_policy.judging_dimensions'
        ? el.value.split('\n').map(x => x.trim()).filter(Boolean)
        : el.type === 'number' ? (el.value === '' ? null : Number(el.value)) : el.value;
    } else state.scene[key] = el.value;
    render();
  }
  if (el.dataset.agentIndex !== undefined) {
    state.scene.agents[Number(el.dataset.agentIndex)][el.dataset.key] = el.value;
    render();
  }
  if (el.dataset.agentTool !== undefined) {
    const tools = state.scene.agents[Number(el.dataset.agentTool)].allowed_tools;
    if (el.checked && !tools.includes(el.value)) tools.push(el.value);
    if (!el.checked) state.scene.agents[Number(el.dataset.agentTool)].allowed_tools = tools.filter(x => x !== el.value);
    render();
  }
  if (el.dataset.stateIndex !== undefined) {
    const field = state.scene.world_state_schema.fields[Number(el.dataset.stateIndex)];
    setPath(state.scene.initial_world_state, field.path, field.type === 'float' ? Number(el.value) : parseInt(el.value, 10));
    render();
  }
  if (el.dataset.fieldVisibility !== undefined) {
    state.scene.world_state_schema.fields[Number(el.dataset.fieldVisibility)].visibility.audience = el.value;
    render();
  }
  if (el.dataset.fieldPreference !== undefined) {
    const field = state.scene.world_state_schema.fields[Number(el.dataset.fieldPreference)];
    field.preference = el.value;
    if (el.value !== 'TARGET_RANGE') { field.target_min = null; field.target_max = null; }
    render();
  }
  if (el.dataset.ruleIndex !== undefined) {
    const action = state.scene.action_specs[Number(el.dataset.ruleIndex)];
    if (el.dataset.ruleKey === 'description') action.description = el.value;
    else {
      action.effects[0][el.dataset.ruleKey === 'path' ? 'path' : 'value'] = el.dataset.ruleKey === 'delta' ? Number(el.value) : el.value;
      syncSimpleRule(action, state.scene.world_state_schema.fields);
    }
    render();
  }
  if (el.dataset.metricIndex !== undefined) {
    state.scene.leaderboard_config.metrics[Number(el.dataset.metricIndex)].weight = Math.max(0, Math.min(1, Number(el.value) / 100));
    render();
  }
  if (el.dataset.targetIndex !== undefined) {
    const target = state.scene.termination_policy.hard_conditions[Number(el.dataset.targetIndex)];
    if (el.dataset.targetKey === 'reason') target.reason = el.value;
    else {
      const old = target.expr.match(/world\['([^']+)'\]\s*>=\s*(\d+)/);
      const path = el.dataset.targetKey === 'path' ? el.value : old?.[1];
      const field = state.scene.world_state_schema.fields.find(item => item.path === path);
      const current = Number(pathValue(state.scene.initial_world_state, path));
      const requested = el.dataset.targetKey === 'value' ? Number(el.value) : Math.min(field?.max ?? 100, current + 1);
      if (!field || requested <= current || requested > (field.max ?? 100)) {
        notify('目标必须高于初始值，且不超过指标上限', 'error'); render(); return;
      }
      target.expr = `world['${path}'] >= ${requested}`;
    }
    render();
  }
  if (el.dataset.toggle) {
    const [parent, prop] = el.dataset.toggle.split('.');
    state.scene[parent][prop] = el.checked;
    render();
  }
});
root.addEventListener('input', event => {
  if (event.target.id === 'world-prompt') {
    state.prompt = event.target.value;
    const remaining = Math.max(0, 12 - state.prompt.trim().length);
    const hint = document.getElementById('draft-hint');
    if (hint) hint.textContent = `${remaining ? `还需输入 ${remaining} 个字；` : ''}${state.prompt.length > 4000 ? '超过 4000 字上限；' : ''}已输入 ${state.prompt.length} / 4000 个字。`;
    const submit = document.getElementById('draft-submit');
    if (submit) submit.disabled = Boolean(state.loading || !state.models.length || remaining || state.prompt.length > 4000);
  }
});
root.addEventListener('click', async event => {
  const button = event.target.closest('[data-action]');
  if (!button || button.disabled) return;
  const action = button.dataset.action;
  if (action === 'close-create' && event.target !== button && !event.target.closest('.modal-close')) return;
  try {
    if (action === 'home') nav('home');
    else if (action === 'collection') { const result = await api('/sessions'); state.sessions = result.sessions || []; nav('collection'); }
    else if (action === 'open-create') { state.modal = true; nav('home'); if (!state.models.length) await refreshModels(); setTimeout(() => document.getElementById('world-prompt')?.focus(), 0); }
    else if (action === 'close-create') { state.modal = false; render(); }
    else if (action === 'create-mode') { state.createMode = button.dataset.mode; render(); if (state.createMode === 'ai') setTimeout(() => document.getElementById('world-prompt')?.focus(), 0); }
    else if (action === 'quick-mode') { state.quickMode = button.dataset.mode; render(); }
    else if (action === 'quick-create') await quickCreate();
    else if (action === 'try-sample') { if (!state.loading) await playSample(button.dataset.sample); }
    else if (action === 'config') nav('config');
    else if (action === 'section') { state.configSection = button.dataset.section; render(); }
    else if (action === 'model-auto') { state.autoModels = true; if (state.models.length) state.scene.agents.forEach(a => a.model_id = preferredModel()); render(); }
    else if (action === 'model-manual') { state.autoModels = false; render(); }
    else if (action === 'refresh-models') await refreshModels();
    else if (action === 'add-agent') {
      const index = state.scene.agents.length + 1;
      const id = `agent_${crypto.randomUUID().slice(0, 8)}`;
      state.scene.agents.push({agent_id:id, display_name:`角色 ${index}`, public_role:'参与者', persona:'提出方案并回应其他角色', personal_goal:'推进共同目标', model_id:preferredModel(), allowed_tools:state.scene.action_specs.map(a => a.action_type)});
      render();
    }
    else if (action === 'remove-agent') { state.scene.agents.splice(Number(button.dataset.index), 1); render(); }
    else if (action === 'add-rule') {
      const field = state.scene.world_state_schema.fields.find(item => ['int','float'].includes(item.type));
      if (!field) throw new Error('请先配置一个数值世界指标');
      const id = `action_${crypto.randomUUID().slice(0, 8)}`;
      const next = {action_type:id, kind:'WRITE', description:'新的行动',
        params_schema:{type:'object',properties:{},additionalProperties:false},
        preconditions:[], effects:[{op:'add',path:field.path,value:1}],
        visibility:{proposal:{audience:'ALL_AGENTS'},result:{audience:'ALL_AGENTS'}}};
      syncSimpleRule(next, state.scene.world_state_schema.fields);
      state.scene.action_specs.push(next);
      state.scene.agents.forEach(agent => agent.allowed_tools.push(id));
      render();
    }
    else if (action === 'remove-rule') {
      const removed = state.scene.action_specs.splice(Number(button.dataset.index), 1)[0];
      state.scene.agents.forEach(agent => { agent.allowed_tools = agent.allowed_tools.filter(id => id !== removed.action_type); });
      render();
    }
    else if (action === 'apply-json') {
      const value = JSON.parse(document.getElementById('advanced-json').value);
      await post('/scenes/validate', {scene:value}); state.scene = value; render(); notify('完整配置已应用', 'success');
    }
    else if (action === 'validate') await validateScene();
    else if (action === 'start') await startSession();
    else if (action === 'open-session') { await openSession(button.dataset.session, 'live', button.dataset.branch); location.hash = `#/session/${state.sessionId}/live`; }
    else if (action === 'live') { if (state.sessionId) { await loadLive(true); nav('live'); location.hash = `#/session/${state.sessionId}/live`; } }
    else if (action === 'results') {
      state.perspective = 'GOD'; state.replaySeq = null; state.events = []; state.snapshot = null;
      await loadLive(true); await loadResults(); nav('results');
      location.hash = `#/session/${state.sessionId}/results`;
    }
    else if (action === 'stage-table') { state.stageMode = 'table'; render(); }
    else if (action === 'stage-map') { state.stageMode = 'map'; render(); }
    else if (action === 'perspective') await switchPerspective(button.dataset.perspective);
    else if (action === 'tab-events') { state.activeTab = 'events'; render(); }
    else if (action === 'tab-threads') { state.activeTab = 'threads'; render(); }
    else if (action === 'event') await jumpTo(Number(button.dataset.seq), button.dataset.eventId);
    else if (action === 'latest') { state.replaySeq = null; state.selectedEvent = null; await loadLive(true); startPolling(); render(); }
    else if (action === 'pause') await command('PAUSE');
    else if (action === 'resume') await resumeSession();
    else if (action === 'step') await command('STEP');
    else if (action === 'stop') { if (confirm('结束本次推演？')) await command('STOP'); }
    else if (action === 'play-replay') {
      if (state.playTimer) { clearInterval(state.playTimer); state.playTimer = null; render(); }
      else { startReplayPlayback(); render(); }
    }
    else if (action === 'evidence') {
      const e = state.events.find(item => item.event_id === button.dataset.ref);
      if (!e) { await loadLive(true); }
      const evidence = state.events.find(item => item.event_id === button.dataset.ref);
      if (!evidence) throw new Error('当前视角无权查看这条证据');
      nav('live'); location.hash = `#/session/${state.sessionId}/live`;
      await jumpTo(evidence.event_seq, evidence.event_id);
    }
  } catch (error) { notify(error.message, 'error'); }
});
window.addEventListener('hashchange', async () => {
  const match = location.hash.match(/^#\/session\/([^/]+)(?:\/(live|results))?/);
  if (!match) return;
  const page = match[2] || 'live';
  if (match[1] === state.sessionId && page === state.page) return;
  try { await openSession(match[1], page, match[1] === state.sessionId ? state.branchId : 'main'); }
  catch (error) { notify(error.message, 'error'); }
});
init();
