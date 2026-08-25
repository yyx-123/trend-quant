# 评审意见 B：覆盖面查漏与建议质量评估

- 评审日期：2026-08-24
- 评审对象：`docs/code-review-2026-08-24/review-report.md`（终版）
- 评审方式：通读终版报告后，独立浏览 `web/templates`、`scripts/`、`tests/`、`pyproject.toml`、`config/app.yaml`、`scripts/deploy.sh`、`Makefile`、`.gitignore`、`src/app/main.py`、`src/data/storage/db.py`、`src/services/auth.py`、`src/trend_mcp/server.py` 等报告自承覆盖薄弱的区域，并与本目录已有的 `review-1-factcheck.md`（事实核查）和 `review-2-arch.md`（架构评审）交叉比对——重点确认「前两轮已亲核属实的发现」在终版中是否被保留。
- 约束：只读；未修改除本文件外的任何项目文件。

---

## 一、遗漏/不足清单

### A. 安全维度

**A-1 【高】`deploy.sh` 整块未审：默认 HTTP 暴露 → 登录密码明文过公网**
`scripts/deploy.sh:150-178` 生成的 nginx 站点仅 `listen 80`，HTTPS 只在结尾提示「可运行 certbot」。该脚本是仓库内唯一的部署自动化，照它部署的实例中，登录接口（`/api/auth/login`）的密码与后续 session cookie **全程明文过公网**。报告 S-1 只讨论了 cookie `Secure` 属性（那是 HTTPS 之后的加固），漏了更根本的「传输层默认明文」。且 `main.py:381-384` 注释自述生产走 frp 直连、无 nginx——deploy.sh 与真实部署形态矛盾，说明脚本已腐烂，照跑即踩坑。

**A-2 【高】`deploy.sh` 三个危险点：root 运行、裸 `rm -rf`、更新无数据保护**
- `scripts/deploy.sh:104`：systemd 单元 `User=root`——一个公网可达的 FastAPI 应用以 root 常驻，任何 RCE 即整机沦陷；
- `scripts/deploy.sh:73`：克隆分支里 `rm -rf "$INSTALL_DIR"`——`/opt/trend-quant` 同时是代码目录和数据目录（SQLite 库、logs、backups 都在其下），一旦 `.git` 目录损坏/被删，重跑脚本会**静默删掉生产数据库**；
- `git pull`（70 行）后不跑迁移、不备份数据就直接 `systemctl restart`（123-127 行），schema 演进靠应用启动时 `_migrate_schema` 裸奔。
review-2 曾指出 v1 的「P1-6 deploy.sh 路径 + 裸 rm -rf」，终版整条消失。

**A-3 【中】session 有效期设计：30 天滑动续期、无绝对上限**
`src/services/auth.py:27` `SESSION_TTL = timedelta(days=30)`，剩余不足一半即顺延——活跃用户实际永不过期。叠加 P0-5（token 明文落库），一旦库文件泄露，历史所有活跃会话在无限长窗口内可用。建议给滑动续期加绝对上限（如 90 天强制重登），与 P0-5 的哈希存储联动修复。

**A-4 【中】登录失败零审计 + 计时侧信道**
`src/services/trade_records.py:58-66` `authenticate` 失败直接抛异常，**不写任何日志**——当前连「有人在爆破」都无从察觉，P0-4 说的「至少在日志里对连续失败告警」其实没有地基（无记录何来告警），报告未点破这一现状。同处 `user is None or not verify_password(...)` 短路：用户不存在时毫秒返回、存在时付 20 万次 PBKDF2，构成用户枚举计时侧信道（单人系统危害极低，一句话记录即可）。

**A-5 【低】无任何安全响应头**
全站无 `X-Content-Type-Options` / `X-Frame-Options` / `Referrer-Policy`。报告 XSS 专项只扫了转义与 innerHTML，未提响应头基线。CSP 与内联 JS 冲突可不做，但前三个是零成本一行中间件。

**A-6 【低】仓库卫生：zip 快照被 git 跟踪**
`trend-quant.zip`（455KB，根目录）与 `.agents/skills/trend-score-calculator.zip` 均在 `git ls-files` 中（已亲核）；旧代码快照入库，既有敏感信息残留面也让仓库体积膨胀。`.gitignore` 排了 `*.bundle` 和 `data/backups/` 却没排 zip。

### B. 运维/部署维度

**B-1 【高】无定时备份机制——2.8GB 不可再生单库的最大风险被降级成「keep=3 修剪」**
`db.py:78-97` `backup_to` 全项目仅两个触发点（启动重建、手工脚本），均为偶发事件。终版 §7 缺口 5 只讨论了 keep=3 挤掉好备份，把「备份线整体事实失效」的判断弄丢了（review-2 曾明确要求将此条升 P1：代码与数据两条备份线同时失效）。对一个单机单用户项目，**丢数据的期望损失远高于报告 P0 列表里的大多数条目**。

