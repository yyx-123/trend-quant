(() => {
  'use strict';

  const board = document.getElementById('subjectBoard');
  const statusEl = document.getElementById('subjectBoardStatus');
  const sideNavEl = document.getElementById('subjectSideNav');
  // 当前已渲染快照的 computed_at（EOD 数据为 null），用于识别后台重算出的新快照。
  let renderedSnapshotTs = null;
  // 当前看板的二级类目锚点清单（侧栏跳转/滚动高亮用），随整表重载重建。
  let boardSections = [];
  const fmtScore = (value) => value == null ? '—' : value.toFixed(1);
  const fmtChange = (value) => value == null ? '—' : `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`;
  const getChangeClass = (value) => value != null && value >= 0 ? 'is-positive' : 'is-negative';
  const fmtPhase = (item) => {
    if (item.macd_phase != null && item.macd_phase_days != null) {
      const phaseLabel = item.macd_phase === 'golden' ? '金叉' : '死叉';
      const phaseClass = item.macd_phase === 'golden' ? 'is-positive' : 'is-negative';
      const changeVal = item.macd_phase_change_pct != null ? item.macd_phase_change_pct.toFixed(2) : '—';
      const changeSign = item.macd_phase_change_pct != null && item.macd_phase_change_pct >= 0 ? '+' : '';
      const changeClass = item.macd_phase_change_pct != null ? (item.macd_phase_change_pct >= 0 ? 'is-positive' : 'is-negative') : '';
      return `<span class="${phaseClass}">${phaseLabel}第 ${item.macd_phase_days} 天</span>，涨跌幅：<span class="${changeClass}">${changeSign}${changeVal}%</span>`;
    }
    return '<span class="index-phase-na">—</span>';
  };
  // 类目行（一级头 / 二级分隔行）的金叉死叉家数：成员相位计数。
  const fmtCrossCounts = (golden, dead) => {
    if ((golden == null && dead == null) || (!golden && !dead)) return '';
    return `<span class="is-positive">金叉 ${golden || 0}</span> / <span class="is-negative">死叉 ${dead || 0}</span>`;
  };

  // ---- 类目热力图（Treemap）------------------------------------------------
  const heatmapPanel = document.getElementById('heatmapPanel');
  const heatmapTabsEl = document.getElementById('heatmapTabs');
  const heatmapChartEl = document.getElementById('heatmapChart');
  const heatmapCrumbEl = document.getElementById('heatmapCrumb');
  const heatmapDimEl = document.getElementById('heatmapDim');
  const heatmapColorNoteEl = document.getElementById('heatmapColorNote');
  let heatmapChart = null;
  let heatmapPayload = null;
  let heatmapActiveL1 = null;
  let heatmapResizeTimer = null;
  let heatActiveGroup = null;   // 当前一级类目分组
  let heatTreeNodes = null;     // 当前分组的 treemap 静态树（buildHeatTree）
  let heatViewPath = [];        // 下钻路径（L2/L3 节点），空 = 顶层视图

  // 热力图着色维度：field 为标的/类目 meta 上的字段名；maxAbs 为该维度映射到
  // 最深色的绝对值上限（趋势值量纲 0~20，与 HEAT_STOPS 档位一致；涨跌幅按
  // ±5% 封顶）。fmt 用于叶子标签第二行数值与图例说明。
  const HEAT_COLOR_DIMS = {
    ma5: { field: 'trend_ma5', maxAbs: 20, note: '最新趋势值 MA5', fmt: (m) => fmtScore(m.trend_ma5) },
    score: { field: 'trend_score', maxAbs: 20, note: '当日趋势值', fmt: (m) => fmtScore(m.trend_score) },
    change: { field: 'daily_change_pct', maxAbs: 5, note: '当日涨跌幅', fmt: (m) => fmtChange(m.daily_change_pct) },
  };
  let heatColorDim = 'ma5';

  // 柔和的红/绿渐变档位（指标绝对值 → RGB），最深不超过末档，避免刺眼。
  const HEAT_STOPS_UP = [[0, [250, 236, 234]], [2.5, [241, 203, 198]], [5, [228, 161, 154]], [10, [211, 105, 97]], [20, [191, 63, 57]]];
  const HEAT_STOPS_DOWN = [[0, [232, 241, 237]], [2.5, [198, 227, 215]], [5, [152, 205, 183]], [10, [96, 174, 143]], [20, [27, 126, 96]]];

  function heatInterpolate(stops, value) {
    const x = Math.min(Math.max(value, 0), stops[stops.length - 1][0]);
    for (let i = 1; i < stops.length; i += 1) {
      if (x <= stops[i][0]) {
        const [x0, c0] = stops[i - 1];
        const [x1, c1] = stops[i];
        const t = x1 === x0 ? 0 : (x - x0) / (x1 - x0);
        return c0.map((a, k) => Math.round(a + (c1[k] - a) * t));
      }
    }
    return stops[stops.length - 1][1];
  }

  function heatColor(value, maxAbs = 20) {
    if (value == null || !Number.isFinite(value)) return { fill: '#e6eaee', text: '#6b7280' };
    // 各维度量纲不同：先归一到 HEAT_STOPS 的 0~20 区间再取色
    const scaled = Math.abs(value) / maxAbs * 20;
    const rgb = heatInterpolate(value >= 0 ? HEAT_STOPS_UP : HEAT_STOPS_DOWN, scaled);
    return { fill: `rgb(${rgb[0]},${rgb[1]},${rgb[2]})`, text: scaled >= 9 ? '#ffffff' : '#3f4756' };
  }

  // 类目标题条底色：趋势色向白色稀释，避免与深色标题文字冲突。
  function heatColorSoft(value, maxAbs = 20) {
    if (value == null || !Number.isFinite(value)) return '#eef1f3';
    const scaled = Math.abs(value) / maxAbs * 20;
    const rgb = heatInterpolate(value >= 0 ? HEAT_STOPS_UP : HEAT_STOPS_DOWN, scaled);
    const mix = (a) => Math.round(a + (255 - a) * 0.66);
    return `rgb(${mix(rgb[0])},${mix(rgb[1])},${mix(rgb[2])})`;
  }

  function clipHeatText(text, maxChars) {
    const s = String(text ?? '');
    if (maxChars < 2) return '';
    return s.length <= maxChars ? s : `${s.slice(0, Math.max(1, maxChars - 1))}…`;
  }

  // 每个标的等大面积：treemap 按兄弟节点的 value 比例分配面积（父节点声明的
  // value 生效，不会被子节点覆盖），因此叶子 value 固定为 1，类目节点 value
  // 取其包含的标的数 —— 每个标的色块面积完全相同，类目块面积正比于标的数量。
  // 这里只构建静态结构（颜色 / 层级），标签文字由 layoutHeatLabels 按当前
  // 下钻视图的实际显示面积动态计算 —— 直接下钻放大后标签会重新生成，
  // 不会出现"色块放大了名字还是不显示"。
  function buildHeatTree(group) {
    const dim = HEAT_COLOR_DIMS[heatColorDim];
    const l2Nodes = [];
    for (const l2 of group.items || []) {
      const l3Nodes = [];
      for (const l3 of l2.children || []) {
        const leaves = (l3.children || []).map((inst) => {
          const color = heatColor(inst[dim.field], dim.maxAbs);
          return {
            name: inst.name || inst.symbol, value: 1, meta: inst, isLeaf: true, level: 3,
            itemStyle: { color: color.fill }, labelColor: color.text,
            // 叶子节点不设标题条（防止下钻后 levels 样式偏移时出现空白条）。
            upperLabel: { show: false },
          };
        });
        if (!leaves.length) continue;
        l3Nodes.push({
          name: l3.category_l3, value: leaves.length, children: leaves, meta: l3, level: 2,
          itemStyle: { color: heatColorSoft(l3[dim.field], dim.maxAbs) },
          // 标题条挂在节点自身：下钻改变层级时不随 levels 偏移。
          upperLabel: { show: true, height: 17, fontSize: 11, color: '#52596b', formatter: (p) => p.data.upperText || '' },
        });
      }
      if (!l3Nodes.length) continue;
      l2Nodes.push({
        name: l2.category_l2, value: l3Nodes.reduce((sum, n) => sum + n.value, 0), children: l3Nodes, meta: l2, level: 1,
        itemStyle: { color: heatColorSoft(l2[dim.field], dim.maxAbs) },
        // 标题条底色 = 当前视图最外层的 borderColor（ECharts 父节点标题条用边框色填充，
        // 非 itemStyle.color），顶层视图下是深色条，文字用浅色；实际颜色随下钻视图
        // 在 layoutHeatLabels 里逐层重设。
        upperLabel: { show: true, height: 24, fontSize: 13, fontWeight: 700, color: '#f3f4f6', formatter: (p) => p.data.upperText || '' },
      });
    }
    return l2Nodes;
  }

  // 按每个色块的真实矩形（宽×高，来自 readHeatRects）生成标签。treemap 的
  // 矩形形状由 squarify 布局决定、预先无法得知，所以 renderHeatView 先渲染
  // 一遍拿到实际宽高 —— 横扁条按实际宽度放完整名称，竖长条把名称旋转 90°
  // 竖排显示，太小的框不放文字。
  function layoutHeatLabels(viewNodes, rects) {
    // depth = 节点在当前下钻视图里的层级（viewNodes 为 0）。父节点标题条底色由
    // levels 里该层的 borderColor 填充：最外层是深色条（#1f2937），内层是浅灰条
    // （#a3adb8）—— 文字颜色必须跟着当前层级走，否则深色条上深字不可见。
    const decorate = (node, depth) => {
      const rect = rects.get(node);
      const meta = node.meta || {};
      if (node.level === 3) {
        const availW = (rect ? rect.width : 0) - 8;
        const availH = (rect ? rect.height : 0) - 6;
        const label = { color: node.labelColor, fontSize: 12 };
        let text = '';
        if (availW >= 18 && availH >= 14) {
          if (availH > availW * 1.6 && availW < 46 && availH >= 46) {
            // 竖长条：名称逐字换行竖排（每字端正、从上往下读），但连续数字、
            // 连续字母各自合并为一行 —— 沪深300ETF → 沪/深/300/ETF 四行。
            // 字号按条宽自适应；若最长的字母/数字行放不下则进一步缩字号。
            label.fontSize = Math.max(10, Math.min(13, availW / 1.5));
            const runs = String(node.name ?? '').match(/\d+|[a-zA-Z]+|[^\da-zA-Z]/g) || [];
            const widest = runs.reduce((m, r) => Math.max(m, r.length > 1 ? r.length * 0.62 : 1), 1);
            if (widest * label.fontSize > availW) {
              label.fontSize = Math.max(10, availW / widest);
            }
            label.lineHeight = Math.round(label.fontSize * 1.18);
            const maxLines = Math.floor(availH / label.lineHeight);
            const lines = runs.length <= maxLines ? runs
              : maxLines < 2 ? [] : [...runs.slice(0, maxLines - 1), '…'];
            text = lines.join('\n');
          } else {
            label.fontSize = Math.max(10, Math.min(14, Math.min(availW / 4.5, availH / 2.1)));
            text = clipHeatText(node.name, Math.floor(availW / label.fontSize));
            if (text && availH >= label.fontSize * 2.9) {
              const score = HEAT_COLOR_DIMS[heatColorDim].fmt(meta);
              if (score !== '—') text += `\n${score}`;
            }
          }
        }
        node.label = label;
        node.labelText = text;
      } else {
        const w = rect ? rect.width : 0;
        const fontSize = node.level === 1 ? 13 : 11;
        node.upperText = w < fontSize * 2.4 ? '' : clipHeatText(node.name, Math.floor((w - 10) / fontSize));
        node.upperLabel.color = depth === 0 ? '#f3f4f6' : '#52596b';
      }
      const kids = node.children || [];
      for (const kid of kids) {
        decorate(kid, depth + 1);
      }
    };
    for (const node of viewNodes) {
      decorate(node, 0);
    }
  }

  // 从刚渲染完的图表读回每个节点色块的实际矩形（key = 我们构建的数据对象）。
  // 注意：getRawDataItem 返回的是 ECharts 内部包装/深拷贝对象，无法按引用
  // 匹配回原始节点 —— 所以改为平行遍历 series data 的内部树（tree.root 的
  // 子节点顺序与传入数据一致，TreeNode 有 getDataIndex()），逐层用名字做
  // 校验，对不上就返回空 Map 让调用方走兜底。
  function readHeatRects(viewNodes) {
    const rects = new Map();
    try {
      const data = heatmapChart.getModel().getSeriesByIndex(0).getData();
      const walk = (treeNode, ourNode) => {
        if (!treeNode || !ourNode || treeNode.name !== ourNode.name) throw new Error('tree mismatch');
        const dataIndex = typeof treeNode.getDataIndex === 'function' ? treeNode.getDataIndex() : treeNode.dataIndex;
        const layout = data.getItemLayout(dataIndex);
        if (layout) rects.set(ourNode, layout);
        const tKids = treeNode.children || [];
        const oKids = ourNode.children || [];
        if (tKids.length !== oKids.length) throw new Error('tree mismatch');
        for (let i = 0; i < tKids.length; i++) walk(tKids[i], oKids[i]);
      };
      const tRoots = (data.tree.root.children || []);
      if (tRoots.length !== viewNodes.length) throw new Error('tree mismatch');
      for (let i = 0; i < tRoots.length; i++) walk(tRoots[i], viewNodes[i]);
    } catch (err) {
      return new Map();
    }
    return rects;
  }

  // 兜底：读不到真实布局时按"当前视图根节点面积 × value 占比"估算方形边长，
  // 据此生成标签（横竖形状未知、不如真实矩形准，但保证有文字）。
  function layoutHeatLabelsFallback(viewNodes, canvasArea) {
    const decorate = (node, share, depth) => {
      const side = Math.sqrt(Math.max(share, 0) * canvasArea);
      const meta = node.meta || {};
      if (node.level === 3) {
        const fontSize = Math.max(10, Math.min(14, side / 7));
        node.label = { color: node.labelColor, fontSize };
        const chars = Math.floor((side * 0.9) / fontSize);
        let text = side < 32 ? '' : clipHeatText(node.name, chars);
        if (text && side >= 92) {
          const score = HEAT_COLOR_DIMS[heatColorDim].fmt(meta);
          if (score !== '—') text += `\n${score}`;
        }
        node.labelText = text;
      } else {
        const chars = Math.floor(side / (node.level === 1 ? 13 : 11));
        node.upperText = side < (node.level === 1 ? 56 : 42) ? '' : clipHeatText(node.name, chars);
        node.upperLabel.color = depth === 0 ? '#f3f4f6' : '#52596b';
      }
      const kids = node.children || [];
      const kidTotal = kids.reduce((sum, kid) => sum + (kid.value || 0), 0);
      for (const kid of kids) {
        decorate(kid, kidTotal > 0 ? share * (kid.value || 0) / kidTotal : 0, depth + 1);
      }
    };
    const viewTotal = viewNodes.reduce((sum, node) => sum + (node.value || 0), 0);
    for (const node of viewNodes) {
      decorate(node, viewTotal > 0 ? (node.value || 0) / viewTotal : 0, 0);
    }
  }

  function heatTooltip(param) {
    const d = param.data || {};
    const m = d.meta || {};
    const signClass = (v) => (v == null || !Number.isFinite(v) || v === 0 ? '' : (v > 0 ? 'heat-tip-pos' : 'heat-tip-neg'));
    const changeCell = (v) => `<span class="${signClass(v)}">${fmtChange(v)}</span>`;
    if (d.isLeaf) {
      return [
        `<div class="heat-tip-title">${esc(d.name)}</div>`,
        `<div class="heat-tip-sub">${esc(m.symbol || '')} · ${esc(m.category_l2 || '')} / ${esc(m.category_l3 || '')}</div>`,
        `<table class="heat-tip-table">`,
        `<tr><td>趋势 MA5</td><td class="${signClass(m.trend_ma5)}">${fmtScore(m.trend_ma5)}</td></tr>`,
        `<tr><td>当日趋势值</td><td class="${signClass(m.trend_score)}">${fmtScore(m.trend_score)}</td></tr>`,
        `<tr><td>强度</td><td>${m.strength == null ? '—' : m.strength}</td></tr>`,
        `<tr><td>日涨跌</td><td class="${signClass(m.daily_change_pct)}">${fmtChange(m.daily_change_pct)}</td></tr>`,
        `<tr><td>5日 / 20日</td><td>${changeCell(m.change_5d)} / ${changeCell(m.change_20d)}</td></tr>`,
        `<tr><td>近20日均成交额</td><td>${m.amount_avg20 == null ? '—' : fmtAmount(m.amount_avg20)}</td></tr>`,
        `</table>`,
        `<div class="heat-tip-hint">点击跳转到标的查看页</div>`,
      ].join('');
    }
    return [
      `<div class="heat-tip-title">${esc(d.name)}</div>`,
      `<div class="heat-tip-sub">${m.member_count == null ? '—' : m.member_count} 个标的 · 趋势值按成交额加权</div>`,
      `<table class="heat-tip-table">`,
      `<tr><td>趋势 MA5</td><td class="${signClass(m.trend_ma5)}">${fmtScore(m.trend_ma5)}</td></tr>`,
      `<tr><td>强度</td><td>${m.strength == null ? '—' : m.strength}</td></tr>`,
      `<tr><td>日涨跌</td><td class="${signClass(m.daily_change_pct)}">${fmtChange(m.daily_change_pct)}</td></tr>`,
      `<tr><td>近20日均成交额</td><td>${m.amount_avg20 == null ? '—' : fmtAmount(m.amount_avg20)}</td></tr>`,
      `</table>`,
    ].join('');
  }

  function heatmapOption(group, viewNodes) {
    return {
      animationDuration: 400,
      tooltip: { confine: true, formatter: heatTooltip },
      series: [{
        type: 'treemap',
        name: group.category_l1,
        left: 0, right: 0, top: 0, bottom: 0,
        roam: false,
        // 下钻不走内置 zoomToNode：标签需要按新视图重算（见 renderHeatView），
        // 因此点击由页面接管后整树重渲染，面包屑也是自定义的。
        nodeClick: false,
        // 类目下钻、叶子跳标的查看页都由页面 click 处理，统一指针手势
        cursor: 'pointer',
        breadcrumb: { show: false },
        label: { show: true, fontSize: 12, lineHeight: 15, formatter: (p) => p.data.labelText || '' },
        upperLabel: { show: false },
        levels: [
          { itemStyle: { borderWidth: 0, gapWidth: 0 } },
          // 当前视图的最外层分组：黑色粗框
          { itemStyle: { borderColor: '#1f2937', borderWidth: 2.5, gapWidth: 2.5 } },
          // 下一层分组：灰色细框
          { itemStyle: { borderColor: '#a3adb8', borderWidth: 1, gapWidth: 1 } },
          // 叶子标的
          { itemStyle: { borderColor: '#ffffff', borderWidth: 0.8, gapWidth: 0.5 } },
        ],
        data: viewNodes,
      }],
    };
  }

  function renderHeatmap(data) {
    heatmapPayload = data;
    const groups = data?.groups || [];
    if (!groups.length) {
      heatmapPanel.hidden = true;
      return;
    }
    if (!groups.some((g) => g.category_l1 === heatmapActiveL1)) {
      heatmapActiveL1 = groups[0].category_l1;
    }
    heatmapTabsEl.innerHTML = groups.map((g) => (
      `<button type="button" role="tab" class="heatmap-tab${g.category_l1 === heatmapActiveL1 ? ' is-active' : ''}" data-l1="${esc(g.category_l1)}">${esc(g.category_l1)}<span>${g.count} 个二级</span></button>`
    )).join('');
    heatmapPanel.hidden = false;
    if (!heatmapChart && window.echarts) {
      try {
        heatmapChart = echarts.init(heatmapChartEl);
        heatmapChart.on('click', (params) => {
          const d = params?.data;
          if (!d) return;
          // 叶子标的：跳转到标的查看页；类目色块：下钻
          if (!Array.isArray(d.children) || !d.children.length) {
            const symbol = d.meta?.symbol;
            if (symbol) window.location.assign(`/market-view?symbol=${encodeURIComponent(symbol)}`);
            return;
          }
          heatViewPath.push(d);
          renderHeatView();
        });
      } catch (err) {
        heatmapChart = null;
      }
    }
    if (!heatmapChart) {
      heatmapPanel.hidden = true;
      return;
    }
    const group = groups.find((g) => g.category_l1 === heatmapActiveL1) || groups[0];
    heatActiveGroup = group;
    // 切换一级类目 / 刷新数据后回到顶层视图并重建树（路径里的节点引用随之失效）
    heatViewPath = [];
    heatTreeNodes = buildHeatTree(group);
    heatmapChart.resize();
    renderHeatView();
  }

  // 按当前下钻路径重算标签并重渲染。ECharts 内置 zoom 只是放大既有数据，
  // 标签是构建时算死的静态字符串，小色块下钻后依然无文字 —— 所以这里每次
  // 都以"当前视图根节点铺满画布"为前提重新生成标签。渲染分两遍：第一遍先
  // 铺出 treemap 真实布局（矩形的横竖/宽高比只有布局后才知道），读回每个
  // 色块的实际矩形后，第二遍按实际宽高写入标签（见 layoutHeatLabels）。
  function renderHeatView() {
    if (!heatmapChart || !heatActiveGroup || !heatTreeNodes) return;
    const width = heatmapChartEl.clientWidth || 1000;
    const height = heatmapChartEl.clientHeight || 560;
    const root = heatViewPath.length ? heatViewPath[heatViewPath.length - 1] : null;
    const viewNodes = root ? [root] : heatTreeNodes;
    heatmapChart.setOption(heatmapOption(heatActiveGroup, viewNodes), true);
    const rects = readHeatRects(viewNodes);
    if (rects.size) {
      layoutHeatLabels(viewNodes, rects);
      // 数据只多了 labelText/upperText 等标签字段，布局不变，直接合并更新
      heatmapChart.setOption({ series: [{ type: 'treemap', data: viewNodes }] });
    } else {
      layoutHeatLabelsFallback(viewNodes, width * height);
      heatmapChart.setOption({ series: [{ type: 'treemap', data: viewNodes }] });
    }
    renderHeatCrumb();
  }

  function renderHeatCrumb() {
    if (!heatViewPath.length) {
      heatmapCrumbEl.hidden = true;
      heatmapCrumbEl.innerHTML = '';
      return;
    }
    const parts = [heatActiveGroup.category_l1, ...heatViewPath.map((n) => n.name)];
    heatmapCrumbEl.innerHTML = parts.map((label, i) => (
      `${i ? '<span class="heatmap-crumb-sep" aria-hidden="true">›</span>' : ''}` +
      `<button type="button" class="heatmap-crumb-btn${i === parts.length - 1 ? ' is-current' : ''}" data-depth="${i}">${esc(label)}</button>`
    )).join('');
    heatmapCrumbEl.hidden = false;
  }

  heatmapCrumbEl.addEventListener('click', (event) => {
    const btn = event.target.closest('.heatmap-crumb-btn');
    if (!btn) return;
    // depth 0 = 回到一级类目顶层；depth i = 截断到路径第 i 个节点
    heatViewPath.length = Number(btn.dataset.depth);
    renderHeatView();
  });

  heatmapTabsEl.addEventListener('click', (event) => {
    const btn = event.target.closest('.heatmap-tab');
    if (!btn || !heatmapPayload) return;
    heatmapActiveL1 = btn.dataset.l1;
    renderHeatmap(heatmapPayload);
  });

  // 着色维度切换：重建树（itemStyle 颜色在 buildHeatTree 里按维度取值），
  // 与切换一级类目一样回到顶层视图。
  heatmapDimEl.addEventListener('click', (event) => {
    const btn = event.target.closest('.heatmap-dim-btn');
    if (!btn || !HEAT_COLOR_DIMS[btn.dataset.dim] || btn.dataset.dim === heatColorDim) return;
    heatColorDim = btn.dataset.dim;
    heatmapDimEl.querySelectorAll('.heatmap-dim-btn').forEach((el) => {
      const on = el === btn;
      el.classList.toggle('is-active', on);
      el.setAttribute('aria-checked', on ? 'true' : 'false');
    });
    heatmapColorNoteEl.textContent = HEAT_COLOR_DIMS[heatColorDim].note;
    if (heatmapPayload) renderHeatmap(heatmapPayload);
  });

  window.addEventListener('resize', () => {
    if (!heatmapChart || heatmapPanel.hidden || !heatmapPayload) return;
    heatmapChart.resize();
    clearTimeout(heatmapResizeTimer);
    heatmapResizeTimer = setTimeout(() => renderHeatmap(heatmapPayload), 160);
  });

const sparkline = (points, dates, upperPoints = [], lowerPoints = []) => {
    const axisTicks = [-15, -10, -5, 0, 5, 10, 15];
    const plotLeft = 18;
    const clamp = (value) => Math.max(-15, Math.min(15, value));
    const xFor = (index) => points.length > 1 ? plotLeft + index / (points.length - 1) * (100 - plotLeft) : (100 + plotLeft) / 2;
    const series = points.map((value, index) => ({ index, value, x: xFor(index) }));
    const values = series.filter(point => point.value != null && Number.isFinite(point.value));
    if (values.length < 2) return '<span class="spark-empty">—</span>';
    const y = (value) => 100 - ((clamp(value) + 15) / 30 * 84 + 8);
    const color = (value) => value > 0 ? '#c13f3a' : value < 0 ? '#187b5f' : '#8a9891';
    const timeTicks = [];
    let previousMonth = '';
    let previousWeek = '';
    for (const point of series) {
      const [year, month, day] = String(dates?.[point.index] || '').slice(0, 10).split('-').map(Number);
      if (!year || !month || !day) continue;
      const date = new Date(Date.UTC(year, month - 1, day));
      const monthKey = `${year}-${month}`;
      const monday = new Date(date);
      monday.setUTCDate(date.getUTCDate() - ((date.getUTCDay() + 6) % 7));
      const weekKey = monday.toISOString().slice(0, 10);
      if (monthKey !== previousMonth) {
        timeTicks.push(`<line class="spark-time-tick spark-month-tick" x1="${point.x.toFixed(1)}" y1="84" x2="${point.x.toFixed(1)}" y2="100"/>`);
        previousMonth = monthKey;
        previousWeek = weekKey;
      } else if (weekKey !== previousWeek) {
        timeTicks.push(`<line class="spark-time-tick spark-week-tick" x1="${point.x.toFixed(1)}" y1="91" x2="${point.x.toFixed(1)}" y2="100"/>`);
        previousWeek = weekKey;
      }
    }
    const segments = [];
    for (let index = 1; index < series.length; index += 1) {
      const previous = series[index - 1], current = series[index];
      if (!Number.isFinite(previous.value) || !Number.isFinite(current.value)) continue;
      if (current.index === values[values.length - 1].index) continue;
      const from = { x: previous.x, y: y(previous.value) };
      const to = { x: current.x, y: y(current.value) };
      if ((previous.value > 0 && current.value < 0) || (previous.value < 0 && current.value > 0)) {
        const ratio = -previous.value / (current.value - previous.value);
        const crossing = { x: previous.x + (current.x - previous.x) * ratio, y: y(0) };
        segments.push(`<line x1="${from.x.toFixed(1)}" y1="${from.y.toFixed(1)}" x2="${crossing.x.toFixed(1)}" y2="${crossing.y.toFixed(1)}" stroke="${color(previous.value)}"/>`);
        segments.push(`<line x1="${crossing.x.toFixed(1)}" y1="${crossing.y.toFixed(1)}" x2="${to.x.toFixed(1)}" y2="${to.y.toFixed(1)}" stroke="${color(current.value)}"/>`);
      } else {
        segments.push(`<line x1="${from.x.toFixed(1)}" y1="${from.y.toFixed(1)}" x2="${to.x.toFixed(1)}" y2="${to.y.toFixed(1)}" stroke="${color(current.value || previous.value)}"/>`);
      }
    }
    const latest = values[values.length - 1];
    const zeroY = y(0).toFixed(1);
    const latestClass = latest.value > 0 ? 'is-positive' : latest.value < 0 ? 'is-negative' : 'is-neutral';
    const pointsFor = (history) => history
      .map((value, index) => value != null && Number.isFinite(value) ? `${xFor(index).toFixed(1)},${y(value).toFixed(1)}` : '')
      .filter(Boolean).join(' ');
    const bandPairs = points.map((_, index) => {
      const upper = upperPoints[index], lower = lowerPoints[index];
      return Number.isFinite(upper) && Number.isFinite(lower) ? { index, upper, lower } : null;
    }).filter(Boolean);
    const band = bandPairs.length > 1 ? `${bandPairs.map(point => `${xFor(point.index).toFixed(1)},${y(point.upper).toFixed(1)}`).join(' ')} ${[...bandPairs].reverse().map(point => `${xFor(point.index).toFixed(1)},${y(point.lower).toFixed(1)}`).join(' ')}` : '';
    const yGrid = axisTicks.filter(value => value !== 0).map(value => `<line class="spark-y-grid" x1="${plotLeft}" y1="${y(value).toFixed(1)}" x2="100" y2="${y(value).toFixed(1)}"/>`).join('');
    const yLabels = axisTicks.map(value => `<span class="spark-y-label" style="top:${y(value).toFixed(1)}%">${value > 0 ? '+' : ''}${value}</span>`).join('');
    const upperLine = pointsFor(upperPoints);
    const lowerLine = pointsFor(lowerPoints);
    return `<div class="spark-plot">${yLabels}<svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-label="近三月趋势 MA5"><g class="spark-y-axis">${yGrid}</g><g class="spark-time-axis">${timeTicks.join('')}</g>${band ? `<polygon class="spark-range-band" points="${band}"/>` : ''}${upperLine ? `<polyline class="spark-bound spark-bound-upper" points="${upperLine}"/>` : ''}${lowerLine ? `<polyline class="spark-bound spark-bound-lower" points="${lowerLine}"/>` : ''}<line class="spark-zero-axis" x1="${plotLeft}" y1="${zeroY}" x2="100" y2="${zeroY}"/>${segments.join('')}</svg><span class="spark-latest-dot ${latestClass}" style="left:${latest.x.toFixed(1)}%;top:${y(latest.value).toFixed(1)}%" aria-label="最新趋势值"></span></div>`;
  };
  // 近30日K线 mini 图：蜡烛（红涨绿跌）+ 5日均线。盘中模式下最后一根为
  // 当日实时合成K线。仅具体标的行有数据，类目聚合行显示「—」。
  // dif/dea 与 candles 同窗口逐根对齐（同一 tail(30)）：据此在图内标注窗口
  // 内全部金叉（橙色小上箭头，置于该根K线低点下方）与死叉（黑色小下箭头，
  // 置于高点上方），而非仅最新一次。
  const klineSpark = (candles, ma5, dif, dea) => {
    if (!Array.isArray(candles) || !candles.length) return '<span class="spark-empty">—</span>';
    const n = candles.length;
    const values = [];
    candles.forEach((k, i) => {
      if (Number.isFinite(k?.h)) values.push(k.h);
      if (Number.isFinite(k?.l)) values.push(k.l);
      const m = ma5?.[i];
      if (Number.isFinite(m)) values.push(m);
    });
    if (!values.length) return '<span class="spark-empty">—</span>';
    let vMin = Math.min(...values);
    let vMax = Math.max(...values);
    if (vMax <= vMin) vMax = vMin + 1e-6;
    const pad = (vMax - vMin) * 0.06;
    vMin -= pad;
    vMax += pad;
    // 上下各留 7% 头部空间，给金叉/死叉箭头留位。
    const y = (value) => 100 - ((Math.min(vMax, Math.max(vMin, value)) - vMin) / (vMax - vMin) * 86 + 7);
    const step = 100 / n;
    const bodyW = step * 0.62;
    const parts = [];
    candles.forEach((k, i) => {
      if (!Number.isFinite(k?.o) || !Number.isFinite(k?.h) || !Number.isFinite(k?.l) || !Number.isFinite(k?.c)) return;
      const cx = (i + 0.5) * step;
      const color = k.c >= k.o ? '#c13f3a' : '#187b5f';
      const top = y(Math.max(k.o, k.c));
      const height = Math.max(y(Math.min(k.o, k.c)) - top, 0.8);
      parts.push(`<line x1="${cx.toFixed(1)}" y1="${y(k.h).toFixed(1)}" x2="${cx.toFixed(1)}" y2="${y(k.l).toFixed(1)}" stroke="${color}"/>`);
      parts.push(`<rect x="${(cx - bodyW / 2).toFixed(1)}" y="${top.toFixed(1)}" width="${bodyW.toFixed(1)}" height="${height.toFixed(1)}" fill="${color}"/>`);
    });
    if (!parts.length) return '<span class="spark-empty">—</span>';
    // 金叉/死叉标注：与 detect_macd_phase 同口径——sign = DIF-DEA，
    // 由负/零转正当根为金叉，由正/零转负当根为死叉。窗口内逐根扫描，
    // 多次交叉全部标注。candles 有剔根时按尾部对齐 dif/dea。
    if (Array.isArray(dif) && Array.isArray(dea)) {
      const offset = Math.max(dif.length, dea.length) - n;
      const signAt = (i) => {
        const d = dif[offset + i], e = dea[offset + i];
        return Number.isFinite(d) && Number.isFinite(e) ? d - e : NaN;
      };
      // ⬆/⬇ 式描边箭头：竖线杆 + 两根斜线箭头羽，白色描边底衬 + 彩色主线
      // 双层绘制，压在蜡烛上也清晰。全部用 stroke 绘制并配合
      // vector-effect:non-scaling-stroke，在 mini 图的非均匀缩放下保持锐利。
      const stemLen = 13;
      const headDx = 1.0;
      const headDy = 3.5;
      const gap = 8;
      const arrow = (cx, tipY, dir, cls) => {
        const s = dir === 'up' ? 1 : -1;
        const stemEnd = (tipY + s * stemLen).toFixed(1);
        const headY = (tipY + s * headDy).toFixed(1);
        const x0 = cx.toFixed(1), y0 = tipY.toFixed(1);
        const xl = (cx - headDx).toFixed(1), xr = (cx + headDx).toFixed(1);
        const lines = `<line x1="${x0}" y1="${y0}" x2="${x0}" y2="${stemEnd}"/><line x1="${x0}" y1="${y0}" x2="${xl}" y2="${headY}"/><line x1="${x0}" y1="${y0}" x2="${xr}" y2="${headY}"/>`;
        return `<g class="kspark-arrow-halo">${lines}</g><g class="kspark-arrow ${cls}">${lines}</g>`;
      };
      for (let i = 1; i < n; i += 1) {
        const prev = signAt(i - 1), curr = signAt(i);
        if (!Number.isFinite(prev) || !Number.isFinite(curr) || curr === 0) continue;
        const k = candles[i];
        if (!Number.isFinite(k?.h) || !Number.isFinite(k?.l)) continue;
        const cx = (i + 0.5) * step;
        if (prev <= 0 && curr > 0) {
          // 金叉：橙色 ⬆，尖端指向该根K线低点（留 gap 间距）；贴底时上移保证完整可见。
          const tipY = Math.min(y(k.l) + gap, 100 - stemLen - 0.5);
          parts.push(arrow(cx, tipY, 'up', 'kspark-cross-golden'));
        } else if (prev >= 0 && curr < 0) {
          // 死叉：黑色 ⬇，尖端指向该根K线高点（留 gap 间距）；贴顶时下移保证完整可见。
          const tipY = Math.max(y(k.h) - gap, stemLen + 0.5);
          parts.push(arrow(cx, tipY, 'down', 'kspark-cross-dead'));
        }
      }
    }
    const maPoints = (Array.isArray(ma5) ? ma5 : [])
      .map((value, i) => Number.isFinite(value) ? `${((i + 0.5) * step).toFixed(1)},${y(value).toFixed(1)}` : '')
      .filter(Boolean).join(' ');
    const maLine = maPoints ? `<polyline class="kspark-ma5" points="${maPoints}"/>` : '';
    return `<svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-label="近30日K线">${parts.join('')}${maLine}</svg>`;
  };
  // MACD mini 图：红柱/绿柱（hist = DIF-DEA 两倍）+ DIF/DEA 双线，根数与
  // 近20日K线 mini 图一致（同一尾部窗口）。仅具体标的行有数据。
  const macdSpark = (dif, dea, hist) => {
    if (!Array.isArray(hist) || !hist.length) return '<span class="spark-empty">—</span>';
    const n = hist.length;
    const values = [0];
    const collect = (series) => (Array.isArray(series) ? series : []).forEach((v) => {
      if (Number.isFinite(v)) values.push(v);
    });
    collect(hist);
    collect(dif);
    collect(dea);
    let vMin = Math.min(...values);
    let vMax = Math.max(...values);
    if (vMax <= vMin) vMax = vMin + 1e-6;
    const pad = (vMax - vMin) * 0.08;
    vMin -= pad;
    vMax += pad;
    const y = (value) => 100 - ((Math.min(vMax, Math.max(vMin, value)) - vMin) / (vMax - vMin) * 100);
    const step = 100 / n;
    const barW = step * 0.62;
    const zeroY = y(0);
    const parts = [];
    hist.forEach((v, i) => {
      if (!Number.isFinite(v)) return;
      const cx = (i + 0.5) * step;
      const color = v >= 0 ? '#c13f3a' : '#187b5f';
      const top = Math.min(y(v), zeroY);
      const height = Math.max(Math.abs(y(v) - zeroY), 0.8);
      parts.push(`<rect x="${(cx - barW / 2).toFixed(1)}" y="${top.toFixed(1)}" width="${barW.toFixed(1)}" height="${height.toFixed(1)}" fill="${color}"/>`);
    });
    if (!parts.length) return '<span class="spark-empty">—</span>';
    const lineFor = (series) => (Array.isArray(series) ? series : [])
      .map((v, i) => Number.isFinite(v) ? `${((i + 0.5) * step).toFixed(1)},${y(v).toFixed(1)}` : '')
      .filter(Boolean).join(' ');
    const difLine = lineFor(dif);
    const deaLine = lineFor(dea);
    return `<svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-label="MACD"><line class="mspark-zero" x1="0" y1="${zeroY.toFixed(1)}" x2="100" y2="${zeroY.toFixed(1)}"/>${parts.join('')}${deaLine ? `<polyline class="mspark-dea" points="${deaLine}"/>` : ''}${difLine ? `<polyline class="mspark-dif" points="${difLine}"/>` : ''}</svg>`;
  };
  // ---- 扁平表格渲染（每行一个标的）+ 左侧分类目录 ---------------------------
  const safeAnchorId = (l1, l2, index) => TQ.safeAnchorId('subject-section', l1, l2, index);
  // 从嵌套 payload 提取二级类目锚点清单：侧栏显示金叉/死叉家数。
  const collectSections = (groups) => {
    const sections = [];
    for (const group of groups || []) {
      for (const l2 of group.items || []) {
        let golden = l2.macd_golden_count;
        let dead = l2.macd_dead_count;
        if (golden == null && dead == null) {
          const members = (l2.children || []).flatMap((l3) => l3.children || []);
          golden = members.filter((inst) => inst.macd_phase === 'golden').length;
          dead = members.filter((inst) => inst.macd_phase === 'dead').length;
        }
        sections.push({
          id: safeAnchorId(group.category_l1, l2.category_l2, sections.length + 1),
          l1: group.category_l1,
          l2: l2.category_l2,
          golden,
          dead,
        });
      }
    }
    return sections;
  };
  const setActiveSideNav = (sectionId) => TQ.setActiveSideNav(sideNavEl, sectionId);
  const renderSideNav = (sections) => {
    const cross = (section) =>
      `<span class="is-positive">${section.golden || 0}</span>/<span class="is-negative">${section.dead || 0}</span>`;
    TQ.renderSideNav(sideNavEl, sections, cross);
  };
  const sectionSync = TQ.createSectionSync(sideNavEl, board, '.subject-l2-head[id]');
  const requestActiveSync = sectionSync.request;
  sideNavEl.addEventListener('click', (event) => {
    const btn = event.target.closest('[data-section-id]');
    if (!btn) return;
    const section = document.getElementById(btn.getAttribute('data-section-id') || '');
    if (!section) return;
    setActiveSideNav(section.id);
    section.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
  window.addEventListener('scroll', requestActiveSync, { passive: true });
  window.addEventListener('resize', requestActiveSync);

  // ---- 类目内排序（强度 / 金叉·死叉相位）------------------------------------
  // 交互与手工交易持仓表一致：点击表头循环 降序 → 升序 → 原始顺序，
  // 缺失值恒排最后、同值保持原始相对顺序（稳定）。排序只重排每个类目行
  // （页面上带金叉/死叉计数的那一级）内部的标的行，类目分组顺序不变。
  // key: null=原始顺序 | 'strength'=强度 | 'phase'=MACD 相位 | 'change'=日变动。
  // 相位排序是「强弱序」而非纯天数序：金叉按天数降序在前、死叉按天数升序
  // 在后（金叉5天 > 金叉1天 > 死叉1天 > 死叉5天），用 金叉=+天数 / 死叉=-天数
  // 的秩实现，升序即整体反转。
  let boardSort = { key: null, dir: 0 };
  let boardData = null;
  const sortValueFor = (item, key) => {
    if (key === 'strength') return Number.isFinite(item.strength) ? item.strength : null;
    if (key === 'change') return Number.isFinite(item.daily_change_pct) ? item.daily_change_pct : null;
    if (key === 'holding') return Number.isFinite(item.holding_value) ? item.holding_value : null;
    if (key === 'phase') {
      if (!Number.isFinite(item.macd_phase_days)) return null;
      return item.macd_phase === 'golden' ? item.macd_phase_days : -item.macd_phase_days;
    }
    return null;
  };
  const cycleBoardSort = (key) => {
    boardSort = cycleSort(boardSort, key);
    if (boardData) renderBoard(boardData);
  };
  const sortHeadHtml = (key, label, tip) => {
    const active = boardSort.key === key && boardSort.dir !== 0;
    const arrow = active ? (boardSort.dir === -1 ? '⬇️' : '⬆️') : '';
    return `<span class="sort-head${active ? ' is-sorted' : ''}" data-sort-key="${key}" title="点击排序（${esc(tip)}）：降序 → 升序 → 原始顺序">${label}<span class="sort-arrow">${arrow}</span></span>`;
  };
  // 具体标的行：持仓金额/相位/日变动/近30日K线/MACD/趋势MA5/强度/操作（查看+试算）。
  const renderInstrumentRow = (item) => {
    const viewBtn = item.symbol
      ? `<a class="index-view-btn" href="/market-view?symbol=${encodeURIComponent(item.symbol)}" title="在标的查看页打开 ${esc(item.symbol)}">查看</a>`
      : '';
    const trialBtn = item.symbol
      ? `<button type="button" class="index-view-btn index-trial-btn" data-symbol="${esc(item.symbol)}" title="试算 ${esc(item.symbol)} 的止损价与仓位">试算</button>`
      : '';
    // 持仓金额：服务端按「看板最新价（快照模式下为实时报价合成K线收盘）× 份数」
    // 实时重算并附加；无持仓则为空。悬停提示带具体计算过程（与手工交易页同风格）。
    let holdingCell = '';
    let holdingTitle = '';
    if (Number.isFinite(item.holding_value)) {
      const fmtShares = (v) => Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 0 });
      const fmtMoney0 = (v) => '¥' + Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 0 });
      holdingCell = `<strong>${fmtMoney0(item.holding_value)}</strong>` +
        (Number.isFinite(item.holding_weight) ? `<span>占 ${item.holding_weight}%</span>` : '');
      const totalText = Number.isFinite(boardData?.holdings_total)
        ? `；占比 = ${fmtMoney0(item.holding_value)} ÷ 看板内持仓合计 ${fmtMoney0(boardData.holdings_total)} = ${item.holding_weight}%`
        : '';
      holdingTitle = `持仓金额 = 最新价 ${item.holding_price} × ${fmtShares(item.holding_shares)} 份 = ${fmtMoney0(item.holding_value)}${totalText}（随快照每 5 分钟重算）`;
    }
    return `<div class="index-row subject-inst-row" data-symbol="${esc(item.symbol || '')}">
      <div class="index-name"><strong>${esc(item.name || item.symbol)}</strong><span>${esc(item.symbol)} · 具体标的</span></div>
      <div class="index-holding" ${holdingTitle ? `title="${esc(holdingTitle)}"` : ''}>${holdingCell}</div>
      <div class="index-kspark">${klineSpark(item.kline || [], item.kline_ma5 || [], item.macd_dif || [], item.macd_dea || [])}<div class="chart-cross" hidden></div></div>
      <div class="index-change ${getChangeClass(item.daily_change_pct)}">${fmtChange(item.daily_change_pct)}</div>
      <div class="index-mspark">${macdSpark(item.macd_dif || [], item.macd_dea || [], item.macd_hist || [])}<div class="chart-cross" hidden></div></div>
      <div class="index-phase">${fmtPhase(item)}</div>
      <div class="index-spark">${sparkline(item.trend_history || [], item.trend_dates || [], item.trend_upper_history || [], item.trend_lower_history || [])}</div>
      <div class="index-strength">${item.strength == null ? '—' : item.strength}</div>
      <div class="index-view">${viewBtn}${trialBtn}</div>
    </div>`;
  };
  const renderGroup = (group, sections) => {
    const golden = (group.items || []).reduce((sum, l2) => sum + (l2.macd_golden_count || 0), 0);
    const dead = (group.items || []).reduce((sum, l2) => sum + (l2.macd_dead_count || 0), 0);
    const cross = fmtCrossCounts(golden, dead);
    const headMeta = `${group.count} 个二级类目${cross ? ` · ${cross}` : ''}`;
    const body = (group.items || []).map((l2) => {
      const section = sections.find((s) => s.l1 === group.category_l1 && s.l2 === l2.category_l2);
      const dividerCross = fmtCrossCounts(l2.macd_golden_count, l2.macd_dead_count);
      // 类目行下挂的所有标的整体参与排序（L3 在页面上无分隔行，用户视角的
      // 「三级类目」就是这个类目行下的全部行）。
      const children = (l2.children || []).flatMap((l3) => l3.children || []);
      const ordered = boardSort.key ? stableSorted(children, boardSort, (item) => sortValueFor(item, boardSort.key)) : children;
      const rows = ordered.map(renderInstrumentRow).join('');
      return `<div class="subject-l2-head"${section ? ` id="${esc(section.id)}"` : ''}><strong>${esc(l2.category_l2)}</strong><span class="subject-l2-meta">${dividerCross}</span></div>${rows}`;
    }).join('');
    const gridHead = `<div class="index-grid-head"><span>标的</span>${sortHeadHtml('holding', '持仓金额', '按持仓金额（看板最新价×持仓份数），各类目内；无持仓为空')}<span>近30日K线</span>${sortHeadHtml('change', '日变动', '按日涨跌幅，各类目内')}<span>MACD</span>${sortHeadHtml('phase', 'MACD相位', '金叉久 > 金叉新 > 死叉新 > 死叉久，各类目内')}<span>近3月趋势 MA5</span>${sortHeadHtml('strength', '强度', '按强度，各类目内')}<span></span></div>`;
    return `<section class="index-provider subject-l1"><div class="index-provider-head"><h2>${esc(group.category_l1)}</h2><span>${headMeta}</span></div><div class="index-table-scroll">${gridHead}${body}</div></section>`;
  };
  const renderBoard = (data) => {
    boardData = data;
    boardSections = collectSections(data.groups || []);
    board.innerHTML = (data.groups || []).map((group) => renderGroup(group, boardSections)).join('');
    renderSideNav(boardSections);
    sectionSync.sync();
    // 悬停提示数据索引：symbol → K线/MACD 序列（含日期、涨跌幅）。
    chartData = {};
    for (const group of data.groups || []) {
      for (const l2 of group.items || []) {
        for (const l3 of l2.children || []) {
          for (const inst of l3.children || []) {
            chartData[inst.symbol] = inst;
          }
        }
      }
    }
  };

  // ---- mini 图悬停提示（日K / MACD）---------------------------------------
  const chartTip = document.createElement('div');
  chartTip.className = 'chart-tip';
  chartTip.hidden = true;
  document.body.appendChild(chartTip);
  let chartData = {};
  let chartCrossEl = null;

  const fmtPrice = (value) => value == null || !Number.isFinite(value) ? '—' : Number(value.toFixed(3)).toString();
  const fmtMacdVal = (value) => value == null || !Number.isFinite(value) ? '—' : value.toFixed(4);
  const fmtPct = (value) => {
    if (value == null || !Number.isFinite(value)) return '<span>—</span>';
    const cls = value >= 0 ? 'heat-tip-pos' : 'heat-tip-neg';
    return `<span class="${cls}">${value >= 0 ? '+' : ''}${value.toFixed(2)}%</span>`;
  };

  const hideChartTip = () => {
    chartTip.hidden = true;
    if (chartCrossEl) {
      chartCrossEl.hidden = true;
      chartCrossEl = null;
    }
  };

  const showChartTip = (event, container, html, index, count) => {
    chartTip.innerHTML = html;
    chartTip.hidden = false;
    const tipRect = chartTip.getBoundingClientRect();
    let x = event.clientX + 14;
    let y = event.clientY + 14;
    if (x + tipRect.width > window.innerWidth - 8) x = event.clientX - tipRect.width - 14;
    if (y + tipRect.height > window.innerHeight - 8) y = event.clientY - tipRect.height - 14;
    chartTip.style.left = `${Math.max(8, x)}px`;
    chartTip.style.top = `${Math.max(8, y)}px`;
    // 十字线：定位到当前柱子中心。
    const cross = container.querySelector('.chart-cross');
    if (cross) {
      if (chartCrossEl && chartCrossEl !== cross) chartCrossEl.hidden = true;
      cross.style.left = `${((index + 0.5) / count * 100).toFixed(2)}%`;
      cross.hidden = false;
      chartCrossEl = cross;
    }
  };

  const klineTipHtml = (k) => {
    const rows = [
      `<tr><td>开盘</td><td>${fmtPrice(k.o)}</td></tr>`,
      `<tr><td>最高</td><td>${fmtPrice(k.h)}</td></tr>`,
      `<tr><td>最低</td><td>${fmtPrice(k.l)}</td></tr>`,
      `<tr><td>收盘</td><td>${fmtPrice(k.c)}</td></tr>`,
      `<tr><td>涨跌幅</td><td>${fmtPct(k.pct)}</td></tr>`,
    ];
    if (k.a != null) rows.push(`<tr><td>成交额</td><td>${fmtAmount(k.a)}</td></tr>`);
    return `<div class="heat-tip-title">${esc(k.d || '')}</div><table class="heat-tip-table">${rows.join('')}</table>`;
  };
  const macdTipHtml = (date, dif, dea, hist) => {
    const histCls = hist != null && Number.isFinite(hist) ? (hist >= 0 ? 'heat-tip-pos' : 'heat-tip-neg') : '';
    return `<div class="heat-tip-title">${esc(date || '')}</div><table class="heat-tip-table">` +
      `<tr><td>DIF</td><td>${fmtMacdVal(dif)}</td></tr>` +
      `<tr><td>DEA</td><td>${fmtMacdVal(dea)}</td></tr>` +
      `<tr><td>MACD柱</td><td><span class="${histCls}">${fmtMacdVal(hist)}</span></td></tr>` +
      `</table>`;
  };
  let chartTipRafPending = false;
  let chartTipLastEvent = null;
  board.addEventListener('mousemove', (event) => {
    // rAF 节流（P2-20）：高频 mousemove 每帧最多处理一次
    chartTipLastEvent = event;
    if (chartTipRafPending) return;
    chartTipRafPending = true;
    window.requestAnimationFrame(() => {
      chartTipRafPending = false;
      handleBoardMouseMove(chartTipLastEvent);
    });
  });
  const handleBoardMouseMove = (event) => {
    if (!event) return;
    const svg = event.target.closest('.index-kspark svg, .index-mspark svg');
    const row = event.target.closest('.subject-inst-row');
    if (!svg || !row) {
      hideChartTip();
      return;
    }
    const inst = chartData[row.dataset.symbol];
    if (!inst) {
      hideChartTip();
      return;
    }
    const container = svg.parentElement;
    const isKline = container.classList.contains('index-kspark');
    const count = isKline ? (inst.kline || []).length : (inst.macd_hist || []).length;
    if (!count) {
      hideChartTip();
      return;
    }
    const rect = svg.getBoundingClientRect();
    const ratio = rect.width > 0 ? (event.clientX - rect.left) / rect.width : 0;
    const index = Math.max(0, Math.min(count - 1, Math.floor(ratio * count)));
    const html = isKline
      ? klineTipHtml(inst.kline[index])
      : macdTipHtml((inst.macd_dates || [])[index], (inst.macd_dif || [])[index], (inst.macd_dea || [])[index], (inst.macd_hist || [])[index]);
    showChartTip(event, container, html, index, count);
  };
  board.addEventListener('mouseleave', hideChartTip);
  window.addEventListener('scroll', hideChartTip, { passive: true });
  // 排序表头点击（与手工交易持仓表同一交互：降序 → 升序 → 原始顺序）。
  board.addEventListener('click', (event) => {
    const head = event.target.closest('.sort-head[data-sort-key]');
    if (head) cycleBoardSort(head.dataset.sortKey);
  });
  const fmtClock = (ts) => {
    if (!ts) return '';
    const d = new Date(ts);
    if (Number.isNaN(d.getTime())) return String(ts);
    const pad = (n) => String(n).padStart(2, '0');
    return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  };
  const boardCountsText = (data) =>
    `${data.groups.length || 0} 个一级类目、${data.secondary_count || 0} 个二级类目、${data.category_count || 0} 个三级类目`;
  function renderStatus(data) {
    if (!board.children.length) {
      statusEl.textContent = '暂无具备完整三级分类和本地日K数据的标的。';
      statusEl.className = 'context-hint';
      return;
    }
    if (data.is_intraday) {
      // 盘中/收盘快照：基于快照时刻的实时报价估算，非盘后确认值。
      statusEl.innerHTML = `<span class="intraday-dot"></span> 实时快照 ${fmtClock(data.snapshot_ts || data.intraday_ts)}（${data.as_of || ''}）· ${boardCountsText(data)}。⚠️ 基于快照时刻实时报价估算，盘后将以日K确认值为准`;
      statusEl.className = 'intraday-status-bar';
    } else {
      statusEl.textContent = `${data.as_of || '最新交易日'} 日K · ${boardCountsText(data)}。`;
      statusEl.className = 'context-hint';
    }
  }
  // session 过期（401）时统一跳回登录墙，登录后回到当前页（后台轮询不再无限刷 401）
  async function fetchBoard() {
    const response = await fetch('/subject-market/api/dashboard');
    if (response.status === 401) { redirectToLogin(); return new Promise(function() {}); }
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || '标的看板加载失败');
    return data;
  }
  async function loadBoard() {
    try {
      const data = await fetchBoard();
      renderBoard(data);
      renderHeatmap(data);
      renderedSnapshotTs = data.snapshot_ts || null;
      renderStatus(data);
    } catch (error) {
      statusEl.textContent = error.message || '标的看板加载失败';
      statusEl.className = 'context-hint';
    }
  }

  // ---- 定时快照状态轮询（算完自动整表替换） -----------------------------
  // 页面不再触发重算，快照只由盘中定时任务（交易时段每 5 分钟）更新；
  // 这里仅轮询任务状态：在跑时显示进度，快照时间戳变化时拉取并整表替换。
  let refreshPollToken = 0;
  const REFRESH_POLL_INTERVAL_MS = 2000;
  async function autoRefreshBoard() {
    const token = ++refreshPollToken;
    // 本地记录已见到的快照时间戳，而非依赖 renderedSnapshotTs —— 盘后切回
    // EOD 看板时后者为 null，但 refresh-status 仍返回旧快照时间戳，直接
    // 对比会导致反复重载。
    let lastSeenTs = renderedSnapshotTs;
    let shownError = null;
    let baseText = null; // 进入「在跑」状态时的状态栏文本，用于叠加进度
    let pollFailures = 0;
    for (;;) {
      await new Promise((r) => setTimeout(r, REFRESH_POLL_INTERVAL_MS));
      if (token !== refreshPollToken) return; // 看板已被重新加载，旧轮询作废
      if (document.hidden) continue; // 页面不可见时暂停轮询（P2-20）
      let st;
      try {
        const resp = await fetch('/subject-market/api/dashboard/refresh-status');
        if (resp.status === 401) { redirectToLogin(); return; }
        st = await resp.json();
        pollFailures = 0;
      } catch (e) {
        // 失败退避：连续失败按 2s/10s/30s 封顶，不再每 2s 空转
        pollFailures += 1;
        await new Promise((r) => setTimeout(r, Math.min(30000, pollFailures * 8000)));
        continue;
      }
      if (st.running) {
        if (baseText === null) baseText = statusEl.innerHTML;
        const pct = Math.round((st.percent || 0) * 100);
        statusEl.innerHTML = `${baseText} <span class="intraday-refreshing">· 正在后台更新实时数据${pct ? ` ${pct}%` : ''}…</span>`;
        continue;
      }
      baseText = null;
      if (st.snapshot_ts && st.snapshot_ts !== lastSeenTs) {
        // 新一轮定时快照已落库：整表替换。
        lastSeenTs = st.snapshot_ts;
        shownError = null;
        await loadBoard();
        continue;
      }
      if (st.last_error && st.last_error !== shownError) {
        shownError = st.last_error;
        statusEl.innerHTML = `${statusEl.innerHTML} <span class="intraday-refreshing">· 实时更新失败：${esc(st.last_error)}（当前为最近快照）</span>`;
      }
    }
  }

  // ---- 仓位试算弹窗（与手工交易页同接口、同渲染口径；独立作用域避免命名冲突）----
  (() => {
    const modal = document.getElementById('trialModal');
    const titleEl = document.getElementById('trialTitle');
    const closeBtn = document.getElementById('trialCloseBtn');
    const symbolEl = document.getElementById('trialSymbol');
    const buyDateEl = document.getElementById('trialBuyDate');
    const buyPriceEl = document.getElementById('trialBuyPrice');
    const riskBudgetEl = document.getElementById('trialRiskBudget');
    const runBtn = document.getElementById('trialRunBtn');
    const hintEl = document.getElementById('trialPriceHint');
    const msgEl = document.getElementById('trialMessage');
    const resultEl = document.getElementById('trialResult');
    const summaryTitleEl = document.getElementById('trialSummaryTitle');
    const stopStatsEl = document.getElementById('trialStopStats');
    const sizingEl = document.getElementById('trialSizing');
    const STOP_MODE = 'tight'; // 与手工交易页默认口径一致
    let dayRange = null;
    let rangeSeq = 0;

    const showMsg = (text, isError) => {
      msgEl.textContent = text || '';
      msgEl.classList.toggle('is-error', !!isError);
      msgEl.hidden = !text;
    };

    const positionSizingTip = (data) => {
      const ps = data.position_sizing || {};
      const stops = data.stops || {};
      if (ps.risk_per_share == null) return '';
      return `每股风险 = 买入价 ${fmtPrice(data.buy_price)} − 硬止损价 ${fmtPrice(stops.hard_stop_price)} = ${fmtPrice(ps.risk_per_share)}；可买份数 = 风险预算 ¥${Number(ps.risk_budget).toFixed(2)} ÷ 每股风险 ${fmtPrice(ps.risk_per_share)}，下取整到百位 = ${ps.max_qty} 份；若硬止损触发亏损约 ¥${Number(ps.max_loss).toFixed(2)}，按买入价计持仓约 ¥${Number(ps.position_value).toFixed(2)}`;
    };
    function renderSizingLine(data) {
      const ps = data.position_sizing;
      if (!ps) {
        sizingEl.hidden = true;
        sizingEl.innerHTML = '';
        return;
      }
      sizingEl.innerHTML = withTip(
        `最大可买入份数 <b>${ps.max_qty} 份</b><span class="mt-stat-note">（风险预算 ¥${Number(ps.risk_budget).toFixed(2)}）</span>　｜　最大可买入金额 <b>¥${Number(ps.position_value).toFixed(2)}</b><span class="mt-stat-note">＝ ${ps.max_qty} 份 × 买入价 ${esc(fmtPrice(data.buy_price))}</span>`,
        positionSizingTip(data));
      sizingEl.hidden = false;
    }

    // 当日K线（含盘中合成）：无K线返回 null（非交易日），请求失败抛错。

    async function refreshRangeHint() {
      const symbol = symbolEl.value.trim();
      const buyDate = buyDateEl.value;
      dayRange = null;
      hintEl.hidden = true;
      if (!symbol || !buyDate) return;
      const seq = ++rangeSeq;
      try {
        const candle = await fetchDayCandle(symbol, buyDate);
        if (seq !== rangeSeq) return;
        if (!candle) {
          hintEl.textContent = `${buyDate} 无K线数据（非交易日或未入库），买入价将按下一交易日口径计算`;
          hintEl.hidden = false;
          return;
        }
        dayRange = { date: buyDate, low: candle.low, high: candle.high, close: candle.close };
        hintEl.innerHTML = `${buyDate} 价格区间：<b>${fmtPrice(dayRange.low)} ~ ${fmtPrice(dayRange.high)}</b>（收盘 ${fmtPrice(dayRange.close)}），价格需落在区间内`;
        hintEl.hidden = false;
      } catch (err) {
        if (seq !== rangeSeq) return;
        hintEl.textContent = err.message || '无法获取当日行情';
        hintEl.hidden = false;
      }
    }

    async function openTrial(symbol) {
      const inst = chartData[symbol] || {};
      resultEl.hidden = true;
      hintEl.hidden = true;
      showMsg('');
      symbolEl.value = symbol;
      riskBudgetEl.value = '';
      titleEl.textContent = `仓位试算 — ${inst.name || symbol}`;
      const candles = inst.kline || [];
      const last = candles[candles.length - 1] || {};
      buyDateEl.value = last.d || localIso(new Date());
      buyPriceEl.value = last.c != null ? last.c : '';
      modal.hidden = false;
      // 默认买入日期=当天（今天是交易日时）、价格=最新价：当日K线未落库时
      // 由实时报价合成；今天非交易日则保留看板末日K（最新交易日）默认值。
      const seq = ++rangeSeq;
      try {
        const candle = await fetchDayCandle(symbol, localIso(new Date()));
        if (candle && seq === rangeSeq && !modal.hidden) {
          buyDateEl.value = localIso(new Date());
          buyPriceEl.value = candle.close;
        }
      } catch (err) { /* 拉取失败则保留看板默认值 */ }
      refreshRangeHint();
    }

    async function runTrial() {
      const symbol = symbolEl.value.trim();
      const buyDate = buyDateEl.value;
      const buyPrice = parseFloat(buyPriceEl.value);
      const riskBudget = parseFloat(riskBudgetEl.value);
      if (!symbol) { showMsg('请输入标的代码', true); return; }
      if (!buyDate) { showMsg('请选择买入日期', true); return; }
      if (!(buyPrice > 0)) { showMsg('请输入大于 0 的买入价格', true); return; }
      if (riskBudgetEl.value.trim() && !(riskBudget > 0)) { showMsg('风险预算需为大于 0 的金额（选填）', true); return; }
      if (dayRange && dayRange.date === buyDate && (buyPrice < dayRange.low || buyPrice > dayRange.high)) {
        showMsg(`买入价格 ${fmtPrice(buyPrice)} 超出 ${buyDate} 当日价格区间 [${fmtPrice(dayRange.low)}, ${fmtPrice(dayRange.high)}]`, true);
        return;
      }
      const form = { symbol, buy_date: buyDate, buy_price: buyPrice, stop_mode: STOP_MODE };
      if (riskBudget > 0) form.risk_budget = riskBudget;
      runBtn.disabled = true;
      showMsg('计算中…', false);
      try {
        const data = await postJson('/manual-trade/api/evaluate', form);
        const stops = data.stops || {};
        const intradayTime = data.intraday_ts ? String(data.intraday_ts).slice(11, 16) : '';
        summaryTitleEl.textContent = `止损价 — ${data.name ? data.name + ' ' : ''}${data.symbol} · ${data.buy_date} 买入 @ ${fmtPrice(data.buy_price)}${data.is_intraday ? `（含今日盘中实时数据 ${intradayTime}）` : ''}`;
        renderStopStats(stopStatsEl, stops);
        renderSizingLine(data);
        resultEl.hidden = false;
        showMsg('');
      } catch (err) {
        resultEl.hidden = true;
        showMsg(err.message || '计算失败', true);
      } finally {
        runBtn.disabled = false;
      }
    }

    board.addEventListener('click', (event) => {
      const btn = event.target.closest('.index-trial-btn');
      if (btn) openTrial(btn.dataset.symbol);
    });
    closeBtn.addEventListener('click', () => { modal.hidden = true; });
    modal.addEventListener('click', (event) => { if (event.target === modal) modal.hidden = true; });
    buyDateEl.addEventListener('change', refreshRangeHint);
    runBtn.addEventListener('click', runTrial);
  })();

  // Init.
  loadBoard().then(autoRefreshBoard);
  // 盘后数据更新完成后重载：今日日K落库后 /api/dashboard 自动切回 EOD 确认值。
  document.addEventListener('daily-update-done', function() {
    loadBoard();
  });
})();
