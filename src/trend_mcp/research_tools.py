"""投研基建 MCP 工具（AI 通道；薄通道厚服务——业务全在 research.api /
pipeline / evaluations，此处只做参数翻译）。

设计约束（详设 §6.7 AI 沙箱边界）：AI 可组合已审核模块提实验、读台账、
保存模块草稿；**不能**让未过测试门的代码进运行路径（propose_module 自动
测试门）、不能改判定规则、不能碰 holdout（grant_holdout 不在此暴露）、
不能删改记录（append-only 由库层触发器兜底）。

有 worker（app 进程）时实验入队异步执行；无 worker（裸 MCP 上下文）时
同步跑（注意大窗口回测会阻塞较久）。
"""

from __future__ import annotations

from audit.app_logger import get_logger

logger = get_logger(__name__)

# Context 注解必须用 mcp 的真实类——FastMCP 的 Context 注入只认 Context
# 子类注解（R2VB 验收 B-1 实证：`ctx: Context = None` 是死代码且污染
# inputSchema）。本模块仅由 trend_mcp/server.py（mcp 已可导入时）加载；
# 守卫导入只为无 mcp 的测试环境直接 import 本模块不炸。
try:
    from mcp.server.fastmcp import Context
except ImportError:  # pragma: no cover - 测试环境无 mcp 包
    Context = None

# 业务错误类（可把 str(exc) 透给 AI 客户端——错误文案面向使用者写成）；
# 其余异常属内部错误，只记日志、回笼统文案（不把 sqlite/路径等
# 内部细节泄给客户端，也不把"沙箱违规"与"服务器 bug"混为一谈）。
from research.errors import IntakeRejected, ResearchError


def _error_payload(exc: Exception) -> dict:
    if isinstance(exc, IntakeRejected):
        return {"ok": False, "error": str(exc), "reasons": list(getattr(exc, "reasons", []) or []),
                "experiment_id": getattr(exc, "experiment_id", None)}
    if isinstance(exc, ResearchError):
        return {"ok": False, "error": str(exc)}
    # 库/服务层的业务异常（LibraryError / ServiceError 等
    # ValueError 家族）此前落进"internal error"分支——业务原因被吞，模型看不
    # 到可读解释。这里把它们与 ResearchError 同口径暴露。
    try:
        from portfolio.library import LibraryError
        from portfolio.registry import ModuleRegistrationError
        from portfolio.service import ServiceError
        from portfolio.strategy import StrategyConfigError
    except Exception:  # pragma: no cover - 导入失败时不改变兜底行为
        LibraryError = ServiceError = ()  # type: ignore[assignment]
        StrategyConfigError = ModuleRegistrationError = ()  # type: ignore[assignment]
    # R21B-P2-2：晋升/载入路径抛的是 StrategyConfigError / ModuleRegistrationError
    # （都是 ValueError 家族的业务错误）——此前不在白名单里 → MCP 返回
    # "internal error (see server logs)"，而 CLI 同调用给真实原因（如
    # "position_risk: param atr_mul: -1.0 < min 0.1"）→ 模型会误判成平台故障并盲目重试。
    if isinstance(exc, (LibraryError, ServiceError, StrategyConfigError,
                        ModuleRegistrationError)):
        return {"ok": False, "error": str(exc)}
    logger.exception("research tool internal error")
    return {"ok": False, "error": "internal error (see server logs)"}


def _service():
    """优先复用 app 挂载的服务面（带 worker）；否则直连默认库（同步模式）。

    意外异常转 ResearchError（R2VB B-7）：工具的 except 全靠 _error_payload
    分类——服务面装配失败属"服务器不可用"业务语义，不该以原始 traceback
    透给 AI 客户端。"""
    try:
        from app.main import app

        service = getattr(app.state, "research_service", None)
        if service is not None:
            return service
    except Exception:
        pass
    try:
        from data.storage import db as db_module
        from portfolio.slots import REGISTRY, ensure_builtins
        from research.api import ResearchService

        ensure_builtins()
        return ResearchService(db_module.get_db(), registry=REGISTRY)
    except Exception:
        logger.exception("research service assembly failed")
        raise ResearchError("research service unavailable (see server logs)")