**B-2 【高】迁移/回填脚本用 `shutil.copy2` 备份 WAL 活库**
`scripts/migrate_category_simplify.py:128`、`scripts/backfill_batch_excess_metrics.py:179`：直接文件复制主库文件——WAL 模式下未 checkpoint 的最近数据在 `-wal` 文件里，**复制出来的「备份」可能缺最近写入甚至不一致**；且服务运行中复制无一致性保证。正确实现 `Database.backup_to()`（VACUUM INTO）就在同项目里没被复用。此条经 review-1 逐字亲核属实（其 P0-4），review-2 建议降 P2 并「统一改 backup_to()」——终版连 P2 都没留，属无声丢失。

**B-3 【中】chinese_calendar 年度数据边界无运维着落**
`src/core/calendar.py:60-85`：超出库数据的年份退化为 weekday-only 判断。2027-01-01 起法定假日会被当成交易日；更糟的是 `_daily_update_catchup`（`main.py:182-222`）的 expected 计算走同一日历，会把假日判为「应更未更」，**每次重启触发一次无效 force 补跑**直到人工升级库。「每年 12 月 pip install --upgrade chinese_calendar」这条 ritual 只写在 calendar.py docstring，README/部署文档均无；叠加无告警通道（报告 §7-3 已提），warning 无人能看见。

**B-4 【中】带外写库脚本与运行中服务无跨进程互斥**
`scripts/rerun_daily_update.py`（全文无锁）在服务运行时执行，会与 16:30 任务/盘中快照并发写同一 WAL 库——进程内的 `_update_job_lock`（`main.py:108`）管不到它。终版 §7-6 只提了 `_market_symbols_cache` 跨进程陈旧，漏了更直接的并发写冲突（与 P0-3 联动：busy_timeout 修复前必撞锁）。同样适用于 `backfill_batch_excess_metrics.py`（docstring 只说「先确认没有批次在跑」，无强制）。

**B-5 【中】无 CI：70 个测试文件无人自动执行**
无 `.github/`、无任何 CI 配置（已亲核）。Makefile 有完整 test 目标但纯靠人跑；`make test-deps` 安装未钉版的最新 pytest 全家桶，同样不可复现。review-2 的 CI 建议（单矩阵 + 前置 dev 依赖）在终版消失。

**B-6 【中】pyproject 无 dev 依赖声明——S-7 只抓到症状**
`pyproject.toml` 只有运行时 dependencies；`pytest`/`pytest-asyncio`/`pytest-cov`/`ruff` 全部未声明（无 dev optional-dependencies，无 requirements-dev.txt）。`asyncio_mode = "auto"`（46 行）在无 pytest-asyncio 的环境下是未知配置项。新机器 `pip install -e .` 后**测试根本跑不起来**，也是 CI 落地的第一个绊脚石。终版 S-7 只提了锁定文件，漏了这个根因。

### C. 测试维度

**C-1 【高】两个 MCP 测试文件收集即失败——当前环境下整套测试跑不起来**
`tests/unit/test_mcp_symbol_detail.py:24`、`tests/unit/test_mcp_stop_mode.py:5` 顶层 `from trend_mcp import server`，两文件均无 `importorskip`（已亲核）；项目 `.venv` 实际未安装 `mcp` 包（`import mcp` 报 ModuleNotFoundError，已亲核）。后果：在项目自己的虚拟环境里 `pytest` **收集阶段直接 2 errors 中断**（review-1 实测 702 collected + 2 errors）。终版 §8 列了 8 条缺口却漏了这条「地基级」问题——§8 里所有补测建议、P0 项的回归测试，都建立在一个当前无法运行的测试套件之上。修复约 10 分钟（importorskip 或 dev 依赖补装），应入 P0。

**C-2 【低】备份/删除类数据安全路径缺直接测试**
`backup_to` 的 keep 修剪、`delete_batch_run` 的级联删除（`db.py:2037-2053`，含 BEGIN IMMEDIATE 与 running 拒绝）均无直接单测（前者仅 integration 中间接触及）。属于「出错即数据事故」的高性价比补测点，终版 §8-8 只覆盖了迁移脚本。

### D. 数据一致性边界

**D-1 【中】`PRAGMA foreign_keys` 从未开启，REFERENCES 全部不生效**
`db.py:67-76` `_connect()` 只设了 `journal_mode=WAL`；而 `sessions.user_id`（`db.py:357`）、`manual_trades.user_id`（`db.py:341`）均声明 `REFERENCES users(id)`。SQLite 默认不强制外键——删用户会留下孤儿 session 与孤儿交易行，级联语义名存实亡。终版 P0-3 抓了 busy_timeout，漏了同函数内的 foreign_keys（review-2 曾指出这是 P2-16 里最重的一项，应单列）。注意：直接全局开启需先确认存量数据无孤儿行，属「开启 + 一次性校验」两步。

