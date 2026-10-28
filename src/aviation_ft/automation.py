"""Resumable model-led evidence selection and two-pass answer verification."""
from __future__ import annotations

import hashlib
import inspect
import json
import re
import unicodedata
from collections import Counter
from contextvars import ContextVar
from functools import wraps
from pathlib import Path

from aviation_ft import core, generation

EVIDENCE_TARGETS = {"train": 12, "validation": 4, "test": 4}
CONFIDENCE = 0.90
EVIDENCE_CONFIDENCE = 0.95
_PROVIDER_JOB_ID: ContextVar[str | None] = ContextVar("provider_job_id", default=None)


def _provider_config() -> dict:
    parameters = inspect.signature(generation.config).parameters
    return generation.config(_PROVIDER_JOB_ID.get()) if "job_id" in parameters else generation.config()


def _with_provider_context(fn):
    @wraps(fn)
    def wrapped(job_id: str, *args, **kwargs):
        token = _PROVIDER_JOB_ID.set(job_id)
        try:
            return fn(job_id, *args, **kwargs)
        finally:
            _PROVIDER_JOB_ID.reset(token)
    return wrapped


def _completion(system: str, user: dict, max_tokens: int = 320) -> dict:
    cfg = _provider_config()
    response = generation._configured_request(
        cfg,
        "/chat/completions",
        {"model": cfg["served_model_name"],
         "messages": [{"role": "system", "content": "/no_think\n" + system},
                      {"role": "user", "content": json.dumps(user, ensure_ascii=False)}],
         "temperature": 0, "max_tokens": max_tokens,
         "chat_template_kwargs": {"enable_thinking": False}},
        timeout=int(cfg["timeout_seconds"]),
    )
    return generation._parse_json(response["choices"][0]["message"]["content"])


def _assessment(data: dict, valid_verdicts: set[str]) -> dict:
    if set(data) != {"verdict", "confidence", "reason", "supporting_quote"}:
        raise ValueError("核验模型未返回规定字段")
    if data["verdict"] not in valid_verdicts or not isinstance(data["reason"], str) or not isinstance(data["supporting_quote"], str):
        raise ValueError("核验模型返回的结论无效")
    score = data["confidence"]
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 1:
        raise ValueError("核验模型返回的置信度无效")
    return data


def _evidence_suitability(item: dict) -> dict:
    data = _completion(
        "你是民航 PDF 数据整理员。判断给定文本是否是可独立引用的连续条款或表格片段。若文字顺序混乱、缺句、页眉页脚占主导或无法判断，选择 uncertain；明显无实质内容选 unsuitable。仅输出 JSON：verdict (suitable/unsuitable/uncertain)、confidence (0-1)、reason、supporting_quote（原文中的精确短句）。",
        {"text": item["quote"], "page": item["pdf_page"]},
    )
    return _assessment(data, {"suitable", "unsuitable", "uncertain"})


def _queue_evidence_issue(path: Path, item: dict, reason: str) -> None:
    key = (item["block_id"], reason)
    existing = core.read_jsonl(path)
    if any((row["block_id"], row["reason"]) == key for row in existing):
        return
    core.append_jsonl(path, {"at": core.now(), "block_id": item["block_id"],
                             "pdf_page": item["pdf_page"], "reason": reason})