def _ai_session(db, ctx=None):
    """AI 会话归属：多 token（TREND_MCP_TOKENS 的 tokenA=用户A）
    部署下按 mcp_user 派生独立会话（ai-mcp-<user>），台账可区分是哪个
    token 用户的研究操作；单 token / 无请求上下文时回退共享默认会话。"""
    from research.sessions import ensure_channel_session, get_or_create_ai_session

    username = None
    if ctx is not None:
        try:
            request = ctx.request_context.request
            state = getattr(request, "scope", {}).get("state") or {}
            username = state.get("mcp_user")
        except (ValueError, AttributeError, TypeError):
            username = None
    if not username:
        return get_or_create_ai_session(db, channel="mcp")
    # 通道不写库——会话命名/归属策略在服务面（"薄通道厚服务"）
    return ensure_channel_session(
        db,
        session_id=f"ai-mcp-{str(username)}",
        label=f"AI（MCP·{username}）",
        channel="mcp",
    )


def _run_or_queue(service, experiment_id: str) -> dict:
    """有 worker 入队；无 worker 同步执行到 evaluating。

    R22B-F2 + R23B-F2：两条分支都要给**统一运行信封**
    （`run_status`/`verdict`/`error`/`experiment_status`）——此前同步分支只取
    `result.get("status")`（成功返回的是 verdict 行、无 status 键 → 恒 null），
    worker 分支干脆只有 `{"mode": "queued"}`（而 app 生产路径恒有 worker），
    模型会把"run 失败/还没跑"读成"已受理成功"。queued 分支显式标
    `run_status="queued"` 并给出轮询入口，不再让调用方猜。

    R23B-F1（安全）：**不接受任何 caller 传入的 token**。样本外放行是治理动作，
    只能由"人把 token 绑定到具体实验"产生——绑定关系在库里，自动带出即可
    （`_pick_unconsumed_token` 沿复现链回溯）。此前 R22 把 `holdout_token` 加进
    工具签名，等于把 4 位顺序号变成 AI 可猜的通行证（实测 AI 传全局 token 即跑到
    样本外窗口并消耗了人发的凭证）。
    """
    if service.worker is not None:
        queued = service.worker.submit(experiment_id)
        return {
            "mode": "queued",
            "run_status": "queued",
            "queued": queued,
            "verdict": None,
            "error": None,
            "experiment_status": _fresh_status(service, experiment_id, "queued"),
            "poll": "research_run_status",
        }
    from core import run_freeze
    from research.pipeline import run_experiment, run_result_envelope

    # 冻结写包裹（GLM53F-P2-11）：同步通道与 worker 同口径（决策 A3）
    with run_freeze.frozen_writes():
        result = run_experiment(service.db, experiment_id, registry=service.registry)
    return {"mode": "sync", **run_result_envelope(result)}


def _fresh_status(service, experiment_id: str, fallback: str = "") -> str:
    """跑完之后读**库内真实状态**（R22B-F2）：顶层 status 此前是运行前的快照
    （恒 `queued`），与实验行里真实的 `evaluating`/`failed` 矛盾。"""
    exp = service.get_experiment(experiment_id)
    if isinstance(exp, dict) and exp.get("status"):
        return str(exp["status"])
    return fallback


