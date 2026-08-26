/* app-common.js — 全站公共前端工具（P1-15/P1-12/P1-10）。
 *
 * 由 base.html 在每页 <head> 引入（无 defer，先于各页内联脚本执行）：
 * - esc：HTML 转义唯一实现（替代各页 7 份重复实现；tooltip formatter 一律 esc）。
 * - fetch 全局拦截（P1-10 CSRF 防线 + P1-12 统一 401 处理）：
 *   1) 同源请求自动携带 X-Requested-With 自定义头（服务端 AuthWall 校验，
 *      跨站简单请求无法伪造该头）；
 *   2) 响应 401（登录接口本身除外）统一跳登录页，登录后回到当前页——
 *      各页无需再手写 401 分支，后台轮询也不会无限刷 401。
 * - postJson：JSON POST 统一封装（以 manual_trade 版为准：401 跳登录、
 *   非 2xx 抛 detail 文案）。
 * - logout：POST 退出（GET 退出可被 CSRF 强制触发，已改 POST）。
 * - 止损卡族（renderStopStats 及其 tip/格式化辅助）：原 manual_trade 与
 *   subject_market 两份已漂移实现，以 manual_trade 文案口径为准统一
 *   （硬止损卡含触发当日最低/收盘明细）。
 * - 其余去重：localIso/localToday、fmtAmount、fetchDayCandle、
 *   cycleSort/stableSorted（排序三件套）。
 *
 * 命名空间：全部挂在 window.TQ；另为调用点众多的通用函数提供 window 级
 * 别名（esc/postJson/redirectToLogin/fmtPrice/fmtPct/pctClass/withTip/
 * localIso/localToday/fetchDayCandle/fmtAmount/renderStopStats/cycleSort/
 * stableSorted），各页删除本地重复实现后调用点无需改动。
 */
