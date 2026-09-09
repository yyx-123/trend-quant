# 线上操作手册：云服务器 nginx 为 MCP SSE 开启 gzip（2026-09-06）

> 目标环境：云服务器（nginx/1.28.3，80 端口统一反代 → frp 隧道 → WSL 后端）。
> 本次只改云服务器上的 nginx 配置，后端代码不变，无需重启后端服务。

## 背景

MCP `dashboard` 看板响应（full 约 8.8MB / lite 约 1.4MB 未压缩 JSON）经
SSE 通道（`text/event-stream`）传输，实测只有 170~215KB/s——full 要 52s。

定位结论（2026-09-05 实测）：

- 本地 → 云服务器 RTT 16ms、0% 丢包，境内链路无高延迟问题；
- 同链路静态文件（经 nginx gzip）单流可达 680KB/s，带宽不是瓶颈；
- `/mcp/sse` 响应头**无 `Content-Encoding`**——nginx 的 `gzip_types`
  默认不含 `text/event-stream`，看板 JSON 逐字节裸传；
- 看板 JSON 重复度极高，gzip 实测压缩比 7.4:1（1.08MB → 146KB）。

## 改动

在云服务器 nginx 反代 `/mcp` 的 server/location 块中补充：

```nginx
location /mcp {
    # …… 原有 proxy_pass / proxy_http_version / proxy_set_header 等保持不变 ……

    proxy_buffering off;              # SSE 必须关缓冲（现有配置应已有，确认即可）

    gzip on;
    gzip_types text/event-stream application/json;
    gzip_min_length 1k;
}
```

说明：

- 看板响应是单条大消息一次写出，gzip 缓冲随写冲刷，不破坏 SSE 语义；
- 唯一副作用：SSE 心跳注释（约 15s 一次）可能被压缩缓冲略微延迟，
  MCP 客户端不依赖心跳计时，无影响；
- `gzip_types` 在 location 内声明会**覆盖** http 块的全局值——如果全局
  已有 `gzip_types`（静态 js/css 已被压缩说明 gzip 已开），请把
  `text/event-stream` 追加进全局列表，而不是在 location 里新建一份。

检查后 reload：

```bash
sudo nginx -t && sudo systemctl reload nginx
```

## 验证

```bash
# 1. SSE 响应头出现 Content-Encoding: gzip 即生效
curl -sN --max-time 3 -H "Authorization: Bearer <token>" \
  -H "Accept-Encoding: gzip" -D - -o /dev/null http://47.114.72.132/mcp/sse

# 2. 看板实测耗时（lite 应从约 6.6s 降到 1s 量级）
cd ~/.agents/skills/daily-trade-report/scripts && python3 - <<'EOF'
import sys, time
sys.path.insert(0, ".")
from common import McpClient
client = McpClient()
t0 = time.time()
board = client.call("dashboard", {"detail": "lite"})
print(f"dashboard lite: {time.time()-t0:.2f}s  data_mode={board.get('data_mode')}  标的数={board.get('instrument_count')}")
EOF
```

## 预期效果

| 响应 | 改前 | 改后（预期） |
|---|---|---|
| dashboard full（8.84MB → 约 1.2MB） | 52s | 2~7s |
| dashboard lite（1.41MB → 约 190KB） | 6.6s | 约 1s |

SSE 通道本身的流式写出速率折损（约为静态文件的 1/3）在开 gzip 后
不再重要——传输基数小了 7 倍。

## 实施记录（2026-09-06 已完成）

云上改动：`/etc/nginx/nginx.conf`（http 块启用全局 gzip_types，原行是注释、
即此前只有默认 text/html 压缩）+ `/etc/nginx/sites-available/trend-quant`
（新增独立 `location /mcp`，复制代理参数并补 `proxy_buffering off`——
原配置 /mcp 走 `location /` 且全配置无此指令）。备份在
`/root/nginx.conf.bak.20260906221001`、`/root/trend-quant.bak.20260906221001`。

实测结果（客户端为 daily-trade-report 技能的 McpClient，requests 透明解压，
客户端零改动）：

| 响应 | 改前 | 改后实测 |
|---|---|---|
| SSE 200 响应头 | 无 Content-Encoding | `Content-Encoding: gzip` ✓ |
| dashboard full（8.84MB 解压后） | 52s | 9.6s |
| dashboard lite（1.41MB 解压后） | 6.6s | 1.5s |
| dashboard 宽基+lite | 0.31s | 0.16s |

遗留观察项：`location /mcp` 沿用 `proxy_read_timeout 60s`——SSE 长连接
依赖服务端心跳（约 15s 一次）保活，若发现 SSE 空闲 60s 被断开，把该
location 的 read timeout 单独调大。
