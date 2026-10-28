"""Claim-span and answer-coverage checks, separate from semantic support."""
from __future__ import annotations

import re

CLAIM_SCHEMA_VERSION = "qa-v2"
FACT_CUES = re.compile(r"\d|不低于|不高于|不得|必须|应当|至少|至多|仅|除外|如果|当|高于|低于")


def exact_span(text: str, quote: str) -> dict | None:
    if not quote:
        return None
    first = text.find(quote)
    if first < 0 or text.find(quote, first + 1) >= 0:
        return None
    return {"start": first, "end": first + len(quote)}


def validate_claim_structure(claims: list[dict], answerability: str, evidence: list[dict],
                             max_claims: int = 8) -> list[str]:
    errors = []
    if not isinstance(claims, list) or len(claims) > max_claims:
        return ["断言数量或格式无效；超限答案应拆分"]
    if answerability == "answerable" and not claims:
        errors.append("可回答样本缺少断言")
    allowed = {ref["evidence_id"]: ref["quote"] for ref in evidence}
    seen = set()
    for claim in claims:
        if not isinstance(claim, dict):
            errors.append("断言格式无效")
            continue
        cid = claim.get("claim_id")
        if not isinstance(cid, str) or cid in seen or not cid:
            errors.append("断言 ID 无效或重复")
        seen.add(cid)
        if not isinstance(claim.get("text"), str) or not claim.get("text", "").strip():
            errors.append("断言内容为空")
        if not isinstance(claim.get("answer_quote"), str) or not claim.get("answer_quote"):
            errors.append("断言缺少答案精确片段")
        ids = claim.get("evidence_ids")
        if not isinstance(ids, list) or not ids or any(eid not in allowed for eid in ids):
            errors.append("断言引用不存在的证据")
        quotes = claim.get("supporting_quotes")
        if not isinstance(quotes, list) or not quotes:
            errors.append("断言缺少证据精确片段")
            continue
        quote_ids = [item.get("evidence_id") for item in quotes if isinstance(item, dict)]
        if isinstance(ids, list) and (set(quote_ids) != set(ids) or len(quote_ids) != len(set(quote_ids))):
            errors.append("断言证据编号与支持片段不一致")
        for item in quotes:
            if not isinstance(item, dict) or item.get("evidence_id") not in allowed:
                errors.append("断言支持片段的证据编号无效")
            elif not exact_span(allowed[item["evidence_id"]], item.get("quote", "")):
                errors.append("支持片段无法唯一定位到证据")
    return list(dict.fromkeys(errors))


def resolve_claim_spans(answer: str, claims: list[dict], evidence: list[dict]) -> tuple[list[dict], list[str]]:
    refs = {r["evidence_id"]: r["quote"] for r in evidence}
    resolved, errors = [], []
    for claim in claims:
        span = exact_span(answer, claim.get("answer_quote", ""))
        if not span:
            errors.append(f"{claim.get('claim_id', '?')} 答案片段缺失或重复")
            continue
        supports = []
        for item in claim.get("supporting_quotes", []):
            ref = refs.get(item.get("evidence_id"))
            location = exact_span(ref or "", item.get("quote", ""))
            if not location:
                errors.append(f"{claim.get('claim_id', '?')} 支持片段缺失或重复")
            else:
                supports.append({**item, **location})
        resolved.append({"claim_id": claim["claim_id"], "answer_span": span,
                         "supporting_spans": supports})
    return resolved, errors


def check_answer_coverage(answer: str, claims: list[dict], answerability: str) -> dict:
    """Flags factual residue; passing this syntactic check is not semantic support."""
    spans = [exact_span(answer, claim.get("answer_quote", "")) for claim in claims]
    if any(span is None for span in spans):
        return {"status": "needs_human", "uncovered_factual_spans": [], "reason": "断言答案片段无法唯一定位"}
    covered = [False] * len(answer)
    for span in spans:
        for i in range(span["start"], span["end"]):
            covered[i] = True
    residue = "".join(" " if covered[i] else char for i, char in enumerate(answer))
    residue = re.sub(r"\[E\d+\]", " ", residue)
    uncovered = [{"start": m.start(), "end": m.end(), "text": m.group()}
                 for m in FACT_CUES.finditer(residue)]
    if answerability == "unanswerable":
        residue = re.sub(r"证据不足|无法确认|无法判断|需要补充信息|根据现有证据", " ", residue)
    # A natural-language remainder may contain unsupported facts without cue words.
    substantive = re.sub(r"[\s，。；：,.!?！？;:（）()、]+", "", residue)
    status = "needs_human" if uncovered or len(substantive) > 12 else "structure_checked"
    return {"status": status, "uncovered_factual_spans": uncovered,
            "reason": "存在未覆盖的事实线索或实质文字" if status == "needs_human" else "片段结构已覆盖，语义尚待审核"}
