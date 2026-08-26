(() => {
  const symbolSearchEl = document.getElementById('mvSymbolSearch');
  const symbolOptionsEl = document.getElementById('mvSymbolOptions');
  const symbolInputEl = document.getElementById('mvSymbolInput');
  const backtestStrategyDropdownEl = document.getElementById('mvBacktestStrategyDropdown');
  const backtestStrategyBtnEl = document.getElementById('mvBacktestStrategyBtn');
  const backtestStrategyLabelEl = document.getElementById('mvBacktestStrategyLabel');

  // Strategy dropdown panel — lives on <body> to escape all parent clipping
  const backtestStrategyPanelEl = document.createElement('div');
  backtestStrategyPanelEl.id = 'mvBacktestStrategyPanel';
  backtestStrategyPanelEl.className = 'multi-select-panel';
  backtestStrategyPanelEl.hidden = true;
  const backtestStrategyListEl = document.createElement('div');
  backtestStrategyListEl.id = 'mvBacktestStrategyList';
  backtestStrategyListEl.className = 'multi-select-list';
  backtestStrategyPanelEl.appendChild(backtestStrategyListEl);
  document.body.appendChild(backtestStrategyPanelEl);

  // Position-sizer dropdown (仓位策略) — same body-mounted panel pattern
  const backtestSizerDropdownEl = document.getElementById('mvBacktestSizerDropdown');
  const backtestSizerBtnEl = document.getElementById('mvBacktestSizerBtn');
  const backtestSizerLabelEl = document.getElementById('mvBacktestSizerLabel');
  const backtestSizerPanelEl = document.createElement('div');
  backtestSizerPanelEl.id = 'mvBacktestSizerPanel';
  backtestSizerPanelEl.className = 'multi-select-panel';
  backtestSizerPanelEl.hidden = true;
  const backtestSizerListEl = document.createElement('div');
  backtestSizerListEl.id = 'mvBacktestSizerList';
  backtestSizerListEl.className = 'multi-select-list';
  backtestSizerPanelEl.appendChild(backtestSizerListEl);
  document.body.appendChild(backtestSizerPanelEl);
  const backtestStartEl = document.getElementById('mvStartDate');
  const backtestEndEl = document.getElementById('mvEndDate');
  const runBacktestBtnEl = document.getElementById('mvRunBacktestBtn');
  const backtestProgressEl = document.getElementById('mvBacktestProgress');
  const backtestProgressBarEl = document.getElementById('mvBacktestProgressBar');
  const backtestProgressTextEl = document.getElementById('mvBacktestProgressText');
  const backtestRangeLabelEl = document.getElementById('mvBacktestRangeLabel');
  const refreshBtnEl = document.getElementById('mvRefreshBtn');
  const chartTitleEl = document.getElementById('mvChartTitle');
  const rangeMetaEl = document.getElementById('mvRangeMeta');
  const backtestSummaryEl = document.getElementById('mvBacktestSummary');
  const tradeMetaEl = document.getElementById('mvTradeMeta');
  const tradesBodyEl = document.getElementById('mvTradesBody');
  const returnsPanelEl = document.getElementById('mvReturnsPanel');
  const heatTitleEl = document.getElementById('mvHeatTitle');
  const annualTitleEl = document.getElementById('mvAnnualTitle');
  const annualTableHeadEl = document.querySelector('#mvAnnualTable thead');
  const annualTableBodyEl = document.querySelector('#mvAnnualTable tbody');
  const debugInfoEl = document.getElementById('mvDebugInfo');
  const debugPreviewEl = document.getElementById('mvDebugPreview');
  const trendControlsEl = document.getElementById('mvTrendControls');
  const trendShortEl = document.getElementById('mvTrendShort');
  const trendMidEl = document.getElementById('mvTrendMid');
  const trendLongEl = document.getElementById('mvTrendLong');
  const trendAtrEl = document.getElementById('mvTrendAtr');
  const rsiControlsEl = document.getElementById('mvRsiControls');
  const rsiPeriodEl = document.getElementById('mvRsiPeriod');
  const stopModeToggleEl = document.getElementById('mvStopModeToggle');
  const stopInfoEl = document.getElementById('mvStopInfoBar');

  let priceChart = null;
  let trendChart = null;
  let volumeChart = null;
  let rsiChart = null;
  let macdChart = null;
  let biasChart = null;
  let heatChart = null;
  let charts = [];
  try {
    priceChart = echarts.init(document.getElementById('mvPriceChart'));
    trendChart = echarts.init(document.getElementById('mvTrendChart'));
    volumeChart = echarts.init(document.getElementById('mvVolumeChart'));
    rsiChart = echarts.init(document.getElementById('mvRsiChart'));
    macdChart = echarts.init(document.getElementById('mvMacdChart'));
    biasChart = echarts.init(document.getElementById('mvBiasChart'));
    charts = [priceChart, trendChart, volumeChart, rsiChart, biasChart, macdChart];
    for (const chart of charts) chart.group = 'market-view-time';
    echarts.connect('market-view-time');
    // 止损线悬停说明：写入图上方 DOM 信息条（ECharts label 会被 grid 边缘裁剪，改用图外展示）
    priceChart.on('mouseover', (e) => {
      if (e.componentType === 'markLine' && e.seriesName === '我的止损' && e.data?.hoverText) {
        stopInfoEl.textContent = e.data.hoverText;
        stopInfoEl.hidden = false;
      }
    });
    priceChart.on('mouseout', (e) => {
      if (e.componentType === 'markLine' && e.seriesName === '我的止损') stopInfoEl.hidden = true;
    });
    priceChart.on('globalout', () => { stopInfoEl.hidden = true; });
  } catch (err) {
    console.warn('ECharts 初始化失败，图表将不可用：', err);
    charts = [];
  }

  const maColors = Object.freeze({
    5: '#7c3aed',
    10: '#0d7a71',
    20: '#c7834c',
    40: '#2563eb',
    60: '#b42318',
    120: '#475569',
    200: '#111827',
  });
  const maPeriods = ['5', '10', '20', '40', '60', '120', '200'];
  let currentCandles = [];
  let currentZoom = null;
  let currentPayload = null;
  let currentBacktest = null;
  // 我的持仓标注：当前标的上本人的交易记录（买卖点 + 双档止损），
  // 来自 /market-view/api/my-trades；null 表示无持仓或加载失败（不渲染标注）
  let myTrades = null;
  let myStopMode = 'tight';  // 止损线档位：tight 紧止损（默认）/ loose 松止损
  let allMultiKline = [];        // [{strategy_id, strategy_name, sizer_id, sizer_name, buy_points, sell_points, skipped_buy_points}]
  let activeResultKey = null;    // which 策略×仓位 combo's markers are shown on chart
  // Degraded sizing flags — synced from /api/meta (ruleMeta.sizing) in
  // loadRuleMeta; this literal is only the pre-meta fallback.
  let SIZING_DEGRADED_FLAGS = new Set(['kelly_floor_applied', 'atr_unavailable_fallback']);
  const SIZING_FLAG_TEXT = {
    kelly_floor_applied: '凯利≤0，降级仓位',
    atr_unavailable_fallback: '无可用ATR，降级仓位',
    atr_fallback_prev_day: '使用历史ATR',
    risk_budget_unconstrained: '全买未超预算',
  };
  const SKIP_REASON_TEXT = {
    insufficient_cash: '现金不足',
    sizer_target_below_lot: '目标不足一手',
    sizer_skip: '仓位策略跳过',
  };
  let allSymbols = [];
  let ruleMeta = { strategies: [] };
  let backtestProgressHideTimer = null;
  const priceAxisSplitCount = 6;


  function num(v, digits = 3) {
    const n = Number(v);
    if (!Number.isFinite(n)) return '-';
    return n.toFixed(digits);
  }

  function pct(v) {
    const n = Number(v);
    if (!Number.isFinite(n)) return '-';
    return `${(n * 100).toFixed(2)}%`;
  }

  function money(v) {
    const n = Number(v);
    if (!Number.isFinite(n)) return '-';
    return n.toFixed(2);
  }

  function setBacktestProgress(percent, state = 'running', text = '') {
    const value = Math.max(0, Math.min(100, Number(percent) || 0));
    backtestProgressEl.hidden = false;
    backtestProgressEl.classList.toggle('is-complete', state === 'complete');
    backtestProgressEl.classList.toggle('is-error', state === 'error');
    backtestProgressBarEl.value = value;
    if (text) backtestProgressTextEl.textContent = text;
  }

  function showBacktestProgress() {
    clearTimeout(backtestProgressHideTimer);
    backtestProgressTextEl.textContent = '';
    setBacktestProgress(0, 'running', '回测任务启动中…');
  }

  function updateBacktestProgress(current, total) {
    const cur = Math.max(0, Number(current) || 0);
    const tot = Math.max(1, Number(total) || 1);
    const pct = Math.min(cur / tot, 1);
    setBacktestProgress(
      pct * 100,
      'running',
      `${(pct * 100).toFixed(1)}%（已计算 ${cur}/${tot} 根K线）`,
    );
  }

  function finishBacktestProgress(ok) {
    setBacktestProgress(100, ok ? 'complete' : 'error', ok ? '回测完成' : '回测失败');
    clearTimeout(backtestProgressHideTimer);
    backtestProgressHideTimer = setTimeout(() => {
      backtestProgressEl.hidden = true;
      backtestProgressBarEl.value = 0;
      backtestProgressTextEl.textContent = '';
    }, ok ? 1200 : 2400);
  }

  function normalizeCandleValues(raw, param) {
    if (!Array.isArray(raw)) return [];
    if (raw.length >= 5) {
      const first = Number(raw[0]);
      const dataIndex = Number(param?.dataIndex);
      const axisValue = String(param?.axisValue ?? param?.name ?? '');
      if ((Number.isFinite(first) && Number.isFinite(dataIndex) && first === dataIndex) || String(raw[0]) === axisValue) {
        return raw.slice(1, 5);
      }
    }
    return raw.slice(0, 4);
  }

  function candleTooltipValues(param) {
    const fromValue = normalizeCandleValues(param?.value, param);
    if (fromValue.length >= 4 && fromValue.every((v) => Number.isFinite(Number(v)))) {
      return fromValue;
    }
    return normalizeCandleValues(param?.data, param);
  }

  function paramDataIndex(param, dates) {
    const idx = Number(param?.dataIndex);
    if (Number.isInteger(idx) && idx >= 0) return idx;
    const axisValue = String(param?.axisValue ?? param?.name ?? '');
    return Array.isArray(dates) ? dates.indexOf(axisValue) : -1;
  }

  function compact(v) {
    const n = Number(v);
    if (!Number.isFinite(n)) return '-';
    if (Math.abs(n) >= 100000000) return `${(n / 100000000).toFixed(2)}亿`;
    if (Math.abs(n) >= 10000) return `${(n / 10000).toFixed(2)}万`;
    return n.toFixed(0);
  }

  function buildDataZoom(start, end) {
    return [
      { type: 'inside', xAxisIndex: 0, start, end, throttle: 30 },
      { type: 'slider', xAxisIndex: 0, start, end, height: 24, bottom: 4 },
    ];
  }

  function oneYearZoom(dates) {
    if (!Array.isArray(dates) || dates.length <= 1) return { start: 0, end: 100 };
    const last = new Date(dates[dates.length - 1]);
    if (Number.isNaN(last.getTime())) return { start: 0, end: 100 };
    const cutoff = new Date(last);
    cutoff.setFullYear(cutoff.getFullYear() - 1);
    let idx = dates.findIndex((d) => {
      const day = new Date(d);
      return !Number.isNaN(day.getTime()) && day >= cutoff;
    });
    if (idx < 0) idx = 0;
    return {
      start: Math.max(0, Math.min(100, (idx / (dates.length - 1)) * 100)),
      end: 100,
    };
  }

  function zoomForDateRange(dates, startDate, endDate) {
    if (!Array.isArray(dates) || dates.length <= 1) return { start: 0, end: 100 };
    const startText = String(startDate || dates[0] || '');
    const endText = String(endDate || dates[dates.length - 1] || '');
    let startIdx = dates.findIndex((d) => d >= startText);
    if (startIdx < 0) startIdx = 0;
    let endIdx = -1;
    for (let i = dates.length - 1; i >= 0; i -= 1) {
      if (dates[i] <= endText) {
        endIdx = i;
        break;
      }
    }
    if (endIdx < 0) endIdx = dates.length - 1;
    if (endIdx < startIdx) endIdx = startIdx;
    return {
      start: Math.max(0, Math.min(100, (startIdx / (dates.length - 1)) * 100)),
      end: Math.max(0, Math.min(100, (endIdx / (dates.length - 1)) * 100)),
    };
  }

  function updateBacktestRangeLabel() {
    backtestRangeLabelEl.textContent = `${backtestStartEl.value || '最早'} ~ ${backtestEndEl.value || '最新'}`;
  }

  function zoomFromEvent(params) {
    const dz = (params && params.batch && params.batch[0]) || params || {};
    const opt = priceChart.getOption() || {};
    const optDz = (opt.dataZoom && opt.dataZoom[0]) || {};
    return {
      start: Number.isFinite(dz.start) ? dz.start : (Number.isFinite(optDz.start) ? optDz.start : 0),
      end: Number.isFinite(dz.end) ? dz.end : (Number.isFinite(optDz.end) ? optDz.end : 100),
    };
  }

  function visibleIndexRange(length, zoom) {
    if (!length) return [0, 0];
    const start = Number.isFinite(zoom?.start) ? zoom.start : 0;
    const end = Number.isFinite(zoom?.end) ? zoom.end : 100;
    const s = Math.max(0, Math.min(length - 1, Math.floor((start / 100) * (length - 1))));
    const e = Math.max(s, Math.min(length - 1, Math.ceil((end / 100) * (length - 1))));
    return [s, e];
  }

  function computeLogPriceRange(candles, zoom) {
    const [start, end] = visibleIndexRange(candles.length, zoom);
    let low = Infinity;
    let high = -Infinity;
    for (let i = start; i <= end; i += 1) {
      const c = candles[i] || [];
      const l = Number(c[2]);
      const h = Number(c[3]);
      if (Number.isFinite(l) && l > 0) low = Math.min(low, l);
      if (Number.isFinite(h) && h > 0) high = Math.max(high, h);
    }
    if (!Number.isFinite(low) || !Number.isFinite(high)) return { min: null, max: null };
    if (high < low) high = low;
    const logLow = Math.log(low);
    const logHigh = Math.log(high);
    const span = Math.max(logHigh - logLow, Math.log(1.02));
    const pad = span * 0.08;
    const paddedSpan = span + pad * 2;
    return {
      min: Math.max(Number.MIN_VALUE, Math.exp(logLow - pad)),
      max: Math.exp(logHigh + pad),
      logBase: Math.exp(paddedSpan / priceAxisSplitCount),
    };
  }

  function updatePriceYAxis(params) {
    if (!currentCandles.length || !priceChart) return;
    currentZoom = zoomFromEvent(params);
    const range = computeLogPriceRange(currentCandles, currentZoom);
    priceChart.setOption({ yAxis: { min: range.min, max: range.max, logBase: range.logBase } }, false);
  }

  function trendParamsFromInputs() {
    const nShort = Number(trendShortEl.value);
    const nMid = Number(trendMidEl.value);
    const nLong = Number(trendLongEl.value);
    const atrPeriod = Number(trendAtrEl.value);
    if (![nShort, nMid, nLong, atrPeriod].every((v) => Number.isInteger(v) && v > 0)) {
      throw new Error('趋势值参数必须为正整数');
    }
    if (!(nShort < nMid && nMid < nLong)) {
      throw new Error('趋势值参数要求 S < M < L');
    }
    return { nShort, nMid, nLong, atrPeriod };
  }

  function applyTrendConfigToInputs(config) {
    if (!config) return;
    trendShortEl.value = config.n_short ?? trendShortEl.value;
    trendMidEl.value = config.n_mid ?? trendMidEl.value;
    trendLongEl.value = config.n_long ?? trendLongEl.value;
    trendAtrEl.value = config.atr_period ?? trendAtrEl.value;
  }

  function appendTrendParams(params) {
    if (!trendShortEl.value || !trendMidEl.value || !trendLongEl.value || !trendAtrEl.value) return;
    const trendParams = trendParamsFromInputs();
    params.set('trend_n_short', trendParams.nShort);
    params.set('trend_n_mid', trendParams.nMid);
    params.set('trend_n_long', trendParams.nLong);
    params.set('trend_atr_period', trendParams.atrPeriod);
  }

  function rsiPeriodFromInput() {
    const period = Number(rsiPeriodEl.value || 14);
    if (!Number.isInteger(period) || period <= 1) {
      throw new Error('RSI 周期必须大于 1');
    }
    return period;
  }

  function applyRsiConfigToInputs(config) {
    if (!config) return;
    rsiPeriodEl.value = config.period ?? rsiPeriodEl.value ?? 14;
  }

  function appendRsiParams(params) {
    params.set('rsi_period', rsiPeriodFromInput());
  }

  function mainOverlaySeries(indicators) {
    const maSeries = maPeriods.map((period) => ({
      name: `MA${period}`,
      type: 'line',
      data: indicators.ma?.[period] || [],
      symbol: 'none',
      smooth: true,
      lineStyle: { width: 1.1, color: maColors[period] || '#475569' },
      // 图例图标圆形取 itemStyle 颜色，需与线色一致避免误导
      itemStyle: { color: maColors[period] || '#475569' },
    }));
    const bollSeries = [
      { name: 'BOLL上轨', type: 'line', data: indicators.boll?.upper || [], symbol: 'none', smooth: true, lineStyle: { width: 1.1, color: '#b42318' }, itemStyle: { color: '#b42318' } },
      { name: 'BOLL中轨', type: 'line', data: indicators.boll?.mid || [], symbol: 'none', smooth: true, lineStyle: { width: 1.1, color: '#c7834c' }, itemStyle: { color: '#c7834c' } },
      { name: 'BOLL下轨', type: 'line', data: indicators.boll?.lower || [], symbol: 'none', smooth: true, lineStyle: { width: 1.1, color: '#0d7a71' }, itemStyle: { color: '#0d7a71' } },
    ];
    return [...maSeries, ...bollSeries];
  }

  function hasDegradedFlag(point) {
    return (point?.flags || []).some(f => SIZING_DEGRADED_FLAGS.has(f));
  }

  function flagText(flag) {
    return SIZING_FLAG_TEXT[flag] || flag;
  }

  function resultKey(r) {
    return `${r?.strategy_id || ''}|${r?.sizer_id || ''}`;
  }

  function skippedBuySeries(points) {
    return {
      name: '跳过',
      type: 'scatter',
      data: (points || []).map((p) => [p.date, p.price, p]),
      symbol: 'circle',
      symbolSize: 14,
      itemStyle: {
        color: 'rgba(255,255,255,0.15)',
        borderColor: '#6b7280',
        borderWidth: 2,
        borderType: 'dashed',
      },
      label: {
        show: true,
        formatter: '跳过',
        position: 'top',
        color: '#6b7280',
        fontWeight: 700,
        fontSize: 10,
        textBorderColor: '#ffffff',
        textBorderWidth: 3,
      },
      tooltip: {
        formatter: (p) => {
          const raw = p.data?.[2] || {};
          return [
            `跳过买入 ${raw.date || ''}`,
            `收盘价：${num(raw.price, 4)}`,
            `原因：${SKIP_REASON_TEXT[raw.reason] || raw.reason || '-'}`,
            raw.note ? `说明：${raw.note}` : '',
          ].filter(Boolean).join('<br/>');
        },
      },
      z: 7,
    };
  }

  function tradeSeries(points, name, color, symbol) {
    return {
      name,
      type: 'scatter',
      data: (points || []).map((p) => [p.date, p.price, p]),
      symbol,
      symbolSize: 22,
      itemStyle: {
        color,
        borderColor: '#ffffff',
        borderWidth: 2.5,
        shadowBlur: 10,
        shadowColor: 'rgba(17, 35, 42, 0.28)',
      },
      label: {
        show: true,
        formatter: name,
        position: name.includes('买') ? 'top' : 'bottom',
        color,
        fontWeight: 800,
        fontSize: 11,
        textBorderColor: '#ffffff',
        textBorderWidth: 3,
      },
      tooltip: {
        formatter: (p) => {
          const raw = p.data?.[2] || {};
          const flagLine = (raw.flags || []).length
            ? `标记：${(raw.flags || []).map(flagText).join('、')}`
            : '';
          return [
            `${name} ${raw.date || ''}`,
            `成交价：${num(raw.price, 4)}`,
            `参考价：${num(raw.reference_price, 4)}`,
            `数量：${raw.qty || 0}`,
            flagLine,
          ].filter(Boolean).join('<br/>');
        },
      },
      z: 8,
    };
  }

  // ── 我的持仓标注（登录用户在该标的上的买卖点 + 止损线）─────────────────
  // 精确价格口径：买卖点散点锚在 [日期, 实际成交价] 坐标上（不是挂在K线
  // 上方/下方的示意点），止损线 y 值为实际止损价，任何缩放级别下位置不变。
  function fmtExact(v) {
    const n = Number(v);
    return Number.isFinite(n) ? String(n) : '-';
  }

  // 止损线悬停文案：硬止损/吊灯止损各一行算式，最后一行给出取值规则
  function stopHoverText(t, s, effectivePrice) {
    const modeLabel = myStopMode === 'tight' ? '紧止损' : '松止损';
    const lines = [
      `${modeLabel}线 = ${fmtExact(effectivePrice)} 元，说明：`,
      `硬止损 = 买入价 ${fmtExact(t.buy_price)} − ${s.hard_stop_atr_mul}×买入日ATR20 ${fmtExact(s.atr_at_buy)} = ${fmtExact(s.hard_stop_price)} 元`,
      `吊灯止损 = 买入以来最高价 ${fmtExact(s.highest_since_buy)}（${s.highest_since_buy_date || '-'}）− ${s.chandelier_stop_atr_mul}×当前ATR20 ${fmtExact(s.current_atr)} = ${fmtExact(s.chandelier_stop_price)} 元`,
      `二者取价高者 ${fmtExact(effectivePrice)} 元作为${modeLabel}线`,
    ];
    return lines.join('\n');
  }

  function myAnnotationSeries() {
    if (!myTrades || !Array.isArray(myTrades.trades) || !myTrades.trades.length) return [];
    // 存在回测结果时，K线让位给策略买卖点，不再叠加本人实际买卖点（避免两套点位混淆）；
    // 止损线不是买卖点，始终显示。
    const showMyPoints = !currentBacktest;
    const buys = [];
    const sells = [];
    const stopLines = [];
    for (const t of myTrades.trades) {
      if (t.buy_date && Number(t.buy_price) > 0) buys.push([t.buy_date, Number(t.buy_price), t]);
      if (t.status === 'closed' && t.sell_date && Number(t.sell_price) > 0) {
        sells.push([t.sell_date, Number(t.sell_price), t]);
      }
      const stops = t.stops && t.stops[myStopMode];
      if (t.status === 'open' && stops) {
        const hard = Number(stops.hard_stop_price);
        const chandelier = Number(stops.chandelier_stop_price);
        const effective = Math.max(hard, chandelier);
        if (Number.isFinite(effective) && effective > 0) {
          stopLines.push({
            yAxis: effective,
            hoverText: stopHoverText(t, stops, effective),
          });
        }
      }
    }
    const series = [];
    // 精确小圆点锚在 [日期, 实际成交价]；配色避开K线红绿与回测蓝/橙 pin
    if (showMyPoints && buys.length) {
      series.push({
        name: '我的买入',
        type: 'scatter',
        data: buys,
        symbol: 'circle',
        symbolSize: 8,
        itemStyle: { color: '#c026d3', borderColor: '#ffffff', borderWidth: 1.5,
          shadowBlur: 6, shadowColor: 'rgba(17,35,42,0.30)' },
        label: { show: true, formatter: (p) => fmtExact(p.data[1]), position: 'top',
          color: '#c026d3', fontWeight: 800, fontSize: 10,
          textBorderColor: '#ffffff', textBorderWidth: 3 },
        tooltip: { formatter: (p) => {
          const t = p.data[2] || {};
          return `我的买入 ${t.buy_date || ''}<br/>成交价：${fmtExact(t.buy_price)}<br/>份数：${t.shares ?? '-'}`;
        } },
        z: 10,
      });
    }
    if (showMyPoints && sells.length) {
      series.push({
        name: '我的卖出',
        type: 'scatter',
        data: sells,
        symbol: 'circle',
        symbolSize: 8,
        itemStyle: { color: '#0891b2', borderColor: '#ffffff', borderWidth: 1.5,
          shadowBlur: 6, shadowColor: 'rgba(17,35,42,0.30)' },
        label: { show: true, formatter: (p) => fmtExact(p.data[1]), position: 'bottom',
          color: '#0891b2', fontWeight: 800, fontSize: 10,
          textBorderColor: '#ffffff', textBorderWidth: 3 },
        tooltip: { formatter: (p) => {
          const t = p.data[2] || {};
          return `我的卖出 ${t.sell_date || ''}<br/>成交价：${fmtExact(t.sell_price)}<br/>份数：${t.shares ?? '-'}` +
            `<br/>（${t.buy_date} 买入 @ ${fmtExact(t.buy_price)}）`;
        } },
        z: 10,
      });
    }
    if (stopLines.length) {
      series.push({
        name: '我的止损',
        type: 'scatter',
        data: [],
        markLine: {
          symbol: 'none',
          animation: false,
          // 深灰虚线：避开K线红绿与回测蓝/橙，任何缩放级别下都可辨识
          lineStyle: { color: '#4b5563', width: 1.5, type: 'dashed' },
          label: {
            show: true,
            // 放在线的最左端：右端是最新K线密集区，左端通常空旷不遮挡
            position: 'insideStartTop',
            distance: 6,
            formatter: (p) => `止损 ${fmtExact(p.data.yAxis)}`,
            // 深灰底白字药丸：压在K线/均线上也能读清
            color: '#ffffff', fontWeight: 700, fontSize: 10,
            backgroundColor: '#4b5563', borderRadius: 3,
            padding: [2, 6],
          },
          // 悬停只加粗线身；计算说明走 DOM 信息条（stopInfoEl），
          // ECharts label 无论定位在哪一端都可能被 grid 边缘裁剪。
          emphasis: { lineStyle: { width: 2.5 } },
          data: stopLines,
        },
        z: 9,
      });
    }
    return series;
  }

  function renderPrice(payload, zoom) {
    if (!priceChart) return;
    const dates = payload.dates || [];
    const candles = payload.candles || [];
    const indicators = payload.indicators || {};
    currentCandles = candles;
    const overlays = mainOverlaySeries(indicators);

    // Build trade-marker series — only the active 策略×仓位 combo
    let backtestSeries = [];
    if (activeResultKey) {
      const activeKline = allMultiKline.find(sk => resultKey(sk) === activeResultKey);
      if (activeKline) {
        const buys = activeKline.buy_points || [];
        const normalBuys = buys.filter(p => !hasDegradedFlag(p));
        const degradedBuys = buys.filter(p => hasDegradedFlag(p));
        backtestSeries = [
          tradeSeries(normalBuys, '买', '#2563eb', 'pin'),
          tradeSeries(degradedBuys, '买(降级)', '#ea580c', 'pin'),
          tradeSeries(activeKline.sell_points || [], '卖', '#f59e0b', 'pin'),
          skippedBuySeries(activeKline.skipped_buy_points || []),
        ];
      }
    }

    const range = computeLogPriceRange(candles, zoom);

    // Legend: only overlays (MA/BOLL); strategy markers NOT in legend
    const legendData = ['K', ...overlays.map(s => s.name)];

    priceChart.setOption({
      animation: false,
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      tooltip: {
        trigger: 'axis',
        axisPointer: {
          type: 'cross',
          label: {
            // y轴十字标签原始值是全精度浮点（如 2.1580301972），按价格精度格式化
            formatter: (p) => p.axisDimension === 'y' ? num(Number(p.value), 4) : String(p.value),
          },
        },
        formatter: (params) => {
          if (!params || !params.length) return '-';
          const lines = [String(params[0].axisValue || '')];
          for (const p of params) {
            if (p.seriesType === 'candlestick') {
              const raw = candleTooltipValues(p);
              lines.push(`开：${num(raw[0])}`);
              lines.push(`收：${num(raw[1])}`);
              lines.push(`低：${num(raw[2])}`);
              lines.push(`高：${num(raw[3])}`);
              const idx = paramDataIndex(p, dates);
              lines.push(`ATR20：${num(indicators.atr?.['20']?.[idx])}`);
            } else if (p.seriesType === 'line') {
              lines.push(`${p.seriesName}：${num(p.value)}`);
            } else if (p.seriesType === 'scatter') {
              const raw = p.data?.[2] || {};
              if (p.seriesName === '我的买入') {
                lines.push(`我的买入：${fmtExact(raw.buy_price)}（${raw.shares ?? '-'} 份）`);
              } else if (p.seriesName === '我的卖出') {
                lines.push(`我的卖出：${fmtExact(raw.sell_price)}（${raw.shares ?? '-'} 份）`);
              } else {
                lines.push(`${p.seriesName}：${num(raw.price, 4)} / ${raw.qty || 0}`);
              }
            }
          }
          return lines.join('<br/>');
        },
      },
      legend: {
        data: legendData,
        top: 4,
        selected: {
          MA60: false, MA120: false, MA200: false,
          BOLL上轨: false, BOLL中轨: false, BOLL下轨: false,
        },
      },
      grid: { left: 62, right: 28, top: 42, bottom: 54 },
      dataZoom: buildDataZoom(zoom.start, zoom.end),
      xAxis: { type: 'category', data: dates, boundaryGap: false, axisLine: { onZero: false } },
      yAxis: {
        type: 'log',
        logBase: range.logBase,
        min: range.min,
        max: range.max,
        splitNumber: priceAxisSplitCount,
        minorTick: { show: true },
        minorSplitLine: { show: false },
        axisLabel: { formatter: (v) => num(v, 2) },
      },
      series: [
        {
          name: 'K',
          type: 'candlestick',
          data: candles,
          itemStyle: {
            color: '#d14343',
            color0: '#00a76f',
            borderColor: '#d14343',
            borderColor0: '#00a76f',
          },
        },
        ...overlays,
        ...backtestSeries,
        ...myAnnotationSeries(),
      ],
    }, true);
  }

  function renderTrend(payload, zoom) {
    if (!trendChart) return;
    const dates = payload.dates || [];
    const trend = payload.indicators?.trend || {};
    const scores = trend.score || [];
    trendChart.setOption({
      animation: false,
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross' },
        formatter: (params) => {
          if (!params || !params.length) return '-';
          const lines = [String(params[0].axisValue || '')];
          for (const p of params) {
            const raw = Array.isArray(p.value) ? p.value[1] : p.value;
            if (raw == null || raw === '-') continue;
            lines.push(`${p.seriesName}：${num(raw, 2)}`);
          }
          return lines.join('<br/>');
        },
      },
      legend: {
        data: ['Trend', 'Trend MA5', 'Trend MA10'],
        top: 2,
        selected: { 'Trend MA10': false },
      },
      grid: { left: 62, right: 28, top: 36, bottom: 42 },
      dataZoom: buildDataZoom(zoom.start, zoom.end),
      visualMap: {
        show: false,
        type: 'piecewise',
        seriesIndex: 0,
        dimension: 1,
        pieces: [
          { lt: -7.5, color: '#0a6a4c' },
          { gte: -7.5, lt: -5, color: '#16805f' },
          { gte: -5, lt: -2.5, color: '#2d9877' },
          { gte: -2.5, lt: 0, color: '#62b99b' },
          { gte: 0, lt: 5, color: '#df8b86' },
          { gte: 5, lt: 10, color: '#d95c58' },
          { gte: 10, lt: 20, color: '#c43d3b' },
          { gte: 20, color: '#a92828' },
        ],
      },
      xAxis: { type: 'category', data: dates, boundaryGap: false },
      yAxis: {
        type: 'value',
        min: -10,
        max: 30,
        interval: 5,
        axisLabel: { formatter: (v) => num(v, 0) },
        splitLine: { lineStyle: { color: 'rgba(102, 118, 128, 0.16)', width: 1 } },
      },
      series: [
        {
          name: 'Trend',
          type: 'line',
          data: scores.map((v, idx) => [dates[idx], v]),
          symbol: 'none',
          smooth: true,
          lineStyle: { width: 2.6 },
          markLine: {
            silent: true,
            symbol: 'none',
            label: { show: false },
            lineStyle: { color: '#334155', width: 2.6 },
            data: [
              { yAxis: 0, lineStyle: { color: '#1f2937', width: 2.8 } },
              { yAxis: 5, lineStyle: { color: 'rgba(51, 65, 85, 0.62)', width: 1.8 } },
            ],
          },
        },
        { name: 'Trend MA5', type: 'line', data: trend.ma?.['5'] || [], symbol: 'none', smooth: true, lineStyle: { width: 1.15, color: '#7c3aed' }, itemStyle: { color: '#7c3aed' } },
        { name: 'Trend MA10', type: 'line', data: trend.ma?.['10'] || [], symbol: 'none', smooth: true, lineStyle: { width: 1.15, color: '#c7834c' }, itemStyle: { color: '#c7834c' } },
      ],
    }, true);
  }

  function renderVolume(payload, zoom) {
    if (!volumeChart) return;
    const dates = payload.dates || [];
    const candles = payload.candles || [];
    const volumes = payload.volumes || [];
    const indicators = payload.indicators || {};
    const data = volumes.map((v, idx) => ({
      value: v,
      itemStyle: { color: Number(candles[idx]?.[1]) >= Number(candles[idx]?.[0]) ? '#d14343' : '#00a76f' },
    }));
    volumeChart.setOption({
      animation: false,
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      tooltip: { trigger: 'axis', axisPointer: { type: 'cross' }, valueFormatter: compact },
      legend: { data: ['成交量', 'VOLMA5', 'VOLMA10'], top: 2 },
      grid: { left: 62, right: 28, top: 36, bottom: 42 },
      dataZoom: buildDataZoom(zoom.start, zoom.end),
      xAxis: { type: 'category', data: dates, boundaryGap: false },
      yAxis: { type: 'value', scale: true, axisLabel: { formatter: compact } },
      series: [
        { name: '成交量', type: 'bar', data, barMaxWidth: 8 },
        { name: 'VOLMA5', type: 'line', data: indicators.volume_ma?.['5'] || [], symbol: 'none', smooth: true, lineStyle: { width: 1, color: '#7c3aed' }, itemStyle: { color: '#7c3aed' } },
        { name: 'VOLMA10', type: 'line', data: indicators.volume_ma?.['10'] || [], symbol: 'none', smooth: true, lineStyle: { width: 1, color: '#c7834c' }, itemStyle: { color: '#c7834c' } },
      ],
    }, true);
  }

  function renderRsi(payload, zoom) {
    if (!rsiChart) return;
    const dates = payload.dates || [];
    const rsi = payload.indicators?.rsi || {};
    const period = Number(rsi.period || rsiPeriodFromInput() || 14);
    rsiChart.setOption({
      animation: false,
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      tooltip: { trigger: 'axis', axisPointer: { type: 'cross' }, valueFormatter: (v) => num(v, 2) },
      legend: { data: [`RSI${period}`], top: 2 },
      grid: { left: 62, right: 28, top: 36, bottom: 42 },
      dataZoom: buildDataZoom(zoom.start, zoom.end),
      xAxis: { type: 'category', data: dates, boundaryGap: false },
      yAxis: {
        type: 'value',
        min: 0,
        max: 100,
        interval: 20,
        axisLabel: { formatter: (v) => num(v, 0) },
        splitLine: { lineStyle: { color: 'rgba(102, 118, 128, 0.16)', width: 1 } },
      },
      series: [
        {
          name: `RSI${period}`,
          type: 'line',
          data: rsi.series || [],
          symbol: 'none',
          smooth: true,
          lineStyle: { width: 1.7, color: '#2563eb' },
          itemStyle: { color: '#2563eb' },
          markLine: {
            silent: true,
            symbol: 'none',
            label: { show: false },
            data: [
              { yAxis: 70, lineStyle: { color: 'rgba(209, 67, 67, 0.45)', width: 1.4 } },
              { yAxis: 50, lineStyle: { color: 'rgba(51, 65, 85, 0.34)', width: 1.2 } },
              { yAxis: 30, lineStyle: { color: 'rgba(0, 167, 111, 0.45)', width: 1.4 } },
            ],
          },
        },
      ],
    }, true);
  }

  function macdCrossSignals(dates, dif, dea) {
    // Detect DIF/DEA crosses; golden (金叉) = DIF crosses up through DEA,
    // death (死叉) = DIF crosses down. Arrows sit close to the cross point.
    const d = dif || [];
    const e = dea || [];
    const fin = (v) => v !== null && v !== undefined && v !== '' && Number.isFinite(Number(v));
    const vals = [];
    for (const v of [...d, ...e]) {
      if (fin(v)) vals.push(Number(v));
    }
    if (!vals.length) return { golden: [], death: [] };
    const span = Math.max(...vals) - Math.min(...vals);
    const pad = span > 0 ? span * 0.04 : Math.max(Math.abs(vals[0]) * 0.04, 1e-4);

    const golden = [];
    const death = [];
    for (let i = 1; i < d.length; i += 1) {
      if (!fin(d[i - 1]) || !fin(e[i - 1]) || !fin(d[i]) || !fin(e[i])) continue;
      const dPrev = Number(d[i - 1]);
      const ePrev = Number(e[i - 1]);
      const dCur = Number(d[i]);
      const eCur = Number(e[i]);
      const date = dates[i];
      const cross = (dCur + eCur) / 2;  // 交叉点近似取两线中点
      if (dPrev <= ePrev && dCur > eCur) {
        golden.push([date, cross - pad]);
      } else if (dPrev >= ePrev && dCur < eCur) {
        death.push([date, cross + pad]);
      }
    }
    return { golden, death };
  }

  function macdSignalSeries(name, data, color, rotate) {
    return {
      name,
      type: 'scatter',
      data,
      symbol: 'triangle',
      symbolRotate: rotate,
      symbolSize: 10,
      itemStyle: {
        color,
        borderColor: 'rgba(255, 255, 255, 0.9)',
        borderWidth: 0.8,
      },
      emphasis: { scale: 1.4 },
      z: 8,
    };
  }

  function renderMacd(payload, zoom) {
    if (!macdChart) return;
    const dates = payload.dates || [];
    const macd = payload.indicators?.macd || {};
    const bars = (macd.bar || []).map((v) => ({
      value: v,
      itemStyle: { color: Number(v) >= 0 ? '#d14343' : '#00a76f' },
    }));
    const signals = macdCrossSignals(dates, macd.dif, macd.dea);
    macdChart.setOption({
      animation: false,
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross' },
        formatter: (params) => {
          if (!params || !params.length) return '-';
          const lines = [String(params[0].axisValue || '')];
          for (const p of params) {
            if (p.seriesType === 'scatter') {
              lines.push(p.seriesName);
              continue;
            }
            const raw = Array.isArray(p.value) ? p.value[1] : p.value;
            if (raw == null || raw === '-') continue;
            lines.push(`${p.seriesName}：${num(raw, 4)}`);
          }
          return lines.join('<br/>');
        },
      },
      legend: { data: ['MACD', 'DIF', 'DEA'], top: 2 },
      grid: { left: 62, right: 28, top: 36, bottom: 42 },
      dataZoom: buildDataZoom(zoom.start, zoom.end),
      xAxis: { type: 'category', data: dates, boundaryGap: false },
      yAxis: { type: 'value', scale: true },
      series: [
        { name: 'MACD', type: 'bar', data: bars, barMaxWidth: 8 },
        { name: 'DIF', type: 'line', data: macd.dif || [], symbol: 'none', smooth: true, lineStyle: { width: 1.2, color: '#2563eb' }, itemStyle: { color: '#2563eb' } },
        { name: 'DEA', type: 'line', data: macd.dea || [], symbol: 'none', smooth: true, lineStyle: { width: 1.2, color: '#c7834c' }, itemStyle: { color: '#c7834c' } },
        // 金叉暖橙红 / 死叉深青绿：与红绿柱 (#d14343 / #00a76f) 同族但可区分
        macdSignalSeries('金叉', signals.golden, '#c2410c', 0),
        macdSignalSeries('死叉', signals.death, '#0f766e', 180),
      ],
    }, true);
  }

  function renderBias(payload, zoom) {
    if (!biasChart) return;
    const dates = payload.dates || [];
    const bias = payload.indicators?.bias || {};
    biasChart.setOption({
      animation: false,
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      tooltip: { trigger: 'axis', axisPointer: { type: 'cross' }, valueFormatter: (v) => `${num(v, 2)}%` },
      legend: { data: ['BIAS6', 'BIAS12', 'BIAS24'], top: 2 },
      grid: { left: 62, right: 28, top: 36, bottom: 42 },
      dataZoom: buildDataZoom(zoom.start, zoom.end),
      xAxis: { type: 'category', data: dates, boundaryGap: false },
      yAxis: { type: 'value', scale: true, axisLabel: { formatter: (v) => `${num(v, 1)}%` } },
      series: [
        { name: 'BIAS6', type: 'line', data: bias['6'] || [], symbol: 'none', smooth: true, lineStyle: { width: 1.2, color: '#7c3aed' }, itemStyle: { color: '#7c3aed' } },
        { name: 'BIAS12', type: 'line', data: bias['12'] || [], symbol: 'none', smooth: true, lineStyle: { width: 1.2, color: '#0d7a71' }, itemStyle: { color: '#0d7a71' } },
        { name: 'BIAS24', type: 'line', data: bias['24'] || [], symbol: 'none', smooth: true, lineStyle: { width: 1.2, color: '#c7834c' }, itemStyle: { color: '#c7834c' } },
      ],
    }, true);
  }

  function metricRowCells(summary, isBenchmark) {
    const s = summary || {};
    const metrics = [
      ['return', pct(s.total_return)],
      ['return', pct(s.annual_return)],
      ['risk',   pct(s.max_drawdown)],
      ['score',  num(s.sharpe, 3)],
      ['score',  num(s.calmar, 3)],
      ['score',  isBenchmark ? '—' : pct(s.win_rate)],
      ['score',  isBenchmark ? '—' : num(s.profit_factor, 2)],
      ['neutral', String(s.trade_count || 0)],
    ];
    return metrics.map(([tone, value]) => {
      const n = Number(String(value).replace('%', ''));
      const cls = tone === 'risk'
        ? 'metric-risk'
        : (tone !== 'neutral' && Number.isFinite(n) && n > 0 ? 'metric-positive' : '');
      return `<td class="${cls}">${esc(value)}</td>`;
    }).join('');
  }

  function strategyLabel(result) {
    const name = result?.strategy_name
      || (ruleMeta.strategies || []).find(s => s.id === result.strategy_id)?.name
      || result.strategy_id || '-';
    return result?.sizer_name ? `${name} × ${result.sizer_name}` : name;
  }

  function selectChartStrategy(key) {
    if (activeResultKey === key) {
      activeResultKey = null;
    } else {
      activeResultKey = key;
    }
    // Re-render chart with updated markers, preserving current zoom
    if (currentPayload) {
      renderPrice(currentPayload, currentZoom);
    }
    // Update trades table to show active combo's trades
    if (currentBacktest) {
      renderBacktestTrades(currentBacktest);
      renderBacktestReturns(currentBacktest);
    }
    // Update row highlights
    for (const row of backtestSummaryEl.querySelectorAll('tr[data-result-key]')) {
      row.classList.toggle('active', row.dataset.resultKey === activeResultKey);
    }
  }

  function renderBacktestSummary(result) {
    const results = result?.results || [];
    // Fallback for single-result backward compat
    const strategyRows = results.length
      ? results.map(r => {
          const key = esc(resultKey(r));
          return `<tr data-result-key="${key}" class="${activeResultKey === resultKey(r) ? 'active' : ''}"><th>${esc(strategyLabel(r))}</th>${metricRowCells(r.summary, false)}</tr>`;
        }).join('')
      : (result?.summary ? `<tr><th>${esc(result.strategy_id || '策略')}</th>${metricRowCells(result.summary, false)}</tr>` : '');
    const bench = result?.benchmark_summary || {};
    backtestSummaryEl.hidden = false;
    backtestSummaryEl.innerHTML = `
      <table class="backtest-metrics-table">
        <thead>
          <tr>
            <th></th>
            <th>总收益</th>
            <th>年化收益</th>
            <th>最大回撤</th>
            <th>夏普</th>
            <th>卡玛比</th>
            <th>胜率</th>
            <th>盈亏比</th>
            <th>交易数</th>
          </tr>
        </thead>
        <tbody>
          ${strategyRows}
          <tr><th>买入持有</th>${metricRowCells(bench, true)}</tr>
        </tbody>
      </table>
    `;
    // Attach click handlers to strategy rows
    for (const row of backtestSummaryEl.querySelectorAll('tr[data-result-key]')) {
      row.style.cursor = 'pointer';
      row.addEventListener('click', () => {
        selectChartStrategy(row.dataset.resultKey);
      });
    }
  }

  function tradePnlCell(trade) {
    if (String(trade?.side || '').toUpperCase() !== 'SELL') return '';
    const value = Number(trade?.pnl);
    if (!Number.isFinite(value)) return '';
    const cls = value >= 0 ? 'pnl-profit' : 'pnl-loss';
    const sign = value > 0 ? '+' : '';
    return `<span class="${cls}">${sign}${money(value)}</span>`;
  }

  function sideLabel(side) {
    const s = String(side || '').toUpperCase();
    if (s === 'BUY') return '买入';
    if (s === 'SELL') return '卖出';
    return side || '';
  }

  function tradeReturnPctCell(trade) {
    if (String(trade?.side || '').toUpperCase() !== 'SELL') return '';
    const value = Number(trade?.return_pct);
    if (!Number.isFinite(value)) return '-';
    const cls = value >= 0 ? 'pnl-profit' : 'pnl-loss';
    const sign = value > 0 ? '+' : '';
    return `<span class="${cls}">${sign}${num(value, 2)}%</span>`;
  }

  function tradePlainPctCell(trade, key) {
    if (String(trade?.side || '').toUpperCase() !== 'SELL') return '';
    const value = Number(trade?.[key]);
    if (!Number.isFinite(value)) return '-';
    const sign = value > 0 ? '+' : '';
    return `${sign}${num(value, 2)}%`;
  }

  function calendarHoldingDays(buyDate, sellDate) {
    const buy = new Date(`${buyDate}T00:00:00`);
    const sell = new Date(`${sellDate}T00:00:00`);
    if (Number.isNaN(buy.getTime()) || Number.isNaN(sell.getTime())) return null;
    return Math.max(0, Math.round((sell.getTime() - buy.getTime()) / 86400000));
  }

  function tradingHoldingDays(buyDate, sellDate) {
    const dates = currentPayload?.dates || [];
    const buyIdx = dates.indexOf(buyDate);
    const sellIdx = dates.indexOf(sellDate);
    if (buyIdx >= 0 && sellIdx >= 0 && sellIdx >= buyIdx) return sellIdx - buyIdx;
    return calendarHoldingDays(buyDate, sellDate);
  }

  function tradeExcursionMetrics(buyTrade, sellTrade) {
    const empty = { return_pct: null, max_profit_pct: null, max_drawdown_pct: null };
    const buyPrice = Number(buyTrade?.exec_price);
    const qty = Number(buyTrade?.qty) || Number(sellTrade?.qty);
    if (!Number.isFinite(buyPrice) || buyPrice <= 0 || !Number.isFinite(qty) || qty <= 0) return empty;

    // 涨跌幅：本次交易收益 / 买入成本（含买入佣金）
    const pnl = Number(sellTrade?.pnl);
    const cost = buyPrice * qty + (Number(buyTrade?.commission) || 0);
    const returnPct = Number.isFinite(pnl) && cost > 0 ? (pnl / cost) * 100 : null;

    // 最大浮盈 / 最大回撤：基于持有期间的最高价 / 最低价
    const dates = currentPayload?.dates || [];
    const candles = currentPayload?.candles || [];
    const buyIdx = dates.indexOf(buyTrade?.date);
    const sellIdx = dates.indexOf(sellTrade?.date);
    if (buyIdx < 0 || sellIdx < 0 || sellIdx < buyIdx) {
      return { ...empty, return_pct: returnPct };
    }
    let maxProfit = 0;
    let maxDrawdown = 0;
    let peak = buyPrice;
    for (let i = buyIdx; i <= sellIdx; i += 1) {
      const c = candles[i] || [];
      const high = Number(c[3]);
      const low = Number(c[2]);
      if (Number.isFinite(high) && high > 0) {
        maxProfit = Math.max(maxProfit, ((high - buyPrice) / buyPrice) * 100);
        peak = Math.max(peak, high);
      }
      if (Number.isFinite(low) && low > 0 && peak > 0) {
        maxDrawdown = Math.max(maxDrawdown, ((peak - low) / peak) * 100);
      }
    }
    return {
      return_pct: returnPct,
      max_profit_pct: maxProfit,
      max_drawdown_pct: -maxDrawdown,
    };
  }

  function attachHoldingDays(rawTrades) {
    const openBuys = [];
    return rawTrades.map((trade) => {
      const side = String(trade?.side || '').toUpperCase();
      if (side === 'BUY') {
        openBuys.push(trade);
        return { ...trade, holding_days: null };
      }
      if (side !== 'SELL') return { ...trade, holding_days: null };
      const buy = openBuys.shift();
      const days = buy ? tradingHoldingDays(buy.date, trade.date) : null;
      const metrics = buy
        ? tradeExcursionMetrics(buy, trade)
        : { return_pct: null, max_profit_pct: null, max_drawdown_pct: null };
      return { ...trade, holding_days: days, ...metrics };
    });
  }

  function holdingDaysCell(trade) {
    if (String(trade?.side || '').toUpperCase() !== 'SELL') return '';
    const days = Number(trade?.holding_days);
    return Number.isFinite(days) ? String(days) : '-';
  }

  function exitReasonText(trade) {
    if (String(trade?.side || '').toUpperCase() !== 'SELL') return '';
    const reason = String(trade?.reason || '').trim();
    const map = {
      hard_stop: '硬止损',
      chandelier_stop: '吊灯止损',
      chandelier_stop_ratchet: '棘轮吊灯止损',
      exit_conditions_passed: '卖出条件触发',
    };
    return map[reason] || reason || '-';
  }

  function pickBacktestSource(result) {
    // Use the active combo's result if available, otherwise the top-level (first) result
    if (activeResultKey && result?.results) {
      const active = result.results.find(r => resultKey(r) === activeResultKey);
      if (active) return active;
    }
    return result;
  }

  function ensureHeatChart() {
    if (heatChart) return heatChart;
    const el = document.getElementById('mvHeatChart');
    if (!el) return null;
    try {
      heatChart = echarts.init(el);
    } catch (err) {
      console.warn('月度收益热力图初始化失败：', err);
      heatChart = null;
    }
    return heatChart;
  }

  function metricTone(value, tone = 'neutral') {
    const n = Number(value);
    if (!Number.isFinite(n) || tone === 'neutral') return '';
    if (tone === 'risk') return n < 0 ? 'metric-risk' : '';
    if (n > 0) return 'metric-positive';
    if (n < 0) return 'metric-negative';
    return '';
  }

  function renderMonthlyHeatmap(monthly) {
    const chart = ensureHeatChart();
    if (!chart) return;
    const years = Array.isArray(monthly?.years) ? monthly.years : [];
    const months = Array.isArray(monthly?.months) && monthly.months.length
      ? monthly.months
      : ['01', '02', '03', '04', '05', '06', '07', '08', '09', '10', '11', '12'];
    const data = Array.isArray(monthly?.data) ? monthly.data : [];

    let absMax = 1;
    for (const row of data) {
      const v = Number(row?.[2]);
      if (Number.isFinite(v)) absMax = Math.max(absMax, Math.abs(v));
    }

    chart.setOption({
      tooltip: { formatter: p => `${years[p.value[1]]}-${months[p.value[0]]}: ${Number(p.value[2]).toFixed(2)}%` },
      grid: { left: 62, right: 28, top: 20, bottom: 70 },
      xAxis: { type: 'category', data: months },
      yAxis: { type: 'category', data: years.map(String) },
      visualMap: {
        min: -absMax,
        max: absMax,
        calculable: true,
        orient: 'horizontal',
        left: 'center',
        bottom: 0,
        inRange: {
          color: ['#0b8a4a', '#86cfaa', '#f7f7f7', '#f0a0a0', '#c81010']
        },
      },
      series: [{
        type: 'heatmap',
        data,
        label: { show: true, formatter: p => `${Number(p.value[2]).toFixed(1)}%` }
      }],
      graphic: data.length ? [] : [{
        type: 'text',
        left: 'center',
        top: 'middle',
        style: { text: '暂无月度收益数据', fill: '#6b7280', fontSize: 14 },
      }],
    }, true);
    setTimeout(() => { if (heatChart) heatChart.resize(); }, 0);
  }

  function valueWithDiff(value, benchmark, formatter) {
    // 主值 + 括号内与买入持有的差值；差值为正红色、为负绿色（A 股配色）
    const v = Number(value);
    if (!Number.isFinite(v)) return '-';
    let html = `<span class="annual-value">${esc(formatter(v))}</span>`;
    const b = Number(benchmark);
    if (Number.isFinite(b)) {
      const d = v - b;
      const sign = d > 0 ? '+' : '';
      const cls = d > 0 ? 'pnl-profit' : (d < 0 ? 'pnl-loss' : 'annual-diff-flat');
      html += `<span class="annual-diff ${cls}">(${sign}${esc(formatter(d))})</span>`;
    }
    return html;
  }

  function renderAnnualTable(rows, strategyName) {
    if (!annualTableHeadEl || !annualTableBodyEl) return;
    annualTableHeadEl.innerHTML = `
      <tr>
        <th>年份</th>
        <th>收益</th>
        <th>夏普</th>
        <th>最大回撤</th>
        <th>卡玛比</th>
        <th>交易数</th>
        <th>胜率</th>
        <th>盈亏比</th>
      </tr>
    `;

    if (!rows || !rows.length) {
      annualTableBodyEl.innerHTML = '<tr><td colspan="8">暂无年度收益数据</td></tr>';
      return;
    }
    annualTableBodyEl.innerHTML = rows.map((r) => `
      <tr>
        <th scope="row" class="year-cell">${r.year ?? '-'}</th>
        <td class="num-cell ${metricTone(r.return, 'return')}">${valueWithDiff(r.return, r.benchmark_return, pct)}</td>
        <td class="num-cell ${metricTone(r.sharpe, 'score')}">${valueWithDiff(r.sharpe, r.benchmark_sharpe, (v) => num(v, 2))}</td>
        <td class="num-cell ${metricTone(r.max_drawdown, 'risk')}">${valueWithDiff(r.max_drawdown, r.benchmark_max_drawdown, pct)}</td>
        <td class="num-cell ${metricTone(r.calmar, 'score')}">${valueWithDiff(r.calmar, r.benchmark_calmar, (v) => num(v, 2))}</td>
        <td class="num-cell">${Number.isFinite(Number(r.trade_count)) ? Math.round(Number(r.trade_count)) : '-'}</td>
        <td class="num-cell ${metricTone(r.win_rate, 'score')}">${pct(r.win_rate)}</td>
        <td class="num-cell ${metricTone(r.profit_factor, 'score')}">${num(r.profit_factor, 2)}</td>
      </tr>
    `).join('');
  }

  function renderBacktestReturns(result) {
    if (!returnsPanelEl) return;
    const source = pickBacktestSource(result);
    const strategyName = source?.strategy_name || strategyLabel(source) || '';
    if (heatTitleEl) heatTitleEl.textContent = `月度收益热力图${strategyName ? `（${strategyName}）` : ''}`;
    if (annualTitleEl) annualTitleEl.textContent = `年度收益表${strategyName ? `（${strategyName}）` : ''}`;
    returnsPanelEl.hidden = false;
    renderMonthlyHeatmap(source?.monthly_heatmap || {});
    renderAnnualTable(source?.annual_returns || [], strategyName);
  }

  function clearBacktestReturns() {
    if (returnsPanelEl) returnsPanelEl.hidden = true;
    if (heatChart) {
      heatChart.clear();
    }
    if (annualTableHeadEl) annualTableHeadEl.innerHTML = '';
    if (annualTableBodyEl) annualTableBodyEl.innerHTML = '';
  }

  // 清空回测相关状态与全部回测面板，恢复标的查看页初始样貌
  // （换标的或点刷新按钮时调用；不触碰已输入的标的）。
  function clearBacktestState(meta = {}) {
    currentBacktest = null;
    allMultiKline = [];
    activeResultKey = null;
    backtestSummaryEl.hidden = true;
    backtestStartEl.value = meta.start || '';
    backtestEndEl.value = meta.end || '';
    updateBacktestRangeLabel();
    tradeMetaEl.textContent = '运行回测后显示。';
    tradesBodyEl.innerHTML = '<tr><td colspan="14">暂无交易明细。</td></tr>';
    clearBacktestReturns();
    debugInfoEl.textContent = '暂无结果。';
    debugPreviewEl.textContent = '';
  }

  function positionPctCell(trade) {
    if (String(trade?.side || '').toUpperCase() !== 'BUY') return '';
    const sizing = trade?.sizing;
    if (!sizing) return '<span class="text-muted">全仓</span>';
    const value = Number(sizing.position_pct);
    return Number.isFinite(value) ? pct(value) : '-';
  }

  function sizingBadge(trade) {
    const sizing = trade?.sizing;
    if (!sizing) return '';
    const flags = sizing.flags || [];
    if (!flags.length) return '';
    const degraded = flags.some(f => SIZING_DEGRADED_FLAGS.has(f));
    const text = flags.map(flagText).join('、');
    const cls = degraded ? 'sizing-badge sizing-badge--degraded' : 'sizing-badge';
    return ` <span class="${cls}" title="${esc(sizing.note || text)}">${esc(degraded ? '降级' : '提示')}</span>`;
  }

  function skippedTradeRow(skip) {
    const reason = SKIP_REASON_TEXT[skip.reason] || skip.reason || '-';
    return `
      <tr class="trade-row-skipped" title="${esc(skip.note || '')}">
        <td>${esc(skip.date || '')}</td>
        <td class="text-center">跳过</td>
        <td class="num-cell">0</td>
        <td class="num-cell">-</td>
        <td class="num-cell">${num(skip.close, 4)}</td>
        <td class="num-cell">-</td>
        <td class="num-cell">-</td>
        <td class="num-cell">-</td>
        <td class="num-cell">-</td>
        <td class="num-cell">-</td>
        <td class="num-cell">${esc(reason)}</td>
        <td class="num-cell">-</td>
        <td class="num-cell">-</td>
        <td class="num-cell">-</td>
      </tr>
    `;
  }

  function renderBacktestTrades(result) {
    const tradesSource = pickBacktestSource(result);
    const trades = attachHoldingDays(tradesSource?.trades || []);
    const skipped = (tradesSource?.skipped_buys || []).map(s => ({ ...s, _skipped: true }));
    const rows = [...trades, ...skipped]
      .sort((a, b) => String(b.date || '').localeCompare(String(a.date || '')));
    const strategyName = tradesSource?.strategy_name || strategyLabel(tradesSource) || '';
    const label = tradesSource?.sizer_name ? `${strategyName} × ${tradesSource.sizer_name}` : strategyName;
    tradeMetaEl.textContent = rows.length
      ? `${label ? label + ' | ' : ''}${tradesSource.start_date || '-'} ~ ${tradesSource.end_date || '-'} | ${trades.length} 笔交易${skipped.length ? ` + ${skipped.length} 次跳过` : ''}`
      : '本次回测无交易。';
    tradesBodyEl.innerHTML = rows.length ? rows.map((t) => {
      if (t._skipped) return skippedTradeRow(t);
      const degraded = (t.sizing?.flags || []).some(f => SIZING_DEGRADED_FLAGS.has(f));
      return `
      <tr class="${degraded ? 'trade-row-degraded' : ''}">
        <td>${esc(t.date || '')}</td>
        <td class="text-center">${esc(sideLabel(t.side))}${sizingBadge(t)}</td>
        <td class="num-cell">${esc(t.qty || 0)}</td>
        <td class="num-cell">${positionPctCell(t)}</td>
        <td class="num-cell">${num(t.reference_price, 4)}</td>
        <td class="num-cell">${num(t.exec_price, 4)}</td>
        <td class="num-cell">${tradePnlCell(t)}</td>
        <td class="num-cell">${tradeReturnPctCell(t)}</td>
        <td class="num-cell">${tradePlainPctCell(t, 'max_profit_pct')}</td>
        <td class="num-cell">${tradePlainPctCell(t, 'max_drawdown_pct')}</td>
        <td class="num-cell">${exitReasonText(t)}</td>
        <td class="num-cell">${holdingDaysCell(t)}</td>
        <td class="num-cell">${money(t.commission)}</td>
        <td class="num-cell">${money(t.stamp_tax)}</td>
      </tr>
    `; }).join('') : '<tr><td colspan="14">本次回测无交易。</td></tr>';
  }

  function renderBacktestDebug(result) {
    const debugLog = result?.debug_log || [];
    debugInfoEl.textContent = debugLog.length ? `debug 日志 ${debugLog.length} 天。` : '暂无 debug 日志。';
    debugPreviewEl.textContent = debugLog.length ? JSON.stringify(debugLog.slice(0, 3), null, 2) : '';
  }

  function renderBacktestResult(result) {
    currentBacktest = result;
    allMultiKline = result?.multi_kline || [];
    // If only one strategy, create a synthetic multi_kline entry from the single result
    if (!allMultiKline.length && result?.charts?.kline) {
      allMultiKline = [{
        strategy_id: result.strategy_id || '',
        strategy_name: strategyLabel({ strategy_id: result.strategy_id }) || result.strategy_id || '',
        sizer_id: result.sizer_id || '',
        sizer_name: result.sizer_name || '',
        buy_points: result.charts.kline.buy_points || [],
        sell_points: result.charts.kline.sell_points || [],
        skipped_buy_points: result.charts.kline.skipped_buy_points || [],
      }];
    }
    // 单一 策略×仓位 组合时直接激活其买卖点标记（用户预期：回测跑完即见策略买卖点）；
    // 多组合仍需点击对比表行选择显示哪一组（既有交互）。
    activeResultKey = allMultiKline.length === 1 ? resultKey(allMultiKline[0]) : null;
    renderBacktestSummary(result);
    renderBacktestTrades(result);
    renderBacktestReturns(result);
    renderBacktestDebug(result);
    if (currentPayload) {
      currentZoom = zoomForDateRange(currentPayload.dates || [], result.start_date, result.end_date);
      renderAll(currentPayload, true);
    }
  }

  // 我的止损档位开关：仅当当前标的存在带止损数据的未平仓持仓时显示
  function updateStopModeToggle() {
    stopModeToggleEl.hidden = !(myTrades?.trades || []).some(
      (t) => t.status === 'open' && t.stops
    );
  }

  function renderAll(payload, preserveZoom = false) {
    const skeletonEl = document.getElementById('mvChartSkeleton');
    if (skeletonEl) skeletonEl.hidden = true;
    const dates = payload.dates || [];
    const zoom = preserveZoom && currentZoom ? currentZoom : oneYearZoom(dates);
    currentZoom = zoom;
    currentPayload = payload;
    applyTrendConfigToInputs(payload.meta?.trend_config || {});
    applyRsiConfigToInputs(payload.meta?.rsi_config || {});
    if (!backtestStartEl.value && payload.meta?.start) backtestStartEl.value = payload.meta.start;
    if (!backtestEndEl.value && payload.meta?.end) backtestEndEl.value = payload.meta.end;
    updateBacktestEndMax();
    updateBacktestRangeLabel();
    // Intraday badge: "盘中实时" while the session is running, "收盘估算"
    // after 15:00 when the bar is synthesized from the closing snapshot
    // because the daily write job has not persisted today's bar yet.
    // 优先用服务端 post_close 标志（浏览器时区解析 ts 会误判）。
    const intradayTs = payload.meta?.intraday_ts ? new Date(payload.meta.intraday_ts) : null;
    const afterClose = payload.meta?.post_close === true ||
      (payload.meta?.post_close == null && intradayTs && !Number.isNaN(intradayTs.getTime()) && intradayTs.getHours() >= 15);
    const intradayLabel = afterClose ? '收盘估算' : '盘中实时';
    chartTitleEl.innerHTML = `${esc(payload.display_label || payload.display_name || payload.symbol || '')} 日 K${payload.meta?.is_intraday ? ` <span class="intraday-badge"><span class="intraday-dot" style="animation:intraday-pulse 1.6s ease-in-out infinite"></span>${intradayLabel}</span>` : ''}`;
    rangeMetaEl.textContent = `${payload.meta?.start || '-'} ~ ${payload.meta?.end || '-'} | ${Number(payload.meta?.rows || 0)} 根${payload.meta?.is_intraday ? ` · 含${intradayLabel}数据` : ''}`;
    // 我的止损档位开关：仅当当前标的存在带止损数据的未平仓持仓时显示
    updateStopModeToggle();
    renderPrice(payload, zoom);
    renderTrend(payload, zoom);
    renderVolume(payload, zoom);
    renderRsi(payload, zoom);
    renderBias(payload, zoom);
    renderMacd(payload, zoom);
    setTimeout(() => { if (charts.length) charts.forEach((chart) => chart.resize()); }, 0);
  }

  function normalizeSearchText(value) {
    return String(value || '').trim().toLowerCase();
  }

  function symbolOptionLabel(item) {
    const display = String(item?.display_label || item?.display_name || item?.name || item?.symbol || '').trim();
    const symbol = String(item?.symbol || '').trim();
    return display && display !== symbol ? `${display} ${symbol}` : symbol;
  }

  function symbolSearchHaystack(item) {
    return [
      item?.symbol,
      item?.name,
      item?.display_name,
      item?.display_label,
      item?.category_path,
      ...(item?.factor_tags || []),
      symbolOptionLabel(item),
    ].map(normalizeSearchText).join(' ');
  }

  function renderSymbolOptions(keyword = '') {
    const q = normalizeSearchText(keyword);
    const items = (allSymbols || [])
      .filter((item) => !q || symbolSearchHaystack(item).includes(q))
      .slice(0, 120);
    symbolOptionsEl.innerHTML = items
      .map((item) => `<option value="${esc(symbolOptionLabel(item))}"></option>`)
      .join('');
  }

  function findSymbolByInput(rawValue) {
    const q = normalizeSearchText(rawValue);
    if (!q) return null;
    const exact = allSymbols.find((item) => {
      const values = [
        item?.symbol,
        item?.name,
        item?.display_name,
        item?.display_label,
        symbolOptionLabel(item),
      ].map(normalizeSearchText);
      return values.includes(q);
    });
    if (exact) return exact;
    return allSymbols.find((item) => symbolSearchHaystack(item).includes(q)) || null;
  }

  function applySelectedSymbol(item) {
    if (!item) return;
    symbolSearchEl.value = symbolOptionLabel(item);
    symbolInputEl.value = item.symbol || '';
    renderSymbolOptions(symbolSearchEl.value);
  }

  async function loadSymbols() {
    const resp = await fetch('/market-view/api/symbols');
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '标的列表加载失败');
    allSymbols = data.items || [];
    renderSymbolOptions();
    if (!allSymbols.length) {
      chartTitleEl.textContent = '没有找到本地日 K 数据';
      rangeMetaEl.textContent = '';
      return;
    }
    // 不默认加载任何标的：等待用户输入代码/名称后再展示（2026-08 需求）
    chartTitleEl.textContent = '请输入标的代码或名称';
    rangeMetaEl.textContent = '';
  }

  function selectedStrategyIds() {
    return [...backtestStrategyListEl.querySelectorAll('input[type="checkbox"]:checked')]
      .map(cb => cb.value)
      .filter(Boolean);
  }

  function selectedSizerIds() {
    return [...backtestSizerListEl.querySelectorAll('input[type="checkbox"]:checked')]
      .map(cb => cb.value)
      .filter(Boolean);
  }

  function updateSizerDropdownLabel() {
    const ids = selectedSizerIds();
    const items = (ruleMeta.position_strategies || []).filter(s => s.valid);
    if (!ids.length) {
      backtestSizerLabelEl.textContent = '全仓（默认）';
    } else if (ids.length === 1) {
      const found = items.find(s => s.id === ids[0]);
      backtestSizerLabelEl.textContent = found ? (found.name || found.id) : ids[0];
    } else {
      backtestSizerLabelEl.textContent = `已选 ${ids.length} 个仓位策略`;
    }
  }

  function updateStrategyDropdownLabel() {
    const ids = selectedStrategyIds();
    const strategies = (ruleMeta.strategies || []).filter(s => s.valid);
    if (!ids.length) {
      backtestStrategyLabelEl.textContent = '选择策略';
    } else if (ids.length === 1) {
      const found = strategies.find(s => s.id === ids[0]);
      backtestStrategyLabelEl.textContent = found ? (found.name || found.id) : ids[0];
    } else {
      backtestStrategyLabelEl.textContent = `已选 ${ids.length} 个策略`;
    }
  }

  async function loadRuleMeta() {
    const resp = await fetch('/rule-backtest/api/meta');
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '策略列表加载失败');
    ruleMeta = data || { strategies: [] };
    // Sync degradation enums from the backend (single source of truth).
    const sizingMeta = ruleMeta.sizing || {};
    if (Array.isArray(sizingMeta.degraded_flags) && sizingMeta.degraded_flags.length) {
      SIZING_DEGRADED_FLAGS = new Set(sizingMeta.degraded_flags);
    }
    const strategies = (ruleMeta.strategies || []).filter((item) => item.valid);
    backtestStrategyListEl.innerHTML = strategies.length
      ? strategies.map((item) => `
        <label class="multi-select-option">
          <input type="checkbox" value="${esc(item.id)}" ${strategies.length === 1 ? 'checked' : ''}>
          <span>${esc(item.name || item.id)}</span>
        </label>
      `).join('')
      : '<div class="multi-select-empty">暂无可用策略</div>';
    updateStrategyDropdownLabel();
    runBacktestBtnEl.disabled = !strategies.length;

    const sizers = (ruleMeta.position_strategies || []).filter((item) => item.valid);
    backtestSizerListEl.innerHTML = sizers.length
      ? sizers.map((item) => `
        <label class="multi-select-option">
          <input type="checkbox" value="${esc(item.id)}">
          <span>${esc(item.name || item.id)}</span>
        </label>
      `).join('')
      : '<div class="multi-select-empty">暂无仓位策略，默认全仓</div>';
    updateSizerDropdownLabel();
  }

  function selectedSymbol() {
    const matched = findSymbolByInput(symbolSearchEl.value);
    return String(matched?.symbol || symbolInputEl.value || '').trim();
  }



  // 拉取当前用户在该标的上的交易标注（买卖点 + 双档止损）。
  // 失败（非 401）时静默降级为无标注，不影响日K主流程。
  async function loadMyTrades(symbol) {
    try {
      const resp = await fetch(`/market-view/api/my-trades?symbol=${encodeURIComponent(symbol)}`);
      if (resp.status === 401) { redirectToLogin(); return null; }
      if (!resp.ok) return null;
      return await resp.json();
    } catch (err) {
      return null;
    }
  }

  async function loadDaily(symbol) {
    const target = String(symbol || selectedSymbol()).trim();
    if (!target) {
      chartTitleEl.textContent = '请输入标的代码或名称';
      rangeMetaEl.textContent = '';
      return;
    }
    refreshBtnEl.disabled = true;
    stopInfoEl.hidden = true;
    try {
      const previousSymbol = currentPayload?.symbol || '';
      const params = new URLSearchParams();
      params.set('symbol', target);
      appendTrendParams(params);
      appendRsiParams(params);
      // 两阶段加载：阶段1 纯DB日K（本地毫秒级）立即渲染保证秒开；
      // 阶段2 带盘中合成K线（需一次 tickflow 报价，实测 RTT 约3秒）后台补齐，
      // 持仓标注同样含报价请求，三者并行，都不阻塞首渲染。
      const resp = await fetch(`/market-view/api/daily?${params.toString()}`);
      if (resp.status === 401) { redirectToLogin(); return; }
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || '日 K 加载失败');
      myTrades = null;
      // 用服务端归一化后的代码拉取，口径与主数据一致
      const myTradesPromise = loadMyTrades(data.symbol);
      const liveParams = new URLSearchParams(params);
      liveParams.set('intraday', 'true');
      const liveDailyPromise = (async () => {
        try {
          const r = await fetch(`/market-view/api/daily?${liveParams.toString()}`);
          if (r.status === 401) { redirectToLogin(); return null; }
          if (!r.ok) return null;
          return await r.json();
        } catch (err) {
          return null;
        }
      })();
      const matched = allSymbols.find((item) => item.symbol === data.symbol) || {
        symbol: data.symbol,
        name: data.name,
        display_name: data.display_name,
        display_label: data.display_label,
        category_path: data.meta?.category_path || '',
      };
      applySelectedSymbol(matched);
      if (previousSymbol && previousSymbol !== data.symbol) {
        clearBacktestState(data.meta || {});
      }
      renderAll(data, false);
      const [trades, liveData] = await Promise.all([myTradesPromise, liveDailyPromise]);
      // 等待期间用户可能已切换标的，仅当仍在同一标的时补画
      if (currentPayload?.symbol !== data.symbol) return;
      // 先挂交易标注再渲染：有盘中合成K线时整图替换的一次渲染即含买卖点，
      // 不再重复第三次 renderPrice（P2-20：单标的加载 3 次全图重渲染 → 最多 2 次）
      if (trades) {
        myTrades = trades;
        updateStopModeToggle();
      }
      if (liveData && liveData.meta?.is_intraday) {
        renderAll(liveData, true);
      } else if (trades) {
        renderPrice(currentPayload, currentZoom);
      }
    } catch (err) {
      chartTitleEl.textContent = `加载失败：${err.message}`;
      rangeMetaEl.textContent = '';
    } finally {
      refreshBtnEl.disabled = false;
    }
  }

  async function pollRuleBacktest(runId) {
    const STALL_LIMIT_MS = 120000;
    const MAX_CONSECUTIVE_FETCH_ERRORS = 10;
    let lastCurrent = -1;
    let lastChangeAt = Date.now();
    let fetchErrors = 0;
    while (true) {
      await new Promise((r) => setTimeout(r, 500));
      let progResp = null;
      try {
        progResp = await fetch(`/rule-backtest/api/progress/${encodeURIComponent(runId)}`);
      } catch (err) {
        fetchErrors += 1;
        if (fetchErrors >= MAX_CONSECUTIVE_FETCH_ERRORS) {
          throw new Error('进度查询连续失败，请检查服务状态后重试');
        }
        if (Date.now() - lastChangeAt > STALL_LIMIT_MS) {
          throw new Error('回测进度长时间无更新，可能已中断，请重试');
        }
        continue;
      }
      fetchErrors = 0;
      if (progResp.status === 404) {
        throw new Error('回测任务不存在或已过期（服务可能已重启），请重试');
      }
      const progData = await progResp.json();
      if (!progResp.ok) throw new Error(progData.detail || '进度查询失败');

      const current = Number(progData.progress_current || 0);
      const total = Number(progData.progress_total || 1);
      if (current !== lastCurrent) {
        lastCurrent = current;
        lastChangeAt = Date.now();
      }
      updateBacktestProgress(current, total);

      if (progData.status !== 'running') {
        if (progData.status === 'error') throw new Error(progData.error || '回测运行出错');
        // Result is fetched separately so polling stays lightweight.
        const resultResp = await fetch(`/rule-backtest/api/result/${encodeURIComponent(runId)}`);
        const resultData = await resultResp.json();
        if (!resultResp.ok) throw new Error(resultData.detail || '回测结果获取失败');
        return resultData;
      }
      if (Date.now() - lastChangeAt > STALL_LIMIT_MS) {
        throw new Error('回测进度长时间无更新，可能已中断，请重试');
      }
    }
  }

  // Batch drill-down context (批量回测快照重跑模式): when set, the backtest
  // runs the frozen strategy payload from the batch snapshot instead of the
  // current DB version, over the batch-recorded date range.
  let drillContext = null;

  async function runRuleBacktest() {
    const symbol = selectedSymbol();
    const strategyIds = drillContext ? [] : selectedStrategyIds();
    if (!symbol || (!strategyIds.length && !drillContext)) return;
    runBacktestBtnEl.disabled = true;
    runBacktestBtnEl.textContent = '回测中';
    showBacktestProgress();
    let ok = false;
    try {
      const resp = await fetch('/rule-backtest/api/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          strategy_ids: strategyIds,
          strategy_config: drillContext ? drillContext.strategy_config : null,
          position_strategy_ids: selectedSizerIds(),
          symbol,
          start_date: backtestStartEl.value,
          end_date: backtestEndEl.value,
          initial_capital: 100000,
          slippage: 0.002,
          fee_rate: 0.0000854,
          fee_min: 5,
          lot_size: 100,
          debug_log_enabled: null,
        }),
      });
      const startData = await resp.json();
      if (!resp.ok || !startData.run_id) throw new Error(startData.detail || '回测启动失败');
      const data = await pollRuleBacktest(startData.run_id);
      renderBacktestResult(data);
      ok = true;
    } catch (err) {
      tradeMetaEl.textContent = `回测失败：${err.message}`;
    } finally {
      finishBacktestProgress(ok);
      runBacktestBtnEl.disabled = !(ruleMeta.strategies || []).some((item) => item.valid);
      runBacktestBtnEl.textContent = '运行回测';
    }
  }

  try {
    for (const chart of charts) {
      chart.on('dataZoom', updatePriceYAxis);
    }
  } catch (err) {
    // charts not initialized – listeners skipped
  }
  // 刷新 = 清掉回测痕迹（结果表/交易明细/收益面板/图上策略买卖点）+ 重新拉日K，
  // 已输入的标的保留。
  refreshBtnEl.addEventListener('click', () => {
    clearBacktestState();
    loadDaily();
  });

  // 我的止损紧/松切换（与手工交易页同一 seg 交互）：只重渲染价格图，不重拉数据
  stopModeToggleEl.addEventListener('click', (ev) => {
    const btn = ev.target.closest('button[data-mode]');
    if (!btn || btn.dataset.mode === myStopMode) return;
    myStopMode = btn.dataset.mode;
    stopModeToggleEl.querySelectorAll('button[data-mode]').forEach((b) => {
      b.classList.toggle('is-active', b.dataset.mode === myStopMode);
    });
    if (currentPayload) renderPrice(currentPayload, currentZoom);
  });
  // 盘后数据更新完成后自动重载当前图表（无选中标的时 loadDaily 内部安全返回）。
  document.addEventListener('daily-update-done', () => loadDaily());
  runBacktestBtnEl.addEventListener('click', runRuleBacktest);

  // --- Strategy dropdown (checkbox panel) ---
  backtestStrategyBtnEl.addEventListener('click', (event) => {
    event.stopPropagation();
    if (backtestStrategyPanelEl.hidden) {
      const rect = backtestStrategyBtnEl.getBoundingClientRect();
      backtestStrategyPanelEl.style.top = (rect.bottom + 4) + 'px';
      backtestStrategyPanelEl.style.left = rect.left + 'px';
      backtestStrategyPanelEl.style.minWidth = rect.width + 'px';
      backtestStrategyPanelEl.hidden = false;
      backtestStrategyBtnEl.classList.add('multi-select-open');
    } else {
      backtestStrategyPanelEl.hidden = true;
      backtestStrategyBtnEl.classList.remove('multi-select-open');
    }
  });

  backtestStrategyListEl.addEventListener('change', () => {
    updateStrategyDropdownLabel();
  });

  // --- Sizer dropdown (checkbox panel) ---
  backtestSizerBtnEl.addEventListener('click', (event) => {
    event.stopPropagation();
    if (backtestSizerPanelEl.hidden) {
      const rect = backtestSizerBtnEl.getBoundingClientRect();
      backtestSizerPanelEl.style.top = (rect.bottom + 4) + 'px';
      backtestSizerPanelEl.style.left = rect.left + 'px';
      backtestSizerPanelEl.style.minWidth = rect.width + 'px';
      backtestSizerPanelEl.hidden = false;
      backtestSizerBtnEl.classList.add('multi-select-open');
    } else {
      backtestSizerPanelEl.hidden = true;
      backtestSizerBtnEl.classList.remove('multi-select-open');
    }
  });

  backtestSizerListEl.addEventListener('change', () => {
    updateSizerDropdownLabel();
  });

  document.addEventListener('click', (event) => {
    if (!backtestStrategyPanelEl.contains(event.target) && !backtestStrategyDropdownEl.contains(event.target)) {
      backtestStrategyPanelEl.hidden = true;
      backtestStrategyBtnEl.classList.remove('multi-select-open');
    }
    if (!backtestSizerPanelEl.contains(event.target) && !backtestSizerDropdownEl.contains(event.target)) {
      backtestSizerPanelEl.hidden = true;
      backtestSizerBtnEl.classList.remove('multi-select-open');
    }
  });
  backtestStartEl.addEventListener('change', updateBacktestRangeLabel);
  backtestEndEl.addEventListener('change', updateBacktestRangeLabel);
  trendControlsEl.addEventListener('submit', (event) => {
    event.preventDefault();
    loadDaily();
  });
  rsiControlsEl.addEventListener('submit', (event) => {
    event.preventDefault();
    loadDaily();
  });
  symbolSearchEl.addEventListener('input', () => {
    renderSymbolOptions(symbolSearchEl.value);
    const matched = findSymbolByInput(symbolSearchEl.value);
    if (matched) symbolInputEl.value = matched.symbol || '';
  });
  symbolSearchEl.addEventListener('change', () => {
    const matched = findSymbolByInput(symbolSearchEl.value);
    if (!matched) return;
    applySelectedSymbol(matched);
    loadDaily(matched.symbol);
  });
  symbolSearchEl.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter') return;
    event.preventDefault();
    const matched = findSymbolByInput(symbolSearchEl.value);
    if (matched) {
      applySelectedSymbol(matched);
      loadDaily(matched.symbol);
    } else {
      loadDaily();
    }
  });
  symbolInputEl.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') loadDaily();
  });
  let resizeDebounceTimer = null;
  window.addEventListener('resize', () => {
    // 150ms 防抖（P2-20）：连续 resize 只触发一次 7 张图重排
    if (resizeDebounceTimer) clearTimeout(resizeDebounceTimer);
    resizeDebounceTimer = setTimeout(() => {
      resizeDebounceTimer = null;
      if (charts.length) charts.forEach((chart) => chart.resize());
      if (heatChart) heatChart.resize();
    }, 150);
  });

  // 回测结束日期上限 = 当前标的数据库内最新K线日期（meta.end，由日K接口基于
  // 落库数据生成，不含盘中实时合成的K线）。当日K线一旦落库（如每日更新任务
  // 完成后）即可选当天；未落库时最多选到最新已有K线。数据尚未加载时用本地
  // 今天兜底，仅防止选到未来日期。


  function updateBacktestEndMax() {
    const maxDate = currentPayload?.meta?.end || localToday();
    backtestEndEl.setAttribute('max', maxDate);
    if (backtestEndEl.value && backtestEndEl.value > maxDate) {
      backtestEndEl.value = maxDate;
      updateBacktestRangeLabel();
    }
  }
  updateBacktestEndMax();

  // --- Batch drill-down (快照重跑模式) ---
  const drillBannerEl = document.getElementById('mvDrillBanner');
  const drillBannerTextEl = document.getElementById('mvDrillBannerText');
  const drillExitBtnEl = document.getElementById('mvDrillExitBtn');

  function exitDrillMode() {
    drillContext = null;
    drillBannerEl.hidden = true;
    updateStrategyDropdownLabel();
    // 去掉 URL 上的钻取参数，避免刷新后再次触发
    window.history.replaceState({}, '', '/market-view');
  }

  drillExitBtnEl.addEventListener('click', exitDrillMode);

  async function initDrillMode() {
    const params = new URLSearchParams(window.location.search);
    if (params.get('drill') !== '1') return;
    const batchId = params.get('batch_id') || '';
    const strategyId = params.get('strategy_id') || '';
    const symbol = (params.get('symbol') || '').trim().toUpperCase();
    if (!batchId || !strategyId || !symbol) return;
    let snap = null;
    try {
      const qs = new URLSearchParams({ strategy_id: strategyId, symbol });
      const resp = await fetch(`/batch-backtest/api/runs/${encodeURIComponent(batchId)}/snapshot?${qs}`);
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.detail || '批次快照读取失败');
      snap = data;
    } catch (err) {
      // 行内提示（P2-28：替代原生 alert）后回退常规模式
      chartTitleEl.textContent = `批次快照读取失败（${err.message}），已回退常规模式。`;
      window.history.replaceState({}, '', '/market-view');
      return;
    }
    drillContext = snap;
    drillBannerTextEl.textContent =
      `批次快照重跑：${snap.batch_name || snap.batch_id} / ${snap.strategy_name || snap.strategy_id}` +
      `，区间 ${snap.start_date || '起点'} ~ ${snap.end_date || '—'}。` +
      '结果基于批次快照重跑，可能因历史数据修正产生轻微差异。';
    drillBannerEl.hidden = false;
    backtestStrategyLabelEl.textContent = `${snap.strategy_name || snap.strategy_id}（批次快照）`;
    symbolSearchEl.value = symbol;
    symbolInputEl.value = symbol;
    await loadDaily(symbol);
    if (snap.start_date) backtestStartEl.value = String(snap.start_date).slice(0, 10);
    if (snap.end_date) backtestEndEl.value = String(snap.end_date).slice(0, 10);
    updateBacktestRangeLabel();
    await runRuleBacktest();
  }

  // URL 直达入口：?drill=1 走批次快照钻取；?symbol=XXX 直接加载该标的
  //（标的看板「查看」按钮跳转）。
  async function initFromUrl() {
    const params = new URLSearchParams(window.location.search);
    if (params.get('drill') === '1') return initDrillMode();
    const symbol = (params.get('symbol') || '').trim().toUpperCase();
    if (!symbol) return;
    const matched = allSymbols.find((item) => (item.symbol || '').toUpperCase() === symbol) || null;
    if (matched) applySelectedSymbol(matched);
    else symbolInputEl.value = symbol;
    await loadDaily(symbol);
  }

  Promise.all([loadRuleMeta(), loadSymbols()]).then(initFromUrl).catch((err) => {
    chartTitleEl.textContent = `加载失败：${err.message}`;
    rangeMetaEl.textContent = '';
  });
})();