**D-2 【低】目录级死代码 ruff 扫不到：5 个空包 + 构建残留**
`src/backtest`、`src/engine`、`src/notify`、`src/portfolio`、`src/strategy` 五个目录只剩 `__pycache__`、无任何 `.py`（骨架时代残留，未被 git 跟踪但污染工作区与打包发现）；`src/trend_etf_system.egg-info/` 构建残留混在 src 下。终版 §5 的 14 条死代码全是文件内条目，目录级残留漏网。

### E. 前端 / UX 维度

**E-1 【中】静态资源版本串三处口径不一，且 §4.3 的 common.js 建议未与此联动**
`main.py:277` `asset_version` 只跟踪 `style.css` 的 mtime；`base.html:7` 又在版本串后硬编码了 `-20260824-auth` 人工后缀（手动 cache-bust 残留）。终版 Q-8 只提了每请求 `stat()` 的开销，没提版本口径问题——而它恰是 §4.3「抽 `web/static/common.js`」的**前置**：新 JS 文件若无版本跟踪，浏览器旧缓存会让「消除 esc 漂移」变成「引入第三种漂移」。此联动 review-2 已明确指出，终版丢失。

**E-2 【低】全站轮询无退避/无页面可见性感知**
`base.html:150` 每个页面 30 秒轮询日更状态直至永远；`subject_market.html` 盘中 2 秒轮询（review-2 曾列为 P1-16）。笔记本合盖/后台标签页照跑。单用户系统代价小，但补一个 `visibilitychange` 挂起是十行内的改善。

**E-3 【低】块级渲染漂移未列证据：止损卡两处副本已不一致**
§4.3 只列了函数级拷贝（esc/fmt/_category_path），没列更危险的块级漂移：`manual_trade.html:304-369` 的止损卡渲染含「硬止损附带当日最低/收盘」，`subject_market.html:1128-1159` 的同族渲染没有——两副本**已经漂移**。抽 common.js 前必须先做产品裁决「以哪份为准」，这不是机械合并，报告把抽取写成了纯技术动作。

---

## 二、建议质量意见（对终版路线图的逐条调整）

1. **编号与路线图自相矛盾，必须先修**：§1 六条「严重问题」编号 P0-1…P0-6，§11 的 P0 却只收 P0-1/3/4/6，P0-2（MCP 零鉴权）与 P0-5（token 明文）被推进 P1。要么改编号要么改路线图。实质判断上：**P0-2 应留在 P0**——它是唯一公网（frp）可达的未鉴权面，且 `intraday_dashboard` 每次调用燃烧付费报价配额，属于「可被陌生人直接造成金钱损失」的洞；共享密钥方案半天可落地，紧迫性不低于 P0-4。P0-5 降 P1 可以接受（依赖库文件泄露这一前提），但请在路线图注明降级理由。
2. **路线图 P0 漏了两条最便宜的**：MCP 测试收集失败（本清单 C-1，10 分钟）与 busy_timeout 同为「让后续一切工作有地基」的条目；建议 P0 增加「importorskip/补装 mcp + dev 依赖声明」。
3. **P0-3 建议本身正确，补两点**：① 修复验证时应覆盖带外脚本并发写场景（B-4），否则 busy_timeout 只在应用内生效；② 5000ms 对日更大事务（rematerialize_qfq 全量重写持写锁）可能不够，建议上线后观察日更窗口的等待时长再校准，别写死成定论。
4. **Q-1 建议欠规格**：「按标的缓存 resolver/指标序列」必须按 `(symbol, 数据版本, 指标参数)` 做键——不同策略的 `indicator_config` 不同，只按标的缓存会让参数不同的策略互相污染序列。报告当前表述照做会引入正确性 bug。
5. **§4.1 公式/聚合合并是高危重构，终版只给目标没给路径**：review-2 的两阶段方案（阶段一只抽 5 个逐字相同的纯函数，零风险半天；阶段二动聚合骨架，以 `test_intraday_trend_consistency` 等一致性测试为守门员，允许评估后停在阶段一）被丢失。当前写法下这条 P1 的执行风险被低估，建议恢复两阶段表述。
6. **P2「db.py 拆 repository」与项目现状不匹配，建议改写**：单人项目不存在协作冲突面，2119 行的真实痛点是导航与 350 行 DDL。全拆 105 个方法是 2-3 天纯风险工时，收益主要是审美。建议降级为「`_init_tables` DDL 按表分段 + users/sessions 域随 P0-5 的哈希改造顺手迁出」，其余冻结。
7. **§7-5 备份建议没算磁盘账**：主库 2.8GB，`VACUUM INTO` 是全量拷贝，「每日一份保留 N 天」= N × 2.8GB 同盘 IO 与空间。建议改为「keep=3 日备（约 8.4GB）+ 每周一份异机/云盘」，并把「每日更新成功后自动触发备份」显式写进路线图（当前 P1/P2 均无备份条目，见 B-1）。
8. **P0-4 路线图条目应显式包含 MCP 两个逐次鉴权工具**：正文提了 `add_trade`/`open_positions` 同理，路线图只写「登录限流」。执行者照路线图打勾时会漏掉 MCP 侧。
9. **§8-7 前端 JS 测试依赖 common.js 落地（P2）**，排位合理；但全部测试建议的共同前置是 C-1（套件当前跑不起来），应在 §8 开头点明。
10. **S-2（CSRF 自定义头）边际收益低**，SameSite=lax 已挡常规跨站表单，维持「低」可以，建议标注「可选」避免占用 P 级注意力。其余 S 系列、C 系列建议与单机单用户 SQLite 现状匹配，无不匹配项。
11. **无「性价比低/不可行」的恶性建议**：终版整体克制（没有让上 Postgres、Redis、Docker 之类与单机定位冲突的药方），这一点值得肯定。

