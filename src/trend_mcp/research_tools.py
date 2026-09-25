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
# 其余异常属内部错误，只记日志、回笼统文案（R2-P3-3：不把 sqlite/路径等
# 内部细节泄给客户端，也不把"沙箱违规"与"服务器 bug"混为一谈）。
from research.errors import IntakeRejected, ResearchError


def _error_payload(exc: Exception) -> dict:
    if isinstance(exc, IntakeRejected):
        return {"ok": False, "error": str(exc), "reasons": list(getattr(exc, "reasons", []) or []),
                "experiment_id": getattr(exc, "experiment_id", None)}
    if isinstance(exc, ResearchError):
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
    """AI 会话归属（R2-P3-4）：多 token（TREND_MCP_TOKENS 的 tokenA=用户A）
    部署下按 mcp_user 派生独立会话（ai-mcp-<user>），台账可区分是哪个
    token 用户的研究操作；单 token / 无请求上下文时回退共享默认会话。"""
    from research.sessions import get_or_create_ai_session, get_session

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
    session_id = f"ai-mcp-{str(username)}"
    existing = get_session(db, session_id)
    if existing is not None:
        return existing
    with db.connect() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO research_sessions (session_id, kind, label, channel)
               VALUES (?, 'ai', ?, 'mcp')""",
            (session_id, f"AI（MCP·{username}）"),
        )
    return get_session(db, session_id)


def _run_or_queue(service, experiment_id: str) -> dict:
    """有 worker 入队；无 worker 同步执行到 evaluating。"""
    if service.worker is not None:
        queued = service.worker.submit(experiment_id)
        return {"mode": "queued", "queued": queued}
    from core import run_freeze
    from research.pipeline import run_experiment

    # 冻结写包裹（GLM53F-P2-11）：同步通道与 worker 同口径（决策 A3）
    with run_freeze.frozen_writes():
        result = run_experiment(service.db, experiment_id, registry=service.registry)
    return {"mode": "sync", "status": result.get("status")}


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
        run=False 时只登记不入队（攒一批再跑）。
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
        return {"ok": True, "experiment_id": exp["id"], "status": exp["status"],
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
            # R3C-P3-10：与同族工具统一错误口径（此前缺 error 字段，客户端/
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
        """复现一个已到终态的实验（同 spec 同 attempt_index、不计尝试计数）。"""
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
                "attempt_index": exp["attempt_index"], "dispatch": dispatch}

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
