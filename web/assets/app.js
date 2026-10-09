/* PCBA 排期工作台：静态前端，所有业务数据以同源服务为准。 */
(() => {
  'use strict';

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const escape = (value) => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const attr = escape;
  const number = (value) => value == null || value === '' ? '—' : Number(value).toLocaleString('zh-CN', {maximumFractionDigits: 4});
  const date = (value) => value ? String(value).slice(0, 10) : '—';
  const stamp = (value) => value ? String(value).replace('T', ' ').slice(0, 16) : '—';
  const display = (value) => value == null || value === '' ? '—' : escape(value);
  const list = (value) => Array.isArray(value) ? value : [];
  const level = (value) => /red|overdue|late|高|逾|延期/i.test(String(value ?? '')) ? 'red' : /yellow|warn|临期|中/i.test(String(value ?? '')) ? 'yellow' : /green|ok|正常/i.test(String(value ?? '')) ? 'green' : 'gray';
  const severityText = value => ({red:'延期',yellow:'临期',green:'正常',gray:'待核对'})[level(value)];
  const roleNames = {admin:'管理员',engineering:'工程',business:'业务',purchasing:'采购',warehouse:'仓库'};
  const views = {dashboard:'交付总览',portfolio:'项目排期',orders:'客户订单',materials:'物料齐套',tasks:'待办中心',imports:'数据导入',audit:'修改记录',settings:'设置与备份'};
  const phaseNames = {all:'全部阶段',rd:'研发打样',pp:'小批试产 PP',mp:'量产 MP',unclassified:'待归类'};
  const boardModeNames = {shared:'共用批次节点',independent:'各板独立排期'};
  const permissionNames = {orders_edit:'订单编辑',production_edit:'生产排期',materials_edit:'物料与仓库',tasks_manage:'待办处理',imports_manage:'数据导入',audit_view:'修改记录'};
  const stageNames = ['订单确认','BOM锁定','采购/到料','IQC','我方备料发料','厂商签收','SMT','DIP','烧录','测试','FQC','厂商完工','回厂/直发','客户签收'];
  const importTypes = {orders:'销售订单',bom:'BOM',inventory:'库存',purchases:'采购记录',receipts:'入库记录'};
  const state = {token:sessionStorage.getItem('pcba_token') || '',user:null,view:'dashboard',orderId:null,batchId:null,drawerBatchId:null,openBoardId:null,materialBatchId:null,materialTab:'requirements',overview:null,boardOverview:null,orders:[],legacyRecords:[],projects:[],selectedProject:'',phase:'all',activeStages:[],ordersFilter:'',ordersSort:'risk',ordersPage:1,portfolioFilter:'',portfolioSort:'risk',portfolioPage:1,portfolioExpanded:'',taskFilter:'open',taskScope:'all',taskAssignee:'',taskProject:'',taskAttention:false,taskSearch:'',taskPage:1,importType:'orders',importInspect:null,importPreview:null,importFile:null,importAiExtractionId:null,importAiConfigured:false,importMapping:{},importFixedFields:{},importHeaderRow:1,bomParentColumn:'',bomCatalogKey:'',bomCostQuantity:'1',snapshotAsOf:'',snapshotBasis:'',loading:false};
  function productionBatchName(batch) {
    const raw = String(batch?.batch_no || '');
    const id = Number(batch?.production_batch_id ?? batch?.batch_id ?? batch?.id);
    const known = batch?.legacy_imported == null && id > 0 ? state.orders.flatMap(order => list(order.lines).flatMap(line => list(line.production_batches))).find(item => Number(item.id) === id) : null;
    const imported = batch?.legacy_imported ?? known?.legacy_imported ?? false;
    const placeholder = imported && /^旧排期-(\d+)$/.exec(raw);
    return placeholder ? `第 ${placeholder[1]} 批` : raw || `批次 #${id || '—'}`;
  }
  let lastFocus = null;
  let toastTimer = null;
  let drawerLastFocus = null;
  let drawerRequest = 0;
  let importInspectRequest = 0;
  let importAiRequest = 0;
  let modalInitialState = null;
  let renderGeneration = 0;
  let portfolioGeneration = 0;

  function routeKey(user = state.user) { return user ? `pcba_route_${user.id}` : null; }
  function saveUiRoute() {
    const key = routeKey();
    if (!key) return;
    sessionStorage.setItem(key, JSON.stringify({
      view: state.view, selectedProject: state.selectedProject, phase: state.phase,
      orderId: state.orderId, batchId: state.batchId, drawerBatchId: state.drawerBatchId, openBoardId: state.openBoardId,
      materialBatchId: state.materialBatchId, materialTab: state.materialTab,
      taskFilter: state.taskFilter, taskScope: state.taskScope, taskAssignee: state.taskAssignee,
      taskProject: state.taskProject, taskAttention: state.taskAttention, importType: state.importType,
    }));
  }
  function restoreUiRoute(user) {
    let saved;
    try { saved = JSON.parse(sessionStorage.getItem(routeKey(user)) || '{}'); } catch (_) { saved = {}; }
    if (Object.hasOwn(views,saved.view)) state.view = saved.view;
    if (Object.hasOwn(phaseNames,saved.phase)) state.phase = saved.phase;
    if (typeof saved.selectedProject === 'string') state.selectedProject = saved.selectedProject;
    for (const key of ['orderId','batchId','drawerBatchId','openBoardId','materialBatchId']) {
      state[key] = Number.isSafeInteger(Number(saved[key])) && Number(saved[key]) > 0 ? Number(saved[key]) : null;
    }
    if (['requirements','inventory','receipts','purchases','substitutes'].includes(saved.materialTab)) state.materialTab = saved.materialTab;
    if (['open','done'].includes(saved.taskFilter)) state.taskFilter = saved.taskFilter;
    state.taskScope = ['mine','all'].includes(saved.taskScope) ? saved.taskScope : user.role === 'admin' ? 'all' : 'mine';
    if (typeof saved.taskAssignee === 'string') state.taskAssignee = saved.taskAssignee;
    if (typeof saved.taskProject === 'string') state.taskProject = saved.taskProject;
    state.taskAttention = saved.taskAttention === true;
    if (Object.hasOwn(importTypes,saved.importType)) state.importType = saved.importType;
  }

  function toast(message, isError = false) {
    const el = $('#toast');
    el.textContent = message;
    el.classList.toggle('error', isError);
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, 4800);
  }
  function setConnection(online) {
    $('#connection-dot').className = `connection-dot ${online ? 'online' : 'offline'}`;
    $('#connection-text').textContent = online ? '局域网服务已连接' : '服务连接中断';
  }
  async function api(path, options = {}) {
    const headers = {...(options.headers || {})};
    if (state.token) headers.Authorization = `Bearer ${state.token}`;
    if (options.body && !(options.body instanceof FormData) && typeof options.body !== 'string') {
      headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(options.body);
    }
    let response;
    try { response = await fetch(`/api${path}`, {...options, headers}); }
    catch (_) { setConnection(false); throw new Error('无法连接工作台服务。请检查主机是否运行，并确认局域网连接。'); }
    setConnection(true);
    const contentType = response.headers.get('content-type') || '';
    const payload = contentType.includes('application/json') ? await response.json() : await response.text();
    if (!response.ok) {
      if (response.status === 401 && state.user) { signOut(false); throw new Error('登录已失效，请重新登录。'); }
      const detail = typeof payload === 'object' ? payload.detail || payload.message : payload;
      const message = typeof detail === 'string' ? detail : Array.isArray(detail) ? detail.map(item => item.msg || String(item)).join('；') : `请求失败（${response.status}）`;
      const error = new Error(message);
      error.status = response.status;
      throw error;
    }
    return payload;
  }
  function showError(error, retry) {
    const content = $('#content');
    content.innerHTML = `<div class="error-panel"><strong>数据未能加载</strong><p>${escape(error.message)}</p><button class="button small" id="retry-view">重试</button></div>`;
    $('#retry-view').onclick = retry || (() => renderView());
  }
  function pageHead(kicker, title, subtitle = '', actions = '') {
    return `<div class="page-head"><div><p class="eyebrow">${escape(kicker)}</p><h1>${escape(title)}</h1>${subtitle ? `<p>${escape(subtitle)}</p>` : ''}</div><div class="page-actions">${actions}</div></div>`;
  }
  function empty(title, description, action = '') {
    return `<div class="empty"><strong>${escape(title)}</strong><p>${escape(description)}</p>${action}</div>`;
  }
  function badge(value, text) { return `<span class="badge ${level(value)}">${escape(text || severityText(value))}</span>`; }
  function panel(title, body, aside = '', tight = false) {
    return `<section class="panel${tight ? ' tight' : ''}"><div class="panel-head"><h2>${escape(title)}</h2>${aside}</div><div class="panel-body">${body}</div></section>`;
  }
  function formatForecast(item) {
    const status = item.forecast_status || item.prediction_status || item.forecast?.status;
    if (status === 'complete') return '<div><strong>已完成</strong><small>以实际发货或签收记录为准</small></div>';
    const confirmed = item.forecast_date || item.predicted_delivery_date || item.forecast?.formal_date || item.forecast?.date;
    const provisional = item.provisional_date || item.temporary_estimate_date || item.forecast?.provisional_date;
    const hasFormal = confirmed && !/unknown|pending|无法|待确认/i.test(String(status ?? ''));
    const explanation = item.forecast?.reason || item.forecast_reason || (status === 'unknown' ? '待厂商确认或补齐日期' : '正式预测');
    return `<div><strong>${hasFormal ? escape(date(confirmed)) : '无法确定'}</strong><small>${escape(explanation)}</small>${provisional ? `<small>临时估算 ${escape(date(provisional))} · 未经厂商确认</small>` : ''}</div>`;
  }
  function forecastDescription(status,reason) {
    return reason || ({unknown:'待补日期或厂商确认',forecast:'按厂商承诺预测',complete:'已完成'}[status] || status || '待补日期或厂商确认');
  }
  function materialReadyCell(item) {
    if (item.missing_eta) return '<span class="badge yellow">缺 ETA</span>';
    const ready = item.ready_date || item.eta;
    if (!ready) return '<span class="badge yellow">无法确定</span>';
    const note = Number(item.shortage || 0) <= 0 ? '已齐套' : item.eta_is_estimate ? '临时估算 · 待供应商确认' : '供应商已确认 ETA · 待 IQC';
    return `${escape(date(ready))}<span class="sub">${note}</span>`;
  }
  function sourceTime(item) { return item?.as_of || item?.snapshot_at || item?.imported_at || item?.created_at || null; }
  function formField(label, name, type = 'text', value = '', required = false, extra = '') {
    return `<label>${escape(label)}<input name="${attr(name)}" type="${attr(type)}" value="${attr(value ?? '')}" ${required ? 'required' : ''} ${extra}></label>`;
  }
  function formSelect(label, name, options, selected = '', required = false) {
    return `<label>${escape(label)}<select name="${attr(name)}" ${required ? 'required' : ''}>${options.map(([value, text]) => `<option value="${attr(value)}" ${String(value) === String(selected) ? 'selected' : ''}>${escape(text)}</option>`).join('')}</select></label>`;
  }
  function getForm(form) { return Object.fromEntries(new FormData(form).entries()); }
  function inputNum(value) { return value === '' || value == null ? null : String(value); }
  function inputDate(value) { return value || null; }
  function bindEstimateReason() {
    const estimate = $('[name=manual_estimate_date]',$('#modal-body'));
    const reason = $('[name=manual_estimate_reason]',$('#modal-body'));
    if (!estimate || !reason) return;
    const sync = () => { reason.required = Boolean(estimate.value); };
    estimate.addEventListener('input',sync);
    sync();
  }
  function validateEstimateReason(form,data) {
    if (!data.manual_estimate_date || data.manual_estimate_reason.trim()) return true;
    const reason = $('[name=manual_estimate_reason]',form);
    reason.setCustomValidity('填写人工预计日时，请填写预计依据。'); reason.reportValidity(); reason.setCustomValidity('');
    return false;
  }
  function fieldChanged(before, after) { return String(before ?? '') !== String(after ?? ''); }
  function changedFields(form, original, fields) {
    const data = getForm(form), changes = {};
    for (const field of fields) if (fieldChanged(original[field], data[field])) changes[field] = data[field] || null;
    return changes;
  }
  function withVersion(entity, changes, reason = '') { return {version:entity.version,changes,...(reason ? {reason} : {})}; }
  function showVersionConflict(container, reload) {
    let notice = $('[data-version-conflict]',container);
    if (!notice) {
      notice = document.createElement('div');
      notice.className = 'notice warning edit-conflict wide';
      notice.dataset.versionConflict = '';
      notice.setAttribute('role','alert');
      notice.innerHTML = '<strong>这条记录已被其他人修改</strong><p>本次填写仍保留在页面中。请先核对新记录；选择重新读取会放弃当前草稿。</p><button class="button small" type="button" data-conflict-reload>放弃草稿并重新读取</button>';
      container.prepend(notice);
    }
    $('[data-conflict-reload]',notice).onclick = reload;
    notice.scrollIntoView({block:'nearest'});
  }
  async function saveMutation(path, payload, method = 'POST', successText = '已保存') {
    try { const result = await api(path, {method,body:payload}); closeModal(); toast(successText); await renderView(); return result; }
    catch (error) {
      if (error.status === 409 && payload?.version != null && !$('#modal').hidden) {
        showVersionConflict($('#modal-body'),() => { closeModal(); renderView(); });
      }
      else { const msg = $('#modal-error'); if (msg) { msg.textContent = error.message; msg.hidden = false; } else toast(error.message, true); }
      return null;
    }
  }
  function openModal({kicker = '编辑',title,body,submitText = '保存',onSubmit,wide = false}) {
    lastFocus = document.activeElement;
    $('#modal-kicker').textContent = kicker;
    $('#modal-title').textContent = title;
    $('#modal-body').innerHTML = `${body}<p id="modal-error" class="inline-error" role="alert" hidden></p>`;
    $('#modal-actions').innerHTML = `<button class="button" id="modal-cancel">取消</button>${onSubmit ? `<button class="button primary" id="modal-submit">${escape(submitText)}</button>` : ''}`;
    $('#modal').hidden = false;
    modalInitialState = onSubmit ? modalEditState() : null;
    $('.modal-panel').classList.toggle('wide', wide);
    $('.modal-panel').style.width = wide ? 'min(860px,100%)' : '';
    $('#modal-close').onclick = requestCloseModal;
    $('#modal-cancel').onclick = requestCloseModal;
    if (onSubmit) $('#modal-submit').onclick = async () => {
      const form = $('#modal-body form');
      if (form && !form.reportValidity()) return;
      const button = $('#modal-submit'); button.disabled = true;
      try { await onSubmit(form); } finally { button.disabled = false; }
    };
    const first = $('#modal-body input, #modal-body select, #modal-body button');
    (first || $('#modal-close')).focus();
  }
  function modalEditState() {
    return JSON.stringify($$('input, select, textarea', $('#modal-body')).map(field =>
      field.type === 'checkbox' || field.type === 'radio' ? field.checked :
      field.type === 'file' ? Array.from(field.files || [], file => `${file.name}:${file.size}`) : field.value));
  }
  function modalHasUnsavedChanges() {
    return !$('#modal').hidden && modalInitialState !== null && modalEditState() !== modalInitialState;
  }
  function requestCloseModal() {
    if (!modalHasUnsavedChanges()) { closeModal(); return; }
    if ($('#modal-unsaved')) return;
    const prompt = document.createElement('div');
    prompt.id = 'modal-unsaved';
    prompt.className = 'modal-unsaved';
    prompt.setAttribute('role', 'alert');
    prompt.innerHTML = '<strong>修改尚未保存</strong><span>关闭后将丢失本次填写。</span><button class="button" type="button" id="modal-keep-editing">继续编辑</button><button class="button danger" type="button" id="modal-discard">放弃修改</button>';
    $('#modal-actions').prepend(prompt);
    $('#modal-keep-editing').onclick = () => { prompt.remove(); $('#modal-body input, #modal-body select, #modal-body textarea')?.focus(); };
    $('#modal-discard').onclick = closeModal;
    $('#modal-keep-editing').focus();
  }
  function closeModal() {
    $('#modal').hidden = true;
    $('#modal-body').innerHTML = '';
    $('#modal-unsaved')?.remove();
    modalInitialState = null;
    lastFocus?.focus?.();
  }
  window.addEventListener('beforeunload', event => {
    if (!modalHasUnsavedChanges()) return;
    event.preventDefault();
    event.returnValue = '';
  });
  $('#modal').addEventListener('click', event => { if (event.target.id === 'modal') requestCloseModal(); });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && !$('#modal').hidden) requestCloseModal();
    else if (event.key === 'Escape' && !$('#node-drawer').hidden) hideNodeDrawer();
    if (event.key === 'Tab' && !$('#modal').hidden) {
      const focusable = $$('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href]', $('#modal')).filter(el => el.offsetParent !== null);
      if (!focusable.length) return;
      if (event.shiftKey && document.activeElement === focusable[0]) { event.preventDefault(); focusable.at(-1).focus(); }
      else if (!event.shiftKey && document.activeElement === focusable.at(-1)) { event.preventDefault(); focusable[0].focus(); }
    }
    else if (event.key === 'Tab' && !$('#node-drawer').hidden) {
      const focusable = $$('button:not(:disabled), a[href]', $('#node-drawer')).filter(el => el.offsetParent !== null);
      if (!focusable.length) return;
      if (event.shiftKey && document.activeElement === focusable[0]) { event.preventDefault(); focusable.at(-1).focus(); }
      else if (!event.shiftKey && document.activeElement === focusable.at(-1)) { event.preventDefault(); focusable[0].focus(); }
    }
  });

  function signOut(message = true) {
    const key = routeKey();
    releaseImportPdf();
    hideNodeDrawer({save:false});
    if (key) sessionStorage.removeItem(key);
    state.token = ''; state.user = null; sessionStorage.removeItem('pcba_token');
    state.view = 'dashboard'; state.selectedProject = ''; state.phase = 'all';
    state.orderId = null; state.batchId = null; state.drawerBatchId = null;
    $('#workspace').hidden = true; $('#login').hidden = false;
    $('#auth-login-panel').hidden = false; $('#auth-register-panel').hidden = true;
    $('#auth-login-notice').hidden = false;
    refreshAuthStatus();
    if (message) toast('已退出工作台');
  }
  function signIn(token, user) {
    state.token = token; state.user = user; sessionStorage.setItem('pcba_token', token);
    restoreUiRoute(user);
    $('#login').hidden = true; $('#workspace').hidden = false;
    $('#user-name').textContent = user.display_name || user.username;
    $('#user-initial').textContent = (user.display_name || user.username || '用').slice(0, 1);
    loadProjects().catch(() => {}).then(async () => {
      await renderView();
      if (state.drawerBatchId) await showNodeDrawer(state.drawerBatchId, {restoring:true});
    });
  }
  $('#login-form').addEventListener('submit', async event => {
    event.preventDefault();
    const form = event.currentTarget, data = getForm(form), button = $('button[type=submit]', form);
    button.disabled = true; $('#login-error').hidden = true;
    try { const result = await api('/auth/login', {method:'POST',body:data}); signIn(result.token, result.user); }
    catch (error) { $('#login-error').textContent = error.message; $('#login-error').hidden = false; }
    finally { button.disabled = false; }
  });
  $('#logout').onclick = () => signOut();
  $('#refresh').onclick = async () => {
    await renderView();
    if (state.drawerBatchId) await showNodeDrawer(state.drawerBatchId,{restoring:true});
  };
  $('#register-open').onclick = () => { $('#auth-login-panel').hidden = true; $('#auth-register-panel').hidden = false; $('#auth-login-notice').hidden = true; $('#register-error').hidden = true; $('#register-success').hidden = true; $('#register-form [name=username]').focus(); };
  $('#register-back').onclick = () => { $('#auth-register-panel').hidden = true; $('#auth-login-panel').hidden = false; $('#auth-login-notice').hidden = false; $('#login-form [name=username]').focus(); };
  $('#register-form').addEventListener('submit', async event => {
    event.preventDefault();
    const form = event.currentTarget, data = getForm(form), error = $('#register-error'), success = $('#register-success'), button = $('button[type=submit]',form);
    error.hidden = true; success.hidden = true;
    if (data.password !== data.confirm) { error.textContent = '两次输入的密码不一致。'; error.hidden = false; return; }
    if (/[^\x21-\x7E]/.test(data.username)) { error.textContent = '登录账号请使用英文、数字或符号，不要使用中文或空格。'; error.hidden = false; return; }
    button.disabled = true;
    try {
      const result = await api('/auth/register',{method:'POST',body:{username:data.username.trim(),display_name:data.display_name.trim(),password:data.password}});
      success.textContent = result.message || '注册申请已提交，等待管理员审批后即可登录。'; success.hidden = false;
      form.reset();
    } catch (failure) { error.textContent = failure.message; error.hidden = false; }
    finally { button.disabled = false; }
  });
  async function refreshAuthStatus() {
    const button = $('#bootstrap-open');
    button.hidden = true;
    if (!['localhost','127.0.0.1','::1'].includes(location.hostname)) return;
    try { const status = await api('/auth/status'); button.hidden = Boolean(status.initialized); }
    catch (_) { button.hidden = true; }
  }
  $('#product-switcher').onchange = event => {
    state.selectedProject = event.target.value;
    state.phase = 'all';
    state.orderId = null; state.batchId = null; state.view = 'portfolio';
    hideNodeDrawer(); window.scrollTo(0,0); renderView();
  };
  $('#nav').addEventListener('click', event => {
    const button = event.target.closest('[data-view]');
    if (!button) return;
    state.view = button.dataset.view; state.orderId = null; state.batchId = null;
    hideNodeDrawer(); window.scrollTo(0,0);
    renderView();
  });
  function setNav() {
    $$('#nav [data-view]').forEach(el => { const active = el.dataset.view === state.view; el.classList.toggle('active', active); el.setAttribute('aria-current', active ? 'page' : 'false'); });
    $('#page-label').textContent = state.batchId ? '生产批次' : state.orderId ? '订单详情' : views[state.view];
  }
  async function renderView() {
    if (!state.user) return;
    const generation = ++renderGeneration;
    if (state.view !== 'imports' || state.batchId || state.orderId) releaseImportPdf();
    saveUiRoute();
    setNav();
    $('#content').innerHTML = '<div class="loading">正在读取共享数据…</div>';
    try {
      if (state.batchId) await renderBatch();
      else if (state.orderId) await renderOrderDetail();
      else if (state.view === 'dashboard') await renderDashboard();
      else if (state.view === 'portfolio') await renderPortfolio();
      else if (state.view === 'orders') await renderOrders();
      else if (state.view === 'materials') await renderMaterials();
      else if (state.view === 'tasks') await renderTasks();
      else if (state.view === 'imports') await renderImports();
      else if (state.view === 'audit') await renderAudit();
      else if (state.view === 'settings') await renderSettings();
      if (generation !== renderGeneration) return;
      $('#data-time').textContent = `更新于 ${new Date().toLocaleTimeString('zh-CN', {hour:'2-digit',minute:'2-digit'})}`;
      saveUiRoute();
    } catch (error) {
      if (generation !== renderGeneration) return;
      if (error.status === 404 && (state.batchId || state.orderId)) {
        state.batchId = null; state.orderId = null; saveUiRoute();
        return renderView();
      }
      showError(error);
    }
  }
  function openOrder(id) { hideNodeDrawer(); state.view = 'orders'; state.orderId = id; state.batchId = null; window.scrollTo(0,0); return renderView(); }
  function openBatch(id) { hideNodeDrawer(); state.view = 'orders'; state.batchId = id; window.scrollTo(0,0); return renderView(); }

  function projectKey(line) { return `line:${line.id}`; }
  function suggestedPhase(quantity) {
    const amount = Number(quantity);
    return !Number.isFinite(amount) || amount <= 0 ? 'unclassified' : amount <= 20 ? 'rd' : amount > 1000 ? 'mp' : 'pp';
  }
  function phaseMark(phase) { return `<span class="phase-mark ${attr(phase || 'unclassified')}">${escape(phaseNames[phase] || phaseNames.unclassified)}</span>`; }
  function projectSchedule(record) { return record.legacy_schedule || record.schedule || {}; }
  function phaseOf(batch, schedule) { return batch?.phase || schedule?.phase || schedule?.suggested_phase || suggestedPhase(batch?.quantity || schedule?.quantity); }
  function fillProjectSwitcher() {
    const select = $('#product-switcher');
    select.innerHTML = `<option value="">全部项目</option>${state.projects.map(project => `<option value="${attr(project.key)}">${escape(project.name)} · ${escape(project.order_no || '项目单号待补')}</option>`).join('')}`;
    if (state.selectedProject && !state.projects.some(project => project.key === state.selectedProject)) state.selectedProject = '';
    select.value = state.selectedProject;
  }
  async function loadProjects() {
    const generation = renderGeneration;
    const [orders, legacy] = await Promise.all([api('/orders'),api('/legacy/records')]);
    if (generation !== renderGeneration) return;
    state.orders = list(orders.orders || orders.items || orders);
    state.legacyRecords = list(legacy.records || legacy.items || legacy);
    const byKey = new Map();
    for (const order of state.orders) for (const line of list(order.lines)) {
      const key = projectKey(line);
      const productName = line.product_name || line.product_code || '未命名产品';
      const name = order.project_name && order.project_name !== productName ? `${order.project_name} / ${productName}` : order.project_name || productName;
      if (!byKey.has(key)) byKey.set(key,{key,name,code:line.product_code,order_id:order.id,order_no:order.order_no,customer:order.customer,project_mode:order.project_mode,owner_name:order.owner_name || order.legacy_owner_name,lines:[],batches:[],legacy:[]});
      const project = byKey.get(key);
      project.lines.push({...line,order_id:order.id,order_no:order.order_no});
      for (const batch of list(line.production_batches)) project.batches.push({...batch,order_id:order.id,order_line_id:line.id,order_no:order.order_no,project_mode:order.project_mode,owner_name:order.owner_name || order.legacy_owner_name,product_name:productName,project_display_name:name,product_code:line.product_code,delivery_batches:list(line.delivery_batches)});
    }
    for (const record of state.legacyRecords) {
      const schedule = projectSchedule(record), source = record.source || {};
      const matching = [...byKey.values()].find(project => project.lines.some(line => String(line.id) === String(record.order_line_id)));
      const name = schedule.name || source.name || record.name || '未命名项目';
      const key = matching?.key || `legacy:${record.id}`;
      if (!byKey.has(key)) byKey.set(key,{key,name,code:null,lines:[],batches:[],legacy:[]});
      const project = byKey.get(key);
      project.legacy.push(record);
      const batch = project.batches.find(item => String(item.id) === String(record.production_batch_id));
      if (batch) batch.sourceSchedule = schedule;
    }
    state.projects = [...byKey.values()].sort((a,b) => a.name.localeCompare(b.name,'zh-CN'));
    fillProjectSwitcher();
  }
  function formalDeliveryDate(delivery) {
    const forecast = delivery.forecast || {};
    if (['unknown','pending'].includes(forecast.status)) return null;
    return forecast.formal_date || delivery.forecast_date || null;
  }
  function dateDeviation(forecastDate,targetDate) {
    if (!forecastDate || !targetDate) return null;
    const forecast = Date.parse(`${String(forecastDate).slice(0,10)}T00:00:00Z`);
    const target = Date.parse(`${String(targetDate).slice(0,10)}T00:00:00Z`);
    return Number.isFinite(forecast) && Number.isFinite(target) ? Math.round((forecast - target) / 86400000) : null;
  }
  function projectDeadline(project,field,label) {
    const deliveries = project.lines.flatMap(line => list(line.delivery_batches));
    const candidate = deliveries.filter(item => item[field]).sort((a,b) => String(a[field]).localeCompare(String(b[field])))[0];
    const target = candidate?.[field], actual = candidate?.forecast?.status === 'complete' ? deliveryActualDate(candidate) : null;
    const compared = actual || (candidate && formalDeliveryDate(candidate));
    const delta = dateDeviation(compared,target);
    const prefix = actual ? '实际' : '正式预测';
    const status = !target ? '无法比较：目标日待补' : !compared ? '无法比较：正式预测待确认' : `${prefix} ${date(compared)} · ${delta > 0 ? `晚 ${delta} 个自然日` : delta < 0 ? `早 ${Math.abs(delta)} 个自然日` : '按期'}`;
    const tone = delta > 0 ? 'red' : !target || !compared ? 'gray' : 'green';
    return `<div class="project-deadline ${tone}"><span>${label}${deliveries.length > 1 ? ' · 最早一批' : ''}</span><strong>${escape(date(target))}</strong><small>${escape(status)}${candidate?.batch_no ? ` · ${escape(candidate.batch_no)}` : ''}</small></div>`;
  }
  function projectRdMilestones(project) {
    const batch = project.batches.find(item => phaseOf(item) === 'rd');
    if (!batch) return '';
    const targetLabel = batch.target_kind === 'customer' ? '客户要求打样日' : batch.target_kind === 'internal' ? '内部样机目标日' : '研发阶段目标';
    const estimate = batch.sourceSchedule?.estimated_delivery_date || null;
    const targetDelta = dateDeviation(estimate,batch.target_date);
    const targetStatus = !batch.target_date ? '目标日期待补' : !estimate ? '节点推算待核对' : `节点草案 ${date(estimate)} · ${targetDelta > 0 ? `晚 ${targetDelta} 个自然日` : targetDelta < 0 ? `早 ${Math.abs(targetDelta)} 个自然日` : '按期'}`;
    const boardDate = batch.board_received_at || batch.board_eta;
    const boardStatus = batch.board_received_at ? '实际到板' : batch.board_eta ? '预计到板，待实际签收' : '预计到板日待补';
    return `<div class="project-deadline ${targetDelta > 0 ? 'red' : 'gray'}"><span>${targetLabel}</span><strong>${escape(date(batch.target_date))}</strong><small>${escape(targetStatus)}</small></div>
      <div class="project-deadline ${batch.board_received_at ? 'green' : 'gray'}"><span>嘉立创到板进度</span><strong>${escape(date(boardDate))}</strong><small>${boardStatus}</small></div>`;
  }
  function batchMaterialProgress(materialBatch) {
    const items = list(materialBatch?.items);
    const reviewed = items.filter(item => item.review?.status === 'current').length;
    const stale = items.filter(item => item.review?.status === 'stale').length;
    const shortage = items.filter(item => Number(item.shortage || 0) > 0).length;
    return {total:items.length,reviewed,stale,shortage};
  }
  function boardLineInfo(line, overview = state.boardOverview) {
    const summary = overview?.order_lines?.[String(line?.id)];
    return summary ? {...summary,board_schedule_mode:summary.board_schedule_mode || line?.board_schedule_mode || 'shared'} :
      {order_line_id:line?.id,confirmed:Boolean(line?.board_set_confirmed),board_schedule_mode:line?.board_schedule_mode || 'shared',boards:list(line?.boards)};
  }
  function batchBoardInfo(batch, overview = state.boardOverview) {
    return overview?.production_batches?.[String(batch?.id)] ||
      {production_batch_id:batch?.id,bom_scope:batch?.bom_scope || 'single',board_schedule_mode:batch?.board_schedule_mode || 'shared',bom_locked:Boolean(batch?.bom_locked),board_set_revision:batch?.board_set_revision,board_set_confirmed:Boolean(batch?.board_set_confirmed_at),boards:list(batch?.boards)};
  }
  function boardReviewProgress(board, materialBatch) {
    const items = list(materialBatch?.items).filter(item => list(item.bom_lines).some(line => Number(line.batch_board_id) === Number(board.id)));
    return {total:items.length,reviewed:items.filter(item => item.review?.status === 'current').length};
  }
  function boardQuantityStatus(board) {
    const difference = Number(board.planned_quantity ?? board.required_quantity ?? 0) - Number(board.required_quantity ?? 0);
    return difference < 0 ? `<span class="board-quantity-gap">缺 ${number(-difference)} 片</span>` : difference > 0 ? `<span class="board-quantity-spare">备损余量 +${number(difference)} 片</span>` : '<span>刚好覆盖</span>';
  }
  function boardForecastLabel(forecast) {
    if (!forecast || forecast.status === 'unknown') return '无法确定';
    return forecast.status === 'complete' ? `已完成${forecast.formal_date ? ` · ${date(forecast.formal_date)}` : ''}` : date(forecast.formal_date);
  }
  function latestBoardFinish(summary) {
    const boards = list(summary?.boards);
    return boards.length && boards.every(board => board.forecast?.formal_date && board.forecast.status !== 'unknown') ? boards.map(board => board.forecast.formal_date).sort().at(-1) : null;
  }
  function boardSchedulePanels(batch,details) {
    if (batch.board_schedule_mode !== 'independent' || batch.bom_scope !== 'multi') return '';
    if (!details.length) return `<div class="notice warning"><p>本批尚未启用逐板排期。请先确认项目板卡组成，再更新本批草案。</p></div>`;
    return `<div class="board-schedule-intro">客户成品 <strong>${number(batch.quantity)} 件</strong>；每块板分别排期、登记完工片数。客户交付仍按成品数量计算一次。本批正式完工取所有必需板卡中较晚的可确认日期，缺任一板资料则为“无法确定”。</div><div class="board-schedule-list">${details.map(({board,stages,forecast}) => `<section class="board-schedule-card" data-board-schedule="${attr(board.id)}"><div class="board-schedule-head"><div><span class="board-ref">${escape(board.board_ref)}</span><strong>${escape(board.board_name)}</strong><small>${escape(board.bom_product_code)} · 每套成品用 ${number(board.quantity_per_product)} 块</small></div><div class="board-schedule-head-actions"><span class="badge ${forecast?.status === 'unknown' ? 'yellow' : 'green'}">${escape(boardForecastLabel(forecast))}</span>${state.user?.role === 'admin' || state.user?.permissions?.production_edit !== false ? `<button class="button small" type="button" data-edit-board-schedule="${attr(board.id)}">编辑本板</button>` : ''}</div></div><div class="board-schedule-metrics"><div><span>成品对应需板</span><strong>${number(board.required_quantity)} 片</strong></div><div><span>本板计划生产</span><strong>${number(board.planned_quantity)} 片</strong><small>${boardQuantityStatus(board)}</small></div><div><span>本板实际完工</span><strong>${number(board.completed_quantity || 0)} 片</strong></div><div><span>外协厂 / 承诺完工</span><strong>${escape(board.vendor || '待确认')}</strong><small>${escape(date(board.commit_finish))}</small></div></div>${list(board.conflicts).length ? `<p class="board-schedule-warning">${list(board.conflicts).length} 项同厂档期重叠 · ${board.conflict_ack_at ? `已确认可并行：${escape(board.conflict_ack_reason || '已核对')}` : `待确认是否可并行${state.user?.role === 'admin' || state.user?.permissions?.production_edit !== false ? ` <button class="button small" type="button" data-board-conflict-ack="${attr(board.id)}">确认可并行</button>` : ''}`}</p>` : ''}<div class="board-schedule-forecast">${escape(forecast?.reason || '请核对本板 BOM、物料和厂商承诺。')}</div><details class="board-stage-disclosure"><summary>查看本板节点 · ${number(board.stage_progress?.completed || 0)}/${number(board.stage_progress?.total || stages.length)} 已完成</summary>${stages.length ? stageTimeline(stages,board.id) : empty('本板节点待生成','请检查阶段模板及本板排期起点。')}<div class="board-schedule-actions">${state.user?.role === 'admin' || state.user?.permissions?.production_edit !== false ? `<button class="button small" type="button" data-add-board-stage="${attr(board.id)}">+ 本板节点</button><button class="button small" type="button" data-board-completion="${attr(board.id)}">登记本板完工片数</button>` : ''}</div></details></section>`).join('')}</div>`;
  }
  async function loadBoardSchedules(batch) {
    if (batch.board_schedule_mode !== 'independent' || batch.bom_scope !== 'multi') return [];
    const boardIds = list(batch.boards).map(board => board.id);
    const results = await Promise.allSettled(boardIds.map(id => api(`/batch-boards/${encodeURIComponent(id)}`)));
    return results.map((result,index) => result.status === 'fulfilled' ? result.value : {board:batch.boards[index],stages:[],forecast:{status:'unknown',reason:`本板节点暂时无法读取：${result.reason.message}`}});
  }
  function projectBoardStrip(line, boardInfo, batchSummaries) {
    const boards = list(boardInfo?.boards);
    if (!boards.length) return '';
    const mode = boardInfo.board_schedule_mode || line.board_schedule_mode || 'shared';
    const batchBoards = batchSummaries.flatMap(summary => list(summary?.boards));
    return `<section class="project-board-strip" aria-label="${attr(line.product_name || line.product_code)} 的项目板卡">
      <div class="project-board-strip-head"><div><strong>项目板卡 · ${escape(boardModeNames[mode] || boardModeNames.shared)}</strong><span>客户产品 ${number(line.quantity)} 件；逐板核对 BOM 与物料。${mode === 'shared' ? '工序节点按生产批次共用。' : '每板独立记录厂商、节点和完工数量。'}客户交付数量只计算一次。</span></div><div>${boardInfo.confirmed ? '<span class="badge green">组成已确认</span>' : '<span class="badge yellow">组成待确认</span>'}<button class="button small" type="button" data-manage-boards="${attr(line.id)}">管理板卡</button></div></div>
      <div class="project-board-grid">${boards.map(board => {
        const activated = batchBoards.filter(item => Number(item.order_line_board_id) === Number(board.id));
        const locked = activated.filter(item => item.bom_locked).length;
        return `<div class="project-board-card"><div class="project-board-id"><span>${escape(board.board_ref)}</span><strong>${escape(board.board_name)}</strong></div><div class="project-board-spec"><span>BOM 母件</span><strong>${escape(board.bom_product_code)}</strong></div><div class="project-board-spec"><span>每件产品用板</span><strong>${number(board.quantity_per_product)} 块</strong></div><div class="project-board-spec"><span>批次 BOM</span><strong>${activated.length ? `${locked} / ${activated.length} 批已锁定` : '待批次启用'}</strong></div></div>`;
      }).join('')}</div></section>`;
  }
  function batchBoardPanel(batch, materialBatch, lineInfo, boardDetails = []) {
    const boards = list(batch.boards);
    const planned = list(lineInfo?.boards).length;
    if (batch.bom_scope !== 'multi' && !planned) return '';
    if (batch.bom_scope !== 'multi') return `<section class="batch-board-panel"><div class="batch-board-heading"><div><span>板卡与 BOM</span><h2>本批尚未启用板卡</h2><p>${batch.bom_locked ? '本批已有单板 BOM 历史快照；多板组成请用于后续新批次。' : lineInfo?.confirmed ? `项目已确认 ${planned} 块板，可为本批启用。` : `项目有 ${planned} 块板待确认。`}</p></div><button class="button small" type="button" data-batch-boards="${attr(batch.id)}">查看板卡配置 →</button></div></section>`;
    const historical = lineInfo && Number(batch.board_set_revision) !== Number(lineInfo.board_set_revision);
    const independent = batch.board_schedule_mode === 'independent';
    const modeChanged = lineInfo && (lineInfo.board_schedule_mode || 'shared') !== (batch.board_schedule_mode || 'shared');
    return `<section class="batch-board-panel"><div class="batch-board-heading"><div><span>板卡与 BOM · 本批快照第 ${escape(batch.board_set_revision || 1)} 版</span><h2>${boards.length} 块板，${boards.filter(board => board.bom_locked).length} 块已锁 BOM</h2><p>逐板核对 BOM 与物料；${independent ? '本批各板独立排期，原有共用节点保留供查看。' : '当前工序节点按生产批次共用。'}客户交付按本批 ${number(batch.quantity)} 件产品计算一次。${historical ? `项目当前组成是第 ${escape(lineInfo.board_set_revision)} 版，本批继续使用历史快照。` : ''}</p></div><button class="button small" type="button" data-batch-boards="${attr(batch.id)}">管理逐板 BOM →</button></div><div class="batch-board-grid">${boards.map(board => {
      const review = boardReviewProgress(board,materialBatch);
      return `<div class="batch-board-item"><div class="batch-board-title"><span class="board-ref">${escape(board.board_ref)}</span><strong>${escape(board.board_name)}</strong>${board.bom_locked ? '<span class="badge green">已锁 BOM</span>' : '<span class="badge yellow">待锁 BOM</span>'}</div><div class="batch-board-detail"><span>${escape(board.bom_product_code)} · BOM ${escape(board.bom_version || '待选择')}</span><span>成品对应 ${number(board.required_quantity)} 片</span>${independent ? `<span>本板计划 ${number(board.planned_quantity)} 片 · ${boardQuantityStatus(board)}</span>` : ''}<span>${board.bom_locked ? `${review.reviewed}/${review.total} 项物料已核对` : '锁定 BOM 后逐项核料'}</span></div><div class="batch-board-item-actions">${!board.bom_locked && batch.board_set_confirmed_at ? `<button class="button small primary" type="button" data-lock-batch-board="${attr(board.id)}">选择并锁定 BOM</button>` : board.bom_locked ? `<button class="button small" type="button" data-board-materials="${attr(batch.id)}">逐项核料 →</button>` : `<button class="button small" type="button" data-batch-boards="${attr(batch.id)}">先启用本批板卡</button>`}</div></div>`;
    }).join('')}</div>${independent ? `<div class="batch-board-schedules">${boardSchedulePanels(batch,boardDetails)}</div>` : ''}${modeChanged ? `<p class="board-history-note">项目当前排期方式：${escape(boardModeNames[lineInfo.board_schedule_mode] || boardModeNames.shared)}；本批保留${escape(boardModeNames[batch.board_schedule_mode] || boardModeNames.shared)}快照。</p>` : ''}</section>`;
  }
  function materialBoardStrip(boardInfo,materialBatch) {
    const boards = list(boardInfo?.boards);
    if (!boards.length || boardInfo?.bom_scope !== 'multi') return '';
    return `<div class="material-board-strip" aria-label="本批板卡核料进度">${boards.map(board => {
      const progress = boardReviewProgress(board,materialBatch);
      return `<div><span class="board-ref">${escape(board.board_ref)}</span><strong>${escape(board.board_name)}</strong><small>${board.bom_locked ? `${progress.reviewed}/${progress.total} 项物料已核对` : 'BOM 待锁定'}</small></div>`;
    }).join('')}</div>`;
  }
  function portfolioBatchRow(batch, linkage, materialBatch, projectKeyValue, stageProgress, boardSummary, plannedBoards = false) {
    const phase = phaseOf(batch), isRd = phase === 'rd';
    const milestone = batch.sourceSchedule?.estimated_delivery_date || batch.target_date || batch.commit_finish || null;
    const selected = Number(state.drawerBatchId) === Number(batch.id);
    const material = batchMaterialProgress(materialBatch);
    const multi = boardSummary?.bom_scope === 'multi';
    const boards = list(boardSummary?.boards);
    const bomText = multi ? !boardSummary.board_set_confirmed ? '本批板卡组成待启用' : `${boards.filter(board => board.bom_locked).length}/${boards.length} 块板 BOM 已锁定` : plannedBoards ? batch.bom_locked ? '单板 BOM 快照' : '本批待启用板卡' : isRd ? '打样阶段' : batch.bom_locked ? `BOM ${batch.bom_version || '已锁定'}` : linkage?.status === 'unlinked' || !linkage ? 'BOM 未关联' : `BOM ${linkage.bom_version} · 待锁定`;
    const materialText = multi ? material.total ? `${material.reviewed}/${material.total} 项已核对${!batch.bom_locked ? ' · 仍有板卡未锁' : ''}` : '逐板锁定后核料' : isRd ? '按打样节点跟进' : !batch.bom_locked ? '锁定后逐项核料' : `${material.reviewed}/${material.total} 项已核对${material.stale ? ` · ${material.stale} 项需复核` : ''}${material.shortage ? ` · ${material.shortage} 项缺料` : ''}`;
    const independent = boardSummary?.board_schedule_mode === 'independent' && multi;
    const boardFinish = independent ? latestBoardFinish(boardSummary) : null;
    const stageText = independent ? boards.length ? `${boards.reduce((total,board) => total + Number(board.stage_progress?.completed || 0),0)}/${boards.reduce((total,board) => total + Number(board.stage_progress?.total || 0),0)} 逐板节点` : '板卡排期待启用'
      : stageProgress?.total ? `${stageProgress.completed}/${stageProgress.total} 节点 · ${stageProgress.current_name || '全部节点已完成'}` : '节点待生成';
    return `<div class="project-phase-row${selected ? ' is-selected' : ''}">
      <div class="project-phase-name">${phaseMark(phase)}<strong>${escape(productionBatchName(batch))}</strong><small>${number(batch.quantity)} 件 · ${escape(stageText)} · ${independent ? `板卡最晚预计 ${escape(boardFinish ? date(boardFinish) : '无法确定')}` : `目标 ${escape(date(milestone))}`}</small></div>
      <div class="project-batch-state"><span>本批 BOM</span><strong class="${!isRd && !batch.bom_locked ? 'needs-action' : ''}">${escape(bomText)}</strong></div>
      <div class="project-batch-state"><span>逐料核对</span><strong>${escape(materialText)}</strong></div>
      <div class="project-batch-actions">${multi || plannedBoards ? `<button class="button small" type="button" data-batch-boards="${attr(batch.id)}">${multi ? boardSummary.board_set_confirmed ? '逐板 BOM' : '更新板卡' : '启用板卡'}</button><button class="button small" type="button" data-project-materials="${attr(batch.id)}">逐项核料</button>` : !isRd ? `<button class="button small" type="button" data-project-bom="${attr(projectKeyValue)}" data-batch-id="${attr(batch.id)}">${batch.bom_locked ? '查看 BOM' : linkage?.status !== 'unlinked' ? '锁定 BOM' : '关联 BOM'}</button><button class="button small" type="button" data-project-materials="${attr(batch.id)}">逐项核料</button>` : ''}<button class="button small project-open" type="button" data-open-project="${attr(batch.id)}" aria-controls="node-drawer" aria-expanded="${selected ? 'true' : 'false'}">${independent ? '逐板节点' : '生产节点'} →</button></div>
    </div>`;
  }
  function hideNodeDrawer({save = true} = {}) {
    drawerRequest++;
    const drawer = $('#node-drawer');
    if (!drawer) return;
    const wasOpen = !drawer.hidden;
    drawer.hidden = true;
    document.body.classList.remove('drawer-open');
    state.drawerBatchId = null;
    $$('[data-open-project][aria-expanded="true"]').forEach(button => {
      button.setAttribute('aria-expanded','false');
      button.closest('.project-phase-row')?.classList.remove('is-selected');
    });
    if (save) saveUiRoute();
    if (wasOpen && drawerLastFocus?.isConnected) drawerLastFocus.focus();
    drawerLastFocus = null;
  }
  function nodeStatus(stage) {
    if (stage.is_complete) return ['green','已完成'];
    return {done:['green','已完成'],in_progress:['blue','进行中'],skipped:['gray','跳过'],pending:['gray','待开始']}[stage.status] || ['gray','待核对'];
  }
  function timelineGridPosition(index,columns) {
    const row = Math.floor(index / columns);
    const column = index % columns;
    return [row + 1,row % 2 ? columns - column : column + 1];
  }
  function stageTimeline(stages,boardId = null) {
    if (!stages.length) return '';
    const done = stage => Boolean(stage.is_complete || stage.status === 'done');
    const completeCount = stages.filter(done).length;
    const activeIndex = stages.findIndex(stage => !done(stage) && stage.status === 'in_progress');
    const currentIndex = activeIndex >= 0 ? activeIndex : stages.findIndex(stage => !done(stage) && stage.status !== 'skipped');
    const currentName = currentIndex < 0 ? '全部节点已处理' : stages[currentIndex].name;
    const unassigned = stages.filter(stage => !stage.owner_id).length;
    return `<section class="stage-timeline" data-stage-timeline ${boardId == null ? '' : `data-board-stage-timeline="${attr(boardId)}"`} aria-label="生产节点进度轴">
      <div class="stage-timeline-heading"><div><strong>${boardId == null ? '批次节点进度' : '本板节点进度'}</strong><span>${completeCount} / ${stages.length} 已完成 · ${activeIndex >= 0 ? '进行中' : '下一节点'}：${escape(currentName)}${unassigned ? ` · ${unassigned} 个节点待指派` : ''}</span><span class="stage-timeline-finish">节点草案最早完工 ${escape(date(stages.at(-1)?.planned_finish))} · 需核对到料/厂商承诺</span></div><small>按顺序查看全部节点 · 点击圆点编辑</small></div>
      <div class="stage-timeline-scroll"><div class="stage-timeline-track"><svg class="stage-timeline-links" aria-hidden="true" focusable="false"></svg>${stages.map((stage,index) => {
        const complete = done(stage), current = index === currentIndex || stage.status === 'in_progress';
        const progress = complete ? 'done' : stage.status === 'skipped' ? 'skipped' : current ? 'current' : 'pending';
        const progressLabel = complete ? '✓ 已完成' : stage.status === 'skipped' ? '已跳过' : stage.status === 'in_progress' ? '● 进行中' : current ? '● 下一节点' : '待开始';
        const [row6,col6] = timelineGridPosition(index,6);
        const [row4,col4] = timelineGridPosition(index,4);
        const [row2,col2] = timelineGridPosition(index,2);
        return `<button class="stage-timeline-step is-${progress}" type="button" data-timeline-stage="${attr(stage.id)}" style="--row-6:${row6};--col-6:${col6};--row-4:${row4};--col-4:${col4};--row-2:${row2};--col-2:${col2}" ${index === currentIndex ? 'aria-current="step"' : ''} aria-label="打开第 ${index + 1} 节点 ${attr(stage.name)}，${complete ? '已完成' : stage.status === 'in_progress' ? '进行中' : current ? '下一节点' : stage.status === 'skipped' ? '已跳过' : '待开始'} 的编辑窗口">
          <span class="stage-timeline-point" aria-hidden="true">${complete ? '✓' : index + 1}</span><span class="stage-timeline-name">${escape(stage.name)}</span><span class="stage-timeline-state">${escape(progressLabel)}</span><span class="stage-timeline-date">计划 ${escape(date(stage.planned_finish))}${stage.actual_finish ? `<br>实际 ${escape(date(stage.actual_finish))}` : ''}</span>${stage.owner_display_name || stage.owner_name ? `<span class="stage-timeline-owner">负责人 ${escape(stage.owner_display_name || stage.owner_name)}</span>` : ''}</button>`;
      }).join('')}</div></div>
    </section>`;
  }
  const timelineObservers = new WeakMap();
  function bindStageTimeline(root,stages,users,batchId,boardId = null) {
    const selector = boardId == null ? '[data-stage-timeline]:not([data-board-stage-timeline])' : `[data-board-stage-timeline="${CSS.escape(String(boardId))}"]`;
    $$(selector,root).forEach(timeline => {
      timelineObservers.get(timeline)?.disconnect();
      const track = $('.stage-timeline-track',timeline);
      const svg = $('.stage-timeline-links',timeline);
      const buttons = $$('[data-timeline-stage]',timeline);
      const drawLinks = () => {
        if (!timeline.isConnected || !track.clientWidth || !track.clientHeight) return;
        const rect = track.getBoundingClientRect();
        const points = buttons.map(button => {
          const circle = $('.stage-timeline-point',button).getBoundingClientRect();
          return {x:circle.left - rect.left + circle.width / 2,y:circle.top - rect.top + circle.height / 2};
        });
        const width = track.clientWidth, height = track.clientHeight;
        svg.setAttribute('viewBox',`0 0 ${width} ${height}`);
        svg.innerHTML = points.slice(0,-1).map((point,index) => {
          const next = points[index + 1];
          const turn = Math.abs(next.y - point.y) > 2;
          const edge = point.x > width / 2 ? width - 3 : 3;
          const path = turn && Math.abs(next.x - point.x) > 2 ? `M ${point.x} ${point.y} H ${edge} V ${next.y} H ${next.x}`
            : `M ${point.x} ${point.y} L ${next.x} ${next.y}`;
          const completed = stages[index].is_complete || stages[index].status === 'done';
          return `<path class="stage-timeline-link${completed ? ' is-done' : ''}" d="${path}"/>`;
        }).join('');
      };
      const observer = new ResizeObserver(drawLinks);
      observer.observe(track);
      timelineObservers.set(timeline,observer);
      requestAnimationFrame(drawLinks);
      timeline.onclick = event => {
        const button = event.target.closest('[data-timeline-stage]');
        if (!button) return;
        const stage = stages.find(item => Number(item.id) === Number(button.dataset.timelineStage));
        if (stage) boardId == null ? openNodeEditor(batchId,stage,users) : openBoardStageEditor(boardId,stage,users);
      };
    });
  }
  function bindSchedulePreview(form,{productCode,batchId = null,boardEta = null,boardReceivedAt = null}) {
    const modeField = $('[name="schedule_mode"]',form)?.closest('label');
    if (!modeField) return;
    modeField.insertAdjacentHTML('afterend','<div class="schedule-preview wide" data-schedule-preview role="status" aria-live="polite"></div>');
    const output = $('[data-schedule-preview]',form);
    let sequence = 0;
    let timer;
    const update = () => {
      clearTimeout(timer);
      const ticket = ++sequence;
      const start = $('[name="schedule_start"]',form)?.value;
      if (!start) {
        output.innerHTML = '<span>填写排期起点后，按当前阶段的节点耗时显示最早完工草案。</span>';
        return;
      }
      output.innerHTML = '<span>正在推算节点日期…</span>';
      timer = setTimeout(async () => {
        const params = new URLSearchParams({product_code:productCode,phase:$('[name="phase"]',form).value,
          schedule_start:start,schedule_mode:$('[name="schedule_mode"]',form).value});
        if (batchId) params.set('batch_id',batchId);
        const eta = $('[name="board_eta"]',form)?.value || boardEta;
        const received = $('[name="board_received_at"]',form)?.value || boardReceivedAt;
        if (eta) params.set('board_eta',eta);
        if (received) params.set('board_received_at',received);
        try {
          const preview = await api(`/schedule-preview?${params}`);
          if (ticket !== sequence || !output.isConnected) return;
          const phaseNote = preview.phase_change_pending ? '阶段更改后保留原节点；如需新模板，保存后选择“重排计划”。' : '';
          output.innerHTML = `<span>节点草案最早完工</span><strong>${escape(date(preview.earliest_finish))}</strong><small>${preview.nodes?.length || 0} 个节点 · ${preview.source === 'current_stages' ? '按本批节点耗时' : '按产品阶段模板耗时'} · 需核对到料/厂商承诺，非正式客户交期${phaseNote ? ` · ${escape(phaseNote)}` : ''}</small>`;
        } catch (error) {
          if (ticket !== sequence || !output.isConnected) return;
          output.innerHTML = `<span>无法推算节点草案：${escape(error.message)}</span>`;
        }
      },120);
    };
    form.addEventListener('input',event => {
      if (['schedule_start','schedule_mode','phase','quantity','board_eta','board_received_at'].includes(event.target.name)) update();
    });
    form.addEventListener('change',event => {
      if (['schedule_start','schedule_mode','phase','board_eta','board_received_at'].includes(event.target.name)) update();
    });
    update();
  }
  function nodeOwnerOptions(stage,users) {
    const active = users.filter(user => user.active && user.status === 'active');
    const current = active.some(user => Number(user.id) === Number(stage.owner_id));
    const name = stage.owner_display_name || stage.owner_name || '';
    const unassignedLabel = stage.owner_id ? '取消指派' : name ? `${name}（待绑定账号）` : '请选择责任人';
    const locked = stage.owner_id && (stage.is_complete || stage.status === 'done') ? ' disabled' : '';
    return `<option value=""${locked}>${escape(unassignedLabel)}</option>` +
      (!current && stage.owner_id ? `<option value="${attr(stage.owner_id)}" selected disabled>${escape(name || `账号 #${stage.owner_id}`)}（已停用）</option>` : '') +
      active.map(user => `<option value="${attr(user.id)}" ${Number(user.id) === Number(stage.owner_id) ? 'selected' : ''}>${escape(user.display_name || user.username)}</option>`).join('');
  }
  function nodeCard(stage,index,editable,users = []) {
    const [tone,label] = nodeStatus(stage);
    const complete = Boolean(stage.is_complete || stage.status === 'done');
    const owner = stage.owner_display_name || stage.owner_name || '待指定';
    const dates = [
      ['计划开始',stage.planned_start],['计划完成',stage.planned_finish],
      ['实际到料',stage.actual_material_at],['实际开始',stage.actual_start],['实际完成',stage.actual_finish],
    ];
    return `<article class="node-card" data-node-id="${attr(stage.id)}" data-open-node="${attr(stage.id)}" tabindex="0" aria-label="打开 ${attr(stage.name)} 节点编辑窗口">
      <div class="node-card-top"><div><h3>${index + 1}. ${escape(stage.name)}</h3><small>${escape(stage.stage_type === 'milestone' ? '里程碑' : '生产工序')} · 耗时 ${number(stage.duration_days)} 天</small></div><span class="badge ${tone}">${label}</span></div>
      <div class="node-owner-row"><label>节点责任人${editable ? `<select data-node-owner="${attr(stage.id)}" aria-label="${attr(stage.name)} 责任人">${nodeOwnerOptions(stage,users)}</select>` : `<strong>${escape(owner)}</strong>`}</label>
        <label class="node-complete"><input type="checkbox" data-node-complete="${attr(stage.id)}" ${complete ? 'checked' : ''} ${!editable || (!stage.owner_id && !complete) ? 'disabled' : ''} aria-label="标记 ${attr(stage.name)} 已完成"><span>已完成</span></label></div>
      ${!stage.owner_id && !complete ? '<p class="node-owner-hint">先指派工作台账号，才能勾选完成。</p>' : stage.completed_by_name ? `<p class="node-owner-hint">由 ${escape(stage.completed_by_name)} 登记完成</p>` : ''}
      <dl class="node-meta">${dates.map(([name,value]) => `<div><dt>${name}</dt><dd class="mono">${escape(date(value))}</dd></div>`).join('')}</dl>
      ${stage.note ? `<p class="node-note">${escape(stage.note)}</p>` : ''}
      ${editable ? `<div class="node-card-actions"><button class="button small" data-edit-node="${attr(stage.id)}" type="button">编辑此节点</button></div>` : ''}
    </article>`;
  }
  async function showNodeDrawer(batchId,{restoring = false} = {}) {
    const requestId = ++drawerRequest;
    const previousScroll = restoring ? $('#node-drawer-body').scrollTop : 0;
    if (!restoring) drawerLastFocus = document.activeElement;
    try {
      const [detail,stageResult,userResult] = await Promise.all([
        api(`/production-batches/${encodeURIComponent(batchId)}`),
        api(`/production-batches/${encodeURIComponent(batchId)}/stages`),
        api('/users').catch(() => []),
      ]);
      if (requestId !== drawerRequest) return;
      const batch = detail.batch || detail;
      const boardDetails = await loadBoardSchedules(batch);
      if (requestId !== drawerRequest) return;
      const independent = batch.board_schedule_mode === 'independent' && batch.bom_scope === 'multi';
      const stages = list(stageResult.stages || stageResult.items || stageResult);
      const users = list(userResult.users || userResult.items || userResult);
      const projectBatch = state.projects.flatMap(project => project.batches).find(item => Number(item.id) === Number(batchId));
      const projectName = projectBatch?.project_display_name || batch.order_line?.product_name || batch.product_name || productionBatchName(batch);
      const owner = projectBatch?.owner_name || '待指定';
      const stageEstimate = stages.at(-1)?.planned_finish || null;
      const target = batch.target_date || null;
      const sourceEstimate = projectBatch?.sourceSchedule?.estimated_delivery_date || null;
      const deliveries = list(projectBatch?.delivery_batches);
      const allocationIds = new Set(list(batch.allocations).map(item => String(item.delivery_batch_id)));
      const relatedDeliveries = deliveries.filter(item => allocationIds.has(String(item.id)));
      const earliest = field => relatedDeliveries.map(item => item[field]).filter(Boolean).sort()[0] || null;
      const rd = phaseOf(batch) === 'rd';
      const summaryDates = independent ? `<div><span>客户成品</span><strong>${number(batch.quantity)} 件</strong></div><div><span>本批排期方式</span><strong>各板独立排期</strong></div><div><span>汇总正式完工</span><strong>${escape(boardForecastLabel(batch.forecast))}</strong></div><div><span>板卡组成</span><strong>${boardDetails.length} 块</strong></div>` : rd ? `<div><span>排期起点</span><strong>${escape(date(batch.schedule_start || batch.commit_start))}</strong></div>
        <div><span>节点草案完工</span><strong>${escape(date(stageEstimate || sourceEstimate))}</strong></div>
        <div><span>${batch.target_kind === 'customer' ? '客户要求打样日' : batch.target_kind === 'internal' ? '内部样机目标日' : '研发目标类型待选'}</span><strong>${escape(date(target))}</strong></div>
        <div><span>嘉立创到板</span><strong>${escape(date(batch.board_received_at || batch.board_eta))}</strong></div>` : `<div><span>排期起点</span><strong>${escape(date(batch.schedule_start || batch.commit_start))}</strong></div>
        <div><span>节点推算完工</span><strong>${escape(date(stageEstimate || sourceEstimate))}</strong></div>
        <div><span>阶段目标日</span><strong>${escape(date(target))}</strong></div>
        <div><span>厂商承诺完工</span><strong>${escape(date(batch.commit_finish))}</strong></div>
        <div><span>关联交付的客户要求日</span><strong>${escape(date(earliest('due_date')))}</strong></div>
        <div><span>关联交付的业务承诺日</span><strong>${escape(date(earliest('promised_date')))}</strong></div>`;
      const editable = state.user?.role === 'admin' || state.user?.permissions?.production_edit !== false;
      state.drawerBatchId = Number(batchId);
      saveUiRoute();
      $('#node-drawer-title').textContent = projectName;
      $('#node-drawer-subtitle').textContent = `${phaseNames[phaseOf(batch)] || phaseNames.unclassified} · ${number(batch.quantity)} 件 · 业务负责人 ${owner}`;
      $('#node-drawer-body').innerHTML = `<div class="node-drawer-summary${rd ? ' is-rd' : ''}${independent ? ' is-independent' : ''}">${summaryDates}</div>${!allocationIds.size && !rd ? '<div class="project-association-notice">本生产批次尚未关联客户交付数量，无法将客户交期归到这一批。<button class="button small" id="drawer-link-delivery" type="button">关联交付数量</button></div>' : ''}${independent ? boardSchedulePanels(batch,boardDetails) : stageTimeline(stages)}<div class="node-drawer-toolbar"><div><h3>${independent ? '逐板生产节点' : '生产节点'}</h3><p>${independent ? '展开一块板查看点到点进度；圆点可编辑本板日期、负责人并勾选完成。客户交付另行确认。' : `${stages.length} 个节点 · 勾选仅表示该工序完成，批次与客户交付另行确认`}</p></div>${editable ? '<button id="drawer-edit-phase" class="button small" type="button">调整阶段与日期</button>' : ''}</div>
      ${independent ? stages.length ? `<details class="node-list-disclosure"><summary>批次共用节点记录（${stages.length} 项，不计入逐板进度）</summary><div class="node-list">${stages.map((stage,index) => nodeCard(stage,index,false,users)).join('')}</div></details>` : '' : stages.length ? `<details class="node-list-disclosure"><summary>展开节点记录列表（${stages.length} 项）</summary><div class="node-list">${stages.map((stage,index) => nodeCard(stage,index,editable,users)).join('')}</div></details>` : empty('此批次尚未生成节点','先在完整批次中确认产品模板，再生成计划。')}`;
      $('#node-drawer-body').scrollTop = previousScroll;
      $('#node-drawer').hidden = false;
      document.body.classList.add('drawer-open');
      $$('[data-open-project]').forEach(button => {
        const selected = Number(button.dataset.openProject) === Number(batchId);
        button.setAttribute('aria-expanded',String(selected));
        button.closest('.project-phase-row')?.classList.toggle('is-selected',selected);
      });
      $('#node-drawer-detail').onclick = () => openBatch(batchId);
      $('#node-drawer-dismiss').onclick = hideNodeDrawer;
      $('#node-drawer-close').onclick = hideNodeDrawer;
      $('.node-drawer-backdrop').onclick = hideNodeDrawer;
      $('#drawer-edit-phase')?.addEventListener('click',() => openEditBatch(batch));
      $('#drawer-link-delivery')?.addEventListener('click',() => {
        const project = state.projects.find(item => item.lines.some(line => Number(line.id) === Number(projectBatch?.order_line_id)));
        if (!project?.lines[0]) return toast('未找到关联的订单产品行，请打开完整批次核对。',true);
        hideNodeDrawer();
        openAllocation(project.lines[0]);
      });
      if (independent) bindBoardScheduleActions($('#node-drawer'),batch,boardDetails,users);
      else bindNodeActions(batchId,stages,users,$('#node-drawer'));
      if (!restoring) $('#node-drawer-close').focus();
    } catch (error) {
      if (requestId !== drawerRequest) return;
      hideNodeDrawer();
      toast(`节点暂时无法打开：${error.message}`,true);
    }
  }
  function openNodeEditor(batchId,stage,users) {
    const editable = state.user?.role === 'admin' || state.user?.permissions?.production_edit !== false;
    if (!editable) {
      const dates = [['计划开始',stage.planned_start],['计划完成',stage.planned_finish],
        ['实际到料',stage.actual_material_at],['实际开始',stage.actual_start],['实际完成',stage.actual_finish]];
      openModal({kicker:'生产节点',title:stage.name,body:`<p class="node-editor-readonly">${escape(nodeStatus(stage)[1])} · 责任人 ${escape(stage.owner_display_name || stage.owner_name || '待指定')}</p><dl class="node-meta">${dates.map(([name,value]) => `<div><dt>${name}</dt><dd>${escape(date(value))}</dd></div>`).join('')}</dl>`});
      return;
    }
    const wasComplete = Boolean(stage.is_complete || stage.status === 'done');
    const fields = ['name','owner_id','stage_type','duration_days','planned_start','planned_finish','actual_material_at','actual_start','actual_finish','note'];
    const refresh = async () => {
      if (Number(state.drawerBatchId) === Number(batchId)) await showNodeDrawer(batchId,{restoring:true});
      else await renderView();
    };
    let saving = false;
    const persist = async (form,automatic = false) => {
      if (saving) return false;
      const checkbox = $('[name="completed"]',form);
      const complete = checkbox.checked;
      const reason = $('[name="reason"]',form).value.trim();
      const showMessage = message => {
        const messageBox = $('#modal-error');
        if (messageBox) { messageBox.textContent = message; messageBox.hidden = false; }
        else toast(message,true);
        if (automatic) checkbox.checked = wasComplete;
        return false;
      };
      if (!form.reportValidity()) { if (automatic) checkbox.checked = wasComplete; return false; }
      if (complete && !$('[name="owner_id"]',form).value) return showMessage('请先选择节点责任人账号，再勾选完成。');
      if (!complete && wasComplete && !reason) {
        $('[name="reason"]',form).focus();
        return showMessage('取消完成必须填写原因，然后再取消勾选。');
      }
      const changes = changedFields(form,stage,fields);
      if (complete !== wasComplete) {
        changes.status = complete ? 'done' : ($('[name="actual_start"]',form).value ? 'in_progress' : 'pending');
        if (!complete) changes.actual_finish = null;
      } else if (complete && changes.actual_finish === null) {
        return showMessage('要取消实际完成日，请取消“已完成”并填写原因。');
      }
      if (!Object.keys(changes).length) {
        if (!automatic) { closeModal(); toast('节点没有变化。'); }
        return true;
      }
      const changeKeys = Object.keys(changes);
      const onlyOwnerOrCompletion = changeKeys.every(key => ['owner_id','status'].includes(key));
      const reasonOptional = onlyOwnerOrCompletion && (changeKeys.includes('owner_id') || (complete && !wasComplete));
      if (!reason && !reasonOptional) {
        $('[name="reason"]',form).focus();
        return showMessage('修改节点日期或信息须填写原因；分配责任人或勾选完成可直接保存。');
      }
      saving = true;
      checkbox.disabled = true;
      try {
        const auditReason = reason || (complete && !wasComplete ? '勾选节点完成' : '节点责任人分配');
        await api(`/stages/${stage.id}`,{method:'PATCH',body:withVersion(stage,changes,auditReason)});
      } catch (error) {
        if (error.status === 409) {
          checkbox.checked = wasComplete;
          showVersionConflict($('#modal-body'),() => { closeModal(); refresh().catch(refreshError => toast(`刷新节点失败：${refreshError.message}`,true)); });
        } else showMessage(error.message);
        return false;
      } finally {
        saving = false;
        if (!$('#modal').hidden) checkbox.disabled = false;
      }
      closeModal();
      toast(complete !== wasComplete ? complete ? '节点已完成，实际完成日和登记人已记录。' : '节点完成标记已撤销。' : '节点已更新。');
      await refresh().catch(error => toast(`节点已保存，刷新失败：${error.message}`,true));
      return true;
    };
    openModal({kicker:'生产节点',title:`编辑 ${stage.name}`,wide:true,body:`<form class="form-grid node-editor-form">
      ${formField('节点名称','name','text',stage.name,true)}<label>节点责任人<select name="owner_id">${nodeOwnerOptions(stage,users)}</select></label>
      <div class="node-editor-completion wide"><label><input type="checkbox" name="completed" data-node-editor-complete ${wasComplete ? 'checked' : ''}><span>已完成</span></label><p>${wasComplete ? `已于 ${escape(date(stage.actual_finish))} 完成${stage.completed_by_name ? `，由 ${escape(stage.completed_by_name)} 登记` : ''}。取消完成须先填写原因。` : '勾选即保存完成状态；未填实际完成日时自动记录今天，并记录操作人。'}</p></div>
      ${formSelect('节点类型','stage_type',[['normal','生产工序'],['milestone','里程碑']],stage.stage_type || 'normal')}
      ${formField('耗时 / 天','duration_days','number',stage.duration_days ?? 0,true,'min="0" step="0.5"')}
      ${formField('计划开始','planned_start','date',stage.planned_start || '')}${formField('计划完成','planned_finish','date',stage.planned_finish || '')}
      ${formField('实际到料','actual_material_at','date',stage.actual_material_at || '')}${formField('实际开始','actual_start','date',stage.actual_start || '')}
      ${formField('实际完成','actual_finish','date',stage.actual_finish || '')}
      <label class="wide">说明<textarea name="note">${escape(stage.note || '')}</textarea></label>
      <label class="wide">修改或撤销原因<textarea name="reason" placeholder="改日期、数量或取消完成时填写依据"></textarea></label>
    </form>`,submitText:'保存节点',onSubmit:form => persist(form)});
    const form = $('#modal-body form');
    $('[data-node-editor-complete]',form).addEventListener('change',() => persist(form,true));
    $('[name="actual_finish"]',form).addEventListener('change',event => {
      if (event.target.value) $('[data-node-editor-complete]',form).checked = true;
    });
  }
  function bindNodeActions(batchId,stages,users,root) {
    bindStageTimeline(root,stages,users,batchId);
    $$('[data-open-node]',root).forEach(card => {
      const open = () => {
        const stage = stages.find(item => Number(item.id) === Number(card.dataset.openNode));
        if (stage) openNodeEditor(batchId,stage,users);
      };
      card.onclick = event => {
        if (event.target.closest('button,input,select,textarea,a,label,summary')) return;
        open();
      };
      card.onkeydown = event => {
        if (event.target !== card || !['Enter',' '].includes(event.key)) return;
        event.preventDefault(); open();
      };
    });
    const refresh = async () => {
      if (Number(state.drawerBatchId) === Number(batchId)) await showNodeDrawer(batchId,{restoring:true});
      else await renderView();
    };
    $$('[data-node-owner]',root).forEach(select => select.onchange = async () => {
      const stage = stages.find(item => Number(item.id) === Number(select.dataset.nodeOwner));
      if (!stage) return;
      const ownerId = select.value ? Number(select.value) : null;
      if (Number(stage.owner_id || 0) === Number(ownerId || 0)) return;
      select.disabled = true;
      try {
        await api(`/stages/${stage.id}`,{method:'PATCH',body:withVersion(stage,{owner_id:ownerId},'节点责任人分配')});
        toast('节点责任人已更新。'); await refresh();
      } catch (error) {
        select.disabled = false;
        toast(error.message,true);
        if (error.status === 409) await refresh();
        else select.value = stage.owner_id ? String(stage.owner_id) : '';
      }
    });
    $$('[data-node-complete]',root).forEach(checkbox => checkbox.onchange = async () => {
      const stage = stages.find(item => Number(item.id) === Number(checkbox.dataset.nodeComplete));
      if (!stage) return;
      if (!checkbox.checked) {
        checkbox.checked = true;
        openModal({kicker:'生产节点',title:`撤销 ${stage.name} 的完成标记`,body:`<div class="notice warning"><p>撤销后会清除该节点实际完成日，并保留修改记录。</p></div><form class="form-grid"><label class="wide">撤销原因<textarea name="reason" required placeholder="例如厂商复检未通过或日期误填"></textarea></label></form>`,submitText:'确认撤销',onSubmit:async form => {
          try {
            await api(`/stages/${stage.id}/completion`,{method:'POST',body:{version:stage.version,completed:false,reason:getForm(form).reason.trim()}});
            closeModal(); toast('节点完成标记已撤销。'); await refresh();
          } catch (error) { const message = $('#modal-error'); message.textContent = error.message; message.hidden = false; }
        }});
        return;
      }
      if (!stage.owner_id) { checkbox.checked = false; toast('请先指派节点责任人。',true); return; }
      checkbox.disabled = true;
      try {
        await api(`/stages/${stage.id}/completion`,{method:'POST',body:{version:stage.version,completed:true}});
        toast('节点已完成，并记录实际完成日。'); await refresh();
      } catch (error) {
        checkbox.checked = false; checkbox.disabled = false;
        toast(error.message,true);
        if (error.status === 409) await refresh();
      }
    });
    $$('[data-edit-node]',root).forEach(button => button.onclick = () => {
      const stage = stages.find(item => Number(item.id) === Number(button.dataset.editNode));
      if (stage) openNodeEditor(batchId,stage,users);
    });
  }
  function deliveryFocus(lines) {
    const deliveries = list(lines).flatMap(line => list(line.delivery_batches).map(batch => ({...batch,product_name:line.product_name || line.product_code || '产品'})));
    const pending = deliveries.filter(batch => batch.forecast?.status !== 'complete');
    return [...(pending.length ? pending : deliveries)].sort((a,b) =>
      String(a.due_date || a.promised_date || '9999-12-31').localeCompare(String(b.due_date || b.promised_date || '9999-12-31')))[0] || null;
  }
  function calendarDeviation(forecastDate,baseline) {
    if (!forecastDate || !baseline) return null;
    const days = Math.round((Date.parse(`${forecastDate}T00:00:00Z`) - Date.parse(`${baseline}T00:00:00Z`)) / 86400000);
    return Number.isFinite(days) ? days > 0 ? `晚 ${days} 天` : days < 0 ? `早 ${-days} 天` : '同日' : null;
  }
  function projectEntry(project,assessment,linkageMap,materialMap,stageMap,riskLevel = '') {
    const batches = state.phase === 'all' ? project.batches : project.batches.filter(batch => phaseOf(batch) === state.phase);
    const line = project.lines[0];
    const boardInfo = boardLineInfo(line);
    const projectBoards = list(boardInfo.boards);
    const batchSummaries = batches.map(batch => batchBoardInfo(batch));
    const hasBoardPlan = projectBoards.length > 0;
    const openingBatch = [...batches].sort((a,b) => ({rd:0,pp:1,mp:2}[phaseOf(b)] ?? -1) - ({rd:0,pp:1,mp:2}[phaseOf(a)] ?? -1))[0];
    const deliveries = project.lines.flatMap(line => list(line.delivery_batches));
    const rdOnly = batches.length > 0 && batches.every(batch => phaseOf(batch) === 'rd');
    const customerProject = project.project_mode !== 'internal' && !rdOnly;
    const tempOrder = /^(PCBA-TEMP-|PCBA-PROJ-)/.test(String(project.order_no || ''));
    const orderMissing = customerProject && (!project.customer || project.customer === '待补录' || tempOrder);
    const deliveryQuantity = deliveries.reduce((sum,item) => sum + Number(item.quantity || 0),0);
    const orderQuantity = project.lines.reduce((sum,item) => sum + Number(item.quantity || 0),0);
    const deliveryMissing = customerProject && (!deliveries.length || deliveries.some(item => !item.due_date || !item.promised_date) || deliveryQuantity < orderQuantity);
    const bomMissing = batches.some(batch => (batchBoardInfo(batch).bom_scope === 'multi' || phaseOf(batch) !== 'rd') && !batch.bom_locked);
    const linkage = linkageMap.get(Number(project.lines[0]?.id));
    const bomCatalogMissing = !hasBoardPlan && !rdOnly && batches.some(batch => phaseOf(batch) !== 'rd') && !linkage?.catalog_match;
    const boardPlanUnconfirmed = hasBoardPlan && !boardInfo.confirmed;
    const batchBoardsPending = hasBoardPlan && batches.some(batch => batchBoardInfo(batch).bom_scope !== 'multi');
    const codes = new Set(list(assessment?.missing_reasons).map(item => item.code));
    const vendorMissing = !rdOnly && (batches.some(batch => {
      if (!['pp','mp'].includes(phaseOf(batch))) return false;
      const summary = batchBoardInfo(batch);
      return summary.board_schedule_mode === 'independent' && summary.bom_scope === 'multi' ? !list(summary.boards).length || list(summary.boards).some(board => !board.vendor || !board.commit_start || !board.commit_finish) : !batch.vendor || !batch.commit_start || !batch.commit_finish;
    }) || (state.phase === 'all' && codes.has('vendor_reconfirm')));
    const reasons = state.phase === 'all' ? [...new Set(list(assessment?.missing_reasons).map(item => item.label || item.code || item).filter(Boolean))] : [];
    const fallback = [rdOnly && !openingBatch?.target_date && '研发目标日期',rdOnly && !openingBatch?.board_eta && !openingBatch?.board_received_at && '嘉立创到板日期',orderMissing && '客户或正式订单号',deliveryMissing && '交付批次或交期',boardPlanUnconfirmed && '项目板卡组成待确认',batchBoardsPending && '生产批次待启用板卡',bomMissing && '生产批次 BOM 未锁定',vendorMissing && '厂商承诺日期'].filter(Boolean);
    const baseMissing = state.phase === 'all' && assessment?.assessable ? [] : reasons.length ? reasons : fallback;
    const missing = [...new Set([...baseMissing,...(bomCatalogMissing ? ['BOM 未关联到导入目录'] : [])])];
    const readiness = riskLevel === 'red' || riskLevel === 'yellow' ? badge(riskLevel,riskLevel === 'red' ? '交期红色风险' : '交期临期') : state.phase !== 'all' && !missing.length ? '<span class="badge green">阶段资料已填</span>' : state.phase === 'all' && assessment?.assessable === true ? '<span class="badge green">交期可判定</span>' : `<span class="badge ${missing.length ? 'yellow' : 'gray'}">${missing.length ? `${missing.length} 项待处理` : '交期待核对'}</span>`;
    const actions = [
      (orderMissing || !project.order_id) && ['order','补项目/订单'],
      deliveryMissing && ['delivery',deliveries.length ? '核对交期' : '建立交付批次'],
      !rdOnly && codes.has('delivery_unallocated') && ['allocation','关联交付数量'],
      hasBoardPlan && (boardPlanUnconfirmed || batchBoardsPending) && ['boards','核对项目板卡'],
      (bomMissing || bomCatalogMissing || !project.batches.length) && ['bom',project.batches.length ? hasBoardPlan ? '逐板锁定 BOM' : '关联或锁定 BOM' : '建立生产批次'],
      vendorMissing && ['vendor',codes.has('vendor_reconfirm') ? '重确认厂商日期' : '填厂商承诺'],
      codes.has('missing_phase_target') && ['rd','补研发目标'],
      !rdOnly && (codes.has('material_eta_unknown') || codes.has('stock_unconfirmed')) && ['materials','逐项核料'],
      codes.has('stage_overdue') && ['nodes','处理逾期节点'],
    ].filter(Boolean);
    const title = openingBatch ? `<button class="project-title-button" type="button" data-open-project="${attr(openingBatch.id)}" aria-controls="node-drawer" aria-expanded="false" aria-label="查看 ${attr(project.name)} 的生产节点"><span>${escape(project.name)}</span><small>查看节点 →</small></button>` : `<strong class="project-title">${escape(project.name)}</strong>`;
    const nextTarget = rdOnly ? openingBatch?.target_date : deliveries.map(item => item.due_date).filter(Boolean).sort()[0];
    const openingProgress = stageMap.get(Number(openingBatch?.id));
    const openingBoards = batchBoardInfo(openingBatch);
    const openingStageText = openingBoards.board_schedule_mode === 'independent' && openingBoards.bom_scope === 'multi' ? list(openingBoards.boards).length ? `${list(openingBoards.boards).reduce((total,board) => total + Number(board.stage_progress?.completed || 0),0)}/${list(openingBoards.boards).reduce((total,board) => total + Number(board.stage_progress?.total || 0),0)} 逐板节点已完成 · 板卡最晚预计 ${latestBoardFinish(openingBoards) || '无法确定'}` : '逐板节点待启用' : openingProgress?.total ? `${openingProgress.completed}/${openingProgress.total} 已完成 · ${openingProgress.current_name || '全部节点已完成'}` : '节点待生成';
    const productionBatches = batches.filter(batch => phaseOf(batch) !== 'rd');
    const lockedCount = productionBatches.filter(batch => batch.bom_locked).length;
    const bomSummary = hasBoardPlan ? `${projectBoards.length} 块板 · ${boardInfo.confirmed ? '组成已确认' : '待确认'} · ${lockedCount}/${productionBatches.length} 批 BOM 已锁定` : rdOnly ? '打样阶段按节点跟进' : !linkage?.catalog_match ? `${lockedCount ? `${lockedCount} 批历史快照保留 · ` : ''}未匹配导入目录` : `${linkage.linked_product_code} · 默认 ${linkage.bom_version} · ${productionBatches.length ? `${lockedCount}/${productionBatches.length} 批已锁定` : '尚无生产批次'}`;
    const nextDelivery = deliveryFocus(project.lines);
    const deliveryForecast = nextDelivery?.forecast || {};
    const formalDate = deliveryForecast.status === 'unknown' ? null : deliveryForecast.formal_date;
    const summaryLabel = rdOnly ? '研发目标 / 到板' : nextDelivery ? `最近交付 · ${nextDelivery.batch_no}` : '交付批次';
    const summaryDates = rdOnly ? `${date(nextTarget)} / ${date(openingBatch?.board_received_at || openingBatch?.board_eta)}` : nextDelivery ? `要求 ${date(nextDelivery.due_date)} · 承诺 ${date(nextDelivery.promised_date)}` : '尚未建立交付批次';
    const forecastSummary = rdOnly
      ? (openingBatch?.sourceSchedule?.estimated_delivery_date ? `节点初稿 ${date(openingBatch.sourceSchedule.estimated_delivery_date)}` : '节点草案待核对')
      : formalDate ? `正式预测 ${date(formalDate)}`
      : deliveryForecast.provisional_date ? `无法确定 · 临时估算 ${date(deliveryForecast.provisional_date)}` : '正式预测无法确定';
    const dueDeviation = formalDate ? calendarDeviation(formalDate,nextDelivery?.due_date) : null;
    const promisedDeviation = formalDate ? calendarDeviation(formalDate,nextDelivery?.promised_date) : null;
    const deviations = [['客户要求',dueDeviation],['业务承诺',promisedDeviation]]
      .filter(([,value]) => value)
      .map(([label,value]) => `<small class="${value.startsWith('晚') ? 'is-late' : ''}">对${label}日 ${escape(value)}（自然日）</small>`).join('');
    return `<article class="project-ledger-entry" data-project-key="${attr(project.key)}">
      <div class="project-ledger-head"><div class="project-ledger-identity">${title}<span>${escape(project.order_no || '项目单号待补')} · ${escape(project.customer && project.customer !== '待补录' ? project.customer : '客户待补录')} · 业务负责人 ${escape(project.owner_name || '待指定')}</span></div>${readiness}</div>
      <div class="project-scan"><div><span>${escape(summaryLabel)}</span><strong>${escape(summaryDates)}</strong></div><div><span>${rdOnly ? '研发计划' : '交付预测'}</span><strong>${escape(forecastSummary)}</strong></div><div><span>当前阶段 / 待处理</span><strong>${escape(batches.length ? [...new Set(batches.map(batch => phaseNames[phaseOf(batch)] || '待分类'))].join('、') : '待建立批次')} · ${missing.length} 项待处理</strong>${deviations}</div></div>
      <details class="project-ledger-details" data-project-details="${attr(project.key)}" ${state.portfolioExpanded === project.key ? 'open' : ''}><summary>展开项目详情：板卡、BOM 与生产批次</summary>
      <div class="project-facts"><div><span>${rdOnly ? '打样目标' : '最近客户要求日'}</span><strong>${escape(date(nextTarget))}</strong></div><div><span>${hasBoardPlan ? '板卡与 BOM' : '项目 BOM 母件'}</span><strong class="${bomCatalogMissing || boardPlanUnconfirmed ? 'needs-action' : ''}">${escape(bomSummary)}</strong></div><div><span>当前批次节点</span><strong>${escape(openingStageText)}</strong></div></div>
      ${hasBoardPlan ? projectBoardStrip(line,boardInfo,batchSummaries) : ''}
      ${missing.length || actions.length ? `<details class="project-issues"><summary>待处理事项 <strong>${missing.length}</strong><span>${escape(missing[0] || '请补齐阶段排期')}</span></summary><div class="project-issues-body"><p>${escape(missing.join(' · ') || '请核对项目资料和阶段排期。')}</p><div class="project-quick-actions">${actions.map(([action,label]) => `<button class="button small" type="button" data-project-action="${action}" data-project-key="${attr(project.key)}">${escape(label)}</button>`).join('')}${project.order_id ? `<button class="button small linklike" type="button" data-project-action="view" data-project-key="${attr(project.key)}">完整订单 →</button>` : ''}</div></div></details>` : ''}
      <div class="project-phase-list"><div class="project-phase-heading"><strong>阶段批次</strong><span>项目 → 板卡 → 批次 → BOM / 逐料核对 / 生产节点</span>${!hasBoardPlan && line ? `<button class="button small linklike" type="button" data-manage-boards="${attr(line.id)}">配置多板产品</button>` : ''}</div>${batches.length ? batches.map(batch => portfolioBatchRow(batch,linkage,materialMap.get(Number(batch.id)),project.key,stageMap.get(Number(batch.id)),batchBoardInfo(batch),hasBoardPlan)).join('') : empty('尚未建立该阶段的生产批次','先建立生产批次，再按产品模板生成工序节点。')}</div></details>
    </article>`;
  }
  async function openProjectAction(key,action) {
    const project = state.projects.find(item => item.key === key);
    if (!project) return toast('项目已更新，请刷新后重试。',true);
    const line = project.lines[0];
    try {
      if (action === 'view') return openOrder(project.order_id);
      if (action === 'order') return await openProjectOrderEditor(project.order_id);
      if (action === 'boards') return openProjectBoards(line.id);
      if (action === 'delivery') {
        const order = await api(`/orders/${encodeURIComponent(project.order_id)}`);
        const currentLine = list(order.lines || order.order_lines).find(item => Number(item.id) === Number(line?.id));
        if (!currentLine) throw new Error('找不到该项目产品行，请刷新项目列表。');
        const deliveries = list(currentLine.delivery_batches);
        const scheduled = deliveries.reduce((sum,item) => sum + Number(item.quantity || 0),0);
        if (scheduled < Number(currentLine.quantity || 0)) return openCreateDelivery(currentLine);
        const needsDate = deliveries.find(item => !item.due_date || !item.promised_date);
        return deliveries.length ? openEditDelivery(needsDate || deliveries[0]) : openCreateDelivery(currentLine);
      }
      if (action === 'allocation') return openAllocation(line);
      if (action === 'nodes') {
        const batch = project.batches.find(item => item.id) || null;
        return batch ? showNodeDrawer(batch.id) : toast('请先建立生产批次。',true);
      }
      if (action === 'rd' || action === 'materials') {
        const batch = project.batches.find(item => action === 'rd' ? phaseOf(item) === 'rd' : phaseOf(item) !== 'rd');
        if (!batch) throw new Error('找不到需处理的阶段批次，请刷新项目列表。');
        if (action === 'materials') return openMaterialBatch(batch.id);
        await openBatch(batch.id);
        $('#rd-quick-form [name=target_kind]')?.focus();
        return;
      }
      if (action === 'bom' || action === 'vendor') {
        const batch = action === 'bom' ? project.batches.find(item => (phaseOf(item) !== 'rd' || list(boardLineInfo(line).boards).length) && !item.bom_locked) :
          project.batches.find(item => ['pp','mp'].includes(phaseOf(item)) && (batchBoardInfo(item).board_schedule_mode === 'independent' ? list(batchBoardInfo(item).boards).some(board => !board.vendor || !board.commit_start || !board.commit_finish) : !item.vendor || !item.commit_start || !item.commit_finish)) || project.batches.find(item => ['pp','mp'].includes(phaseOf(item)));
        if (!batch && action === 'bom' && !project.batches.length && line) return openCreateProduction(line);
        if (!batch && action === 'bom') {
          const multiBatch = project.batches.find(item => batchBoardInfo(item).bom_scope === 'multi');
          if (multiBatch) return openBatchBoards(multiBatch.id);
          return list(boardLineInfo(line).boards).length ? openProjectBoards(line.id) : openProjectBomFlow(key);
        }
        if (!batch) throw new Error('找不到待补录的生产批次，请刷新项目列表。');
        if (action === 'bom') return batchBoardInfo(batch).bom_scope === 'multi' || list(boardLineInfo(line).boards).length ? openBatchBoards(batch.id) : openProjectBomFlow(key,batch.id);
        if (batchBoardInfo(batch).board_schedule_mode === 'independent') {
          const board = list(batchBoardInfo(batch).boards).find(item => !item.vendor || !item.commit_start || !item.commit_finish) || list(batchBoardInfo(batch).boards)[0];
          return board ? openBoardSchedule(board.id) : openBatchBoards(batch.id);
        }
        const detail = await api(`/production-batches/${encodeURIComponent(batch.id)}`);
        return openEditBatch(detail.batch || detail);
      }
    } catch (error) { toast(error.message,true); }
  }
  function openMaterialBatch(batchId) {
    hideNodeDrawer();
    state.view = 'materials'; state.orderId = null; state.batchId = null;
    state.materialTab = 'requirements'; state.materialBatchId = Number(batchId);
    window.scrollTo(0,0); renderView();
  }
  async function openProjectBoards(lineId) {
    try {
      const info = await api(`/order-lines/${encodeURIComponent(lineId)}/boards`);
      const line = info.order_line;
      const boards = list(info.boards);
      const editable = state.user?.role === 'admin' || state.user?.permissions?.orders_edit !== false;
      const mode = line.board_schedule_mode || 'shared';
      const entries = boards.length ? boards.map(board => `<div class="board-manager-row"><div class="board-manager-name"><span class="board-ref">${escape(board.board_ref)}</span><strong>${escape(board.board_name)}</strong><small>${escape(board.bom_product_code)} · 每件产品 ${number(board.quantity_per_product)} 块</small></div><div class="board-manager-version"><span>默认 BOM</span><strong>${display(board.default_bom_version)}</strong><small>${board.catalog_item_count ? `${number(board.catalog_item_count)} 项已导入` : '目录待核对'}</small></div>${editable ? `<div class="board-manager-actions"><button class="button small" type="button" data-edit-board="${attr(board.id)}">编辑</button><button class="button small" type="button" data-delete-board="${attr(board.id)}">删除</button></div>` : ''}</div>`).join('') : empty('还没有项目板卡','若客户产品含主板、按键板等多块 PCBA，请逐块添加；普通单板项目可继续沿用原有 BOM 流程。');
      openModal({kicker:'项目板卡',title:`${line.product_name || line.product_code} · 板卡组成`,wide:true,
        body:`<div class="board-manager-intro"><strong>客户产品 ${number(line.quantity)} 件</strong><span>板卡用于 BOM、物料和排期核对，不会增加客户交付数量。</span></div><div class="board-mode-selector"><div><strong>项目排期方式</strong><small>后续新批次自动采用；已有未锁 BOM、无实际进度的批次需在板卡配置里单独更新。</small></div><div role="group" aria-label="板卡排期方式">${Object.entries(boardModeNames).map(([value,label]) => `<button class="button small ${mode === value ? 'is-selected' : ''}" type="button" data-board-mode="${value}" aria-pressed="${mode === value}" ${editable ? '' : 'disabled'}>${escape(label)}</button>`).join('')}</div></div><div class="board-manager-list">${entries}</div><div class="board-manager-footer"><span>${boards.length} 块板 · ${info.confirmed ? '组成已确认' : '组成待确认'}</span>${editable ? `<div><button class="button small" id="board-manager-add" type="button">+ 添加板卡</button>${boards.length && !info.confirmed ? '<button class="button small primary" id="board-manager-confirm" type="button">确认板卡组成</button>' : ''}</div>` : ''}</div>`});
      $$('[data-board-mode]',$('#modal-body')).forEach(button => button.onclick = () => {
        if (button.dataset.boardMode !== mode) openBoardModeChange(info,button.dataset.boardMode);
      });
      $('#board-manager-add')?.addEventListener('click',() => openEditProjectBoard(info));
      $('#board-manager-confirm')?.addEventListener('click',() => openConfirmProjectBoards(info));
      $$('[data-edit-board]',$('#modal-body')).forEach(button => button.onclick = () => openEditProjectBoard(info,boards.find(board => Number(board.id) === Number(button.dataset.editBoard))));
      $$('[data-delete-board]',$('#modal-body')).forEach(button => button.onclick = () => openDeleteProjectBoard(info,boards.find(board => Number(board.id) === Number(button.dataset.deleteBoard))));
    } catch (error) { toast(error.message,true); }
  }
  function openBoardModeChange(info,mode) {
    const line = info.order_line;
    openModal({kicker:'项目排期方式',title:`切换为${boardModeNames[mode]}`,body:`<div class="notice"><p>${mode === 'independent' ? '每块板分别登记生产数量、外协厂、承诺日期和工序节点；整件完工以必需板卡中较晚的日期判断。' : '本项目新批次按一套共用工序节点跟进；板卡仍逐板锁 BOM 和核料。'}此设置只作用于后续新批次；已有未锁 BOM 且无实际进度的批次可手动更新。</p></div><form class="form-grid"><label class="wide">调整原因<textarea name="reason" required placeholder="说明该项目选择此排期方式的依据"></textarea></label></form>`,submitText:'保存排期方式',onSubmit:async form => {
      const result = await saveMutation(`/order-lines/${line.id}/board-schedule-mode`,{version:line.version,mode,reason:getForm(form).reason.trim()},'PATCH','项目排期方式已更新。');
      if (result) await openProjectBoards(line.id);
    }});
  }
  function openEditProjectBoard(info,board = null) {
    const line = info.order_line;
    if (!line) return;
    openModal({kicker:'项目板卡',title:board ? `编辑 ${board.board_name}` : `为 ${line.product_name || line.product_code} 添加板卡`,
      body:`<div class="notice"><p>板位用于区分主板、按键板等实际 PCBA。每件产品用板数须为正整数，用于计算本批板卡及 BOM 需求。</p></div><form class="form-grid">${formField('板位 / 代号','board_ref','text',board?.board_ref || '',true)}${formField('板卡名称','board_name','text',board?.board_name || '',true)}${formField('BOM 母件编码','bom_product_code','text',board?.bom_product_code || '',true)}${formField('每件产品用板数','quantity_per_product','number',board?.quantity_per_product || '1',true,'min="1" step="1"')}${formField('默认 BOM 版本','default_bom_version','text',board?.default_bom_version || '')}<label class="wide">${board ? '修改原因' : '板卡配置依据'}<textarea name="reason" required placeholder="例如工程 BOM 主板与按键板清单"></textarea></label></form>`,submitText:board ? '保存板卡' : '添加板卡',onSubmit:async form => {
        const values = getForm(form);
        const payload = {version:board?.version ?? line.version,board_ref:values.board_ref.trim(),board_name:values.board_name.trim(),bom_product_code:values.bom_product_code.trim(),quantity_per_product:values.quantity_per_product,default_bom_version:values.default_bom_version.trim() || null,reason:values.reason.trim()};
        const result = await saveMutation(board ? `/order-line-boards/${board.id}` : `/order-lines/${line.id}/boards`,payload,board ? 'PATCH' : 'POST',board ? '板卡已更新，请重新确认板卡组成。' : '板卡已添加，请确认板卡组成。');
        if (result) await openProjectBoards(line.id);
      }});
  }
  function openDeleteProjectBoard(info,board) {
    if (!board) return;
    openModal({kicker:'项目板卡',title:`删除 ${board.board_name}`,body:`<div class="notice warning"><p>删除会更新项目当前板卡组成，并要求重新确认。已有生产批次保留其板卡历史快照；未锁定的批次可再按最新组成启用。</p></div><form class="form-grid"><label class="wide">删除原因<textarea name="reason" required placeholder="说明为何从产品组成移除"></textarea></label></form>`,submitText:'确认删除',onSubmit:async form => {
      const result = await saveMutation(`/order-line-boards/${board.id}`,{version:board.version,reason:getForm(form).reason.trim()},'DELETE','板卡已删除，请重新确认板卡组成。');
      if (result) await openProjectBoards(info.order_line.id);
    }});
  }
  function openConfirmProjectBoards(info) {
    const boards = list(info.boards);
    if (!boards.length) return toast('请先添加项目板卡。',true);
    openModal({kicker:'项目板卡',title:'确认板卡组成',body:`<div class="notice"><p>确认 ${boards.map(board => `${escape(board.board_name)}（每件 ${number(board.quantity_per_product)} 块）`).join('、')}。确认后，可在生产批次逐板锁定 BOM。</p></div><form class="form-grid"><label class="wide">工程确认依据<textarea name="reason" required placeholder="例如工程图纸版本、BOM 审核记录"></textarea></label></form>`,submitText:'确认组成',onSubmit:async form => {
      const result = await saveMutation(`/order-lines/${info.order_line.id}/boards/confirm`,{version:info.order_line.version,reason:getForm(form).reason.trim()},'POST','板卡组成已确认。');
      if (result) await openProjectBoards(info.order_line.id);
    }});
  }
  async function refreshBoardUi() {
    if (state.drawerBatchId) await showNodeDrawer(state.drawerBatchId,{restoring:true});
    else await renderView();
  }
  function bindBoardScheduleActions(root,batch,details,users) {
    $$('.board-stage-disclosure',root).forEach(disclosure => disclosure.addEventListener('toggle',() => {
      const boardId = Number(disclosure.closest('[data-board-schedule]')?.dataset.boardSchedule);
      if (!disclosure.open) { if (state.openBoardId === boardId) { state.openBoardId = null; saveUiRoute(); } return; }
      state.openBoardId = boardId;
      saveUiRoute();
      $$('.board-stage-disclosure',root).forEach(other => { if (other !== disclosure) other.open = false; });
    }));
    for (const detail of details) {
      const board = detail.board;
      const section = $(`[data-board-schedule="${CSS.escape(String(board.id))}"]`,root);
      if (!section) continue;
      if (Number(state.openBoardId) === Number(board.id)) $('.board-stage-disclosure',section).open = true;
      bindStageTimeline(section,list(detail.stages),users,batch.id,board.id);
      $('[data-edit-board-schedule]',section)?.addEventListener('click',() => openEditBoardSchedule(board));
      $('[data-board-conflict-ack]',section)?.addEventListener('click',() => openAcknowledgeBoardConflict(board));
      $('[data-add-board-stage]',section)?.addEventListener('click',() => openAddBoardStage(board,users));
      $('[data-board-completion]',section)?.addEventListener('click',() => openBoardCompletion(board));
    }
  }
  async function openBoardSchedule(boardId) {
    try {
      const [detail,userResult] = await Promise.all([api(`/batch-boards/${encodeURIComponent(boardId)}`),api('/users').catch(() => [])]);
      const board = detail.board, users = list(userResult.users || userResult.items || userResult);
      const batch = {id:board.production_batch_id,quantity:Number(board.required_quantity) / Number(board.quantity_per_product),board_schedule_mode:'independent',bom_scope:'multi'};
      state.openBoardId = Number(boardId);
      openModal({kicker:'逐板排期',title:`${board.board_name} · 工序与产量`,wide:true,body:boardSchedulePanels(batch,[detail])});
      bindBoardScheduleActions($('#modal-body'),batch,[detail],users);
      $('.board-stage-disclosure',$('#modal-body'))?.setAttribute('open','');
    } catch (error) { toast(`本板排期暂时无法打开：${error.message}`,true); }
  }
  function openEditBoardSchedule(board) {
    const remaining = Number(board.planned_quantity || 0) - Number(board.completed_quantity || 0);
    openModal({kicker:'逐板排期',title:`编辑 ${board.board_name}`,body:`<div class="board-manager-intro"><strong>成品对应需板 ${number(board.required_quantity)} 片</strong><span>每套用 ${number(board.quantity_per_product)} 块。本板计划可加备损片数；少于需求会导致正式预测无法确定。已完工 ${number(board.completed_quantity || 0)} 片，余 ${number(remaining)} 片计划。</span></div><form class="form-grid">${formField('本板计划生产 / 片','planned_quantity','number',board.planned_quantity,true,'min="1" step="1"')}${formField('本板外协厂','vendor','text',board.vendor || '')}${formField('厂商承诺开工日','commit_start','date',board.commit_start || '')}${formField('厂商承诺完工日','commit_finish','date',board.commit_finish || '')}<label class="wide">修改原因<textarea name="reason" required placeholder="例如本板备损数量或厂商邮件承诺"></textarea></label></form>`,submitText:'保存本板排期',onSubmit:async form => {
      const changes = changedFields(form,board,['planned_quantity','vendor','commit_start','commit_finish']);
      if (!Object.keys(changes).length) { closeModal(); return; }
      if (changes.planned_quantity && (!Number.isSafeInteger(Number(changes.planned_quantity)) || Number(changes.planned_quantity) < 1)) return modalMessage('本板计划生产数量须为正整数片。');
      try {
        await api(`/batch-boards/${board.id}`,{method:'PATCH',body:withVersion(board,changes,getForm(form).reason.trim())});
        closeModal(); toast('本板数量与厂商承诺已更新。'); await refreshBoardUi();
      } catch (error) {
        if (error.status === 409) showVersionConflict($('#modal-body'),() => { closeModal(); refreshBoardUi(); });
        else modalMessage(error.message);
      }
    }});
  }
  function openBoardCompletion(board) {
    const remaining = Number(board.planned_quantity || 0) - Number(board.completed_quantity || 0);
    if (remaining <= 0) return toast('本板计划片数已全部登记完工。');
    openModal({kicker:'逐板完工',title:`登记 ${board.board_name} 的完工片数`,body:`<div class="board-manager-intro"><strong>本板计划 ${number(board.planned_quantity)} 片</strong><span>已完工 ${number(board.completed_quantity || 0)} 片，可登记 ${number(remaining)} 片。登记板卡完工不会自动增加客户交付件数。</span></div><form class="form-grid">${formField('本次完工 / 片','quantity','number',remaining,true,`min="1" max="${attr(remaining)}" step="1"`)}${formField('实际完工日','event_date','date',new Date().toISOString().slice(0,10),true)}<label class="wide">完工依据<textarea name="reason" required placeholder="例如厂商完工单或验收记录"></textarea></label></form>`,submitText:'登记本板完工',onSubmit:async form => {
      const data = getForm(form);
      try { await api(`/batch-boards/${board.id}/completion`,{method:'POST',body:{version:board.version,quantity:data.quantity,event_date:data.event_date,reason:data.reason.trim()}}); closeModal(); toast('本板实际完工片数已记录。'); await refreshBoardUi(); }
      catch (error) {
        if (error.status === 409) showVersionConflict($('#modal-body'),() => { closeModal(); refreshBoardUi(); });
        else modalMessage(error.message);
      }
    }});
  }
  function openAcknowledgeBoardConflict(board) {
    openModal({kicker:'外协档期',title:`确认 ${board.board_name} 可并行`,body:`<div class="notice warning"><p>本板与 ${list(board.conflicts).length} 项同厂档期重叠。请先向厂商核对能否并行生产；确认只处理本板冲突，不代表厂商全部产能已验证。</p></div><form class="form-grid"><label class="wide">厂商确认依据<textarea name="reason" required placeholder="例如厂商联系人、确认日期及可并行产线"></textarea></label></form>`,submitText:'记录厂商确认',onSubmit:async form => {
      try { await api(`/batch-boards/${board.id}/conflicts/acknowledge`,{method:'POST',body:{version:board.version,reason:getForm(form).reason.trim()}}); closeModal(); toast('本板档期并行依据已记录。'); await refreshBoardUi(); }
      catch (error) {
        if (error.status === 409) showVersionConflict($('#modal-body'),() => { closeModal(); refreshBoardUi(); });
        else modalMessage(error.message);
      }
    }});
  }
  function openAddBoardStage(board,users) {
    openModal({kicker:'逐板节点',title:`为 ${board.board_name} 添加工序`,body:`<form class="form-grid">${formField('节点名称','name','text','',true)}${formField('耗时 / 天','duration_days','number','0',true,'min="0" step="0.5"')}${formSelect('节点类型','stage_type',[['normal','生产工序'],['milestone','里程碑']],'normal')}${formSelect('节点责任人','owner_id',[['','待指派'],...users.filter(user => user.active && user.status === 'active').map(user => [user.id,user.display_name || user.username])],'')}<label class="wide">备注<textarea name="note"></textarea></label><label class="wide">新增原因<textarea name="reason" required placeholder="例如本板增加专项测试"></textarea></label></form>`,submitText:'添加本板节点',onSubmit:async form => {
      const data = getForm(form);
      try { await api(`/batch-boards/${board.id}/stages`,{method:'POST',body:{...data,owner_id:data.owner_id ? Number(data.owner_id) : null}}); closeModal(); toast('本板节点已添加。'); await refreshBoardUi(); }
      catch (error) {
        if (error.status === 409) showVersionConflict($('#modal-body'),() => { closeModal(); refreshBoardUi(); });
        else modalMessage(error.message);
      }
    }});
  }
  function openBoardStageEditor(boardId,stage,users) {
    const editable = state.user?.role === 'admin' || state.user?.permissions?.production_edit !== false;
    if (!editable) {
      openModal({kicker:'逐板工序节点',title:stage.name,body:`<p>责任人 ${escape(stage.owner_display_name || '待指定')} · ${escape(nodeStatus(stage)[1])}</p><dl class="node-meta">${[['计划开始',stage.planned_start],['计划完成',stage.planned_finish],['实际开始',stage.actual_start],['实际完成',stage.actual_finish]].map(([name,value]) => `<div><dt>${name}</dt><dd>${escape(date(value))}</dd></div>`).join('')}</dl>`});
      return;
    }
    const wasComplete = Boolean(stage.is_complete || stage.status === 'done');
    const fields = ['name','owner_id','stage_type','duration_days','planned_start','planned_finish','actual_start','note'];
    let saving = false;
    const persist = async (form,automatic = false) => {
      if (saving) return;
      const checkbox = $('[name=completed]',form);
      const completed = checkbox.checked, data = getForm(form), reason = data.reason?.trim() || '';
      const changes = changedFields(form,stage,fields);
      const finishChanged = fieldChanged(stage.actual_finish,data.actual_finish);
      const completionChanged = completed !== wasComplete || (completed && finishChanged);
      if (!Object.keys(changes).length && !completionChanged) { if (!automatic) closeModal(); return; }
      if (completed && !data.owner_id) { modalMessage('请先选择本板节点责任人账号。'); checkbox.checked = wasComplete; return; }
      if (Object.keys(changes).length && !reason && Object.keys(changes).some(key => key !== 'owner_id')) { modalMessage('修改节点信息或日期须填写原因。'); checkbox.checked = wasComplete; return; }
      if ((!completed && wasComplete || wasComplete && finishChanged) && !reason) { modalMessage('撤销完成或修改实际完成日须填写原因。'); checkbox.checked = wasComplete; return; }
      saving = true; checkbox.disabled = true;
      try {
        let current = stage;
        if (Object.keys(changes).length) current = await api(`/board-stages/${stage.id}`,{method:'PATCH',body:withVersion(stage,changes,reason || '节点责任人分配')});
        if (completionChanged) await api(`/board-stages/${stage.id}/completion`,{method:'POST',body:{version:current.version,completed,actual_finish:completed ? data.actual_finish || null : null,...(reason ? {reason} : {})}});
        closeModal(); toast(completionChanged ? completed ? '本板节点已勾选完成。' : '本板节点完成标记已撤销。' : '本板节点已更新。'); await refreshBoardUi();
      } catch (error) {
        checkbox.checked = wasComplete;
        if (error.status === 409) showVersionConflict($('#modal-body'),() => { closeModal(); refreshBoardUi(); });
        else modalMessage(error.message);
      } finally { saving = false; if (!$('#modal').hidden) checkbox.disabled = false; }
    };
    openModal({kicker:'逐板工序节点',title:`${stage.name} · 本板节点`,wide:true,body:`<form class="form-grid node-editor-form">${formField('节点名称','name','text',stage.name,true)}<label>节点责任人<select name="owner_id">${nodeOwnerOptions(stage,users)}</select></label>${formSelect('节点类型','stage_type',[['normal','生产工序'],['milestone','里程碑']],stage.stage_type || 'normal')}${formField('耗时 / 天','duration_days','number',stage.duration_days ?? 0,true,'min="0" step="0.5"')}${formField('计划开始','planned_start','date',stage.planned_start || '')}${formField('计划完成','planned_finish','date',stage.planned_finish || '')}${formField('实际开始','actual_start','date',stage.actual_start || '')}${formField('实际完成','actual_finish','date',stage.actual_finish || '')}<div class="node-editor-completion wide"><label><input type="checkbox" name="completed" ${wasComplete ? 'checked' : ''}><span>已完成</span></label><p>勾选后记录实际完成日和操作人。${wasComplete ? '撤销或改动完成日须填写原因。' : '完成日期留空时记录今天。'}</p></div><label class="wide">说明<textarea name="note">${escape(stage.note || '')}</textarea></label><label class="wide">修改原因<textarea name="reason" placeholder="修改日期、内容或撤销完成时填写"></textarea></label></form>`,submitText:'保存本板节点',onSubmit:form => persist(form)});
    $('[name=completed]',$('#modal-body')).addEventListener('change',event => persist(event.target.form,true));
    $('[name=actual_finish]',$('#modal-body')).addEventListener('change',event => {
      if (event.target.value) $('[name=completed]',event.target.form).checked = true;
    });
  }
  async function openBatchBoards(batchId) {
    try {
      const response = await api(`/production-batches/${encodeURIComponent(batchId)}`);
      const batch = response.batch || response;
      const boards = list(batch.boards);
      const lineId = batch.order_line_id || batch.order_line?.id;
      const definition = lineId ? await api(`/order-lines/${encodeURIComponent(lineId)}/boards`) : null;
      const hasPlan = list(definition?.boards).length > 0;
      const editable = state.user?.role === 'admin' || state.user?.permissions?.production_edit !== false;
      const multi = batch.bom_scope === 'multi';
      const historical = multi && Number(batch.board_set_revision) !== Number(definition?.order_line?.board_set_revision);
      const modeChanged = multi && (batch.board_schedule_mode || 'shared') !== (definition?.order_line?.board_schedule_mode || 'shared');
      const legacyLocked = !multi && Boolean(batch.bom_locked);
      const frozenBoards = Boolean(batch.bom_locked || boards.some(board => board.bom_locked));
      const needsRefresh = multi && (!batch.board_set_confirmed_at || historical || modeChanged) && !frozenBoards;
      const canActivate = hasPlan && definition.confirmed && editable && !legacyLocked && (!multi || needsRefresh);
      const boardRows = multi ? boards.map(board => `<div class="board-manager-row"><div class="board-manager-name"><span class="board-ref">${escape(board.board_ref)}</span><strong>${escape(board.board_name)}</strong><small>${escape(board.bom_product_code)} · 每套成品 ${number(board.quantity_per_product)} 块 · 成品对应需板 ${number(board.required_quantity)} 片${batch.board_schedule_mode === 'independent' ? ` · 本板计划 ${number(board.planned_quantity)} 片` : ''}</small></div><div class="board-manager-version"><span>本批 BOM</span><strong>${board.bom_locked ? escape(board.bom_version || '已锁定') : '待锁定'}</strong><small>${board.bom_locked ? `${number(board.bom_item_count)} 项物料${board.missing_price_count ? ` · ${number(board.missing_price_count)} 项待补价` : ''}` : `默认 ${escape(board.bom_version || '待选')}`}</small></div><div class="board-manager-actions">${editable && !board.bom_locked && batch.board_set_confirmed_at ? `<button class="button small primary" type="button" data-lock-board="${attr(board.id)}">选择 BOM 并锁定</button>` : ''}${batch.board_schedule_mode === 'independent' ? `<button class="button small" type="button" data-open-board-schedule="${attr(board.id)}">本板排期</button>` : ''}</div></div>`).join('') : '';
      const initial = !hasPlan ? `<div class="notice"><p>该产品尚未配置多块板，当前批次可继续沿用原有单板 BOM。若产品包含多块 PCBA，请先在项目中添加并确认板卡。</p></div><button class="button small" id="batch-go-boards" type="button">配置项目板卡</button>` : !definition.confirmed ? `<div class="notice warning"><p>项目板卡组成尚未确认。请先核对板位、BOM 母件和每件产品用板数。</p></div><button class="button small" id="batch-go-boards" type="button">确认项目板卡</button>` : legacyLocked ? `<div class="notice warning"><p>本批已有单板 BOM 快照。请保留这份历史批次记录；新的多板组成请在后续生产批次启用。</p></div>` : `<div class="notice"><p>当前批次仍使用单板 BOM 记录。启用板卡后，本批将按每块板分别锁定 BOM；客户交付数量保持不变。</p></div>${editable ? '<form class="form-grid"><label class="wide">启用依据<textarea name="reason" required placeholder="例如工程确认本批按主板、按键板分别领料"></textarea></label></form>' : ''}`;
      const refreshForm = canActivate && multi ? '<form class="form-grid"><label class="wide">更新依据<textarea name="reason" required placeholder="例如项目板卡组成已确认，更新本批草案"></textarea></label></form>' : '';
      const multiNotice = multi && !batch.board_set_confirmed_at ? `<div class="notice warning"><p>本批板卡草案尚未启用，不能锁定 BOM。${definition?.confirmed ? '请按当前项目组成更新本批。' : '请先确认项目板卡组成。'}</p>${!definition?.confirmed ? `<button class="button small" id="batch-go-boards" type="button">确认项目板卡</button>` : ''}</div>` : multi && needsRefresh ? '<div class="notice warning"><p>项目板卡组成或排期方式已更新；本批尚未锁 BOM，可按当前设置重新生成板卡草案。</p></div>' : '';
      openModal({kicker:'生产批次 / 板卡 BOM',title:`${productionBatchName(batch)} · ${multi ? `${boards.length} 块板` : '板卡配置'}`,wide:true,body:`<div class="board-manager-intro"><strong>本批客户产品 ${number(batch.quantity)} 件</strong><span>本批采用${escape(boardModeNames[batch.board_schedule_mode] || boardModeNames.shared)}；板卡数量和物料单独计算，客户交付按成品件数登记一次。</span></div>${multi ? `${multiNotice}<div class="board-manager-list">${boardRows}</div><div class="board-manager-footer"><span>${boards.filter(board => board.bom_locked).length} / ${boards.length} 块板 BOM 已锁定</span><button class="button small" id="batch-boards-materials" type="button">查看汇总核料 →</button></div>${refreshForm}` : initial}`,
        submitText:canActivate ? multi ? '按当前组成更新本批' : '本批启用板卡' : '保存',onSubmit:canActivate ? async form => {
          const result = await saveMutation(`/production-batches/${batch.id}/boards/activate`,{version:batch.version,reason:getForm(form).reason.trim()},'POST','本批已启用板卡，请逐板锁定 BOM。');
          if (result) await openBatchBoards(batch.id);
        } : null});
      if (historical) $('.board-manager-intro',$('#modal-body'))?.insertAdjacentHTML('afterend',`<div class="notice warning"><p>本批使用第 ${escape(batch.board_set_revision)} 版板卡快照；项目当前为第 ${escape(definition.order_line.board_set_revision)} 版。历史批次保持原板卡组成，请按本批快照核料。</p></div>`);
      if (modeChanged && !needsRefresh) $('.board-manager-intro',$('#modal-body'))?.insertAdjacentHTML('afterend',`<div class="notice warning"><p>项目当前使用${escape(boardModeNames[definition.order_line.board_schedule_mode])}；本批已锁 BOM 或已有实际进度，继续沿用${escape(boardModeNames[batch.board_schedule_mode])}。新批次会使用项目当前设置。</p></div>`);
      $('#batch-go-boards')?.addEventListener('click',() => openProjectBoards(lineId));
      $('#batch-boards-materials')?.addEventListener('click',() => { closeModal(); openMaterialBatch(batch.id); });
      $$('[data-lock-board]',$('#modal-body')).forEach(button => button.onclick = () => openBatchBoardLock(batch,boards.find(board => Number(board.id) === Number(button.dataset.lockBoard))));
      $$('[data-open-board-schedule]',$('#modal-body')).forEach(button => button.onclick = () => openBoardSchedule(Number(button.dataset.openBoardSchedule)));
    } catch (error) { toast(error.message,true); }
  }
  async function openBatchBoardLock(batch,board) {
    if (!board) return;
    try {
      const catalog = list(await api(`/bom-catalog?product_code=${encodeURIComponent(board.bom_product_code)}`));
      const versions = new Map();
      for (const item of catalog) {
        const key = String(item.bom_version || '');
        if (!key) continue;
        if (!versions.has(key)) versions.set(key,{count:0,missing:0});
        const info = versions.get(key); info.count++;
        if (item.unit_price == null || item.unit_price === '') info.missing++;
      }
      if (!versions.size) {
        openModal({kicker:'逐板 BOM',title:`${board.board_name} · 目录待导入`,body:`<div class="notice warning"><p>找不到母件 ${escape(board.bom_product_code)} 的 BOM。请先导入该板的 BOM，再返回此批次选择版本。</p></div><button class="button small primary" id="board-bom-import" type="button">打开 BOM 导入</button>`});
        $('#board-bom-import').onclick = () => { closeModal(); state.view = 'imports'; state.importType = 'bom'; renderView(); };
        return;
      }
      const choices = [...versions].map(([version,info]) => [version,`${version} · ${info.count} 项${info.missing ? ` · ${info.missing} 项待补价` : ''}`]);
      openModal({kicker:'逐板 BOM',title:`锁定 ${board.board_name} 的 BOM`,body:`<div class="board-manager-intro"><strong>${escape(board.bom_product_code)}</strong><span>每件产品 ${number(board.quantity_per_product)} 块，本批共 ${number(board.required_quantity)} 块。锁定后该板 BOM 成为本批快照。</span></div><form class="form-grid">${formSelect('BOM 版本','bom_version',choices,versions.has(board.bom_version) ? board.bom_version : choices[0][0],true)}<label class="wide">锁定依据<textarea name="reason" required placeholder="例如工程确认版本及物料清单"></textarea></label></form>`,submitText:'锁定这块板',onSubmit:async form => {
        const values = getForm(form);
        const result = await saveMutation(`/production-batches/${batch.id}/boards/${board.id}/lock-from-catalog`,{version:board.version,batch_version:batch.version,bom_version:values.bom_version,reason:values.reason.trim()},'POST',`${board.board_name} BOM 已锁定。`);
        if (result) await openBatchBoards(batch.id);
      }});
    } catch (error) { toast(error.message,true); }
  }
  async function openBatchBomSnapshot(batchId,linkage) {
    const result = await api(`/production-batches/${batchId}`);
    const batch = result.batch || result;
    const items = list(batch.bom_items);
    openModal({kicker:'批次 BOM 快照',title:`${productionBatchName(batch)} · ${batch.bom_version || '版本待补'}`,wide:true,
      body:`<div class="bom-link-current"><div><span>项目产品编码</span><strong>${escape(linkage.product_code)}</strong><small>${escape(linkage.product_name || '')}</small></div><div><span>关联母件 / 本批锁定版本</span><strong>${escape(linkage.linked_product_code)} · ${escape(batch.bom_version || '')}</strong><small>本批锁定 ${items.length} 行；后续导入不会覆盖</small></div></div>${items.length ? `<div class="table-wrap"><table class="data-table"><thead><tr><th>行号</th><th>物料编码 / 名称</th><th class="num">单台用量</th><th class="num">损耗率</th><th class="num">单价 CNY</th></tr></thead><tbody>${items.map(item => `<tr><td>${escape(item.source_line_no || item.id)}</td><td><strong>${escape(item.material_code)}</strong><span class="sub">${escape(item.material_name || '')}</span></td><td class="num">${number(item.quantity_per)}</td><td class="num">${number(Number(item.loss_rate || 0) * 100)}%</td><td class="num">${item.unit_price == null ? '待填' : number(item.unit_price)}</td></tr>`).join('')}</tbody></table></div>` : empty('本批没有 BOM 物料行','请核对已锁定快照。')}<div class="bom-flow-actions"><button class="button primary" id="bom-snapshot-materials" type="button">逐项核料 →</button><button class="button" id="bom-snapshot-detail" type="button">完整批次记录</button></div>`});
    $('#bom-snapshot-materials').onclick = () => { closeModal(); openMaterialBatch(batchId); };
    $('#bom-snapshot-detail').onclick = () => { closeModal(); openBatch(batchId); };
  }
  async function openProjectBomFlow(projectKeyValue,batchId = null) {
    const project = state.projects.find(item => item.key === projectKeyValue);
    const line = project?.lines[0];
    if (!line) { toast('项目产品行已更新，请刷新后重试。',true); return; }
    try {
      const info = await api(`/order-lines/${line.id}/bom-linkage`);
      const batch = list(info.batches).find(item => Number(item.id) === Number(batchId));
      if (batch?.bom_locked) return openBatchBomSnapshot(batch.id,info);
      const catalogs = list(info.catalogs);
      if (!catalogs.length) {
        openModal({kicker:'项目与 BOM',title:`${project.name} 尚无可关联的 BOM`,body:`<div class="notice warning"><p>导入的 BOM 先保存在产品编码与版本目录。请导入含“产品编码、BOM 版本、物料编码、单位用量”的清单，再回来选择本项目使用的版本。</p></div><div class="bom-flow-actions"><button class="button primary" id="bom-flow-import" type="button">前往导入 BOM →</button>${batch ? '<button class="button" id="bom-flow-manual" type="button">手工填写本批 BOM</button>' : ''}</div>`});
        $('#bom-flow-import').onclick = () => { closeModal(); state.view = 'imports'; state.orderId = null; state.batchId = null; state.importType = 'bom'; renderView(); };
        $('#bom-flow-manual')?.addEventListener('click',async () => { const detail = await api(`/production-batches/${batch.id}`); openLockBom(detail.batch || detail); });
        return;
      }
      const selectedIndex = catalogs.findIndex(item => item.product_code === info.linked_product_code && item.bom_version === info.bom_version);
      const legacyFirstLink = /^PCBA-TEMP-/.test(String(info.product_code || '')) && !info.linked_explicitly;
      const preview = () => `<div class="bom-link-current"><div><span>项目产品</span><strong>${escape(info.product_name || project.name)}</strong><small>${escape(info.product_code)}</small></div><div><span>BOM 母件 / 默认版本</span><strong>${info.status === 'unlinked' ? '尚未关联' : `${escape(info.linked_product_code)} · ${escape(info.bom_version)}`}</strong><small>${escape(info.reason || '')}</small></div></div>`;
      openModal({kicker:'项目与 BOM',title:batch ? `为 ${productionBatchName(batch)} 关联并锁定 BOM` : `为 ${project.name} 关联 BOM`,wide:true,
        body:`${preview()}<div class="notice"><p>导入清单按“BOM 产品编码＋版本”保存。项目关联母件编码和默认版本；PP/MP 每个批次分别锁定自己的版本和物料快照。${info.locked_batch_count ? legacyFirstLink ? '临时项目允许首次补关联，已锁定批次的快照会保留。' : '本项目已有锁定批次，只能为新批次选择同一母件编码下的其他版本。' : ''}</p></div><form class="form-grid"><label class="wide">选择已导入的 BOM<select name="catalog_index" required><option value="" ${selectedIndex < 0 ? 'selected' : ''} disabled>请核对母件编码和版本后选择</option>${catalogs.map((item,index) => `<option value="${index}" ${index === selectedIndex ? 'selected' : ''}>${escape(item.product_code)} · ${escape(item.bom_version)} · ${number(item.item_count)} 项物料${Number(item.missing_price_count) ? ` · ${number(item.missing_price_count)} 项缺单价` : ''}${item.match === 'exact' ? ' · 当前匹配' : ''}</option>`).join('')}</select></label><label class="wide">关联及锁定原因<textarea name="reason" required placeholder="例如工程确认该产品使用的 BOM 编码、版本和报价"></textarea></label></form><p class="form-note">${batch ? `保存后将所选版本锁定到本批，再进入逐项核料。` : '关联完成后，请到每个 PP/MP 生产批次执行锁定。'}</p>${batch ? '<div class="bom-flow-actions"><button class="button small" id="bom-flow-manual" type="button">手工填写本批 BOM</button></div>' : ''}`,submitText:batch ? '关联并锁定本批' : '关联到项目',onSubmit:async form => {
          const choice = $('[name=catalog_index]',form).value;
          const chosen = choice === '' ? null : catalogs[Number(choice)];
          const reason = $('[name=reason]',form).value.trim();
          if (!chosen || !reason) { modalMessage('请选择 BOM 并填写关联原因。'); return; }
          try {
            if (info.locked_batch_count && !legacyFirstLink && chosen.product_code !== info.linked_product_code) { modalMessage('已有批次锁定 BOM，母件编码不能改变。请在同一母件编码下选择版本。'); return; }
            if (info.locked_batch_count && !legacyFirstLink && !batch && chosen.bom_version !== info.bom_version) { modalMessage('已有批次锁定时，请到具体生产批次选择并锁定其他版本。'); return; }
            if ((!info.locked_batch_count || legacyFirstLink) && (chosen.product_code !== info.linked_product_code || chosen.bom_version !== info.bom_version || info.status === 'unlinked' || legacyFirstLink)) {
              await api(`/order-lines/${line.id}/bom-linkage`,{method:'POST',body:{version:info.order_line.version,bom_product_code:chosen.product_code,bom_version:chosen.bom_version,reason}});
            }
            if (batch) {
              const latest = await api(`/production-batches/${batch.id}`);
              const current = latest.batch || latest;
              await api(`/production-batches/${batch.id}/bom-lock-from-catalog`,{method:'POST',body:{version:current.version,bom_version:chosen.bom_version,reason}});
            }
            closeModal(); toast(batch ? 'BOM 已关联并锁定到本批，可开始逐项核料。' : '项目 BOM 已关联。'); await renderView();
          } catch (error) {
            if (error.status === 409) showVersionConflict($('#modal-body'),() => { closeModal(); renderView(); });
            else modalMessage(error.message);
          }
        }});
      $('#bom-flow-manual')?.addEventListener('click',async () => { const detail = await api(`/production-batches/${batch.id}`); openLockBom(detail.batch || detail); });
    } catch (error) { toast(error.message,true); }
  }
  async function openProjectOrderEditor(orderId) {
    const [order,usersResult] = await Promise.all([api(`/orders/${encodeURIComponent(orderId)}`),api('/users')]);
    const users = list(usersResult.users || usersResult.items || usersResult).filter(user => user.active && user.status === 'active');
    const ownerOptions = [['',order.legacy_owner_name ? `原负责人 ${order.legacy_owner_name}（待绑定账号）` : '未分配'],...users.map(user => [user.id,`${user.display_name || user.username} · ${roleNames[user.role] || user.role}`])];
    const orderNo = /^(PCBA-TEMP-|PCBA-PROJ-)/.test(String(order.order_no || '')) ? '' : order.order_no || '';
    openModal({kicker:'项目资料',title:`补齐 ${projectNames(order)}`,wide:true,body:`<form class="form-grid">${formSelect('项目类型','project_mode',[['internal','内部研发'],['customer','客户订单']],order.project_mode || 'customer',true)}${formField('项目名称','project_name','text',order.project_name || projectNames(order),true)}${formField('正式客户订单号','order_no','text',orderNo)}${formField('客户名称','customer','text',order.customer === '待补录' ? '' : order.customer || '')}${formSelect('业务负责人','owner_id',ownerOptions,order.owner_id ?? '')}<label class="wide">修改原因<input name="reason" required placeholder="例如取得正式订单或负责人移交"></label></form>`,submitText:'保存项目资料',onSubmit:async form => {
      const changes = changedFields(form,order,['project_mode','project_name','order_no','customer','owner_id']);
      if (!Object.keys(changes).length) { closeModal(); return; }
      await saveMutation(`/orders/${order.id}`,withVersion(order,changes,getForm(form).reason.trim()),'PATCH','项目资料已更新');
    }});
    const form = $('#modal-body form'), mode = $('[name=project_mode]',form);
    const syncMode = () => {
      const customer = mode.value === 'customer';
      for (const name of ['order_no','customer']) { const field = $(`[name=${name}]`,form); field.required = customer; field.closest('label').hidden = !customer; }
    };
    mode.addEventListener('change',syncMode); syncMode();
  }
  async function openOwnershipSetup() {
    let setup;
    try { setup = await api('/setup/ownership'); }
    catch (error) { toast(`无法读取待配置的责任人：${error.message}`,true); return; }
    const orderGroups = list(setup.order_groups), stageGroups = list(setup.stage_groups), users = list(setup.users);
    if (!orderGroups.length && !stageGroups.length) { toast('历史业务负责人和生产节点均已完成账号指派。'); return; }
    const options = `<option value="">暂不指派</option>${users.map(user => `<option value="${attr(user.id)}">${escape(user.display_name)} · ${escape(roleNames[user.role] || user.role || '员工')}</option>`).join('')}`;
    const orderRows = orderGroups.map(group => `<label class="ownership-map-row"><span>原业务负责人 <strong>${escape(group.legacy_name)}</strong><small>${number(group.count)} 张订单</small></span><select name="order_binding" data-legacy-name="${attr(group.legacy_name)}">${options}</select></label>`).join('');
    const stageRows = stageGroups.map(group => `<label class="ownership-map-row"><span>${escape(phaseNames[group.phase] || group.phase)} / <strong>${escape(group.name)}</strong><small>${number(group.count)} 个未指派节点</small></span><select name="stage_binding" data-phase="${attr(group.phase)}" data-stage-name="${attr(group.name)}">${options}</select></label>`).join('');
    let preview = null, previewBindings = null;
    const selections = form => ({
      order_bindings:$$('[name=order_binding]',form).filter(el => el.value).map(el => ({legacy_name:el.dataset.legacyName,user_id:Number(el.value)})),
      stage_bindings:$$('[name=stage_binding]',form).filter(el => el.value).map(el => ({phase:el.dataset.phase,name:el.dataset.stageName,user_id:Number(el.value)})),
    });
    openModal({kicker:'管理员 / 责任链',title:'批量配置责任人',wide:true,body:`<div class="notice"><p>历史业务负责人按原姓名绑定账号；生产节点按阶段和工序分别指派。两类记录互不代替。只有未绑定的记录会进入本次预览。</p></div><form class="ownership-form">
      <section><h3>历史业务负责人</h3>${orderRows || '<p class="form-note">没有待绑定的历史业务负责人。</p>'}</section>
      <section><h3>生产节点责任人</h3>${stageRows || '<p class="form-note">没有未指派的生产节点。</p>'}</section>
      <div class="ownership-review-actions"><button class="button" type="button" id="ownership-preview">预览将修改的记录</button><span class="form-note">预览后填写原因，才可应用。</span></div>
      <div id="ownership-preview-result" class="ownership-preview-result" role="status" aria-live="polite"></div>
      <label class="ownership-reason">批量调整原因<textarea name="reason" required placeholder="例如历史项目责任人账号已确认，生产工序按岗位指派"></textarea></label>
    </form>`,submitText:'应用预览中的分配',onSubmit:async form => {
      const current = selections(form);
      if (!preview?.signature || JSON.stringify(current) !== JSON.stringify(previewBindings)) { modalMessage('请重新预览当前选择，再应用分配。'); return; }
      let applied;
      try { applied = await api('/setup/ownership/apply',{method:'POST',body:{...current,signature:preview.signature,reason:getForm(form).reason.trim()}}); }
      catch (error) { modalMessage(`应用失败：${error.message}`); return; }
      closeModal();
      toast(`已绑定 ${number(applied.summary?.orders ?? preview.summary?.orders ?? 0)} 张订单、指派 ${number(applied.summary?.stages ?? preview.summary?.stages ?? 0)} 个节点。`);
      try { await loadProjects(); await renderView(); }
      catch (error) { toast(`分配已应用，页面刷新失败：${error.message}`,true); }
    }});
    const form = $('#modal-body form'), apply = $('#modal-submit'), result = $('#ownership-preview-result');
    apply.disabled = true;
    form.addEventListener('change',event => {
      if (!event.target.matches('select')) return;
      preview = null; previewBindings = null; apply.disabled = true;
      result.textContent = '分配已调整，请重新预览。';
    });
    $('#ownership-preview').onclick = async event => {
      const bindings = selections(form);
      if (!bindings.order_bindings.length && !bindings.stage_bindings.length) { result.textContent = '请至少选择一组要绑定的账号。'; return; }
      const button = event.currentTarget; button.disabled = true; button.textContent = '正在生成预览…';
      try {
        const candidate = await api('/setup/ownership/preview',{method:'POST',body:bindings});
        if (!form.isConnected) return;
        preview = candidate; previewBindings = bindings;
        const orderItems = list(candidate.orders), stageItems = list(candidate.stages);
        result.innerHTML = `<div class="ownership-preview-heading"><strong>将更新 ${number(candidate.summary?.orders ?? orderItems.length)} 张订单、${number(candidate.summary?.stages ?? stageItems.length)} 个节点</strong><span>逐项核对后填写原因并应用</span></div>
          ${orderItems.length ? `<details open><summary>历史业务负责人（${orderItems.length}）</summary><div class="ownership-preview-list">${orderItems.map(item => `<div>${escape(item.order_no || `订单 #${item.id}`)} · ${escape(item.legacy_name)} → <strong>${escape(item.new_owner_name)}</strong></div>`).join('')}</div></details>` : ''}
          ${stageItems.length ? `<details open><summary>生产节点（${stageItems.length}）</summary><div class="ownership-preview-list">${stageItems.map(item => `<div>批次 #${escape(item.batch_id)} · ${escape(phaseNames[item.phase] || item.phase || '')} ${escape(item.name)} → <strong>${escape(item.new_owner_name)}</strong></div>`).join('')}</div></details>` : ''}`;
        apply.disabled = !(orderItems.length || stageItems.length);
      } catch (error) { preview = null; previewBindings = null; apply.disabled = true; result.textContent = `预览失败：${error.message}`; }
      finally { if (button.isConnected) { button.disabled = false; button.textContent = '重新预览将修改的记录'; } }
    };
  }
  async function renderPortfolio() {
    const generation = renderGeneration;
    const request = ++portfolioGeneration;
    const [,overview,linkageResult,materialResult,stageResult,boardOverview] = await Promise.all([loadProjects(),api('/overview').catch(() => null),api('/bom-linkages').catch(() => []),api('/materials').catch(() => ({batches:[]})),api('/stage-progress').catch(() => []),api('/board-overview').catch(() => ({order_lines:{},production_batches:{}}))]);
    if (generation !== renderGeneration || request !== portfolioGeneration) return;
    state.overview = overview;
    state.boardOverview = boardOverview;
    const projects = state.selectedProject ? state.projects.filter(project => project.key === state.selectedProject) : state.projects;
    const batches = projects.flatMap(project => project.batches);
    const counts = Object.fromEntries(['all','rd','pp','mp','unclassified'].map(phase => [phase,phase === 'all' ? batches.length : batches.filter(batch => phaseOf(batch) === phase).length]));
    const visibleProjects = state.phase === 'all' ? projects : projects.filter(project => project.batches.some(batch => phaseOf(batch) === state.phase));
    const visibleBatchCount = visibleProjects.reduce((sum,project) => sum + project.batches.filter(batch => state.phase === 'all' || phaseOf(batch) === state.phase).length,0);
    const assessments = new Map(list(state.overview?.assessment).map(item => [String(item.order_id),item]));
    const allRisks = list(state.overview?.risks);
    const projectRiskLevel = project => {
      const deliveryIds = new Set(project.lines.flatMap(line => list(line.delivery_batches).map(batch => Number(batch.id))));
      const productionIds = new Set(project.batches.map(batch => Number(batch.id)));
      const related = allRisks.filter(risk => deliveryIds.has(Number(risk.delivery_batch_id)) || productionIds.has(Number(risk.production_batch_id)));
      return related.some(risk => risk.level === 'red') ? 'red' : related.some(risk => risk.level === 'yellow') ? 'yellow' : '';
    };
    const linkageMap = new Map(list(linkageResult.linkages || linkageResult.items || linkageResult).map(item => [Number(item.order_line_id),item]));
    const materialMap = new Map(list(materialResult.batches || materialResult.items || materialResult).map(item => [Number(item.id),item]));
    const stageMap = new Map(list(stageResult.progress || stageResult.items || stageResult).map(item => [Number(item.production_batch_id),item]));
    const adminSetup = state.user?.role === 'admin' ? '<button class="button" id="portfolio-ownership" type="button">批量配置责任人</button>' : '';
    const exportLabel = state.selectedProject || state.phase !== 'all' ? '导出当前筛选排期 Excel' : '导出全部项目排期 Excel';
    $('#content').innerHTML = pageHead('项目 / 排期台账',state.selectedProject ? projects[0]?.name || '项目排期' : '项目排期','先关联 BOM，逐项核对物料，再查看生产节点；问题集中在“待处理事项”。',`${adminSetup}<button class="button" id="portfolio-export" type="button">${exportLabel}</button><button class="button" id="portfolio-import">导入销售订单</button><button class="button primary" id="portfolio-create">+ 新建项目</button>`) +
      `<div class="phase-tabs" role="group" aria-label="筛选排期阶段">${Object.entries(phaseNames).map(([phase,label]) => `<button type="button" aria-pressed="${state.phase === phase}" class="phase-tab ${state.phase === phase ? 'active' : ''}" data-phase="${phase}">${escape(label)}<span>${counts[phase] || 0}</span></button>`).join('')}</div>` +
      `<div class="toolbar portfolio-toolbar"><div class="search"><input id="portfolio-filter" aria-label="搜索项目、客户、订单或负责人" placeholder="搜索项目、客户、订单或负责人" value="${attr(state.portfolioFilter)}"></div><label>排序 <select id="portfolio-sort" aria-label="项目排序"><option value="risk" ${state.portfolioSort === 'risk' ? 'selected' : ''}>风险优先</option><option value="due" ${state.portfolioSort === 'due' ? 'selected' : ''}>交期最近</option><option value="name" ${state.portfolioSort === 'name' ? 'selected' : ''}>项目名称</option></select></label><span class="muted small-copy" id="portfolio-result-count" role="status"></span></div>` +
      `<p class="form-note portfolio-export-note">关键词和排序仅调整页面显示；Excel 按顶部项目选择和阶段导出。</p>` +
      `${panel('项目与阶段排期','<div id="portfolio-list"></div><div id="portfolio-pages" class="portfolio-pages"></div>',`<small>${visibleProjects.length} 个项目 · ${visibleBatchCount} 个阶段批次</small>`,true)}`;
    $('#portfolio-create').onclick = openCreateOrder;
    $('#portfolio-export').onclick = event => exportProjectSchedule(event.currentTarget);
    $('#portfolio-ownership')?.addEventListener('click',openOwnershipSetup);
    $('#portfolio-import').onclick = () => { state.view = 'imports'; state.orderId = null; state.batchId = null; window.scrollTo(0,0); renderView(); };
    $$('[data-phase]').forEach(button => button.onclick = () => { state.phase = button.dataset.phase; saveUiRoute(); renderPortfolio(); });
    const dueKey = project => {
      const focus = deliveryFocus(project.lines);
      return focus?.due_date || focus?.promised_date || project.batches.map(batch => batch.target_date).filter(Boolean).sort()[0] || '9999-12-31';
    };
    const rank = project => {
      if (state.orders.find(order => Number(order.id) === Number(project.order_id))?.status === 'complete') return 4;
      const risk = projectRiskLevel(project);
      return risk === 'red' ? 0 : risk === 'yellow' ? 1 : assessments.get(String(project.order_id))?.assessable === false ? 2 : 3;
    };
    const renderLedger = () => {
      const query = state.portfolioFilter.trim().toLowerCase();
      const filtered = visibleProjects.filter(project => !query || [project.name,project.code,project.order_no,project.customer,project.owner_name,...project.batches.map(batch => batch.batch_no)].some(value => String(value || '').toLowerCase().includes(query)));
      filtered.sort((a,b) => state.portfolioSort === 'name' ? a.name.localeCompare(b.name,'zh-CN') : state.portfolioSort === 'due' ? dueKey(a).localeCompare(dueKey(b)) || a.name.localeCompare(b.name,'zh-CN') : rank(a) - rank(b) || dueKey(a).localeCompare(dueKey(b)) || a.name.localeCompare(b.name,'zh-CN'));
      const pageSize = 12, pages = Math.max(1,Math.ceil(filtered.length / pageSize));
      state.portfolioPage = Math.min(Math.max(1,state.portfolioPage),pages);
      const start = (state.portfolioPage - 1) * pageSize;
      const shown = filtered.slice(start,start + pageSize);
      $('#portfolio-result-count').textContent = query ? `匹配 ${filtered.length} / ${visibleProjects.length} 个项目` : `共 ${visibleProjects.length} 个项目`;
      $('#portfolio-list').innerHTML = shown.length ? `<div class="project-ledger">${shown.map(project => projectEntry(project,assessments.get(String(project.order_id)),linkageMap,materialMap,stageMap,projectRiskLevel(project))).join('')}</div>` : empty(query ? '没有匹配的项目' : '此阶段暂无项目排期',query ? '请更换关键词。' : '可手动新建项目，或到数据导入页上传销售订单。');
      $('#portfolio-pages').innerHTML = filtered.length > pageSize ? `<span>显示 ${start + 1}–${Math.min(start + pageSize,filtered.length)} / ${filtered.length}</span><button class="button small" type="button" data-portfolio-page="previous" ${state.portfolioPage === 1 ? 'disabled' : ''}>上一页</button><span>${state.portfolioPage} / ${pages} 页</span><button class="button small" type="button" data-portfolio-page="next" ${state.portfolioPage === pages ? 'disabled' : ''}>下一页</button>` : '';
      $$('[data-portfolio-page]').forEach(button => button.onclick = () => { state.portfolioPage += button.dataset.portfolioPage === 'next' ? 1 : -1; renderLedger(); $('#portfolio-list').scrollIntoView({block:'start'}); });
      $$('[data-project-details]').forEach(details => details.ontoggle = () => {
        if (!details.isConnected) return;
        if (details.open) {
          state.portfolioExpanded = details.dataset.projectDetails;
          $$('[data-project-details]').filter(other => other !== details && other.open).forEach(other => { other.open = false; });
        } else if (state.portfolioExpanded === details.dataset.projectDetails) state.portfolioExpanded = '';
      });
      $$('[data-open-project]').forEach(button => button.onclick = () => showNodeDrawer(Number(button.dataset.openProject)));
      $$('[data-project-action]').forEach(button => button.onclick = () => openProjectAction(button.dataset.projectKey,button.dataset.projectAction));
      $$('[data-project-bom]').forEach(button => button.onclick = () => openProjectBomFlow(button.dataset.projectBom,Number(button.dataset.batchId)));
      $$('[data-project-materials]').forEach(button => button.onclick = () => openMaterialBatch(Number(button.dataset.projectMaterials)));
      $$('[data-manage-boards]').forEach(button => button.onclick = () => openProjectBoards(Number(button.dataset.manageBoards)));
      $$('[data-batch-boards]').forEach(button => button.onclick = () => openBatchBoards(Number(button.dataset.batchBoards)));
    };
    $('#portfolio-filter').oninput = event => { state.portfolioFilter = event.target.value; state.portfolioPage = 1; renderLedger(); };
    $('#portfolio-sort').onchange = event => { state.portfolioSort = event.target.value; state.portfolioPage = 1; renderLedger(); };
    renderLedger();
    saveUiRoute();
  }
  function togglePhaseRow(batchId) {
    const row = $(`[data-phase-row="${CSS.escape(String(batchId))}"]`), batch = state.projects.flatMap(project => project.batches).find(item => String(item.id) === String(batchId));
    if (!row || !batch) return;
    const cell = row.querySelector('td:last-child');
    if (cell.querySelector('[data-save-phase]')) { renderPortfolio(); return; }
    cell.innerHTML = `<div class="form-stack"><label>阶段<select data-phase-value>${Object.entries(phaseNames).filter(([key]) => key !== 'all').map(([key,label]) => `<option value="${key}" ${phaseOf(batch) === key ? 'selected' : ''}>${escape(label)}</option>`).join('')}</select></label><label>本阶段单号<input data-phase-order value="${attr(batch.phase_order_no || '')}" placeholder="可选"></label><label>调整原因<input data-phase-reason placeholder="记录分类依据"></label><div class="table-actions"><button class="button small primary" data-save-phase="${attr(batchId)}">保存阶段</button><button class="button small" data-cancel-phase>取消</button></div></div>`;
    $('[data-save-phase]',cell).onclick = () => savePhaseRow(batch,row);
    $('[data-cancel-phase]',cell).onclick = renderPortfolio;
  }
  async function savePhaseRow(batch,row) {
    const phase = $('[data-phase-value]',row).value, phaseOrderNo = $('[data-phase-order]',row).value.trim(), reason = $('[data-phase-reason]',row).value.trim();
    const changes = {};
    if (phase !== (batch.phase || 'unclassified')) changes.phase = phase;
    if (fieldChanged(batch.phase_order_no,phaseOrderNo)) changes.phase_order_no = phaseOrderNo || null;
    if (!Object.keys(changes).length) { toast('阶段与单号没有变化。'); return; }
    if (!reason) { toast('请填写阶段或单号调整原因。',true); $('[data-phase-reason]',row).focus(); return; }
    try { await api(`/production-batches/${batch.id}`,{method:'PATCH',body:withVersion(batch,changes,reason)}); toast('阶段排期已更新。'); renderPortfolio(); }
    catch (error) {
      if (error.status === 409) showVersionConflict(row.querySelector('td:last-child'),() => renderPortfolio());
      else toast(error.message,true);
    }
  }
  async function renderDashboard() {
    const generation = renderGeneration;
    const data = await api('/overview');
    const stageProgress = await api('/stage-progress').catch(() => null);
    if (generation !== renderGeneration) return;
    state.overview = data;
    const summary = data.summary || {};
    $('#task-count').textContent = summary.tasks_open || '';
    $('#task-count').hidden = !summary.tasks_open;
    const risks = list(data.risks), tasks = list(data.tasks), orders = list(data.orders);
    const assessment = list(data.assessment);
    const coverageKnown = summary.assessable != null && summary.unassessable != null;
    const unassessable = Number(summary.unassessable ?? assessment.filter(item => !item.assessable).length);
    const assessable = coverageKnown ? Number(summary.assessable) : null;
    const missingAssessment = assessment.filter(item => !item.assessable);
    const coverage = assessable == null ? '交期判断覆盖待计算' : `${assessable} / ${number(summary.orders_open ?? orders.length)} 张订单可判断交期`;
    const progressByBatch = new Map(list(stageProgress).map(item => [Number(item.production_batch_id),item]));
    const batches = orders.flatMap(order => list(order.lines).flatMap(line => list(line.production_batches).map(batch => ({order,line,batch,progress:progressByBatch.get(Number(batch.id))}))));
    const phaseLanes = ['rd','pp','mp',...(batches.some(item => phaseOf(item.batch) === 'unclassified') ? ['unclassified'] : [])];
    const batchBoard = `<div class="dashboard-phase-board">${phaseLanes.map(phase => {
      const lane = batches.filter(item => phaseOf(item.batch) === phase);
      return `<section class="dashboard-phase-lane"><div class="dashboard-phase-head"><h3>${escape(phaseNames[phase])}</h3><span>${number(lane.length)} 批</span></div><div class="dashboard-phase-items">${lane.length ? lane.slice(0,3).map(({order,batch,progress}) => {
        const total = Number(progress?.total || 0), done = Number(progress?.completed || 0);
        const percent = total ? Math.max(0,Math.min(100,Math.round(100 * done / total))) : 0;
        const node = stageProgress === null ? '节点状态暂不可用' : !total ? '节点待生成' : done >= total ? '节点已完成' : progress.current_name || '节点待核对';
        const target = phase === 'rd' ? batch.target_date : batch.commit_finish;
        return `<button class="dashboard-batch${total && done >= total ? ' is-complete' : ''}" type="button" data-dashboard-batch="${attr(batch.id)}" aria-label="查看 ${attr(projectNames(order))} ${attr(productionBatchName(batch))} 生产节点"><span class="dashboard-batch-top"><strong>${escape(projectNames(order))}</strong><b>${total ? `${done}/${total}` : '—'}</b></span><span class="dashboard-batch-meta">${escape(productionBatchName(batch))} · ${number(batch.quantity)} 件</span><span class="dashboard-batch-track" aria-hidden="true"><i style="width:${percent}%"></i></span><span class="dashboard-batch-foot"><span>${escape(node)}</span><span>${target ? `目标 ${escape(date(target))}` : '目标待补'}</span></span></button>`;
      }).join('') : '<p class="dashboard-phase-empty">暂无此阶段批次</p>'}${lane.length > 3 ? `<span class="dashboard-phase-more">另有 ${number(lane.length - 3)} 批，请在项目排期查看</span>` : ''}</div></section>`;
    }).join('')}</div>`;
    const metricHtml = `<div class="summary-grid dashboard-metrics">
      <div class="metric"><span>在跟订单</span><strong>${number(summary.orders_open ?? 0)}</strong><small>当前未完结的客户订单</small></div>
      <div class="metric ${unassessable > 0 ? 'warn' : ''}"><span>交期可判断</span><strong>${assessable == null ? '—' : `${number(assessable)}<em> / ${number(summary.orders_open ?? orders.length)}</em>`}</strong><small>${unassessable > 0 ? `${number(unassessable)} 张缺少关键依据` : '订单交期判断依据已齐'}</small></div>
      <div class="metric ${Number(summary.red ?? 0) > 0 ? 'danger' : Number(summary.yellow ?? 0) > 0 ? 'warn' : ''}"><span>交期预警</span><strong>${number(Number(summary.red ?? 0) + Number(summary.yellow ?? 0))}</strong><small><span class="metric-red">红 ${number(summary.red ?? 0)}</span><span class="metric-yellow">黄 ${number(summary.yellow ?? 0)}</span> · 仅已识别风险</small></div>
      <div class="metric"><span>待确认事项</span><strong>${number(summary.tasks_open ?? 0)}</strong><small>责任人需处理并确认</small></div>
    </div>`;
    const riskHtml = risks.length ? risks.slice(0, 5).map(item => {
      const provisional = item.provisional_date || (item.is_provisional ? item.forecast_date : null);
      return `<div class="risk-row">${badge(item.level || item.severity || item.status, item.label || item.level_text)}<div class="risk-main"><strong>${escape(item.order_no || item.title || item.name || '订单风险')}</strong><p>${escape(item.message || item.description || item.reason || '请打开订单核对')}</p>${item.is_provisional || provisional ? `<p>临时估算${provisional ? ` ${escape(date(provisional))}` : ''}，待采购或厂商确认</p>` : ''}</div><button class="button linklike" data-open-order="${attr(item.order_id || '')}" ${!item.order_id ? 'disabled' : ''}>查看</button></div>`;
    }).join('') : empty(!coverageKnown ? '交期判断状态待更新' : unassessable > 0 ? '尚不能判断全部交期风险' : '已可判断的订单暂无红黄风险',!coverageKnown ? '请刷新数据并核对交付批次、物料和厂商承诺。' : unassessable > 0 ? `${unassessable} 张订单缺少关键日期或资料；补齐前，风险为 0 不代表交期安全。` : '系统会根据交期、物料与厂商承诺自动更新此处。');
    const assessmentHtml = missingAssessment.length ? missingAssessment.slice(0,4).map(item => {
      const reasons = list(item.missing_reasons).map(reason => reason.label || reason.code || reason).filter(Boolean);
      return `<div class="risk-row assessment-row"><span class="badge yellow">待补</span><div class="risk-main"><strong>${escape(item.project_name || item.order_no || `订单 #${item.order_id}`)}</strong><p>${escape(reasons.slice(0,3).join(' · ') || '交期判断依据待补录')}${reasons.length > 3 ? ` · 另 ${reasons.length - 3} 项` : ''}</p></div><button class="button linklike" data-assessment-order="${attr(item.order_id)}" type="button">补资料 →</button></div>`;
    }).join('') : empty(unassessable > 0 ? '关键资料待核对' : '没有待补的交期判断资料',unassessable > 0 ? '请打开项目排期，逐项核对订单、BOM、物料与厂商日期。' : '仍请定期核对金蝶数据快照和厂商承诺。');
    const taskHtml = tasks.length ? tasks.slice(0,5).map((task,index) => `<button class="dashboard-task${task.condition_active ? ' is-active' : ''}" type="button" data-dashboard-task="${attr(task.id)}" aria-label="查看待办：${attr(task.title || task.kind || '待处理事项')}"><span class="dashboard-task-index">${String(index + 1).padStart(2,'0')}</span><span><strong>${escape(task.title || task.kind || '待处理事项')}</strong>${task.description || task.message ? `<span class="dashboard-task-description">${escape(task.description || task.message)}</span>` : ''}</span><span class="dashboard-task-dot" aria-hidden="true"></span></button>`).join('') + (tasks.length > 5 ? `<p class="dashboard-task-more">另有 ${number(tasks.length - 5)} 项，请到待办中心处理。</p>` : '') : empty('待办已清空','风险消除后，责任人仍需确认关联待办。');
    $('#content').innerHTML = pageHead('工作台 / 今日','交付总览',`${coverage}。先补关键资料，再处理已识别的交期风险与责任待办。`,`${state.user?.role === 'admin' ? '<button class="button" id="dash-ownership">批量配置责任人</button>' : ''}<button class="button" id="dash-refresh">刷新数据</button><button class="button primary" id="dash-order">+ 新建订单</button>`) +
      metricHtml +
      `<div class="dashboard-grid overview-layout"><div class="column-stack">${panel('阶段批次 · 生产节点',batchBoard,`<small>${number(batches.length)} 批</small><button class="button linklike" data-view="portfolio">项目排期 →</button>`,true)}<div class="dashboard-followups">${panel('交期判断缺口',assessmentHtml,`<small>${unassessable} 张无法判断</small><button class="button linklike" data-view="portfolio">全部项目 →</button>`,true)}${panel('交期风险',riskHtml,`<small>${risks.length} 项</small><button class="button linklike" data-view="orders">全部订单 →</button>`,true)}</div></div><div class="column-stack">${panel('待确认事项',taskHtml,`<button class="button linklike" data-view="tasks">查看全部 →</button>`,true)}${panel('数据来源',sourceStatus(data))}</div></div>`;
    $('#dash-refresh').onclick = renderView;
    $('#dash-order').onclick = openCreateOrder;
    $('#dash-ownership')?.addEventListener('click',openOwnershipSetup);
    $$('[data-open-order]', $('#content')).forEach(el => el.onclick = () => openOrder(el.dataset.openOrder));
    $$('[data-dashboard-batch]', $('#content')).forEach(el => el.onclick = async () => {
      await loadProjects().catch(() => {});
      await showNodeDrawer(Number(el.dataset.dashboardBatch));
    });
    $$('[data-dashboard-task]', $('#content')).forEach(el => el.onclick = async () => {
      try {
        const current = list(await api('/tasks?status=open')).find(item => String(item.id) === el.dataset.dashboardTask);
        if (current) await openTaskTarget(current);
        else toast('这项待办已更新，请打开待办中心查看。',true);
      } catch (error) { toast(error.message,true); }
    });
    $$('[data-assessment-order]', $('#content')).forEach(el => el.onclick = async () => {
      await loadProjects().catch(() => {});
      const project = state.projects.find(item => Number(item.order_id) === Number(el.dataset.assessmentOrder));
      state.selectedProject = project?.key || '';
      state.phase = 'all'; state.view = 'portfolio'; state.orderId = null; state.batchId = null;
      window.scrollTo(0,0); renderView();
    });
    $$('[data-view]', $('#content')).forEach(el => el.onclick = () => { state.view = el.dataset.view; renderView(); });
  }
  function sourceStatus(data) {
    const sources = data.sources || data.import_sources || data.imports || {};
    const items = Array.isArray(sources) ? sources : Object.entries(sources).map(([type,value]) => ({type,...(typeof value === 'object' ? value : {imported_at:value})}));
    return items.length ? `<div class="small-copy">${items.map(item => `<div class="key-values"><div><dt>${escape(importTypes[item.type] || item.type || '数据')}</dt><dd>${escape(stamp(sourceTime(item)))}</dd></div><div><dt>来源状态</dt><dd>${item.imported_at || item.as_of ? '导入快照' : '待导入'}</dd></div></div>`).join('')}</div><p class="form-note">KIS 导出按需导入；库存与采购日期不是实时同步。</p>` : `<div class="notice warning"><p>尚无 KIS 导入时间。库存与采购记录可能未同步，请在导入中心核对来源。</p></div><button class="button small" data-view="imports">打开导入中心</button>`;
  }
  function projectNames(order) {
    if (order.project_name) return order.project_name;
    const names = list(order.lines).map(line => line.product_name || line.product_code).filter(Boolean);
    return names.length ? names.join(' / ') : '项目/产品待补录';
  }
  function ownerDisplay(order) {
    return order.owner_name || order.legacy_owner_name || '未分配';
  }
  function legacyOwnerUnbound(order) {
    return order.owner_binding_status === 'legacy_unbound' || (!order.owner_id && Boolean(order.legacy_owner_name));
  }
  function routeCard(order) {
    const lines = list(order.lines);
    const productions = lines.flatMap(line => list(line.production_batches));
    const deliveries = lines.flatMap(line => list(line.delivery_batches));
    const next = [...deliveries].sort((a,b) => String(a.due_date || a.promised_date || '9999').localeCompare(String(b.due_date || b.promised_date || '9999')))[0];
    const risks = list(state.overview?.risks).filter(item => String(item.order_id) === String(order.id));
    const status = risks.some(item => item.level === 'red') ? 'red' : risks.some(item => item.level === 'yellow') ? 'yellow' : 'gray';
    const forecast = next?.forecast || {};
    const bomLocked = productions.length > 0 && productions.every(batch => batch.bom_locked);
    const vendorCommitted = productions.length > 0 && productions.every(batch => batch.commit_start && batch.commit_finish);
    const deliveryComplete = order.status === 'complete';
    const legacy = Boolean(order.legacy_record);
    const heading = legacy ? projectNames(order) : `${order.order_no || `订单 #${order.id}`} · ${order.customer || '客户待补录'}`;
    const caption = legacy ? `项目单号 ${order.order_no} · 负责人 ${ownerDisplay(order)}${legacyOwnerUnbound(order) ? '（账号待绑定）' : ''}`
                           : `${projectNames(order)} · 负责人 ${ownerDisplay(order)}`;
    return `<article class="route-card" data-order-id="${attr(order.id)}" tabindex="0" role="button" aria-label="查看订单 ${attr(order.order_no || order.id)}"><div class="route-head"><div><strong>${escape(heading)}</strong><small>${escape(caption)}</small></div>${badge(deliveryComplete ? 'green' : status, deliveryComplete ? '已交付' : risks.length ? severityText(status) : '待跟进')}</div><div class="route-track"><div class="route-stop ${bomLocked ? 'done' : ''}"><strong>BOM 锁定</strong><small>${bomLocked ? '版本已确认' : productions.length ? '待锁定' : '待建立批次'}</small></div><div class="route-stop ${vendorCommitted ? 'done' : ''}"><strong>厂商承诺</strong><small>${vendorCommitted ? '开工/完工已确认' : '待确认日期'}</small></div><div class="route-stop ${forecast.formal_date ? 'done' : ''}"><strong>交付预测</strong><small>${forecast.formal_date ? escape(date(forecast.formal_date)) : '无法确定'}</small></div><div class="route-stop ${deliveryComplete ? 'done' : ''}"><strong>客户交付</strong><small>${deliveries.length ? `${number(order.completed_quantity ?? 0)} / ${number(order.total_quantity ?? 0)} 件` : '待建立批次'}</small></div></div><div class="route-foot"><span>要求 ${escape(date(next?.due_date))} · 承诺 ${escape(date(next?.promised_date))}</span><b>${forecast.provisional_date ? `临时估算 ${escape(date(forecast.provisional_date))}` : escape(risks[0]?.message || '')}</b></div></article>`;
  }

  function orderTableRow(order) {
    const ownerNote = legacyOwnerUnbound(order) ? '账号待绑定' : '';
    const customer = order.customer && order.customer !== '待补录' ? escape(order.customer) : '<span class="badge gray">待补客户</span>';
    const focus = deliveryFocus(order.lines);
    const forecast = focus?.forecast || {};
    const formal = forecast.status === 'unknown' ? null : forecast.formal_date;
    const dueDifference = calendarDeviation(formal,focus?.due_date);
    const promisedDifference = calendarDeviation(formal,focus?.promised_date);
    const localToday = new Date().toLocaleDateString('sv-SE',{timeZone:'Asia/Taipei'});
    const pending = list(order.lines).flatMap(line => list(line.delivery_batches)).filter(batch => batch.forecast?.status !== 'complete');
    const risky = pending.filter(batch => {
      const predicted = batch.forecast?.formal_date || batch.forecast?.provisional_date;
      return [batch.due_date,batch.promised_date].some(baseline => baseline && (baseline < localToday || predicted && predicted > baseline));
    });
    const focusRisk = risky.some(batch => Number(batch.id) === Number(focus?.id));
    const otherRiskCount = risky.length - Number(focusRisk);
    const unknownCount = pending.filter(batch => batch.forecast?.status === 'unknown' || !batch.forecast?.formal_date).length;
    const forecastText = forecast.status === 'complete' ? `已完成 ${date(formal)}` : formal ? date(formal) : '无法确定';
    const deviationText = formal && focus ? `对要求 ${dueDifference || '待补日期'} · 对承诺 ${promisedDifference || '待补日期'}` : forecast.provisional_date ? `临时估算 ${date(forecast.provisional_date)} · 待确认` : '待补资料或厂商确认';
    const overallStatus = order.status === 'complete' ? badge('green','已完成') : risky.length ? badge('red',`${risky.length} 批交期风险`) : unknownCount ? badge('yellow',`${unknownCount} 批交期待确认`) : badge('gray','跟进中');
    return `<tr><td data-label="项目 / 订单"><strong>${escape(projectNames(order))}</strong><span class="sub mono">${escape(order.order_no || `订单 #${order.id}`)} · ${number(list(order.lines).length)} 个产品行</span></td><td data-label="客户 / 负责人">${customer}<span class="sub">负责人 ${escape(ownerDisplay(order))}${ownerNote ? ` · ${ownerNote}` : ''}</span></td><td data-label="最近交付批次">${focus ? `<strong>${escape(focus.batch_no)}</strong><span class="sub">${escape(focus.product_name)} · ${number(focus.quantity)} 件</span>` : '<span class="badge yellow">待建立交付批次</span>'}</td><td class="order-dates" data-label="要求 / 承诺"><span>要求 <strong>${escape(date(focus?.due_date))}</strong></span><span>承诺 <strong>${escape(date(focus?.promised_date))}</strong></span></td><td class="order-forecast" data-label="正式预测 / 偏差"><strong>${escape(forecastText)}</strong><span class="sub ${focusRisk ? 'is-late' : ''}">${escape(deviationText)}${formal && focus ? '（自然日）' : ''}</span>${otherRiskCount ? `<span class="sub is-late">另有 ${otherRiskCount} 批存在交期风险</span>` : ''}</td><td data-label="已交付 / 总量"><strong class="mono">${number(order.completed_quantity ?? 0)} / ${number(order.total_quantity ?? 0)}</strong><span class="sub">${overallStatus}</span></td><td data-label="操作"><button class="button small" data-order-id="${attr(order.id)}">查看</button></td></tr>`;
  }

  function customerExportLabel() {
    return state.ordersFilter.trim() ? '导出当前筛选客户订单 Excel' : '导出全部客户订单 Excel';
  }
  async function renderOrders() {
    const generation = renderGeneration;
    const rows = await api('/orders');
    if (generation !== renderGeneration) return;
    state.orders = list(rows.orders || rows.items || rows);
    const exportLabel = customerExportLabel();
    $('#content').innerHTML = pageHead('业务 / 项目台账','项目与客户订单','内部研发按阶段目标跟踪；客户订单按交付批次分别核对交期。',`<button class="button" id="export-customer-orders" type="button">${exportLabel}</button><button class="button primary" id="create-order">+ 新建项目</button>`) +
      `<div class="toolbar orders-toolbar"><div class="search"><input id="order-search" aria-label="搜索单号、客户或产品" placeholder="搜索单号、客户或产品" value="${attr(state.ordersFilter)}"></div><label>排序 <select id="order-sort" aria-label="客户订单排序"><option value="risk" ${state.ordersSort === 'risk' ? 'selected' : ''}>交期风险优先</option><option value="due" ${state.ordersSort === 'due' ? 'selected' : ''}>交期最近</option><option value="name" ${state.ordersSort === 'name' ? 'selected' : ''}>项目名称</option></select></label><span class="muted small-copy" id="order-result-count" role="status">共 ${state.orders.length} 张订单</span></div>` +
      `<p class="form-note">导出仅包含客户订单，不包含内部研发项目；搜索词决定导出范围，排序和翻页不影响导出。</p>` +
      panel('订单列表', state.orders.length ? `<div id="orders-no-match" class="empty" hidden><strong>没有匹配的订单</strong><p>尝试其他单号、客户或产品关键词。</p></div><div id="orders-table" class="table-wrap"><table class="data-table order-scan-table"><thead><tr><th>项目 / 订单</th><th>客户 / 负责人</th><th>最近交付批次</th><th>要求 / 承诺</th><th>正式预测 / 偏差</th><th>已交付 / 总量</th><th>操作</th></tr></thead><tbody></tbody></table></div><div id="orders-pages" class="portfolio-pages"></div>` : empty('还没有订单','点击“新建订单”开始跟踪，或先导入 KIS 销售订单。'),'',true);
    $('#create-order').onclick = openCreateOrder;
    $('#export-customer-orders').onclick = event => exportCustomerOrders(event.currentTarget);
    const dueKey = order => {
      const focus = deliveryFocus(order.lines);
      return focus?.due_date || focus?.promised_date || '9999-12-31';
    };
    const riskRank = order => {
      if (order.status === 'complete') return 3;
      const deliveries = list(order.lines).flatMap(line => list(line.delivery_batches)).filter(batch => batch.forecast?.status !== 'complete');
      const localToday = new Date().toLocaleDateString('sv-SE',{timeZone:'Asia/Taipei'});
      if (deliveries.some(batch => {
        const predicted = batch.forecast?.formal_date || batch.forecast?.provisional_date;
        return [batch.due_date,batch.promised_date].some(baseline => baseline && (baseline < localToday || predicted && predicted > baseline));
      })) return 0;
      return !deliveries.length || deliveries.some(batch => !batch.forecast?.formal_date || batch.forecast.status === 'unknown') ? 1 : 2;
    };
    const renderRows = () => {
      if (!state.orders.length) return;
      const query = state.ordersFilter.trim().toLowerCase();
      const filtered = state.orders.filter(order => !query || [order.project_name,order.order_no,order.customer,...list(order.lines).flatMap(line => [line.product_code,line.product_name])].some(value => String(value || '').toLowerCase().includes(query)));
      filtered.sort((a,b) => state.ordersSort === 'name' ? projectNames(a).localeCompare(projectNames(b),'zh-CN') : state.ordersSort === 'due' ? dueKey(a).localeCompare(dueKey(b)) || projectNames(a).localeCompare(projectNames(b),'zh-CN') : riskRank(a) - riskRank(b) || dueKey(a).localeCompare(dueKey(b)) || projectNames(a).localeCompare(projectNames(b),'zh-CN'));
      const pageSize = window.matchMedia('(max-width: 760px)').matches ? 10 : 20;
      const pages = Math.max(1,Math.ceil(filtered.length / pageSize));
      state.ordersPage = Math.min(Math.max(1,state.ordersPage),pages);
      const start = (state.ordersPage - 1) * pageSize;
      const shown = filtered.slice(start,start + pageSize);
      $('#orders-table tbody').innerHTML = shown.map(orderTableRow).join('');
      $('#order-result-count').textContent = query ? `匹配 ${filtered.length} / 共 ${state.orders.length} 张订单` : `共 ${state.orders.length} 张订单`;
      $('#orders-table').hidden = filtered.length === 0;
      $('#orders-no-match').hidden = filtered.length !== 0;
      $('#orders-pages').innerHTML = filtered.length > pageSize ? `<span>显示 ${start + 1}–${Math.min(start + pageSize,filtered.length)} / ${filtered.length}</span><button class="button small" type="button" data-orders-page="previous" ${state.ordersPage === 1 ? 'disabled' : ''}>上一页</button><span>${state.ordersPage} / ${pages} 页</span><button class="button small" type="button" data-orders-page="next" ${state.ordersPage === pages ? 'disabled' : ''}>下一页</button>` : '';
      $$('[data-orders-page]').forEach(button => button.onclick = () => { state.ordersPage += button.dataset.ordersPage === 'next' ? 1 : -1; renderRows(); $('#orders-table').scrollIntoView({block:'start'}); });
      $$('[data-order-id]', $('#orders-table')).forEach(button => button.onclick = () => openOrder(button.dataset.orderId));
    };
    $('#order-search').oninput = event => {
      state.ordersFilter = event.target.value;
      state.ordersPage = 1;
      if (!$('#export-customer-orders').disabled) $('#export-customer-orders').textContent = customerExportLabel();
      renderRows();
    };
    $('#order-sort').onchange = event => { state.ordersSort = event.target.value; state.ordersPage = 1; renderRows(); };
    renderRows();
  }
  async function downloadExportXlsx(path, button, filename, description) {
    const originalLabel = button.textContent;
    button.disabled = true;
    button.textContent = '正在生成 Excel…';
    try {
      let response;
      try {
        response = await fetch(`/api${path}`, {headers:{Authorization:`Bearer ${state.token}`}});
      } catch (_) {
        setConnection(false);
        throw new Error('无法连接工作台服务。请检查主机和局域网连接。');
      }
      setConnection(true);
      if (!response.ok) {
        const contentType = response.headers.get('content-type') || '';
        const payload = contentType.includes('application/json') ? await response.json() : await response.text();
        if (response.status === 401 && state.user) signOut(false);
        const detail = typeof payload === 'object' ? payload.detail || payload.message : payload;
        throw new Error(typeof detail === 'string' && detail ? detail : `导出失败（${response.status}）`);
      }
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement('a');
      link.href = url;
      link.download = filename;
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 30000);
      toast(`${description}已下载。`);
    } catch (error) {
      toast(error.message, true);
    } finally {
      button.disabled = false;
      button.textContent = button.id === 'export-customer-orders' ? customerExportLabel() : originalLabel;
    }
  }
  function exportProjectSchedule(button) {
    const params = new URLSearchParams();
    if (state.selectedProject) {
      const match = /^line:(\d+)$/.exec(state.selectedProject);
      if (!match) { toast('当前项目标识无效，请刷新页面后重试。', true); return; }
      params.set('order_line_id', match[1]);
    }
    if (state.phase !== 'all') params.set('phase', state.phase);
    const query = params.toString();
    return downloadExportXlsx(`/exports/project-schedule.xlsx${query ? `?${query}` : ''}`, button, `PCBA项目排期_${new Date().toISOString().slice(0,10)}.xlsx`, '项目排期');
  }
  function exportCustomerOrders(button) {
    const query = state.ordersFilter.trim();
    const params = new URLSearchParams();
    if (query) params.set('q', query);
    return downloadExportXlsx(`/exports/customer-orders.xlsx${query ? `?${params}` : ''}`, button, `PCBA客户订单_${new Date().toISOString().slice(0,10)}.xlsx`, '客户订单');
  }
  function openCreateOrder() {
    openModal({kicker:'项目 / 客户订单',title:'新建排期项目',submitText:'创建项目',wide:true,body:`<form class="form-grid">${formSelect('项目类型','project_mode',[['customer','客户订单'],['internal','内部研发打样']],'customer',true)}${formField('项目名称','project_name','text','')}${formField('客户订单号','order_no','text','',true)}${formField('客户名称','customer','text','',true)}${formField('产品行号','line_no','text','1',true)}${formField('产品编码','product_code','text','',true)}${formField('产品名称','product_name','text','',true)}${formField('项目 / 订单数量','quantity','number','',true,'min="0.0001" step="any"')}${formField('BOM 版本（可稍后锁定）','bom_version')}<div id="customer-delivery-fields" class="wide form-grid"><div class="form-subheading wide">首批交付 <span>客户订单按交付批次跟踪</span></div>${formField('首批交付数量（留空为整单）','delivery_quantity','number','',false,'min="0.0001" step="any"')}${formSelect('交付完成口径','completion_basis',[['shipment','实际发货'],['receipt','客户签收']],'shipment',true)}${formField('实际交付日（仅整批已完成时填写）','actual_delivery_date','date')}${formField('客户要求交期','due_date','date')}${formField('业务承诺交期','promised_date','date')}${formField('人工预计交付日（临时估算）','manual_estimate_date','date')}<label class="wide">人工预计依据（填写预计日时必填）<input name="manual_estimate_reason" type="text" placeholder="例如电话确认、物流估算"></label><p class="form-note wide">实际交付日会按所选口径登记整批完成。正式预测由物料齐套、厂商承诺和物流天数计算；人工预计日仅作临时估算。分批交付时请填首批数量，后续在订单详情添加其他批次。</p></div><p id="internal-project-note" class="form-note wide" hidden>内部研发项目先记录打样目标。同一产品行可继续增加 PP 与 MP 批次，各阶段分别填写单号；客户交付批次可在取得销售单后补录。</p></form>`,onSubmit:async form => {
      const d = getForm(form), internal = d.project_mode === 'internal';
      if (!internal && !validateEstimateReason(form,d)) return;
      if (!internal && d.delivery_quantity && Number(d.delivery_quantity) > Number(d.quantity)) {
        const quantity = $('[name=delivery_quantity]',form);
        quantity.setCustomValidity('首批交付数量不能超过订单数量。'); quantity.reportValidity(); quantity.setCustomValidity('');
        return;
      }
      if (!internal && d.actual_delivery_date && d.delivery_quantity && Number(d.delivery_quantity) !== Number(d.quantity)) {
        const actual = $('[name=actual_delivery_date]',form);
        actual.setCustomValidity('历史整单交付时，首批交付数量须等于订单数量。'); actual.reportValidity(); actual.setCustomValidity('');
        return;
      }
      const today = new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0,10);
      if (!internal && d.actual_delivery_date > today) {
        const actual = $('[name=actual_delivery_date]',form);
        actual.setCustomValidity('实际交付日期不能晚于今天。'); actual.reportValidity(); actual.setCustomValidity('');
        return;
      }
      const payload = {project_mode:d.project_mode,project_name:d.project_name.trim() || d.product_name.trim(),order_no:internal ? null : d.order_no.trim(),customer:internal ? null : d.customer.trim(),lines:[{line_no:d.line_no,product_code:d.product_code.trim(),product_name:d.product_name.trim(),quantity:d.quantity,bom_version:d.bom_version || null}]};
      if (!internal) {
        const initialDelivery = {due_date:inputDate(d.due_date),promised_date:inputDate(d.promised_date),completion_basis:d.completion_basis,manual_estimate_date:inputDate(d.manual_estimate_date),manual_estimate_reason:d.manual_estimate_date ? d.manual_estimate_reason.trim() : null,actual_delivery_date:inputDate(d.actual_delivery_date)};
        if (d.delivery_quantity) initialDelivery.quantity = d.delivery_quantity;
        payload.initial_delivery = initialDelivery;
      }
      const result = await saveMutation('/orders',payload,'POST',internal ? '内部研发项目已创建' : '订单与首批交付已创建');
      if (result?.id) openOrder(result.id);
    }});
    const form = $('#modal-body form'), mode = $('[name=project_mode]',form);
    const syncMode = () => {
      const internal = mode.value === 'internal';
      for (const name of ['order_no','customer']) { const field = $(`[name=${name}]`,form); field.required = !internal; field.closest('label').hidden = internal; }
      $('#customer-delivery-fields').hidden = internal;
      $('#internal-project-note').hidden = !internal;
      $$('input,select', $('#customer-delivery-fields')).forEach(input => input.disabled = internal);
    };
    mode.addEventListener('change',syncMode); syncMode();
    bindEstimateReason();
  }

  async function renderOrderDetail() {
    const generation = renderGeneration;
    const [order,boardOverview] = await Promise.all([api(`/orders/${encodeURIComponent(state.orderId)}`),api('/board-overview').catch(() => ({order_lines:{},production_batches:{}}))]);
    if (generation !== renderGeneration) return;
    state.boardOverview = boardOverview;
    const lines = list(order.lines || order.order_lines);
    const owners = ownerDisplay(order);
    const ownerNote = legacyOwnerUnbound(order) ? '（账号待绑定）' : '';
    const isLegacy = Boolean(order.legacy_record);
    const detailTitle = isLegacy || order.project_mode === 'internal' ? projectNames(order) : order.order_no || `订单 #${order.id}`;
    const detailSubtitle = isLegacy ? `临时订单 ${order.order_no} · 客户待补录` : order.project_mode === 'internal' ? `内部研发项目 · ${order.order_no}` : order.customer || '未填写客户';
    const lineHtml = lines.length ? lines.map(line => {
      const productions = list(line.production_batches), deliveries = list(line.delivery_batches);
      const temporaryCode = String(line.product_code || '').startsWith('PCBA-TEMP-');
      const lineTitle = temporaryCode ? line.product_name : `${line.product_code || '产品编码待补录'} · ${line.product_name || ''}`;
      const boardInfo = boardLineInfo(line,boardOverview);
      const boardSection = list(boardInfo.boards).length ? projectBoardStrip(line,boardInfo,productions.map(batch => batchBoardInfo(batch,boardOverview))) : '';
      return `<div class="line-block"><div class="line-block-head"><div><h3>${escape(lineTitle)}</h3><p class="muted">${temporaryCode ? '产品编码待补录 · ' : ''}行号 ${escape(line.line_no || line.id)} · 客户产品 ${number(line.quantity)} 件</p></div><div class="table-actions"><button class="button small" data-manage-boards="${attr(line.id)}">${list(boardInfo.boards).length ? '管理板卡' : '配置多板'}</button><button class="button small" data-line-edit="${attr(line.id)}">编辑产品行</button><button class="button small" data-line-allocate="${attr(line.id)}">关联数量</button></div></div><div class="inline-edit-panel" data-line-editor="${attr(line.id)}" hidden></div>${boardSection}<div class="mini-grid"><div class="mini-card"><label>外协生产批次</label><strong>${productions.length} 批</strong><small>可与多个交付批次关联</small></div><div class="mini-card"><label>客户交付批次</label><strong>${deliveries.length} 批</strong><small>按批比较要求日与承诺日</small></div><div class="mini-card"><label>客户交付完成 / 订单数量</label><strong>${number(line.completed_quantity ?? 0)} / ${number(line.quantity)}</strong><small>板卡用量不计入交付数量</small></div></div><div class="section-title">外协生产 <span>厂商承诺与物料齐套</span></div>${productions.length ? productions.map(prod => productionCard(prod)).join('') : empty('尚无生产批次','创建批次并锁定 BOM，才能逐料跟踪。')}<div class="form-row-actions"><span></span><button class="button small" data-create-production="${attr(line.id)}">+ 建立生产批次</button></div><div class="section-title">客户交付 <span>每批交期分别预警</span></div>${deliveries.length ? deliveries.map(delivery => deliveryCard(delivery)).join('') : empty('尚无交付批次','按客户交付数量和日期建立批次。')}<div class="form-row-actions"><span></span><button class="button small" data-create-delivery="${attr(line.id)}">+ 建立交付批次</button></div></div>`;
    }).join('') : empty('订单尚无产品行','请添加产品行，再建立生产和交付批次。');
    $('#content').innerHTML = `<button class="back-link" id="back-orders">← 返回订单列表</button><div class="detail-banner"><div><p class="eyebrow">业务 / 客户订单</p><h1>${escape(detailTitle)}</h1><p>${escape(detailSubtitle)}</p></div><div class="detail-meta"><div>业务负责人<br><strong>${escape(owners + ownerNote)}</strong></div><div>产品行<br><strong>${lines.length}</strong></div><div>最后修改<br><strong>${escape(stamp(order.updated_at))}</strong></div><div>状态<br><strong>${order.status === 'complete' ? '已完成' : '跟进中'}</strong></div><div>交付完成<br><strong>${number(order.completed_quantity ?? 0)} / ${number(order.total_quantity ?? 0)}</strong></div></div></div><div id="order-inline-editor" class="inline-edit-panel" hidden></div><div class="detail-grid"><div>${panel('产品、生产与交付',lineHtml,`<div class="table-actions"><button class="button small" id="add-line">+ 产品行</button><button class="button small" id="edit-order">编辑订单信息</button></div>`,true)}</div><div>${panel('这张订单的交付口径',`<div class="notice"><p>客户要求日与业务承诺日分别保留、分别预警。生产批次与交付批次按数量关联，避免重复计算。</p></div><p class="form-note">正式预测缺少采购 ETA 或厂商确认日期时显示“无法确定”；顺延日期只作临时估算。</p>`)}${panel('订单修改记录',`<button class="button small" id="order-audit">查看这张订单的修改历史</button>`)}</div></div>`;
    $('#back-orders').onclick = () => { state.orderId = null; renderView(); };
    $('#add-line').onclick = () => openAddLine(order);
    $('#edit-order').onclick = () => toggleOrderInline(order);
    $('#order-audit').onclick = () => { state.view = 'audit'; state.orderId = null; state.auditFilter = {entity_type:'order',entity_id:order.id}; renderView(); };
    $$('[data-line-edit]').forEach(el => el.onclick = () => toggleLineInline(lines.find(x => String(x.id) === el.dataset.lineEdit)));
    $$('[data-manage-boards]').forEach(el => el.onclick = () => openProjectBoards(Number(el.dataset.manageBoards)));
    $$('[data-line-allocate]').forEach(el => el.onclick = () => openAllocation(lines.find(x => String(x.id) === el.dataset.lineAllocate)));
    $$('[data-create-production]').forEach(el => el.onclick = () => openCreateProduction(lines.find(x => String(x.id) === el.dataset.createProduction)));
    $$('[data-create-delivery]').forEach(el => el.onclick = () => openCreateDelivery(lines.find(x => String(x.id) === el.dataset.createDelivery)));
    $$('[data-production-id]').forEach(el => el.onclick = () => openBatch(el.dataset.productionId));
    $$('[data-delivery-edit]').forEach(el => el.onclick = () => openEditDelivery(deliveriesFor(lines).find(x => String(x.id) === el.dataset.deliveryEdit)));
    $$('[data-delivery-event]').forEach(el => el.onclick = () => openDeliveryEvent(deliveriesFor(lines).find(x => String(x.id) === el.dataset.deliveryEvent)));
  }
  function deliveriesFor(lines) { return lines.flatMap(line => list(line.delivery_batches)); }
  function productionCard(batch) {
    const committed = batch.commit_finish || batch.promised_finish;
    const rd = phaseOf(batch) === 'rd';
    return `<div class="entity-card"><div class="entity-head"><div><strong>${escape(productionBatchName(batch))}</strong><small>${phaseMark(phaseOf(batch))} · ${escape(batch.vendor || '外协厂待填写')} · ${number(batch.quantity)} 件 · BOM ${display(batch.bom_version)}</small>${batch.phase_order_no ? `<small>阶段单号 ${escape(batch.phase_order_no)}</small>` : ''}</div>${badge(batch.material_status || batch.status, batch.status_text || batch.material_status || batch.status || '跟进中')}</div><dl class="key-values"><div><dt>${rd ? (batch.target_kind === 'customer' ? '客户要求打样日' : '内部样机目标日') : '承诺开工'}</dt><dd>${escape(date(rd ? batch.target_date : batch.commit_start))}</dd></div><div><dt>${rd ? '预计到板' : '承诺完工'}</dt><dd>${escape(date(rd ? batch.board_eta : committed))}</dd></div></dl><div class="entity-actions"><button class="button small" data-production-id="${attr(batch.id)}">阶段、物料与工序 →</button></div></div>`;
  }
  function deliveryCard(batch) {
    const latestActual = deliveryActualDate(batch);
    const complete = batch.forecast?.status === 'complete';
    return `<div class="entity-card"><div class="entity-head"><div><strong>${escape(batch.batch_no || `交付批次 #${batch.id}`)}</strong><small>${number(batch.quantity)} 件 · ${batch.completion_basis === 'receipt' ? '以客户签收为完成' : '以发货为完成'}</small></div>${badge(complete ? 'green' : batch.risk_level || batch.status, complete ? '已完成' : batch.risk_text || batch.status || '待交付')}</div><dl class="key-values"><div><dt>客户要求日</dt><dd>${escape(date(batch.due_date))}</dd></div><div><dt>业务承诺日</dt><dd>${escape(date(batch.promised_date))}</dd></div><div><dt>正式预测</dt><dd>${formatForecast(batch)}</dd></div><div><dt>人工预计交付日（临时）</dt><dd>${escape(date(batch.manual_estimate_date))}${batch.manual_estimate_reason ? `<small class="sub">${escape(batch.manual_estimate_reason)}</small>` : ''}</dd></div><div><dt>${complete ? '实际完成日' : '最近实际交付日'}</dt><dd>${escape(date(latestActual))}</dd></div><div><dt>已完成数量</dt><dd>${number(batch.forecast?.completed_quantity ?? batch.completed_quantity ?? 0)}</dd></div><div><dt>物流天数（人工估算）</dt><dd>发货 +${number(batch.shipping_lead_days ?? 1)} / 签收 +${number(batch.receipt_lead_days ?? 2)} 工作日</dd></div></dl>${list(batch.events).length ? `<div class="form-section"><h3>交付记录</h3>${eventList(batch.events)}</div>` : ''}<div class="entity-actions"><button class="button small" data-delivery-edit="${attr(batch.id)}">修改交期</button><button class="button small" data-delivery-event="${attr(batch.id)}">记录发货/签收</button></div></div>`;
  }
  function deliveryActualDate(batch) {
    const completionKind = batch.completion_basis === 'receipt' ? 'customer_received' : 'shipped';
    return list(batch.events).filter(event => event.kind === completionKind).map(event => event.event_date).sort().at(-1) || null;
  }
  function eventLabel(kind) {
    return {material_sent:'我方发料',vendor_received:'厂商签收',finished:'厂商完工',returned:'成品回厂',direct_shipped:'成品直发',shipped:'发货',customer_received:'客户签收'}[kind] || kind || '节点';
  }
  function eventList(events) {
    const items = list(events);
    return items.length ? `<ul class="stage-list">${items.map(item => `<li class="done"><div><strong>${escape(eventLabel(item.kind))}</strong><small>${item.material_code ? `BOM ${escape(item.for_material_code || item.material_code)} → ${escape(item.material_code)} · ` : ''}${item.quantity ? `${number(item.quantity)} 件 · ` : ''}${escape(item.note || '')}</small></div><time class="mono">${escape(date(item.event_date))}</time></li>`).join('')}</ul>` : empty('尚无物流节点记录','发料、厂商签收、成品回厂或直发后在批次上登记。');
  }
  async function saveInlineRecord(path,entity,fields,form,successText) {
    const data = getForm(form), changes = changedFields(form,entity,fields);
    if (!Object.keys(changes).length) { toast('没有需要保存的变化。'); return; }
    const reason = data.reason.trim();
    if (!reason) { toast('请填写修改原因。',true); $('[name=reason]',form).focus(); return; }
    const button = $('[type=submit]',form); button.disabled = true;
    try {
      await api(path,{method:'PATCH',body:withVersion(entity,changes,reason)});
      await loadProjects().catch(() => {});
      toast(successText); renderView();
    } catch (error) {
      if (error.status === 409) showVersionConflict(form,() => renderView());
      else toast(error.message,true);
      button.disabled = false;
    }
  }
  async function toggleOrderInline(order) {
    const editor = $('#order-inline-editor');
    if (!editor.hidden) { editor.hidden = true; editor.innerHTML = ''; return; }
    editor.hidden = false; editor.innerHTML = '<p class="form-note">正在读取可选负责人…</p>';
    let users = [];
    try { const response = await api('/users'); users = list(response.users || response.items || response); }
    catch (error) { editor.innerHTML = `<p class="inline-error">${escape(error.message)}</p>`; return; }
    const ownerOptions = [['',order.legacy_owner_name ? `原负责人 ${order.legacy_owner_name}（账号待绑定）` : '未分配'],...users.filter(user => user.active && user.status !== 'pending').map(user => [user.id,`${user.display_name || user.username} · ${roleNames[user.role] || user.role}`])];
    editor.innerHTML = `<div class="inline-edit-heading"><strong>项目与订单信息</strong><span>内部研发可在取得正式销售单后补齐客户信息</span></div><form class="form-grid">${formSelect('项目类型','project_mode',[['internal','内部研发'],['customer','客户订单']],order.project_mode || 'customer',true)}${formField('项目名称','project_name','text',order.project_name || projectNames(order),true)}${formField('正式客户订单号','order_no','text',order.project_mode === 'internal' && String(order.order_no).startsWith('PCBA-PROJ-') ? '' : order.order_no || '')}${formField('客户名称','customer','text',order.customer === '内部研发' ? '' : order.customer || '')}${formSelect('业务负责人','owner_id',ownerOptions,order.owner_id ?? '')}<label class="wide">修改原因<input name="reason" placeholder="例如取得客户正式订单、项目类型变更或负责人移交" required></label><div class="wide form-row-actions"><span></span><button class="button small" type="button" data-cancel-inline>取消</button><button class="button small primary" type="submit">保存项目与订单信息</button></div></form>`;
    const form = $('form',editor), mode = $('[name=project_mode]',form);
    const syncMode = () => {
      const customer = mode.value === 'customer';
      for (const name of ['order_no','customer']) { const field = $(`[name=${name}]`,form); field.required = customer; field.closest('label').hidden = !customer; }
    };
    mode.addEventListener('change',syncMode); syncMode();
    $('[data-cancel-inline]',editor).onclick = () => { editor.hidden = true; editor.innerHTML = ''; };
    form.onsubmit = event => { event.preventDefault(); saveInlineRecord(`/orders/${order.id}`,order,mode.value === 'customer' ? ['project_mode','project_name','order_no','customer','owner_id'] : ['project_mode','project_name','owner_id'],event.currentTarget,'项目与订单信息已更新。'); };
    $('[name=project_name]',editor).focus();
  }
  function toggleLineInline(line) {
    if (!line) return;
    const editor = $(`[data-line-editor="${CSS.escape(String(line.id))}"]`);
    if (!editor.hidden) { editor.hidden = true; editor.innerHTML = ''; return; }
    editor.hidden = false;
    editor.innerHTML = `<div class="inline-edit-heading"><strong>产品行信息</strong><span>数量及 BOM 变更会重新核对相关批次</span></div><form class="form-grid">${formField('产品编码','product_code','text',line.product_code || '',true)}${formField('产品名称','product_name','text',line.product_name || '',true)}${formField('订单数量','quantity','number',line.quantity,true,'min="0.0001" step="any"')}${formField('BOM 版本','bom_version','text',line.bom_version || '')}<label class="wide">修改原因<input name="reason" placeholder="例如客户更改数量或工程确认改版" required></label><div class="wide form-row-actions"><span></span><button class="button small" type="button" data-cancel-inline>取消</button><button class="button small primary" type="submit">保存产品行</button></div></form>`;
    $('[data-cancel-inline]',editor).onclick = () => { editor.hidden = true; editor.innerHTML = ''; };
    $('form',editor).onsubmit = event => { event.preventDefault(); saveInlineRecord(`/order-lines/${line.id}`,line,['product_code','product_name','quantity','bom_version'],event.currentTarget,'产品行已更新。'); };
    $('[name=product_code]',editor).focus();
  }
  async function openEditOrder(order) {
    const users = await api('/users').catch(() => []);
    const unboundLabel = order.legacy_owner_name ? `原负责人 ${order.legacy_owner_name}（账号待绑定）` : '未分配';
    const choices = [['',unboundLabel],...list(users.users || users.items || users).filter(user => Number(user.active) === 1).map(user => [user.id,`${user.display_name || user.username}（${roleNames[user.role] || user.role}）`])];
    openModal({kicker:'订单信息',title:`修改 ${order.order_no}`,body:`<form class="form-grid">${formField('客户名称','customer','text',order.customer,true)}${formSelect('业务负责人','owner_id',choices,order.owner_id ?? '')}<label class="wide">修改原因（建议填写）<textarea name="reason" placeholder="说明本次修改依据"></textarea></label></form>`,onSubmit:async form => {
      const data = getForm(form), changes = changedFields(form,order,['customer','owner_id']);
      if (!Object.keys(changes).length) { closeModal(); return; }
      await saveMutation(`/orders/${order.id}`,withVersion(order,changes,data.reason),'PATCH','订单已更新');
    }});
  }
  function openAddLine(order) {
    openModal({kicker:'订单产品',title:'添加产品行',body:`<form class="form-grid">${formField('产品行号','line_no','text',String(list(order.lines).length + 1),true)}${formField('产品编码','product_code','text','',true)}${formField('产品名称','product_name','text','',true)}${formField('数量','quantity','number','',true,'min="0.0001" step="any"')}${formField('BOM 版本','bom_version')}</form>`,onSubmit:async form => {
      const d = getForm(form);
      await saveMutation(`/orders/${order.id}/lines`,{line_no:d.line_no,product_code:d.product_code.trim(),product_name:d.product_name.trim(),quantity:d.quantity,bom_version:d.bom_version || null},'POST','产品行已添加');
    }});
  }
  function openEditLine(line) {
    if (!line) return;
    openModal({kicker:'订单产品',title:`修改 ${line.product_code}`,body:`<form class="form-grid">${formField('产品名称','product_name','text',line.product_name,true)}${formField('订单数量','quantity','number',line.quantity,true,'min="0.0001" step="any"')}${formField('BOM 版本','bom_version','text',line.bom_version || '')}<label class="wide">修改原因（数量或 BOM 版本变更必填）<textarea name="reason" placeholder="例如客户变更订单数量"></textarea></label></form>`,onSubmit:async form => {
      const d = getForm(form), changes = changedFields(form,line,['product_name','quantity','bom_version']);
      if (!Object.keys(changes).length) { closeModal(); return; }
      if (('quantity' in changes || 'bom_version' in changes) && !d.reason.trim()) { $('#modal-error').textContent = '修改数量或 BOM 版本必须填写原因。'; $('#modal-error').hidden = false; return; }
      await saveMutation(`/order-lines/${line.id}`,withVersion(line,changes,d.reason),'PATCH','产品行已更新');
    }});
  }
  function bomRowsHtml(item = {}) { return `<div class="form-array-row bom-entry-row"><label>物料编码<input name="material_code" value="${attr(item.material_code || '')}" placeholder="编码"></label><label>物料名称<input name="material_name" value="${attr(item.material_name || '')}" placeholder="名称"></label><label>单位用量<input name="quantity_per" value="${attr(item.quantity_per || '')}" type="number" min="0" step="any" placeholder="每件用量"></label><label>损耗率<input name="loss_rate" value="${attr(item.loss_rate ?? '0')}" type="number" min="0" max="1" step="any" placeholder="如 0.02"></label><label>单价 CNY<input name="unit_price" value="${attr(item.unit_price ?? '')}" type="number" min="0" step="any" inputmode="decimal" placeholder="待报价"></label><input name="source_line_no" type="hidden" value="${attr(item.source_line_no || '')}"><button type="button" class="icon-button remove-row" aria-label="删除物料行">×</button></div>`; }
  function readBomItems(root) {
    return $$('.form-array-row',root).map(row => ({material_code:$('[name=material_code]',row)?.value.trim(),material_name:$('[name=material_name]',row)?.value.trim(),quantity_per:$('[name=quantity_per]',row)?.value,loss_rate:$('[name=loss_rate]',row)?.value || '0',unit_price:$('[name=unit_price]',row)?.value || null,source_line_no:$('[name=source_line_no]',row)?.value || null})).filter(x => x.material_code);
  }
  function decimalFraction(raw) {
    const value = String(raw ?? '').trim();
    const match = value.match(/^\+?(?:(\d+)(?:\.(\d*))?|\.(\d+))(?:e([+-]?\d+))?$/i);
    if (!match) return null;
    const fraction = match[2] ?? match[3] ?? '';
    const exponent = Number(match[4] || 0);
    if (value.length > 80 || !Number.isSafeInteger(exponent) || Math.abs(exponent) > 36) return null;
    let numerator = BigInt((match[1] || '0') + fraction), denominator = 10n ** BigInt(fraction.length);
    if (exponent > 0) numerator *= 10n ** BigInt(exponent);
    if (exponent < 0) denominator *= 10n ** BigInt(-exponent);
    return {numerator,denominator};
  }
  function priceFraction(raw) {
    const value = decimalFraction(raw);
    if (!value || value.numerator >= 1000000000000n * value.denominator) return null;
    let numerator = value.numerator, denominator = value.denominator;
    while (denominator > 1n && numerator % 10n === 0n) { numerator /= 10n; denominator /= 10n; }
    return denominator <= 100000000n ? value : null;
  }
  function samePrice(before,after) {
    const oldValue = before === '' || before == null ? null : priceFraction(before);
    const newValue = after === '' || after == null ? null : priceFraction(after);
    if (!oldValue || !newValue) return oldValue === newValue;
    return oldValue.numerator * newValue.denominator === newValue.numerator * oldValue.denominator;
  }
  function moneyText(raw) {
    const value = decimalFraction(raw);
    return value ? centsText((value.numerator * 200n + value.denominator) / (value.denominator * 2n)) : '—';
  }
  function centsText(cents) {
    const whole = String(cents / 100n).replace(/\B(?=(\d{3})+(?!\d))/g,',');
    return `¥${whole}.${String(cents % 100n).padStart(2,'0')}`;
  }
  function bomPricingRows(items,pricing,editable = true,boardNames = null,fixedRequirements = false) {
    const priced = new Map(list(pricing?.lines).map(line => [Number(line.bom_item_id),line]));
    return `<div class="table-wrap"><table class="data-table bom-price-table"><thead><tr><th>物料</th><th>单位用量 / 损耗</th><th>本批需求</th><th>单价 CNY</th><th>金额</th></tr></thead><tbody>${items.map(item => {
      const line = priced.get(Number(item.id));
      const boardName = boardNames?.get(Number(item.batch_board_id));
      return `<tr data-bom-price-row data-item-id="${attr(item.id)}" data-version="${attr(item.version)}" data-per="${attr(item.quantity_per)}" data-loss="${attr(item.loss_rate || '0')}" ${fixedRequirements ? `data-fixed-required="${attr(line?.required_quantity ?? '')}"` : ''}><td><strong>${escape(item.material_code)}</strong><span class="sub">${boardName ? `${escape(boardName)} · ` : ''}${escape(item.material_name || '')}</span></td><td>${escape(item.quantity_per)} / ${escape(item.loss_rate || '0')}</td><td class="mono" data-cost-required>${escape(number(line?.required_quantity))}</td><td><input class="input bom-unit-price" type="number" min="0" step="any" inputmode="decimal" value="${attr(item.unit_price ?? '')}" data-original-price="${attr(item.unit_price ?? '')}" aria-label="${attr(boardName ? `${boardName} ${item.material_code}` : item.material_code)} 单价 人民币" ${editable ? '' : 'disabled'}></td><td class="mono" data-cost-line>${line?.line_total == null ? '待填单价' : moneyText(line.line_total)}</td></tr>`;
    }).join('')}</tbody></table></div>`;
  }
  function updateBomPricingPreview(root,quantity) {
    const qty = decimalFraction(quantity), summary = $('[data-bom-summary]',root);
    if (!summary) return false;
    if (!qty || qty.numerator <= 0n) { summary.textContent = '请填写大于 0 的生产数量。'; return false; }
    let cents = 0n, missing = 0, invalid = false;
    $$('[data-bom-price-row]',root).forEach(row => {
      const per = decimalFraction(row.dataset.per), loss = decimalFraction(row.dataset.loss), priceInput = $('.bom-unit-price',row);
      const rawPrice = priceInput.value.trim(), price = rawPrice ? priceFraction(rawPrice) : null;
      if (!per || per.numerator <= 0n || !loss || loss.numerator > loss.denominator ||
          (rawPrice && (!price || !priceInput.checkValidity()))) { invalid = true; return; }
      const numerator = qty.numerator * per.numerator * (loss.denominator + loss.numerator);
      const denominator = qty.denominator * per.denominator * loss.denominator;
      const fixed = row.hasAttribute('data-fixed-required') ? decimalFraction(row.dataset.fixedRequired) : null;
      if (row.hasAttribute('data-fixed-required') && (!fixed || fixed.numerator % fixed.denominator !== 0n)) { invalid = true; return; }
      const required = fixed ? fixed.numerator / fixed.denominator : (numerator + denominator - 1n) / denominator;
      $('[data-cost-required]',row).textContent = required.toLocaleString('zh-CN');
      if (!price) { missing++; $('[data-cost-line]',row).textContent = '待填单价'; return; }
      const lineCents = (required * price.numerator * 200n + price.denominator) / (price.denominator * 2n);
      cents += lineCents;
      $('[data-cost-line]',row).textContent = centsText(lineCents);
    });
    if (invalid) { summary.textContent = '请核对单位用量、损耗率和单价。'; return false; }
    summary.innerHTML = `<span>${missing ? '已报价小计' : '预计物料总价'}</span><strong>${centsText(cents)}</strong><small>${missing ? `${missing} 项缺单价，总价待补齐` : '所有物料已报价 · 保存后以系统精确计算为准'}</small>`;
    return true;
  }
  function updateBomEntryPreview(container,quantity) {
    const root = $(container), summary = $('[data-bom-entry-summary]',root.parentElement);
    if (!summary) return;
    const qty = decimalFraction(quantity), items = readBomItems(root);
    if (!items.length) { summary.textContent = '填写 BOM 行后自动计算物料总价。'; return; }
    if (!qty || qty.numerator <= 0n) { summary.textContent = '请先填写生产数量。'; return; }
    let cents = 0n, missing = 0;
    for (const item of items) {
      const per = decimalFraction(item.quantity_per), loss = decimalFraction(item.loss_rate);
      const price = item.unit_price == null ? null : priceFraction(item.unit_price);
      if (!per || per.numerator <= 0n || !loss || loss.numerator > loss.denominator || (item.unit_price != null && !price)) { summary.textContent = '请核对单位用量、损耗率和单价。'; return; }
      const numerator = qty.numerator * per.numerator * (loss.denominator + loss.numerator);
      const denominator = qty.denominator * per.denominator * loss.denominator;
      const required = (numerator + denominator - 1n) / denominator;
      if (!price) { missing++; continue; }
      cents += (required * price.numerator * 200n + price.denominator) / (price.denominator * 2n);
    }
    summary.textContent = `${missing ? '已报价小计' : '预计物料总价'} ${centsText(cents)}${missing ? ` · ${missing} 项缺单价，总价待补齐` : ''}`;
  }
  function bomEntryError(container) {
    for (const [index,row] of $$('.bom-entry-row',$(container)).entries()) {
      const value = name => $(`[name=${name}]`,row).value.trim();
      const code = value('material_code'), name = value('material_name'), per = value('quantity_per');
      const loss = value('loss_rate') || '0', price = value('unit_price');
      if (!code && !name && !per && !price && (!loss || loss === '0')) continue;
      if (!code) return `第 ${index + 1} 行缺少物料编码。`;
      const perValue = decimalFraction(per), lossValue = decimalFraction(loss);
      if (!perValue || perValue.numerator <= 0n) return `第 ${index + 1} 行单位用量必须大于 0。`;
      if (!lossValue || lossValue.numerator > lossValue.denominator) return `第 ${index + 1} 行损耗率应为 0 至 1。`;
      if (price && !priceFraction(price)) return `第 ${index + 1} 行单价无效；最多 8 位小数。`;
      if (['quantity_per','loss_rate','unit_price'].some(field => !$(`[name=${field}]`,row).checkValidity()))
        return `第 ${index + 1} 行数字无效，请核对后再保存。`;
    }
    return null;
  }
  function bindBomEntryPreview(container,quantityGetter) {
    const root = $(container);
    root.addEventListener('input',() => updateBomEntryPreview(container,quantityGetter()));
    updateBomEntryPreview(container,quantityGetter());
  }
  async function loadCatalogBom(productCode, version, target) {
    if (!version) { toast('请先填写 BOM 版本。',true); return; }
    try {
      const result = await api(`/bom-catalog?product_code=${encodeURIComponent(productCode)}&bom_version=${encodeURIComponent(version)}`);
      const items = list(result.items || result);
      if (!items.length) { toast('KIS BOM 目录中找不到该产品与版本，请先导入 BOM 或逐行填写。',true); return; }
      $(target).innerHTML = items.map(item => bomRowsHtml(item)).join('');
      bindRemoveRows();
      $(target).dispatchEvent(new Event('input',{bubbles:true}));
      toast(`已加载 ${items.length} 条 BOM 物料，请核对后锁定。`);
    } catch (error) { toast(error.message,true); }
  }
  function installArrayRows(container, button) {
    $(button).onclick = () => { $(container).insertAdjacentHTML('beforeend',bomRowsHtml()); bindRemoveRows(); $(container).dispatchEvent(new Event('input',{bubbles:true})); };
    bindRemoveRows();
  }
  function suggestedBatchQuantity(line, field) {
    const used = list(line[field]).reduce((sum,item) => sum + Number(item.quantity || 0), 0);
    return String(Math.max(0, Number(line.quantity || 0) - used));
  }
  function suggestedPhaseQuantity(line,phase) {
    const used = list(line.production_batches).filter(batch => phaseOf(batch) === phase).reduce((sum,batch) => sum + Number(batch.quantity || 0),0);
    return String(Math.max(0,Number(line.quantity || 0) - used));
  }
  function bindRemoveRows() { $$('.remove-row').forEach(el => el.onclick = () => { const row = el.closest('.form-array-row'), container = row.parentElement; row.remove(); container.dispatchEvent(new Event('input',{bubbles:true})); }); }
  function openCreateProduction(line) {
    if (!line) return;
    const multiBoard = list(boardLineInfo(line).boards).length > 0;
    const initialPhase = suggestedPhase(line.quantity);
    let phaseEdited = false;
    openModal({kicker:'阶段 / 外协生产',title:`建立 ${line.product_code} 的生产批次`,wide:true,body:`<div class="notice"><p>研发打样、PP 与 MP 可以各有自己的生产批次和阶段单号。初始阶段按数量建议，保存前可调整。</p></div><form class="form-grid">${formField('批次号','batch_no','text','',true)}${formField('生产数量','quantity','number',suggestedPhaseQuantity(line,initialPhase),true,'min="0.0001" step="any"')}${formSelect('排期阶段','phase',Object.entries(phaseNames).filter(([key]) => ['rd','pp','mp'].includes(key)).map(([key,label]) => [key,label]),initialPhase,true)}${formField('本阶段单号','phase_order_no','text','')}${formField('排期起点','schedule_start','date')}${formSelect('日历规则','schedule_mode',[['workday','工作日'],['natural','自然日']],'workday')}${formField('外协厂','vendor')}${formField('BOM 版本','bom_version','text',line.bom_version || '')}${formField('厂商承诺开工','commit_start','date')}${formField('厂商承诺完工','commit_finish','date')}<div class="wide rd-create-fields" id="rd-create-fields">${formSelect('打样目标类型','target_kind',[['','待选择'],['internal','内部样机目标'],['customer','客户要求交付']],'')}${formField('打样目标日','target_date','date')}${formField('嘉立创下单日','sampling_order_date','date')}${formField('预计到板日','board_eta','date')}${formField('实际到板日','board_received_at','date')}${formField('调试验收日','debug_accepted_at','date')}</div></form><div class="form-section"><h3>BOM 物料快照（可先建批次，再逐料锁定）</h3><p class="form-note">单位用量按每件产品；损耗率如 2% 填 0.02。已锁定 BOM 不会被后来导入改写。</p><div id="create-bom-rows">${bomRowsHtml()}</div><button class="button small" id="add-bom-row">+ 物料行</button> <button class="button small" id="create-load-bom">从 KIS BOM 加载</button><div class="bom-entry-summary" data-bom-entry-summary role="status" aria-live="polite"></div></div>`,onSubmit:async form => {
      const d = getForm(form), error = multiBoard ? null : bomEntryError('#create-bom-rows');
      if (error) { modalMessage(error); return; }
      const items = multiBoard ? [] : readBomItems($('#create-bom-rows'));
      if (d.phase === 'rd' && d.target_date && !d.target_kind) { modalMessage('填写打样目标日时，请选择内部样机目标或客户要求交付。'); return; }
      const rd = d.phase === 'rd' ? {target_kind:d.target_kind || null,target_date:inputDate(d.target_date),sampling_order_date:inputDate(d.sampling_order_date),board_eta:inputDate(d.board_eta),board_received_at:inputDate(d.board_received_at),debug_accepted_at:inputDate(d.debug_accepted_at)} : {};
      const result = await saveMutation('/production-batches',{order_line_id:line.id,batch_no:d.batch_no.trim(),quantity:d.quantity,phase:d.phase,phase_source:phaseEdited ? 'manual' : 'quantity_rule',phase_order_no:d.phase_order_no.trim() || null,schedule_start:inputDate(d.schedule_start),schedule_mode:d.schedule_mode,vendor:d.vendor || null,commit_start:inputDate(d.commit_start),commit_finish:inputDate(d.commit_finish),bom_version:d.bom_version || null,...rd,...(items.length ? {bom_items:items} : {})},'POST','生产批次已建立');
      if (result?.conflicts?.length) toast(`已建立批次；发现 ${result.conflicts.length} 项同厂档期重叠，请核对。`,true);
    }});
    const form = $('#modal-body form'), phaseInput = $('[name=phase]',form), quantityInput = $('[name=quantity]',form), rdFields = $('#rd-create-fields');
    if (multiBoard) {
      quantityInput.min = '1'; quantityInput.step = '1';
      const versionInput = $('[name=bom_version]',form);
      versionInput.closest('label').hidden = true; versionInput.disabled = true;
      $('#create-bom-rows').closest('.form-section').innerHTML = '<h3>本批逐板 BOM</h3><p class="form-note">生产数量须为整数件。保存批次后，为每块板分别选择并锁定 BOM；客户交付数量仍按产品件数登记。</p>';
    }
    const syncPhase = () => { rdFields.hidden = phaseInput.value !== 'rd'; };
    phaseInput.addEventListener('change',() => { phaseEdited = true; syncPhase(); });
    quantityInput.addEventListener('input',() => { if (!phaseEdited) { phaseInput.value = suggestedPhase(quantityInput.value); syncPhase(); } if (!multiBoard) updateBomEntryPreview('#create-bom-rows',quantityInput.value); });
    syncPhase();
    bindSchedulePreview(form,{productCode:line.product_code});
    if (!multiBoard) {
      installArrayRows('#create-bom-rows','#add-bom-row');
      bindBomEntryPreview('#create-bom-rows',() => quantityInput.value);
      $('#create-load-bom').onclick = () => loadCatalogBom(line.product_code, $('[name=bom_version]',$('#modal-body')).value, '#create-bom-rows');
    }
  }
  function openCreateDelivery(line) {
    if (!line) return;
    openModal({kicker:'客户交付',title:`建立 ${line.product_code} 的交付批次`,body:`<form class="form-grid">${formField('交付批次号','batch_no','text','',true)}${formField('交付数量','quantity','number',suggestedBatchQuantity(line,'delivery_batches'),true,'min="0.0001" step="any"')}${formField('客户要求日','due_date','date')}${formField('业务正式承诺日','promised_date','date')}${formField('人工预计交付日（临时）','manual_estimate_date','date')}<label>人工预计依据（填写预计日时必填）<input name="manual_estimate_reason" type="text" placeholder="例如客户电话、物流估算"></label>${formSelect('完成口径','completion_basis',[['shipment','发货'],['receipt','客户签收']],'shipment',true)}${formField('完工至发货（工作日）','shipping_lead_days','number','1',true,'min="0" max="30" step="1"')}${formField('完工至签收（工作日）','receipt_lead_days','number','2',true,'min="0" max="30" step="1"')}</form><p class="form-note">人工预计日仅作临时估算；正式预测根据物料、厂商承诺和物流工作日计算。</p>`,onSubmit:async form => {
      const d = getForm(form);
      if (!validateEstimateReason(form,d)) return;
      await saveMutation('/delivery-batches',{order_line_id:line.id,batch_no:d.batch_no.trim(),quantity:d.quantity,due_date:inputDate(d.due_date),promised_date:inputDate(d.promised_date),manual_estimate_date:inputDate(d.manual_estimate_date),manual_estimate_reason:d.manual_estimate_date ? d.manual_estimate_reason.trim() : null,completion_basis:d.completion_basis,shipping_lead_days:Number(d.shipping_lead_days),receipt_lead_days:Number(d.receipt_lead_days)},'POST','交付批次已建立');
    }});
    bindEstimateReason();
  }
  function openEditDelivery(delivery) {
    if (!delivery) return;
    openModal({kicker:'客户交期',title:`修改 ${delivery.batch_no}`,body:`<form class="form-grid">${formField('交付数量','quantity','number',delivery.quantity,true,'min="0.0001" step="any"')}${formField('客户要求日','due_date','date',delivery.due_date || '')}${formField('业务正式承诺日','promised_date','date',delivery.promised_date || '')}${formField('人工预计交付日（临时）','manual_estimate_date','date',delivery.manual_estimate_date || '')}<label>人工预计依据（填写预计日时必填）<input name="manual_estimate_reason" type="text" value="${attr(delivery.manual_estimate_reason || '')}" placeholder="例如客户电话、物流估算"></label>${formSelect('完成口径','completion_basis',[['shipment','发货'],['receipt','客户签收']],delivery.completion_basis || 'shipment')}${formField('完工至发货（工作日）','shipping_lead_days','number',delivery.shipping_lead_days ?? 1,true,'min="0" max="30" step="1"')}${formField('完工至签收（工作日）','receipt_lead_days','number',delivery.receipt_lead_days ?? 2,true,'min="0" max="30" step="1"')}<label class="wide">变更原因（必填）<textarea name="reason" required placeholder="记录客户、厂商或物流确认依据"></textarea></label></form><p class="form-note">人工预计日只作临时估算，正式预测按当前物料、厂商及物流记录计算。</p>`,onSubmit:async form => {
      const d = getForm(form);
      if (!validateEstimateReason(form,d)) return;
      const changes = changedFields(form,delivery,['quantity','due_date','promised_date','manual_estimate_date','manual_estimate_reason','completion_basis','shipping_lead_days','receipt_lead_days']);
      if (d.manual_estimate_date && 'manual_estimate_date' in changes) changes.manual_estimate_reason = d.manual_estimate_reason.trim();
      if (!d.manual_estimate_date) {
        delete changes.manual_estimate_reason;
        if ('manual_estimate_date' in changes) changes.manual_estimate_reason = null;
      }
      if (!Object.keys(changes).length) { closeModal(); return; }
      await saveMutation(`/delivery-batches/${delivery.id}`,withVersion(delivery,changes,d.reason),'PATCH','交付批次已更新');
    }});
    bindEstimateReason();
  }
  function openAllocation(line) {
    if (!line) return;
    const productions = list(line.production_batches), deliveries = list(line.delivery_batches);
    if (!productions.length || !deliveries.length) { toast('请先建立生产批次和交付批次。',true); return; }
    openModal({kicker:'数量分配',title:`关联 ${line.product_code} 生产与交付`,body:`<div class="notice"><p>一个生产批次可供应多个交付批次；系统会检查生产侧和交付侧的可分配数量。</p></div><form class="form-grid">${formSelect('生产批次','production_batch_id',productions.map(x => [x.id,`${productionBatchName(x)} · ${number(x.quantity)} 件`]),'',true)}${formSelect('交付批次','delivery_batch_id',deliveries.map(x => [x.id,`${x.batch_no} · ${number(x.quantity)} 件`]),'',true)}${formField('关联数量','quantity','number','',true,'min="0.0001" step="any"')}</form>`,onSubmit:async form => { await saveMutation('/allocations',getForm(form),'POST','批次数量已关联'); }});
  }
  function openDeliveryEvent(batch) {
    if (!batch) return;
    openModal({kicker:'交付记录',title:`记录 ${batch.batch_no} 的发货或签收`,body:`<form class="form-grid">${formSelect('事件','kind',[['shipped','发货'],['customer_received','客户签收']],'shipped',true)}${formField('日期','event_date','date',new Date().toISOString().slice(0,10),true)}${formField('数量','quantity','number','',true,'min="0.0001" step="any"')}<label class="wide">备注<textarea name="note" placeholder="物流单号或签收依据"></textarea></label></form>`,onSubmit:async form => { const d = getForm(form); await saveMutation(`/delivery-batches/${batch.id}/events`,d,'POST','交付记录已添加'); }});
  }

  async function renderBatch() {
    const generation = renderGeneration;
    const id = encodeURIComponent(state.batchId);
    const results = await Promise.allSettled([api(`/production-batches/${id}`),api(`/production-batches/${id}/materials`),api(`/production-batches/${id}/stages`),api('/vendors/conflicts'),api('/users'),api('/board-overview')]);
    if (generation !== renderGeneration) return;
    if (results[0].status === 'rejected') throw results[0].reason;
    const batch = results[0].value.batch || results[0].value;
    if (!state.orderId && batch.order_line?.order_id) state.orderId = batch.order_line.order_id;
    const materialsResult = results[1].status === 'fulfilled' ? results[1].value : null;
    const boardOverview = results[5].status === 'fulfilled' ? results[5].value : {order_lines:{},production_batches:{}};
    state.boardOverview = boardOverview;
    const boardSummary = batchBoardInfo(batch,boardOverview);
    if (!list(batch.boards).length) batch.boards = list(boardSummary.boards);
    batch.bom_scope = batch.bom_scope || boardSummary.bom_scope;
    batch.board_schedule_mode = batch.board_schedule_mode || boardSummary.board_schedule_mode || 'shared';
    batch.board_set_revision = batch.board_set_revision || boardSummary.board_set_revision;
    const independent = batch.board_schedule_mode === 'independent' && batch.bom_scope === 'multi';
    const boardDetails = await loadBoardSchedules(batch);
    const stagesResult = results[2].status === 'fulfilled' ? results[2].value : null;
    const conflictResult = results[3].status === 'fulfilled' ? results[3].value : null;
    const usersResult = results[4].status === 'fulfilled' ? results[4].value : null;
    const materials = list(materialsResult?.items || materialsResult?.materials || materialsResult);
    const stages = list(stagesResult?.stages || stagesResult?.items || stagesResult);
    const users = list(usersResult?.users || usersResult?.items || usersResult);
    const conflictGroup = list(conflictResult?.conflicts || conflictResult).find(x => String(x.batch?.id) === String(batch.id));
    const conflicts = list(conflictGroup?.conflicts || batch.conflicts);
    const missing = results.slice(1,5).map((r,i) => r.status === 'rejected' ? ['物料','工序','档期冲突','账号'][i] : null).filter(Boolean);
    const materialRows = materials.length ? `<div class="table-wrap"><table class="data-table"><thead><tr><th>物料编码 / 名称</th><th class="num">需求</th><th class="num">已预留/已发</th><th class="num">缺口</th><th>预计齐套</th><th>操作</th></tr></thead><tbody>${materials.map(item => `<tr><td><strong>${escape(item.material_code)}</strong><span class="sub">${escape(item.material_name || '')}</span></td><td class="num">${number(item.required)}</td><td class="num">${number(item.reserved)}</td><td class="num">${Number(item.shortage || 0) > 0 ? `<span class="badge red">${number(item.shortage)}</span>` : number(item.shortage)}</td><td class="mono">${materialReadyCell(item)}</td><td><div class="table-actions"><button class="button small" data-reserve="${attr(item.material_code)}">预留</button><button class="button small" data-purchase="${attr(item.material_code)}">采购</button><button class="button small" data-substitute="${attr(item.material_code)}">替代</button><button class="button small" data-reserve-substitute="${attr(item.material_code)}">替代预留</button><button class="button small" data-loss="${attr(item.material_code)}">损耗</button></div></td></tr>`).join('')}</tbody></table></div>` : empty('BOM 尚未锁定或没有物料行','锁定本批使用的 BOM 版本及全部物料后，可计算逐料缺口。',`<button class="button small" id="lock-bom-empty">锁定 BOM</button>`);
    state.activeStages = stages;
    const stageEditable = state.user.role === 'admin' || state.user.permissions?.production_edit !== false;
    const stageRows = independent ? `<div class="notice"><p>本批按板卡独立排期。下方是原有共用节点，仅供查阅，不计入任何板卡的工序完成和正式预测。</p></div>${stages.length ? `<details class="node-list-disclosure"><summary>查看批次共用节点记录（${stages.length} 项）</summary><div class="node-list">${stages.map((stage,index) => nodeCard(stage,index,false,users)).join('')}</div></details>` : ''}` : stages.length ? `${stageTimeline(stages)}<details class="node-list-disclosure"><summary>展开节点记录列表（${stages.length} 项）</summary><div class="node-list">${stages.map((stage,index) => nodeCard(stage,index,stageEditable,users)).join('')}</div></details>` : empty('尚无工序计划','请核对产品工序模板，按阶段排期起点生成计划初稿。');
    const conflictHtml = conflicts.length ? `<div class="notice warning"><p>同一外协厂在本工作台有 ${conflicts.length} 个重叠档期。只能说明需核对，不能据此判断厂商真实产能。</p></div>${conflicts.map(item => `<div class="entity-card"><strong>${escape(item.batch_no ? productionBatchName(item) : '档期重叠')}</strong><p class="small-copy">${escape(date(item.commit_start))} — ${escape(date(item.commit_finish))}</p>${!conflictGroup?.acknowledged && !batch.conflict_ack_at ? `<button class="button small" data-conflict-ack="${attr(batch.id)}">确认可并行</button>` : `<small>已核对：${escape(batch.conflict_ack_reason || '厂商确认可并行')}</small>`}</div>`).join('')}` : `<div class="notice"><p>本工作台暂无未确认的同厂档期重叠；厂商其他客户排产仍需人工确认。</p></div>`;
    const forecast = batch.forecast || materialsResult?.forecast || {};
    const forecastStatus = batch.forecast_status || forecast.status || materialsResult?.forecast_status;
    const formal = batch.forecast_date || forecast.formal_date || forecast.date || materialsResult?.forecast_date;
    const provisional = batch.provisional_date || forecast.provisional_date || materialsResult?.provisional_date;
    const ready = materialsResult?.ready_date || batch.material_ready_date;
    const allocations = list(batch.allocations);
    const allocationHtml = allocations.length ? allocations.map(item => `<div class="entity-card"><strong>交付批次 #${escape(item.delivery_batch_id)}</strong><small>已关联 ${number(item.quantity)} 件</small></div>`).join('') : empty('尚未关联交付批次','返回订单，为生产批次和交付批次分配数量。');
    const eventHtml = eventList(batch.events);
    const bomItems = list(batch.bom_items);
    const canPrice = state.user.role === 'admin' || state.user.permissions?.materials_edit !== false;
    const boardNames = new Map(list(batch.boards).map(board => [Number(board.id),`${board.board_ref} ${board.board_name}`]));
    const bomCostPanel = batch.bom_locked && bomItems.length ? `<div class="batch-cost-panel">${panel('BOM 物料成本',`<div id="batch-bom-pricing"><p class="form-note">${independent ? '按每块板计划生产片数及损耗计算需求；实际用量以逐板 BOM 明细为准。' : `按本批 ${number(batch.quantity)} 件及各行损耗计算整件需求；`}金额逐行保留两位小数，缺单价时只显示已报价小计。</p>${bomPricingRows(bomItems,batch.bom_pricing,canPrice,boardNames,independent)}<div class="bom-cost-footer"><div class="bom-cost-summary" data-bom-summary role="status" aria-live="polite"></div>${canPrice ? `<div class="bom-cost-actions"><label>修改原因<input id="batch-bom-price-reason" placeholder="例如供应商报价单号"></label><button class="button small primary" id="save-batch-bom-prices" type="button">保存单价</button></div>` : ''}</div></div>`,`<span class="muted">人民币 · 仅物料成本</span>`,true)}</div>` : '';
    const phase = phaseOf(batch);
    const phaseSummary = `<div class="phase-summary"><div><span>当前阶段</span><strong>${phaseMark(phase)}${batch.phase_source === 'quantity_rule' ? '<small>按数量建议，可调整</small>' : ''}</strong></div><div><span>本阶段单号</span><strong class="mono">${display(batch.phase_order_no)}</strong></div><div><span>排期起点</span><strong class="mono">${escape(date(batch.schedule_start || batch.commit_start))}</strong></div><div><span>日历</span><strong>${batch.schedule_mode === 'natural' ? '自然日' : '工作日'}</strong></div></div>`;
    const rdTarget = batch.target_kind === 'customer' ? '客户要求日' : batch.target_kind === 'internal' ? '内部样机目标日' : '目标类型待选';
    const rdSummary = phase === 'rd' ? `<div class="rd-summary"><div><span>${rdTarget}</span><strong>${escape(date(batch.target_date))}</strong></div><div><span>嘉立创下单</span><strong>${escape(date(batch.sampling_order_date))}</strong></div><div><span>预计到板</span><strong>${escape(date(batch.board_eta))}</strong></div><div><span>实际到板</span><strong>${escape(date(batch.board_received_at))}</strong></div><div><span>调试验收</span><strong>${escape(date(batch.debug_accepted_at))}</strong></div></div>` : '';
    const rdQuick = phase === 'rd' && stageEditable ? `<form id="rd-quick-form" class="form-grid rd-quick-form">${formSelect('打样目标类型','target_kind',[['','待选择'],['internal','内部样机目标'],['customer','客户要求交付']],batch.target_kind || '')}${formField('目标日期','target_date','date',batch.target_date || '')}${formField('嘉立创下单日','sampling_order_date','date',batch.sampling_order_date || '')}${formField('预计到板日','board_eta','date',batch.board_eta || '')}${formField('实际到板日','board_received_at','date',batch.board_received_at || '')}${formField('调试验收日','debug_accepted_at','date',batch.debug_accepted_at || '')}<label class="wide">修改原因<input name="reason" placeholder="记录打样日期依据，变更时必填"></label><div class="wide form-row-actions"><span>内部目标与客户要求分别标记；此处日期不覆盖交付批次正式承诺。</span><button class="button small primary" type="submit">保存打样进度</button></div></form>` : '';
    $('#content').innerHTML = `<button class="back-link" id="back-batch">← 返回订单</button><div class="detail-banner"><div><p class="eyebrow">PHASE / 外协生产排期</p><h1>${escape(productionBatchName(batch))}</h1><p>${independent ? '各板独立外协排期' : escape(batch.vendor || '外协厂待填写')} · 客户成品 ${number(batch.quantity)} 件 · BOM ${display(batch.bom_version)}</p></div><div class="detail-meta"><div>${independent ? '逐板厂商承诺' : '厂商承诺开工'}<br><strong>${independent ? `${boardDetails.length} 块板分别登记` : escape(date(batch.commit_start))}</strong></div><div>${independent ? '汇总正式完工' : '厂商承诺完工'}<br><strong>${independent ? escape(boardForecastLabel(forecast)) : escape(date(batch.commit_finish))}</strong></div><div>${materialsResult?.status === 'estimated' ? '物料齐套（临时估算）' : '物料齐套'}<br><strong>${escape(date(ready))}</strong></div><div>${independent ? '逐板工序' : '工序'}<br><strong>${independent ? boardDetails.reduce((total,detail) => total + list(detail.stages).length,0) : stages.length} 项</strong></div></div></div>${missing.length ? `<div class="notice danger">${escape(missing.join('、'))}信息暂时无法加载；请刷新后核对。</div>` : ''}${panel('阶段排期',phaseSummary + rdSummary + rdQuick,`<button class="button small" id="edit-batch">${independent ? '编辑阶段基础信息' : '编辑阶段与厂商承诺'}</button>`,true)}<div class="summary-grid"><div class="metric"><span>正式预测</span><strong style="font-size:19px">${formal && !/无法|待确认|unknown|pending/i.test(String(forecastStatus || '')) ? escape(date(formal)) : '无法确定'}</strong><small>${escape(forecastDescription(forecastStatus,forecast.reason))}</small></div><div class="metric warn"><span>临时顺延估算</span><strong style="font-size:19px">${escape(date(provisional))}</strong><small>未经厂商重新确认</small></div><div class="metric"><span>${independent ? '逐板承诺日期' : '厂商承诺完工'}</span><strong style="font-size:19px">${independent ? `${boardDetails.filter(detail => detail.board.commit_finish).length}/${boardDetails.length} 板已填` : escape(date(batch.commit_finish))}</strong><small>${independent ? '在上方逐板排期中编辑' : '修改日期需填写原因'}</small></div><div class="metric ${Number(materialsResult?.shortage_count || 0) ? 'danger' : ''}"><span>逐料跟踪</span><strong>${materials.length}</strong><small>覆盖锁定 BOM 的全部物料</small></div></div><div class="detail-grid"><div>${panel('BOM 与物料齐套',materialRows,`<div class="table-actions">${batch.bom_locked ? '<span class="badge green">BOM 已锁定</span>' : '<button class="button small" id="lock-bom">锁定 BOM</button>'}<button class="button small" id="record-receipt">登记到料</button></div>`,true)}${bomCostPanel}<div style="height:16px"></div>${panel(independent ? '批次共用节点记录' : '外协工序计划与实际',stageRows,independent ? '' : `<div class="table-actions"><button class="button small" id="edit-template">产品模板</button><button class="button small" id="regenerate-stages">重排计划</button><button class="button small" id="add-stage">+ 工序</button></div>`,true)}</div><div>${panel('批次操作',`<div class="form-stack"><button class="button" id="add-batch-event">记录发料/签收/完工</button><button class="button" id="view-batch-audit">查看修改记录</button></div>`)}${panel(independent ? '逐板档期冲突' : '档期冲突',independent ? '<p class="small-copy">同厂档期重叠在上方逐板排期中核对；每板单独确认可并行并记录依据。</p>' : conflictHtml)}${panel('交付数量关联',allocationHtml)}${panel('外协物流记录',eventHtml)}</div></div>`;
    const boardPanel = batchBoardPanel(batch,materialsResult,boardLineInfo(batch.order_line || {id:batch.order_line_id},boardOverview),boardDetails);
    if (boardPanel) $('#content .summary-grid')?.insertAdjacentHTML('beforebegin',boardPanel);
    if (batch.bom_scope === 'multi') {
      for (const button of [$('#lock-bom'),$('#lock-bom-empty')].filter(Boolean)) button.textContent = '逐板锁定 BOM';
    }
    $$('[data-batch-boards]',$('#content')).forEach(button => button.onclick = () => openBatchBoards(Number(button.dataset.batchBoards)));
    $$('[data-lock-batch-board]',$('#content')).forEach(button => button.onclick = () => openBatchBoardLock(batch,list(batch.boards).find(board => Number(board.id) === Number(button.dataset.lockBatchBoard))));
    $$('[data-board-materials]',$('#content')).forEach(button => button.onclick = () => openMaterialBatch(Number(button.dataset.boardMaterials)));
    $('#back-batch').onclick = () => { state.batchId = null; renderView(); };
    $('#edit-batch').onclick = () => openEditBatch(batch);
    $('#add-batch-event').onclick = () => openProductionEvent(batch,materials);
    $('#view-batch-audit').onclick = () => { state.view = 'audit'; state.batchId = null; state.orderId = null; state.auditFilter = {entity_type:'production_batch',entity_id:batch.id}; renderView(); };
    $('#lock-bom') && ($('#lock-bom').onclick = () => batch.bom_scope === 'multi' ? openBatchBoards(batch.id) : openLockBom(batch));
    $('#lock-bom-empty') && ($('#lock-bom-empty').onclick = () => batch.bom_scope === 'multi' ? openBatchBoards(batch.id) : openLockBom(batch));
    $('#record-receipt').onclick = () => openReceipt(batch);
    const pricingRoot = $('#batch-bom-pricing');
    if (pricingRoot) {
      pricingRoot.addEventListener('input',() => updateBomPricingPreview(pricingRoot,batch.quantity));
      updateBomPricingPreview(pricingRoot,batch.quantity);
      $('#save-batch-bom-prices')?.addEventListener('click',() => saveBatchBomPrices(batch,pricingRoot));
    }
    $('#edit-template') && ($('#edit-template').onclick = () => openStageTemplate(batch.product_code || batch.product?.code || batch.order_line?.product_code));
    $('#regenerate-stages') && ($('#regenerate-stages').onclick = () => openRegenerateStages(batch));
    $('#add-stage') && ($('#add-stage').onclick = () => openAddStage(batch,stages,users));
    if (independent) bindBoardScheduleActions($('#content'),batch,boardDetails,users);
    else bindNodeActions(batch.id,stages,users,$('#content'));
    $('#rd-quick-form')?.addEventListener('submit',event => { event.preventDefault(); saveRdQuick(batch,event.currentTarget); });
    $$('[data-reserve]').forEach(el => el.onclick = () => openReserve(batch,materials.find(x => x.material_code === el.dataset.reserve)));
    $$('[data-reserve-substitute]').forEach(el => el.onclick = () => openSubstituteReserve(batch,materials.find(x => x.material_code === el.dataset.reserveSubstitute)));
    $$('[data-purchase]').forEach(el => el.onclick = () => openPurchase(batch,el.dataset.purchase));
    $$('[data-substitute]').forEach(el => el.onclick = () => openSubstitute(batch,el.dataset.substitute));
    $$('[data-loss]').forEach(el => el.onclick = () => openEditLoss(materials.find(x => x.material_code === el.dataset.loss)));
    $$('[data-conflict-ack]').forEach(el => el.onclick = () => openAcknowledgeConflict(batch));
  }
  async function saveBatchBomPrices(batch,root) {
    if (!updateBomPricingPreview(root,batch.quantity)) { toast('请先核对 BOM 单价。',true); return; }
    const changed = $$('[data-bom-price-row]',root).filter(row => {
      const input = $('.bom-unit-price',row);
      return !samePrice(input.dataset.originalPrice,input.value.trim());
    });
    if (!changed.length) { toast('单价没有变化。'); return; }
    const reasonInput = $('#batch-bom-price-reason',root), reason = reasonInput.value.trim();
    if (!reason) { toast('修改单价需填写原因。',true); reasonInput.focus(); return; }
    const button = $('#save-batch-bom-prices',root);
    button.disabled = true;
    try {
      await api('/bom-items/prices',{method:'PATCH',body:{production_batch_id:batch.id,reason,
        items:changed.map(row => ({id:Number(row.dataset.itemId),version:Number(row.dataset.version),
          unit_price:$('.bom-unit-price',row).value.trim() || null}))}});
    } catch (error) {
      if (error.status === 409) showVersionConflict(root,() => renderBatch());
      else toast(error.message,true);
      button.disabled = false;
      return;
    }
    toast('本批 BOM 单价已保存。');
    try { await renderBatch(); }
    catch (error) { toast(`单价已保存，刷新页面失败：${error.message}`,true); }
  }
  function openEditBatch(batch) {
    const independent = batch.board_schedule_mode === 'independent' && batch.bom_scope === 'multi';
    const fields = independent ? ['phase','phase_order_no','schedule_start','schedule_mode','quantity'] : ['phase','phase_order_no','schedule_start','schedule_mode','vendor','quantity','commit_start','commit_finish'];
    openModal({kicker:independent ? '阶段基础信息' : '阶段与厂商承诺',title:`修改 ${productionBatchName(batch)}`,wide:true,body:`${independent ? '<div class="notice"><p>本批各板独立排期，外协厂和承诺日期请到对应板卡编辑。</p></div>' : ''}<form class="form-grid">${formSelect('排期阶段','phase',Object.entries(phaseNames).filter(([key]) => key !== 'all' && key !== 'unclassified').map(([key,label]) => [key,label]),phaseOf(batch),true)}${formField('本阶段单号','phase_order_no','text',batch.phase_order_no || '')}${formField('排期起点','schedule_start','date',batch.schedule_start || '')}${formSelect('日历规则','schedule_mode',[['workday','工作日'],['natural','自然日']],batch.schedule_mode || 'workday')}${independent ? '' : formField('外协厂','vendor','text',batch.vendor || '')}${formField('客户成品生产数量','quantity','number',batch.quantity,true,'min="0.0001" step="any"')}${independent ? '' : `${formField('厂商承诺开工日','commit_start','date',batch.commit_start || '')}${formField('厂商承诺完工日','commit_finish','date',batch.commit_finish || '')}`}<label class="wide">变更原因（必填）<textarea name="reason" required placeholder="例如阶段调整、客户单号或厂商邮件重新确认"></textarea></label></form>`,onSubmit:async form => {
      const d = getForm(form), changes = changedFields(form,batch,fields);
      if (!Object.keys(changes).length) { closeModal(); return; }
      const result = await saveMutation(`/production-batches/${batch.id}`,withVersion(batch,changes,d.reason),'PATCH','阶段与厂商承诺已更新');
      if (result && Number(state.drawerBatchId) === Number(batch.id)) await showNodeDrawer(batch.id,{restoring:true});
    }});
    bindSchedulePreview($('#modal-body form'),{productCode:batch.product_code || batch.order_line?.product_code,
      batchId:batch.id,boardEta:batch.board_eta,boardReceivedAt:batch.board_received_at});
    if (batch.bom_scope === 'multi') { const input = $('#modal-body [name=quantity]'); input.min = '1'; input.step = '1'; }
  }
  async function saveRdQuick(batch,form) {
    const data = getForm(form), changes = changedFields(form,batch,['target_kind','target_date','sampling_order_date','board_eta','board_received_at','debug_accepted_at']);
    if (!Object.keys(changes).length) { toast('打样进度没有变化。'); return; }
    if (data.target_date && !data.target_kind) { toast('填写打样目标日期时，请选择内部样机或客户要求。',true); $('[name=target_kind]',form).focus(); return; }
    const reason = data.reason.trim();
    if (!reason) { toast('请填写打样日期变更原因。',true); $('[name=reason]',form).focus(); return; }
    const button = $('[type=submit]',form); button.disabled = true;
    try { await api(`/production-batches/${batch.id}`,{method:'PATCH',body:withVersion(batch,changes,reason)}); toast('打样进度已更新。'); renderView(); }
    catch (error) {
      if (error.status === 409) showVersionConflict(form,() => renderView());
      else toast(error.message,true);
      button.disabled = false;
    }
  }
  function openLockBom(batch) {
    if (batch.bom_scope === 'multi') return openBatchBoards(batch.id);
    openModal({kicker:'BOM 快照',title:`锁定 ${productionBatchName(batch)} 的 BOM`,wide:true,body:`<div class="notice warning"><p>锁定后，新导入的 BOM 不会改写本批物料。若需改版，请建立新的生产批次。</p></div><form class="form-grid">${formField('BOM 版本','bom_version','text',batch.bom_version || '',true)}<label>锁定原因<input name="reason" placeholder="例如工程确认 BOM 版本"></label></form><div class="form-section"><h3>物料清单</h3><div id="bom-lock-rows">${bomRowsHtml()}</div><button class="button small" id="bom-lock-add">+ 添加物料行</button> <button class="button small" id="lock-load-bom">从 KIS BOM 加载</button><div class="bom-entry-summary" data-bom-entry-summary role="status" aria-live="polite"></div></div>`,submitText:'锁定 BOM',onSubmit:async form => {
      const d = getForm(form), error = bomEntryError('#bom-lock-rows');
      if (error) { modalMessage(error); return; }
      const items = readBomItems($('#bom-lock-rows'));
      if (!items.length) { modalMessage('至少填写一条完整物料行。'); return; }
      await saveMutation(`/production-batches/${batch.id}/bom-lock`,{version:batch.version,bom_version:d.bom_version,items,reason:d.reason || null},'POST','BOM 已锁定');
    }});
    installArrayRows('#bom-lock-rows','#bom-lock-add');
    bindBomEntryPreview('#bom-lock-rows',() => batch.quantity);
    $('#lock-load-bom').onclick = () => loadCatalogBom(batch.order_line?.bom_product_code || batch.bom_product_code || batch.product_code, $('[name=bom_version]',$('#modal-body')).value, '#bom-lock-rows');
  }
  function openReserve(batch,item) {
    if (!item) return;
    openModal({kicker:'仓库预留',title:`预留 ${item.material_code}`,body:`<div class="notice"><p>只调整这条原料的在库预留。已发料量和替代料预留分别记录；工作台预留不回写 KIS。</p></div><div class="mini-grid"><div class="mini-card"><label>本批需求</label><strong>${number(item.required)}</strong></div><div class="mini-card"><label>原料在库预留</label><strong>${number(item.direct_reservation_quantity ?? 0)}</strong></div><div class="mini-card"><label>总齐套贡献</label><strong>${number(item.reserved)}</strong></div></div><form class="form-grid form-section">${formField('原料在库预留目标量','quantity','number',item.direct_reservation_quantity ?? '0',true,'min="0" step="any"')}</form>`,onSubmit:async form => {
      const d = getForm(form);
      await saveMutation('/reservations',{production_batch_id:batch.id,material_code:item.material_code,quantity:d.quantity,...(item.direct_reservation_version != null ? {version:item.direct_reservation_version} : {})},'PUT','原料预留已更新');
    }});
  }
  async function openSubstituteReserve(batch,item) {
    if (!item) return;
    try {
      const result = await api('/substitutes');
      const approved = list(result.substitutes || result.items || result).filter(x => String(x.production_batch_id) === String(batch.id) && x.original_material_code === item.material_code && x.status === 'approved');
      const codes = [...new Set(approved.map(x => x.substitute_material_code))];
      if (!codes.length) { toast('此物料尚无工程批准的替代料。',true); return; }
      const existing = list(item.substitute_reservations);
      openModal({kicker:'替代料预留',title:`为 ${item.material_code} 预留替代料`,body:`<div class="notice"><p>只可选择工程已批准的料号。输入的是该替代料当前在库预留目标量，不含已发料。</p></div><form class="form-grid">${formSelect('替代料编码','material_code',codes.map(code => [code,code]),codes[0],true)}${formField('在库预留目标量','quantity','number',existing.find(x => x.stock_material_code === codes[0])?.quantity || '0',true,'min="0" step="any"')}</form>`,onSubmit:async form => {
        const d = getForm(form), prior = existing.find(x => x.stock_material_code === d.material_code);
        await saveMutation('/reservations',{production_batch_id:batch.id,for_material_code:item.material_code,material_code:d.material_code,quantity:d.quantity,...(prior ? {version:prior.version} : {})},'PUT','替代料预留已更新');
      }});
      $('[name=material_code]',$('#modal-body')).onchange = event => { $('[name=quantity]',$('#modal-body')).value = existing.find(x => x.stock_material_code === event.target.value)?.quantity || '0'; };
    } catch (error) { toast(error.message,true); }
  }
  function openPurchase(batch,code) {
    openModal({kicker:'采购跟进',title:`登记 ${code} 的采购`,body:`<form class="form-grid">${formField('采购数量','quantity','number','',true,'min="0.0001" step="any"')}${formField('预计到货日','eta','date')}${formSelect('ETA 依据','eta_is_estimate',[['true','人工估算，待确认'],['false','供应商已确认']],'true',true)}${formField('采购单号 / 来源','reference')}<p class="form-note wide">人工估算只用于临时推算；正式预测须有供应商确认日期及外协厂承诺。无 ETA 会生成补日期待办。</p></form>`,onSubmit:async form => {
      const d = getForm(form);
      if (d.eta_is_estimate === 'false' && !d.eta) { $('#modal-error').textContent = '标记为供应商已确认时须填写预计到货日。'; $('#modal-error').hidden = false; return; }
      await saveMutation('/purchases',{production_batch_id:batch.id,material_code:code,quantity:d.quantity,eta:inputDate(d.eta),eta_is_estimate:d.eta_is_estimate === 'true',reference:d.reference || null},'POST','采购记录已添加');
    }});
  }
  function openReceipt(batch,code = '') {
    openModal({kicker:'仓库到料',title:'登记分批到料',body:`<form class="form-grid">${formField('物料编码','material_code','text',code,true)}${formField('到料数量','quantity','number','',true,'min="0.0001" step="any"')}${formField('到料日期','received_at','date',new Date().toISOString().slice(0,10),true)}${formField('来源单号','source_ref')}</form><p class="form-note">到料后需做 IQC；只有合格数量才计入可预留库存。</p>`,onSubmit:async form => {
      const d = getForm(form);
      await saveMutation('/receipts',{...d,production_batch_id:batch.id,source_ref:d.source_ref || null},'POST','到料已登记，请完成 IQC');
    }});
  }
  function openSubstitute(batch,code) {
    openModal({kicker:'替代料申请',title:`申请替代 ${code}`,body:`<div class="notice warning"><p>替代料须由工程账号审批，且合格料由仓库预留后才计入齐套。</p></div><form class="form-grid">${formField('替代料编码','substitute_material_code','text','',true)}${formField('替代数量','quantity','number','',true,'min="0.0001" step="any"')}<label class="wide">申请原因<textarea name="reason" required></textarea></label></form>`,onSubmit:async form => {
      const d = getForm(form);
      await saveMutation('/substitutes',{production_batch_id:batch.id,original_material_code:code,...d},'POST','替代料已提交工程审批');
    }});
  }
  function openAcknowledgeConflict(batch) {
    openModal({kicker:'厂商档期',title:'确认厂商可以并行生产',body:`<div class="notice warning"><p>请先向厂商核实。同厂日期重叠只是本系统的提示，不能证明其真实超产能。</p></div><form class="form-grid"><label class="wide">确认依据与原因<textarea name="reason" required placeholder="记录厂商联系人、确认时间和可并行原因"></textarea></label></form>`,submitText:'记录并行确认',onSubmit:async form => {
      await saveMutation(`/vendors/conflicts/${batch.id}/acknowledge`,{reason:getForm(form).reason},'POST','厂商并行确认已记录');
    }});
  }
  function openProductionEvent(batch,materials) {
    const kinds = [['material_sent','我方发料'],['vendor_received','厂商签收'],['finished','厂商完工'],['returned','成品回厂'],['direct_shipped','成品直发']];
    const reservations = materials.flatMap(item => [{original:item.material_code,actual:item.material_code,quantity:item.direct_reservation_quantity || '0'},...list(item.substitute_reservations).map(res => ({original:item.material_code,actual:res.stock_material_code,quantity:res.quantity}))]).filter(res => Number(res.quantity) > 0);
    openModal({kicker:'外协物流',title:`记录 ${productionBatchName(batch)} 的节点`,body:`<form class="form-grid">${formSelect('节点','kind',kinds,'material_sent',true)}${formField('日期','event_date','date',new Date().toISOString().slice(0,10),true)}<label class="wide" id="material-event-field">选择已预留物料<select name="reservation"><option value="">请选择</option>${reservations.map(res => `<option value="${attr(`${res.original}|${res.actual}`)}">BOM ${escape(res.original)} → 发出 ${escape(res.actual)} · 可发 ${number(res.quantity)}</option>`).join('')}</select></label>${formField('数量','quantity','number','','','min="0.0001" step="any"')}<label class="wide">备注<textarea name="note" placeholder="发料单号、签收凭证或回厂记录"></textarea></label></form>`,onSubmit:async form => {
      const d = getForm(form);
      if (d.kind === 'material_sent' && (!d.reservation || !d.quantity)) { $('#modal-error').textContent = '发料时须选择已预留物料和数量。'; $('#modal-error').hidden = false; return; }
      const [original,actual] = d.reservation?.split('|') || [];
      await saveMutation(`/production-batches/${batch.id}/events`,{kind:d.kind,event_date:d.event_date,quantity:inputNum(d.quantity),...(d.kind === 'material_sent' ? {material_code:actual,for_material_code:original} : {}),note:d.note || null},'POST','外协节点已记录');
    }});
    $('[name=kind]',$('#modal-body')).onchange = event => { $('#material-event-field').hidden = event.target.value !== 'material_sent'; };
  }
  function openAddStage(batch,stages,users = []) {
    const missing = stageNames.filter(name => !list(stages).some(stage => stage.name === name));
    if (!missing.length) { toast('已包含全部流程节点；可直接修改各节点计划与实际日期。'); return; }
    openModal({kicker:'生产工序',title:'启用缺失的流程节点',body:`<form class="form-grid">${formSelect('工序名称','name',missing.map(x => [x,x]),missing[0],true)}${formSelect('节点责任人','owner_id',[['','待指派'],...users.filter(user => user.active && user.status === 'active').map(user => [user.id,user.display_name || user.username])],'')}${formField('计划开始','planned_start','date')}${formField('计划完成','planned_finish','date')}${formField('实际开始','actual_start','date')}${formField('实际完成','actual_finish','date')}${formSelect('状态','status',[['pending','待开始'],['in_progress','进行中'],['done','已完成'],['skipped','跳过']],'pending')}</form>`,onSubmit:async form => {
      const d = getForm(form); ['planned_start','planned_finish','actual_start','actual_finish'].forEach(k => d[k] = inputDate(d[k]));
      d.owner_id = d.owner_id ? Number(d.owner_id) : null;
      d.sequence = stageNames.indexOf(d.name) + 1;
      await saveMutation(`/production-batches/${batch.id}/stages`,d,'POST','工序已添加');
    }});
  }
  function openRegenerateStages(batch) {
    openModal({kicker:'工序计划',title:'按产品模板重新生成计划',body:`<div class="notice warning"><p>会重排本批计划工序；已有实际日期时系统会拒绝覆盖。请先保存模板，再执行此操作。</p></div><form class="form-grid"><label class="wide">重排原因<textarea name="reason" required placeholder="例如厂商重新确认开工日"></textarea></label></form>`,submitText:'重新生成计划',onSubmit:async form => {
      await saveMutation(`/production-batches/${batch.id}/generate-stages`,{version:batch.version,reason:getForm(form).reason},'POST','工序计划已重新生成');
    }});
  }
  function openEditLoss(item) {
    const lines = list(item?.bom_lines);
    if (!lines.length) return;
    const options = lines.map(line => [line.id,`BOM 行 ${line.source_line_no || line.id} · 当前损耗 ${Number(line.loss_rate || 0) * 100}%`]);
    openModal({kicker:'BOM 损耗',title:`调整 ${item.material_code} 损耗率`,body:`<div class="notice"><p>损耗率按小数填写：2% 填 0.02。保存后自动重算物料需求与缺口。</p></div><form class="form-grid">${formSelect('BOM 行','bom_item_id',options,options[0][0],true)}${formField('新损耗率','loss_rate','number',lines[0].loss_rate || '0',true,'min="0" max="1" step="any"')}<label class="wide">变更原因<textarea name="reason" required></textarea></label></form>`,onSubmit:async form => {
      const d = getForm(form), line = lines.find(x => String(x.id) === String(d.bom_item_id));
      await saveMutation(`/bom-items/${line.id}`,{version:line.version,changes:{loss_rate:d.loss_rate},reason:d.reason},'PATCH','损耗率已更新');
    }});
    $('[name=bom_item_id]',$('#modal-body')).onchange = event => { const line = lines.find(x => String(x.id) === event.target.value); $('[name=loss_rate]',$('#modal-body')).value = line?.loss_rate || '0'; };
  }
  async function openStageTemplate(productCode) {
    if (!productCode) { toast('此批次缺少产品编码，请先从订单产品行核对。',true); return; }
    try {
      const template = await api(`/products/${encodeURIComponent(productCode)}/stage-template`);
      const steps = list(template.steps).length ? list(template.steps) : stageNames.map(name => ({name,duration_days:'1',enabled:!['DIP','烧录'].includes(name)}));
      openModal({kicker:'产品模板',title:`${productCode} 工序模板`,wide:true,body:`<div class="notice"><p>模板固定使用已确认的 14 个节点及顺序，仅 DIP、烧录可跳过。按整工作日生成新批次计划初稿；已有批次可逐节点调整。</p></div><div class="table-wrap"><table class="data-table"><thead><tr><th>启用</th><th>工序</th><th>工作日耗时</th></tr></thead><tbody>${steps.map(step => `<tr><td><input type="checkbox" class="template-enabled" ${step.enabled !== false ? 'checked' : ''} ${['DIP','烧录'].includes(step.name) ? '' : 'disabled'}></td><td><input class="input template-name" value="${attr(step.name)}" aria-label="工序名称" readonly></td><td><input class="input template-duration" type="number" min="0" step="1" value="${attr(step.duration_days ?? 1)}" aria-label="工作日耗时"></td></tr>`).join('')}</tbody></table></div>`,submitText:'保存模板',onSubmit:async () => {
        const rows = $$('.data-table tbody tr',$('#modal-body')).map(row => ({name:$('.template-name',row).value.trim(),duration_days:$('.template-duration',row).value,enabled:$('.template-enabled',row).checked}));
        if (rows.some(row => !row.name || Number(row.duration_days) < 0)) { $('#modal-error').textContent = '请填写工序名称与非负耗时。'; $('#modal-error').hidden = false; return; }
        await saveMutation(`/products/${encodeURIComponent(productCode)}/stage-template`,{version:template.version,steps:rows},'PUT','产品工序模板已保存');
      }});
    } catch (error) { toast(error.message,true); }
  }

  function materialReviewTable(rows) {
    return `<div class="table-wrap"><table class="data-table material-review-table"><thead><tr><th>#</th><th>物料编码 / 名称与来源</th><th class="num">BOM 需求</th><th class="num">已预留/已发</th><th class="num">缺口</th><th>到料预计</th><th>人工核对</th><th></th></tr></thead><tbody>${rows.map((item,index) => {
      const review = item.review || {};
      const label = review.status === 'current' ? '已核对' : review.status === 'stale' ? '需复核' : '未核对';
      const boardNames = [...new Set(list(item.bom_lines).map(line => line.board_name || line.board_ref).filter(Boolean))];
      return `<tr><td class="mono">${index + 1}</td><td><strong>${escape(item.material_code)}</strong><span class="sub">${escape(item.material_name || '')}${boardNames.length ? ` · ${escape(boardNames.join(' / '))}` : ''}</span></td><td class="num">${number(item.required)}</td><td class="num">${number(item.reserved)}</td><td class="num">${Number(item.shortage || 0) > 0 ? `<span class="badge red">${number(item.shortage)}</span>` : number(item.shortage)}</td><td>${materialReadyCell(item)}</td><td>${badge(review.status === 'current' ? 'green' : 'yellow',label)}${review.status === 'stale' ? `<span class="sub">${escape(list(review.stale_reasons).join('、'))}</span>` : ''}</td><td><button class="button small ${review.status === 'current' ? '' : 'primary'}" type="button" data-check-material="${attr(item.material_code)}">${review.status === 'current' ? '查看核对' : '逐项核对'} →</button></td></tr>`;
    }).join('')}</tbody></table></div>`;
  }

  async function renderMaterials() {
    const generation = renderGeneration;
    const tabs = [['requirements','逐项核料'],['inventory','库存快照'],['receipts','到料与 IQC'],['purchases','采购 ETA'],['substitutes','替代料审批']];
    const selected = state.materialTab;
    const path = {requirements:'/materials',inventory:'/inventory',receipts:'/receipts',purchases:'/purchases',substitutes:'/substitutes'}[selected];
    const [result,boardOverview] = await Promise.all([api(path),selected === 'requirements' ? api('/board-overview').catch(() => ({order_lines:{},production_batches:{}})) : Promise.resolve(null)]);
    if (generation !== renderGeneration) return;
    if (boardOverview) state.boardOverview = boardOverview;
    let body = '';
    if (selected === 'requirements') {
      const batches = list(result.batches || result.items || result);
      if (!batches.some(batch => Number(batch.id) === Number(state.materialBatchId))) state.materialBatchId = batches[0]?.id || null;
      const batch = batches.find(item => Number(item.id) === Number(state.materialBatchId));
      const progress = batchMaterialProgress(batch);
      const boardInfo = batchBoardInfo(batch,boardOverview);
      const multi = boardInfo.bom_scope === 'multi';
      const canReview = Boolean(batch?.bom_locked || multi && list(batch?.items).length);
      const selector = batches.length ? `<div class="material-workbench-head"><label for="material-batch-select">选择项目 / 生产批次</label><select id="material-batch-select">${batches.map(item => { const boards = batchBoardInfo(item,boardOverview); return `<option value="${attr(item.id)}" ${Number(item.id) === Number(state.materialBatchId) ? 'selected' : ''}>${escape(item.project_name || item.product_name || item.order_no || '未命名项目')} · ${escape(phaseNames[item.phase] || item.phase || '阶段待定')} · ${escape(productionBatchName(item))}${boards.bom_scope === 'multi' ? ` · ${list(boards.boards).length} 块板` : ''}</option>`; }).join('')}</select><span>${batch ? `${escape(batch.order_no || '项目单号待补')} · ${multi ? `${list(boardInfo.boards).filter(board => board.bom_locked).length}/${list(boardInfo.boards).length} 板 BOM 已锁` : `BOM ${escape(batch.bom_version || '待锁定')}`}` : ''}</span></div>` : '';
      const summary = canReview ? `<div class="material-progress"><div><span>本批 BOM 物料</span><strong>${progress.total}</strong></div><div><span>核对记录仍有效</span><strong>${progress.reviewed}</strong></div><div><span>需要重新核对</span><strong>${progress.stale}</strong></div><div><span>仍有缺口</span><strong>${progress.shortage}</strong></div></div><p class="material-definition">“已核对”表示有人确认了当时的 BOM、需求、库存预留与采购依据；“已齐套”仍按合格且已预留的数量单独计算。依据变化后核对会自动失效。</p>` : '';
      const rows = list(batch?.items);
      const boardStrip = multi ? materialBoardStrip(boardInfo,batch) : '';
      const partialNotice = multi && !batch?.bom_locked && rows.length ? '<div class="notice warning"><p>以下是已锁定板卡的物料，可先核对；其余板卡锁定前，本批齐套和正式交期仍无法确定。</p></div>' : '';
      body = `${selector}${boardStrip}${summary}${partialNotice}${!batch ? empty('还没有生产批次','请先在项目排期建立阶段生产批次。') : !canReview ? `<div class="material-gate"><strong>${multi ? '板卡 BOM 尚未锁定' : '本批尚未锁定 BOM'}</strong><p>${multi ? '请逐块选择该批次使用的 BOM 版本；已锁定的板卡物料可先核对。' : '先选择导入目录中的产品编码与版本，并锁定到本批；物料清单才会用于缺口计算和逐项核对。'}</p><button class="button primary" type="button" id="material-link-bom">${multi ? '逐板锁定 BOM' : '关联并锁定 BOM'} →</button></div>` : rows.length ? materialReviewTable(rows) : empty('锁定的 BOM 没有物料','请核对本批 BOM 快照。')}`;
      state.materialBatches = batches;
    } else if (selected === 'inventory') {
      const items = list(result.items || result.inventory || result);
      const latestAt = items.map(item => item.snapshot_as_of).filter(Boolean).sort().at(-1);
      const confirmationCount = items.filter(item => item.confirmation_required).length;
      body = `<div class="notice warning"><p>最近快照：${escape(stamp(latestAt))}。${confirmationCount ? `${confirmationCount} 项 KIS 现存量待仓库确认，这些快照的可预留基数为 0；请核实合格可用量后以“可用量”口径重新导入。` : '按物料分别核对快照口径与时间。'}工作台预留不回写 KIS；快照之后的 IQC 合格入库另计入可用量。</p></div>${items.length ? `<div class="table-wrap"><table class="data-table"><thead><tr><th>物料编码</th><th class="num">KIS 快照数量</th><th class="num">可预留快照基数</th><th class="num">工作台预留</th><th class="num">可再预留</th><th>口径状态</th><th>快照时间</th></tr></thead><tbody>${items.map(item => `<tr><td class="strong">${escape(item.material_code)}</td><td class="num">${number(item.snapshot_quantity)}</td><td class="num">${number(item.usable_snapshot_quantity ?? (item.confirmation_required ? 0 : item.snapshot_quantity))}</td><td class="num">${number(item.reserved)}</td><td class="num">${number(item.free)}</td><td>${item.confirmation_required ? '<span class="badge yellow">现存量待确认</span>' : item.basis === 'available' ? '<span class="badge green">可用量已确认</span>' : '<span class="badge gray">未有快照</span>'}</td><td class="mono">${escape(stamp(item.snapshot_as_of))}</td></tr>`).join('')}</tbody></table></div>` : empty('尚无库存快照','从 KIS 导出小范围库存后，在导入中心导入并确认口径。',`<button class="button small" id="goto-imports">打开导入中心</button>`)}`;
    } else if (selected === 'receipts') {
      const items = list(result.receipts || result.items || result);
      body = `${items.length ? `<div class="table-wrap"><table class="data-table"><thead><tr><th>物料 / 来源</th><th>到料日期</th><th class="num">到料数量</th><th class="num">IQC 合格数量</th><th>IQC 状态</th><th></th></tr></thead><tbody>${items.map(item => `<tr><td><strong>${escape(item.material_code)}</strong><span class="sub">${escape(item.source_ref || '人工登记')}</span></td><td class="mono">${escape(date(item.received_at))}</td><td class="num">${number(item.quantity)}</td><td class="num">${number(item.accepted_quantity ?? 0)}</td><td>${badge(item.iqc_status === 'fail' ? 'red' : item.iqc_status === 'pass' ? 'green' : 'yellow',item.iqc_status === 'fail' ? '不合格' : item.iqc_status === 'pass' ? '已合格' : '待检')}</td><td><button class="button small" data-iqc="${attr(item.id)}">判定 IQC</button></td></tr>`).join('')}</tbody></table></div>` : empty('尚无到料记录','到料可由仓库登记；检验合格后才进入可预留数量。')}`;
      state.receipts = items;
    } else if (selected === 'purchases') {
      const items = list(result.purchases || result.items || result);
      body = `<div class="notice"><p>KIS 导入的采购行须分配到对应生产批次，ETA 才参与该批齐套推算。人工估算日期只显示为临时估算，待采购向供应商确认。</p></div>${items.length ? `<div class="table-wrap"><table class="data-table"><thead><tr><th>采购单号 / 来源</th><th>物料</th><th class="num">采购量</th><th class="num">已分配 / 可分配</th><th>预计到货 / 依据</th><th>操作</th></tr></thead><tbody>${items.map(item => {
        const allocated = list(item.allocations).reduce((sum,allocation) => sum + Number(allocation.quantity || 0),0);
        const remaining = Math.max(0,Number(item.quantity || 0) - allocated);
        return `<tr><td class="mono">${display(item.reference)}<span class="sub">${item.eta_source === 'KIS' ? 'KIS 导入' : '人工登记'}</span></td><td class="strong">${escape(item.material_code)}</td><td class="num">${number(item.quantity)}</td><td class="num">${number(allocated)} / ${number(remaining)}</td><td class="mono">${item.eta ? `${escape(date(item.eta))}<span class="sub">${item.eta_is_estimate ? (item.eta_source === 'KIS' ? 'KIS 日期 · 待供应商确认' : '人工估算 · 待供应商确认') : '供应商已确认'}</span>` : '<span class="badge yellow">缺 ETA</span>'}</td><td><div class="table-actions"><button class="button small" data-purchase-edit="${attr(item.id)}">更新 ETA</button> ${remaining > 0 ? `<button class="button small" data-purchase-allocate="${attr(item.id)}">分配到生产批次</button>` : ''}</div></td></tr>`;
      }).join('')}</tbody></table></div>` : empty('尚无采购记录','从 KIS 导入采购记录，或在生产批次的物料行上登记采购。')}`;
      state.purchases = items;
    } else {
      const items = list(result.substitutes || result.items || result);
      body = `<div class="notice"><p>替代料须工程账号审批；批准后仍需有合格料可用量且由仓库预留。</p></div>${items.length ? `<div class="table-wrap"><table class="data-table"><thead><tr><th>生产批次</th><th>原物料 → 替代料</th><th class="num">数量</th><th>申请原因</th><th>状态</th><th></th></tr></thead><tbody>${items.map(item => `<tr><td>${escape(productionBatchName(item))}</td><td><strong>${escape(item.original_material_code)}</strong> → <strong>${escape(item.substitute_material_code)}</strong></td><td class="num">${number(item.quantity)}</td><td class="wrap">${escape(item.request_reason || item.reason || '')}</td><td>${badge(item.status === 'approved' ? 'green' : item.status === 'rejected' ? 'red' : 'yellow',item.status === 'approved' ? '已批准' : item.status === 'rejected' ? '已拒绝' : '待工程审批')}</td><td>${item.status === 'pending' && state.user.role === 'engineering' ? `<button class="button small" data-sub-approve="${attr(item.id)}">审批</button>` : ''}</td></tr>`).join('')}</tbody></table></div>` : empty('暂无替代料申请','有替代需求时，从生产批次的物料行提交工程审批。')}`;
      state.substitutes = items;
    }
    $('#content').innerHTML = pageHead('采购与仓库 / 物料','按生产批次逐项核对 BOM','先选项目批次；每条物料分别核实数量、预留、到料和 IQC。') + `<div class="tabs" role="tablist">${tabs.map(([key,text]) => `<button class="tab ${selected === key ? 'active' : ''}" role="tab" aria-selected="${selected === key}" data-material-tab="${key}">${text}</button>`).join('')}</div><p class="material-tab-hint">左右滑动标签可查看采购 ETA 和替代料审批 →</p>${panel(tabs.find(x => x[0] === selected)[1],body,'',true)}`;
    $$('[data-material-tab]').forEach(el => el.onclick = () => { state.materialTab = el.dataset.materialTab; renderView(); });
    $('#material-batch-select')?.addEventListener('change',event => { state.materialBatchId = Number(event.target.value); saveUiRoute(); renderView(); });
    $('#material-link-bom')?.addEventListener('click',() => { const batch = state.materialBatches.find(item => Number(item.id) === Number(state.materialBatchId)); if (batch) {
      const hasBoardPlan = list(boardLineInfo({id:batch.order_line_id}).boards).length > 0;
      if (batchBoardInfo(batch).bom_scope === 'multi' || hasBoardPlan) openBatchBoards(batch.id);
      else openProjectBomFlow(`line:${batch.order_line_id}`,batch.id);
    } });
    $$('[data-check-material]').forEach(el => el.onclick = () => openMaterialCheck(Number(state.materialBatchId),el.dataset.checkMaterial));
    $('#goto-imports') && ($('#goto-imports').onclick = () => { state.view = 'imports'; renderView(); });
    $$('[data-iqc]').forEach(el => el.onclick = () => openIqc(state.receipts.find(x => String(x.id) === el.dataset.iqc)));
    $$('[data-purchase-edit]').forEach(el => el.onclick = () => openEditPurchase(state.purchases.find(x => String(x.id) === el.dataset.purchaseEdit)));
    $$('[data-purchase-allocate]').forEach(el => el.onclick = () => openAllocatePurchase(state.purchases.find(x => String(x.id) === el.dataset.purchaseAllocate)));
    $$('[data-sub-approve]').forEach(el => el.onclick = () => openApproveSubstitute(state.substitutes.find(x => String(x.id) === el.dataset.subApprove)));
  }
  async function openMaterialCheck(batchId,materialCode) {
    try {
      const [result,batchResult,inventoryResult,receiptResult,purchaseResult] = await Promise.all([api(`/production-batches/${batchId}/materials`),api(`/production-batches/${batchId}`),api('/inventory'),api('/receipts'),api('/purchases')]);
      const batch = batchResult.batch || batchResult;
      const items = list(result.items);
      const index = items.findIndex(item => item.material_code === materialCode);
      if (index < 0) { toast('该物料已不在本批锁定的 BOM 中，请刷新列表。',true); await renderView(); return; }
      const item = items[index], review = item.review || {};
      const canEdit = state.user.role === 'admin' || state.user.permissions?.materials_edit !== false;
      const lines = list(item.bom_lines);
      const inventory = list(inventoryResult.items || inventoryResult.inventory || inventoryResult).find(entry => entry.material_code === item.material_code);
      const receipts = list(receiptResult.receipts || receiptResult.items || receiptResult).filter(entry => entry.material_code === item.material_code && Number(entry.production_batch_id) === Number(batchId));
      const purchases = list(purchaseResult.purchases || purchaseResult.items || purchaseResult).filter(entry => entry.material_code === item.material_code && list(entry.allocations).some(allocation => Number(allocation.production_batch_id) === Number(batchId)));
      const reviewText = review.status === 'current' ? `已由 ${review.reviewed_by_name || '同事'} 于 ${stamp(review.reviewed_at)} 核对` : review.status === 'stale' ? `上次核对已失效：${list(review.stale_reasons).join('、') || '依据变化'}` : '尚未人工核对';
      const next = items[index + 1]?.material_code, previous = items[index - 1]?.material_code;
      openModal({kicker:`逐项核料 · 第 ${index + 1} / ${items.length} 项`,title:`${item.material_code} ${item.material_name || ''}`,wide:true,
        body:`<div class="material-review-context"><span>${escape(productionBatchName(batch))} · BOM ${escape(batch.bom_version || '待确认')}</span><span>${escape(reviewText)}</span></div>
          <div class="material-review-facts"><div><span>本批 BOM 需求</span><strong>${number(item.required)}</strong></div><div><span>合格已预留 / 已发</span><strong>${number(item.reserved)}</strong></div><div><span>仍有缺口</span><strong class="${Number(item.shortage || 0) > 0 ? 'needs-action' : ''}">${number(item.shortage)}</strong></div><div><span>缺口预计到料</span><strong>${item.missing_eta ? '缺 ETA' : escape(date(item.eta))}</strong><small>${item.eta_is_estimate ? '临时估算，待供应商确认' : item.eta ? '采购 ETA' : '无需到料'}</small></div></div>
          <div class="material-evidence"><div><span>KIS 库存快照</span><strong>${inventory ? `${number(inventory.free)} 可再预留` : '暂无快照'}</strong><small>${inventory ? `${inventory.confirmation_required ? '现存量待仓库确认 · ' : ''}${escape(stamp(inventory.snapshot_as_of))}` : '请核对库存导入记录'}</small></div><div><span>分配到本批的采购</span><strong>${purchases.length} 条</strong><small>${escape(purchases.map(entry => `${entry.reference || '人工登记'} ${date(entry.eta)}${entry.eta_is_estimate ? '（估算）' : ''}`).join('；') || '暂无采购分配')}</small></div><div><span>本批到料与 IQC</span><strong>${receipts.length} 条</strong><small>${escape(receipts.map(entry => `${number(entry.quantity)} 到料 / ${number(entry.accepted_quantity || 0)} 合格`).join('；') || '暂无到料记录')}</small></div></div>
          <details class="material-bom-lines"><summary>BOM 用量依据（${lines.length} 行）</summary><div class="table-wrap"><table class="data-table"><thead><tr><th>来源板卡 / BOM 行</th><th class="num">每件产品有效用量</th><th class="num">损耗率</th><th class="num">本批需求</th></tr></thead><tbody>${lines.map(line => `<tr><td>${line.board_name || line.board_ref ? `${escape(line.board_ref || '')} ${escape(line.board_name || '')} · ` : ''}${escape(line.catalog_source_line_no || line.source_line_no || line.id)}</td><td class="num">${number(line.quantity_per)}</td><td class="num">${number(Number(line.loss_rate || 0) * 100)}%</td><td class="num">${number(line.required)}</td></tr>`).join('')}</tbody></table></div></details>
          ${Number(item.shortage || 0) > 0 ? '<div class="notice warning"><p>此项尚有缺口。核对记录只证明已检查当前依据；仓库预留和采购到料仍需单独处理。</p></div>' : ''}
          ${canEdit ? `<div class="material-review-actions"><button class="button small" id="review-reserve" type="button">仓库预留</button><button class="button small" id="review-purchase" type="button">登记采购 / ETA</button><button class="button small" id="review-receipt" type="button">登记到料</button><details><summary>更多处理</summary><button class="button small" id="review-substitute" type="button">申请替代</button><button class="button small" id="review-loss" type="button">调整损耗</button><button class="button small" id="review-iqc" type="button">查看到料 / IQC</button></details></div>` : ''}
          <form class="form-grid material-review-note"><label class="wide">核对备注（可选）<textarea name="note" placeholder="例如已核对 KIS 库存口径、采购单号和供应商 ETA">${escape(review.note || '')}</textarea></label></form>
          <div class="material-review-nav"><button class="button small" id="review-previous" type="button" ${previous ? '' : 'disabled'}>← 上一项</button><span>${index + 1} / ${items.length}</span><button class="button small" id="review-next" type="button" ${next ? '' : 'disabled'}>下一项 →</button>${review.status === 'current' && canEdit ? '<button class="button small" id="review-uncheck" type="button">撤销核对</button>' : ''}</div>`,
        submitText:review.status === 'current' ? '更新核对记录' : '确认已核对并继续',onSubmit:canEdit ? async form => {
          try {
            await api('/material-reviews',{method:'PUT',body:{production_batch_id:batchId,material_code:item.material_code,state_hash:review.state_hash,confirmed:true,note:$('[name=note]',form).value.trim(),...(review.version != null ? {version:review.version} : {})}});
            closeModal(); toast('核对已留痕；物料齐套仍按合格预留量计算。'); await renderView();
            const following = items.slice(index + 1).find(candidate => candidate.review?.status !== 'current');
            if (following) await openMaterialCheck(batchId,following.material_code);
          } catch (error) {
            if (error.status === 409) showVersionConflict($('#modal-body'),() => { closeModal(); renderView(); });
            else modalMessage(error.message);
          }
        } : null});
      $('#review-previous')?.addEventListener('click',() => openMaterialCheck(batchId,previous));
      $('#review-next')?.addEventListener('click',() => openMaterialCheck(batchId,next));
      $('#review-uncheck')?.addEventListener('click',async () => {
        try { await api('/material-reviews',{method:'PUT',body:{production_batch_id:batchId,material_code:item.material_code,state_hash:review.state_hash,confirmed:false,version:review.version,note:$('[name=note]',$('#modal-body'))?.value.trim() || ''}}); closeModal(); toast('核对记录已撤销。'); await renderView(); }
        catch (error) {
          if (error.status === 409) showVersionConflict($('#modal-body'),() => { closeModal(); renderView(); });
          else modalMessage(error.message);
        }
      });
      $('#review-reserve')?.addEventListener('click',() => openReserve(batch,item));
      $('#review-purchase')?.addEventListener('click',() => openPurchase(batch,item.material_code));
      $('#review-receipt')?.addEventListener('click',() => openReceipt(batch,item.material_code));
      $('#review-substitute')?.addEventListener('click',() => openSubstitute(batch,item.material_code));
      $('#review-loss')?.addEventListener('click',() => openEditLoss(item));
      $('#review-iqc')?.addEventListener('click',() => { closeModal(); state.materialTab = 'receipts'; renderView(); });
    } catch (error) { toast(error.message,true); }
  }
  function openIqc(receipt) {
    if (!receipt) return;
    openModal({kicker:'到料检验',title:`IQC 判定 · ${receipt.material_code}`,body:`<div class="mini-grid"><div class="mini-card"><label>到料数量</label><strong>${number(receipt.quantity)}</strong></div><div class="mini-card"><label>已判合格</label><strong>${number(receipt.accepted_quantity || 0)}</strong></div></div><form class="form-grid form-section">${formSelect('判定结果','status',[['pass','合格'],['fail','不合格']],'pass',true)}${formField('合格数量','accepted_quantity','number',receipt.quantity,true,'min="0" step="any"')}<label class="wide">检验说明<textarea name="reason" placeholder="记录不合格或部分合格原因"></textarea></label></form><p class="form-note">不合格会重算缺口并给采购生成补购或退换待办。</p>`,onSubmit:async form => {
      const d = getForm(form);
      await saveMutation(`/receipts/${receipt.id}/iqc`,{version:receipt.version,status:d.status,accepted_quantity:d.accepted_quantity,reason:d.reason || null},'PATCH','IQC 判定已记录');
    }});
    $('[name=status]',$('#modal-body')).onchange = event => {
      $('[name=accepted_quantity]',$('#modal-body')).value = event.target.value === 'fail' ? '0' : receipt.quantity;
    };
  }
  function openEditPurchase(purchase) {
    if (!purchase) return;
    openModal({kicker:'采购 ETA',title:`更新 ${purchase.material_code} 到货预计`,body:`<form class="form-grid">${formField('预计到货日','eta','date',purchase.eta || '')}${formSelect('ETA 依据','eta_is_estimate',[['true',purchase.eta_source === 'KIS' ? 'KIS 日期，待确认' : '人工估算，待确认'],['false','供应商已确认']],purchase.eta_is_estimate ? 'true' : 'false',true)}${formField('采购单号','reference','text',purchase.reference || '')}<label class="wide">修改说明（日期或依据变更必填）<textarea name="reason" placeholder="例如供应商联系人、确认时间及邮件记录"></textarea></label></form>`,onSubmit:async form => {
      const d = getForm(form), changes = changedFields(form,purchase,['eta','reference']);
      if ('eta' in changes || (d.eta_is_estimate === 'true') !== Boolean(purchase.eta_is_estimate)) changes.eta_is_estimate = d.eta_is_estimate === 'true';
      if (!Object.keys(changes).length) { closeModal(); return; }
      if (d.eta_is_estimate === 'false' && !d.eta) { $('#modal-error').textContent = '标记为供应商已确认时须填写预计到货日。'; $('#modal-error').hidden = false; return; }
      if (('eta' in changes || 'eta_is_estimate' in changes) && !d.reason.trim()) { $('#modal-error').textContent = '修改 ETA 日期或确认依据必须填写原因。'; $('#modal-error').hidden = false; return; }
      await saveMutation(`/purchases/${purchase.id}`,withVersion(purchase,changes,d.reason),'PATCH','采购 ETA 已更新');
    }});
  }
  async function openAllocatePurchase(purchase) {
    if (!purchase) return;
    const allocated = list(purchase.allocations).reduce((sum,allocation) => sum + Number(allocation.quantity || 0),0);
    const remaining = Math.max(0,Number(purchase.quantity || 0) - allocated);
    if (remaining <= 0) { toast('这条采购记录已全部分配。',true); return; }
    try {
      const result = await api('/materials');
      const batches = list(result.batches || result.items || result).filter(batch => list(batch.items).some(item => item.material_code === purchase.material_code));
      if (!batches.length) { toast('没有包含该物料的已锁定 BOM 生产批次。请先建立批次并锁定 BOM。',true); return; }
      const assigned = list(purchase.allocations).map(item => {
        const batch = batches.find(candidate => String(candidate.id) === String(item.production_batch_id));
        return `${batch ? productionBatchName(batch) : `批次 #${item.production_batch_id}`}：${number(item.quantity)}`;
      }).join('；');
      openModal({kicker:'采购分配',title:`将 ${purchase.material_code} 分配到生产批次`,body:`<div class="notice"><p>采购单 ${escape(purchase.reference || '人工登记')} · 采购量 ${number(purchase.quantity)} · 尚可分配 ${number(remaining)}。${assigned ? `已分配：${escape(assigned)}。` : ''}分配后该采购 ETA 才参与所选批次的齐套推算。</p></div><form class="form-grid">${formSelect('生产批次','production_batch_id',batches.map(batch => [batch.id,`${batch.order_no || '订单'} · ${productionBatchName(batch)} · ${batch.product_code || ''}`]),batches[0].id,true)}${formField('本次分配数量','quantity','number',String(remaining),true,`min="0.0001" max="${attr(remaining)}" step="any"`)}</form>`,submitText:'确认分配',onSubmit:async form => {
        const d = getForm(form);
        await saveMutation('/purchase-allocations',{purchase_id:purchase.id,production_batch_id:Number(d.production_batch_id),quantity:d.quantity},'POST','采购数量已分配到生产批次');
      }});
    } catch (error) { toast(error.message,true); }
  }
  function openApproveSubstitute(item) {
    if (!item || state.user.role !== 'engineering') return;
    openModal({kicker:'工程审批',title:`替代料 ${item.substitute_material_code}`,body:`<div class="notice warning"><p>原料 ${escape(item.original_material_code)}，替代 ${escape(item.substitute_material_code)}，数量 ${number(item.quantity)}。审批适用于此生产批次及其已锁定 BOM。</p></div><form class="form-grid">${formSelect('决定','approve',[['true','批准'],['false','拒绝']],'true',true)}<label class="wide">审批依据<textarea name="reason" required placeholder="验证、料号或文件依据"></textarea></label></form>`,onSubmit:async form => {
      const d = getForm(form);
      await saveMutation(`/substitutes/${item.id}/approve`,{version:item.version,approve:d.approve === 'true',reason:d.reason},'POST','替代料审批已记录');
    }});
  }

  const taskKindNames = {
    missing_owner:'指定业务负责人', board_composition_unconfirmed:'确认板卡组成',
    missing_delivery_batch:'建立交付批次', missing_batch:'建立生产批次',
    bom_unlocked:'锁定 BOM', missing_eta:'补充采购 ETA', eta_confirm:'确认采购 ETA',
    stock_confirmation:'确认库存口径', stock_shortage:'库存预留缺口', stock_time_check:'核对库存时间',
    board_quantity_short:'补足板卡计划', missing_vendor_date:'补充厂商承诺',
    vendor_conflict:'核对厂商档期', vendor_reconfirm:'重确认厂商日期',
    stage_overdue:'生产节点逾期', missing_phase_target:'补充研发目标',
    missing_board_eta:'补充预计到板日', phase_target_risk:'研发目标风险',
    delivery_unallocated:'关联生产与交付', due_risk:'客户交期风险',
    missing_delivery_date:'补充交付日期', legacy_pending:'补录项目信息',
    iqc_failed:'处理 IQC 不合格', substitute_approval:'工程审批替代料',
  };
  const importantTaskKinds = new Set(['stage_overdue','due_risk','phase_target_risk','stock_shortage','stock_time_check','iqc_failed','vendor_conflict','vendor_reconfirm','missing_eta','eta_confirm']);
  function taskLookup(orders) {
    const orderById = new Map(), lineById = new Map(), batchById = new Map(), deliveryById = new Map(), boardById = new Map();
    const projects = [];
    for (const order of orders) {
      orderById.set(Number(order.id),order);
      for (const line of list(order.lines)) {
        const project = {key:projectKey(line),name:order.project_name && order.project_name !== line.product_name ? `${order.project_name} / ${line.product_name || line.product_code}` : order.project_name || line.product_name || line.product_code || '未命名项目',order};
        projects.push(project);
        lineById.set(Number(line.id),{line,project});
        for (const batch of list(line.production_batches)) {
          batchById.set(Number(batch.id),{batch,project});
          for (const board of list(batch.boards)) boardById.set(Number(board.id),{board,project});
        }
        for (const delivery of list(line.delivery_batches)) deliveryById.set(Number(delivery.id),{delivery,project});
      }
    }
    return {orderById,lineById,batchById,deliveryById,boardById,projects};
  }
  function taskContext(task,lookup) {
    const type = task.entity_type;
    const id = Number(task.entity_id);
    const line = lookup.lineById.get(Number(task.order_line_id || (type === 'order_lines' ? id : 0)));
    const batch = lookup.batchById.get(Number(task.production_batch_id || (type === 'production_batches' ? id : 0)));
    const delivery = lookup.deliveryById.get(Number(task.delivery_batch_id || (type === 'delivery_batches' ? id : 0)));
    const board = lookup.boardById.get(Number(task.batch_board_id || (type === 'batch_boards' ? id : 0)));
    const project = line?.project || batch?.project || delivery?.project || board?.project;
    const order = lookup.orderById.get(Number(task.order_id || (type === 'orders' ? id : project?.order?.id)));
    const projectKeys = project ? [project.key] : order ? list(order.lines).map(projectKey) : [];
    return {project,order,projectKeys,materialCode:task.material_code || null};
  }
  function taskLabel(task) { return taskKindNames[task.kind] || '其他事项'; }
  function taskIsImportant(task) { return Boolean(task.condition_active) && importantTaskKinds.has(task.kind); }
  function taskMatches(task,context) {
    if (task.status !== state.taskFilter) return false;
    if (state.taskScope === 'mine' && String(task.assignee_id) !== String(state.user.id)) return false;
    if (state.taskScope === 'all' && state.taskAssignee && (state.taskAssignee === 'unassigned' ? task.assignee_id != null : String(task.assignee_id) !== state.taskAssignee)) return false;
    if (state.taskProject && (state.taskProject === 'unlinked' ? context.projectKeys.length : !context.projectKeys.includes(state.taskProject))) return false;
    if (state.taskAttention && !taskIsImportant(task)) return false;
    const query = state.taskSearch.trim().toLocaleLowerCase();
    return !query || [task.title,taskLabel(task),context.project?.name,context.order?.order_no,context.materialCode].some(value => String(value || '').toLocaleLowerCase().includes(query));
  }
  function focusTaskTarget(target) {
    if (!target) return;
    target.classList.add('task-target-highlight');
    target.scrollIntoView({block:'center',behavior:'smooth'});
    setTimeout(() => target.classList.remove('task-target-highlight'),4500);
  }
  async function openTaskTarget(task) {
    if (!task) return;
    const batchId = Number(task.production_batch_id || (task.entity_type === 'production_batches' ? task.entity_id : 0));
    const boardId = Number(task.batch_board_id || (task.entity_type === 'batch_boards' ? task.entity_id : 0));
    const orderId = Number(task.order_id || (task.entity_type === 'orders' ? task.entity_id : 0));
    const materialCode = String(task.material_code || '');
    if (task.entity_type === 'legacy_records' && orderId) { await openOrder(orderId); return; }
    if (task.entity_type === 'inventory' || task.entity_type === 'receipts' || task.entity_type === 'substitutes') {
      state.view = 'materials'; state.orderId = null; state.batchId = null;
      state.materialTab = task.entity_type === 'inventory' ? 'inventory' : task.entity_type === 'receipts' ? 'receipts' : 'substitutes';
      await renderView();
      const rows = $$('.data-table tbody tr',$('#content'));
      const item = task.entity_type === 'inventory' ? rows.find(row => $('td',row)?.textContent.trim() === materialCode)
        : task.entity_type === 'receipts' ? rows.find(row => $('[data-iqc]',row)?.dataset.iqc === String(task.entity_id))
        : rows.find((row,index) => String(state.substitutes?.[index]?.id) === String(task.entity_id));
      focusTaskTarget(item || $('#content .panel'));
      return;
    }
    if (batchId && materialCode && ['missing_eta','eta_confirm'].includes(task.kind)) {
      state.view = 'materials'; state.orderId = null; state.batchId = null;
      state.materialTab = 'requirements'; state.materialBatchId = batchId;
      await renderView();
      const button = $$('[data-check-material]',$('#content')).find(item => item.dataset.checkMaterial === materialCode);
      if (button) { button.click(); return; }
    }
    if (boardId) {
      await openBoardSchedule(boardId);
      if (task.board_stage_id) {
        const step = $$('[data-timeline-stage]',$('#modal-body')).find(item => item.dataset.timelineStage === String(task.board_stage_id));
        if (step) step.click();
      }
      return;
    }
    if (batchId) {
      await openBatch(batchId);
      if (task.stage_id) {
        const step = $$('[data-timeline-stage]',$('#content')).find(item => item.dataset.timelineStage === String(task.stage_id));
        if (step) step.click();
      }
      return;
    }
    if (orderId) {
      await openOrder(orderId);
      if (task.delivery_batch_id) focusTaskTarget($$('[data-delivery-edit]',$('#content')).find(item => item.dataset.deliveryEdit === String(task.delivery_batch_id))?.closest('.entity-card'));
      else if (task.order_line_id) focusTaskTarget($$('[data-line-edit]',$('#content')).find(item => item.dataset.lineEdit === String(task.order_line_id))?.closest('.line-block'));
      return;
    }
    toast('这项待办暂时无法定位，请刷新后再试。',true);
  }
  function paintTaskRows(tasks,users,lookup) {
    const matching = tasks.map(task => ({task,context:taskContext(task,lookup)})).filter(({task,context}) => taskMatches(task,context));
    const scopeTotal = tasks.filter(task => task.status === state.taskFilter && (state.taskScope !== 'mine' || String(task.assignee_id) === String(state.user.id))).length;
    $('#task-results-count').textContent = `${matching.length} / ${scopeTotal} 项`;
    const pageSize = window.matchMedia('(max-width: 760px)').matches ? 10 : 20;
    const pages = Math.max(1,Math.ceil(matching.length / pageSize));
    state.taskPage = Math.min(Math.max(1,state.taskPage),pages);
    const start = (state.taskPage - 1) * pageSize;
    const shown = matching.slice(start,start + pageSize);
    $('#task-results').innerHTML = matching.length ? `<div class="table-wrap"><table class="data-table task-table"><thead><tr><th>问题与处理位置</th><th>责任人</th><th>产生时间</th><th>状态</th><th>操作</th></tr></thead><tbody>${shown.map(({task,context}) => {
      const assignee = users.find(user => String(user.id) === String(task.assignee_id));
      const canClose = task.status !== 'done' && (state.user.role === 'admin' || String(task.assignee_id) === String(state.user.id));
      const stillActive = canClose && Boolean(task.condition_active);
      const location = context.project?.name || context.order?.project_name || context.order?.order_no || (task.entity_type === 'inventory' ? `物料 ${context.materialCode || '库存'}` : '待关联项目');
      return `<tr><td class="wrap task-main" data-label="问题与处理位置"><span class="task-kind ${taskIsImportant(task) ? 'important' : ''}">${escape(taskLabel(task))}</span><strong>${escape(task.title || '待处理事项')}</strong><span class="task-location">${escape(location)}${context.order?.order_no ? ` · ${escape(context.order.order_no)}` : ''}</span></td><td data-label="责任人">${escape(task.assignee_name || assignee?.display_name || (task.assignee_id ? `账号 #${task.assignee_id}` : '待分配'))}</td><td class="mono" data-label="产生时间">${escape(stamp(task.created_at))}</td><td data-label="状态">${badge(task.status === 'done' ? 'green' : task.condition_active ? 'yellow' : 'gray',task.status === 'done' ? '已确认' : task.condition_active ? '待处理' : '条件已恢复')} ${task.status === 'done' && task.condition_active ? '<span class="task-status-note">问题仍存在</span>' : ''}</td><td data-label="操作"><div class="table-actions task-actions"><button class="button small" data-task-open="${attr(task.id)}">查看问题</button>${task.status !== 'done' ? `<button class="button small" data-task-reassign="${attr(task.id)}">转派</button>` : ''}${canClose ? `<button class="button small primary" data-task-done="${attr(task.id)}" ${stillActive ? 'disabled title="先解决问题，条件恢复后才能确认"' : ''}>确认完成</button>` : ''}</div></td></tr>`;
    }).join('')}</tbody></table></div>` : empty('没有符合筛选条件的待办','可调整责任人、项目或关键词查看其他事项。');
    $('#task-pages').innerHTML = matching.length > pageSize ? `<span>显示 ${start + 1}–${start + shown.length} / ${matching.length}</span><button class="button small" type="button" data-task-page="previous" ${state.taskPage === 1 ? 'disabled' : ''}>上一页</button><span>${state.taskPage} / ${pages} 页</span><button class="button small" type="button" data-task-page="next" ${state.taskPage === pages ? 'disabled' : ''}>下一页</button>` : '';
    $$('[data-task-page]').forEach(button => button.onclick = () => { state.taskPage += button.dataset.taskPage === 'next' ? 1 : -1; paintTaskRows(tasks,users,lookup); $('#task-results').scrollIntoView({block:'start'}); });
    $$('[data-task-open]').forEach(button => button.onclick = async () => {
      button.disabled = true;
      try { await openTaskTarget(tasks.find(task => String(task.id) === button.dataset.taskOpen)); }
      catch (error) { toast(error.message,true); }
      finally { button.disabled = false; }
    });
    $$('[data-task-reassign]').forEach(button => button.onclick = () => openReassignTask(tasks.find(task => String(task.id) === button.dataset.taskReassign),users));
    $$('[data-task-done]').forEach(button => button.onclick = () => openDoneTask(tasks.find(task => String(task.id) === button.dataset.taskDone)));
  }
  function paintTaskWorkspace(tasks,users,lookup,projectLoadFailed) {
    const openTasks = tasks.filter(task => task.status === 'open');
    const myOpen = openTasks.filter(task => String(task.assignee_id) === String(state.user.id));
    const activeUsers = users.filter(user => user.active && user.status === 'active');
    const projectOptions = lookup.projects.map(project => `<option value="${attr(project.key)}" ${state.taskProject === project.key ? 'selected' : ''}>${escape(project.name)} · ${escape(project.order.order_no || '内部项目')}</option>`).join('');
    $('#content').innerHTML = pageHead('协作 / 责任待办','待办中心','按责任人和项目处理问题；进入原记录核对后，再确认待办完成。') +
      `<section class="task-workspace"><div class="task-scope" role="group" aria-label="待办范围"><button type="button" data-task-scope="mine" aria-pressed="${state.taskScope === 'mine'}">我的待办 <strong>${myOpen.length}</strong></button><button type="button" data-task-scope="all" aria-pressed="${state.taskScope === 'all'}">全部待办 <strong>${openTasks.length}</strong></button></div>
      <div class="task-controls"><div class="task-status" role="group" aria-label="待办状态"><button type="button" data-task-filter="open" aria-pressed="${state.taskFilter === 'open'}">待确认</button><button type="button" data-task-filter="done" aria-pressed="${state.taskFilter === 'done'}">已完成</button></div><label>责任人<select id="task-assignee" ${state.taskScope === 'mine' ? 'disabled' : ''}><option value="">全部责任人</option><option value="unassigned" ${state.taskAssignee === 'unassigned' ? 'selected' : ''}>待分配</option>${activeUsers.map(user => `<option value="${attr(user.id)}" ${state.taskAssignee === String(user.id) ? 'selected' : ''}>${escape(user.display_name || user.username)}</option>`).join('')}</select></label><label>项目<select id="task-project" ${projectLoadFailed ? 'disabled' : ''}><option value="">全部项目</option><option value="unlinked" ${state.taskProject === 'unlinked' ? 'selected' : ''}>未关联项目 / 公共物料</option>${projectOptions}</select></label><label class="task-search">搜索<input id="task-search" type="search" placeholder="问题、订单号、物料编码" value="${attr(state.taskSearch)}"></label><label class="task-attention"><input id="task-attention" type="checkbox" ${state.taskAttention ? 'checked' : ''}> 仅看重点问题</label></div>
      ${projectLoadFailed ? '<p class="task-hint">项目信息暂时未能读取，项目筛选不可用；仍可按责任人查看待办。问题条件恢复后才能确认完成。</p>' : '<p class="task-hint">重点问题包含交期风险、节点逾期、库存缺口与 IQC 异常。请进入源记录处理；问题条件恢复后才能确认完成。</p>'}
      <div class="panel task-results-panel"><div class="panel-head"><h2>处理清单</h2><small id="task-results-count"></small></div><div class="panel-body" id="task-results"></div><div id="task-pages" class="task-pages"></div></div></section>`;
    paintTaskRows(tasks,users,lookup);
    $$('[data-task-scope]').forEach(button => button.onclick = () => { state.taskScope = button.dataset.taskScope; state.taskAssignee = ''; state.taskPage = 1; saveUiRoute(); paintTaskWorkspace(tasks,users,lookup,projectLoadFailed); });
    $$('[data-task-filter]').forEach(button => button.onclick = () => { state.taskFilter = button.dataset.taskFilter; state.taskPage = 1; saveUiRoute(); paintTaskWorkspace(tasks,users,lookup,projectLoadFailed); });
    $('#task-assignee').onchange = event => { state.taskAssignee = event.target.value; state.taskPage = 1; saveUiRoute(); paintTaskWorkspace(tasks,users,lookup,projectLoadFailed); };
    $('#task-project').onchange = event => { state.taskProject = event.target.value; state.taskPage = 1; saveUiRoute(); paintTaskWorkspace(tasks,users,lookup,projectLoadFailed); };
    $('#task-attention').onchange = event => { state.taskAttention = event.target.checked; state.taskPage = 1; saveUiRoute(); paintTaskRows(tasks,users,lookup); };
    $('#task-search').oninput = event => { state.taskSearch = event.target.value; state.taskPage = 1; paintTaskRows(tasks,users,lookup); };
  }
  async function renderTasks() {
    const generation = renderGeneration;
    const results = await Promise.allSettled([api('/tasks'),api('/users'),api('/orders')]);
    if (generation !== renderGeneration) return;
    if (results[0].status === 'rejected') throw results[0].reason;
    const tasks = list(results[0].value.tasks || results[0].value.items || results[0].value);
    const users = results[1].status === 'fulfilled' ? list(results[1].value.users || results[1].value.items || results[1].value) : [];
    const orders = results[2].status === 'fulfilled' ? list(results[2].value.orders || results[2].value.items || results[2].value) : [];
    const lookup = taskLookup(orders);
    if (state.taskProject && state.taskProject !== 'unlinked' && !lookup.projects.some(project => project.key === state.taskProject)) state.taskProject = '';
    state.tasks = tasks; state.users = users;
    paintTaskWorkspace(tasks,users,lookup,results[2].status === 'rejected');
  }
  function openReassignTask(task,users) {
    if (!task) return;
    const candidates = users.filter(user => user.active && user.status === 'active' && Number(user.id) !== Number(task.assignee_id));
    if (!candidates.length) return toast('没有其他已启用且获批的账号可接收此待办。',true);
    openModal({kicker:'待办责任',title:`转派 ${task.title || task.kind}`,body:`<form class="form-grid">${formSelect('新责任人','assignee_id',[['','请选择接收人'],...candidates.map(u => [u.id,`${u.display_name || u.username}（${roleNames[u.role] || u.role}）`])],'',true)}<label class="wide">转派说明<textarea name="note" required placeholder="说明交接内容和原因"></textarea></label></form>`,submitText:'转派待办',onSubmit:async form => {
      const d = getForm(form);
      await saveMutation(`/tasks/${task.id}`,{version:task.version,changes:{assignee_id:Number(d.assignee_id),note:d.note.trim()},reason:d.note.trim()},'PATCH','待办已转派');
    }});
  }
  function openDoneTask(task) {
    if (!task) return;
    if (task.condition_active) return toast('问题条件仍存在，请先通过“查看问题”处理源记录。',true);
    openModal({kicker:'待办闭环',title:`确认完成 ${task.title || task.kind}`,body:`<div class="notice"><p>问题条件已恢复。请核对处理结果，确认后此待办将移至“已完成”。</p></div><form class="form-grid"><label class="wide">处理结果<textarea name="note" required placeholder="例如新到料日已确认，厂商承诺已更新"></textarea></label></form>`,submitText:'确认完成',onSubmit:async form => {
      const d = getForm(form);
      await saveMutation(`/tasks/${task.id}`,{version:task.version,changes:{status:'done',note:d.note.trim()},reason:d.note.trim()},'PATCH','待办已确认完成');
    }});
  }

  function parseJson(value) { if (value == null) return null; if (typeof value === 'object') return value; try { return JSON.parse(value); } catch (_) { return value; } }
  function auditDiff(entry) {
    const before = parseJson(entry.before_json ?? entry.before), after = parseJson(entry.after_json ?? entry.after);
    if (!before || !after || typeof before !== 'object' || typeof after !== 'object' || Array.isArray(before) || Array.isArray(after)) {
      return `<span class="sub">${escape(entry.action || '修改')}</span>`;
    }
    const keys = [...new Set([...Object.keys(before),...Object.keys(after)])].filter(key => JSON.stringify(before[key]) !== JSON.stringify(after[key]) && !['updated_at','version','password_hash','password_salt'].includes(key));
    return keys.length ? keys.slice(0,6).map(key => `<div><strong>${escape(key)}</strong>：${escape(typeof before[key] === 'object' ? JSON.stringify(before[key]) : before[key] ?? '—')} → ${escape(typeof after[key] === 'object' ? JSON.stringify(after[key]) : after[key] ?? '—')}</div>`).join('') : `<span class="sub">${escape(entry.action || '修改')}</span>`;
  }
  async function renderAudit() {
    const generation = renderGeneration;
    const filter = state.auditFilter || {};
    const query = new URLSearchParams();
    if (filter.entity_type) query.set('entity_type',filter.entity_type);
    if (filter.entity_id) query.set('entity_id',filter.entity_id);
    const result = await api(`/audit${query.size ? `?${query}` : ''}`);
    if (generation !== renderGeneration) return;
    const entries = list(result.entries || result.items || result);
    const rows = entries.length ? `<div class="table-wrap"><table class="data-table"><thead><tr><th>时间 / 操作人</th><th>对象</th><th>前后变化</th><th>原因</th></tr></thead><tbody>${entries.map(entry => `<tr><td class="nowrap"><strong>${escape(stamp(entry.created_at || entry.timestamp))}</strong><span class="sub">${escape(entry.actor_name || entry.user_name || entry.actor_id || '系统')}</span></td><td><strong>${escape(entry.entity_type || '')}</strong><span class="sub">#${escape(entry.entity_id || '')} · ${escape(entry.action || '')}</span></td><td class="wrap">${auditDiff(entry)}</td><td class="wrap">${escape(entry.reason || '—')}</td></tr>`).join('')}</tbody></table></div>` : empty('没有匹配的修改记录','关键字段变更、审批和转派将保存在这里。');
    $('#content').innerHTML = pageHead('留痕 / 修改记录','每次修改都可追溯','查看修改人、时间、前后值及变更原因。',filter.entity_id ? `<button class="button" id="clear-audit-filter">查看全部</button>` : '') + panel(filter.entity_id ? `${filter.entity_type} #${filter.entity_id} 的记录` : '最近修改记录',rows,`<small>${entries.length} 条</small>`,true);
    $('#clear-audit-filter') && ($('#clear-audit-filter').onclick = () => { state.auditFilter = null; renderView(); });
  }

  async function renderSettings() {
    const generation = renderGeneration;
    const requests = [api('/users'),api('/holidays'),api('/health')];
    if (state.user.role === 'admin') requests.push(api('/backups'));
    const results = await Promise.allSettled(requests);
    if (generation !== renderGeneration) return;
    const users = results[0].status === 'fulfilled' ? list(results[0].value.users || results[0].value.items || results[0].value) : [];
    const holidays = results[1].status === 'fulfilled' ? list(results[1].value.holidays || results[1].value.items || results[1].value) : [];
    const backupStatus = results[2].status === 'fulfilled' ? results[2].value.backup || {} : null;
    const backups = results[3]?.status === 'fulfilled' ? list(results[3].value.backups || results[3].value.items || results[3].value) : [];
    const errors = results.map((r,i) => r.status === 'rejected' ? ['用户','工作日','备份状态','备份列表'][i] : null).filter(Boolean);
    const pendingUsers = users.filter(user => user.status === 'pending');
    const activeUsers = users.filter(user => user.status !== 'pending');
    const userBody = activeUsers.length ? `<div class="table-wrap"><table class="data-table account-table"><thead><tr><th>姓名 / 账号</th><th>部门角色</th><th>状态</th><th>权限</th>${state.user.role === 'admin' ? '<th>管理</th>' : ''}</tr></thead><tbody>${activeUsers.map(user => `<tr><td data-label="姓名 / 账号"><strong>${escape(user.display_name || user.username)}</strong><span class="sub mono">${escape(user.username)}</span></td><td data-label="部门角色">${escape(roleNames[user.role] || user.role)}</td><td data-label="状态"><span class="account-status ${attr(user.status || 'active')}">${escape(({active:'启用',disabled:'停用',rejected:'已拒绝'})[user.status] || user.status || '启用')}</span></td><td data-label="权限">${user.role === 'admin' ? '全部权限' : Object.entries(permissionNames).filter(([key]) => user.permissions?.[key]).map(([,label]) => label).join('、') || '无'}</td>${state.user.role === 'admin' ? `<td data-label="管理"><div class="account-actions"><button class="button small" data-manage-user="${attr(user.id)}">修改</button><button class="button small" data-reset-user="${attr(user.id)}">重置密码</button>${Number(user.id) === Number(state.user.id) ? '' : `<button class="button small danger" data-delete-user="${attr(user.id)}">删除</button>`}</div></td>` : ''}</tr>`).join('')}</tbody></table></div>` : empty('还没有已启用账号','管理员可创建账号，或审批注册申请。');
    const pendingBody = pendingUsers.length ? `<div class="table-wrap"><table class="data-table approval-table"><thead><tr><th>申请人</th><th>账号</th><th>提交时间</th><th>操作</th></tr></thead><tbody>${pendingUsers.map(user => `<tr><td class="strong" data-label="申请人">${escape(user.display_name || user.username)}</td><td class="mono" data-label="账号">${escape(user.username)}</td><td data-label="提交时间">${escape(stamp(user.created_at))}</td><td data-label="操作"><div class="account-actions"><button class="button small primary" data-approve-user="${attr(user.id)}">审批通过</button><button class="button small" data-reject-user="${attr(user.id)}">拒绝</button><button class="button small danger" data-delete-user="${attr(user.id)}">删除</button></div></td></tr>`).join('')}</tbody></table></div>` : empty('暂无待审批申请','新同事提交注册申请后会出现在这里。');
    const holidayBody = `${holidays.length ? `<div class="table-wrap"><table class="data-table"><thead><tr><th>日期</th><th>工作日</th><th>说明</th></tr></thead><tbody>${holidays.map(day => `<tr><td class="mono">${escape(date(day.day || day.date))}</td><td>${day.is_workday ? '是' : '否'}</td><td>${display(day.description)}</td></tr>`).join('')}</tbody></table></div>` : empty('暂无特殊日历设置','默认周一至周五为工作日；管理员可维护公司假期或调休。')}`;
    const mirrorStatus = !backupStatus ? '状态暂不可用' : !backupStatus.mirror_configured ? '未配置异地镜像' : backupStatus.mirror_ok === true ? '最近一次镜像成功' : backupStatus.mirror_ok === false ? '最近一次镜像失败' : '已配置，尚无执行结果';
    const localBackupLabel = !backupStatus ? '状态暂不可用' : ({fresh:'备份正常',stale:'备份已过期',missing:'尚无本机备份'})[backupStatus.local_status] || '状态待核对';
    const backupWarnings = [];
    if (backupStatus?.local_status === 'stale') backupWarnings.push(`本机备份超过 ${backupStatus.local_stale_after_hours || 36} 小时，请立即核对自动备份。`);
    if (backupStatus?.local_status === 'missing') backupWarnings.push('尚无本机备份，请先创建一份。');
    if (backupStatus?.disk_low === true) backupWarnings.push('主机磁盘空间不足，请清理或扩容后再导入文件。');
    if (backupStatus?.last_local_failure_at) backupWarnings.push(`本机备份曾在 ${stamp(backupStatus.last_local_failure_at)} 失败，请核对原因。`);
    const backupHealth = `<div class="backup-health"><div><span>本机备份</span><strong class="${backupStatus?.local_status === 'fresh' ? '' : 'is-error'}">${escape(localBackupLabel)}</strong><small>${backupStatus?.last_local_backup_at ? `最近完成 ${escape(stamp(backupStatus.last_local_backup_at))}` : '暂无成功记录'}${backupStatus?.local_age_hours != null ? ` · ${number(backupStatus.local_age_hours)} 小时前` : ''}</small>${backupStatus?.disk_free_gib != null ? `<small>磁盘剩余 ${number(backupStatus.disk_free_gib)} GiB</small>` : ''}</div><div><span>异地镜像</span><strong class="${backupStatus?.mirror_ok === false ? 'is-error' : ''}">${mirrorStatus}</strong>${backupStatus?.last_mirror_success_at ? `<small>上次成功：${escape(stamp(backupStatus.last_mirror_success_at))}</small>` : ''}</div></div>${backupWarnings.length ? `<div class="notice warning"><p>${escape(backupWarnings.join(' '))}</p></div>` : ''}${backupStatus && !backupStatus.mirror_configured ? '<p class="form-note">目前只有主机本地备份；第二备份位置尚未配置。</p>' : ''}`;
    const backupRows = backups.map(item => {
      const filename = typeof item === 'string' ? item : item.filename || item.name;
      return `<tr><td><span class="backup-filename mono" title="${attr(filename)}">${escape(filename)}</span></td><td class="backup-created">${escape(stamp(item.created_at || item.modified_at))}</td><td><button class="button small" data-restore="${attr(filename)}">恢复</button></td></tr>`;
    }).join('');
    const backupList = backups.length
      ? `<div class="backup-list-heading"><strong>备份文件</strong><span>最近 ${backups.length} 份 · 列表内滚动</span></div><div class="table-wrap backup-table-wrap" role="region" tabindex="0" aria-label="本机备份文件"><table class="data-table backup-table"><thead><tr><th>备份文件</th><th>时间</th><th></th></tr></thead><tbody>${backupRows}</tbody></table></div>`
      : empty('暂无备份','点击“立即备份”生成首份本机快照。');
    const backupBody = `${backupHealth}${state.user.role === 'admin' ? `<div class="notice warning"><p>恢复会替换当前工作台数据，请先核对备份时间；系统会在恢复前再创建一份本机备份。</p></div>${backupList}` : `<div class="notice"><p>备份与恢复由管理员执行。</p></div>`}`;
    $('#content').innerHTML = pageHead('系统 / 设置与备份','账号、日历与备份','每人使用个人账号；管理员审批申请并设置权限。工作日按公司日历计算。',`<button class="button" id="change-my-password">修改我的密码</button>`) + (errors.length ? `<div class="notice danger">${escape(errors.join('、'))}信息暂时无法读取。</div>` : '') + `<div class="dashboard-grid settings-layout"><div class="column-stack">${state.user.role === 'admin' ? `${panel('待审批申请',pendingBody,`<span class="badge ${pendingUsers.length ? 'yellow' : 'gray'}">${pendingUsers.length} 人</span>`,true)}<div style="height:16px"></div>` : ''}${panel('内部账号',userBody,state.user.role === 'admin' ? `<button class="button small" id="add-user">+ 创建账号</button>` : '',true)}${panel('公司日历',holidayBody,state.user.role === 'admin' ? `<button class="button small" id="edit-holiday">+ 设置日期</button>` : '',true)}</div><div>${panel('本机备份',backupBody,state.user.role === 'admin' ? `<button class="button small" id="backup-now">立即备份</button>` : '',true)}</div></div>`;
    $('#change-my-password').onclick = openChangePassword;
    $('#add-user') && ($('#add-user').onclick = openCreateUser);
    $('#edit-holiday') && ($('#edit-holiday').onclick = openHoliday);
    $('#backup-now') && ($('#backup-now').onclick = async () => { try { await api('/backups',{method:'POST'}); toast('本机备份已创建'); renderView(); } catch (error) { toast(error.message,true); } });
    $$('[data-restore]').forEach(el => el.onclick = () => openRestore(el.dataset.restore));
    $$('[data-approve-user]').forEach(el => el.onclick = () => openApproveUser(users.find(user => String(user.id) === el.dataset.approveUser)));
    $$('[data-reject-user]').forEach(el => el.onclick = () => openRejectUser(users.find(user => String(user.id) === el.dataset.rejectUser)));
    $$('[data-manage-user]').forEach(el => el.onclick = () => openManageUser(users.find(user => String(user.id) === el.dataset.manageUser)));
    $$('[data-reset-user]').forEach(el => el.onclick = () => openResetUserPassword(users.find(user => String(user.id) === el.dataset.resetUser)));
    $$('[data-delete-user]').forEach(el => el.onclick = async () => {
      const target = users.find(user => String(user.id) === el.dataset.deleteUser);
      if (!target) return;
      try { openDeleteUser(target,users,await api(`/users/${target.id}/delete-preview`)); }
      catch (error) {
        const serviceNotUpdated = error.status === 404 && error.message === 'Not Found';
        toast(serviceNotUpdated ? '服务端尚未加载删除账号功能，请在主机重启 PCBA 服务后刷新页面。' : error.message,true);
      }
    });
  }
  function permissionFields(permissions = {}) {
    return `<fieldset class="permission-fieldset wide"><legend>可执行的操作</legend><div class="permission-grid">${Object.entries(permissionNames).map(([key,label]) => `<label><input type="checkbox" data-permission="${attr(key)}" ${permissions[key] ? 'checked' : ''}><span>${escape(label)}</span></label>`).join('')}</div></fieldset>`;
  }
  function selectedPermissions(form) {
    return Object.fromEntries(Object.keys(permissionNames).map(key => [key,Boolean($(`[data-permission="${key}"]`,form)?.checked)]));
  }
  function permissionSync(form) {
    const role = $('[name=role]',form);
    if (!role) return;
    const update = () => {
      const admin = role.value === 'admin';
      $$('[data-permission]',form).forEach(input => { input.disabled = admin; if (admin) input.checked = true; });
    };
    role.addEventListener('change',update);
    update();
  }
  function modalMessage(message) { const error = $('#modal-error'); error.textContent = message; error.hidden = false; }
  function openApproveUser(user) {
    if (!user || state.user.role !== 'admin') return;
    const defaults = {orders_edit:true,production_edit:true,materials_edit:true,tasks_manage:true,imports_manage:false,audit_view:false};
    openModal({kicker:'账号审批',title:`批准 ${user.display_name || user.username} 的申请`,wide:true,body:`<div class="notice"><p>批准后此人可使用自己的账号登录。请按实际职责选择角色和权限。</p></div><form class="form-grid">${formSelect('部门角色','role',Object.entries(roleNames).map(([key,label]) => [key,label]),'business',true)}${permissionFields({...defaults,...user.permissions})}</form>`,submitText:'批准并启用',onSubmit:async form => {
      const role = $('[name=role]',form).value;
      await saveMutation(`/users/${user.id}/approve`,{role,permissions:selectedPermissions(form)},'POST','账号已审批通过');
    }});
    permissionSync($('#modal-body form'));
  }
  function openRejectUser(user) {
    if (!user || state.user.role !== 'admin') return;
    openModal({kicker:'账号审批',title:`拒绝 ${user.display_name || user.username} 的申请`,body:`<form class="form-grid"><label class="wide">拒绝原因<textarea name="reason" required placeholder="记录审批依据"></textarea></label></form>`,submitText:'确认拒绝',onSubmit:async form => {
      await saveMutation(`/users/${user.id}/reject`,{reason:getForm(form).reason.trim()},'POST','注册申请已拒绝');
    }});
  }
  function openManageUser(user) {
    if (!user || state.user.role !== 'admin') return;
    if (user.status === 'rejected') { toast('已拒绝的申请不能直接启用，请申请人重新注册。',true); return; }
    openModal({kicker:'账号权限',title:`管理 ${user.display_name || user.username}`,wide:true,body:`<form class="form-grid">${formField('姓名','display_name','text',user.display_name || '',true)}${formSelect('部门角色','role',Object.entries(roleNames).map(([key,label]) => [key,label]),user.role,true)}${formSelect('账号状态','active',[['true','启用'],['false','停用']],user.active ? 'true' : 'false',true)}${permissionFields(user.permissions || {})}</form>`,submitText:'保存账号设置',onSubmit:async form => {
      const data = getForm(form), changes = {};
      if (fieldChanged(user.display_name,data.display_name.trim())) changes.display_name = data.display_name.trim();
      if (fieldChanged(user.role,data.role)) changes.role = data.role;
      if (Boolean(user.active) !== (data.active === 'true')) changes.active = data.active === 'true';
      if (data.role !== 'admin') {
        const permissions = selectedPermissions(form);
        if (Object.keys(permissionNames).some(key => Boolean(user.permissions?.[key]) !== permissions[key])) changes.permissions = permissions;
      }
      if (!Object.keys(changes).length) { closeModal(); return; }
      await saveMutation(`/users/${user.id}`,changes,'PATCH','账号设置已更新');
    }});
    permissionSync($('#modal-body form'));
  }
  function openResetUserPassword(user) {
    if (!user || state.user.role !== 'admin') return;
    openModal({kicker:'账号安全',title:`重置 ${user.display_name || user.username} 的密码`,body:`<div class="notice"><p>新密码仅在此处输入，不在工作台中显示或保存明文。</p></div><form class="form-grid">${formField('新密码（至少 10 位）','password','password','',true,'minlength="10" autocomplete="new-password"')}${formField('确认新密码','confirm','password','',true,'minlength="10" autocomplete="new-password"')}</form>`,submitText:'重置密码',onSubmit:async form => {
      const data = getForm(form);
      if (data.password !== data.confirm) { modalMessage('两次输入的密码不一致。'); return; }
      await saveMutation(`/users/${user.id}/reset-password`,{password:data.password},'POST','密码已重置');
    }});
  }
  function openDeleteUser(user,users,preview) {
    if (!user || state.user.role !== 'admin' || Number(user.id) === Number(state.user.id)) return;
    const replacements = users.filter(candidate => Number(candidate.id) !== Number(user.id) && candidate.active && candidate.status === 'active');
    const counts = preview.assignments || {};
    const summary = `在跟订单 ${number(counts.orders || 0)} · 未完成节点 ${number(Number(counts.stages || 0) + Number(counts.board_stages || 0))} · 未确认待办 ${number(counts.tasks || 0)}`;
    openModal({kicker:'账号删除',title:`删除 ${user.display_name || user.username}`,body:`<div class="notice warning"><p>账号 ${escape(user.username)} 删除后立即无法登录；进行中的责任会转给接替人。历史记录和原账号名保留用于追溯，同名账号不能重新注册。</p></div><p class="form-note">待交接：${escape(summary)}</p><form class="form-grid">${formSelect('进行中工作接替人','replacement_user_id',replacements.map(candidate => [candidate.id,`${candidate.display_name || candidate.username} · ${roleNames[candidate.role] || candidate.role}`]),state.user.id,true)}<label class="wide">删除原因<textarea name="reason" required maxlength="200" placeholder="例如人员离职或账号重复"></textarea></label>${formField('输入登录账号确认删除','confirm_username','text','',true,'autocomplete="off"')}</form>`,submitText:'确认删除账号',onSubmit:async form => {
      const data = getForm(form);
      if (data.confirm_username.trim() !== user.username) { modalMessage('请输入准确的登录账号后再删除。'); return; }
      await saveMutation(`/users/${user.id}`,{replacement_user_id:Number(data.replacement_user_id),reason:data.reason.trim()},'DELETE','账号已删除，进行中的工作已交接');
    }});
    $('#modal-submit').classList.remove('primary');
    $('#modal-submit').classList.add('danger');
  }
  function openChangePassword() {
    openModal({kicker:'账号安全',title:'修改我的密码',body:`<form class="form-grid">${formField('当前密码','current_password','password','',true,'autocomplete="current-password"')}${formField('新密码（至少 10 位）','new_password','password','',true,'minlength="10" autocomplete="new-password"')}${formField('确认新密码','confirm','password','',true,'minlength="10" autocomplete="new-password"')}</form>`,submitText:'修改密码',onSubmit:async form => {
      const data = getForm(form);
      if (data.new_password !== data.confirm) { modalMessage('两次输入的新密码不一致。'); return; }
      await saveMutation('/auth/change-password',{current_password:data.current_password,new_password:data.new_password},'POST','密码已修改');
    }});
  }
  function openCreateUser() {
    if (state.user.role !== 'admin') return;
    openModal({kicker:'个人账号',title:'创建内部用户',body:`<form class="form-grid">${formField('登录账号','username','text','',true)}${formField('姓名','display_name','text','',true)}${formSelect('部门角色','role',Object.entries(roleNames).filter(([key]) => key !== 'admin').map(([key,name]) => [key,name]),'business',true)}${formField('初始密码（至少 10 位）','password','password','',true,'minlength="10"')}</form>`,submitText:'创建账号',onSubmit:async form => { await saveMutation('/users',getForm(form),'POST','内部账号已创建'); }});
  }
  function openHoliday() {
    if (state.user.role !== 'admin') return;
    openModal({kicker:'公司日历',title:'设置特殊日期',body:`<form class="form-grid">${formField('日期','date','date','',true)}${formSelect('是否工作日','is_workday',[['false','放假（非工作日）'],['true','调休（工作日）']],'false',true)}${formField('说明','description','text','',true)}</form>`,onSubmit:async form => {
      const d = getForm(form);
      await saveMutation(`/holidays/${encodeURIComponent(d.date)}`,{is_workday:d.is_workday === 'true',description:d.description},'PUT','公司日历已更新');
    }});
  }
  function openRestore(filename) {
    if (state.user.role !== 'admin') return;
    openModal({kicker:'管理员操作',title:'恢复本机备份',body:`<div class="notice danger"><p>恢复 ${escape(filename)} 会替换当前工作台数据。系统将在恢复前先备份当前数据库；恢复后请重新核对订单数据。</p></div><form class="form-grid"><label class="wide">请输入“恢复”以确认<input name="confirmation" required autocomplete="off" placeholder="恢复"></label></form>`,submitText:'执行恢复',onSubmit:async form => {
      if (getForm(form).confirmation !== '恢复') { $('#modal-error').textContent = '请输入“恢复”后再执行。'; $('#modal-error').hidden = false; return; }
      await saveMutation(`/backups/${encodeURIComponent(filename)}/restore`,{},'POST','数据库已恢复，请重新核对数据');
    }});
  }

  let importPdfFile = null;
  let importPdfUrl = null;
  function releaseImportPdf() {
    if (importPdfUrl) URL.revokeObjectURL(importPdfUrl);
    importPdfFile = null;
    importPdfUrl = null;
  }
  function sourcePdfUrl() {
    const file = state.importFile;
    if (!file || !/\.pdf$/i.test(file.name)) return null;
    if (file !== importPdfFile) {
      releaseImportPdf();
      importPdfFile = file;
      // Force PDF MIME and isolate the preview from the application origin.
      importPdfUrl = URL.createObjectURL(new Blob([file], {type:'application/pdf'}));
    }
    return importPdfUrl;
  }
  const importFields = {
    orders:[['order_no','客户订单号 *'],['line_no','订单行号 *'],['customer','客户名称'],['product_code','产品编码 *'],['product_name','产品名称'],['quantity','订单数量 *'],['due_date','客户要求交期'],['promised_date','业务承诺交期']],
    bom:[['product_code','产品编码 *'],['bom_version','BOM 版本 *'],['source_line_no','BOM 行号'],['material_code','物料编码 *'],['material_name','物料名称'],['quantity_per','单位用量 *'],['loss_rate','损耗率'],['unit_price','单价 CNY']],
    inventory:[['material_code','物料编码 *'],['quantity','库存数量 *'],['as_of','快照日期'],['basis','库存口径']],
    purchases:[['reference','采购单号 *'],['source_line_no','采购行号'],['material_code','物料编码 *'],['quantity','采购数量 *'],['eta','预计到货日'],['order_no','关联订单号'],['line_no','关联订单行号']],
    receipts:[['reference','入库单号 *'],['source_line_no','入库行号'],['material_code','物料编码 *'],['quantity','入库数量 *'],['received_at','入库日期'],['iqc_status','IQC 状态'],['accepted_quantity','合格数量'],['order_no','关联订单号'],['line_no','关联订单行号']]
  };
  const sourceAliases = {
    order_no:['订单号','销售订单号','单据编号'],line_no:['行号','分录号','序号'],customer:['客户','客户名称'],product_code:['产品编码','物料代码','产品代码','物料编号'],product_name:['产品名称','物料名称','物 料 描 述','物料描述'],quantity:['数量','订单数量','现存量','库存数量','采购数量','入库数量'],due_date:['交货日期','客户交期','要求交期','交货日期/备注'],promised_date:['承诺交期'],bom_version:['BOM版本','版本号'],source_line_no:['分录号','行号'],material_code:['物料编码','物料代码','物料编号'],material_name:['物料名称'],quantity_per:['单位用量','基本用量','数量'],loss_rate:['损耗率'],unit_price:['单价','含税单价','采购单价','物料单价','参考材料单价','单价(元)','单价（元）'],as_of:['快照日期','库存日期'],basis:['库存口径'],reference:['单据编号','采购单号','入库单号'],eta:['预计到货日期','预计到货日'],received_at:['入库日期','收料日期'],iqc_status:['检验状态'],accepted_quantity:['合格数量']
  };
  function guessedColumn(field,columns) {
    const aliases = state.importType === 'bom' && field === 'product_code'
      ? ['产品编码','母件编码','成品编码'] : (sourceAliases[field] || []);
    return columns.find(column => String(column).toLowerCase() === field.toLowerCase()) ||
      columns.find(column => aliases.includes(String(column))) || '';
  }
  function importTypeName(kind) { return kind === 'legacy' ? '历史项目迁移' : importTypes[kind] || kind || '—'; }
  async function catalogPricingPanel() {
    const result = await api('/bom-catalog');
    const items = list(result.items || result);
    if (!items.length) return panel('BOM 单价与总价',empty('暂无 BOM 物料','导入 BOM 后，可在这里补录单价并按生产数量查看预计物料总价。'));
    const groups = new Map();
    items.forEach(item => {
      const key = JSON.stringify([item.product_code,item.bom_version]);
      if (!groups.has(key)) groups.set(key,[]);
      groups.get(key).push(item);
    });
    if (!groups.has(state.bomCatalogKey)) state.bomCatalogKey = groups.keys().next().value;
    const [productCode,bomVersion] = JSON.parse(state.bomCatalogKey);
    const draftQuantity = decimalFraction(state.bomCostQuantity);
    const summaryQuantity = draftQuantity && draftQuantity.numerator > 0n ? state.bomCostQuantity : '1';
    const pricing = await api(`/bom-catalog/summary?product_code=${encodeURIComponent(productCode)}&bom_version=${encodeURIComponent(bomVersion)}&quantity=${encodeURIComponent(summaryQuantity)}`);
    const options = [...groups.keys()].map(key => {
      const [code,version] = JSON.parse(key);
      return [key,`${code} · ${version}（${groups.get(key).length} 项）`];
    });
    const body = `<div id="bom-catalog-pricing" class="bom-pricing"><div class="form-grid">${formSelect('产品 / BOM 版本','catalog_group',options,state.bomCatalogKey,true)}${formField('测算生产数量','cost_quantity','number',state.bomCostQuantity || '1',true,'min="0.0001" step="any"')}</div><p class="form-note">需求量按生产数量、单位用量和损耗率逐行向上取整；金额按人民币计算。目录价格变更不会覆盖已经锁定的生产批次。</p>${bomPricingRows(groups.get(state.bomCatalogKey),pricing)}<div class="bom-cost-summary" data-bom-summary></div><div class="form-row-actions"><label>改单原因 <input class="input" id="bom-catalog-reason" placeholder="例如采购报价单日期和编号"></label><button class="button primary" id="save-catalog-prices">保存单价</button></div></div>`;
    return panel('BOM 单价与总价',body,`<span class="subtle">${items.length} 条目录物料</span>`);
  }
  function bindCatalogPricing() {
    const root = $('#bom-catalog-pricing');
    if (!root) return;
    const quantity = $('[name=cost_quantity]',root);
    const refresh = () => updateBomPricingPreview(root,quantity.value);
    $('[name=catalog_group]',root).onchange = event => { state.bomCatalogKey = event.target.value; renderImports(); };
    quantity.oninput = () => { state.bomCostQuantity = quantity.value; refresh(); };
    $$('.bom-unit-price',root).forEach(input => { input.oninput = refresh; });
    refresh();
    $('#save-catalog-prices').onclick = async () => {
      if (!refresh()) { toast('请先核对数量和单价。',true); return; }
      const updates = $$('[data-bom-price-row]',root).map(row => {
        const input = $('.bom-unit-price',row), price = input.value.trim();
        if (samePrice(price,input.dataset.originalPrice)) return null;
        return {id:Number(row.dataset.itemId),version:Number(row.dataset.version),unit_price:price || null};
      }).filter(Boolean);
      if (!updates.length) { toast('单价没有变化。'); return; }
      const reason = $('#bom-catalog-reason').value.trim();
      if (!reason) { toast('请填写改单原因。',true); $('#bom-catalog-reason').focus(); return; }
      const button = $('#save-catalog-prices'); button.disabled = true;
      try {
        await api('/bom-catalog/prices',{method:'PATCH',body:{items:updates,quantity:quantity.value,reason}});
        toast(`已保存 ${updates.length} 条 BOM 单价。`);
        await renderImports();
      } catch (error) { toast(error.message,true); button.disabled = false; }
    };
  }
  async function inspectImportFile(file,autoDetectBomHeader = false) {
    const ticket = ++importInspectRequest;
    const kind = state.importType;
    let chosenHeaderRow = state.importHeaderRow;
    const readHeader = async headerRow => {
      const data = new FormData();
      data.append('file',file);
      data.append('kind',kind);
      data.append('header_row',String(headerRow));
      return api('/imports/inspect',{method:'POST',body:data});
    };
    let inspected = await readHeader(chosenHeaderRow);
    if (autoDetectBomHeader && kind === 'bom' && /\.xlsx$/i.test(file.name) && file.size <= 2000000) {
      const score = result => list(result.columns).filter(column =>
        ['material_code','material_name','物料代码','物料编码','物料名称','数量','单位用量']
          .includes(String(column).trim().toLowerCase())).length;
      if (score(inspected) < 2) {
        const next = await readHeader(2);
        if (score(next) > score(inspected)) {
          inspected = next;
          chosenHeaderRow = 2;
        }
      }
    }
    if (ticket !== importInspectRequest || state.importFile !== file || state.importType !== kind) return;
    state.importHeaderRow = chosenHeaderRow;
    state.importInspect = inspected;
    state.importMapping = {...(inspected.suggested_column_map || {})};
  }
  async function extractImportWithDeepSeek() {
    const file = state.importFile;
    const kind = state.importType;
    if (!file || !/\.pdf$/i.test(file.name)) return;
    const ticket = ++importAiRequest;
    const button = $('#ai-extract');
    button.disabled = true;
    button.textContent = 'DeepSeek 正在解析…';
    const data = new FormData();
    data.append('file',file);
    data.append('kind',kind);
    try {
      const result = await api('/imports/ai-extract',{method:'POST',body:data});
      if (ticket !== importAiRequest || state.importFile !== file || state.importType !== kind) return;
      state.importAiExtractionId = result.ai_extraction_id;
      state.importInspect = result;
      state.importPreview = null;
      state.importMapping = {...(result.suggested_column_map || {})};
      state.importFixedFields = {...(result.suggested_fixed_fields || {})};
      state.importHeaderRow = 1;
      state.importSheet = null;
      await renderImports();
      toast(`DeepSeek 已提取 ${number(result.row_count)} 行候选数据，请核对列名和每行差异。`);
    } catch (error) {
      if (ticket !== importAiRequest || state.importFile !== file || state.importType !== kind) return;
      if (!state.importInspect) {
        try { await inspectImportFile(file); } catch (_) { /* Keep the DeepSeek error as the main result. */ }
      }
      toast(`DeepSeek 解析失败：${error.message}`,true);
      await renderImports();
    }
  }
  function parentGeneratedField(field) {
    return state.importType === 'bom' && Boolean(state.bomParentColumn) &&
      (field === 'product_code' || field === 'source_line_no');
  }
  function importMapControl(field,label,columns) {
    const generated = parentGeneratedField(field);
    const selected = Object.hasOwn(state.importMapping,field) ? state.importMapping[field] : guessedColumn(field,columns);
    return `<label for="map-${field}">${escape(label)}${generated ? ' · 由层级生成' : ''}</label><select class="input import-map" id="map-${field}" data-field="${field}" ${generated ? 'disabled' : ''}><option value="">不导入此字段</option>${columns.map(column => `<option value="${attr(column)}" ${selected === column ? 'selected' : ''}>${escape(column)}</option>`).join('')}</select>`;
  }
  function importFixedControl(field,label) {
    const generated = parentGeneratedField(field);
    return `<label>${escape(label.replace(' *',''))}<input class="input import-fixed" data-field="${field}" value="${attr(state.importFixedFields[field] || '')}" placeholder="${generated ? '由母件及 Excel 行号生成' : '无对应列时填一次即可'}" ${generated ? 'disabled' : ''}></label>`;
  }
  async function renderImports() {
    const generation = renderGeneration;
    const [recentResult,aiStatus] = await Promise.all([
      api('/imports').catch(error => ({_error:error.message})),
      api('/imports/ai-status').catch(() => ({configured:false,model:'deepseek-flash'})),
    ]);
    if (generation !== renderGeneration) return;
    state.importAiConfigured = aiStatus.configured === true;
    const pricingHtml = state.importType === 'bom' ? await catalogPricingPanel().catch(error => panel('BOM 单价与总价',`<div class="error-panel">${escape(error.message)}</div>`)) : '';
    if (generation !== renderGeneration) return;
    const recent = list(recentResult.imports || recentResult.items || recentResult);
    const inspect = state.importInspect, preview = state.importPreview;
    const sheets = list(inspect?.sheets);
    const currentSheet = state.importSheet || sheets[0]?.name || '';
    const activeSheet = sheets.find(sheet => sheet.name === currentSheet);
    const columns = list(activeSheet?.columns || inspect?.columns);
    if (state.bomParentColumn && !columns.includes(state.bomParentColumn)) state.bomParentColumn = '';
    const isPdf = state.importFile?.name.toLowerCase().endsWith('.pdf');
    const fixedFieldsSupported = isPdf || (state.importType === 'bom' && /\.xlsx$/i.test(state.importFile?.name || ''));
    const fixedFieldsHtml = fixedFieldsSupported ? `<details class="form-section" open><summary><strong>${isPdf ? 'PDF 页眉' : 'BOM 公共'}固定字段</strong> <span class="form-note">整份文件每一明细行共用；已映射的表格列优先</span></summary><div class="form-grid" style="margin-top:12px">${importFields[state.importType].map(([field,label]) => importFixedControl(field,label)).join('')}</div></details>` : '';
    const bomParentHtml = state.importType === 'bom' && /\.xlsx$/i.test(state.importFile?.name || '') && columns.length ? `<div class="notice warning"><p>${activeSheet?.suggested_bom_parent_column || inspect?.suggested_bom_parent_column ? '检测到母件与子件层级。' : '若文件包含母件和子件层级，'}选择层级列后，系统将母件物料代码作为产品编码、Excel 原行号作为 BOM 行号；重排文件后请逐行核对差异。</p></div>${formSelect('按母件 / 子件拆分','bom_parent_column',[['','不启用，按普通 BOM 行导入'],...columns.map(column => [column,column])],state.bomParentColumn)}` : '';
    const aiConfigured = state.importAiConfigured;
    const aiControls = isPdf ? `<div class="ai-import-controls"><button class="button primary" id="ai-extract" type="button" ${aiConfigured ? '' : 'disabled'}>${state.importAiExtractionId ? '重新载入识别结果' : '重试 DeepSeek 解析'}</button><span class="form-note">${aiConfigured ? `已连接 ${escape(aiStatus.model || 'deepseek-flash')} · 选择 PDF 后自动发送页面图像至 DeepSeek，结果仍需逐行确认` : '管理员须先在 server/.env 配置 DEEPSEEK_API_KEY'}</span></div>` : '';
    const aiWarnings = state.importAiExtractionId ? list(inspect?.warnings) : [];
    const recognitionNotice = state.importAiExtractionId && !aiWarnings.length ? '<div class="notice warning"><p>以下是 DeepSeek 提取的候选数据。请核对页眉单号、每一行物料、数量、日期和价格，再应用更改。</p></div>' : '';
    const aiWarningsHtml = aiWarnings.length ? `<div class="notice warning"><p>${aiWarnings.slice(0,5).map(item => escape(item.message || item)).join('；')}</p></div>` : '';
    const mapping = columns.length ? `<div class="notice"><p>将文件列对应到工作台字段。标 * 的字段须映射表格列或填写文件固定值；未映射字段不会覆盖手工录入值。</p></div>${recognitionNotice}${aiWarningsHtml}${bomParentHtml}<div class="mapping-grid">${importFields[state.importType].map(([field,label]) => importMapControl(field,label,columns)).join('')}</div>${fixedFieldsHtml}<div class="form-row-actions"><span class="form-note">表格 ${number(activeSheet?.row_count ?? inspect.row_count)} 行 · ${number(columns.length)} 列</span><button class="button primary" id="preview-import">预览差异</button></div>` : empty(state.importFile ? '等待解析结果' : '请选择文件',state.importFile ? 'PDF 选择后会自动解析。失败时可用左侧按钮重试。' : '支持 CSV、XLSX 与 PDF 单据。');
    const history = recent.length ? `<div class="table-wrap"><table class="data-table"><thead><tr><th>导入时间</th><th>来源文件</th><th>类型</th><th>结果</th></tr></thead><tbody>${recent.slice(0,12).map(row => `<tr><td class="mono">${escape(stamp(row.created_at || row.imported_at))}</td><td>${escape(row.filename || row.source_filename || '—')}</td><td>${escape(importTypeName(row.kind || row.type))}</td><td>${escape(row.status || row.result || '已导入')}</td></tr>`).join('')}</tbody></table></div>` : empty('暂无导入批次','每次导入的文件、操作者和应用选择会留存。');
    $('#content').innerHTML = pageHead('数据 / 单据导入','文件检查与差异预览','支持 KIS 导出表和 PDF 单据；先核对列名，再逐项选择新增或更新。') + pricingHtml + `<div class="split-layout"><div class="column-stack">${panel('选择数据',`<div class="form-stack">${formSelect('导入类型','kind',Object.entries(importTypes),state.importType)}<div class="upload-drop"><strong>${state.importFile ? escape(state.importFile.name) : '选择 CSV、XLSX 或 PDF 单据'}</strong><p>CSV / XLSX 本机读取 · PDF 选中后自动发送页面图片至 DeepSeek</p><button type="button" class="button small" id="choose-import-file">选择文件</button><input id="import-file" class="visually-hidden" type="file" accept=".csv,.xlsx,.pdf,application/pdf" aria-label="选择导入文件"></div>${aiControls}${state.importFile && !isPdf ? formField('表头所在行','header_row','number',state.importHeaderRow,true,'min="1" max="100" step="1"') : ''}${state.importType === 'inventory' ? `<div class="notice warning"><p>请按 KIS 导出时点填写快照日期，核对数量口径。若现存量含待检、不合格或冻结库存，先由仓库核出可用合格量再导入，避免高估齐套。真实 KIS 字段需等样表确认。</p></div><div class="form-grid">${formField('快照日期','snapshot_as_of','date',state.snapshotAsOf,true)}${formSelect('库存数量口径','snapshot_basis',[['','请选择口径'],['on_hand','现存量'],['available','可用量']],state.snapshotBasis,true)}</div>` : ''}${sheets.length > 1 ? formSelect('工作表','sheet_name',sheets.map(sheet => [sheet.name,`${sheet.name}（${number(sheet.row_count)} 行）`]),currentSheet) : ''}${inspect?.errors?.length ? `<div class="notice danger">${list(inspect.errors).map(e => escape(e.message || e)).join('；')}</div>` : ''}${state.importFile?.name.toLowerCase().endsWith('.pdf') ? '<div class="notice warning"><p>若订单号、BOM 版本等只印在 PDF 页眉，请在“PDF 页眉固定字段”核对或填写。</p></div>' : ''}</div>`)}${panel('最近导入',history,'',true)}</div><div class="column-stack">${panel('列名映射',mapping)}</div></div>${preview ? panel('导入核对',renderImportReview(preview),'',true) : ''}`;
    bindCatalogPricing();
    bindImportReview();
    if (recentResult._error) toast(`最近导入记录暂不可用：${recentResult._error}`,true);
    $('[name=kind]')?.addEventListener('change',async event => {
      ++importAiRequest;
      state.importType = event.target.value; state.importPreview = null; state.importMapping = {}; state.importAiExtractionId = null;
      state.importFixedFields = {}; state.bomParentColumn = '';
      if (state.importFile) {
        if (/\.pdf$/i.test(state.importFile.name)) {
          await renderImports();
          if (state.importAiConfigured) { await extractImportWithDeepSeek(); return; }
        }
        try { await inspectImportFile(state.importFile,state.importType === 'bom'); }
        catch (error) { toast(error.message,true); }
      }
      renderImports();
    });
    $('#choose-import-file').onclick = () => $('#import-file').click();
    $('#ai-extract')?.addEventListener('click',extractImportWithDeepSeek);
    $('#import-file').onchange = async event => {
      const file = event.target.files?.[0]; if (!file) return;
      ++importAiRequest;
      releaseImportPdf();
      state.importFile = file; state.importInspect = null; state.importPreview = null; state.importAiExtractionId = null; state.importMapping = {}; state.importFixedFields = {}; state.importHeaderRow = 1; state.bomParentColumn = ''; state.importSheet = null;
      if (/\.pdf$/i.test(file.name)) {
        await renderImports();
        if (state.importFile !== file) return;
        if (state.importAiConfigured) { await extractImportWithDeepSeek(); return; }
      }
      try { await inspectImportFile(file,true); renderImports(); }
      catch (error) { toast(error.message,true); renderImports(); }
    };
    $('[name=sheet_name]')?.addEventListener('change',event => { state.importSheet = event.target.value; state.importPreview = null; state.importMapping = {}; renderImports(); });
    $('[name=header_row]')?.addEventListener('change',async event => {
      const row = Number(event.target.value);
      if (!Number.isInteger(row) || row < 1 || row > 100 || !state.importFile) { toast('表头行须为 1 至 100。',true); return; }
      ++importAiRequest;
      state.importHeaderRow = row; state.importPreview = null; state.importAiExtractionId = null; state.importMapping = {}; state.importSheet = null;
      try { await inspectImportFile(state.importFile); renderImports(); } catch (error) { toast(error.message,true); }
    });
    $$('.import-map').forEach(el => el.onchange = () => { state.importMapping[el.dataset.field] = el.value; });
    $$('.import-fixed').forEach(el => el.oninput = () => { state.importFixedFields[el.dataset.field] = el.value; });
    $('[name=bom_parent_column]')?.addEventListener('change',event => {
      state.bomParentColumn = event.target.value;
      state.importPreview = null;
      renderImports();
    });
    $('[name=snapshot_as_of]')?.addEventListener('change',event => { state.snapshotAsOf = event.target.value; });
    $('[name=snapshot_basis]')?.addEventListener('change',event => { state.snapshotBasis = event.target.value; });
    $('#preview-import') && ($('#preview-import').onclick = previewImport);
    $('#apply-import') && ($('#apply-import').onclick = applyImport);
  }
  function importReviewState(preview) {
    if (state.importReview?.previewId !== preview.preview_id) {
      state.importReview = {previewId:preview.preview_id, rows:{}, fields:{}, filter:'all', reason:''};
    }
    return state.importReview;
  }
  function exactImportQuantity(values) {
    let numerator = 0n, denominator = 1n;
    for (const raw of values) {
      const value = decimalFraction(raw);
      if (!value) return null;
      if (value.denominator > denominator) {
        numerator *= value.denominator / denominator;
        denominator = value.denominator;
      }
      numerator += value.numerator * (denominator / value.denominator);
    }
    const places = String(denominator).length - 1;
    const whole = String(numerator / denominator).replace(/\B(?=(\d{3})+(?!\d))/g,',');
    const fraction = places ? String(numerator % denominator).padStart(places,'0').replace(/0+$/,'') : '';
    return fraction ? `${whole}.${fraction}` : whole;
  }
  function renderImportReconciliation(preview) {
    const rows = list(preview.rows), valid = rows.filter(row => row.valid);
    const sourceCount = Number(state.importInspect?.row_count);
    const previewCount = Number(preview.summary?.row_count ?? rows.length);
    const sourceText = Number.isFinite(sourceCount) && state.importInspect ? number(sourceCount) : '—';
    let metric = '';
    if (state.importType === 'orders') {
      const quantities = valid.map(row => row.values?.quantity).filter(value => value != null && value !== '');
      metric = `订单数量 ${quantities.length === valid.length ? exactImportQuantity(quantities) : '有缺项'}`;
    } else if (state.importType === 'bom') {
      const priced = valid.filter(row => row.values?.unit_price != null && row.values.unit_price !== '').length;
      const products = new Set(valid.map(row => row.values?.product_code).filter(Boolean)).size;
      metric = `${products} 个产品 · 已填单价 ${priced}/${valid.length} 行`;
    } else {
      const filled = valid.filter(row => row.values?.quantity != null && row.values.quantity !== '').length;
      metric = `已填数量 ${filled}/${valid.length} 行`;
    }
    const difference = Number.isFinite(sourceCount) && state.importInspect ? sourceCount - previewCount : 0;
    const countNote = difference > 0 ? `有 ${difference} 行未进入预览；请核对上方警告及源文件。` :
      difference < 0 ? '预览行数多于检查行数，请重新核对文件。' : '候选明细行数与预览一致。';
    const costNote = state.importType === 'bom' ? 'BOM 总价需在上方按生产数量、损耗和单价测算；这里不跨产品合计物料数量。' :
      state.importType === 'orders' ? '订单数量为预览中有效行的合计，请与原单据核对。' : '不同物料的数量单位可能不同，此处不跨物料求和。';
    return `<div class="import-review-reconcile"><div><span>源文件候选</span><strong>${sourceText} 行</strong></div><div><span>导入预览</span><strong>${number(previewCount)} 行</strong></div><div><span>有效 / 错误</span><strong>${number(valid.length)} / ${number(rows.length - valid.length)}</strong></div><div><span>数量与价格</span><strong>${escape(metric)}</strong></div></div><p class="import-review-count-note">${escape(countNote)} ${escape(costNote)}</p>`;
  }
  function renderImportReview(preview) {
    const details = renderImportPreview(preview);
    const pdfUrl = sourcePdfUrl();
    if (!pdfUrl) return details;
    return `<div class="import-review-layout"><section class="import-review-source" aria-label="原始 PDF 对照"><div class="import-review-source-head"><div><strong>原始 PDF</strong><p>${escape(state.importFile.name)} · 请逐行对照</p></div><a class="button small" href="${attr(pdfUrl)}" target="_blank" rel="noopener noreferrer">新窗口查看</a></div><iframe src="${attr(pdfUrl)}#toolbar=1" title="原始 PDF 对照" referrerpolicy="no-referrer" loading="lazy"></iframe><p class="form-note">若浏览器未能内嵌显示，请点“新窗口查看”。文件仅在当前浏览器本地预览。</p></section><section class="import-review-result" aria-label="解析结果核对">${details}</section></div>`;
  }
  function renderImportPreview(preview) {
    const summary = preview.summary || {}, rows = list(preview.rows), errors = list(preview.errors), warnings = list(preview.warnings);
    const ai = summary.ai === true, review = importReviewState(preview);
    const fieldLabels = Object.fromEntries((importFields[state.importType] || []).map(([field,label]) => [field,label.replace(' *','')]));
    const rowErrors = new Map();
    errors.filter(error => error.row_number != null).forEach(error => {
      const key = String(error.row_number);
      if (!rowErrors.has(key)) rowErrors.set(key,[]);
      rowErrors.get(key).push(error.message || String(error));
    });
    const filters = [['all','全部'],['create','新增'],['update','有差异'],['invalid','错误'],['unchanged','无变化']];
    const rowHtml = rows.length ? rows.map(row => {
      const index = String(row.row_index), invalid = !row.valid || row.action === 'invalid';
      const action = {create:'新增',update:'字段有差异',unchanged:'无变化',invalid:'有错误'}[row.action] || row.action;
      const selected = Object.hasOwn(review.rows,index) ? review.rows[index] : row.action === 'create' && !ai;
      const values = row.values || {};
      const orderedFields = [...Object.keys(fieldLabels).filter(field => Object.hasOwn(values,field)),
                             ...Object.keys(values).filter(field => !Object.hasOwn(fieldLabels,field))];
      const fieldHtml = orderedFields.map(field => {
        const raw = values[field], value = raw == null || raw === '' ? '—' : typeof raw === 'object' ? JSON.stringify(raw) : String(raw);
        return `<div><dt>${escape(fieldLabels[field] || field)}</dt><dd class="${raw == null || raw === '' ? 'missing' : ''}">${escape(value)}</dd></div>`;
      }).join('');
      const changes = list(row.changes);
      const changeHtml = row.action === 'update' ? `<details class="import-review-changes"><summary>选择要更新的字段（默认不选）</summary>${changes.map(change => {
        const fieldKey = `${index}:${change.field}`;
        return `<label class="check-row"><input class="import-field" type="checkbox" data-index="${attr(index)}" data-field="${attr(change.field)}" ${review.fields[fieldKey] ? 'checked' : ''}><span><strong>${escape(fieldLabels[change.field] || change.field)}</strong>：${escape(change.old ?? '—')} → ${escape(change.new ?? '—')}</span></label>`;
      }).join('')}</details>` : '';
      const issueHtml = (rowErrors.get(String(row.row_number)) || []).map(message => `<p class="import-review-row-error">${escape(message)}</p>`).join('');
      return `<article class="diff-field import-review-row" data-import-action="${attr(invalid ? 'invalid' : row.action)}" ${review.filter !== 'all' && review.filter !== (invalid ? 'invalid' : row.action) ? 'hidden' : ''}><div class="import-review-row-head"><label class="check-row"><input type="checkbox" class="import-row" data-index="${attr(index)}" data-action="${attr(row.action)}" ${selected ? 'checked' : ''} ${invalid || row.action === 'unchanged' ? 'disabled' : ''} aria-label="选择候选第 ${attr(row.row_number)} 行"><span><strong>${ai ? '候选' : '源文件'}第 ${escape(row.row_number)} 行</strong><small class="mono">${escape(row.key || '待核对匹配键')}</small></span></label>${badge(invalid ? 'red' : row.action === 'update' ? 'yellow' : 'blue',action)}</div><dl class="import-review-fields">${fieldHtml}</dl>${issueHtml}${changeHtml}</article>`;
    }).join('') : empty('预览没有记录','请检查文件、工作表和列名映射。');
    const errorHtml = errors.length ? `<div class="notice danger"><p>${errors.slice(0,5).map(error => `第 ${error.row_number || '—'} 行 ${escape(error.message || error)}`).join('<br>')}${errors.length > 5 ? `<br>另有 ${errors.length - 5} 项错误` : ''}</p></div>` : '';
    const warningHtml = warnings.length ? `<details class="import-review-issues"><summary>${warnings.length} 项导入提示，应用前请核对</summary><ul>${warnings.map(warning => `<li>第 ${warning.row_number || '—'} 行：${escape(warning.message || warning)}</li>`).join('')}</ul></details>` : '';
    return `<div id="import-review" class="import-review-preview">${renderImportReconciliation(preview)}${ai ? '<div class="notice warning"><p>AI 识别结果仅是候选。新增行默认不选，请对照原始 PDF，确认每行全部字段后再勾选。</p></div>' : ''}<div class="mini-grid import-review-summary"><div class="mini-card"><label>新增</label><strong>${number(summary.create || 0)}</strong></div><div class="mini-card"><label>有差异</label><strong>${number(summary.update || 0)}</strong></div><div class="mini-card"><label>无变化</label><strong>${number(summary.unchanged || 0)}</strong></div><div class="mini-card"><label>错误</label><strong>${number(summary.invalid || rows.filter(row => !row.valid).length)}</strong></div></div>${errorHtml}${warningHtml}<div class="import-review-toolbar" role="group" aria-label="筛选导入预览">${filters.map(([key,label]) => `<button class="button small ${review.filter === key ? 'primary' : ''}" type="button" data-import-filter="${key}" aria-pressed="${review.filter === key}">${label}</button>`).join('')}<span id="import-review-visible" class="form-note"></span></div><div class="import-review-list">${rowHtml}<p id="import-review-filter-empty" class="form-note" hidden>当前筛选没有记录。</p></div><label class="import-review-reason form-note">更新原因（修改数量、交期、损耗率或单价时必填）<textarea id="import-reason" class="input" placeholder="记录单据更新依据">${escape(review.reason)}</textarea></label><div class="form-row-actions import-review-actions"><span id="import-review-selection-count" class="form-note"></span><button class="button primary" id="apply-import" ${!preview.preview_id ? 'disabled title="预览未生成批次编号"' : ''}>${ai ? '应用已核对行' : '应用所选更改'}</button></div></div>`;
  }
  function bindImportReview() {
    const root = $('#import-review');
    if (!root || !state.importReview) return;
    const review = state.importReview;
    const updateCounts = () => {
      const rows = $$('.import-row:checked',root);
      const updates = rows.filter(row => row.dataset.action === 'update' &&
        $$('.import-field:checked',row.closest('.import-review-row')).length);
      $('#import-review-selection-count',root).textContent = `已选 ${rows.filter(row => row.dataset.action === 'create').length} 条新增、${updates.length} 条更新；筛选不会清除已选内容。`;
      const visible = $$('.import-review-row:not([hidden])',root).length;
      $('#import-review-visible',root).textContent = `显示 ${visible} / ${$$('.import-review-row',root).length} 行`;
      $('#import-review-filter-empty',root).hidden = visible !== 0;
    };
    const applyFilter = () => {
      $$('.import-review-row',root).forEach(row => { row.hidden = review.filter !== 'all' && row.dataset.importAction !== review.filter; });
      $$('[data-import-filter]',root).forEach(button => {
        const active = button.dataset.importFilter === review.filter;
        button.classList.toggle('primary',active);
        button.setAttribute('aria-pressed',String(active));
      });
      updateCounts();
    };
    $$('[data-import-filter]',root).forEach(button => button.onclick = () => { review.filter = button.dataset.importFilter; applyFilter(); });
    $$('.import-row',root).forEach(input => input.onchange = () => {
      const card = input.closest('.import-review-row');
      if (!input.checked) {
        $$('.import-field',card).forEach(field => { field.checked = false; review.fields[`${field.dataset.index}:${field.dataset.field}`] = false; });
      }
      review.rows[input.dataset.index] = input.checked;
      updateCounts();
    });
    $$('.import-field',root).forEach(input => input.onchange = () => {
      const card = input.closest('.import-review-row'), row = $('.import-row',card);
      review.fields[`${input.dataset.index}:${input.dataset.field}`] = input.checked;
      row.checked = $$('.import-field:checked',card).length > 0;
      review.rows[input.dataset.index] = row.checked;
      updateCounts();
    });
    $('#import-reason',root).oninput = event => { review.reason = event.target.value; };
    applyFilter();
  }
  async function previewImport() {
    if (!state.importFile) { toast('请先选择导出文件。',true); return; }
    const map = {};
    $$('.import-map').forEach(el => { if (!el.disabled && el.value) map[el.dataset.field] = el.value; });
    const fixed = {};
    $$('.import-fixed').forEach(el => { if (!el.disabled && el.value.trim()) fixed[el.dataset.field] = el.value.trim(); });
    const required = importFields[state.importType].filter(([,label]) => label.includes('*')).map(([key]) => key);
    const missing = required.filter(key => !map[key] && !fixed[key] && !parentGeneratedField(key));
    if (missing.length) { toast(`请映射必填字段：${missing.join('、')}`,true); return; }
    state.importMapping = map; state.importFixedFields = fixed;
    const data = new FormData(); data.append('file',state.importFile); data.append('kind',state.importType); data.append('column_map',JSON.stringify(map)); data.append('header_row',String(state.importHeaderRow));
    if (state.importAiExtractionId) data.append('ai_extraction_id',state.importAiExtractionId);
    if (Object.keys(fixed).length) data.append('fixed_fields',JSON.stringify(fixed));
    if (state.bomParentColumn) data.append('bom_parent_column',state.bomParentColumn);
    if (state.importSheet) data.append('sheet_name',state.importSheet);
    if (state.importType === 'inventory') {
      const asOf = $('[name=snapshot_as_of]')?.value;
      const basis = $('[name=snapshot_basis]')?.value;
      if (!asOf || !basis) { toast('库存导入须填写快照日期并确认现存量或可用量口径。',true); return; }
      state.snapshotAsOf = asOf; state.snapshotBasis = basis;
      data.append('snapshot_as_of',asOf); data.append('snapshot_basis',basis);
    }
    const button = $('#preview-import'); button.disabled = true;
    try { state.importPreview = await api('/imports/preview',{method:'POST',body:data}); renderImports(); }
    catch (error) { toast(error.message,true); button.disabled = false; }
  }
  async function applyImport() {
    const preview = state.importPreview;
    if (!preview?.preview_id) return;
    const selections = $$('.import-row:checked').map(row => {
      const rowIndex = Number(row.dataset.index);
      const detail = list(preview.rows).find(x => x.row_index === rowIndex);
      const fields = $$('.import-field:checked').filter(el => Number(el.dataset.index) === rowIndex).map(el => el.dataset.field);
      return detail?.action === 'update' ? {row_index:rowIndex,fields} : {row_index:rowIndex};
    }).filter(row => !('fields' in row) || row.fields.length);
    if (!selections.length) { toast(preview.summary?.ai ? '请先对照原始 PDF，逐条勾选确认过的候选行。' : '请勾选至少一条新增记录或一个要更新的字段。',true); return; }
    const reason = $('#import-reason')?.value.trim() || '';
    const protectedFields = new Set(['quantity','due_date','promised_date','eta','quantity_per','loss_rate','unit_price']);
    if (!reason && selections.some(row => row.fields?.some(field => protectedFields.has(field)))) { toast('更新数量、交期、ETA、损耗率或单价必须填写原因。',true); $('#import-reason').focus(); return; }
    const button = $('#apply-import'); button.disabled = true;
    try { const result = await api('/imports/apply',{method:'POST',body:{preview_id:preview.preview_id,selections,reason:reason || null}}); releaseImportPdf(); state.importReview = null; state.importPreview = null; state.importInspect = null; state.importFile = null; state.importAiExtractionId = null; state.importMapping = {}; toast(`已应用 ${number(result.rows_applied ?? selections.length)} 行，请核对导入记录。`); renderView(); }
    catch (error) { toast(error.message,true); button.disabled = false; }
  }

  const bootstrapButton = $('#bootstrap-open');
  bootstrapButton.hidden = true;
  bootstrapButton.onclick = () => openModal({kicker:'首次设置',title:'创建主机管理员',body:`<div class="notice"><p>只在工作台主机本机首次创建。之后每人使用自己的账号登录。</p></div><form class="form-grid">${formField('登录账号','username','text','admin',true)}${formField('姓名','display_name','text','管理员',true)}${formField('密码（至少 10 位）','password','password','',true,'minlength="10"')}</form>`,submitText:'创建并登录',onSubmit:async form => {
    const data = getForm(form);
    try { await api('/auth/bootstrap',{method:'POST',body:data}); const result = await api('/auth/login',{method:'POST',body:{username:data.username,password:data.password}}); closeModal(); toast('管理员已创建'); signIn(result.token,result.user); }
    catch (error) { $('#modal-error').textContent = error.message; $('#modal-error').hidden = false; }
  }});
  if (state.token) api('/auth/me').then(user => signIn(state.token,user)).catch(() => signOut(false));
  else signOut(false);
})();