@_with_provider_context
def prepare_evidence(job_id: str, max_assessments: int = 120) -> dict:
    """Select 20 supported, non-OCR page spans per PDF; defer ambiguous pages."""
    if not 1 <= max_assessments <= 500:
        raise ValueError("证据核验数量必须在 1 到 500 之间")
    p = core.job_dir(job_id)
    if not (p / "parsed/report.json").exists():
        core.parse_job(job_id)
    if (p / "evidence/splits.json").exists():
        return {"status": "frozen", "added": 0, "assessed": 0, "issues": []}
    cfg = _provider_config()
    pages = {(page["doc_id"], page["pdf_page"]): page for page in core.pages_for(job_id)}
    records = core.active_evidence_for(job_id)
    assessments_path = p / "evidence/auto_assessments.jsonl"
    cached = {row["block_id"]: row for row in core.read_jsonl(assessments_path)}
    issue_path = p / "evidence/human_queue.jsonl"
    added = assessed = 0
    issues = []
    for doc in core.load_job(job_id)["documents"]:
        counts = Counter(row["split"] for row in records if row["doc_id"] == doc["doc_id"])
        proposals = core.evidence_suggestions(job_id, doc["doc_id"], limit=200)
        for split, target in EVIDENCE_TARGETS.items():
            for item in [x for x in proposals if x["split"] == split]:
                if counts[split] >= target:
                    break
                page = pages[(doc["doc_id"], item["pdf_page"])]
                quality_issues = item.get("quality_issues") or core.evidence_source_issues(item)
                if page.get("requires_verification") and not page.get("source_verified"):
                    quality_issues = [*quality_issues, "当前来源版本待人工核实"]
                if quality_issues:
                    reason = "；".join(quality_issues)
                    _queue_evidence_issue(issue_path, item, reason)
                    issues.append({"file": doc["filename"], "page": item["pdf_page"], "reason": reason})
                    continue
                if (
                    item.get("possible_continuation_from_previous_page")
                    or item.get("possible_continuation_to_next_page")
                ):
                    reason = "语义段落可能跨页，需合并上下页后人工核实"
                    _queue_evidence_issue(issue_path, item, reason)
                    issues.append({"file": doc["filename"], "page": item["pdf_page"], "reason": reason})
                    continue
                if item["ocr_used"] or page["possible_scanned"]:
                    reason = "OCR 或少文本页需对照 PDF 核实"
                    _queue_evidence_issue(issue_path, item, reason)
                    issues.append({"file": doc["filename"], "page": item["pdf_page"], "reason": reason})
                    continue
                previous = cached.get(item["block_id"])
                quote_sha = hashlib.sha256(item["quote"].encode()).hexdigest()
                if previous and previous["quote_sha256"] == quote_sha and previous.get("source_snapshot") == item["source_snapshot"]:
                    verdict = previous["verdict"]
                    score = previous["confidence"]
                    supporting = previous["supporting_quote"]
                else:
                    if assessed >= max_assessments:
                        return {"status": "in_progress", "added": added, "assessed": assessed, "issues": issues}
                    try:
                        result = _evidence_suitability(item)
                        verdict, score, supporting = result["verdict"], result["confidence"], result["supporting_quote"]
                        reason = result["reason"]
                    except Exception as exc:
                        verdict, score, supporting, reason = "uncertain", 0, "", str(exc)
                    assessed += 1
                    log = {"at": core.now(), "block_id": item["block_id"], "source_snapshot": item["source_snapshot"], "quote_sha256": quote_sha,
                           "verdict": verdict, "confidence": score, "supporting_quote": supporting,
                           "reason": reason, "model_id": cfg["model_id"]}
                    core.append_jsonl(assessments_path, log)
                    cached[item["block_id"]] = log
                if verdict == "suitable" and score >= EVIDENCE_CONFIDENCE and supporting and supporting in item["quote"]:
                    new = core.add_evidence_batch(job_id, [item], f"model:{cfg['served_model_name']}")[0]
                    records.append(new)
                    counts[split] += 1
                    added += 1
                elif verdict == "uncertain" or verdict == "suitable":
                    reason = "模型无法可靠判断原文是否完整"
                    _queue_evidence_issue(issue_path, item, reason)
                    issues.append({"file": doc["filename"], "page": item["pdf_page"], "reason": reason})
        for split, target in EVIDENCE_TARGETS.items():
            if counts[split] < target:
                issues.append({"file": doc["filename"], "split": split,
                               "reason": f"证据不足：{counts[split]}/{target}"})
    if issues and any("证据不足" in item["reason"] for item in issues):
        return {"status": "needs_human", "added": added, "assessed": assessed, "issues": issues}
    all_units = core.active_evidence_for(job_id)
    by_split = Counter(unit["split"] for unit in all_units)
    document_count = len(core.load_job(job_id)["documents"])
    if (len(all_units) < 20 * document_count or
            by_split["validation"] < 4 * document_count or
            by_split["test"] < 4 * document_count):
        return {"status": "needs_human", "added": added, "assessed": assessed,
                "issues": issues + [{"reason": "总证据或验证/测试证据不足"}]}
    core.freeze_splits(job_id)
    return {"status": "frozen", "added": added, "assessed": assessed, "issues": issues}


def _normalized_text(text: str) -> str:
    """Normalize PDF line wrapping without weakening source traceability."""
    return "".join(
        char for char in unicodedata.normalize("NFKC", text)
        if not char.isspace()
    )


