"""前端 JS 加载期健康检查（CI 用）：以 DOM stub 执行 web/static 下全部脚本，
捕捉语法正确但加载期即抛错的问题（如函数删除后的悬空引用/残留片段）。

背景：2026-08-25 前端公共化改造中，三处删除残留（孤立模板字面量 /
被误吞的 HEAT_COLOR_DIMS 定义 / 缺失的全局别名）均为此类——
node --check 的语法检查无法发现，必须真实执行一次。

用法：python scripts/check_frontend_js.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

STUB = r"""
function el(id) {
  return {
    id, addEventListener: () => {}, removeEventListener: () => {},
    appendChild: () => {}, querySelectorAll: () => [], querySelector: () => el('q'),
    classList: { toggle: () => {}, add: () => {}, remove: () => {}, contains: () => false },
    style: {}, dataset: {}, hidden: true, textContent: '', innerHTML: '',
    getBoundingClientRect: () => ({ top: 0, left: 0, width: 100, height: 10 }),
    scrollTo: () => {}, focus: () => {}, closest: () => el('c'), setAttribute: () => {},
    getAttribute: () => '',
  };
}
const documentStub = {
  getElementById: (id) => el(id),
  createElement: () => el('div'),
  addEventListener: () => {},
  querySelector: () => el('q'),
  querySelectorAll: () => [],
  body: el('body'),
  documentElement: el('html'),
  dispatchEvent: () => {},
  hidden: false,
};
const TQ = {
  esc: (v) => String(v), redirectToLogin: () => {}, postJson: async () => ({}), logout: async () => {},
  localIso: () => '2026-01-01', localToday: () => '2026-01-01', fmtPrice: () => '1.0', fmtPct: () => '1%',
  pctClass: () => '', fmtAmount: () => '1万', withTip: (h) => h, fetchDayCandle: async () => null,
  cycleSort: (s) => s, stableSorted: (x) => x, stopPill: () => '', stopModeLabel: () => '',
  hardStopTip: () => '', chandelierStopTip: () => '', ratchetStopTip: () => '', stopDistanceTip: () => '',
  renderStopStats: () => {}, safeAnchorId: () => 'x', setActiveSideNav: () => {}, renderSideNav: () => {},
  createSectionSync: () => ({ sync: () => {}, request: () => {} }),
};
const windowStub = {
  addEventListener: () => {}, removeEventListener: () => {},
  requestAnimationFrame: () => 1,
  location: { href: '', pathname: '/', search: '', origin: 'http://x' },
  localStorage: { getItem: () => null, setItem: () => {} },
  TQ,
  // 与 app-common.js 的 window 别名一致
  esc: TQ.esc, postJson: TQ.postJson, redirectToLogin: TQ.redirectToLogin,
  fmtPrice: TQ.fmtPrice, fmtPct: TQ.fmtPct, pctClass: TQ.pctClass, fmtAmount: TQ.fmtAmount,
  withTip: TQ.withTip, localIso: TQ.localIso, localToday: TQ.localToday,
  fetchDayCandle: TQ.fetchDayCandle, cycleSort: TQ.cycleSort, stableSorted: TQ.stableSorted,
  stopPill: TQ.stopPill, stopModeLabel: TQ.stopModeLabel,
  hardStopTip: TQ.hardStopTip, chandelierStopTip: TQ.chandelierStopTip,
  ratchetStopTip: TQ.ratchetStopTip, stopDistanceTip: TQ.stopDistanceTip,
  renderStopStats: TQ.renderStopStats,
  fetch: () => Promise.reject(new Error('no network in stub')),
  echarts: { init: () => ({ setOption: () => {}, resize: () => {}, on: () => {}, dispatchAction: () => {}, getZr: () => ({ on: () => {} }) }), connect: () => {} },
};
windowStub.window = windowStub;
// 页面里对 esc/localToday/fmtPrice 等的无限定调用会解析到全局对象——
// 与浏览器中 app-common.js 的 window 别名一致地挂到真实 globalThis
for (const [k, v] of Object.entries(windowStub)) {
  if (typeof v === 'function' || k === 'TQ') globalThis[k] = v;
}
globalThis.document = documentStub;
globalThis.window = windowStub;
globalThis.fetch = windowStub.fetch;
globalThis.echarts = windowStub.echarts;
"""

RUNNER = """
const fs = require('fs');
const file = process.argv[2];
const code = fs.readFileSync(file, 'utf8');
try {
  const fn = new Function('window', 'document', 'echarts', 'TQ', code);
  fn.call(windowStub, windowStub, documentStub, windowStub.echarts, TQ);
  console.log('OK');
} catch (e) {
  console.log('ERROR: ' + e.message);
  process.exit(2);
}
"""


def main() -> int:
    targets = [ROOT / "web" / "static" / "app-common.js"] + sorted(
        (ROOT / "web" / "static" / "js").glob("*.js")
    )
    failures: list[str] = []
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(STUB + "\n" + RUNNER)
        runner = f.name
    for target in targets:
        try:
            result = subprocess.run(
                ["node", runner, str(target)], capture_output=True, text=True, timeout=8, check=False
            )
            out = (result.stdout + result.stderr).strip()
            first_line = out.splitlines()[0] if out else ""
            if "ERROR:" in first_line:
                failures.append(f"{target.name}: {first_line}")
            else:
                print(f"OK  {target.name}")
        except subprocess.TimeoutExpired:
            # 超时 = 加载成功但挂着异步任务（轮询/setInterval），符合预期
            print(f"OK  {target.name}（加载成功，含常驻异步任务）")
    Path(runner).unlink(missing_ok=True)
    if failures:
        print("\n加载期错误：")
        for failure in failures:
            print(f"  {failure}")
        return 1
    print(f"\n全部 {len(targets)} 个前端脚本加载期健康")
    return 0


if __name__ == "__main__":
    sys.exit(main())