(function () {
  'use strict';

  // ── 基础 ──────────────────────────────────────────────
  function esc(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function redirectToLogin() {
    location.href = '/login?next=' + encodeURIComponent(location.pathname + location.search);
  }

  // ── fetch 全局拦截 ─────────────────────────────────────
  var nativeFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    init = init || {};
    var url = typeof input === 'string' ? input : (input && input.url) || '';
    var sameOrigin = url.charAt(0) === '/' || url.indexOf(location.origin) === 0;
    if (sameOrigin) {
      var headers = new Headers(init.headers || (typeof input !== 'string' && input ? input.headers : undefined));
      if (!headers.has('X-Requested-With')) {
        headers.set('X-Requested-With', 'XMLHttpRequest');
      }
      init.headers = headers;
    }
    return nativeFetch(input, init).then(function (resp) {
      if (resp.status === 401 && url.indexOf('/api/auth/login') === -1) {
        redirectToLogin();
        // 挂起后续 then 链：页面即将跳转，避免业务代码把 401 JSON 当数据处理
        return new Promise(function () {});
      }
      return resp;
    });
  };

  async function postJson(url, body) {
    const resp = await window.fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '请求失败');
    return data;
  }

  async function logout() {
    try {
      await window.fetch('/api/auth/logout', { method: 'POST' });
    } finally {
      location.href = '/login';
    }
  }

  // ── 日期 / 数字格式化 ──────────────────────────────────
  // 本地日期 → 'YYYY-MM-DD'（不用 toISOString，避免时区把今天变成昨天）
  function localIso(d) {
    const mm = String(d.getMonth() + 1).padStart(2, '0');
    const dd = String(d.getDate()).padStart(2, '0');
    return d.getFullYear() + '-' + mm + '-' + dd;
  }

  function localToday() {
    return localIso(new Date());
  }

  function fmtPrice(v) {
    return Number(v).toFixed(3);
  }

  function fmtPct(v) {
    const n = Number(v);
    return (n > 0 ? '+' : '') + n.toFixed(2) + '%';
  }

  function pctClass(v) {
    return Number(v) > 0 ? 'metric-positive' : (Number(v) < 0 ? 'metric-negative' : '');
  }

  // 金额缩写（统一口径：亿 1 位小数、万 0 位小数；原 4 份精度各异的实现收敛）
  function fmtAmount(value) {
    if (value == null || !Number.isFinite(value)) return '—';
    if (Math.abs(value) >= 1e8) return `${(value / 1e8).toFixed(1)}亿`;
    if (Math.abs(value) >= 1e4) return `${(value / 1e4).toFixed(0)}万`;
    return value.toFixed(0);
  }

  // 通用悬停包装：tip 非空时给 HTML 包一层带 title 的 span（cursor: help）
  function withTip(html, tip) {
    return tip ? '<span class="mt-tip" title="' + esc(tip) + '">' + html + '</span>' : html;
  }

  // ── 当日K线（买入/清仓区间提示共用）─────────────────────
  // intraday=true：当日K线未落库时由实时报价合成（含盘后收盘快照）。
  // 无K线返回 null（非交易日），请求失败抛错。
  async function fetchDayCandle(symbol, date) {
    const params = new URLSearchParams({
      symbol: symbol, start_date: date, end_date: date, limit: '1', intraday: 'true',
    });
    const resp = await window.fetch('/market-view/api/daily?' + params.toString());
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '无法获取当日行情');
    const candle = (data.candles || [])[0];  // [open, close, low, high]
    if (!candle) return null;
    return { low: Number(candle[2]), high: Number(candle[3]), close: Number(candle[1]) };
  }

  // ── 排序三件套 ─────────────────────────────────────────
  // 点击同一列循环：降序 → 升序 → 原始顺序
  function cycleSort(state, key) {
    if (state.key !== key) return { key: key, dir: -1 };
    if (state.dir === -1) return { key: key, dir: 1 };
    return { key: null, dir: 0 };
  }

  // 按排序状态排序；值相同或均缺失时保持原始相对顺序（稳定），缺失值恒排最后
  function stableSorted(items, state, getterOrMap) {
    if (!state.key || state.dir === 0) return items;
    const getter = typeof getterOrMap === 'function' ? getterOrMap : getterOrMap[state.key];
    if (!getter) return items;
    return items.map(function (t, i) {
      return { t: t, i: i, v: getter(t) };
    }).sort(function (a, b) {
      if (a.v == null && b.v == null) return a.i - b.i;
      if (a.v == null) return 1;
      if (b.v == null) return -1;
      let cmp;
      if (typeof a.v === 'string') cmp = a.v < b.v ? -1 : (a.v > b.v ? 1 : 0);
      else cmp = a.v - b.v;
      if (cmp === 0) return a.i - b.i;
      return state.dir === -1 ? -cmp : cmp;
    }).map(function (x) { return x.t; });
  }

  // ── 止损卡族（manual_trade 文案口径为准）─────────────────
  function stopPill(triggered, okText, dangerText) {
    return '<span class="mt-stop-pill ' + (triggered ? 'is-danger' : 'is-ok') + '">' +
      esc(triggered ? dangerText : okText) + '</span>';
  }

  function stopModeLabel(stops) {
    return stops.stop_mode === 'tight' ? '紧止损' : '松止损';
  }

  // 硬止损价 = 买入价 − mul×ATR(买入日)
  function hardStopTip(stops) {
    if (stops.atr_at_buy == null) return '';
    return '硬止损价（' + stopModeLabel(stops) + '口径）= 买入价 ' + fmtPrice(stops.buy_price) +
      ' − ' + stops.hard_stop_atr_mul + ' × ATR(买入日) ' + fmtPrice(stops.atr_at_buy) +
      ' = ' + fmtPrice(stops.hard_stop_price);
  }

  // 吊灯止损价 = 买入以来最高价 − mul×ATR(最新)
  function chandelierStopTip(stops) {
    if (stops.highest_since_buy == null || stops.current_atr == null) return '';
    return '吊灯止损价（' + stopModeLabel(stops) + '口径）= 买入以来最高价 ' +
      fmtPrice(stops.highest_since_buy) +
      (stops.highest_since_buy_date ? '（' + stops.highest_since_buy_date + '）' : '') +
      ' − ' + stops.chandelier_stop_atr_mul +
      ' × ATR(最新) ' + fmtPrice(stops.current_atr) +
      ' = ' + fmtPrice(stops.chandelier_stop_price);
  }

  // 棘轮吊灯：与吊灯同公式逐日回放，取历史最大值（只上移不下移）
  function ratchetStopTip(stops) {
    const price = Number(stops.chandelier_stop_ratchet_price);
    if (!isFinite(price) || price <= 0) return '';
    return '棘轮吊灯止损价（' + stopModeLabel(stops) + '口径）= max(逐日：截至当日最高价 − ' +
      stops.chandelier_stop_atr_mul + ' × 当日ATR)，只上移不下移 = ' + fmtPrice(price);
  }

  // 现价距止损 = 最新价 / 止损价 − 1
  function stopDistanceTip(label, stops, priceKey, pctKey) {
    const price = Number(stops[priceKey]);
    if (!isFinite(price) || price <= 0 || stops.latest_price == null) return '';
    return label + ' = 最新价 ' + fmtPrice(stops.latest_price) + ' / 止损价 ' +
      fmtPrice(price) + ' − 1 = ' + fmtPct(stops[pctKey]);
  }

  function renderStopStats(container, stops) {
    const hardTriggered = !!stops.hard_stop_triggered;
    const chandTriggered = !!stops.chandelier_stop_triggered;
    const ratchetPrice = Number(stops.chandelier_stop_ratchet_price) || 0;
    const ratchetTriggered = !!stops.chandelier_stop_ratchet_triggered;
    const ratchetCard = ratchetPrice > 0 ?
      '<div class="mt-stop-card mt-stop-chandelier">' +
        '<div class="mt-stop-card-head">' +
          '<span class="mt-stat-label">棘轮吊灯止损价</span>' +
          stopPill(ratchetTriggered, '未跌破', '⚠ 已跌破') +
        '</div>' +
        withTip('<span class="mt-stop-price">' + esc(fmtPrice(ratchetPrice)) + '</span>',
          ratchetStopTip(stops)) +
        '<span class="mt-stat-note">同吊灯公式但只上移不下移 · 现价距 ' +
          esc(fmtPct(stops.chandelier_stop_ratchet_distance_pct)) + '</span>' +
      '</div>' : '';
    container.innerHTML =
      '<div class="mt-stop-card mt-stop-hard">' +
        '<div class="mt-stop-card-head">' +
          '<span class="mt-stat-label">硬止损价</span>' +
          stopPill(hardTriggered, '未击穿', '⚠ 已击穿') +
        '</div>' +
        withTip('<span class="mt-stop-price">' + esc(fmtPrice(stops.hard_stop_price)) + '</span>',
          hardStopTip(stops)) +
        '<span class="mt-stat-note">买入价 − ' + esc(stops.hard_stop_atr_mul) + '×ATR(买入日) · 距买入价 ' +
          esc(fmtPct(stops.hard_stop_pct)) +
          (hardTriggered ? ' · 于 ' + esc(stops.hard_stop_trigger_date) + ' 被击穿' +
            (stops.hard_stop_trigger_low != null
              ? '（当日最低 ' + esc(fmtPrice(stops.hard_stop_trigger_low)) +
                (stops.hard_stop_trigger_close != null ? '、收盘 ' + esc(fmtPrice(stops.hard_stop_trigger_close)) : '') + '）'
              : '')
            : '') + '</span>' +
      '</div>' +
      '<div class="mt-stop-card mt-stop-chandelier">' +
        '<div class="mt-stop-card-head">' +
          '<span class="mt-stat-label">吊灯止损价</span>' +
          stopPill(chandTriggered, '未跌破', '⚠ 已跌破') +
        '</div>' +
        withTip('<span class="mt-stop-price">' + esc(fmtPrice(stops.chandelier_stop_price)) + '</span>',
          chandelierStopTip(stops)) +
        '<span class="mt-stat-note">买入以来最高价 − ' + esc(stops.chandelier_stop_atr_mul) + '×ATR(最新) · 距最高价 ' +
          esc(fmtPct(stops.chandelier_stop_pct_from_high)) + '</span>' +
      '</div>' +
      ratchetCard +
      '<div class="mt-stop-card mt-stop-dist">' +
        '<div class="mt-stop-card-head"><span class="mt-stat-label">现价距硬止损</span></div>' +
        withTip('<span class="mt-stop-price ' + pctClass(stops.hard_stop_distance_pct) + '">' +
          esc(fmtPct(stops.hard_stop_distance_pct)) + '</span>',
          stopDistanceTip('现价距硬止损', stops, 'hard_stop_price', 'hard_stop_distance_pct')) +
        '<span class="mt-stat-note">最新价 ' + esc(fmtPrice(stops.latest_price)) + ' / 止损价 ' +
          esc(fmtPrice(stops.hard_stop_price)) + '</span>' +
      '</div>' +
      '<div class="mt-stop-card mt-stop-dist">' +
        '<div class="mt-stop-card-head"><span class="mt-stat-label">现价距吊灯止损</span></div>' +
        withTip('<span class="mt-stop-price ' + pctClass(stops.chandelier_stop_distance_pct) + '">' +
          esc(fmtPct(stops.chandelier_stop_distance_pct)) + '</span>',
          stopDistanceTip('现价距吊灯止损', stops, 'chandelier_stop_price', 'chandelier_stop_distance_pct')) +
        '<span class="mt-stat-note">最新价 ' + esc(fmtPrice(stops.latest_price)) + ' / 止损价 ' +
          esc(fmtPrice(stops.chandelier_stop_price)) + '</span>' +
      '</div>';
  }


  // ── 侧栏目录四件套（instruments 与 subject_market 原近乎逐行重复）─────
  function safeAnchorId(prefix, l1, l2, index) {
    const raw = `${l1}-${l2}`.trim().replace(/\s+/g, '-');
    const safe = raw.replace(/[^\w\u4e00-\u9fa5-]/g, '-').replace(/-+/g, '-').replace(/^-|-$/g, '');
    return `${prefix}-${index}-${safe || 'uncategorized'}`;
  }

  function setActiveSideNav(sideNavEl, sectionId) {
    if (!sectionId) return;
    let activeBtn = null;
    for (const btn of sideNavEl.querySelectorAll('[data-section-id]')) {
      const active = btn.getAttribute('data-section-id') === sectionId;
      btn.classList.toggle('is-active', active);
      if (active) activeBtn = btn;
    }
    if (activeBtn) {
      const scroller = sideNavEl.closest('.instrument-side-nav') || sideNavEl;
      const targetTop = activeBtn.offsetTop - (scroller.clientHeight / 2) + (activeBtn.offsetHeight / 2);
      scroller.scrollTo({ top: Math.max(0, targetTop), behavior: 'smooth' });
    }
  }

  // badgeFn(section) → <em> 内 HTML（计数或金叉/死叉家数，页面自定义）
  function renderSideNav(sideNavEl, sections, badgeFn) {
    if (!sections.length) {
      sideNavEl.innerHTML = '<div class="instrument-side-nav-empty">无可跳转分类</div>';
      return;
    }
    const parts = [];
    let lastL1 = null;
    for (const section of sections) {
      if (section.l1 !== lastL1) {
        if (lastL1 !== null) parts.push('</div>');
        parts.push(`<div class="instrument-side-nav-group"><div class="instrument-side-nav-l1">${esc(section.l1)}</div>`);
        lastL1 = section.l1;
      }
      parts.push(`<button class="instrument-side-nav-l2" type="button" data-section-id="${esc(section.id)}" title="${esc(`${section.l1} - ${section.l2}`)}"><span>${esc(section.l2)}</span><em>${badgeFn(section)}</em></button>`);
    }
    if (lastL1 !== null) parts.push('</div>');
    sideNavEl.innerHTML = parts.join('');
  }

  // 滚动高亮同步：返回 { sync, request }，container 内匹配 selector 的锚点行
  // 越过 anchorTop 时高亮对应侧栏项（request 以 rAF 合并高频滚动事件）。
  function createSectionSync(sideNavEl, container, selector, anchorTop) {
    let pending = false;
    function sync() {
      pending = false;
      const sections = Array.from(container.querySelectorAll(selector));
      if (!sections.length) return;
      let current = sections[0];
      for (const section of sections) {
        if (section.getBoundingClientRect().top <= (anchorTop || 260)) current = section;
        else break;
      }
      setActiveSideNav(sideNavEl, current.id);
    }
    function request() {
      if (pending) return;
      pending = true;
      window.requestAnimationFrame(sync);
    }
    return { sync, request };
  }

  // ── 导出 ──────────────────────────────────────────────
  window.TQ = {
    esc: esc,
    redirectToLogin: redirectToLogin,
    postJson: postJson,
    logout: logout,
    localIso: localIso,
    localToday: localToday,
    fmtPrice: fmtPrice,
    fmtPct: fmtPct,
    pctClass: pctClass,
    fmtAmount: fmtAmount,
    withTip: withTip,
    fetchDayCandle: fetchDayCandle,
    cycleSort: cycleSort,
    stableSorted: stableSorted,
    stopPill: stopPill,
    stopModeLabel: stopModeLabel,
    hardStopTip: hardStopTip,
    chandelierStopTip: chandelierStopTip,
    ratchetStopTip: ratchetStopTip,
    stopDistanceTip: stopDistanceTip,
    renderStopStats: renderStopStats,
    safeAnchorId: safeAnchorId,
    setActiveSideNav: setActiveSideNav,
    renderSideNav: renderSideNav,
    createSectionSync: createSectionSync,
  };
  // window 级别名：各页删除本地重复实现后，原有无限定调用点直接解析到全局
  var aliases = {
    esc: esc,
    postJson: postJson,
    redirectToLogin: redirectToLogin,
    fmtPrice: fmtPrice,
    fmtPct: fmtPct,
    pctClass: pctClass,
    fmtAmount: fmtAmount,
    withTip: withTip,
    localIso: localIso,
    localToday: localToday,
    fetchDayCandle: fetchDayCandle,
    cycleSort: cycleSort,
    stableSorted: stableSorted,
    stopPill: stopPill,
    stopModeLabel: stopModeLabel,
    hardStopTip: hardStopTip,
    chandelierStopTip: chandelierStopTip,
    ratchetStopTip: ratchetStopTip,
    stopDistanceTip: stopDistanceTip,
    renderStopStats: renderStopStats,
    safeAnchorId: safeAnchorId,
    setActiveSideNav: setActiveSideNav,
    renderSideNav: renderSideNav,
    createSectionSync: createSectionSync,
  };
  Object.keys(aliases).forEach(function (name) {
    if (!(name in window)) window[name] = aliases[name];
  });
})();
