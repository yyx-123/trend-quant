---
name: trend-quant-mcp
description: >
  通过 MCP 连接 Trend Quant 后端服务器，获取 A 股 ETF 的趋势量化分析
  （趋势看板、标的详情、技术指标、止损计算）和手工交易（录入、持仓）能力。
  当用户需要查看市场趋势、分析 ETF 技术面、计算止损价、查询标的、
  记录交易或查看持仓时触发使用。
  服务端会持续新增和调整工具，可用工具清单以运行时 tools/list 返回为准。
---

# Trend Quant MCP 服务

A 股 ETF 趋势量化后端，以 MCP 服务器形式远程提供。本 Skill 说明**如何连接和调用**该服务；具体有哪些工具、每个工具的参数与返回值，**一律以运行时 `tools/list` 的返回为准**（服务端会持续新增/调整工具，不要假设存在固定工具清单）。

## 连接信息

| 项目 | 值 |
|---|---|
| 公网 URL | `http://121.199.173.214:9000/mcp/sse` |
| 服务器本机/内网 URL | `http://127.0.0.1:9000/mcp/sse`（或 `http://<内网IP>:9000/mcp/sse`） |
| 传输协议 | MCP over **SSE**（Server-Sent Events） |
| 认证 | 无（当前版本未启用 API Key） |

端口说明：服务进程监听 9000，经 frp TCP 隧道映射到公网 `121.199.173.214:9000`，两者等价。在服务器本机调用时优先用本地地址，不依赖 frp 隧道状态。

## 客户端配置

支持 MCP 的客户端（Kimi Code、Claude Code、Cursor 等）在 MCP 配置中加入：

```json
{
  "mcpServers": {
    "trend-quant": {
      "url": "http://121.199.173.214:9000/mcp/sse"
    }
  }
}
```

配置后该服务器的工具会以 `mcp__trend-quant__<工具名>` 的形式出现在工具列表中，直接调用即可。

## 工具发现（重要）

**不要凭记忆或文档假设工具清单。** 每次会话中如需了解可用能力，调用 MCP 标准的 `tools/list` 方法（MCP 客户端通常自动完成），返回中包含每个工具的：

- `name`：工具名
- `description`：功能说明、适用场景、使用注意事项（服务端维护，是最权威的用法文档）
- `inputSchema`：参数的 JSON Schema（名称、类型、默认值、是否必填）

调用工具前若不确定参数，先看该工具的 `inputSchema` 和 `description`。

## 原始协议调用（无 MCP 客户端时）

标准 MCP SSE 握手流程（已实测可用，协议版本 `2024-11-05`）：

```bash
# 1. 建立 SSE 长连接，第一条事件给出消息端点（含 session_id）
curl -N -H 'Accept: text/event-stream' http://121.199.173.214:9000/mcp/sse
# → event: endpoint
# → data: /mcp/messages/?session_id=<SESSION_ID>

# 2. 保持 SSE 连接不断开，向消息端点 POST JSON-RPC 请求
#    （POST 只返回 "Accepted"，真正的响应通过 SSE 流推送）
curl -X POST "http://121.199.173.214:9000/mcp/messages/?session_id=<SESSION_ID>" \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"manual","version":"0.1"}}}'

# 3. 发送 initialized 通知后即可调用 tools/list、tools/call
curl -X POST "http://121.199.173.214:9000/mcp/messages/?session_id=<SESSION_ID>" \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}'
curl -X POST "http://121.199.173.214:9000/mcp/messages/?session_id=<SESSION_ID>" \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
```

注意：`session_id` 只在 SSE 连接存活期间有效，连接断开需重新握手。

---

## 领域知识

解读工具返回数据所需的领域概念（与具体工具无关，较为稳定）。

### 趋势值（Trend Score）

趋势值是 Trend Quant 的核心指标，范围 -100 ~ +100，用于判断标的当前的趋势方向和强度。

- **正趋势值** → 上升趋势，数值越大越强
- **负趋势值** → 下降趋势，数值越小越强
- **接近 0** → 震荡市，方向不明确

计算公式：Trend Score = Price Direction × Confidence

**Price Direction**（价格方向分）由两个部分合成：
- **Bias**（价格偏离）：当前价格偏离均线的程度，用 ATR 标准化。短中长周期（5/10/20 日）加权混合。
- **Slope**（均线斜率）：均线上升/下降的速度，同样用 ATR 标准化。

**Confidence**（置信度）由两个因子合成：
- **成交量因子**：近期成交量相对均量的放大程度
- **效率比率（ER）**：价格运动的平滑程度，趋势越顺畅 ER 越高

### 趋势值 MA5（Trend MA5）

趋势值的 5 日均值，是**看板类数据的主要排序指标**。与单点趋势值相比，MA5 更能反映趋势的持续性而非短期波动。

### 强度百分位（Strength）

在同级别分类中，某标的的 trend_ma5 在所有同类中的百分位（0-100）。数值越大表示在该分类中趋势越强。

### 趋势相位（Trend Phase）

对趋势状态的定性信号，基于趋势值与 MA5 的绝对水平判定（不是方向比较）：

- **趋势启动（start）**：trend_score ≥ 5 且 trend_ma5 ≥ 0
- **趋势结束（end）**：trend_score ≤ -5 且 trend_ma5 ≤ 0
- **其余情况**：无相位（None）

相位信息还包含：`days`（相位已持续的交易日数，信号日为第 1 天）、`change_pct`（信号日收盘 → 最新收盘的涨跌幅）、`signal_date`（信号触发日）。

### 硬止损（Hard Stop）

买入后立即生效的止损线，计算方式：买入价 − 买入日 ATR(20) × 1.5（默认值，标的可自定义倍数覆盖）。

目的是在趋势判断错误时快速止损，防止损失扩大。止损位应该在买入前计算好，作为风险控制的重要参考。

### 吊灯止损（Chandelier Stop）

持仓期间动态调整的止损线，计算方式：买入以来最高价 − 最新 ATR(20) × 2.5。

随着价格上涨，吊灯止损会自动上移，锁定利润。但不会随价格下跌下移。

---

## 通用注意事项

- **标的代码格式**：支持 `510300.SS`（带后缀）和 `510300`（自动补全）。`.SS` 上海、`.SZ` 深圳。
- **数据口径**：日 K 数据依赖后端每日 16:30 的数据更新，最新一根 K 线通常是上一个交易日；盘中实时数据仅在交易日 9:30-15:00（含午间休盘）可得，其余时段相关工具会返回提示或静默回退为日 K 口径。具体行为以各工具的 description 为准。
- **写操作类工具**（如手工交易录入）：需要用户提供手工交易账号的用户名和密码（与网页端同一套账号），调用前先向用户索取；这类工具操作用户的真实交易记录，调用前必须与用户确认关键参数（日期、价格、数量）无误。
- **性能**：看板类数据有缓存通常秒返；全市场（600+ 标的）的实时计算可能需要 1 分钟以上，能缩小范围就缩小范围。
