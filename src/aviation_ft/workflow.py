"""Task progress and review triage shared by the UI and CLI."""
from __future__ import annotations

from collections import Counter

from aviation_ft import core


def task_progress(job_id: str) -> dict:
    meta = core.load_job(job_id)
    job_path = core.job_dir(job_id)
    evidence = core.evidence_for(job_id)
    candidates = core.candidates_for(job_id)
    report = core.read_json(job_path / "parsed/report.json")
    split_frozen = bool(core.read_json(job_path / "evidence/splits.json"))
    recipe_frozen = bool(core.read_json(job_path / "candidates/generation_recipe.json"))
    evidence_by_split = {split: sum(e["split"] == split for e in evidence) for split in sorted(core.SPLITS)}
    evidence_by_document = {doc["filename"]: sum(e["doc_id"] == doc["doc_id"] for e in evidence) for doc in meta["documents"]}
    candidates_by_split = {split: sum(r["split"] == split for r in candidates) for split in sorted(core.SPLITS)}
    review_counts = dict(Counter(r["review"]["status"] for r in candidates))
    approved = review_counts.get("approved", 0) + review_counts.get("approved_after_edit", 0)
    model_development = sum(r["origin"] == "model" and r["split"] in {"train", "validation"} for r in candidates)
    development_sample = min(20, max(1, evidence_by_split["train"] + evidence_by_split["validation"]))
    if not meta["documents"]:
        next_step = "逐步上传 PDF；上传完成后启动自动流程。"
    elif not report:
        next_step = "点击一键运行自动流程，模型会解析 PDF 并检查证据。"
    elif not split_frozen:
        next_step = "运行自动流程，让模型选择并核验已上传文档的证据。"
    elif any(count < 20 for count in evidence_by_document.values()):
        next_step = "证据分布不达标：复制当前任务重建完整证据，每份 PDF 至少 20 条。"
    elif model_development < development_sample:
        next_step = "点击一键运行自动流程，生成并双重核验问答。"
    elif not recipe_frozen:
        next_step = "自动流程会冻结配方，再生成 test 候选。"
    elif candidates_by_split["test"] == 0:
        next_step = "继续自动流程，使用冻结配方生成 test 候选。"
    elif review_counts.get("pending", 0):
        next_step = "集中审核待审问答；先处理 OCR 和结构异常。"
    elif approved < 90 * len(meta["documents"]):
        next_step = "继续批量生成并审核问答，直到达到每份文档约 90 条的结构目标。"
    else:
        quality = core.quality_report(job_id)
        next_step = ("下载正式数据包。" if meta["status"] == "final_exported" else "生成并下载正式数据包。") if quality["final_ready"] else "查看导出页的质量门槛并补足缺项。"
    return {
        "job_id": job_id,
        "documents": len(meta["documents"]),
        "parsed_pages": report["pages"] if report else 0,
        "evidence": len(evidence),
        "evidence_by_split": evidence_by_split,
        "evidence_by_document": evidence_by_document,
        "split_frozen": split_frozen,
        "recipe_frozen": recipe_frozen,
        "candidates": len(candidates),
        "candidates_by_split": candidates_by_split,
        "review_counts": review_counts,
        "approved": approved,
        "next_step": next_step,
    }


def review_queue(records: list[dict], units: list[dict], include_all: bool = False) -> list[dict]:
    """Surface OCR and invalid references first; never approve on a heuristic alone."""
    unit_map = {item["evidence_unit_id"]: item for item in units}
    pending = []
    for record in records:
        if not include_all and record["review"]["status"] not in {"pending", "pending_expert_review"}:
            continue
        errors = core.validate_candidate(record, unit_map)
        flags = []
        if errors:
            flags.append("结构异常")
        if any(not core.review_policy.source_verified(unit_map.get(ref.get("evidence_unit_id"), {}))
               for ref in record["evidence"]):
            flags.append("来源未核实")
        if record.get("coverage_check", {}).get("status") != "structure_checked":
            flags.append("事实覆盖待核对")
        if record.get("numeric_checks", {}).get("status") not in {"structure_checked", "not_applicable"}:
            flags.append("数值待核对")
        if any(unit_map.get(ref.get("evidence_unit_id"), {}).get("ocr_used") for ref in record["evidence"]):
            flags.append("OCR 原文")
        if record["answerability"] == "unanswerable":
            flags.append("证据不足回答")
        if record["review"]["status"] == "pending_expert_review":
            flags.append("模型不确定：" + record["review"].get("comment", "请核对")[:80])
        if any(len(ref["quote"]) > 1000 for ref in record["evidence"]):
            flags.append("长引用")
        pending.append({"record": record, "flags": flags, "errors": errors})
    return sorted(pending, key=lambda item: (not item["errors"], not item["flags"], item["record"]["created_at"]))
