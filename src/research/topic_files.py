"""课题文件夹制（决策 17，详设 §6.4.1）。

每个课题一个文件夹：`research/topics/<topic_id>_<slug>/` 承载
TOPIC.md（问题/结论/实验清单）、experiments/<exp_id>/REPORT.md +
report.json（完整报告，§6.5.0）、manifest.json（复现清单：spec 全文、
模块版本、data_version、engine/git 版本、窗口）。

**看板（DB）是查询面，文件夹是阅读/审计/复现面**——两者由平台从同一份
记录生成，永不手工维护、可再生成。manifest 里的 data_version 同时承担
诚实义务：重跑时数据已重述（qfq 改写）会被显式标出。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from research import lifecycle
from research import runs as runs_mod
from research import topics as topics_mod
from research import verdict as verdict_mod


def _slug(text: str, max_len: int = 40) -> str:
    text = re.sub(r"\s+", "-", str(text).strip())
    text = re.sub(r"[\\/:*?\"<>|]", "", text)
    return text[:max_len].strip("-") or "topic"


def materialize_topic(db, topic_id: str, *, root: str | Path) -> Path:
    """从 DB 记录物化课题文件夹（幂等覆盖写——同源生成，随时可重建）。"""
    root = Path(root)
    topic = topics_mod.get_topic(db, topic_id)
    if topic is None:
        raise ValueError(f"topic not found: {topic_id}")
    topic_dir = root / f"{topic_id}_{_slug(topic['title'])}"
    exp_dir_root = topic_dir / "experiments"
    exp_dir_root.mkdir(parents=True, exist_ok=True)

    experiments = lifecycle.list_experiments(db, topic_id=topic_id, include_archived=True)
    lines = [
        f"# {topic['id']} {topic['title']}",
        "",
        f"- 状态：{topic['status']}",
        f"- 问题：{topic['question']}",
        f"- 创建：{topic['created_at']}（{topic['owner_session']}）",
    ]
    if topic["status"] == "concluded":
        lines += [
            f"- 结论分级：{topic.get('conclusion_grade')}",
            f"- 结论：{topic.get('conclusion')}",
        ]
        summary = topics_mod.loads(topic.get("conclusion_summary_json"), None)
        if summary:
            lines += ["", "## 平台量化摘要（决策 20）", "",
                      "```json", json.dumps(summary, ensure_ascii=False, indent=2), "```"]
    lines += ["", "## 实验清单", "",
              "| 实验 | 评估模块 | 状态 | verdict | 标题 |",
              "|---|---|---|---|---|"]

    for exp in experiments:
        # 物化信封必须带上**解码后的 spec**——原始实验行只有
        # `spec_json`，直接塞进信封会让 `spec` 变成 null，与 HTTP 下载不一致。
        if isinstance(exp.get("spec_json"), str) and "spec" not in exp:
            try:
                exp = {**exp, "spec": json.loads(exp["spec_json"])}
            except (TypeError, ValueError):
                exp = {**exp, "spec": None}
        verdicts = verdict_mod.list_verdicts(db, exp["id"])
        # 定论优先（GLM53F-P1-4）：复核稿不劫持展示/物化位
        latest = verdict_mod.canonical_verdict(verdicts)
        final = (latest or {}).get("final_verdict") or ""
        lines.append(f"| {exp['id']} | {exp['evaluation_module']} | {exp['status']} | {final} | {exp['title']} |")

        # 每个实验一个子文件夹：REPORT.md + report.json + manifest.json
        exp_dir = exp_dir_root / exp["id"]
        exp_dir.mkdir(parents=True, exist_ok=True)
        if latest:
            # 物化产物必须是**完整报告信封**（与 HTTP 下载端点同构）
            (exp_dir / "report.json").write_text(
                json.dumps(
                    verdict_mod.verdict_envelope(exp, latest),
                    ensure_ascii=False, indent=2, sort_keys=True,
                ),
                encoding="utf-8",
            )
            (exp_dir / "REPORT.md").write_text(
                _render_report_md(exp, latest), encoding="utf-8"
            )
            manifest = _build_manifest(db, exp, latest)
            (exp_dir / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    (topic_dir / "TOPIC.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return topic_dir


def _render_report_md(exp: dict, verdict_row: dict) -> str:
    evidence = verdict_row.get("evidence", {})
    evidence_json = json.dumps(evidence, ensure_ascii=False, indent=2)
    # DS-复审-R2 §4-7：截断必须带标记（§6.5.0"一个都不许摘要化"的人读面
    # 完整性——静默截断会让读者误以为看全了）
    if len(evidence_json) > 6000:
        evidence_json = evidence_json[:6000] + "\n…（截断，完整证据见 report.json）"
    return "\n".join([
        f"# {exp['id']} {exp['title']}",
        "",
        f"- 假设：{exp['hypothesis']}",
        f"- 评估模块：{exp['evaluation_module']}（attempt #{exp['attempt_index']}）",
        f"- 平台建议：{verdict_row.get('suggested_verdict')}；"
        f"最终判定：{verdict_row.get('final_verdict')}",
        f"- 理由：{verdict_row.get('reasoning') or ''}",
        f"- 警告：{', '.join(verdict_row.get('warnings') or [])}",
        "",
        "## 证据（完整明细见 report.json）",
        "",
        "```json",
        evidence_json,
        "```",
    ])


def _build_manifest(db, exp: dict, verdict_row: dict) -> dict:
    spec = json.loads(exp["spec_json"])
    run_rows = runs_mod.list_runs(db, exp["id"])
    engine_runs = []
    data_version = None
    engine_version = None
    git_hash = None
    if not any(r.get("engine_run_id") for r in run_rows):
        # 非引擎类实验（event/bucket/distribution 的 runs 行 engine_run_id=None）：
        # 取数留痕在 gateway_audit，data_version 从最新一条审计行补齐
        # （评审 DS-P2-2 + 验证复审：原 `if not run_rows` 永不触发，已修）
        with db.connect() as conn:
            audit = conn.execute(
                """SELECT data_version FROM gateway_audit
                   WHERE run_id = ? AND data_version > 0
                   ORDER BY id DESC LIMIT 1""",
                (exp["id"],),
            ).fetchone()
        if audit:
            data_version = int(audit["data_version"])
    if run_rows:
        from portfolio import service as portfolio_service  # 分层铁律：L4→L3→L2

        for r in run_rows:
            if r.get("engine_run_id"):
                run = portfolio_service.get_engine_run(db, r["engine_run_id"])
                if run:
                    engine_runs.append(run["run_id"])
                    data_version = run["data_version"]
                    engine_version = run["engine_version"]
                    git_hash = run["git_hash"]
    return {
        "experiment_id": exp["id"],
        "spec": spec,
        "evaluation_module": exp["evaluation_module"],
        "engine_runs": engine_runs,
        "data_version": data_version,
        "engine_version": engine_version,
        "git_hash": git_hash,
        "runs": [
            {"window_start": r["window_start"], "window_end": r["window_end"],
             "window_kind": r["window_kind"], "holdout_touched": bool(r["holdout_touched"])}
            for r in run_rows
        ],
        "generated_at": verdict_row.get("generated_at"),
        "note": "复现 = spec 本身：research rerun <experiment_id>（声明式实验）；"
                "data_version 变化 = 数据已重述，差异显式标出",
    }
