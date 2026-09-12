(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const root = document.documentElement;
  const ide = $('ide');
  const panes = [...document.querySelectorAll('[data-pane]')];
  const navItems = [...document.querySelectorAll('.nav-item')];
  const state = {loaded: false, current: 'overview', logs: {bot: '', setup: '', panel: ''}, extra: {}, logSource: 'all', paused: false};
  const paneInfo = {
    overview: ['运行总览', 'WORKSPACE / OVERVIEW', '你的 AI 工作空间，一切尽在掌握。'],
    model: ['模型连接', 'AI CONFIG / MODEL', '配置模型服务、密钥与连接能力。'],
    reply: ['回复规则', 'AI CONFIG / REPLY', '决定机器人何时参与群聊，以及使用哪些触发词。'],
    runtime: ['运行参数', 'SYSTEM / RUNTIME', '调整上下文、轮询与输出限制。'],
    storage: ['数据持久化', 'SYSTEM / STORAGE', '查看本地 SQLite 状态并管理对话历史。'],
    permissions: ['身份与权限', 'SYSTEM / ACCESS', '批准聊天、管理身份与权限范围。'],
    chats: ['聊天与用户', 'WORKSPACE / CHATS', '批准、停用和管理私聊与群聊。'],
    members: ['群成员', 'WORKSPACE / MEMBERS', '确认群成员身份并设置群内权限。'],
    queue: ['任务队列', 'WORKSPACE / QUEUE', '处理异常任务，避免不明确消息重复发送。'],
    roles: ['角色与提示词', 'AI CONFIG / ROLES', '为不同聊天定义独立的 AI 个性与规则。'],
    contexts: ['会话上下文', 'AI CONFIG / CONTEXTS', '查看和清理隔离的对话范围。'],
    knowledge: ['知识库', 'AI CONFIG / KNOWLEDGE', '导入资料并显式授权检索范围。'],
    tools: ['只读工具', 'AI CONFIG / TOOLS', '配置安全的只读工具调用。'],
    attachments: ['附件管理', 'WORKSPACE / ATTACHMENTS', '查看图片、语音和文档的提取状态。'],
    capabilities: ['模型能力', 'AI CONFIG / CAPABILITIES', '分别检查 tools、vision、embeddings 与 transcription。'],
    diagnostics: ['身份诊断', 'SYSTEM / DIAGNOSTICS', '查看群成员识别路径和失败原因。'],
    account: ['账户与备份', 'SYSTEM / ACCOUNT', '管理后台账户、会话和数据库备份。']
  };
  const managementMap = {
    chats: '聊天/私聊用户', members: '群成员/权限', queue: '任务队列', roles: '角色', contexts: '上下文',
    knowledge: '知识库', tools: '只读工具', attachments: '附件', capabilities: '模型能力', diagnostics: '身份诊断', account: '账户/备份'
  };

  function formatBytes(value) {
    const n = Number(value || 0);
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    if (n < 1024 * 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + ' MB';
    return (n / 1024 / 1024 / 1024).toFixed(1) + ' GB';
  }

  function toast(text, ok = true) {
    const el = $('toast');
    if (!el) return;
    el.textContent = text || '';
    el.hidden = false;
    el.className = 'toast show ' + (ok ? 'ok' : 'err');
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { el.hidden = true; }, 5000);
  }
  window.consoleToast = toast;

  function notice(text) {
    const el = $('notice');
    if (!el) return;
    el.textContent = text || '';
    el.hidden = !text;
  }

  async function api(path, options = {}) {
    const headers = Object.assign({'Content-Type': 'application/json', 'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]').content}, options.headers || {});
    const response = await fetch(path, Object.assign({}, options, {headers}));
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.ok === false) throw new Error(data.error || ('HTTP ' + response.status));
    return data;
  }
  window.consoleApi = api;

  function setView(name, updateHash = true) {
    if (!paneInfo[name]) name = 'overview';
    state.current = name;
    panes.forEach((pane) => { pane.hidden = pane.dataset.pane !== name && !(name in managementMap && pane.dataset.pane === 'manager'); });
    navItems.forEach((item) => {
      const active = item.dataset.view === name;
      item.classList.toggle('active', active);
      item.setAttribute('aria-current', active ? 'page' : 'false');
    });
    const info = paneInfo[name];
    $('pageTitle').textContent = info[0];
    $('pageEyebrow').textContent = info[1];
    $('pageDescription').textContent = info[2];
    $('editorTab').textContent = name;
    $('topPath').textContent = name;
    if (updateHash && location.hash !== '#' + name) history.replaceState(null, '', '#' + name);
    document.querySelector('.workspace-scroll')?.scrollTo({top: 0, behavior: 'smooth'});
    if (name in managementMap) {
      const manager = $('content');
      manager?.setAttribute('aria-busy', 'true');
      if (window.showManagementPage) {
        window.showManagementPage(managementMap[name]).finally(() => manager?.setAttribute('aria-busy', 'false'));
      }
    }
    if (window.innerWidth < 900) closeDrawer();
  }

  function toggleClass(key, className, button, force) {
    const enabled = force === undefined ? !ide.classList.contains(className) : force;
    ide.classList.toggle(className, enabled);
    localStorage.setItem(key, enabled ? '1' : '0');
    if (button) button.setAttribute('aria-expanded', String(!enabled));
  }
  function applyLayout() {
    const navCollapsed = localStorage.getItem('wechat-ai-nav-collapsed') === '1';
    const logsCollapsed = localStorage.getItem('wechat-ai-logs-collapsed') === '1';
    ide.classList.toggle('nav-collapsed', navCollapsed);
    ide.classList.toggle('logs-collapsed', logsCollapsed);
    $('toggleNav')?.setAttribute('aria-expanded', String(!navCollapsed));
    $('toggleLogs')?.setAttribute('aria-expanded', String(!logsCollapsed));
  }
  function closeDrawer() {
    ide.classList.remove('drawer-open');
    const backdrop = $('drawerBackdrop');
    if (backdrop) backdrop.hidden = true;
  }
  function openDrawer() {
    ide.classList.add('drawer-open');
    const backdrop = $('drawerBackdrop');
    if (backdrop) backdrop.hidden = false;
  }

  function lines(id) {
    const field = $(id);
    return field ? field.value.split(/\r?\n/).map((s) => s.trim()).filter(Boolean) : [];
  }
  function payload() {
    const mode = document.querySelector('input[name=group_mode]:checked');
    const data = {
      llm_base_url: $('llm_base_url')?.value.trim() || '', llm_api_key: $('llm_api_key')?.value.trim() || '', llm_model: $('llm_model')?.value.trim() || '',
      private_chats: lines('private_chats'), groups: lines('groups'), bot_names: lines('bot_names'), group_mode: mode ? mode.value : 'mention',
      group_prefix: $('group_prefix')?.value || '', system_prompt: $('system_prompt')?.value || ''
    };
    ['poll_seconds', 'context_turns', 'max_input_chars', 'max_reply_chars', 'timeout_seconds', 'max_tokens'].forEach((key) => { data[key] = Number($(key)?.value || 0); });
    return data;
  }

  function setConfigState(data) {
    const cfg = data.config || {};
    const set = (id, value) => { if ($(id)) $(id).value = value ?? ''; };
    set('llm_base_url', data.llm_base_url || ''); set('llm_model', data.llm_model || ''); set('llm_api_key', '');
    if ($('keyhint')) $('keyhint').textContent = data.llm_api_key_set ? '已保存密钥：' + (data.llm_api_key_masked || '****') : '还没有保存密钥';
    ['private_chats', 'groups', 'bot_names'].forEach((key) => set(key, (cfg[key] || []).join('\n')));
    ['poll_seconds', 'context_turns', 'max_input_chars', 'max_reply_chars', 'timeout_seconds', 'max_tokens'].forEach((key) => set(key, cfg[key]));
    set('group_prefix', cfg.group_prefix || '/ai '); set('system_prompt', cfg.system_prompt || '');
    const radio = document.querySelector('input[name=group_mode][value="' + (cfg.group_mode || 'mention') + '"]');
    if (radio) radio.checked = true;
    $('overviewModel').textContent = data.llm_model || '未配置';
    $('overviewEndpoint').textContent = data.llm_base_url || '未配置';
    $('overviewContext').textContent = (cfg.context_turns || '—') + ' 轮上下文';
    const configured = Boolean(data.llm_base_url && data.llm_model);
    if ($('modelConfigured')) { $('modelConfigured').textContent = configured ? '已配置' : '待配置'; $('modelConfigured').classList.toggle('good', configured); }
    if (data.config_error) toast('当前配置有问题：' + data.config_error, false);
  }

  function applyStorage(storage) {
    if (!storage || !storage.ok) {
      if ($('storageSummary')) $('storageSummary').textContent = '数据库不可用：' + (storage?.error || '未知错误');
      return;
    }
    if ($('storageSummary')) $('storageSummary').textContent = 'SQLite v' + storage.schema_version + ' · 会话 ' + storage.chat_count + ' · 消息 ' + storage.message_count + ' · 失败 ' + storage.failed_count + ' · 状态不明 ' + storage.unknown_count + ' · 文件 ' + formatBytes(storage.size_bytes);
    if ($('metricChats')) $('metricChats').textContent = storage.chat_count ?? '—';
    if ($('metricMessages')) $('metricMessages').textContent = storage.message_count ?? '—';
    if ($('metricSize')) $('metricSize').textContent = 'SQLite · ' + formatBytes(storage.size_bytes);
    if ($('statusSchema')) $('statusSchema').textContent = 'SQLite v' + storage.schema_version;
    api('/api/storage/chats').then((data) => {
      const select = $('storageChat'); if (!select) return;
      const current = select.value; select.replaceChildren();
      if (!data.chats.length) { select.add(new Option('暂无会话', '')); return; }
      data.chats.forEach((chat) => select.add(new Option((chat.kind === 'group' ? '群聊：' : '私聊：') + chat.name + '（' + chat.message_count + ' 条）', chat.kind + '\t' + chat.name)));
      if ([...select.options].some((option) => option.value === current)) select.value = current;
    }).catch(() => {});
  }

  function applyState(data, fill = false) {
    const running = Boolean(data.running);
    const badge = $('badge');
    if (badge) badge.className = 'status' + (running ? ' on' : '');
    if ($('badgeText')) $('badgeText').textContent = running ? '运行中' + (data.pid ? ' · PID ' + data.pid : '') + (data.stop_requested ? ' · 正在停止' : '') : '未运行';
    if ($('botDetail')) $('botDetail').textContent = running ? '自动回复服务正在处理已批准会话' : '服务当前处于停止状态';
    if ($('btnStart')) $('btnStart').disabled = running;
    if ($('btnStop')) $('btnStop').disabled = !running;
    if ($('sideState')) $('sideState').textContent = running ? '机器人运行中' : '机器人已停止';
    if ($('sideConnection')) $('sideConnection').classList.toggle('online', running);
    if ($('statusConnection')) $('statusConnection').classList.toggle('online', running);
    if ($('connectionText')) $('connectionText').textContent = running ? 'Bot 在线' : 'Bot 离线';
    if (fill) setConfigState(data);
    applyStorage(data.storage || {});
    ingestLogs(data);
    state.loaded = true;
  }

  function ingestLogs(data) {
    state.logs = {bot: data.log || '', setup: data.setup_log || '', panel: data.panel_log || ''};
    renderLogs();
    if ($('logUpdate')) $('logUpdate').textContent = '刚刚更新 · ' + new Date().toLocaleTimeString();
  }
  function lineLevel(line) {
    if (/\b(ERROR|CRITICAL|fatal|失败|错误)\b/i.test(line)) return 'error';
    if (/\b(WARNING|WARN|warning|警告)\b/i.test(line)) return 'warn';
    return 'info';
  }
  function logLines() {
    const source = state.logSource || 'all';
    const search = ($('logSearch')?.value || '').trim().toLowerCase();
    const level = $('logLevel')?.value || 'all';
    const rows = [];
    const sources = source === 'all' ? ['bot', 'setup', 'panel'] : [source];
    sources.forEach((name) => (state.logs[name] || '').split(/\r?\n/).filter(Boolean).forEach((text) => rows.push({source: name, text, level: lineLevel(text)})));
    if (source === 'audit') (state.extra.audit || []).forEach((item) => rows.push({source: 'audit', text: JSON.stringify(item), level: 'info'}));
    if (source === 'tools') (state.extra.tools || []).forEach((item) => rows.push({source: 'tools', text: JSON.stringify(item), level: item.status === 'failed' ? 'error' : 'info'}));
    if (source === 'diagnostics') (state.extra.diagnostics || []).forEach((item) => rows.push({source: 'diagnostics', text: JSON.stringify(item), level: item.reason ? 'warn' : 'info'}));
    return rows.filter((row) => (!search || (row.source + ' ' + row.text).toLowerCase().includes(search)) && (level === 'all' || (level === 'warn' && row.level !== 'info') || (level === 'error' && row.level === 'error')));
  }
  function renderLogs() {
    const viewport = $('logViewport'); if (!viewport) return;
    const rows = logLines(); viewport.replaceChildren();
    if (!rows.length) { const empty = document.createElement('div'); empty.className = 'log-empty'; empty.textContent = state.paused ? '日志更新已暂停' : '没有匹配的日志'; viewport.appendChild(empty); }
    rows.forEach((row) => { const line = document.createElement('div'); line.className = 'log-line ' + row.level; const source = document.createElement('span'); source.className = 'log-source'; source.textContent = row.source.toUpperCase(); const text = document.createElement('span'); text.textContent = row.text; line.append(source, text); viewport.appendChild(line); });
    if ($('logCount')) $('logCount').textContent = rows.length + ' 条';
    const selected = document.querySelector('.log-tabs [aria-selected="true"]');
    const extra = document.querySelector('[data-log-extra="' + state.logSource + '"]');
    const sourceLabel = extra?.textContent || selected?.textContent || '全部';
    if ($('logSourceTitle')) $('logSourceTitle').textContent = sourceLabel + ' · 最近记录';
    if ($('autoScroll')?.checked) viewport.scrollTop = viewport.scrollHeight;
  }
  async function loadExtra(source) {
    try {
      const path = source === 'audit' ? '/api/v1/audit' : source === 'tools' ? '/api/v1/tools/runs' : '/api/v1/members/diagnostics';
      const data = await api(path); state.extra[source] = data.events || data.items || []; renderLogs();
    } catch (error) { if ($('logError')) { $('logError').hidden = false; $('logError').textContent = error.message; } }
  }

  async function refresh(fill = false) {
    if (state.paused && !fill) return;
    try { applyState(await api(fill ? '/api/state' : '/api/log'), fill); }
    catch (error) { toast(error.message, false); if ($('connectionText')) $('connectionText').textContent = '控制台连接失败'; }
  }
  async function clearSelectedChat() {
    const value = $('storageChat')?.value; if (!value) return toast('请先选择会话', false);
    const [kind, ...parts] = value.split('\t'); const name = parts.join('\t'); if (!confirm('确定清空“' + name + '”的全部上下文吗？')) return;
    try { const data = await api('/api/storage/clear-chat', {method: 'POST', body: JSON.stringify({kind, name})}); toast(data.message || '已清空'); await refresh(true); } catch (error) { toast(error.message, false); }
  }
  async function clearAllHistory() {
    if (!confirm('确定清空所有好友和群聊的全部上下文吗？此操作不可恢复。')) return;
    try { const data = await api('/api/storage/clear-all', {method: 'POST', body: JSON.stringify({confirm: 'clear-all'})}); toast(data.message || '已清空'); await refresh(true); } catch (error) { toast(error.message, false); }
  }
  async function saveCfg() {
    try { const data = await api('/api/save', {method: 'POST', body: JSON.stringify(payload())}); toast(data.message || '配置已保存'); if ($('llm_api_key')) $('llm_api_key').value = ''; await refresh(true); } catch (error) { toast(error.message, false); }
  }
  async function startBot() {
    try { const data = await api('/api/start', {method: 'POST', body: JSON.stringify(payload())}); toast(data.message || '已启动'); if ($('llm_api_key')) $('llm_api_key').value = ''; applyState(data); await refresh(true); } catch (error) { toast(error.message, false); }
  }
  async function stopBot() {
    try { const data = await api('/api/stop', {method: 'POST', body: '{}'}); toast(data.message || '已停止'); applyState(data); await refresh(false); } catch (error) { toast(error.message, false); }
  }
  async function testLlm() {
    try { toast('正在测试模型…'); const data = await api('/api/test', {method: 'POST', body: JSON.stringify(payload())}); toast((data.message || '模型可用') + (data.reply ? ' · 回复：' + data.reply : '')); } catch (error) { toast(error.message, false); }
  }

  function bind() {
    applyLayout();
    navItems.forEach((item) => item.addEventListener('click', (event) => { event.preventDefault(); setView(item.dataset.view); }));
    document.querySelectorAll('[data-action]').forEach((button) => button.addEventListener('click', () => {
      const action = button.dataset.action; const fn = {saveCfg, startBot, stopBot, testLlm, clearSelectedChat, clearAllHistory}[action]; if (fn) fn();
    }));
    $('refresh')?.addEventListener('click', () => state.current in managementMap ? setView(state.current) : refresh(true));
    $('toggleNav')?.addEventListener('click', () => { if (window.innerWidth < 900) openDrawer(); else toggleClass('wechat-ai-nav-collapsed', 'nav-collapsed', $('toggleNav')); });
    $('toggleLogs')?.addEventListener('click', () => toggleClass('wechat-ai-logs-collapsed', 'logs-collapsed', $('toggleLogs')));
    $('closeLogs')?.addEventListener('click', () => toggleClass('wechat-ai-logs-collapsed', 'logs-collapsed', $('toggleLogs'), true));
    $('statusLogToggle')?.addEventListener('click', () => toggleClass('wechat-ai-logs-collapsed', 'logs-collapsed', $('toggleLogs'), false));
    $('drawerBackdrop')?.addEventListener('click', closeDrawer);
    document.addEventListener('keydown', (event) => { if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'b') { event.preventDefault(); toggleClass('wechat-ai-nav-collapsed', 'nav-collapsed', $('toggleNav')); } if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'j') { event.preventDefault(); toggleClass('wechat-ai-logs-collapsed', 'logs-collapsed', $('toggleLogs')); } if (event.key === 'Escape') closeDrawer(); });
    document.querySelectorAll('.log-tabs [data-log]').forEach((tab) => tab.addEventListener('click', async () => {
      state.logSource = tab.dataset.log || 'all';
      document.querySelectorAll('.log-tabs [data-log]').forEach((item) => { const active = item === tab; item.setAttribute('aria-selected', String(active)); item.tabIndex = active ? 0 : -1; });
      document.querySelectorAll('[data-log-extra]').forEach((item) => item.setAttribute('aria-pressed', 'false'));
      if (state.logSource !== 'all' && !state.logs[state.logSource]) await loadExtra(state.logSource);
      renderLogs();
    }));
    document.querySelectorAll('[data-log-extra]').forEach((button) => button.addEventListener('click', async () => {
      state.logSource = button.dataset.logExtra || 'all';
      document.querySelectorAll('.log-tabs [data-log]').forEach((item) => { const active = item.dataset.log === 'all'; item.setAttribute('aria-selected', String(active)); item.tabIndex = active ? 0 : -1; });
      document.querySelectorAll('[data-log-extra]').forEach((item) => item.setAttribute('aria-pressed', String(item === button)));
      await loadExtra(state.logSource);
      renderLogs();
    }));
    $('logSearch')?.addEventListener('input', renderLogs); $('logLevel')?.addEventListener('change', renderLogs); $('autoScroll')?.addEventListener('change', renderLogs);
    $('pauseLogs')?.addEventListener('click', () => { state.paused = !state.paused; $('pauseLogs').textContent = state.paused ? '继续更新' : '暂停更新'; $('pauseLogs').setAttribute('aria-pressed', String(state.paused)); renderLogs(); });
    $('copyLogs')?.addEventListener('click', async () => { try { await navigator.clipboard.writeText(logLines().map((row) => '[' + row.source + '] ' + row.text).join('\n')); toast('已复制当前日志'); } catch { toast('浏览器禁止访问剪贴板', false); } });
    $('downloadLogs')?.addEventListener('click', () => { const blob = new Blob([logLines().map((row) => '[' + row.source + '] ' + row.text).join('\n')], {type: 'text/plain;charset=utf-8'}); const link = document.createElement('a'); link.href = URL.createObjectURL(blob); link.download = 'wechat-ai-log-' + new Date().toISOString().slice(0, 10) + '.txt'; link.click(); URL.revokeObjectURL(link.href); });
    document.querySelectorAll('.text-link, .quick-card').forEach((link) => link.addEventListener('click', (event) => { const hash = link.getAttribute('href')?.slice(1); if (paneInfo[hash]) { event.preventDefault(); setView(hash); } }));
    const hash = location.hash.slice(1); setView(paneInfo[hash] ? hash : 'overview', false);
    refresh(true).catch(() => {}); setInterval(() => refresh(false), 2000);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bind); else bind();
})();
