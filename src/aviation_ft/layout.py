"""Conservative region and reading-order proposals; ambiguous order stays reviewable."""
from __future__ import annotations

import re

LAYOUT_VERSION = "layout-v2"
NEW_UNIT = re.compile(r"^(?:\d+(?:\.\d+)+\s+|第[一二三四五六七八九十百零〇\d]+[章节条])")


def detect_regions(page: dict, classify=None) -> list[dict]:
    blocks = [dict(block) for block in page.get("blocks", [])]
    height = float(page.get("height") or 0)
    for block in blocks:
        text = block["text"].strip()
        kind = classify(block) if classify else "prose"
        if re.match(r"^图\s*\d", text):
            kind = "caption"
        elif height and block["bbox"][1] > height * .86 and len(text) < 120 and not NEW_UNIT.match(text):
            kind = "footnote" if re.match(r"^[*注①②③\d]", text) else kind
        block["region_kind"] = kind
        block["column_id"] = 0
        block["order_status"] = "proposed"
    candidates = [b for b in blocks if b["region_kind"] not in {"heading", "caption", "footnote"}]
    if len(candidates) >= 4:
        centers = sorted((b["bbox"][0] + b["bbox"][2]) / 2 for b in candidates)
        gaps = [(centers[i + 1] - centers[i], i) for i in range(len(centers) - 1)]
        gap, index = max(gaps)
        boundary = (centers[index] + centers[index + 1]) / 2
        left = [b for b in candidates if (b["bbox"][0] + b["bbox"][2]) / 2 < boundary]
        right = [b for b in candidates if b not in left]
        if gap > float(page.get("width") or 0) * .12 and len(left) >= 2 and len(right) >= 2:
            if max(b["bbox"][2] for b in left) < min(b["bbox"][0] for b in right):
                for b in right:
                    b["column_id"] = 1
                for b in blocks:
                    if b not in candidates and b["bbox"][0] > boundary:
                        b["column_id"] = 1
            else:
                for b in candidates:
                    b["order_status"] = "needs_review"
    return blocks


def infer_reading_order(regions: list[dict]) -> tuple[list[dict], str]:
    if any(b["order_status"] == "needs_review" for b in regions):
        return regions, "needs_review"
    # Wide headings are emitted before columns; footnotes follow main text.
    ordered = sorted(regions, key=lambda b: (
        1 if b["region_kind"] == "footnote" else 0,
        b["column_id"], b["bbox"][1], b["bbox"][0],
    ))
    for index, block in enumerate(ordered):
        block["reading_order_index"] = index
    return ordered, "proposed"


def propose_semantic_groups(ordered: list[dict]) -> list[list[dict]]:
    groups: list[list[dict]] = []
    for block in ordered:
        if (not groups or block["column_id"] != groups[-1][-1]["column_id"]
                or block["region_kind"] != groups[-1][-1]["region_kind"]
                or NEW_UNIT.match(block["text"].strip())):
            groups.append([block])
        else:
            groups[-1].append(block)
    return groups


def evaluate_cross_page_merge(previous: dict, current: dict, config: dict) -> dict:
    reasons = []
    if previous.get("doc_id") != current.get("doc_id") or previous.get("end_pdf_page", previous.get("pdf_page", 0)) + 1 != current.get("pdf_page"):
        reasons.append("文档不同或页面不连续")
    if current.get("kind") in {"heading", "caption", "footnote"} or previous.get("kind") != current.get("kind"):
        reasons.append("区域类型不同")
    if NEW_UNIT.match(current.get("text", "").strip()):
        reasons.append("新条款开始")
    if previous.get("end_pdf_page", previous.get("pdf_page", 0)) - previous.get("pdf_page", 0) + 2 > config.get("max_cross_page_span", 2):
        reasons.append("跨页范围超限")
    if (config.get("section_consistency_required", True)
            and previous.get("section_path") and current.get("section_path")
            and previous["section_path"] != current["section_path"]):
        reasons.append("章节不同")
    if previous.get("kind") == "table" or current.get("kind") == "table":
        if config.get("table_continuation_requires_schema_match", True):
            reasons.append("续表行列与单位尚未核实")
    if config.get("same_column_required", True) and previous.get("column_id") != current.get("column_id"):
        reasons.append("跨栏")
    if previous.get("order_status") == "needs_review" or current.get("order_status") == "needs_review":
        reasons.append("阅读顺序待核实")
    if previous.get("ocr_used") or current.get("ocr_used") or previous.get("quality_issue_ids") or current.get("quality_issue_ids"):
        reasons.append("来源存在未核实问题")
    if len(previous.get("text", "")) + len(current.get("text", "")) > config.get("max_semantic_chars", 12000):
        reasons.append("语义块过长")
    return {"decision": "separate" if reasons else "proposed", "reasons": reasons,
            "rule_version": LAYOUT_VERSION, "confirmed_by": None}
