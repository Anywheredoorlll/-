"""One approval gate shared by automated, API, Streamlit and CLI entry points."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path

POLICY_VERSION = "review-v2"
DEFAULT = {"auto_approve_enabled": False, "require_distinct_model_identity": True,
           "calibration_id": None, "max_claims_per_answer": 8}


def recipe_fingerprint(root: Path, job_id: str) -> str:
    """Hash the actual model roles, prompts, schemas and review parameters, never keys."""
    from aviation_ft import generation
    generation_cfg = json.loads((root / "configs/generation.json").read_text(encoding="utf-8"))
    review_cfg = json.loads((root / "configs/review.json").read_text(encoding="utf-8"))
    review_cfg.pop("calibration_id", None)
    review_cfg.pop("auto_approve_enabled", None)
    roles = {}
    for role in ("generator", "judge_primary", "judge_countercheck"):
        cfg = generation.config(job_id, role=role)
        roles[role] = {key: cfg.get(key) for key in (
            "model_id", "model_revision", "served_model_name", "provider_mode", "base_url")}
    prompts = {}
    for name in ("generate_grounded_qa.md", "judge_grounded_qa.md", "judge_unanswerable_qa.md"):
        path = root / "prompts" / name
        prompts[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    payload = {"generation": generation_cfg, "review": review_cfg, "roles": roles,
               "prompts": prompts, "candidate_schema_version": "qa-v2",
               "review_policy_version": POLICY_VERSION}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def config(root: Path, job_id: str | None = None) -> dict:
    path = root / "configs/review.json"
    policy = {**DEFAULT, **(json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {})}
    calibration_id = policy.get("calibration_id")
    if job_id and isinstance(calibration_id, str) and calibration_id.isidentifier():
        certificate = root / "runtime/calibrations" / f"{calibration_id}.json"
        if certificate.is_file():
            try:
                data = json.loads(certificate.read_text(encoding="utf-8"))
                if (data.get("calibration_id") == calibration_id
                        and data.get("job_id") == job_id
                        and data.get("accepted") is True
                        and data.get("evaluation", {}).get("acceptance", {}).get("auto_approval_error_rate", {}).get("ci95", [1, 1])[1] <= .01
                        and data.get("scope_groups")
                        and data.get("recipe_fingerprint") == recipe_fingerprint(root, job_id)):
                    policy["_calibration"] = data
            except (ValueError, KeyError, OSError, TypeError, IndexError):
                pass
    return policy


def source_verified(unit: dict) -> bool:
    verification = unit.get("source_verification") or {}
    return (verification.get("status") == "source_verified"
            and not unit.get("unresolved_blockers")
            and unit.get("parse_version") == "parse-v2"
            and verification.get("source_pdf_sha256") == unit.get("source_pdf_sha256")
            and bool(verification.get("reviewer"))
            and bool(verification.get("verified_at")))


def evaluate_approval(record: dict, units: dict[str, dict], assessment: dict | None,
                      policy: dict, *, automated: bool, claim_reviews=None,
                      source_reviews=None, numeric_review=None) -> dict:
    reasons = []
    refs = record.get("evidence", [])
    if any(not source_verified(units.get(ref.get("evidence_unit_id"), {})) for ref in refs):
        reasons.append("引用证据未对照 PDF 核实")
    if record.get("candidate_schema_version") != "qa-v2":
        reasons.append("旧问答结构尚未完成覆盖核验")
    if record.get("coverage_check", {}).get("status") != "structure_checked":
        reasons.append("答案事实覆盖尚未核实")
    numeric = record.get("numeric_checks", {})
    if numeric.get("status") not in {"structure_checked", "not_applicable"}:
        direct_conflict = any(issue in {"数字或单位在证据中缺失", "数值比较关系冲突"}
                              for issue in numeric.get("issues", []))
        fact_ids = set(range(len(numeric.get("answer_facts", []))))
        reviews = numeric_review.get("fact_reviews", []) if isinstance(numeric_review, dict) else []
        if not isinstance(reviews, list):
            reviews = []
        checked_ids = {x.get("index") for x in reviews
                       if isinstance(x, dict) and x.get("verdict") == "supported"}
        manual_numeric = (not automated and isinstance(numeric_review, dict)
                          and numeric_review.get("verdict") == "verified"
                          and isinstance(numeric_review.get("reason"), str)
                          and len(numeric_review["reason"].strip()) >= 10
                          and checked_ids == fact_ids and len(reviews) == len(fact_ids))
        if direct_conflict or not manual_numeric:
            reasons.append("数值关系待核实或存在确定性冲突")
    claims = record.get("claims", [])
    if automated:
        if not policy.get("auto_approve_enabled"):
            reasons.append("自动通过未启用")
        calibration = policy.get("_calibration")
        if not calibration:
            reasons.append("没有有效校准记录")
        elif any({"doc_id": units.get(ref.get("evidence_unit_id"), {}).get("doc_id"),
                   "clause_group": units.get(ref.get("evidence_unit_id"), {}).get("clause_group")}
                 not in calibration.get("scope_groups", []) for ref in refs):
            reasons.append("样本不在已验证的自动通过范围")
        passes = (assessment or {}).get("passes", [])
        if (assessment or {}).get("verdict") != "supported":
            reasons.append("综合评审未支持")
        if len(passes) != 2 or any(p.get("verdict") != "supported" for p in passes):
            reasons.append("两轮独立评审未一致支持")
        from aviation_ft import core
        expected_hash = core.candidate_content_sha256(record)
        if any(p.get("judged_record_sha256") != expected_hash
               or not all(p.get(field) for field in ("provider", "model_id", "prompt_sha256",
                                                       "request_settings", "response_sha256"))
               for p in passes):
            reasons.append("评审溯源元数据或候选版本不匹配")
        if any(p.get("coverage") != "supported" for p in passes):
            reasons.append("评审认为答案覆盖不完整")
        identities = [p.get("model_identity") for p in passes]
        if policy.get("require_distinct_model_identity", True) and (None in identities or len(set(identities)) != 2):
            reasons.append("评审模型身份未证实独立")
        def claim_result(review_pass, claim):
            results = review_pass.get("claim_results") or {}
            result = results.get(claim.get("claim_id"), {}) if isinstance(results, dict) else {}
            return result if isinstance(result, dict) else {}

        if any(claim_result(p, c).get("verdict") != "supported"
               for p in passes for c in claims):
            reasons.append("有断言未被逐项支持")
        ref_quotes = {ref.get("evidence_id"): ref.get("quote", "") for ref in refs}
        def quote_in_refs(quote, evidence_ids):
            return (isinstance(quote, str) and bool(quote)
                    and any(quote in ref_quotes.get(eid, "") for eid in evidence_ids))

        if any((record.get("answerability") == "answerable"
               and not quote_in_refs(p.get("supporting_quote"), ref_quotes))
               or any(not quote_in_refs(
                   claim_result(p, claim).get("supporting_quote"),
                   claim.get("evidence_ids", [])) for claim in claims)
               for p in passes):
            reasons.append("评审支持片段无法精确定位到对应证据")
    else:
        claim_ids = {c.get("claim_id") for c in claims}
        if any(not isinstance(x, dict) for x in (claim_reviews or [])):
            reasons.append("逐断言审核格式无效")
        elif (len(claim_reviews or []) != len({x.get("claim_id") for x in (claim_reviews or [])})
              or any(x.get("claim_id") not in claim_ids or x.get("verdict") not in {"supported", "unsupported", "uncertain"}
                     for x in (claim_reviews or []))):
            reasons.append("逐断言审核编号、数量或结论无效")
        reviewed = {x.get("claim_id") for x in (claim_reviews or [])
                    if isinstance(x, dict) and x.get("verdict") == "supported"}
        if any(c.get("claim_id") not in reviewed for c in claims):
            reasons.append("缺少逐断言人工核对")
        source_ids = {r.get("evidence_unit_id") for r in refs}
        if any(not isinstance(x, dict) for x in (source_reviews or [])):
            reasons.append("逐来源审核格式无效")
        elif (len(source_reviews or []) != len({x.get("evidence_unit_id") for x in (source_reviews or [])})
              or any(x.get("evidence_unit_id") not in source_ids or not isinstance(x.get("verified"), bool)
                     for x in (source_reviews or []))):
            reasons.append("逐来源审核编号、数量或结论无效")
        checked = {x.get("evidence_unit_id") for x in (source_reviews or [])
                   if isinstance(x, dict) and x.get("verified")}
        if any(ref.get("evidence_unit_id") not in checked for ref in refs):
            reasons.append("缺少逐来源人工核对")
    return {"approved": not reasons, "reasons": reasons, "review_policy_version": POLICY_VERSION}