def _supporting_quote_match_mode(supporting_quote: str, quotes: list[str]) -> str:
    if not supporting_quote:
        return "none"
    if any(supporting_quote in quote for quote in quotes):
        return "exact"
    normalized = _normalized_text(supporting_quote)
    if normalized and any(normalized in _normalized_text(quote) for quote in quotes):
        return "normalized"
    return "none"


def _number_tokens(text: str) -> set[str]:
    text = unicodedata.normalize("NFKC", re.sub(r"\[E\d+\]", "", text))
    text = re.sub(r"\b[A-Z]{2,12}\s*[-_]?\d{3,}(?:[-—]\d{2,4})?\b", "", text)
    text = re.sub(r"(?<=\d)[\s\u00a0](?=\d{3}(?:\D|$))", "", text)
    return set(re.findall(r"(?<![0-9A-Za-z])\d+(?:[.,]\d+)*(?![0-9A-Za-z])", text))


def _judge_candidate(record: dict) -> dict:
    prompt_name = (
        "judge_unanswerable_qa.md"
        if record["answerability"] == "unanswerable"
        else "judge_grounded_qa.md"
    )
    prompt = (core.ROOT / "prompts" / prompt_name).read_text(encoding="utf-8")
    evidence_text = "\n".join(f"[{ref['evidence_id']}] {ref['quote']}" for ref in record["evidence"])
    user = {"question": record["question"], "answer": record["answer"],
            "answerability": record["answerability"], "claims": record["claims"],
            "evidence": evidence_text}
    first = _assessment(_completion(prompt, user), {"supported", "unsupported", "uncertain"})
    if first["verdict"] != "supported" or first["confidence"] < CONFIDENCE:
        verdict = first["verdict"] if first["confidence"] >= CONFIDENCE else "uncertain"
        return {
            "passes": [first], "verdict": verdict,
            "reason": f"第一轮核验：{first['reason']}",
        }
    countercheck = (
        "请从反例角度核查：证据是否其实足以回答问题，或拒答答案是否添加了证据外事实；有疑点选择 uncertain。"
        if record["answerability"] == "unanswerable"
        else "请从反例角度再次核查，尤其寻找答案中任何原文没有的数字、条件或断言；有疑点选择 uncertain。"
    )
    second = _assessment(
        _completion(prompt + "\n" + countercheck, user),
        {"supported", "unsupported", "uncertain"},
    )
    verdict = "supported" if second["verdict"] == "supported" and second["confidence"] >= CONFIDENCE else "uncertain"
    return {
        "passes": [first, second], "verdict": verdict,
        "reason": (
            "两轮核验均通过"
            if verdict == "supported"
            else f"第二轮核验：{second['reason']}"
        ),
    }


@_with_provider_context
def review_pending(
    job_id: str, limit: int = 300, include_expert_review: bool = False,
) -> dict:
    if not 1 <= limit <= 500:
        raise ValueError("自动审核数量必须在 1 到 500 之间")
    cfg = _provider_config()
    units = {unit["evidence_unit_id"]: unit for unit in core.evidence_for(job_id)}
    summary = Counter()
    eligible = {"pending", "pending_expert_review"} if include_expert_review else {"pending"}
    for record in core.candidates_for(job_id):
        if record["review"]["status"] not in eligible:
            continue
        if summary["processed"] >= limit:
            break
        errors = core.validate_candidate(record, units)
        if errors:
            verdict = "rejected"
            assessment = {"verdict": verdict, "reason": "；".join(errors), "check": "deterministic"}
        elif any(units[ref["evidence_unit_id"]].get("ocr_used") for ref in record["evidence"]):
            verdict = "pending_expert_review"
            assessment = {"verdict": "uncertain", "reason": "OCR 原文需人工核对", "check": "deterministic"}
        elif (record["answerability"] == "answerable" and
              not _number_tokens(record["answer"] + " " + " ".join(c["text"] for c in record["claims"])).issubset(
                  _number_tokens(" ".join(ref["quote"] for ref in record["evidence"])))):
            verdict = "rejected"
            assessment = {"verdict": "unsupported", "reason": "答案数字未能在证据中直接找到，不符合直接引用要求", "check": "deterministic"}
        else:
            try:
                assessment = _judge_candidate(record)
                if assessment["verdict"] == "supported":
                    quotes = [ref["quote"] for ref in record["evidence"]]
                    if record["answerability"] == "answerable":
                        modes = [
                            _supporting_quote_match_mode(item["supporting_quote"], quotes)
                            for item in assessment["passes"]
                        ]
                        assessment["supporting_quote_match"] = modes
                        if "none" in modes:
                            assessment["verdict"] = "uncertain"
                            assessment["reason"] = "模型支持片段规范化后仍无法定位原文"
                verdict = {"supported": "approved", "unsupported": "rejected",
                           "uncertain": "pending_expert_review"}[assessment["verdict"]]
            except Exception as exc:
                verdict = "pending_expert_review"
                assessment = {"verdict": "uncertain", "reason": str(exc), "check": "model_error"}
        assessment["model_id"] = cfg["model_id"]
        assessment["model_revision"] = cfg.get("model_revision")
        judge_path = core.ROOT / "prompts/judge_grounded_qa.md"
        assessment["judge_prompt_sha256"] = core.sha256_file(judge_path) if judge_path.is_file() else None
        core.review_candidate(job_id, record["sample_id"], verdict,
                              f"model:{cfg['served_model_name']}", assessment.get("reason", ""), assessment)
        summary["processed"] += 1
        summary[verdict] += 1
    return dict(summary)


