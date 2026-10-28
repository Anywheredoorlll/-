"""Versioned PDF extraction candidates and conservative page diagnostics.

Ratios describe page geometry or text anomalies; none estimates extraction accuracy.
Character offsets elsewhere refer to Python Unicode indices in the named candidate.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from difflib import SequenceMatcher

import pymupdf as fitz

PARSE_VERSION = "parse-v2"
UNKNOWN = "unknown"
UNITS = re.compile(r"\d+(?:\.\d+)?\s*(?:毫米|厘米|千米|公里|米|吨|千克|公斤|百分点|%|％|℃|°C|mm|km|kg|kPa|MPa|m(?:²|³|2|3)?)", re.I)
NUMBERS = re.compile(r"(?<![0-9A-Za-z])\d+(?:\.\d+)?(?![0-9A-Za-z])")
NEGATIONS = re.compile(r"不得|不能|不应|禁止|不低于|不高于|不少于|不大于|高于|低于|至少|至多")


def digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def config_hash(config: dict) -> str:
    return digest_text(json.dumps(config, ensure_ascii=False, sort_keys=True))


def union_area(boxes: list[list[float]], width: float, height: float) -> float:
    """Exact rectangle union using x slabs; overlapping boxes count once."""
    rects = []
    for box in boxes:
        x0, y0, x1, y1 = map(float, box)
        x0, x1 = max(0, min(width, x0)), max(0, min(width, x1))
        y0, y1 = max(0, min(height, y0)), max(0, min(height, y1))
        if x1 > x0 and y1 > y0:
            rects.append((x0, y0, x1, y1))
    xs = sorted({x for x0, _, x1, _ in rects for x in (x0, x1)})
    area = 0.0
    for left, right in zip(xs, xs[1:]):
        intervals = sorted((y0, y1) for x0, y0, x1, y1 in rects if x0 < right and x1 > left)
        end = -1.0
        for y0, y1 in intervals:
            area += (right - left) * max(0.0, y1 - max(y0, end))
            end = max(end, y1)
    return area


def _blocks(page, textpage, doc_id: str, page_no: int, ocr_numbers=frozenset()) -> list[dict]:
    result = []
    for raw in page.get_text("blocks", textpage=textpage):
        if len(raw) < 7 or raw[6] != 0 or not str(raw[4]).strip():
            continue
        result.append({
            "block_id": f"{doc_id}-p{page_no:04d}-b{len(result) + 1:04d}",
            "bbox": [round(float(x), 2) for x in raw[:4]],
            "text": str(raw[4]),
            "ocr_used": raw[5] in ocr_numbers,
        })
    return result


def _candidate(blocks: list[dict], method: str, settings: dict) -> dict:
    value = "\n".join(b["text"].rstrip("\n") for b in blocks)
    return {
        "candidate_id": f"{method}-{digest_text(value)[:16]}", "method": method,
        "text": value, "blocks": blocks, "text_sha256": digest_text(value),
        "engine": "PyMuPDF" if method == "native" else "PyMuPDF/Tesseract",
        "engine_version": getattr(fitz, "VersionBind", UNKNOWN),
        "settings": settings, "quality": {},
    }


def extract_native_page(page, doc_id: str, page_no: int) -> dict:
    return _candidate(_blocks(page, None, doc_id, page_no), "native", {})


def extract_ocr_page(page, doc_id: str, page_no: int, language: str, dpi: int,
                     mode: str, tessdata: str) -> dict:
    if mode not in {"supplement", "full_page"}:
        raise ValueError("无效 OCR 模式")
    textpage = page.get_textpage_ocr(language=language, dpi=dpi,
                                     full=mode == "full_page", tessdata=tessdata)
    numbers = {
        block["number"] for block in page.get_text("dict", textpage=textpage)["blocks"]
        if any(span.get("font") == "GlyphLessFont"
               for line in block.get("lines", []) for span in line.get("spans", []))
    }
    blocks = _blocks(page, textpage, doc_id, page_no, numbers)
    return _candidate(blocks, f"ocr_{mode}", {"language": language, "dpi": dpi,
                                               "mode": mode, "tessdata": tessdata})


def assess_page_extraction(page, candidate: dict, config: dict) -> dict:
    text = candidate["text"]
    chars = [c for c in text if not c.isspace()]
    count = len(chars)
    width, height = float(page.rect.width), float(page.rect.height)
    area = width * height
    images = page.get_image_info()
    image_boxes = [list(info["bbox"]) for info in images if info.get("bbox")]
    text_ratio = union_area([b["bbox"] for b in candidate["blocks"]], width, height) / area if area else UNKNOWN
    image_ratio = union_area(image_boxes, width, height) / area if area else UNKNOWN
    private = sum(0xE000 <= ord(c) <= 0xF8FF or 0xF0000 <= ord(c) <= 0x10FFFD for c in chars)
    replacement = sum(c == "\ufffd" for c in chars)
    quality = {
        "nonspace_char_count": count,
        "replacement_char_ratio": replacement / count if count else 0.0,
        "private_use_char_ratio": private / count if count else 0.0,
        "text_area_ratio": text_ratio, "image_area_ratio": image_ratio,
        "suspected_unextracted_regions": UNKNOWN,
        "font_mapping_anomaly": UNKNOWN,
        "table_risks": [], "reading_order_risks": [], "native_ocr_conflicts": [],
    }
    candidate["quality"] = quality
    return quality


def decide_ocr(quality: dict, config: dict) -> dict:
    """Return a trigger, mode and reasons; short title pages alone are not errors."""
    reasons = []
    damaged = quality["replacement_char_ratio"] >= config.get("replacement_char_ratio_trigger", .005)
    damaged |= quality["private_use_char_ratio"] >= config.get("private_use_char_ratio_trigger", .01)
    damaged |= quality.get("font_mapping_anomaly") is True
    if damaged:
        reasons.append("文字层含替换或私用区字符")
    image = quality["image_area_ratio"]
    text = quality["text_area_ratio"]
    sparse_image = (
        isinstance(image, (int, float)) and image >= config.get("image_area_ratio_trigger", .60)
        and isinstance(text, (int, float)) and text <= config.get("text_area_ratio_low", .01)
    )
    if sparse_image:
        reasons.append("大面积图片与少量文字区域并存")
    short_image = (quality["nonspace_char_count"] < config.get("min_nonspace_chars", 30)
                   and isinstance(image, (int, float)) and image > 0.05)
    if short_image and not sparse_image:
        reasons.append("含图片且提取文字较少")
    mode = "full_page" if damaged else "supplement"
    return {"attempt": bool(reasons), "mode": mode, "reasons": reasons}


def _normalized(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKC", text) if not c.isspace())


def compare_extractions(native: dict, ocr: dict, config: dict) -> dict:
    """SequenceMatcher disagreement is 1 - matched-ratio, not an accuracy score."""
    first, second = _normalized(native["text"]), _normalized(ocr["text"])
    disagreement = 1 - SequenceMatcher(None, first, second, autojunk=False).ratio() if first or second else 0.0
    conflicts = []
    for label, pattern in (("numbers", NUMBERS), ("units", UNITS), ("negations", NEGATIONS)):
        left, right = pattern.findall(native["text"]), pattern.findall(ocr["text"])
        if left != right:
            conflicts.append({"kind": label, "native": left, "ocr": right})
    return {"normalization": "NFKC and remove whitespace", "alignment": "SequenceMatcher",
            "disagreement_rate": disagreement, "conflicts": conflicts,
            "requires_source_review": bool(conflicts or disagreement >= config.get("native_ocr_disagreement_trigger", .05))}


def select_extraction(native: dict, ocr: dict | None, comparison: dict | None,
                      ocr_errors: list[str]) -> dict:
    """Keep native as the provisional view when the two sources conflict."""
    conflicts = comparison["conflicts"] if comparison else []
    needs_review = bool(ocr_errors or (comparison and comparison["requires_source_review"]))
    selected = native
    method = "native_provisional"
    if ocr and (not native["text"].strip() or
                (ocr["method"] == "ocr_supplement" and len(native["text"].strip()) < 30)):
        selected, method = ocr, "ocr_supplement_provisional"
        needs_review = True
    return {"selected_candidate_id": selected["candidate_id"],
            "selection_method": method,
            "selection_reasons": ocr_errors or (["原生与 OCR 冲突"] if conflicts else ["保留原生提取作为待核实视图"]),
            "requires_source_review": needs_review or bool(ocr),
            "conflicts": conflicts}