---

## 三、结构与表达意见

1. **最大结构问题：终版相对 v1 的修订无任何追溯**。本目录 README 承诺的 `blind-audit-comparison.md` 与 `CHANGELOG.md` 均不存在（已亲核目录只有 6 个文件）。后果是：review-1 逐字亲核属实、review-2 明确要求「降级保留」的发现（copy2 备份、MCP 测试收集失败、dev 依赖、deploy.sh、定时备份、common.js 的 asset_version 前置）在终版**无声消失**——读者无法区分「已裁决不采纳」与「丢失」。本评审第一节中带【高】的条目过半属于此类。建议补一份修订记录，哪怕只是「v1 条目 → 终版去向」的对照表。
2. **编号体系三层脱钩**：§1 的 P0-x 编号、各专项节的 S/Q/C 编号、§11 路线图的 P0/P1/P2 分档，三者映射关系不一致（见建议质量 1、8）。建议路线图表格加「来源编号」列，做到每个编号条目在路线图中必有去向（哪怕是「不采纳+理由」）。
3. **重要发现被埋没**：① C-2 时区问题的最重证据（`core/jobs.py:63` 日更主任务的交易日门控用宿主机本地日期）埋在 §6 表格一行，比 §10 UX 的多数条目重得多；② Q-1（最重任务的 N 倍重复计算）在 §3 表格中只占一行，未进 §0 总体评价的四大问题清单；③ §7-6 的跨进程缓存陈旧与 B-4 的并发写应合并叙述。建议 §0 按「不修会怎样」重排点名。
4. **§5 死代码 14 条混排**：从 `build_sw_tree` 整块死循环体（有真实行为影响）到「连续 6 个空行」（纯洁癖）同列，建议分「删除有行为收益 / 纯卫生」两组，避免最扎眼的一条被稀释。
5. **P1/P2 条目缺验收标准**：P0 项都有测试配套（好榜样），但 P1/P2 多数没有完成定义——「时区统一」怎么算完成（grep `date.today()` 为零？）、「公式抽共享核心」怎么算完成（一致性测试通过且旧实现删除？）。建议每条补一行 Done 定义。
6. **正面记录不平衡**：§7 开头有一段运维正面清单（好），但并发设计（`RevisionCache` 双检锁单飞、`_update_job_lock` 非阻塞防重入、per-symbol 锁、每调用新建连接、`delete_batch_run` 的 BEGIN IMMEDIATE）全文无正面记录。缺了「已检查、结论良好」的显式结论，读者会高估并发与事务风险——这本身就是覆盖面信息。

---

## 四、总体结论

**采纳本评审意见后可以交付。**

终版报告在事实准确性上经得住抽查（本评审亲核了 P0-1、P0-2、P0-3、P0-4、P0-5、P0-6、Q-1、§4.2、§5 部分条目，行号与论断全部属实），建议与单机单用户 SQLite 定位总体匹配，无恶性过度设计。但存在两个必须修补的系统性问题：

1. **修订链路断裂导致「已确认属实」的发现无声丢失**（copy2 备份、MCP 测试收集失败、dev 依赖、deploy.sh、定时备份、asset_version 前置）——其中 B-1（无定时备份）、B-2（备份方式错误）、C-1（测试套件跑不起来）、A-1/A-2（deploy.sh）五条必须回炉，前三条进 P1 以上；
2. **编号与路线图自相矛盾**（P0-2/P0-5 的定位、Q-1 的规格、§4.1 的两阶段路径）需按第二节修正。

修补这两处 + 补一份 v1→终版的修订去向表后，本报告可以达到交付标准。