def advance(job_id: str, max_new: int = 200, max_review: int = 300) -> dict:
    """Advance one bounded stage; safe to retry after interruption."""
    p = core.job_dir(job_id)
    if not core.load_job(job_id)["documents"]:
        return {"status": "needs_upload", "reason": "请先上传至少一份 PDF，确认本任务上传完成后再运行"}
    generation.probe(job_id)
    if not (p / "evidence/splits.json").exists():
        evidence = prepare_evidence(job_id)
        if evidence["status"] != "frozen":
            return {"status": evidence["status"], "evidence": evidence}
    else:
        evidence = {"status": "frozen", "added": 0}
    levels = [8, 12, 18, 24, 30]
    level_path = p / "automation/generation_level.json"
    level = core.read_json(level_path, {"per_unit": levels[0]})["per_unit"]
    if level not in levels:
        raise ValueError("自动生成级别记录无效")
    per_unit = level
    progress_path = p / "automation/progress.jsonl"
    def report_generation(stage: str, summary: dict) -> None:
        if summary["processed"] % 10 == 0 or summary["processed"] == summary["total_planned"]:
            core.append_jsonl(progress_path, {"at": core.now(), "status": "generating",
                                                  "stage": stage, "summary": summary})
    generated_dev = generation.generate_batch(
        job_id, per_unit=per_unit, max_new=max_new,
        progress=lambda state: report_generation("development", state),
    )
    reviewed_dev = review_pending(job_id, max_review)
    if not (p / "candidates/generation_recipe.json").exists():
        if not any(r["review"]["status"] == "approved" and r["split"] in {"train", "validation"}
                   for r in core.candidates_for(job_id)):
            return {"status": "needs_human", "reason": "开发集没有通过自动核验的样本",
                    "evidence": evidence, "generated_dev": generated_dev, "reviewed_dev": reviewed_dev}
        generation.freeze_recipe(job_id)
    generated_test = generation.generate_batch(
        job_id, per_unit=per_unit, max_new=max_new,
        progress=lambda state: report_generation("test", state), split="test",
    )
    reviewed_test = review_pending(job_id, max_review)
    report = core.quality_report(job_id)
    result = {"status": "ready" if report["final_ready"] else "in_progress",
              "evidence": evidence, "generated_dev": generated_dev, "reviewed_dev": reviewed_dev,
              "generated_test": generated_test, "reviewed_test": reviewed_test,
              "quality": report, "per_unit": per_unit}
    made_progress = (generated_dev["created"] + generated_test["created"] +
                     reviewed_dev.get("processed", 0) + reviewed_test.get("processed", 0))
    if report["final_ready"]:
        artifact, digest, _ = core.export_job(job_id, final=True)
        result["export"] = {"filename": artifact.name, "sha256": digest}
    elif made_progress == 0 and per_unit < levels[-1]:
        next_level = levels[levels.index(per_unit) + 1]
        core.write_json(level_path, {"per_unit": next_level, "changed_at": core.now()})
        result["reason"] = f"当前问法用尽，自动扩大到每条证据 {next_level} 个问法"
    elif made_progress == 0:
        result["status"] = "needs_human"
        result["reason"] = "所有自动问法已尝试，仍无法达到正式门槛；查看质量报告和人工队列"
    return result
