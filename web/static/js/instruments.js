(() => {
  const statusEl = document.getElementById('instStatus');
  const backfillAllBtn = document.getElementById('backfillAllBtn');
  const progressWrapEl = document.getElementById('instProgressWrap');
  const progressEl = document.getElementById('instProgress');
  const progressTextEl = document.getElementById('instProgressText');
  const completionNoticeEl = document.getElementById('instCompletionNotice');
  const completionTextEl = document.getElementById('instCompletionText');
  const completionCloseBtn = document.getElementById('instCompletionClose');
  const clearFiltersBtn = document.getElementById('instClearFiltersBtn');
  const sideNavEl = document.getElementById('instSideNav');
  const tableBody = document.querySelector('#instTable tbody');
  const addInstrumentBtn = document.getElementById('addInstrumentBtn');
  const addInstrumentModal = document.getElementById('addInstrumentModal');
  const addInstrumentForm = document.getElementById('addInstrumentForm');
  const addInstrumentCloseBtn = document.getElementById('addInstrumentCloseBtn');
  const addInstrumentCancelBtn = document.getElementById('addInstrumentCancelBtn');
  const addInstrumentConfirmBtn = document.getElementById('addInstrumentConfirmBtn');
  const addInstrumentSymbolEl = document.getElementById('addInstrumentSymbol');
  const addInstrumentNameEl = document.getElementById('addInstrumentName');
  const addInstrumentMsgEl = document.getElementById('addInstrumentMsg');
  const addCategoryEls = {
    l1: document.getElementById('addCategoryL1'),
    l2: document.getElementById('addCategoryL2'),
    l3: document.getElementById('addCategoryL3'),
  };
  const addCategorySelectsEl = document.getElementById('addCategorySelects');
  const addCategoryAutoEl = document.getElementById('addCategoryAuto');
  const addCategoryAutoText = document.getElementById('addCategoryAutoText');
  const addCategoryManualBtn = document.getElementById('addCategoryManualBtn');
  const editInstrumentModal = document.getElementById('editInstrumentModal');
  const editInstrumentForm = document.getElementById('editInstrumentForm');
  const editInstrumentCloseBtn = document.getElementById('editInstrumentCloseBtn');
  const editInstrumentCancelBtn = document.getElementById('editInstrumentCancelBtn');
  const editInstrumentConfirmBtn = document.getElementById('editInstrumentConfirmBtn');
  const editInstrumentSymbolEl = document.getElementById('editInstrumentSymbol');
  const editInstrumentNameEl = document.getElementById('editInstrumentName');
  const editInstrumentMsgEl = document.getElementById('editInstrumentMsg');
  const editCategoryEls = {
    l1: document.getElementById('editCategoryL1'),
    l2: document.getElementById('editCategoryL2'),
    l3: document.getElementById('editCategoryL3'),
  };
  const filterEls = {
    l1: document.getElementById('instFilterL1'),
    l2: document.getElementById('instFilterL2'),
    l3: document.getElementById('instFilterL3'),
  };
  const etfConstituentsModal = document.getElementById('etfConstituentsModal');
  const etfConstituentsTitle = document.getElementById('etfConstituentsTitle');
  const etfConstituentsFreshness = document.getElementById('etfConstituentsFreshness');
  const etfConstituentsBody = document.getElementById('etfConstituentsBody');
  const etfConstituentsMsg = document.getElementById('etfConstituentsMsg');
  const etfConstituentsImportBtn = document.getElementById('etfConstituentsImportBtn');
  const etfConstituentsCloseBtn = document.getElementById('etfConstituentsCloseBtn');
  const today = localToday();
  const filters = { l1: new Set(), l2: new Set(), l3: new Set() };
  let allItems = [];
  let categoryItems = [];
  let addLookupTimer = null;
  let addLookupSeq = 0;
  let isAddRunning = false;
  let addPollTimer = null;
  let isBulkRunning = false;
  let bulkPollTimer = null;
  let currentBulkJobId = null;
  let currentCompletionJobId = null;
  let completedJobRefreshDone = null;
  let completedAddJobRefreshDone = null;
  let editingSymbol = '';
  let isEditSaving = false;
  let addAutoCategory = null; // 申万命中时的自动归类 {l1, l2, l3}；null=手动选择模式
  let constituentsEtfSymbol = '';
  let constituentsPollTimer = null;
  let isConstituentsImporting = false;
  const dismissedCompletionKey = 'instrumentBulkBackfillDismissedJobId';


  function dateRangeText(start, end) {
    if (!start && !end) return '-';
    return `${start || '-'} ~ ${end || '-'}`;
  }

  function dateOnly(value) {
    const text = String(value || '').trim();
    const match = text.match(/^\d{4}-\d{2}-\d{2}/);
    return match ? match[0] : '';
  }


  function valueOrDash(value) {
    const text = String(value || '').trim();
    return text || '-';
  }

  function normalizeSymbolInput(value) {
    const text = String(value || '').trim().toUpperCase();
    if (!text) return '';
    if (text.includes('.')) {
      const [code, rawSuffix] = text.split('.', 2);
      const suffix = rawSuffix === 'SH' ? 'SS' : rawSuffix;
      return `${code}.${suffix}`;
    }
    const digits = text.replace(/\D/g, '');
    if (digits.length !== 6) return text;
    return `${digits}.${digits.startsWith('5') || digits.startsWith('6') ? 'SS' : 'SZ'}`;
  }

  function knownSymbolSet() {
    return new Set((allItems || []).map((item) => String(item.symbol || '').trim().toUpperCase()).filter(Boolean));
  }

  function setAddMessage(text, isError = false) {
    if (!addInstrumentMsgEl) return;
    addInstrumentMsgEl.textContent = text || '';
    addInstrumentMsgEl.classList.toggle('is-error', Boolean(isError));
  }

  function syncTaskControls() {
    const disabled = isBulkRunning || isAddRunning;
    if (backfillAllBtn) backfillAllBtn.disabled = disabled;
    if (addInstrumentBtn) addInstrumentBtn.disabled = disabled;
    updateAddConfirmState();
  }

  function addFormPayload() {
    const nameLoading = addInstrumentNameEl?.dataset.loading === '1';
    return {
      symbol: normalizeSymbolInput(addInstrumentSymbolEl?.dataset.symbol || addInstrumentSymbolEl?.value || ''),
      name: nameLoading ? '' : String(addInstrumentNameEl?.value || '').trim(),
      category_l1: String(addCategoryEls.l1?.value || '').trim(),
      category_l2: String(addCategoryEls.l2?.value || '').trim(),
      category_l3: String(addCategoryEls.l3?.value || '').trim(),
    };
  }

  function setAddCategoryMode(mode, data) {
    // auto：申万命中，隐藏下拉、显示自动归类文本；manual：显示下拉手动选择
    addAutoCategory = mode === 'auto' ? data : null;
    if (addCategorySelectsEl) addCategorySelectsEl.classList.toggle('is-hidden', mode === 'auto');
    if (addCategoryAutoEl) addCategoryAutoEl.hidden = mode !== 'auto';
    if (mode === 'auto' && addCategoryAutoText) {
      addCategoryAutoText.textContent = `${data.category_l1}-${data.category_l2}-${data.category_l3}`;
    }
  }

  function updateAddConfirmState() {
    if (!addInstrumentConfirmBtn) return;
    const payload = addFormPayload();
    const hasCategory = addAutoCategory || (payload.category_l1 && payload.category_l2 && payload.category_l3);
    const ready = payload.symbol && payload.name && hasCategory;
    addInstrumentConfirmBtn.disabled = !ready || isBulkRunning || isAddRunning;
  }

  function tagsText(item) {
    const tags = Array.isArray(item.factor_tags) ? item.factor_tags : [];
    return tags.length ? tags.join(' / ') : '-';
  }

  function itemMatchesFilters(item) {
    const l1 = String(item.category_l1 || '').trim();
    const l2 = String(item.category_l2 || '').trim();
    const l3 = String(item.category_l3 || '').trim();
    if (filters.l1.size && !filters.l1.has(l1)) return false;
    if (filters.l2.size && !filters.l2.has(l2)) return false;
    if (filters.l3.size && !filters.l3.has(l3)) return false;
    return true;
  }

  function optionItems(level) {
    const seen = new Set();
    const out = [];
    for (const item of allItems) {
      const l1 = String(item.category_l1 || '').trim();
      const l2 = String(item.category_l2 || '').trim();
      const l3 = String(item.category_l3 || '').trim();
      if (level === 'l2' && filters.l1.size && !filters.l1.has(l1)) continue;
      if (level === 'l3') {
        if (filters.l1.size && !filters.l1.has(l1)) continue;
        if (filters.l2.size && !filters.l2.has(l2)) continue;
      }
      const value = level === 'l1' ? l1 : (level === 'l2' ? l2 : l3);
      if (!value || seen.has(value)) continue;
      seen.add(value);
      out.push(value);
    }
    return out;
  }

  function optionGroups(level) {
    if (level === 'l1') {
      return [{ key: 'l1', title: '', values: optionItems('l1') }];
    }

    const groups = [];
    const groupMap = new Map();
    for (const item of allItems) {
      const l1 = String(item.category_l1 || '').trim();
      const l2 = String(item.category_l2 || '').trim();
      const l3 = String(item.category_l3 || '').trim();
      if (level === 'l2') {
        if (filters.l1.size && !filters.l1.has(l1)) continue;
        if (!l2) continue;
        const key = l1 || '未分类';
        if (!groupMap.has(key)) {
          groupMap.set(key, { key, title: key, values: [], seen: new Set() });
          groups.push(groupMap.get(key));
        }
        const group = groupMap.get(key);
        if (!group.seen.has(l2)) {
          group.seen.add(l2);
          group.values.push(l2);
        }
      } else if (level === 'l3') {
        if (filters.l1.size && !filters.l1.has(l1)) continue;
        if (filters.l2.size && !filters.l2.has(l2)) continue;
        if (!l3) continue;
        const key = `${l1 || '未分类'}-${l2 || '未分二级'}`;
        const title = filters.l2.size ? (l2 || '未分二级') : `${l1 || '未分类'} / ${l2 || '未分二级'}`;
        if (!groupMap.has(key)) {
          groupMap.set(key, { key, title, values: [], seen: new Set() });
          groups.push(groupMap.get(key));
        }
        const group = groupMap.get(key);
        if (!group.seen.has(l3)) {
          group.seen.add(l3);
          group.values.push(l3);
        }
      }
    }
    return groups.map(({ key, title, values }) => ({ key, title, values }));
  }

  function pruneInvalidFilters() {
    const availableL2 = new Set(optionItems('l2'));
    const availableL3 = new Set(optionItems('l3'));
    for (const value of Array.from(filters.l2)) {
      if (!availableL2.has(value)) filters.l2.delete(value);
    }
    for (const value of Array.from(filters.l3)) {
      if (!availableL3.has(value)) filters.l3.delete(value);
    }
  }

  function renderFilterGroup(level, el, groups) {
    const selected = filters[level];
    const visibleGroups = groups.filter((group) => group.values.length);
    if (!visibleGroups.length) {
      el.innerHTML = '<span class="instrument-filter-empty">无可选项</span>';
      return;
    }
    el.innerHTML = visibleGroups.map((group) => {
      const options = group.values.map((value) => {
        const id = `inst-${level}-${group.key}-${value}`.replace(/[^\w\u4e00-\u9fa5-]/g, '-');
        const checked = selected.has(value) ? 'checked' : '';
        return `
          <label for="${esc(id)}">
            <input id="${esc(id)}" type="checkbox" data-level="${esc(level)}" value="${esc(value)}" ${checked}>
            <span>${esc(value)}</span>
          </label>
        `;
      }).join('');
      const title = group.title ? `<div class="instrument-filter-subtitle">${esc(group.title)}</div>` : '';
      return `<div class="instrument-filter-option-row">${title}<div class="instrument-filter-option-list">${options}</div></div>`;
    }).join('');
  }

  function renderFilters() {
    pruneInvalidFilters();
    renderFilterGroup('l1', filterEls.l1, optionGroups('l1'));
    renderFilterGroup('l2', filterEls.l2, optionGroups('l2'));
    renderFilterGroup('l3', filterEls.l3, optionGroups('l3'));
  }

  function categoryItemsFromList() {
    const rows = [];
    const seen = new Set();
    for (const item of allItems) {
      const l1 = String(item.category_l1 || '').trim();
      const l2 = String(item.category_l2 || '').trim();
      const l3 = String(item.category_l3 || '').trim();
      const candidates = [
        { path: l1, level: 1, name: l1, parent_path: '', priority: item.priority_l1 },
        { path: `${l1}-${l2}`, level: 2, name: l2, parent_path: l1, priority: item.priority_l2 },
        { path: `${l1}-${l2}-${l3}`, level: 3, name: l3, parent_path: `${l1}-${l2}`, priority: item.priority_l3 },
      ];
      for (const row of candidates) {
        if (!row.name || seen.has(row.path)) continue;
        seen.add(row.path);
        rows.push(row);
      }
    }
    return rows;
  }

  function categoryRows() {
    return Array.isArray(categoryItems) && categoryItems.length ? categoryItems : categoryItemsFromList();
  }

  function categoryChildren(level, parentPath = '') {
    return categoryRows()
      .filter((row) => Number(row.level || 0) === level && String(row.parent_path || '') === parentPath)
      .sort((a, b) => {
        const ap = Number(a.priority || 9999);
        const bp = Number(b.priority || 9999);
        if (ap !== bp) return ap - bp;
        return String(a.name || '').localeCompare(String(b.name || ''), 'zh-Hans-CN');
      });
  }

  function setSelectOptions(select, rows, placeholder) {
    if (!select) return;
    const current = select.value;
    const options = [`<option value="">${esc(placeholder)}</option>`].concat(
      rows.map((row) => `<option value="${esc(row.name)}" data-path="${esc(row.path)}">${esc(row.name)}</option>`)
    );
    select.innerHTML = options.join('');
    if (rows.some((row) => row.name === current)) select.value = current;
  }

  function selectedCategoryPath(selectEls, level) {
    const select = selectEls[level];
    if (!select) return '';
    const option = select.selectedOptions && select.selectedOptions[0];
    return option ? String(option.dataset.path || '').trim() : '';
  }

  function renderCategorySelects(selectEls, changedLevel, onRendered) {
    setSelectOptions(selectEls.l1, categoryChildren(1), '选择一级类目');
    if (changedLevel === 'l1') {
      selectEls.l2.value = '';
      selectEls.l3.value = '';
    }

    const l1Path = selectedCategoryPath(selectEls, 'l1');
    setSelectOptions(selectEls.l2, l1Path ? categoryChildren(2, l1Path) : [], '选择二级类目');
    if (changedLevel === 'l1' || changedLevel === 'l2') selectEls.l3.value = '';

    const l2Path = selectedCategoryPath(selectEls, 'l2');
    setSelectOptions(selectEls.l3, l2Path ? categoryChildren(3, l2Path) : [], '选择三级类目');
    if (typeof onRendered === 'function') onRendered();
  }

  function renderAddCategorySelects(changedLevel = '') {
    renderCategorySelects(addCategoryEls, changedLevel, updateAddConfirmState);
  }

  function renderEditCategorySelects(changedLevel = '') {
    renderCategorySelects(editCategoryEls, changedLevel, updateEditConfirmState);
  }

  async function loadCategories() {
    try {
      const resp = await fetch('/instruments/api/categories');
      const data = await resp.json();
      if (!resp.ok || !data.ok) throw new Error(data.detail || '分类加载失败');
      categoryItems = data.items || [];
    } catch (err) {
      categoryItems = categoryItemsFromList();
    }
    renderAddCategorySelects();
  }

  function safeAnchorId(l1, l2, index) {
    return TQ.safeAnchorId('inst-section', l1, l2, index);
  }

  function categorySections(items) {
    const out = [];
    let lastL1 = null;
    let lastL2 = null;
    for (const item of items) {
      const l1 = String(item.category_l1 || '').trim() || '未分类';
      const l2 = String(item.category_l2 || '').trim() || '未分二级';
      if (l1 === lastL1 && l2 === lastL2) {
        out[out.length - 1].count += 1;
        continue;
      }
      out.push({
        id: safeAnchorId(l1, l2, out.length + 1),
        l1,
        l2,
        count: 1,
      });
      lastL1 = l1;
      lastL2 = l2;
    }
    return out;
  }

  function setActiveSideNav(sectionId) {
    TQ.setActiveSideNav(sideNavEl, sectionId);
  }

  function renderSideNav(sections) {
    TQ.renderSideNav(sideNavEl, sections, (section) => `${Number(section.count || 0)}`);
  }

  const sectionSync = TQ.createSectionSync(sideNavEl, tableBody, 'tr.instrument-group-l2[id]');
  function requestActiveSync() {
    sectionSync.request();
  }


  function rowHtml(item) {
    const symbol = String(item.symbol || '').trim();
    const localStart = dateOnly(item.local_start_date);
    const localEnd = dateOnly(item.local_end_date);
    return `
      <tr
        data-symbol="${esc(symbol)}"
        data-local-start="${esc(localStart)}"
        data-local-end="${esc(localEnd)}"
      >
        <td class="instrument-symbol-cell">${esc(symbol)}</td>
        <td class="instrument-name-cell">${item.name
          ? `<a class="instrument-name-link" href="/market-view?symbol=${encodeURIComponent(symbol)}" title="查看 ${esc(symbol)} 行情详情">${esc(item.name)}</a>`
          : '-'}</td>
        <td class="instrument-category-cell">${esc(valueOrDash(item.category_l1))}</td>
        <td class="instrument-category-cell">${esc(valueOrDash(item.category_l2))}</td>
        <td class="instrument-category-cell">${esc(valueOrDash(item.category_l3))}</td>
        <td class="instrument-factor-cell">${esc(tagsText(item))}</td>
        <td class="instrument-date-cell" data-role="range">${esc(dateRangeText(localStart, localEnd))}</td>
        <td class="instrument-op-cell">
          ${String(item.category_l1 || '').trim() === 'ETF'
            ? '<button type="button" class="ghost-btn instrument-constituents-btn" data-role="constituents">权重股</button>'
            : ''}
          <button type="button" class="ghost-btn instrument-edit-btn" data-role="edit">编辑</button>
        </td>
      </tr>
    `;
  }

  function renderTable(items, sections) {
    if (!Array.isArray(items) || !items.length) {
      tableBody.innerHTML = '<tr><td colspan="8">没有符合条件的本地标的数据。</td></tr>';
      return;
    }

    const parts = [];
    let lastL1 = null;
    let lastL2 = null;
    let sectionIndex = 0;
    for (const item of items) {
      const l1 = String(item.category_l1 || '').trim() || '未分类';
      const l2 = String(item.category_l2 || '').trim() || '未分二级';
      if (l1 !== lastL1) {
        parts.push(`<tr class="instrument-group-l1"><td colspan="8">${esc(l1)}</td></tr>`);
        lastL1 = l1;
        lastL2 = null;
      }
      if (l2 !== lastL2) {
        const section = sections[sectionIndex] || { id: safeAnchorId(l1, l2, sectionIndex + 1), l1, l2 };
        sectionIndex += 1;
        parts.push(`<tr id="${esc(section.id)}" class="instrument-group-l2" data-l1="${esc(l1)}" data-l2="${esc(l2)}"><td colspan="8">${esc(l2)}</td></tr>`);
        lastL2 = l2;
      }
      parts.push(rowHtml(item));
    }
    tableBody.innerHTML = parts.join('');
  }

  function render() {
    renderFilters();
    const visibleItems = allItems.filter(itemMatchesFilters);
    const sections = categorySections(visibleItems);
    renderSideNav(sections);
    renderTable(visibleItems, sections);
    if (!isBulkRunning && !isAddRunning) {
      statusEl.textContent = `已加载 ${allItems.length} 个标的，当前显示 ${visibleItems.length} 个。`;
    }
  }

  async function loadList() {
    statusEl.textContent = '正在加载标的...';
    try {
      const resp = await fetch('/instruments/api/list');
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || '列表加载失败');
      allItems = data.items || [];
      render();
    } catch (err) {
      statusEl.textContent = `加载失败：${err.message}`;
      tableBody.innerHTML = '<tr><td colspan="8">数据加载失败。</td></tr>';
    }
  }

  function setBackfillControlsDisabled(disabled) {
    if (backfillAllBtn) backfillAllBtn.disabled = disabled || isAddRunning;
    syncTaskControls();
  }

  function summaryText(summary) {
    const total = Number(summary?.total || 0);
    const failed = Number(summary?.failed || 0);
    const upToDate = Number(summary?.up_to_date || 0);
    const noData = Number(summary?.no_data || 0);
    const addedRows = Number(summary?.added_rows || 0);
    return `共 ${total} 个标的，已最新 ${upToDate} 个，未获取到数据 ${noData} 个，失败 ${failed} 个，新增 ${addedRows} 行。`;
  }

  function dismissedCompletionJobId() {
    try {
      return window.localStorage.getItem(dismissedCompletionKey) || '';
    } catch (err) {
      return '';
    }
  }

  function dismissCompletion(jobId) {
    if (completionNoticeEl) completionNoticeEl.hidden = true;
    try {
      if (jobId) window.localStorage.setItem(dismissedCompletionKey, jobId);
    } catch (err) {
      // Ignore localStorage failures; dismissal only affects the current browser.
    }
  }

  function showCompletionNotice(job) {
    const jobId = String(job?.job_id || '');
    if (!completionNoticeEl || !completionTextEl || !jobId) return;
    if (dismissedCompletionJobId() === jobId) {
      completionNoticeEl.hidden = true;
      return;
    }
    currentCompletionJobId = jobId;
    const text = job.status === 'failed'
      ? `后台补齐任务失败：${job.error || '请查看日志。'}`
      : `后台补齐完成，${summaryText(job.summary || {})}`;
    completionTextEl.textContent = text;
    completionNoticeEl.hidden = false;
  }

  function showAddCompletionNotice(job) {
    const jobId = String(job?.job_id || '');
    if (!completionNoticeEl || !completionTextEl || !jobId) return;
    currentCompletionJobId = jobId;
    const summary = job.summary || {};
    const text = job.status === 'failed'
      ? `新增标的任务失败：${job.error || '请查看日志。'}`
      : `新增标的完成：${summary.symbol || ''} ${summary.name || ''}，新增 ${Number(summary.added_rows || 0)} 行。`;
    completionTextEl.textContent = text;
    completionNoticeEl.hidden = false;
  }

  function hideCompletionNotice() {
    if (completionNoticeEl) completionNoticeEl.hidden = true;
  }

  function setProgressVisible(visible) {
    if (progressWrapEl) progressWrapEl.hidden = !visible;
  }

  function setProgress(current, total) {
    const safeTotal = Math.max(Number(total || 0), 1);
    const safeCurrent = Math.max(0, Math.min(Number(current || 0), safeTotal));
    const ratio = safeCurrent / safeTotal;
    if (progressEl) progressEl.value = ratio;
    if (progressTextEl) {
      progressTextEl.textContent = `${(ratio * 100).toFixed(1)}% (${safeCurrent}/${safeTotal})`;
    }
  }

  function stopBulkPolling() {
    if (bulkPollTimer) window.clearInterval(bulkPollTimer);
    bulkPollTimer = null;
  }

  function stopAddPolling() {
    if (addPollTimer) window.clearInterval(addPollTimer);
    addPollTimer = null;
  }

  function startBulkPolling() {
    if (bulkPollTimer) return;
    bulkPollTimer = window.setInterval(() => {
      refreshBulkStatus({ silent: true });
    }, 2000);
  }

  function startAddPolling() {
    if (addPollTimer) return;
    addPollTimer = window.setInterval(() => {
      refreshAddStatus({ silent: true });
    }, 2000);
  }

  function applyBulkJob(job) {
    const status = String(job?.status || 'idle');
    const running = status === 'running';
    isBulkRunning = running;
    setBackfillControlsDisabled(running);

    if (backfillAllBtn) {
      backfillAllBtn.textContent = running ? '后台补齐中...' : '一键补齐当前列表至今日';
    }

    if (running) {
      currentBulkJobId = String(job.job_id || '');
      hideCompletionNotice();
      setProgressVisible(true);
      setProgress(job.progress_current, job.progress_total);
      statusEl.textContent = job.message || '后台补齐进行中...';
      startBulkPolling();
      return;
    }

    stopBulkPolling();
    if (!isAddRunning) setProgressVisible(false);

    if ((status === 'completed' || status === 'failed') && job?.job_id) {
      currentBulkJobId = String(job.job_id || '');
      currentCompletionJobId = currentBulkJobId;
      statusEl.textContent = job.message || (status === 'completed' ? '后台补齐完成。' : '后台补齐失败。');
      showCompletionNotice(job);
      if (status === 'completed' && completedJobRefreshDone !== currentBulkJobId) {
        completedJobRefreshDone = currentBulkJobId;
        loadList();
      }
    }
  }

  async function refreshBulkStatus(options = {}) {
    try {
      const resp = await fetch('/instruments/api/backfill-all/status');
      const data = await resp.json();
      if (!resp.ok || !data.ok) throw new Error(data.detail || '状态查询失败');
      applyBulkJob(data.job || {});
    } catch (err) {
      if (!options.silent) statusEl.textContent = `补齐状态查询失败：${err.message}`;
    }
  }

  function applyAddJob(job) {
    const status = String(job?.status || 'idle');
    const running = status === 'running';
    isAddRunning = running;
    syncTaskControls();

    if (running) {
      hideCompletionNotice();
      setProgressVisible(true);
      setProgress(job.progress_current, job.progress_total);
      statusEl.textContent = job.message || '新增标的任务进行中...';
      startAddPolling();
      return;
    }

    stopAddPolling();
    if (!isBulkRunning) setProgressVisible(false);

    if ((status === 'completed' || status === 'failed') && job?.job_id) {
      const jobId = String(job.job_id || '');
      statusEl.textContent = job.message || (status === 'completed' ? '新增标的完成。' : '新增标的失败。');
      showAddCompletionNotice(job);
      if (status === 'completed' && completedAddJobRefreshDone !== jobId) {
        completedAddJobRefreshDone = jobId;
        loadList();
        loadCategories();
      }
    }
  }

  async function refreshAddStatus(options = {}) {
    try {
      const resp = await fetch('/instruments/api/add/status');
      const data = await resp.json();
      if (!resp.ok || !data.ok) throw new Error(data.detail || '新增状态查询失败');
      applyAddJob(data.job || {});
    } catch (err) {
      if (!options.silent) statusEl.textContent = `新增状态查询失败：${err.message}`;
    }
  }

  function nextStartDateForRow(row) {
    return String(row.dataset.localEnd || row.dataset.localStart || '2020-01-01').trim();
  }

  function resetAddForm() {
    if (!addInstrumentForm) return;
    addInstrumentForm.reset();
    if (addInstrumentSymbolEl) addInstrumentSymbolEl.dataset.symbol = '';
    if (addInstrumentNameEl) {
      addInstrumentNameEl.dataset.loading = '0';
      addInstrumentNameEl.value = '';
    }
    setAddCategoryMode('manual');
    setAddMessage('');
    renderAddCategorySelects();
    updateAddConfirmState();
  }

  async function openAddModal() {
    resetAddForm();
    if (addInstrumentModal) addInstrumentModal.hidden = false;
    if (!categoryRows().length) await loadCategories();
    else renderAddCategorySelects();
    addInstrumentSymbolEl?.focus();
  }

  // 弹窗 a11y（P2-28）：Esc 关闭任一打开的弹窗
  document.addEventListener('keydown', function (event) {
    if (event.key !== 'Escape') return;
    if (addInstrumentModal && !addInstrumentModal.hidden) closeAddModal();
    if (editInstrumentModal && !editInstrumentModal.hidden) closeEditModal();
    if (etfConstituentsModal && !etfConstituentsModal.hidden) closeConstituentsModal();
  });

  function closeAddModal() {
    if (addInstrumentModal) addInstrumentModal.hidden = true;
  }

  function setEditMessage(text, isError = false) {
    if (!editInstrumentMsgEl) return;
    editInstrumentMsgEl.textContent = text || '';
    editInstrumentMsgEl.classList.toggle('is-error', Boolean(isError));
  }

  function updateEditConfirmState() {
    if (!editInstrumentConfirmBtn) return;
    const ready = Boolean(
      editingSymbol &&
      editCategoryEls.l1?.value &&
      editCategoryEls.l2?.value &&
      editCategoryEls.l3?.value
    );
    editInstrumentConfirmBtn.disabled = !ready || isEditSaving;
  }

  async function openEditModal(symbol) {
    const normalized = String(symbol || '').trim().toUpperCase();
    const item = allItems.find(
      (entry) => String(entry.symbol || '').trim().toUpperCase() === normalized
    );
    if (!item || !editInstrumentModal) return;
    editingSymbol = normalized;
    if (editInstrumentSymbolEl) editInstrumentSymbolEl.value = normalized;
    if (editInstrumentNameEl) editInstrumentNameEl.value = String(item.name || '');
    setEditMessage('');
    editInstrumentModal.hidden = false;

    if (!categoryRows().length) await loadCategories();
    renderEditCategorySelects();
    if (editCategoryEls.l1) editCategoryEls.l1.value = String(item.category_l1 || '').trim();
    const l1Path = selectedCategoryPath(editCategoryEls, 'l1');
    setSelectOptions(editCategoryEls.l2, l1Path ? categoryChildren(2, l1Path) : [], '选择二级类目');
    if (editCategoryEls.l2) editCategoryEls.l2.value = String(item.category_l2 || '').trim();
    const l2Path = selectedCategoryPath(editCategoryEls, 'l2');
    setSelectOptions(editCategoryEls.l3, l2Path ? categoryChildren(3, l2Path) : [], '选择三级类目');
    if (editCategoryEls.l3) editCategoryEls.l3.value = String(item.category_l3 || '').trim();
    updateEditConfirmState();
  }

  function closeEditModal() {
    if (editInstrumentModal) editInstrumentModal.hidden = true;
    editingSymbol = '';
  }

  async function runEditInstrument(event) {
    event.preventDefault();
    if (!editingSymbol || isEditSaving) return;
    const payload = {
      category_l1: String(editCategoryEls.l1?.value || '').trim(),
      category_l2: String(editCategoryEls.l2?.value || '').trim(),
      category_l3: String(editCategoryEls.l3?.value || '').trim(),
    };
    if (!payload.category_l1 || !payload.category_l2 || !payload.category_l3) {
      setEditMessage('一二三级类目均必选。', true);
      return;
    }

    isEditSaving = true;
    updateEditConfirmState();
    setEditMessage('正在保存...');
    try {
      const resp = await fetch(`/instruments/api/${encodeURIComponent(editingSymbol)}/update`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await resp.json();
      if (!resp.ok || !data.ok) throw new Error(data.detail || '保存失败');
      const savedSymbol = editingSymbol;
      const savedPath = String(data.category_path || '');
      closeEditModal();
      await loadList();
      loadCategories();
      statusEl.textContent = `已更新 ${savedSymbol} 的类目：${savedPath}。`;
    } catch (err) {
      setEditMessage(err.message, true);
    } finally {
      isEditSaving = false;
      updateEditConfirmState();
    }
  }

  function prefillAddCategorySelects(data) {
    // 尽力预填下拉（切手动模式时可直接改）；旧树过渡期申万类目对不上选项也无妨
    renderAddCategorySelects();
    if (addCategoryEls.l1) addCategoryEls.l1.value = String(data.category_l1 || '').trim();
    const l1Path = selectedCategoryPath(addCategoryEls, 'l1');
    setSelectOptions(addCategoryEls.l2, l1Path ? categoryChildren(2, l1Path) : [], '选择二级类目');
    if (addCategoryEls.l2) addCategoryEls.l2.value = String(data.category_l2 || '').trim();
    const l2Path = selectedCategoryPath(addCategoryEls, 'l2');
    setSelectOptions(addCategoryEls.l3, l2Path ? categoryChildren(3, l2Path) : [], '选择三级类目');
    if (addCategoryEls.l3) addCategoryEls.l3.value = String(data.category_l3 || '').trim();
  }

  async function suggestAddCategory(symbol, seq, recognizedName) {
    // 行业分类在本地表，不依赖名称查询成功（评审 A4）；建议失败静默，不打断添加流程
    try {
      const resp = await fetch(`/instruments/api/suggest-category/${encodeURIComponent(symbol)}`);
      const data = await resp.json();
      if (seq !== addLookupSeq) return;
      if (!resp.ok || !data.ok) return;
      prefillAddCategorySelects(data);
      const prefix = recognizedName ? `已识别：${recognizedName}，` : '';
      if (data.hit) {
        setAddCategoryMode('auto', data);
        setAddMessage(`${prefix}将按申万行业自动归类：${data.category_l2}-${data.category_l3}。`);
      } else {
        setAddCategoryMode('manual');
        setAddMessage(`${prefix}暂未识别行业：股票可选择「待分类」（后续自动回补），ETF 请手动选择类目。`);
      }
      updateAddConfirmState();
    } catch (err) {
      // 忽略建议失败
    }
  }

  async function lookupAddInstrumentName() {
    const rawSymbol = String(addInstrumentSymbolEl?.value || '').trim();
    const symbol = normalizeSymbolInput(rawSymbol);
    const seq = ++addLookupSeq;

    if (addInstrumentSymbolEl) addInstrumentSymbolEl.dataset.symbol = '';
    if (addInstrumentNameEl) {
      addInstrumentNameEl.dataset.loading = '0';
      addInstrumentNameEl.value = '';
    }
    updateAddConfirmState();

    if (!symbol) {
      setAddMessage('');
      return;
    }
    if (!/^\d{6}\.(SS|SZ|BJ)$/.test(symbol)) {
      setAddMessage('请输入 6 位标的代码，或完整代码如 518850.SH。', true);
      return;
    }
    if (knownSymbolSet().has(symbol)) {
      setAddMessage(`${symbol} 已被管理，不能重复添加。`, true);
      return;
    }

    setAddMessage(`正在查询 ${symbol} 名称...`);
    if (addInstrumentNameEl) {
      addInstrumentNameEl.dataset.loading = '1';
      addInstrumentNameEl.value = '查询中...';
    }
    try {
      const resp = await fetch('/instruments/api/lookup', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbol }),
      });
      const data = await resp.json();
      if (seq !== addLookupSeq) return;
      if (!resp.ok || !data.ok) throw new Error(data.detail || '名称查询失败');
      if (addInstrumentSymbolEl) {
        addInstrumentSymbolEl.dataset.symbol = data.symbol || symbol;
        addInstrumentSymbolEl.value = data.symbol || symbol;
      }
      if (addInstrumentNameEl) {
        addInstrumentNameEl.dataset.loading = '0';
        addInstrumentNameEl.value = data.name || '';
      }
      await suggestAddCategory(data.symbol || symbol, seq, data.name || '');
      if (seq !== addLookupSeq) return;
      if (!data.name) setAddMessage('未查询到名称，请检查代码。', true);
    } catch (err) {
      if (seq !== addLookupSeq) return;
      if (addInstrumentNameEl) {
        addInstrumentNameEl.dataset.loading = '0';
        addInstrumentNameEl.value = '';
      }
      setAddMessage(err.message, true);
      await suggestAddCategory(symbol, seq, '');
    } finally {
      updateAddConfirmState();
    }
  }

  function scheduleAddLookup() {
    if (addLookupTimer) window.clearTimeout(addLookupTimer);
    const symbol = normalizeSymbolInput(addInstrumentSymbolEl?.value || '');
    if (addInstrumentSymbolEl) addInstrumentSymbolEl.dataset.symbol = '';
    if (addInstrumentNameEl) {
      addInstrumentNameEl.dataset.loading = '0';
      addInstrumentNameEl.value = '';
    }
    setAddCategoryMode('manual');
    updateAddConfirmState();
    if (!symbol) {
      setAddMessage('');
      return;
    }
    addLookupTimer = window.setTimeout(lookupAddInstrumentName, symbol.length >= 9 ? 250 : 650);
  }

  async function runAddInstrument(event) {
    event.preventDefault();
    if (isBulkRunning || isAddRunning) return;
    const payload = addFormPayload();
    const hasCategory = addAutoCategory || (payload.category_l1 && payload.category_l2 && payload.category_l3);
    if (!payload.symbol || !payload.name || !hasCategory) {
      setAddMessage('请先完成代码识别和一二三级类目选择。', true);
      return;
    }
    if (knownSymbolSet().has(payload.symbol)) {
      setAddMessage(`${payload.symbol} 已被管理，不能重复添加。`, true);
      return;
    }

    isAddRunning = true;
    syncTaskControls();
    hideCompletionNotice();
    setProgressVisible(true);
    setProgress(0, 3);
    statusEl.textContent = `正在提交新增标的任务：${payload.symbol}。`;
    setAddMessage('任务已提交，后台正在补充数据。');

    try {
      const resp = await fetch('/instruments/api/add', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...payload, end_date: today }),
      });
      const data = await resp.json();
      if (!resp.ok || !data.ok) throw new Error(data.detail || '新增标的任务启动失败');
      closeAddModal();
      applyAddJob(data.job || {});
    } catch (err) {
      isAddRunning = false;
      syncTaskControls();
      setProgressVisible(false);
      statusEl.textContent = `新增标的启动失败：${err.message}`;
      setAddMessage(err.message, true);
    }
  }

  async function runBackfillAll() {
    if (isBulkRunning || isAddRunning) return;
    const rows = Array.from(tableBody.querySelectorAll('tr[data-symbol]'));
    if (!rows.length) {
      statusEl.textContent = '没有可补齐的标的。';
      return;
    }

    isBulkRunning = true;
    setBackfillControlsDisabled(true);
    hideCompletionNotice();
    setProgressVisible(true);
    setProgress(0, rows.length);
    statusEl.textContent = `正在提交后台补齐任务：当前列表 ${rows.length} 个标的至 ${today}。`;

    try {
      const items = rows.map((row) => ({
        symbol: String(row.dataset.symbol || '').trim(),
        start_date: nextStartDateForRow(row),
      })).filter((item) => item.symbol);
      const resp = await fetch('/instruments/api/backfill-all', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ items, end_date: today }),
      });
      const data = await resp.json();
      if (!resp.ok || !data.ok) throw new Error(data.detail || '后台补齐启动失败');
      applyBulkJob(data.job || {});
    } catch (err) {
      statusEl.textContent = `后台补齐启动失败：${err.message}`;
      isBulkRunning = false;
      setBackfillControlsDisabled(false);
      setProgressVisible(false);
    }
  }

  function setConstituentsMessage(text, isError = false) {
    if (!etfConstituentsMsg) return;
    etfConstituentsMsg.textContent = text;
    etfConstituentsMsg.style.color = isError ? '#b42318' : '';
  }

  function stopConstituentsPolling() {
    if (constituentsPollTimer) window.clearInterval(constituentsPollTimer);
    constituentsPollTimer = null;
  }

  function closeConstituentsModal() {
    // 导入任务在服务端后台运行，关闭弹窗不影响任务本身（状态页/列表稍后刷新即可）
    stopConstituentsPolling();
    isConstituentsImporting = false;
    if (etfConstituentsModal) etfConstituentsModal.hidden = true;
    constituentsEtfSymbol = '';
  }

  function renderConstituents(data, etfName) {
    if (etfConstituentsTitle) {
      etfConstituentsTitle.textContent = `${data.symbol || ''} ${etfName || ''} 前十大重仓股`;
    }
    if (etfConstituentsFreshness) {
      if (data.period) {
        etfConstituentsFreshness.textContent =
          `数据期次 ${data.period}（抓取于 ${String(data.fetched_at || '').slice(0, 10)}）`;
        etfConstituentsFreshness.style.color = data.stale ? '#b45309' : '#556';
      } else {
        etfConstituentsFreshness.textContent = '';
      }
    }
    const items = Array.isArray(data.items) ? data.items : [];
    if (etfConstituentsBody) {
      etfConstituentsBody.innerHTML = items.map((item) => {
        const manageable = item.manageable !== false;
        const managed = item.already_managed;
        const hit = item.hit;
        // A 股行展示申万行业（stock_industry 事实源），未识别标黄；
        // 非 A 股行（港股等）类目列展示市场名，状态固定不纳入管理
        const categoryText = manageable
          ? String(item.resolved_category || '')
          : String(item.market_label || '');
        const categoryStyle = manageable && !hit ? 'color:#b45309;' : '';
        const status = !manageable
          ? '<span style="color:#98a2b3;">不纳入管理</span>'
          : managed
            ? '<span style="color:#98a2b3;">已管理</span>'
            : '<span style="color:#067647;">待导入</span>';
        return `<tr>
          <td>${esc(item.rank)}</td>
          <td>${esc(item.stock_symbol)}</td>
          <td title="${esc(item.stock_name || '')}">${esc(item.stock_name || '-')}</td>
          <td>${item.weight == null ? '-' : Number(item.weight).toFixed(2)}</td>
          <td style="${categoryStyle}" title="${esc(categoryText)}">${esc(categoryText)}</td>
          <td>${status}</td>
        </tr>`;
      }).join('');
    }
    const importable = items.filter((item) => item.manageable !== false && !item.already_managed).length;
    if (etfConstituentsImportBtn) {
      etfConstituentsImportBtn.disabled = importable === 0;
      etfConstituentsImportBtn.textContent = importable
        ? `导入全部未管理标的（${importable} 只）`
        : '全部已在管理中';
    }
    return importable;
  }

  async function openConstituentsModal(symbol) {
    if (!etfConstituentsModal) return;
    constituentsEtfSymbol = String(symbol || '').trim();
    const item = allItems.find((entry) => entry.symbol === constituentsEtfSymbol);
    etfConstituentsModal.hidden = false;
    if (etfConstituentsTitle) etfConstituentsTitle.textContent = `${constituentsEtfSymbol} 前十大重仓股`;
    if (etfConstituentsFreshness) etfConstituentsFreshness.textContent = '';
    if (etfConstituentsBody) etfConstituentsBody.innerHTML = '';
    if (etfConstituentsImportBtn) {
      etfConstituentsImportBtn.disabled = true;
      etfConstituentsImportBtn.textContent = '导入全部未管理标的';
    }
    setConstituentsMessage('正在加载重仓股快照...');
    try {
      const resp = await fetch(`/instruments/api/etf-constituents/${encodeURIComponent(constituentsEtfSymbol)}`);
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || '加载失败');
      if (!data.ok) {
        setConstituentsMessage(data.message || '该 ETF 暂无重仓股快照。', true);
        return;
      }
      const importable = renderConstituents(data, item?.name || '');
      const pending = (data.items || []).filter((entry) => entry.manageable !== false && !entry.already_managed && !entry.hit).length;
      setConstituentsMessage(
        importable
          ? `待导入 ${importable} 只` + (pending ? `，其中 ${pending} 只暂未识别行业（导入后归入待分类，后续自动回补）` : '')
          : ''
      );
    } catch (err) {
      setConstituentsMessage(err.message, true);
    }
  }

  async function pollConstituentsImport() {
    try {
      const resp = await fetch('/instruments/api/etf-constituents/import/status');
      const data = await resp.json();
      if (!resp.ok || !data.ok) return;
      const job = data.job || {};
      if (job.status === 'running') {
        setConstituentsMessage(job.message || '导入中...');
        return;
      }
      if (job.status === 'completed' || job.status === 'failed') {
        stopConstituentsPolling();
        isConstituentsImporting = false;
        if (etfConstituentsImportBtn) etfConstituentsImportBtn.disabled = true;
        setConstituentsMessage(job.message || '', job.status === 'failed');
        statusEl.textContent = job.message || '';
        loadList();
      }
    } catch (err) {
      // 轮询失败下轮再试
    }
  }

  async function runConstituentsImport() {
    if (!constituentsEtfSymbol || isConstituentsImporting) return;
    isConstituentsImporting = true;
    if (etfConstituentsImportBtn) etfConstituentsImportBtn.disabled = true;
    setConstituentsMessage('正在提交导入任务...');
    try {
      const resp = await fetch('/instruments/api/etf-constituents/import', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ etf_symbol: constituentsEtfSymbol, end_date: today }),
      });
      const data = await resp.json();
      if (!resp.ok || !data.ok) throw new Error(data.detail || '导入任务启动失败');
      setConstituentsMessage('导入任务已启动，正在后台写入标的并回补行情 —— 可以安全关闭本窗口，稍后在标的管理页查看结果。');
      stopConstituentsPolling();
      constituentsPollTimer = window.setInterval(pollConstituentsImport, 2000);
      pollConstituentsImport();
    } catch (err) {
      isConstituentsImporting = false;
      if (etfConstituentsImportBtn) etfConstituentsImportBtn.disabled = false;
      setConstituentsMessage(err.message, true);
    }
  }

  tableBody.addEventListener('click', async (event) => {
    const target = event.target;
    if (!(target instanceof HTMLElement)) return;
    const role = target.getAttribute('data-role');
    const row = target.closest('tr');
    if (!row) return;
    if (role === 'edit') {
      openEditModal(row.dataset.symbol);
      return;
    }
    if (role === 'constituents') {
      openConstituentsModal(row.dataset.symbol);
      return;
    }
  });

  for (const el of Object.values(filterEls)) {
    el.addEventListener('change', (event) => {
      const target = event.target;
      if (!(target instanceof HTMLInputElement)) return;
      const level = target.dataset.level;
      if (!filters[level]) return;
      if (target.checked) filters[level].add(target.value);
      else filters[level].delete(target.value);
      render();
    });
  }

  for (const [level, el] of Object.entries(addCategoryEls)) {
    el?.addEventListener('change', () => renderAddCategorySelects(level));
  }

  for (const [level, el] of Object.entries(editCategoryEls)) {
    el?.addEventListener('change', () => renderEditCategorySelects(level));
  }

  clearFiltersBtn.addEventListener('click', () => {
    filters.l1.clear();
    filters.l2.clear();
    filters.l3.clear();
    render();
  });
  sideNavEl.addEventListener('click', (event) => {
    const target = event.target;
    if (!(target instanceof HTMLElement)) return;
    const btn = target.closest('[data-section-id]');
    if (!(btn instanceof HTMLElement)) return;
    const sectionId = btn.getAttribute('data-section-id') || '';
    const section = document.getElementById(sectionId);
    if (!section) return;
    setActiveSideNav(sectionId);
    section.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
  window.addEventListener('scroll', requestActiveSync, { passive: true });
  window.addEventListener('resize', requestActiveSync);
  backfillAllBtn?.addEventListener('click', runBackfillAll);
  addInstrumentBtn?.addEventListener('click', openAddModal);
  addInstrumentCloseBtn?.addEventListener('click', closeAddModal);
  addInstrumentCancelBtn?.addEventListener('click', closeAddModal);
  addInstrumentModal?.addEventListener('click', (event) => {
    if (event.target === addInstrumentModal) closeAddModal();
  });
  addInstrumentSymbolEl?.addEventListener('input', scheduleAddLookup);
  addInstrumentSymbolEl?.addEventListener('blur', () => {
    if (addLookupTimer) window.clearTimeout(addLookupTimer);
    addLookupTimer = null;
    lookupAddInstrumentName();
  });
  addInstrumentForm?.addEventListener('submit', runAddInstrument);
  editInstrumentCloseBtn?.addEventListener('click', closeEditModal);
  editInstrumentCancelBtn?.addEventListener('click', closeEditModal);
  editInstrumentModal?.addEventListener('click', (event) => {
    if (event.target === editInstrumentModal) closeEditModal();
  });
  editInstrumentForm?.addEventListener('submit', runEditInstrument);
  etfConstituentsCloseBtn?.addEventListener('click', closeConstituentsModal);
  etfConstituentsModal?.addEventListener('click', (event) => {
    if (event.target === etfConstituentsModal) closeConstituentsModal();
  });
  etfConstituentsImportBtn?.addEventListener('click', runConstituentsImport);
  addCategoryManualBtn?.addEventListener('click', () => setAddCategoryMode('manual'));
  completionCloseBtn?.addEventListener('click', () => dismissCompletion(currentCompletionJobId || currentBulkJobId));

  loadList();
  loadCategories();
  refreshBulkStatus({ silent: true });
  refreshAddStatus({ silent: true });
})();
