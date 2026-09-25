/* manual_trade.js — 手工交易页：试算 / 持仓记录 / 清仓交易（统计 + 热力图）。
 *
 * 2026-09-14 由 manual_trade.html 内联脚本抽出（与 market_view / subject_market /
 * batch_backtest 同构：模板只留 DOM 骨架，脚本独立成文件，便于缓存与复用）。
 * 本次改动：
 * - 持仓行 mini 日K图（近 20 根 + 买点 + 止损线；数据随列表接口一次下发，不逐标的拉行情）；
 * - 清仓统计卡片（总盈亏 / 胜率 / 清仓次数 / 平均持股天数 / 交易股票数 /
 *   平均盈利 / 平均亏损 / 最大盈利 / 最大亏损）；
 * - 清仓表「清仓后距今」列 = 现价 / 清仓价 − 1；
 * - 已清仓盈亏热力图（|盈亏金额| 降序取前 20，色块面积 ∝ |盈亏金额|）；
 * - 时间范围切换改为 近三个月 / 近六个月 / 今年来 / 自定义（持仓按买入日期、
 *   清仓按清仓日期各自过滤，统计与热力图随之变化）。
 */
(function() {
  // ── 元素 ────────────────────────────────────────────────
  const symbolEl = document.getElementById('mtSymbol');
  const symbolOptionsEl = document.getElementById('mtSymbolOptions');
  const buyDateEl = document.getElementById('mtBuyDate');
  const buyPriceEl = document.getElementById('mtBuyPrice');
  const riskBudgetEl = document.getElementById('mtRiskBudget');
  const evaluateBtn = document.getElementById('mtEvaluateBtn');
  const priceHintEl = document.getElementById('mtPriceHint');
  const messageEl = document.getElementById('mtMessage');
  const resultEl = document.getElementById('mtResult');
  const summaryTitleEl = document.getElementById('mtSummaryTitle');
  const stopStatsEl = document.getElementById('mtStopStats');
  const holdingStatsEl = document.getElementById('mtHoldingStats');
  const sizingPanelEl = document.getElementById('mtSizingPanel');

  const openEnterBtn = document.getElementById('mtOpenEnterBtn');
  const refreshBtn = document.getElementById('mtRefreshBtn');
  const tradeListEl = document.getElementById('mtTradeList');
  const tradeEmptyEl = document.getElementById('mtTradeEmpty');
  const stopModeToggleEl = document.getElementById('mtStopModeToggle');
  const closedListEl = document.getElementById('mtClosedList');
  const closedEmptyEl = document.getElementById('mtClosedEmpty');
  const closedStatsEl = document.getElementById('mtClosedStats');
  const heatPanelEl = document.getElementById('mtHeatPanel');
  const heatChartEl = document.getElementById('mtHeatChart');
  const heatNoteEl = document.getElementById('mtHeatNote');

  const enterModalEl = document.getElementById('mtEnterModal');
  const enterSymbolEl = document.getElementById('mtEnterSymbol');
  const enterDateEl = document.getElementById('mtEnterDate');
  const enterPriceEl = document.getElementById('mtEnterPrice');
  const enterSharesEl = document.getElementById('mtEnterShares');
  const enterConfirmBtn = document.getElementById('mtEnterConfirmBtn');
  const enterCancelBtn = document.getElementById('mtEnterCancelBtn');
  const enterHintEl = document.getElementById('mtEnterHint');
  const enterMessageEl = document.getElementById('mtEnterMessage');

  const closeModalEl = document.getElementById('mtCloseModal');
  const closeTitleEl = document.getElementById('mtCloseTitle');
  const sellDateEl = document.getElementById('mtSellDate');
  const sellPriceEl = document.getElementById('mtSellPrice');
  const closeConfirmBtn = document.getElementById('mtCloseConfirmBtn');
  const closeCancelBtn = document.getElementById('mtCloseCancelBtn');
  const closeHintEl = document.getElementById('mtCloseHint');
  const closeMessageEl = document.getElementById('mtCloseMessage');

  // ── 状态 ────────────────────────────────────────────────
  // 登录态由全站登录墙（session cookie）保证，页面不持有任何凭据
  let currentTrades = [];      // 最近一次列表数据
  let dayRange = null;         // 买入表单：当日价格区间缓存
  let rangeRequestSeq = 0;
  let closeCtx = null;         // 清仓弹窗当前交易
  let closeRange = null;       // 清仓弹窗：当日价格区间缓存
  let closeRangeSeq = 0;
  let enterRange = null;       // 录入弹窗：当日价格区间缓存
  let enterRangeSeq = 0;
  // 表格排序：dir 0=原始顺序，-1=降序，1=升序；点击同一列循环 降序→升序→原始
  // 持仓表与清仓表各自维护一份排序状态
  let sortState = {key: null, dir: 0};
  let closedSortState = {key: null, dir: 0};
  // 止损松紧档位：tight 紧止损（硬 1×ATR / 吊灯 2×ATR），loose 松止损（1.5×/2.5×ATR）
  // 默认紧止损，试算与交易记录共用
  let stopMode = 'tight';
  // 时间范围：持仓表按买入日期、清仓表（含统计与热力图）按清仓日期，各自独立
  let lastClosedTrades = [];   // 最近一次过滤后的清仓交易（供窗口尺寸变化时重排热力图）

  // ── 工具 ────────────────────────────────────────────────
  function showMessage(text, isError) {
    messageEl.textContent = text;
    messageEl.classList.toggle('is-error', !!isError);
    messageEl.hidden = !text;
  }

  function showCloseMessage(text, isError) {
    closeMessageEl.textContent = text;
    closeMessageEl.classList.toggle('is-error', !!isError);
    closeMessageEl.hidden = !text;
  }

  function showEnterMessage(text, isError) {
    enterMessageEl.textContent = text;
    enterMessageEl.classList.toggle('is-error', !!isError);
    enterMessageEl.hidden = !text;
  }

  function fmtMoney(v) {
    const n = Number(v);
    const sign = n > 0 ? '+' : (n < 0 ? '-' : '');
    return sign + '¥' + Math.abs(n).toFixed(2);
  }

  function fmtShares(v) {
    return String(Number(v));
  }

  function numOrNull(v) {
    const n = Number(v);
    return isFinite(n) ? n : null;
  }

  function todayStr() {
    return localIso(new Date());
  }

  function mean(values) {
    if (!values.length) return null;
    return values.reduce(function(a, b) { return a + b; }, 0) / values.length;
  }

  function clamp(v, lo, hi) {
    return Math.max(lo, Math.min(hi, v));
  }

  function renderStats(container, stats) {
    container.innerHTML = stats.map(function(stat) {
      const cls = ['mt-stat'];
      if (stat.primary) cls.push('mt-stat-primary');
      if (stat.cls === 'metric-positive') cls.push('mt-stat-pos');
      if (stat.cls === 'metric-negative') cls.push('mt-stat-neg');
      const valueCls = 'mt-stat-value' + (stat.cls ? ' ' + stat.cls : '');
      const valueHtml = '<span class="' + valueCls + '">' + esc(stat.value) + '</span>';
      const note = stat.note ? '<span class="mt-stat-note">' + esc(stat.note) + '</span>' : '';
      return '<div class="' + cls.join(' ') + '">' +
        '<span class="mt-stat-label">' + esc(stat.label) + '</span>' +
        (stat.tip ? withTip(valueHtml, stat.tip) : valueHtml) +
        note +
        '</div>';
    }).join('');
  }

  function rangeHintText(date, range) {
    return date + ' 价格区间：<b>' + esc(fmtPrice(range.low)) + ' ~ ' +
      esc(fmtPrice(range.high)) + '</b>（收盘 ' + esc(fmtPrice(range.close)) + '），价格需落在区间内';
  }

  // ── 时间范围（持仓 / 清仓两处共用同一实现，各自持有一份状态）──
  // 纯自然日口径（不考虑交易日数量与当月天数）：
  //   近三个月 = 三个月前同日至今，近六个月 = 六个月前同日至今，
  //   今年来 = 1 月 1 日至今，自定义 = 起止日期闭区间（留空表示不限）。
  const RANGE_LABELS = {'3m': '近三个月', '6m': '近六个月', ytd: '今年来', custom: '自定义'};

  function rangeStart(kind, from) {
    const now = new Date();
    if (kind === '3m') {
      const d = new Date(now);
      d.setMonth(d.getMonth() - 3);
      return localIso(d);
    }
    if (kind === '6m') {
      const d = new Date(now);
      d.setMonth(d.getMonth() - 6);
      return localIso(d);
    }
    if (kind === 'ytd') return now.getFullYear() + '-01-01';
    if (kind === 'custom') return from || null;
    return null;
  }

  // ISO 日期字符串字典序即时间序
  function createRangeFilter(segEl, customEl, fromEl, toEl, onChange) {
    const state = {kind: '3m', from: '', to: ''};

    function start() {
      return rangeStart(state.kind, state.from);
    }

    function end() {
      return state.kind === 'custom' ? (state.to || null) : null;
    }

    // 起始日 + 结束日（闭区间）判定的时间范围命中
    function pass(dateStr) {
      if (!dateStr) return false;
      const from = start();
      const to = end();
      return (!from || dateStr >= from) && (!to || dateStr <= to);
    }

    function syncCustom() {
      customEl.hidden = state.kind !== 'custom';
    }

    function label() {
      return RANGE_LABELS[state.kind];
    }

    segEl.addEventListener('click', function(ev) {
      const btn = ev.target.closest('button[data-range]');
      if (!btn || btn.dataset.range === state.kind) return;
      // 切到「自定义」时用上一个预设的起点 + 今天预填，避免出现空区间
      const presetStart = start();
      state.kind = btn.dataset.range;
      if (state.kind === 'custom' && !fromEl.value && !toEl.value) {
        const fallback = new Date();
        fallback.setFullYear(fallback.getFullYear() - 1);
        fromEl.value = presetStart || localIso(fallback);
        toEl.value = todayStr();
        state.from = fromEl.value;
        state.to = toEl.value;
      }
      segEl.querySelectorAll('button[data-range]').forEach(function(b) {
        b.classList.toggle('is-active', b.dataset.range === state.kind);
      });
      syncCustom();
      onChange();
    });

    [fromEl, toEl].forEach(function(el) {
      el.addEventListener('change', function() {
        state.from = fromEl.value;
        state.to = toEl.value;
        if (state.kind === 'custom') onChange();
      });
    });

    syncCustom();
    return {state: state, start: start, end: end, pass: pass, label: label};
  }

  // ── ① 试算区 ───────────────────────────────────────────
  async function loadSymbolOptions() {
    try {
      const resp = await fetch('/market-view/api/symbols');
      const data = await resp.json();
      if (!resp.ok) return;
      symbolOptionsEl.innerHTML = (data.items || []).map(function(item) {
        return '<option value="' + esc(item.symbol) + '">' + esc(item.display_name || item.name || '') + '</option>';
      }).join('');
    } catch (err) {
      // 代码提示加载失败不影响手动输入
    }
  }

  // 标的 + 买入日期填好后，拉取当日K线提示价格区间（后端仍会强制校验）
  async function refreshDayRange() {
    const symbol = symbolEl.value.trim();
    const buyDate = buyDateEl.value;
    dayRange = null;
    priceHintEl.hidden = true;
    if (!symbol || !buyDate) return;
    const seq = ++rangeRequestSeq;
    try {
      const candle = await fetchDayCandle(symbol, buyDate);
      if (seq !== rangeRequestSeq) return;  // 输入已变化，丢弃过期响应
      if (!candle) {
        priceHintEl.textContent = buyDate + ' 无K线数据（非交易日或未入库），买入价将按下一交易日口径计算';
        priceHintEl.hidden = false;
        return;
      }
      dayRange = {date: buyDate, low: candle.low, high: candle.high, close: candle.close};
      priceHintEl.innerHTML = rangeHintText(buyDate, dayRange);
      priceHintEl.hidden = false;
    } catch (err) {
      if (seq !== rangeRequestSeq) return;
      priceHintEl.textContent = err.message || '无法获取当日行情';
      priceHintEl.hidden = false;
    }
  }

  let rangeDebounce = null;
  function scheduleRangeRefresh() {
    clearTimeout(rangeDebounce);
    rangeDebounce = setTimeout(refreshDayRange, 350);
  }

  function readBuyForm() {
    const symbol = symbolEl.value.trim();
    const buyDate = buyDateEl.value;
    const buyPrice = parseFloat(buyPriceEl.value);
    const riskBudget = parseFloat(riskBudgetEl.value);
    if (!symbol) { showMessage('请输入标的代码', true); return null; }
    if (!buyDate) { showMessage('请选择买入日期', true); return null; }
    if (!(buyPrice > 0)) { showMessage('请输入大于 0 的买入价格', true); return null; }
    if (riskBudgetEl.value.trim() && !(riskBudget > 0)) {
      showMessage('风险预算需为大于 0 的金额（选填）', true);
      return null;
    }
    if (dayRange && dayRange.date === buyDate &&
        (buyPrice < dayRange.low || buyPrice > dayRange.high)) {
      showMessage('买入价格 ' + fmtPrice(buyPrice) + ' 超出 ' + buyDate + ' 当日价格区间 [' +
        fmtPrice(dayRange.low) + ', ' + fmtPrice(dayRange.high) + ']', true);
      return null;
    }
    const form = {symbol: symbol, buy_date: buyDate, buy_price: buyPrice};
    if (riskBudget > 0) form.risk_budget = riskBudget;
    return form;
  }

  async function evaluate() {
    const form = readBuyForm();
    if (!form) return;

    evaluateBtn.disabled = true;
    showMessage('计算中…', false);
    try {
      const data = await postJson('/manual-trade/api/evaluate',
        Object.assign({}, form, {stop_mode: stopMode}));
      renderResult(data);
      showMessage('', false);
    } catch (err) {
      resultEl.hidden = true;
      showMessage(err.message || '计算失败', true);
    } finally {
      evaluateBtn.disabled = false;
    }
  }

  // 最大可买入份数 = 风险预算 ÷ 每股风险（买入价 − 硬止损价），下取整到百位
  function positionSizingTip(data) {
    const ps = data.position_sizing || {};
    const stops = data.stops || {};
    if (ps.risk_per_share == null) return '';
    return '每股风险 = 买入价 ' + fmtPrice(data.buy_price) + ' − 硬止损价 ' +
      fmtPrice(stops.hard_stop_price) + ' = ' + fmtPrice(ps.risk_per_share) +
      '；可买份数 = 风险预算 ¥' + Number(ps.risk_budget).toFixed(2) + ' ÷ 每股风险 ' +
      fmtPrice(ps.risk_per_share) + '，下取整到百位 = ' + fmtShares(ps.max_qty) +
      ' 份；若硬止损触发亏损约 ¥' + Number(ps.max_loss).toFixed(2) +
      '，按买入价计持仓约 ¥' + Number(ps.position_value).toFixed(2);
  }

  // 仓位试算单行展示：最大可买入份数 + 最大可买入金额（份数 × 买入价），推算明细在悬停提示
  function renderSizingLine(data) {
    const ps = data.position_sizing;
    if (!ps) {
      sizingPanelEl.hidden = true;
      holdingStatsEl.innerHTML = '';
      return;
    }
    const html = '最大可买入份数 <b>' + esc(fmtShares(ps.max_qty)) + ' 份</b>' +
      '<span class="mt-stat-note">（风险预算 ¥' + Number(ps.risk_budget).toFixed(2) + '）</span>' +
      '　｜　最大可买入金额 <b>¥' + Number(ps.position_value).toFixed(2) + '</b>' +
      '<span class="mt-stat-note">＝ ' + esc(fmtShares(ps.max_qty)) + ' 份 × 买入价 ' +
      esc(fmtPrice(data.buy_price)) + '</span>';
    holdingStatsEl.innerHTML = withTip(html, positionSizingTip(data));
    sizingPanelEl.hidden = false;
  }

  function renderResult(data) {
    const stops = data.stops || {};
    const title = (data.name ? data.name + ' ' : '') + data.symbol +
      ' · ' + data.buy_date + ' 买入 @ ' + fmtPrice(data.buy_price);
    const intradayTime = data.intraday_ts ? String(data.intraday_ts).slice(11, 16) : '';
    summaryTitleEl.textContent = '止损价 — ' + title +
      (data.is_intraday ? '（含今日盘中实时数据 ' + intradayTime + '）' : '');

    renderStopStats(stopStatsEl, stops);
    renderSizingLine(data);

    resultEl.hidden = false;
  }

  // ── ② 交易记录 ─────────────────────────────────────────
  async function loadTrades() {
    refreshBtn.disabled = true;
    try {
      const data = await postJson('/manual-trade/api/trades/list', {stop_mode: stopMode});
      currentTrades = data.trades || [];
      renderTradeList();
      renderClosedList();
      renderClosedStats();
      renderHeatmap(lastClosedTrades);
    } catch (err) {
      showMessage(err.message || '交易记录加载失败', true);
    } finally {
      refreshBtn.disabled = false;
    }
  }

  // 统一表格展示：各值的格式与原卡片保持一致，持有天数挪到外层后无需再展开详情
  const TRADE_TABLE_HEADERS = ['标的', '标的代码', '日K', '买入日期', '买价', '现价', '仓位占比', '涨跌幅', '份数', '持仓金额',
    '盈亏', '最大浮盈', '最大回撤', '硬止损价', '吊灯止损价', '风险', '持有天数', '操作'];

  // 清仓交易表：体现一笔完整交易的结果（收益金额与收益率分列），不展示现价/持仓金额/止损价/操作
  const CLOSED_TABLE_HEADERS = ['标的', '标的代码', '买入日期', '卖出日期', '买价', '卖价', '份数',
    '买入金额', '收益', '收益率', '清仓后距今', '最大浮盈', '最大回撤', '持有天数'];

  // 非排序列的表头悬停说明（可排序列的说明会拼在排序提示前面）
  const HEADER_TIPS = {
    '日K': 'mini 日K图：默认最近 20 根；买入更早时从买入那一根起（最多 120 根）。'
      + '紫色圆点为买点，灰色虚线为当前止损线（硬止损 / 吊灯止损取较高者）',
    '清仓后距今': '现价 / 清仓价 − 1：现价取最新可得价格（盘中为实时价），用于对照清仓后的走势',
  };
  const OPEN_ERROR_COLSPAN = TRADE_TABLE_HEADERS.length - 6;   // 前 5 列 + 末 1 列操作
  const CLOSED_ERROR_COLSPAN = CLOSED_TABLE_HEADERS.length - 7; // 前 7 列

  // 可排序列 → 排序键取值；与单元格展示口径一致，
  // 无法取值的行（计算失败）恒排在最后。盈亏按绝对金额而非百分比比较。
  const SORTABLE_COLUMNS = {
    '买入日期': function(t) { return t.buy_date || null; },  // ISO 日期字符串，字典序即时间序，越新越大
    '买价': function(t) { return numOrNull(t.buy_price); },
    '现价': function(t) {
      if (t.error) return null;
      return numOrNull(t.latest_price);
    },
    // _weight 由 renderTradeList 在排序前按「持仓金额 / 全部持仓总资产」预计算
    '仓位占比': function(t) {
      if (t.error) return null;
      return numOrNull(t._weight);
    },
    '涨跌幅': function(t) {
      if (t.error) return null;
      return numOrNull(t.daily_change_pct);
    },
    '持仓金额': function(t) {
      if (t.error) return null;
      return numOrNull(t.position_value);
    },
    '盈亏': function(t) {
      if (t.error) return null;
      return numOrNull(t.pnl_amount);
    },
    '持有天数': function(t) {
      if (t.error) return null;
      return numOrNull((t.holding || {}).hold_days);
    },
    // 风险 = (买价 − 较高止损价) × 份数，见 riskAmount
    '风险': function(t) { return riskAmount(t); },
  };

  const CLOSED_SORTABLE_COLUMNS = {
    '买入日期': function(t) { return t.buy_date || null; },
    '卖出日期': function(t) { return t.sell_date || null; },
    '买价': function(t) { return numOrNull(t.buy_price); },
    '卖价': function(t) { return numOrNull(t.sell_price); },
    '买入金额': function(t) {
      if (t.error) return null;
      return numOrNull(Number(t.buy_price) * Number(t.shares));
    },
    '收益': function(t) {
      if (t.error) return null;
      return numOrNull(t.realized_pnl);
    },
    '收益率': function(t) {
      if (t.error) return null;
      return numOrNull(t.realized_pnl_pct);
    },
    '清仓后距今': function(t) {
      if (t.error) return null;
      return numOrNull(t.since_close_pct);
    },
    '持有天数': function(t) {
      if (t.error) return null;
      return numOrNull((t.holding || {}).hold_days);
    },
  };

  function tradeHeaderHtml(h, state, columns) {
    if (!columns.hasOwnProperty(h)) {
      return '<th' + (HEADER_TIPS[h] ? ' title="' + esc(HEADER_TIPS[h]) + '"' : '') +
        (HEADER_TIPS[h] ? ' class="mt-th-tip"' : '') + '>' + esc(h) + '</th>';
    }
    const active = state.key === h && state.dir !== 0;
    const arrow = active ? (state.dir === -1 ? '⬇️' : '⬆️') : '';
    // 可排序列：点击排序说明在前，列口径说明（如有）在后
    const tip = (HEADER_TIPS[h] ? HEADER_TIPS[h] + '。' : '') + '点击排序：降序 → 升序 → 原始顺序';
    return '<th class="mt-th-sortable' + (active ? ' is-sorted' : '') + '" data-sort-key="' + esc(h) +
      '" title="' + esc(tip) + '">' + esc(h) +
      '<span class="mt-sort-arrow">' + arrow + '</span></th>';
  }

  // 止损价单元格：329.06（+20.35%）；两个止损价中较高者更紧、会先触发，价格加下划线提示以其为准。
  // 悬停展示该价格的计算过程（数据来自当前松紧口径的后端响应，切换后自动更新）。
  function stopCellHtml(price, distPct, dominant, tip) {
    if (price == null || !isFinite(Number(price))) return '—';
    const pctHtml = '<span class="' + pctClass(distPct) + '">' + esc(fmtPct(distPct)) + '</span>';
    const priceHtml = dominant
      ? '<span class="mt-stop-dominant">' + esc(fmtPrice(price)) + '</span>'
      : esc(fmtPrice(price));
    return '<span class="mt-stop-cell"' + (tip ? ' title="' + esc(tip) + '"' : '') + '>' +
      priceHtml + '（' + pctHtml + '）</span>';
  }

  // ── 持仓指标悬停明细：展示每个数字的计算过程（数据均来自后端响应） ──

  // 持有交易日数：日期区间 + 是否含盘中实时
  function holdDaysTip(t, holding) {
    if (holding.hold_days == null) return '';
    const end = t.status === 'closed' ? t.sell_date : t.latest_date;
    let tip = (t.buy_date || '?') + ' ~ ' + (end || '?') +
      ' 共 ' + holding.hold_days + ' 个交易日';
    if (t.is_intraday && t.intraday_ts) {
      tip += '（含今日盘中实时数据 ' + String(t.intraday_ts).slice(11, 16) + '）';
    }
    return tip;
  }

  // 最大浮盈 = 买入以来最高价 / 买入价 − 1
  function maxGainTip(t, holding) {
    if (holding.max_gain_pct == null || holding.highest_since_buy == null) return '';
    return '最大浮盈 = 买入以来最高价 ' + fmtPrice(holding.highest_since_buy) +
      (holding.highest_since_buy_date ? '（' + holding.highest_since_buy_date + '）' : '') +
      ' / 买入价 ' + fmtPrice(t.buy_price) + ' − 1 = ' + fmtPct(holding.max_gain_pct);
  }

  // 最大回撤 = 持有期净值谷底 / 净值峰值 − 1（净值为买入价=1.0 口径，括注换算价格）
  function maxDrawdownTip(t, holding) {
    if (holding.max_drawdown == null) return '';
    if (!holding.max_dd_peak_date) return '持有期净值未出现回撤';
    const buyPrice = Number(t.buy_price);
    const priceNote = function(equity) {
      return isFinite(buyPrice) && buyPrice > 0 ? '，约合价格 ' + fmtPrice(equity * buyPrice) : '';
    };
    return '最大回撤 = 净值谷底 ' + Number(holding.max_dd_trough_equity).toFixed(4) +
      '（' + holding.max_dd_trough_date + priceNote(Number(holding.max_dd_trough_equity)) + '）' +
      ' / 净值峰值 ' + Number(holding.max_dd_peak_equity).toFixed(4) +
      '（' + holding.max_dd_peak_date + priceNote(Number(holding.max_dd_peak_equity)) + '）' +
      ' − 1 = ' + fmtPct(holding.max_drawdown);
  }

  // 当日涨跌幅 = 最新价 / 前一交易日收盘 − 1（盘中最新价为实时价）
  function dailyChangeTip(t) {
    if (t.daily_change_pct == null || t.prev_close == null || t.latest_price == null) return '';
    return '当日涨跌幅 = 最新价 ' + fmtPrice(t.latest_price) + ' / 前一交易日收盘 ' +
      fmtPrice(t.prev_close) + ' − 1 = ' + fmtPct(t.daily_change_pct) +
      (t.is_intraday && t.intraday_ts ? '（最新价为盘中实时价 ' + String(t.intraday_ts).slice(11, 16) + '）' : '');
  }

  // 浮动盈亏 = (现价 − 买价) × 份数；收益率 = 现价 / 买价 − 1
  function pnlTip(t, holding) {
    if (holding.pnl_pct == null || t.latest_price == null) return '';
    return '盈亏 = (现价 ' + fmtPrice(t.latest_price) + ' − 买价 ' + fmtPrice(t.buy_price) +
      ') × 份数 ' + fmtShares(t.shares) + ' = ' + fmtMoney(t.pnl_amount) +
      '；现价 / 买价 − 1 = ' + fmtPct(holding.pnl_pct);
  }

  // 持仓金额 = 现价 × 份数
  function positionValueTip(t) {
    if (t.position_value == null || t.latest_price == null) return '';
    return '持仓金额 = 现价 ' + fmtPrice(t.latest_price) + ' × 份数 ' + fmtShares(t.shares) +
      ' = ¥' + Number(t.position_value).toFixed(2);
  }

  // 买入金额 = 买价 × 份数
  function buyAmountTip(t) {
    if (t.buy_price == null || t.shares == null) return '';
    return '买入金额 = 买价 ' + fmtPrice(t.buy_price) + ' × 份数 ' + fmtShares(t.shares) +
      ' = ¥' + (Number(t.buy_price) * Number(t.shares)).toFixed(2);
  }

  // 实际盈亏 = (卖价 − 买价) × 份数；收益率 = 卖价 / 买价 − 1（未计交易费用）
  function realizedPnlTip(t) {
    if (t.realized_pnl == null || t.sell_price == null) return '';
    return '实际盈亏 = (卖价 ' + fmtPrice(t.sell_price) + ' − 买价 ' + fmtPrice(t.buy_price) +
      ') × 份数 ' + fmtShares(t.shares) + ' = ' + fmtMoney(t.realized_pnl) +
      '；收益率 = 卖价 / 买价 − 1 = ' + fmtPct(t.realized_pnl_pct) + '（未计交易费用）';
  }

  // 清仓后距今 = 现价 / 清仓价 − 1（现价为最新可得价格，盘中即实时价）
  function sinceCloseTip(t) {
    if (t.since_close_pct == null || t.latest_price == null) return '';
    return '清仓后距今 = 现价 ' + fmtPrice(t.latest_price) + ' / 清仓价 ' + fmtPrice(t.sell_price) +
      ' − 1 = ' + fmtPct(t.since_close_pct) +
      '（' + (t.sell_date || '?') + ' 清仓，现价取 ' + (t.latest_date || '?') + '）';
  }

  function holdDaysCell(holding) {
    return holding.hold_days != null ? esc(String(holding.hold_days)) : '—';
  }

  // 止损徽章：区分"已击穿"（现价仍在止损价下方，红色）与"曾击穿"（历史触发过但已收复，琥珀色）。
  // 悬停提示展示首次击穿当日的价格明细（后端随 stops 下发）。
  function stopBadge(text, level, tip) {
    return '<span class="mt-badge ' + (level === 'danger' ? 'mt-badge-danger' : 'mt-badge-warn') + '"' +
      (tip ? ' title="' + esc(tip) + '"' : '') + '>' + esc(text) + '</span>';
  }

  function hardTriggerTip(stops) {
    if (!stops.hard_stop_trigger_date) return '';
    let tip = '最早于 ' + stops.hard_stop_trigger_date + ' 击穿硬止损';
    if (stops.hard_stop_trigger_low != null) {
      tip += '：当日最低 ' + fmtPrice(stops.hard_stop_trigger_low) +
        (stops.hard_stop_trigger_close != null ? '、收盘 ' + fmtPrice(stops.hard_stop_trigger_close) : '') +
        '，硬止损价 ' + fmtPrice(stops.hard_stop_price);
    }
    return tip;
  }

  function chandelierTriggerTip(stops) {
    if (!stops.chandelier_first_trigger_date) return '';
    let tip = '最早于 ' + stops.chandelier_first_trigger_date + ' 跌破吊灯止损';
    if (stops.chandelier_first_trigger_close != null) {
      tip += '：当日收盘 ' + fmtPrice(stops.chandelier_first_trigger_close) +
        '，吊灯止损价(当日口径) ' + fmtPrice(stops.chandelier_first_trigger_stop);
    }
    return tip;
  }

  function stopBadges(t) {
    const stops = t.stops;
    if (!stops) return [];
    const badges = [];
    const latest = Number(stops.latest_price);
    const hardPrice = Number(stops.hard_stop_price);
    if (isFinite(latest) && isFinite(hardPrice) && latest <= hardPrice) {
      badges.push(stopBadge('⚠ 硬止损已击穿', 'danger', hardTriggerTip(stops)));
    } else if (stops.hard_stop_triggered) {
      badges.push(stopBadge('⚠ 硬止损曾击穿', 'warn', hardTriggerTip(stops)));
    }
    if (stops.chandelier_stop_triggered) {
      badges.push(stopBadge('⚠ 吊灯已跌破', 'danger', chandelierTriggerTip(stops)));
    } else if (stops.chandelier_first_trigger_date) {
      badges.push(stopBadge('⚠ 吊灯曾跌破', 'warn', chandelierTriggerTip(stops)));
    }
    return badges;
  }

  // 仓位占比 = 该标的持仓金额 / 全部持仓总资产（悬停展示计算过程）
  function positionWeightTip(t, totalValue) {
    if (t._weight == null || !(totalValue > 0)) return '';
    return '仓位占比 = 持仓金额 ¥' + Number(t.position_value).toFixed(2) +
      ' / 全部持仓总资产 ¥' + totalValue.toFixed(2) +
      ' = ' + Number(t._weight).toFixed(2) + '%';
  }

  // 较高止损价：硬止损 / 吊灯止损取较高者（更紧、先触发），用于风险口径
  function higherStopPrice(t) {
    const stops = t.stops || {};
    const hard = numOrNull(stops.hard_stop_price);
    const chand = numOrNull(stops.chandelier_stop_price);
    if (hard != null && chand != null) return Math.max(hard, chand);
    return hard != null ? hard : chand;
  }

  // 风险 = 以较高止损价离场时的亏损金额 = (买价 − 止损价) × 份数；
  // 止损价 ≥ 买价时止损离场不亏损，风险记 0；止损价或份数缺失时无法计算
  function riskAmount(t) {
    if (t.error) return null;
    const stop = higherStopPrice(t);
    const buy = numOrNull(t.buy_price);
    const shares = numOrNull(t.shares);
    if (stop == null || buy == null || shares == null) return null;
    return Math.max(0, (buy - stop) * shares);
  }

  // 风险悬停：展示取较高止损价的过程与亏损金额计算
  function riskTip(t) {
    const risk = riskAmount(t);
    if (risk == null) return '';
    const stops = t.stops || {};
    const stop = higherStopPrice(t);
    const hard = numOrNull(stops.hard_stop_price);
    const chand = numOrNull(stops.chandelier_stop_price);
    let stopNote = '止损价取较高者 = max(硬止损价 ' + (hard != null ? fmtPrice(hard) : '—') +
      ', 吊灯止损价 ' + (chand != null ? fmtPrice(chand) : '—') + ') = ' + fmtPrice(stop);
    if (stop >= Number(t.buy_price)) {
      return stopNote + '；止损价不低于买价 ' + fmtPrice(t.buy_price) + '，止损离场不亏损，风险为 0';
    }
    return '风险 = (买价 ' + fmtPrice(t.buy_price) + ' − 止损价 ' + fmtPrice(stop) +
      ') × 份数 ' + fmtShares(t.shares) + ' = ¥' + risk.toFixed(2) + '（' + stopNote + '）';
  }

  // ── 持仓 mini 日K图（买点 + 止损线）────────────────────
  // 每行一个 ECharts 实例（轻量 20 根蜡烛，无坐标轴文字），数据来自列表接口的
  // mini_chart 字段。重渲染前统一 dispose，避免排序/筛选反复重建累积实例。
  const miniCharts = {};

  function disposeMiniCharts() {
    Object.keys(miniCharts).forEach(function(id) {
      try {
        miniCharts[id].dispose();
      } catch (err) {
        // 实例已销毁 / 容器已移除：忽略
      }
      delete miniCharts[id];
    });
  }

  function miniOption(t, containerEl) {
    const mini = t.mini_chart;
    const candles = mini.candles || [];
    const stop = higherStopPrice(t);
    const buy = mini.buy || {};
    const highs = candles.map(function(c) { return c[3]; });
    const lows = candles.map(function(c) { return c[2]; });
    const values = highs.concat(lows);
    if (buy.price != null) values.push(Number(buy.price));
    if (stop != null) values.push(Number(stop));
    let vMin = Math.min.apply(null, values);
    let vMax = Math.max.apply(null, values);
    if (!isFinite(vMin) || !isFinite(vMax)) { vMin = 0; vMax = 1; }
    if (vMax <= vMin) vMax = vMin + 1e-6;
    const pad = (vMax - vMin) * 0.08;
    vMin -= pad;
    vMax += pad;

    const series = [{
      type: 'candlestick',
      data: candles,
      itemStyle: {color: '#c13f3a', color0: '#187b5f', borderColor: '#c13f3a', borderColor0: '#187b5f'},
      barMaxWidth: 7,
      z: 3,
    }];
    // 买点：只用一个紫色圆点锚在 [该根K线, 买入价]，不写价格文字 ——
    // 图上文字会压住K线（用户 2026-09-14 反馈），价格看「买价」列或悬停该根时的
    // 「★ 买入 …」一行。白描边 + 阴影保证压在红绿蜡烛上也能一眼认出。
    if (buy.index != null && buy.price != null) {
      series.push({
        name: '买点',
        type: 'scatter',
        data: [[buy.index, Number(buy.price)]],
        symbol: 'circle',
        symbolSize: 8,
        itemStyle: {
          color: '#c026d3',
          borderColor: '#ffffff',
          borderWidth: 1.5,
          shadowBlur: 5,
          shadowColor: 'rgba(17,35,42,0.35)',
        },
        z: 6,
      });
    } else if (buy.price != null) {
      // 买入那一根已滑出窗口（超长持有）：退化为一条紫色点线（与买点圆点同色，
      // 全图唯一的紫色元素即买点位置）。同样不写价格文字，口径见悬停提示末行。
      series.push({
        name: '买入价',
        type: 'scatter',
        data: [],
        markLine: {
          symbol: 'none',
          silent: true,
          animation: false,
          lineStyle: {color: '#c026d3', width: 1.2, type: 'dotted'},
          label: {show: false},
          data: [{yAxis: Number(buy.price)}],
        },
        z: 5,
      });
    }
    // 止损线：硬止损 / 吊灯止损取较高者（更紧、先触发），与风险列同口径；
    // 深灰虚线 + 实心底药丸标签，压在蜡烛上也读得清
    if (stop != null) {
      series.push({
        name: '止损线',
        type: 'scatter',
        data: [],
        markLine: {
          symbol: 'none',
          silent: true,
          animation: false,
          lineStyle: {color: '#4b5563', width: 1.3, type: 'dashed'},
          label: {
            show: true,
            position: 'insideStartTop',
            distance: 4,
            formatter: '止损 ' + fmtPrice(stop),
            color: '#ffffff',
            fontWeight: 700,
            fontSize: 10,
            backgroundColor: '#4b5563',
            borderRadius: 3,
            padding: [1, 5],
          },
          data: [{yAxis: Number(stop)}],
        },
        z: 5,
      });
    }
    return {
      animation: false,
      grid: {left: 2, right: 2, top: 12, bottom: 6},
      tooltip: {
        trigger: 'axis',
        // 图只有 180×54：提示框留在图内会整块遮住K线，也不该被表格的
        // overflow-x:clip 裁掉 —— 挂到 body 上按「图外浮层」定位（同
        // 标的看板 mini 图的思路），位置见 miniTooltipPos。
        appendToBody: true,
        confine: false,
        position: miniTooltipPos(containerEl),
        extraCssText: 'pointer-events:none;z-index:99;',
        backgroundColor: 'rgba(255,255,255,0.97)',
        borderColor: 'rgba(20,41,51,0.12)',
        textStyle: {color: '#17232b', fontSize: 12},
        formatter: miniTooltipHtml(mini, t),
      },
      xAxis: {
        type: 'category',
        data: mini.dates,
        boundaryGap: true,
        axisLine: {show: false},
        axisTick: {show: false},
        axisLabel: {show: false},
        splitLine: {show: false},
      },
      yAxis: {
        type: 'value',
        min: vMin,
        max: vMax,
        axisLine: {show: false},
        axisTick: {show: false},
        axisLabel: {show: false},
        splitLine: {show: false},
      },
      series: series,
    };
  }

  // mini 图悬停提示定位：贴在被悬停那一行 mini 图的下方（图只有 54px 高，
  // 压在图内就什么也看不见了）；窗口底部放不下时翻到图上方，左右贴边收进视口。
  // rect / contentSize 由 ECharts 传入：rect 是容器视口矩形，appendToBody 下
  // 返回值需为文档坐标（视口坐标 + 滚动量）。
  // mini 图悬停提示定位：图只有 180×54，提示框压在图内就什么也看不见了，
  // 所以固定贴到图的**外侧**（优先下方，下方放不下且上方更宽裕时翻到上方）。
  // 坐标契约（ECharts 5 实测）：position 回调的返回值与入参 point 同为**容器局部
  // 坐标**，appendToBody 时由 ECharts 自行换算成文档坐标；本版 ECharts 给的 rect
  // 为 null，容器位置只能自己从元素取。
  function miniTooltipPos(containerEl) {
    return function (point, params, dom, rect, size) {
      const box = containerEl.getBoundingClientRect();
      const width = size.contentSize[0];
      const height = size.contentSize[1];
      const left = clamp(box.left, 8, Math.max(8, window.innerWidth - width - 8)) - box.left;
      const roomBelow = window.innerHeight - box.bottom;
      const roomAbove = box.top;
      if (roomBelow >= height + 8 || roomBelow >= roomAbove) {
        return [left, box.height + 6];
      }
      return [left, -height - 6];
    };
  }

  // mini 图悬停：该根K线的 OHLC + 涨跌幅（盘中最后一根为实时合成K线）
  function miniTooltipHtml(mini, t) {
    return function(params) {
      const list = Array.isArray(params) ? params : [params];
      const date = list.length ? list[0].axisValue : '';
      const idx = (mini.dates || []).indexOf(date);
      const lines = [esc(date)];
      if (idx >= 0) {
        const c = mini.candles[idx];
        const prev = idx > 0 ? mini.candles[idx - 1][1] : null;
        lines.push('开 ' + fmtPrice(c[0]) + '　收 ' + fmtPrice(c[1]));
        lines.push('低 ' + fmtPrice(c[2]) + '　高 ' + fmtPrice(c[3]));
        if (prev) lines.push('涨跌 ' + fmtPct(round2((c[1] / prev - 1) * 100)));
        if (mini.buy && mini.buy.index === idx) {
          lines.push('★ 买入 ' + fmtPrice(mini.buy.price) + ' × ' + fmtShares(t.shares) + ' 份');
        } else if (mini.buy && mini.buy.index == null) {
          // 买入那一根已滑出窗口（超长持有）：图上只剩一条紫色点线，价格在提示里给
          lines.push('紫点线 = 买入价 ' + fmtPrice(mini.buy.price) + '（' + (mini.buy.date || '') + ' 买入）');
        }
      }
      return lines.join('<br/>');
    };
  }

  function round2(v) {
    return Math.round(v * 100) / 100;
  }

  function renderMiniCharts(trades) {
    if (!window.echarts) return;
    const byId = {};
    trades.forEach(function(t) { byId[t.id] = t; });
    tradeListEl.querySelectorAll('[data-mini-id]').forEach(function(el) {
      const trade = byId[Number(el.dataset.miniId)];
      if (!trade || !trade.mini_chart) {
        el.innerHTML = '<span class="mt-mini-empty">—</span>';
        return;
      }
      try {
        const chart = echarts.init(el);
        chart.setOption(miniOption(trade, el));
        miniCharts[trade.id] = chart;
      } catch (err) {
        el.innerHTML = '<span class="mt-mini-empty">—</span>';
      }
    });
  }

  function renderTradeRow(t, totalValue) {
    const holding = t.holding || {};
    const badges = [];
    if (t.is_intraday) badges.push('<span class="mt-badge mt-badge-live">实时</span>');
    badges.push.apply(badges, stopBadges(t));

    let cells =
      '<td class="mt-cell-name"><span class="mt-trade-name">' + esc(t.name || t.symbol) + '</span>' +
        (badges.length ? '<span class="mt-trade-badges">' + badges.join('') + '</span>' : '') + '</td>' +
      '<td class="mt-trade-symbol">' + esc(t.symbol) + '</td>' +
      '<td class="mt-cell-mini"><div class="mt-mini" data-mini-id="' + esc(t.id) + '"></div></td>' +
      '<td>' + esc(t.buy_date) + '</td>' +
      '<td>' + esc(fmtPrice(t.buy_price)) + '</td>';
    if (t.error) {
      cells += '<td>—</td>' +
        '<td>—</td>' +
        '<td>—</td>' +
        '<td>' + esc(fmtShares(t.shares)) + '</td>' +
        '<td colspan="' + OPEN_ERROR_COLSPAN + '"><span class="mt-trade-error">指标计算失败：' + esc(t.error) + '</span></td>';
    } else {
      const stops = t.stops || {};
      const hardPrice = Number(stops.hard_stop_price);
      const chandPrice = Number(stops.chandelier_stop_price);
      const hardDominant = isFinite(hardPrice) && (!isFinite(chandPrice) || hardPrice >= chandPrice);
      const chandDominant = isFinite(chandPrice) && (!isFinite(hardPrice) || chandPrice >= hardPrice);
      cells +=
        '<td>' + esc(fmtPrice(t.latest_price)) + '</td>' +
        '<td>' + (t._weight != null
          ? withTip(esc(Number(t._weight).toFixed(2) + '%'), positionWeightTip(t, totalValue))
          : '—') + '</td>' +
        '<td>' + (t.daily_change_pct != null
          ? withTip('<span class="' + pctClass(t.daily_change_pct) + '">' +
            esc(fmtPct(t.daily_change_pct)) + '</span>', dailyChangeTip(t))
          : '—') + '</td>' +
        '<td>' + esc(fmtShares(t.shares)) + '</td>' +
        '<td>' + withTip(esc(fmtMoney(t.position_value).replace(/^\+/, '')), positionValueTip(t)) + '</td>' +
        '<td>' + withTip('<span class="' + pctClass(holding.pnl_pct) + '">' + esc(fmtPct(holding.pnl_pct)) +
          '（' + esc(fmtMoney(t.pnl_amount)) + '）</span>', pnlTip(t, holding)) + '</td>' +
        '<td>' + withTip('<span class="' + pctClass(holding.max_gain_pct) + '">' +
          esc(fmtPct(holding.max_gain_pct)) + '</span>', maxGainTip(t, holding)) + '</td>' +
        '<td>' + withTip('<span class="' + pctClass(holding.max_drawdown) + '">' +
          esc(fmtPct(holding.max_drawdown)) + '</span>', maxDrawdownTip(t, holding)) + '</td>' +
        '<td>' + stopCellHtml(stops.hard_stop_price, stops.hard_stop_distance_pct, hardDominant, hardStopTip(stops)) + '</td>' +
        '<td>' + stopCellHtml(stops.chandelier_stop_price, stops.chandelier_stop_distance_pct, chandDominant, chandelierStopTip(stops)) + '</td>' +
        '<td>' + (function() {
          const risk = riskAmount(t);
          if (risk == null) return '—';
          return withTip('<span class="' + (risk > 0 ? 'metric-negative' : '') + '">¥' +
            risk.toFixed(2) + '</span>', riskTip(t));
        })() + '</td>' +
        '<td>' + withTip(holdDaysCell(holding), holdDaysTip(t, holding)) + '</td>';
    }
    cells += '<td class="mt-cell-actions">' +
      (t.error ? '' : '<button type="button" class="mt-btn-sm mt-btn-danger mt-close-btn">清仓</button>') +
      '</td>';
    return '<tr data-id="' + esc(t.id) + '">' + cells + '</tr>';
  }

  // 清仓交易行：聚焦一笔完整交易的结果（买→卖→盈亏），无止损价与操作列
  function renderClosedRow(t) {
    const holding = t.holding || {};
    let cells =
      '<td class="mt-cell-name"><span class="mt-trade-name">' + esc(t.name || t.symbol) + '</span></td>' +
      '<td class="mt-trade-symbol">' + esc(t.symbol) + '</td>' +
      '<td>' + esc(t.buy_date) + '</td>' +
      '<td>' + esc(t.sell_date || '—') + '</td>' +
      '<td>' + esc(fmtPrice(t.buy_price)) + '</td>' +
      '<td>' + (t.sell_price != null ? esc(fmtPrice(t.sell_price)) : '—') + '</td>' +
      '<td>' + esc(fmtShares(t.shares)) + '</td>';
    if (t.error) {
      cells += '<td colspan="' + CLOSED_ERROR_COLSPAN + '"><span class="mt-trade-error">指标计算失败：' + esc(t.error) + '</span></td>';
    } else {
      const buyAmount = Number(t.buy_price) * Number(t.shares);
      const sinceHtml = t.since_close_pct == null
        ? '—'
        : withTip('<span class="' + pctClass(t.since_close_pct) + '">' +
          esc(fmtPct(t.since_close_pct)) + '</span>', sinceCloseTip(t));
      cells +=
        '<td>' + withTip(esc(fmtMoney(buyAmount).replace(/^\+/, '')), buyAmountTip(t)) + '</td>' +
        '<td>' + withTip('<span class="' + pctClass(t.realized_pnl) + '">' +
          esc(fmtMoney(t.realized_pnl)) + '</span>', realizedPnlTip(t)) + '</td>' +
        '<td>' + withTip('<span class="' + pctClass(t.realized_pnl_pct) + '">' +
          esc(fmtPct(t.realized_pnl_pct)) + '</span>', realizedPnlTip(t)) + '</td>' +
        '<td>' + sinceHtml + '</td>' +
        '<td>' + withTip('<span class="' + pctClass(holding.max_gain_pct) + '">' +
          esc(fmtPct(holding.max_gain_pct)) + '</span>', maxGainTip(t, holding)) + '</td>' +
        '<td>' + withTip('<span class="' + pctClass(holding.max_drawdown) + '">' +
          esc(fmtPct(holding.max_drawdown)) + '</span>', maxDrawdownTip(t, holding)) + '</td>' +
        '<td>' + withTip(holdDaysCell(holding), holdDaysTip(t, holding)) + '</td>';
    }
    return '<tr data-id="' + esc(t.id) + '" class="is-closed">' + cells + '</tr>';
  }

  function openTrades() {
    return currentTrades.filter(function(t) {
      return t.status !== 'closed' && tradeRange.pass(t.buy_date);
    });
  }

  function closedTrades() {
    return currentTrades.filter(function(t) {
      // 清仓交易按清仓日期（卖出日期）过滤，缺失时回退买入日期
      return t.status === 'closed' && closedRange.pass(t.sell_date || t.buy_date);
    });
  }

  function renderTradeList() {
    const trades = openTrades();
    disposeMiniCharts();
    tradeEmptyEl.hidden = trades.length > 0;
    if (!trades.length) {
      tradeListEl.innerHTML = '';
      return;
    }
    // 仓位占比的分母：**全部未清仓持仓**的资产总和（与悬停文案"全部持仓总资产"一致）。
    // 此前用时间筛选后的 trades 作分母 → 存在 >筛选窗口 的老持仓时占比会被系统性夸大
    // （R17B backlog：会误导加减仓判断）。指标失败的行无持仓金额，不计入。
    const allOpen = currentTrades.filter(function(t) { return t.status !== 'closed'; });
    const totalValue = allOpen.reduce(function(sum, t) {
      return sum + (t.error ? 0 : (numOrNull(t.position_value) || 0));
    }, 0);
    trades.forEach(function(t) {
      const v = t.error ? null : numOrNull(t.position_value);
      t._weight = (v != null && totalValue > 0) ? (v / totalValue * 100) : null;
    });
    tradeListEl.innerHTML = '<div class="mt-trade-table-wrap"><table class="mt-trade-table"><thead><tr>' +
      TRADE_TABLE_HEADERS.map(function(h) { return tradeHeaderHtml(h, sortState, SORTABLE_COLUMNS); }).join('') +
      '</tr></thead><tbody>' + stableSorted(trades, sortState, SORTABLE_COLUMNS).map(function(t) {
        return renderTradeRow(t, totalValue);
      }).join('') +
      '</tbody></table></div>';
    renderMiniCharts(trades);
  }

  function renderClosedList() {
    const trades = closedTrades();
    lastClosedTrades = trades;
    closedEmptyEl.hidden = trades.length > 0;
    if (!trades.length) {
      closedListEl.innerHTML = '';
      return;
    }
    closedListEl.innerHTML = '<div class="mt-trade-table-wrap"><table class="mt-trade-table"><thead><tr>' +
      CLOSED_TABLE_HEADERS.map(function(h) { return tradeHeaderHtml(h, closedSortState, CLOSED_SORTABLE_COLUMNS); }).join('') +
      '</tr></thead><tbody>' + stableSorted(trades, closedSortState, CLOSED_SORTABLE_COLUMNS).map(renderClosedRow).join('') +
      '</tbody></table></div>';
  }

  // ── ③ 清仓统计（口径随清仓时间范围变化）────────────────
  // 只统计指标计算成功的交易（error 行无盈亏/持有天数，计入会污染均值）。
  function closedStatCards(trades) {
    const rows = trades.filter(function(t) { return !t.error && t.realized_pnl != null; });
    const pnls = rows.map(function(t) { return Number(t.realized_pnl); });
    const wins = pnls.filter(function(v) { return v > 0; });
    const losses = pnls.filter(function(v) { return v < 0; });
    const total = pnls.reduce(function(a, b) { return a + b; }, 0);
    const holdDays = rows
      .map(function(t) { return numOrNull((t.holding || {}).hold_days); })
      .filter(function(v) { return v != null; });
    const symbols = {};
    rows.forEach(function(t) { symbols[t.symbol] = true; });
    const symbolCount = Object.keys(symbols).length;
    const avgWin = mean(wins);
    const avgLoss = mean(losses);
    let best = null;
    let worst = null;
    rows.forEach(function(t) {
      const pnl = Number(t.realized_pnl);
      if (best == null || pnl > Number(best.realized_pnl)) best = t;
      if (worst == null || pnl < Number(worst.realized_pnl)) worst = t;
    });
    const pick = function(t) {
      return t ? (t.name || t.symbol) + '（' + (t.sell_date || '?') + ' 清仓）' : '';
    };
    const winRate = rows.length ? wins.length / rows.length * 100 : null;
    const holdAvg = mean(holdDays);
    return [
      {
        label: '总盈亏', value: fmtMoney(total), cls: pctClass(total), primary: true,
        note: rows.length ? '共 ' + rows.length + ' 笔已清仓' : '',
        tip: '总盈亏 = 各笔已实现盈亏之和 = ' + fmtMoney(total) +
          '（盈利 ' + wins.length + ' 笔 / 亏损 ' + losses.length + ' 笔 / 未计交易费用）',
      },
      {
        label: '胜率', value: winRate == null ? '—' : winRate.toFixed(2) + '%',
        cls: winRate == null ? '' : (winRate >= 50 ? 'metric-positive' : 'metric-negative'),
        note: rows.length ? wins.length + ' 盈 / ' + losses.length + ' 亏' : '',
        tip: '胜率 = 盈利笔数 / 清仓次数 = ' + wins.length + ' / ' + rows.length +
          ' = ' + (winRate == null ? '—' : winRate.toFixed(2) + '%') + '（盈亏为 0 的笔不计入盈利）',
      },
      {
        label: '清仓次数', value: String(rows.length),
        note: symbolCount ? '涉及 ' + symbolCount + ' 只标的' : '',
        tip: '清仓次数 = 时间范围内指标计算成功且已清仓的交易笔数 = ' + rows.length,
      },
      {
        label: '平均持股天数', value: holdAvg == null ? '—' : holdAvg.toFixed(1) + ' 天',
        note: '按交易日计',
        tip: '平均持股天数 = 各笔持有交易日之和 ' + holdDays.reduce(function(a, b) { return a + b; }, 0) +
          ' / 笔数 ' + holdDays.length + ' = ' + (holdAvg == null ? '—' : holdAvg.toFixed(1) + ' 天'),
      },
      {
        label: '交易股票数', value: symbolCount + ' 只', note: '',
        tip: '交易股票数 = 已清仓标的去重计数 = ' + symbolCount + ' 只（同一标的多次交易只计 1 只）',
      },
      {
        label: '平均盈利', value: avgWin == null ? '—' : fmtMoney(avgWin),
        cls: avgWin == null ? '' : 'metric-positive',
        note: wins.length ? wins.length + ' 笔盈利' : '无盈利笔',
        tip: avgWin == null ? '' : '平均盈利 = 盈利金额之和 ' +
          fmtMoney(wins.reduce(function(a, b) { return a + b; }, 0)) + ' / 盈利笔数 ' +
          wins.length + ' = ' + fmtMoney(avgWin),
      },
      {
        label: '平均亏损', value: avgLoss == null ? '—' : fmtMoney(avgLoss),
        cls: avgLoss == null ? '' : 'metric-negative',
        note: losses.length ? losses.length + ' 笔亏损' : '无亏损笔',
        tip: avgLoss == null ? '' : '平均亏损 = 亏损金额之和 ' +
          fmtMoney(losses.reduce(function(a, b) { return a + b; }, 0)) + ' / 亏损笔数 ' +
          losses.length + ' = ' + fmtMoney(avgLoss),
      },
      {
        label: '最大盈利', value: best && wins.length ? fmtMoney(best.realized_pnl) : '—',
        cls: best && wins.length ? 'metric-positive' : '',
        note: wins.length ? pick(best) : '区间内无盈利笔',
        tip: best && wins.length ? '最大盈利 = max(各笔已实现盈亏) = ' + fmtMoney(best.realized_pnl) +
          '（' + pick(best) + '，买 ' + fmtPrice(best.buy_price) + ' → 卖 ' +
          fmtPrice(best.sell_price) + '）' : '',
      },
      {
        label: '最大亏损', value: worst && losses.length ? fmtMoney(worst.realized_pnl) : '—',
        cls: worst && losses.length ? 'metric-negative' : '',
        note: losses.length ? pick(worst) : '区间内无亏损笔',
        tip: worst && losses.length ? '最大亏损 = min(各笔已实现盈亏) = ' + fmtMoney(worst.realized_pnl) +
          '（' + pick(worst) + '，买 ' + fmtPrice(worst.buy_price) + ' → 卖 ' +
          fmtPrice(worst.sell_price) + '）' : '',
      },
    ];
  }

  function renderClosedStats() {
    const trades = closedTrades();
    closedStatsEl.hidden = !trades.length;
    closedStatsEl.innerHTML = '';
    if (!trades.length) return;
    renderStats(closedStatsEl, closedStatCards(trades));
  }

  // ── ④ 已清仓盈亏热力图（treemap）──────────────────────
  // 面积 ∝ |盈亏金额|（treemap 按 value 分配面积），取 |盈亏金额| 最大的 20 只；
  // 颜色沿用全站涨跌口径（红=盈利 / 绿=亏损）。标签字号按色块真实矩形自适应：
  // 先渲染一遍读回布局，再按实际宽高写入标签（同标的看板热力图的两遍渲染套路）。
  const HEAT_MAX_ITEMS = 20;
  const HEAT_COLOR_UP = '#c15b58';    // 盈利（与 .metric-positive 同色）
  const HEAT_COLOR_DOWN = '#1b825f';  // 亏损（与 .metric-negative 同色）
  let heatChart = null;

  function heatItems(trades) {
    return trades
      .filter(function(t) { return !t.error && numOrNull(t.realized_pnl) != null; })
      .map(function(t) { return {trade: t, abs: Math.abs(Number(t.realized_pnl))}; })
      .sort(function(a, b) { return b.abs - a.abs; })
      .slice(0, HEAT_MAX_ITEMS);
  }

  function buildHeatNodes(items) {
    return items.map(function(item) {
      const t = item.trade;
      const pnl = Number(t.realized_pnl);
      return {
        name: t.name || t.symbol,
        value: item.abs,
        meta: t,
        itemStyle: {
          color: pnl >= 0 ? HEAT_COLOR_UP : HEAT_COLOR_DOWN,
          borderColor: 'rgba(255,255,255,0.92)',
          borderWidth: 1.5,
          gapWidth: 1.5,
        },
        label: {color: '#ffffff', fontSize: 12},
        labelText: '',
      };
    });
  }

  function heatTooltip(param) {
    const t = (param.data || {}).meta;
    if (!t) return '';
    const cls = pctClass(t.realized_pnl);
    const row = function(label, html) {
      return '<tr><td>' + label + '</td><td>' + html + '</td></tr>';
    };
    const sub = t.symbol + ' · ' + (t.buy_date || '') + ' 买 → ' + (t.sell_date || '') + ' 卖';
    return '<div class="heat-tip-title">' + esc(t.name || t.symbol) + '</div>' +
      '<div class="heat-tip-sub">' + esc(sub) + '</div>' +
      '<table class="heat-tip-table">' +
      row('盈亏金额', '<span class="' + cls + '">' + esc(fmtMoney(t.realized_pnl)) + '</span>') +
      row('盈亏比例', '<span class="' + cls + '">' + esc(fmtPct(t.realized_pnl_pct)) + '</span>') +
      row('买入 / 卖出', esc(fmtPrice(t.buy_price)) + ' → ' + esc(fmtPrice(t.sell_price))) +
      row('份数', esc(fmtShares(t.shares))) +
      row('持有天数', esc(String((t.holding || {}).hold_days == null ? '—' : (t.holding || {}).hold_days))) +
      row('清仓后距今', t.since_close_pct == null
        ? '—'
        : '<span class="' + pctClass(t.since_close_pct) + '">' + esc(fmtPct(t.since_close_pct)) + '</span>') +
      '</table>' +
      '<div class="heat-tip-hint">面积 ∝ |盈亏金额|，按 |盈亏金额| 降序取前 ' + HEAT_MAX_ITEMS + ' 只</div>';
  }

  // 读回每个色块的真实矩形（key = 我们构建的节点对象）。treemap 的矩形形状由
  // squarify 布局决定，只有渲染完才知道宽高 —— 标签字号/换行必须据此计算。
  function readHeatRects(nodes) {
    const rects = new Map();
    try {
      const data = heatChart.getModel().getSeriesByIndex(0).getData();
      const roots = (data.tree.root.children || []);
      if (roots.length !== nodes.length) throw new Error('tree mismatch');
      nodes.forEach(function(node, i) {
        const idx = typeof roots[i].getDataIndex === 'function' ? roots[i].getDataIndex() : roots[i].dataIndex;
        const layout = data.getItemLayout(idx);
        if (layout) rects.set(node, layout);
      });
    } catch (err) {
      return new Map();
    }
    return rects;
  }

  // 名称按可用宽度折行（中文逐字、连续数字/字母各自成段），名称放不下则让位给数字
  function wrapHeatName(name, fontSize, availW, maxLines) {
    const perLine = Math.max(1, Math.floor(availW / fontSize));
    const runs = String(name || '').match(/\d+|[a-zA-Z]+|[^\da-zA-Z]/g) || [];
    const lines = [];
    let line = '';
    for (const run of runs) {
      if (line && (line + run).length > perLine) {
        lines.push(line);
        line = run;
        if (lines.length >= maxLines) break;
      } else {
        line += run;
      }
    }
    if (lines.length < maxLines && line) lines.push(line);
    return lines.slice(0, maxLines);
  }

  function layoutHeatLabels(nodes, rects, canvasArea) {
    const total = nodes.reduce(function(sum, n) { return sum + (n.value || 0); }, 0) || 1;
    nodes.forEach(function(node) {
      const t = node.meta;
      const rect = rects.get(node);
      // 读不到真实矩形时按面积占比估算方形边长（近似，保证色块里有字）
      const share = (node.value || 0) / total;
      const w = rect ? rect.width : Math.sqrt(Math.max(share, 0) * canvasArea);
      const h = rect ? rect.height : w;
      const fontSize = clamp(Math.min(w / 4.6, h / 3.6), 9, 20);
      node.label.fontSize = fontSize;
      node.label.lineHeight = Math.round(fontSize * 1.22);
      const availW = Math.max(w - 10, 0);
      const availH = Math.max(h - 8, 0);
      const maxLines = Math.max(0, Math.floor(availH / node.label.lineHeight));
      // 色块内自上而下：名称（最多两行）→ 盈亏比例 → 盈亏金额，放不下就少显示几行
      const lines = wrapHeatName(node.name, fontSize, availW, Math.min(maxLines, 2));
      if (lines.length < maxLines) lines.push(fmtPct(t.realized_pnl_pct));
      if (lines.length < maxLines) lines.push(heatAmountText(t.realized_pnl));
      node.labelText = lines.join('\n');
    });
  }

  // 金额带符号、不带货币符号（热力图色块内文字，与图例同口径）
  function heatAmountText(v) {
    const n = Number(v);
    return (n > 0 ? '+' : (n < 0 ? '-' : '')) + Math.abs(n).toFixed(2);
  }

  function heatOption(nodes) {
    return {
      animation: false,
      tooltip: {confine: true, formatter: heatTooltip},
      series: [{
        type: 'treemap',
        left: 0, right: 0, top: 0, bottom: 0,
        roam: false,
        nodeClick: false,
        breadcrumb: {show: false},
        label: {show: true, formatter: function(p) { return p.data.labelText || ''; }},
        upperLabel: {show: false},
        itemStyle: {borderColor: 'rgba(255,255,255,0.92)', borderWidth: 1.5, gapWidth: 1.5},
        data: nodes,
      }],
    };
  }

  function renderHeatmap(trades) {
    const items = heatItems(trades);
    if (!items.length || !window.echarts) {
      heatPanelEl.hidden = true;
      return;
    }
    heatPanelEl.hidden = false;
    heatNoteEl.textContent = '面积 ∝ |盈亏金额|，按 |盈亏金额| 降序取前 ' + items.length + ' 只' +
      '（共 ' + trades.length + ' 笔已清仓）· 红=盈利 / 绿=亏损';
    if (!heatChart) {
      try {
        heatChart = echarts.init(heatChartEl);
      } catch (err) {
        heatChart = null;
        heatPanelEl.hidden = true;
        return;
      }
    }
    heatChart.resize();
    const nodes = buildHeatNodes(items);
    heatChart.setOption(heatOption(nodes), true);
    const rects = readHeatRects(nodes);
    layoutHeatLabels(nodes, rects, heatChartEl.clientWidth * heatChartEl.clientHeight);
    heatChart.setOption({series: [{type: 'treemap', data: nodes}]});
  }

  // ── 录入弹窗 ───────────────────────────────────────────
  // 弹窗 a11y（P2-28）：Esc 关闭任一打开的弹窗
  document.addEventListener('keydown', function (event) {
    if (event.key !== 'Escape') return;
    if (!enterModalEl.hidden) enterModalEl.hidden = true;
    if (!closeModalEl.hidden) closeModalEl.hidden = true;
  });

  function openEnterModal() {
    // 预填试算区的输入，减少重复录入
    enterSymbolEl.value = symbolEl.value.trim();
    enterDateEl.value = buyDateEl.value || todayStr();
    enterPriceEl.value = buyPriceEl.value;
    enterSharesEl.value = '';
    showEnterMessage('', false);
    enterModalEl.hidden = false;
    refreshEnterRange();
  }

  async function refreshEnterRange() {
    const symbol = enterSymbolEl.value.trim();
    const buyDate = enterDateEl.value;
    enterRange = null;
    enterHintEl.hidden = true;
    if (!symbol || !buyDate) return;
    const seq = ++enterRangeSeq;
    try {
      const candle = await fetchDayCandle(symbol, buyDate);
      if (seq !== enterRangeSeq) return;
      if (!candle) {
        enterHintEl.textContent = buyDate + ' 无K线数据（非交易日或未入库），买入价将按下一交易日口径计算';
        enterHintEl.hidden = false;
        return;
      }
      enterRange = {date: buyDate, low: candle.low, high: candle.high, close: candle.close};
      enterHintEl.innerHTML = rangeHintText(buyDate, enterRange);
      enterHintEl.hidden = false;
    } catch (err) {
      if (seq !== enterRangeSeq) return;
      enterHintEl.textContent = err.message || '无法获取当日行情';
      enterHintEl.hidden = false;
    }
  }

  async function confirmEnter() {
    const symbol = enterSymbolEl.value.trim();
    const buyDate = enterDateEl.value;
    const buyPrice = parseFloat(enterPriceEl.value);
    const shares = parseFloat(enterSharesEl.value);
    if (!symbol) { showEnterMessage('请输入标的代码', true); return; }
    if (!buyDate) { showEnterMessage('请选择买入日期', true); return; }
    if (!(buyPrice > 0)) { showEnterMessage('请输入大于 0 的买入价格', true); return; }
    if (!(shares > 0)) { showEnterMessage('请输入大于 0 的买入份数', true); return; }
    if (enterRange && enterRange.date === buyDate &&
        (buyPrice < enterRange.low || buyPrice > enterRange.high)) {
      showEnterMessage('买入价格 ' + fmtPrice(buyPrice) + ' 超出 ' + buyDate + ' 当日价格区间 [' +
        fmtPrice(enterRange.low) + ', ' + fmtPrice(enterRange.high) + ']', true);
      return;
    }

    enterConfirmBtn.disabled = true;
    showEnterMessage('录入中…', false);
    try {
      await postJson('/manual-trade/api/trades/create', {
        symbol: symbol, buy_date: buyDate, buy_price: buyPrice, shares: shares,
      });
      enterModalEl.hidden = true;
      showMessage('已录入：' + symbol + ' ' + buyDate + ' @ ' +
        fmtPrice(buyPrice) + ' × ' + fmtShares(shares), false);
      loadTrades();
    } catch (err) {
      showEnterMessage(err.message || '录入失败', true);
    } finally {
      enterConfirmBtn.disabled = false;
    }
  }

  // ── 清仓弹窗 ───────────────────────────────────────────
  function openCloseModal(trade) {
    closeCtx = trade;
    closeTitleEl.textContent = '清仓 — ' + (trade.name ? trade.name + ' ' : '') + trade.symbol +
      '（' + trade.buy_date + ' @ ' + fmtPrice(trade.buy_price) + ' × ' + fmtShares(trade.shares) + '）';
    sellDateEl.value = todayStr();
    sellPriceEl.value = trade.latest_price != null ? trade.latest_price : '';
    showCloseMessage('', false);
    closeModalEl.hidden = false;
    refreshCloseRange();
  }

  async function refreshCloseRange() {
    if (!closeCtx) return;
    const sellDate = sellDateEl.value;
    closeRange = null;
    closeHintEl.hidden = true;
    if (!sellDate) return;
    const seq = ++closeRangeSeq;
    try {
      const candle = await fetchDayCandle(closeCtx.symbol, sellDate);
      if (seq !== closeRangeSeq || !closeCtx) return;
      if (!candle) {
        closeHintEl.textContent = sellDate + ' 无K线数据（非交易日或未入库）';
        closeHintEl.hidden = false;
        return;
      }
      closeRange = {date: sellDate, low: candle.low, high: candle.high, close: candle.close};
      closeHintEl.innerHTML = rangeHintText(sellDate, closeRange);
      closeHintEl.hidden = false;
    } catch (err) {
      if (seq !== closeRangeSeq) return;
      closeHintEl.textContent = err.message || '无法获取当日行情';
      closeHintEl.hidden = false;
    }
  }

  async function confirmClose() {
    if (!closeCtx) return;
    const sellDate = sellDateEl.value;
    const sellPrice = parseFloat(sellPriceEl.value);
    if (!sellDate) { showCloseMessage('请选择清仓日期', true); return; }
    if (!(sellPrice > 0)) { showCloseMessage('请输入大于 0 的清仓价格', true); return; }
    if (sellDate < closeCtx.buy_date) {
      showCloseMessage('清仓日期早于买入日期 ' + closeCtx.buy_date, true); return;
    }
    if (closeRange && closeRange.date === sellDate &&
        (sellPrice < closeRange.low || sellPrice > closeRange.high)) {
      showCloseMessage('清仓价格 ' + fmtPrice(sellPrice) + ' 超出 ' + sellDate + ' 当日价格区间 [' +
        fmtPrice(closeRange.low) + ', ' + fmtPrice(closeRange.high) + ']', true);
      return;
    }

    closeConfirmBtn.disabled = true;
    try {
      await postJson('/manual-trade/api/trades/close', {
        trade_id: closeCtx.id, sell_date: sellDate, sell_price: sellPrice,
      });
      closeModalEl.hidden = true;
      closeCtx = null;
      showMessage('已清仓', false);
      loadTrades();
    } catch (err) {
      showCloseMessage(err.message || '清仓失败', true);
    } finally {
      closeConfirmBtn.disabled = false;
    }
  }

  // ── 时间范围与事件绑定 ─────────────────────────────────
  const tradeRange = createRangeFilter(
    document.getElementById('mtTimeFilter'),
    document.getElementById('mtTimeCustom'),
    document.getElementById('mtTimeFrom'),
    document.getElementById('mtTimeTo'),
    function() { renderTradeList(); }
  );
  const closedRange = createRangeFilter(
    document.getElementById('mtClosedTimeFilter'),
    document.getElementById('mtClosedTimeCustom'),
    document.getElementById('mtClosedTimeFrom'),
    document.getElementById('mtClosedTimeTo'),
    function() {
      // 清仓表、统计与热力图同源：一处范围变化三者一起重算
      renderClosedList();
      renderClosedStats();
      renderHeatmap(lastClosedTrades);
    }
  );

  evaluateBtn.addEventListener('click', evaluate);
  [symbolEl, buyDateEl, buyPriceEl, riskBudgetEl].forEach(function(el) {
    el.addEventListener('keydown', function(ev) {
      if (ev.key === 'Enter') evaluate();
    });
  });
  // 风险预算变更后，试算结果已展示时按新预算自动重算（与止损松紧切换同一模式）
  riskBudgetEl.addEventListener('change', function() {
    if (!resultEl.hidden) evaluate();
  });
  symbolEl.addEventListener('change', scheduleRangeRefresh);
  buyDateEl.addEventListener('change', scheduleRangeRefresh);

  openEnterBtn.addEventListener('click', openEnterModal);
  refreshBtn.addEventListener('click', loadTrades);
  // 盘后数据更新完成后自动刷新持仓
  document.addEventListener('daily-update-done', function() { loadTrades(); });

  enterConfirmBtn.addEventListener('click', confirmEnter);
  enterCancelBtn.addEventListener('click', function() {
    enterModalEl.hidden = true;
  });
  [enterSymbolEl, enterDateEl].forEach(function(el) {
    el.addEventListener('change', refreshEnterRange);
  });
  [enterPriceEl, enterSharesEl].forEach(function(el) {
    el.addEventListener('keydown', function(ev) {
      if (ev.key === 'Enter') confirmEnter();
    });
  });
  enterModalEl.addEventListener('click', function(ev) {
    if (ev.target === enterModalEl) enterModalEl.hidden = true;
  });

  tradeListEl.addEventListener('click', function(ev) {
    // 表头排序点击（事件委托：表头随每次渲染重建，需在容器上拦截）
    const th = ev.target.closest('th[data-sort-key]');
    if (th) {
      sortState = cycleSort(sortState, th.dataset.sortKey);
      renderTradeList();
      return;
    }
    const btn = ev.target.closest('button');
    if (!btn) return;
    const rowEl = btn.closest('tr[data-id]');
    if (!rowEl) return;
    const trade = currentTrades.find(function(t) { return t.id === Number(rowEl.dataset.id); });
    if (!trade) return;
    if (btn.classList.contains('mt-close-btn')) openCloseModal(trade);
  });

  closedListEl.addEventListener('click', function(ev) {
    const th = ev.target.closest('th[data-sort-key]');
    if (th) {
      closedSortState = cycleSort(closedSortState, th.dataset.sortKey);
      renderClosedList();
    }
  });

  // 止损松紧切换：持仓记录止损价同步切换；试算结果已展示时按新口径重算
  stopModeToggleEl.addEventListener('click', function(ev) {
    const btn = ev.target.closest('button[data-mode]');
    if (!btn || btn.dataset.mode === stopMode) return;
    stopMode = btn.dataset.mode;
    stopModeToggleEl.querySelectorAll('button[data-mode]').forEach(function(b) {
      b.classList.toggle('is-active', b.dataset.mode === stopMode);
    });
    loadTrades();
    if (!resultEl.hidden) evaluate();
  });

  // mini 图与热力图随窗口尺寸自适应（ECharts 不跟随容器自动重排）
  let resizeTimer = null;
  window.addEventListener('resize', function() {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function() {
      Object.keys(miniCharts).forEach(function(id) {
        try {
          miniCharts[id].resize();
        } catch (err) {
          // 实例已失效：忽略
        }
      });
      if (heatChart && !heatPanelEl.hidden) {
        renderHeatmap(lastClosedTrades);
      }
    }, 160);
  });

  closeConfirmBtn.addEventListener('click', confirmClose);
  closeCancelBtn.addEventListener('click', function() {
    closeModalEl.hidden = true;
    closeCtx = null;
  });
  sellDateEl.addEventListener('change', refreshCloseRange);
  closeModalEl.addEventListener('click', function(ev) {
    if (ev.target === closeModalEl) {
      closeModalEl.hidden = true;
      closeCtx = null;
    }
  });

  buyDateEl.value = todayStr();
  loadSymbolOptions();
  refreshDayRange();
  loadTrades();
})();