def register_research_tools(mcp) -> None:
    """把研究工具注册到 FastMCP 实例（server.py 末尾调用）。"""

    @mcp.tool()
    def research_register_module_catalog() -> dict:
        """列出已注册的插槽模块与评估模块（AI 提实验前先读这个）。"""
        service = _service()
        try:
            return {
                "ok": True,
                "modules": service.list_modules(),
                "evaluations": service.list_evaluations(),
            }
        except Exception as exc:
            return _error_payload(exc)

    @mcp.tool()
    def research_propose_topic(title: str, question: str, ctx: Context = None) -> dict:
        """提研究课题（AI 工作流固定：先提课题、再在课题下设计实验）。"""
        service = _service()
        session = _ai_session(service.db, ctx)
        try:
            topic = service.propose_topic(
                session_id=session["session_id"], title=title, question=question
            )
            return {"ok": True, "topic": topic}
        except Exception as exc:
            return _error_payload(exc)

    @mcp.tool()
    def research_propose_experiment(
        topic_id: str,
        evaluation_module: str,
        spec: dict,
        hypothesis: str,
        title: str = "",
        allow_duplicate: bool = False,
        run: bool = True,
        ctx: Context = None,
    ) -> dict:
        """提实验（骨架校验不过即 rejected_intake 留痕并返回原因）。

        spec 形态见各评估模块说明；backtest 改进型 diff 恰一槽。
        `run=False` 时只登记（不入队）；入队后再跑请用 `research_run_experiment`
        （此前文档写"攒一批再跑"但没有任何派发入口，实验会停到进程重启）。
        样本外（holdout）放行**不经本工具**：窗口触碰样本外时，须先由人把 token
        绑定到本实验（Web 发放时指定实验），运行时平台自动带出；未绑定则 fail-closed。
        返回 `dispatch.run_status` 才是**这次运行**的结果（`ok=true` 只表示实验对象已创建）。
        """
        service = _service()
        session = _ai_session(service.db, ctx)
        try:
            exp = service.propose_experiment(
                session_id=session["session_id"], title=title or f"{evaluation_module} 实验",
                topic_id=topic_id, evaluation_module=evaluation_module, spec=spec,
                hypothesis=hypothesis, allow_duplicate=allow_duplicate,
                auto_queue=False,
            )
        except Exception as exc:
            return _error_payload(exc)
        dispatch = _run_or_queue(service, exp["id"]) if run else {"mode": "parked"}
        return {"ok": True, "experiment_id": exp["id"],
                "status": _fresh_status(service, exp["id"], exp["status"]),
                "attempt_index": exp["attempt_index"], "dispatch": dispatch}

    @mcp.tool()
    def research_get_experiment(experiment_id: str) -> dict:
        """读实验详情（spec + verdicts + 证据）。"""
        service = _service()
        try:
            detail = service.get_experiment(experiment_id)
        except Exception as exc:
            return _error_payload(exc)
        if detail is None:
            # 与同族工具统一错误口径（此前缺 error 字段，客户端/
            # 模型无法区分"不存在"与内部错误）
            return {"ok": False, "error": f"experiment not found: {experiment_id}",
                    "experiment": None}
        return {"ok": True, "experiment": detail}

    @mcp.tool()
    def research_run_status(experiment_id: str) -> dict:
        try:
            service = _service()
            return {"ok": True, **service.run_status(experiment_id)}
        except Exception as exc:
            return _error_payload(exc)

    @mcp.tool()
    def research_search_ledger(
        subject_key: str = "", topic_id: str = "", final_verdict: str = "",
    ) -> dict:
        """台账检索（全员只读：含他人失败实验——先读台账再提假设）。"""
        service = _service()
        try:
            results = service.search_ledger(
                subject_key=subject_key or None, topic_id=topic_id or None,
                final_verdict=final_verdict or None,
            )
        except Exception as exc:
            return _error_payload(exc)
        return {"ok": True, "results": results}

    @mcp.tool()
    def research_list_topics(status: str = "") -> dict:
        try:
            service = _service()
            topics = service.list_topics(status=status or None)
        except Exception as exc:
            return _error_payload(exc)
        return {"ok": True, "topics": topics}

    @mcp.tool()
    def research_confirm_verdict(
        experiment_id: str, final_verdict: str, reasoning: str,
        ctx: Context = None,
    ) -> dict:
        """确认 verdict（final 可降不可升平台建议；reasoning 必填）。"""
        service = _service()
        session = _ai_session(service.db, ctx)
        try:
            result = service.confirm_verdict(
                experiment_id=experiment_id, final_verdict=final_verdict,
                reasoning=reasoning, session_id=session["session_id"],
            )
            return {"ok": True, "verdict": result["final_verdict"]}
        except Exception as exc:
            return _error_payload(exc)

    @mcp.tool()
    def research_rerun_experiment(experiment_id: str, run: bool = True, ctx: Context = None) -> dict:
        """复现一个已到终态的实验（同 spec 同 attempt_index、不计尝试计数）。

        样本外放行同 `research_propose_experiment`：token 由人绑定，复现链自动
        回溯父实验的未消费 token（无需也不能由调用方直传 token id）。
        """
        service = _service()
        session = _ai_session(service.db, ctx)
        try:
            exp = service.rerun_experiment(
                experiment_id=experiment_id, session_id=session["session_id"],
                auto_queue=False,
            )
        except Exception as exc:
            return _error_payload(exc)
        dispatch = _run_or_queue(service, exp["id"]) if run else {"mode": "parked"}
        return {"ok": True, "experiment_id": exp["id"],
                "status": _fresh_status(service, exp["id"], exp.get("status", "")),
                "attempt_index": exp["attempt_index"], "dispatch": dispatch}

    @mcp.tool()
    def research_run_experiment(experiment_id: str, ctx: Context = None) -> dict:
        """执行一个**已登记但未派发**的实验（`run=False` 攒下来的那批）。

        R22B-F5：文档一直承诺"攒一批再跑"，但此前除了 `run=True` 的同调用
        派发与"进程启动补投"之外没有任何入口——实验停在 queued 直到应用
        重启，而 `research_conclude_topic` 要求课题内无在途实验 → 一个 parked
        实验会把关题卡死。这里补上与 CLI `run` 对称的显式派发入口。
        """
        service = _service()
        try:
            exp = service.get_experiment(experiment_id)
            if exp is None:
                return {"ok": False, "error": f"experiment not found: {experiment_id}"}
            if exp.get("status") != "queued":
                return {"ok": False,
                        "error": f"experiment {experiment_id} not queued"
                                 f" (status={exp.get('status')})"}
            dispatch = _run_or_queue(service, experiment_id)
        except Exception as exc:
            return _error_payload(exc)
        return {"ok": True, "experiment_id": experiment_id,
                "status": _fresh_status(service, experiment_id, exp.get("status", "")),
                "dispatch": dispatch}

    @mcp.tool()
    def research_promote_to_library(
        experiment_id: str, strategy_id: str, name: str = "", ctx: Context = None,
    ) -> dict:
        """实验晋升入策略库（决策 8 唯一的门：须 verdicted + final=confirmed；
        2026-09-24 用户决策：AI 可全流程闭环自动晋升）。"""
        service = _service()
        session = _ai_session(service.db, ctx)
        try:
            version = service.promote_to_library(
                experiment_id=experiment_id, strategy_id=strategy_id,
                session_id=session["session_id"], name=name,
            )
            return {"ok": True, "version_id": version["id"]}
        except Exception as exc:
            return _error_payload(exc)

    @mcp.tool()
    def research_conclude_topic(
        topic_id: str, conclusion: str, grade: str = "",
        experiment_ids: list[str] | None = None, ctx: Context = None,
    ) -> dict:
        """关题（平台量化摘要先行；grade 可降不可升）。"""
        service = _service()
        session = _ai_session(service.db, ctx)
        try:
            topic = service.conclude_topic(
                topic_id=topic_id, conclusion=conclusion,
                session_id=session["session_id"],
                experiment_ids=experiment_ids, grade=grade or None,
            )
            return {"ok": True, "topic": topic}
        except Exception as exc:
            return _error_payload(exc)

    @mcp.tool()
    def research_propose_module(
        slot: str, name: str, version: int, kind: str, source: str,
        params_schema: dict | None = None, ctx: Context = None,
    ) -> dict:
        """提新模块（自动测试门：契约/确定性/前缀稳定性；DSL 免测）。

        kind: "dsl"（表达式，如 "cross_above(close, sma(close, 20))"）|
              "python"（定义 Module 类的源码）。
        """
        service = _service()
        session = _ai_session(service.db, ctx)
        try:
            draft = service.propose_module(
                session_id=session["session_id"], slot=slot, name=name,
                version=version, kind=kind, source=source,
                params_schema=params_schema,
            )
            return {"ok": draft["status"] != "rejected", "draft": draft}
        except Exception as exc:
            return _error_payload(exc)

    logger.info("research MCP tools registered")
