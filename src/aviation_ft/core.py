from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import stat
import tempfile
import unicodedata
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pymupdf as fitz

from aviation_ft import claim_validation, extraction_quality, layout, numeric_validation, review_policy

ROOT = Path(__file__).resolve().parents[2]
JOB_RE = re.compile(r"^[0-9a-f]{32}$")
SPLITS = {"train", "validation", "test"}
DECISIONS = {"approved", "approved_after_edit", "regenerate", "rejected", "pending_expert_review"}


class RevisionConflict(ValueError):
    """A reviewer submitted an older candidate revision."""
CLAUSE_START_RE = re.compile(r"^(?:\d+(?:\.\d+){1,}\s+|第[一二三四五六七八九十百零〇\d]+[章节条]\s*|[（(]\d+[）)]\s*)")
TABLE_TITLE_RE = re.compile(r"^表\s*\d")
HEADING_RE = re.compile(r"^(?:\d+(?:\.\d+)*\s+\S|第[一二三四五六七八九十百零〇\d]+章)")
LIST_ITEM_RE = re.compile(r"^(?:[（(]\s*(?:\d+|[一二三四五六七八九十]+)\s*[）)]|(?:\d+|[一二三四五六七八九十]+)[)、]|(?:\d+|[一二三四五六七八九十]+)\.\s+)\s*\S")
SIMPLE_LIST_ITEM_RE = re.compile(r"^\d+\s+\S")
TERMINAL_PUNCTUATION = frozenset("。！？；.!?;")
PDF_CLAUSE_DOT = "\U001001b0"
PDF_LEADER_DOT = "\U001001ba"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def settings() -> dict:
    return json.loads((ROOT / "configs/settings.json").read_text(encoding="utf-8"))


def quality_settings() -> dict:
    return read_json(ROOT / "configs/quality.json", {})


def jobs_root() -> Path:
    p = ROOT / "runtime/jobs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def job_dir(job_id: str) -> Path:
    if not JOB_RE.fullmatch(job_id):
        raise ValueError("无效任务 ID")
    p = jobs_root() / job_id
    if not p.is_dir() or p.is_symlink():
        raise ValueError("任务不存在")
    return p


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(value, out, ensure_ascii=False, indent=2, sort_keys=True)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            for record in records:
                out.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as out:
        out.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        out.flush()
        os.fsync(out.fileno())


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def create_job(label: str = "") -> str:
    job_id = uuid.uuid4().hex
    p = jobs_root() / job_id
    for sub in ("raw_pdf", "parsed", "evidence", "candidates", "reviewed", "exports"):
        (p / sub).mkdir(parents=True, exist_ok=False)
    write_json(p / "job.json", {"job_id": job_id, "label": label.strip()[:100], "created_at": now(), "status": "created", "documents": []})
    return job_id


def load_job(job_id: str) -> dict:
    return read_json(job_dir(job_id) / "job.json")


def list_jobs() -> list[dict]:
    result = []
    for p in jobs_root().iterdir():
        if p.is_dir() and JOB_RE.fullmatch(p.name) and not p.is_symlink():
            meta = read_json(p / "job.json")
            if meta:
                result.append(meta)
    return sorted(result, key=lambda x: x["created_at"], reverse=True)


def clone_parsed_job(job_id: str, label: str = "") -> str:
    """Start a new evidence version from verified PDF files and parsed pages."""
    source = job_dir(job_id)
    meta = load_job(job_id)
    parsed_files = ["pages.jsonl", "report.json"]
    if not meta["documents"] or any(not (source / "parsed" / name).is_file() for name in parsed_files):
        raise ValueError("源任务尚未完成 PDF 解析")
    for document in meta["documents"]:
        pdf = source / "raw_pdf" / document["filename"]
        if pdf.is_symlink() or not pdf.is_file() or sha256_file(pdf) != document["sha256"]:
            raise ValueError(f"原始 PDF 缺失或哈希变化：{document['filename']}")
    new_id = create_job(label or f"{meta['label']} · 完整证据版")
    target = job_dir(new_id)
    try:
        for document in meta["documents"]:
            shutil.copy2(source / "raw_pdf" / document["filename"], target / "raw_pdf" / document["filename"])
        for name in parsed_files:
            shutil.copy2(source / "parsed" / name, target / "parsed" / name)
        clone = load_job(new_id)
        clone["documents"] = [dict(document) for document in meta["documents"]]
        clone["source_job_id"] = job_id
        clone["status"] = "parsed"
        write_json(target / "job.json", clone)
        return new_id
    except Exception:
        shutil.rmtree(target)
        raise


def upload_pdf(job_id: str, name: str, source) -> dict:
    p = job_dir(job_id)
    if (p / "parsed/pages.jsonl").exists() or (p / "evidence/units.jsonl").exists():
        raise ValueError("任务已开始解析或生成证据；请在开始构建前完成本任务的 PDF 上传")
    if not name or name != Path(name).name or "\\" in name or not name.lower().endswith(".pdf"):
        raise ValueError("仅允许无路径的 PDF 文件名")
    cfg = settings()
    meta = load_job(job_id)
    if name in {d["filename"] for d in meta["documents"]}:
        raise ValueError("同名 PDF 已存在")
    limit = int(cfg["max_file_mb"]) * 1024**2
    total_limit = int(cfg["max_job_mb"]) * 1024**2
    used = sum(d["size_bytes"] for d in meta["documents"])
    free = shutil.disk_usage(p).free
    if free < (int(cfg["min_free_gb_after_upload"]) * 1024**3 + limit):
        raise ValueError("服务器剩余空间不足，上传被拒绝")
    fd, tmp = tempfile.mkstemp(prefix=".upload-", dir=p / "raw_pdf")
    size = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit or used + size > total_limit:
                    raise ValueError("PDF 超出单文件或任务总大小限制")
                out.write(chunk)
        with open(tmp, "rb") as f:
            if f.read(5) != b"%PDF-":
                raise ValueError("文件不是有效 PDF")
        with fitz.open(tmp) as doc:
            pages = doc.page_count
        digest = sha256_file(Path(tmp))
        if any(document["sha256"] == digest for document in meta["documents"]):
            raise ValueError("相同内容的 PDF 已上传，请勿仅更改文件名重复加入任务")
        target = p / "raw_pdf" / name
        os.replace(tmp, target)
        target.chmod(0o444)
        entry = {"doc_id": f"DOC-{digest[:12]}", "filename": name, "sha256": digest, "size_bytes": size, "pages": pages, "uploaded_at": now()}
        meta["documents"].append(entry)
        meta["status"] = "uploaded"
        write_json(p / "job.json", meta)
        return entry
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def remove_unparsed_pdf(job_id: str, filename: str) -> None:
    """Remove a mistaken upload before any parsing or evidence work starts."""
    p = job_dir(job_id)
    if (p / "parsed/pages.jsonl").exists() or (p / "evidence/units.jsonl").exists():
        raise ValueError("PDF 已解析或生成证据，不能单独移除；请新建任务")
    meta = load_job(job_id)
    document = next((d for d in meta["documents"] if d["filename"] == filename), None)
    if not document:
        raise ValueError("上传文件不存在")
    source = p / "raw_pdf" / filename
    if source.is_symlink() or not source.is_file() or sha256_file(source) != document["sha256"]:
        raise ValueError("原始 PDF 缺失或哈希变化，拒绝移除")
    source.chmod(stat.S_IWRITE | stat.S_IREAD)
    source.unlink()
    meta["documents"] = [d for d in meta["documents"] if d["filename"] != filename]
    meta["status"] = "uploaded" if meta["documents"] else "created"
    write_json(p / "job.json", meta)


def _prose_text(blocks: list[dict]) -> str:
    """Remove visual line wrapping while retaining meaningful word boundaries."""
    parts = [line.strip() for block in blocks for line in str(block["text"]).splitlines() if line.strip()]
    if not parts:
        return ""
    text = parts[0]
    for part in parts[1:]:
        if text.endswith("-") and part[:1].isascii() and part[:1].isalpha():
            text = text[:-1] + part
        elif text[-1:].isascii() and text[-1:].isalnum() and part[:1] not in "，。！？；：,.!?;:":
            text += " " + part
        else:
            text += part
    return text


def _match_text(text: str) -> str:
    """Normalize PDF glyph substitutions used by clause punctuation."""
    return unicodedata.normalize("NFKC", text).translate(str.maketrans({
        PDF_CLAUSE_DOT: ".", PDF_LEADER_DOT: ".", "ꎮ": "。", "ꎬ": "，", "ꎻ": "；",
    }))


def _ends_terminal(text: str) -> bool:
    normalized = _match_text(text).rstrip()
    return bool(normalized) and normalized[-1] in TERMINAL_PUNCTUATION


def _page_height(page: dict, blocks: list[dict]) -> float | None:
    height = page.get("height")
    if isinstance(height, (int, float)) and height > 0:
        return float(height)
    max_y = max((float(block["bbox"][3]) for block in blocks), default=0.0)
    return max_y / 0.93 if max_y > 500 else None


def _content_blocks(page: dict) -> list[dict]:
    """Return content blocks in extractor order, excluding detectable page furniture."""
    blocks = list(page.get("blocks", []))
    height = _page_height(page, blocks)
    if len(blocks) <= 1 or height is None:
        return blocks
    return [
        block for block in blocks
        if not (
            height * 0.065 <= float(block["bbox"][1]) < height * 0.085
            and len(str(block["text"]).strip()) <= 64
            and not CLAUSE_START_RE.match(_match_text(str(block["text"]).strip()))
            and not _ends_terminal(str(block["text"]))
        )
        and not (
            float(block["bbox"][1]) >= height * 0.90
            and re.fullmatch(r"[\s—–−\-]*[\dIVXLCDMivxlcdm]+[\s—–−\-\d]*",
                             _match_text(str(block["text"])).strip())
        )
    ]


def _block_kind(block: dict) -> str:
    text = str(block.get("text", "")).strip()
    match_text = _match_text(text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if TABLE_TITLE_RE.match(match_text) or match_text == "续表":
        return "table"
    if (
        LIST_ITEM_RE.match(match_text)
        or (SIMPLE_LIST_ITEM_RE.match(match_text) and float(block["bbox"][0]) < 140)
    ):
        return "list_item"
    if len(lines) >= 2 and re.match(r"^\d+\s*[<>≤≥]", match_text):
        return "table"
    if HEADING_RE.match(match_text) and len(match_text) <= 32 and float(block["bbox"][0]) >= 140:
        return "heading"
    if CLAUSE_START_RE.match(match_text) or text.startswith("【条文说明】"):
        return "prose"
    if len(lines) >= 2 and (
        len(lines[0]) <= 16
        or (all(len(line) <= 32 for line in lines) and sum(char.isdigit() for char in text) >= 2)
    ):
        return "table"
    return "prose"


def _starts_semantic_unit(block: dict) -> bool:
    text = str(block.get("text", "")).strip()
    match_text = _match_text(text)
    return bool(
        CLAUSE_START_RE.match(match_text) or LIST_ITEM_RE.match(match_text)
        or (SIMPLE_LIST_ITEM_RE.match(match_text) and float(block["bbox"][0]) < 140)
        or text.startswith("【条文说明】") or TABLE_TITLE_RE.match(match_text) or match_text == "续表"
        or (HEADING_RE.match(match_text) and len(match_text) <= 32 and float(block["bbox"][0]) >= 140)
    )


def _nearby_vertical(previous: dict, current: dict) -> bool:
    previous_box, current_box = previous["bbox"], current["bbox"]
    previous_height = max(1.0, float(previous_box[3]) - float(previous_box[1]))
    current_height = max(1.0, float(current_box[3]) - float(current_box[1]))
    gap = float(current_box[1]) - float(previous_box[3])
    return -8.0 <= gap <= max(28.0, 1.8 * max(previous_height, current_height))


def semantic_blocks_for_page(page: dict, previous_page: dict | None = None) -> list[dict]:
    """Derive readable clauses/tables without replacing auditable raw PDF blocks."""
    raw_blocks = page.get("blocks", [])
    retained = {b["block_id"] for b in _content_blocks(page)}
    regions = layout.detect_regions(page, _block_kind)
    ordered, order_status = layout.infer_reading_order(regions)
    content_blocks = [b for b in ordered if b["block_id"] in retained]
    group_starts = {group[0]["block_id"] for group in layout.propose_semantic_groups(content_blocks)}
    if not content_blocks:
        return []
    groups: list[tuple[str, list[dict]]] = []
    for block_index, block in enumerate(content_blocks):
        kind = block.get("region_kind", _block_kind(block))
        if not groups:
            groups.append((kind, [block]))
            continue
        previous_kind, current = groups[-1]
        previous = current[-1]
        same_left_edge = abs(float(previous["bbox"][0]) - float(block["bbox"][0])) <= 24
        merge = False
        block_text = str(block.get("text", "")).strip()
        match_text = _match_text(block_text)
        # PDF producers may emit a tall merged cell before its table rows.
        # Compare against the whole table and look ahead for a row bridging the gap.
        table_bottom = max(float(part["bbox"][3]) for part in current)
        table_top = min(float(part["bbox"][1]) for part in current)
        table_gap = float(block["bbox"][1]) - table_bottom
        following = content_blocks[block_index + 1:block_index + 2]
        bridges_cell = bool(
            following and _block_kind(following[0]) == "table"
            and float(following[0]["bbox"][1]) <= table_bottom + 45
            and float(block["bbox"][1]) >= table_top
            and float(block["bbox"][1]) <= float(following[0]["bbox"][1]) + 100
        )
        overlaps_table = (
            float(block["bbox"][3]) >= table_top
            and float(block["bbox"][1]) <= table_bottom
        )
        if (
            previous_kind == "table"
            and not TABLE_TITLE_RE.match(match_text)
            and not CLAUSE_START_RE.match(match_text)
            and kind not in {"heading", "list_item"}
            and not block_text.startswith(("注", "【条文说明】"))
            and (overlaps_table or 0 <= table_gap <= 45.0 or bridges_cell)
        ):
            merge = True
        elif (
            previous_kind in {"prose", "list_item"}
            and kind == "prose"
            and same_left_edge
            and _nearby_vertical(previous, block)
        ):
            merge = not _starts_semantic_unit(block)
        if merge:
            current.append(block)
        else:
            groups.append((kind, [block]))

    result = []
    previous_semantic = semantic_blocks_for_page(previous_page) if previous_page else []
    section_path = list(previous_semantic[-1].get("section_path", [])) if previous_semantic else []
    for index, (kind, blocks) in enumerate(groups, 1):
        raw_text = "\n".join(str(block["text"]).rstrip("\n") for block in blocks).strip()
        text = raw_text if kind == "table" else _prose_text(blocks)
        if kind == "heading":
            section_path = [text]
        raw_positions = [next(i for i, raw in enumerate(raw_blocks)
                              if raw["block_id"] == block["block_id"]) for block in blocks]
        range_start, range_end = min(raw_positions), max(raw_positions)
        start_id = raw_blocks[range_start]["block_id"]
        end_id = raw_blocks[range_end]["block_id"]
        bbox = [
            round(min(block["bbox"][0] for block in blocks), 2),
            round(min(block["bbox"][1] for block in blocks), 2),
            round(max(block["bbox"][2] for block in blocks), 2),
            round(max(block["bbox"][3] for block in blocks), 2),
        ]
        block_id = start_id if len(blocks) == 1 else (
            f"{page['doc_id']}-p{page['pdf_page']:04d}-r"
            f"{range_start + 1:04d}-{range_end + 1:04d}"
        )
        starts_mid_sentence = (
            index == 1 and bool(previous_semantic)
            and previous_semantic[-1]["possible_continuation_to_next_page"]
            and (
                (
                    kind in {"prose", "list_item"}
                    and not _starts_semantic_unit(blocks[0])
                )
                or (
                    kind == "table"
                    and _match_text(str(blocks[0].get("text", ""))).strip() == "续表"
                )
            )
        )
        height = _page_height(page, content_blocks)
        ends_mid_sentence = (
            (
                kind in {"prose", "list_item"}
                and bool(text)
                and not _ends_terminal(text)
            )
            or (
                index == len(groups)
                and kind == "table"
                and height is not None
                and float(bbox[3]) >= height * 0.80
            )
        )
        result.append({
            "semantic_id": f"{page['doc_id']}-p{page['pdf_page']:04d}-s{index:04d}",
            "block_id": block_id,
            "start_block_id": start_id,
            "end_block_id": end_id,
            "block_ids": [block["block_id"] for block in blocks],
            "bboxes": [block["bbox"] for block in blocks],
            "bbox": bbox,
            "text": text,
            "raw_text": raw_text,
            "kind": kind,
            "section_path": list(section_path),
            "column_id": blocks[0].get("column_id", 0),
            "order_status": order_status,
            "reading_order_index": index - 1,
            "layout_proposal_start": blocks[0]["block_id"] in group_starts,
            "source_fragments": [{"order": n, "block_id": block["block_id"],
                "pdf_page": page["pdf_page"],
                "candidate_id": page.get("extraction_candidate_id"),
                "char_start": 0, "char_end": len(block["text"]),
                "source_text_sha256": extraction_quality.digest_text(block["text"])}
                for n, block in enumerate(blocks)],
            "possible_continuation_from_previous_page": starts_mid_sentence,
            "possible_continuation_to_next_page": ends_mid_sentence,
            "ocr_used": any(bool(block.get("ocr_used")) for block in blocks),
        })
        result[-1]["quality_issues"] = evidence_source_issues({
            **result[-1], "source_text": text,
        })
    return result


def semantic_blocks_for_document(pages: list[dict], confirmed_pairs: set[tuple[str, str]] | None = None) -> list[dict]:
    """Join adjacent-page continuations and retain a locator for every raw block."""
    result = []
    previous_page = None
    for page in sorted(pages, key=lambda p: (p["doc_id"], p["pdf_page"])):
        adjacent = (previous_page is not None
                    and previous_page["doc_id"] == page["doc_id"]
                    and previous_page["pdf_page"] + 1 == page["pdf_page"])
        parts = semantic_blocks_for_page(page, previous_page if adjacent else None)
        for index, part in enumerate(parts):
            part = dict(part, doc_id=page["doc_id"], pdf_page=page["pdf_page"],
                        end_pdf_page=page["pdf_page"],
                        parse_version=page.get("parse_version", "legacy"),
                        quality_issue_ids=page.get("quality_issue_ids", []))
            part["source_locations"] = [
                {"pdf_page": page["pdf_page"], "block_id": bid, "bbox": box}
                for bid, box in zip(part["block_ids"], part["bboxes"])
            ]
            previous = result[-1] if result else None
            compatible = previous and (
                part["kind"] == previous["kind"]
                or (part["kind"] == "prose" and previous["kind"] == "list_item")
            )
            merge_review = layout.evaluate_cross_page_merge(previous, part, quality_settings()) if previous else None
            part["cross_page_merge"] = merge_review
            auto_merge = (page.get("parse_version") != extraction_quality.PARSE_VERSION
                          or bool(previous and (previous["semantic_id"], part["semantic_id"])
                                  in (confirmed_pairs or set())))
            if (auto_merge and adjacent and index == 0 and compatible
                    and (page.get("parse_version") != extraction_quality.PARSE_VERSION
                         or (merge_review and merge_review["decision"] == "proposed"))
                    and part["possible_continuation_from_previous_page"]
                    and previous["possible_continuation_to_next_page"]
                    and len(previous["text"]) + len(part["text"]) <= 12000):
                previous["text"] = (
                    previous["text"] + "\n" + part["text"]
                    if part["kind"] == "table" else
                    _prose_text([{"text": previous["text"]}, {"text": part["text"]}])
                )
                previous["raw_text"] += "\n" + part["raw_text"]
                previous["block_ids"] += part["block_ids"]
                previous["bboxes"] += part["bboxes"]
                previous["source_locations"] += part["source_locations"]
                offset = len(previous["source_fragments"])
                previous["source_fragments"] += [
                    {**fragment, "order": offset + position}
                    for position, fragment in enumerate(part.get("source_fragments", []))]
                previous["quality_issue_ids"] = sorted(set(previous.get("quality_issue_ids", []))
                                                        | set(part.get("quality_issue_ids", [])))
                previous["cross_page_merge"] = {"decision": "confirmed", "rule_version": layout.LAYOUT_VERSION}
                previous["end_block_id"] = part["end_block_id"]
                previous["end_pdf_page"] = page["pdf_page"]
                previous["block_id"] = (
                    previous["start_block_id"] + "--" + part["end_block_id"]
                )
                previous["possible_continuation_to_next_page"] = part["possible_continuation_to_next_page"]
                previous["ocr_used"] |= part["ocr_used"]
                previous["quality_issues"] = evidence_source_issues({
                    **previous, "source_text": previous["text"],
                })
            else:
                result.append(part)
        previous_page = page
    return result


def parse_job(job_id: str) -> dict:
    p = job_dir(job_id)
    if (p / "evidence/units.jsonl").exists():
        raise ValueError("已有证据单元，不能重新解析；请创建新任务")
    if (p / "parsed/pages.jsonl").exists():
        existing = read_json(p / "parsed/report.json")
        if existing:
            return existing
        raise ValueError("已有不完整解析结果；请显式迁移或复制任务，不能覆盖")
    meta = load_job(job_id)
    if not meta["documents"]:
        raise ValueError("请先上传 PDF")
    pages = []
    cfg = settings()
    ocr_languages = cfg.get("ocr_languages", "chi_sim+eng")
    ocr_dpi = int(cfg.get("ocr_dpi", 300))
    qcfg = quality_settings()
    extraction_cfg_hash = extraction_quality.config_hash({"ocr_languages": ocr_languages,
        "ocr_dpi": ocr_dpi, "ocr_tessdata": cfg.get("ocr_tessdata"), **qcfg})
    tessdata = Path(cfg.get("ocr_tessdata", "models/ocr/tessdata"))
    tessdata = str(tessdata if tessdata.is_absolute() else ROOT / tessdata)
    counts = {"pages": 0, "blocks": 0, "ocr_pages": 0, "ocr_blocks": 0,
              "possible_scanned_pages": 0, "successful_pages": 0,
              "failed_pages": 0, "needs_review_pages": 0,
              "parse_version": extraction_quality.PARSE_VERSION}
    candidates, issues, page_quality = [], [], []
    for entry in meta["documents"]:
        path = p / "raw_pdf" / entry["filename"]
        if sha256_file(path) != entry["sha256"]:
            raise ValueError(f"原始 PDF 哈希不匹配：{entry['filename']}")
        with fitz.open(path) as doc:
            previous_page = None
            for idx, page in enumerate(doc):
                native_error = None
                try:
                    native = extraction_quality.extract_native_page(page, entry["doc_id"], idx + 1)
                except Exception as exc:
                    native_error = f"原生文字提取失败：{exc}"
                    native = {"candidate_id": "native-failed", "method": "native",
                              "text": "", "blocks": [],
                              "text_sha256": extraction_quality.digest_text(""),
                              "engine": "PyMuPDF", "engine_version": getattr(fitz, "VersionBind", "unknown"),
                              "settings": {}, "quality": {}}
                native["candidate_id"] = f"{entry['doc_id']}-p{idx + 1:04d}-{native['candidate_id']}"
                native_quality = extraction_quality.assess_page_extraction(page, native, qcfg)
                decision = extraction_quality.decide_ocr(native_quality, qcfg)
                if native_error:
                    decision = {"attempt": True, "mode": "full_page", "reasons": [native_error]}
                ocr, ocr_errors = None, []
                if decision["attempt"]:
                    for attempt in range(max(1, int(qcfg.get("ocr_max_attempts", 2)))):
                        dpi = ocr_dpi if attempt == 0 else int(qcfg.get("ocr_retry_dpi", 400))
                        try:
                            ocr = extraction_quality.extract_ocr_page(
                                page, entry["doc_id"], idx + 1, ocr_languages,
                                dpi, decision["mode"], tessdata)
                            ocr["candidate_id"] = f"{entry['doc_id']}-p{idx + 1:04d}-{ocr['candidate_id']}"
                            extraction_quality.assess_page_extraction(page, ocr, qcfg)
                            break
                        except Exception as exc:
                            ocr_errors.append(f"OCR {dpi} DPI 失败：{exc}")
                comparison = extraction_quality.compare_extractions(native, ocr, qcfg) if ocr else None
                selection = extraction_quality.select_extraction(native, ocr, comparison, ocr_errors if not ocr else [])
                selected = next(c for c in (native, ocr) if c and c["candidate_id"] == selection["selected_candidate_id"])
                blocks = selected["blocks"]
                text_len = sum(len(b["text"].strip()) for b in blocks)
                ocr_used = bool(ocr and selected is ocr)
                scanned = text_len < qcfg.get("min_nonspace_chars", 30) and bool(page.get_image_info())
                page_issue_ids = []
                def add_page_issue(code: str, severity: str, message: str):
                    issue_id = f"QI-{entry['doc_id']}-p{idx + 1:04d}-{len(page_issue_ids) + 1:02d}"
                    page_issue_ids.append(issue_id)
                    issues.append({"issue_id": issue_id, "code": code, "severity": severity,
                        "doc_id": entry["doc_id"], "pdf_page": idx + 1,
                        "candidate_ids": [c["candidate_id"] for c in (native, ocr) if c],
                        "block_ids": [b["block_id"] for b in blocks],
                        "bboxes": [b["bbox"] for b in blocks], "message": message,
                        "status": "待处理", "resolved_by": None, "resolution_reason": None,
                        "parse_version": extraction_quality.PARSE_VERSION})
                if decision["attempt"] and not ocr:
                    add_page_issue("ocr_failed", "blocker", "；".join(ocr_errors))
                    counts["failed_pages"] += 1
                if native_error:
                    add_page_issue("native_failed", "blocker", native_error)
                if comparison and comparison["requires_source_review"]:
                    add_page_issue("native_ocr_conflict", "blocker", "原生文字与 OCR 存在差异或关键事实冲突")
                if decision["attempt"] and not page_issue_ids:
                    add_page_issue("ocr_source_review", "warning", "OCR 候选需对照 PDF 核实")
                parsed_page = {
                    "doc_id": entry["doc_id"], "filename": entry["filename"],
                    "pdf_page": idx + 1, "printed_page": None,
                    "width": round(float(page.rect.width), 2),
                    "height": round(float(page.rect.height), 2),
                    "blocks": blocks, "ocr_used": ocr_used, "possible_scanned": scanned,
                    "parse_version": extraction_quality.PARSE_VERSION,
                    "source_pdf_sha256": entry["sha256"],
                    "extraction_candidate_id": selected["candidate_id"],
                    "extraction_method": selected["method"],
                    "extraction_config_hash": extraction_cfg_hash,
                    "quality_issue_ids": page_issue_ids,
                    "extraction_selection": selection,
                    "quality": {**selected["quality"], "native_ocr_conflicts": comparison["conflicts"] if comparison else []},
                }
                parsed_page["semantic_blocks"] = semantic_blocks_for_page(parsed_page, previous_page)
                table_risks = [s["semantic_id"] for s in parsed_page["semantic_blocks"] if s["kind"] == "table"]
                order_risks = [s["semantic_id"] for s in parsed_page["semantic_blocks"] if s["order_status"] == "needs_review"]
                parsed_page["quality"]["table_risks"] = table_risks
                parsed_page["quality"]["reading_order_risks"] = order_risks
                if table_risks:
                    add_page_issue("table_structure", "warning", "检测到表格区域，行列及脚注需对照 PDF 核实")
                if order_risks:
                    add_page_issue("reading_order", "blocker", "阅读顺序存在歧义")
                parsed_page["quality_status"] = "needs_review" if page_issue_ids else "pass"
                pages.append(parsed_page)
                for candidate in (native, ocr):
                    if candidate:
                        candidates.append({"doc_id": entry["doc_id"], "pdf_page": idx + 1,
                                           "source_pdf_sha256": entry["sha256"], **candidate})
                page_quality.append({"doc_id": entry["doc_id"], "pdf_page": idx + 1,
                                     "quality_status": parsed_page["quality_status"],
                                     "quality_issue_ids": page_issue_ids, **parsed_page["quality"]})
                previous_page = parsed_page
                counts["pages"] += 1
                counts["blocks"] += len(blocks)
                counts["ocr_pages"] += int(ocr_used)
                counts["ocr_blocks"] += sum(b["ocr_used"] for b in blocks)
                counts["possible_scanned_pages"] += int(scanned)
                counts["successful_pages"] += int(not page_issue_ids)
                counts["needs_review_pages"] += int(bool(page_issue_ids))
    write_jsonl(p / "parsed/pages.jsonl", pages)
    write_jsonl(p / "parsed/extraction_candidates.jsonl", candidates)
    write_jsonl(p / "parsed/issues.jsonl", issues)
    write_jsonl(p / "parsed/page_quality.jsonl", page_quality)
    txt_report = build_semantic_txt(job_id)
    counts["semantic_units"] = txt_report["semantic_units"]
    counts["semantic_txt_chars"] = txt_report["chars"]
    write_json(p / "parsed/report.json", counts)
    meta["status"] = "parsed"
    write_json(p / "job.json", meta)
    return counts


def pages_for(job_id: str) -> list[dict]:
    return read_jsonl(job_dir(job_id) / "parsed/pages.jsonl")


def confirmed_merge_pairs(job_id: str) -> set[tuple[str, str]]:
    return {(row["start_semantic_id"], row["end_semantic_id"])
            for row in read_jsonl(job_dir(job_id) / "parsed/cross_page_decisions.jsonl")
            if row.get("decision") == "confirmed"}


def cross_page_merge_proposals(job_id: str) -> list[dict]:
    proposals = []
    confirmed = confirmed_merge_pairs(job_id)
    documents = load_job(job_id)["documents"]
    pages = pages_for(job_id)
    for document in documents:
        parts = semantic_blocks_for_document([p for p in pages if p["doc_id"] == document["doc_id"]])
        for previous, current in zip(parts, parts[1:]):
            if (current["pdf_page"] != previous["end_pdf_page"] + 1
                    or not previous.get("possible_continuation_to_next_page")
                    or not current.get("possible_continuation_from_previous_page")):
                continue
            decision = layout.evaluate_cross_page_merge(previous, current, quality_settings())
            if (previous["semantic_id"], current["semantic_id"]) in confirmed:
                decision = {**decision, "decision": "confirmed"}
            proposals.append({"doc_id": document["doc_id"], "filename": document["filename"],
                "start_semantic_id": previous["semantic_id"], "end_semantic_id": current["semantic_id"],
                "start_page": previous["pdf_page"], "end_page": current["pdf_page"],
                "start_text": previous["text"], "end_text": current["text"],
                **decision})
    return proposals


def evidence_for(job_id: str) -> list[dict]:
    return read_jsonl(job_dir(job_id) / "evidence/units.jsonl")


def current_evidence_units(job_id: str) -> list[dict]:
    """Read source records with live PDF, parsed-text and issue integrity flags."""
    root = job_dir(job_id)
    pages = {(p["doc_id"], p["pdf_page"]): p for p in pages_for(job_id)}
    issues = {i["issue_id"]: i for i in read_jsonl(root / "parsed/issues.jsonl")}
    documents = {d["doc_id"]: d for d in load_job(job_id)["documents"]}
    pdf_hashes = {}
    result = []
    for stored in evidence_for(job_id):
        unit = dict(stored)
        blockers = list(unit.get("unresolved_blockers", []))
        document = documents.get(unit.get("doc_id"))
        if document:
            if document["doc_id"] not in pdf_hashes:
                path = root / "raw_pdf" / document["filename"]
                pdf_hashes[document["doc_id"]] = sha256_file(path) if path.is_file() else None
            if (pdf_hashes[document["doc_id"]] != document["sha256"]
                    or unit.get("source_pdf_sha256") != document["sha256"]):
                blockers.append("PDF 文件身份已变化")
        else:
            blockers.append("来源文档不存在")
        if extraction_quality.digest_text(unit.get("source_text", "")) != unit.get("source_text_sha256"):
            blockers.append("证据文本哈希不匹配")
        for issue_id in unit.get("quality_issue_ids", []):
            if issues.get(issue_id, {}).get("status") not in {"已核实", "已排除"}:
                blockers.append(f"页面问题未解决：{issue_id}")
        for fragment in unit.get("source_fragments", []):
            page = pages.get((unit.get("doc_id"), fragment.get("pdf_page")))
            block = next((b for b in page.get("blocks", []) if b["block_id"] == fragment.get("block_id")), None) if page else None
            if (not page or not block
                    or page.get("parse_version") != unit.get("parse_version")
                    or page.get("extraction_candidate_id") != fragment.get("candidate_id")
                    or extraction_quality.digest_text(block["text"]) != fragment.get("source_text_sha256")):
                blockers.append("来源片段或解析版本已变化")
        unit["unresolved_blockers"] = list(dict.fromkeys(blockers))
        result.append(unit)
    return result


def _build_evidence_record(job_id: str, page: dict, source: dict, quote: str,
                           topic: str, clause_group: str, split: str, reviewer: str) -> dict:
    """Bind every evidence entry to immutable parsed blocks; never trust caller locators."""
    pages = {(p["doc_id"], p["pdf_page"]): p for p in pages_for(job_id)}
    locations = source.get("source_locations") or [
        {"pdf_page": page["pdf_page"], "block_id": bid, "bbox": box}
        for bid, box in zip(source["block_ids"], source["bboxes"])
    ]
    fragments, issue_ids = [], set()
    for position, loc in enumerate(locations):
        origin = pages.get((page["doc_id"], loc["pdf_page"]))
        block = next((b for b in origin["blocks"] if b["block_id"] == loc["block_id"]), None) if origin else None
        if not block or block["bbox"] != loc["bbox"]:
            raise ValueError("证据来源位置与解析原文不一致")
        issue_ids.update(origin.get("quality_issue_ids", []))
        fragments.append({"order": position, "pdf_page": loc["pdf_page"],
            "block_id": block["block_id"], "candidate_id": origin.get("extraction_candidate_id"),
            "source_text_sha256": extraction_quality.digest_text(block["text"]),
            "char_start": 0, "char_end": len(block["text"]),
            "bbox": block["bbox"]})
    text = source["source_text"]
    start = text.find(quote)
    if start < 0 or not quote:
        raise ValueError("引用必须是来源文本的精确子串")
    if text.find(quote, start + 1) >= 0:
        raise ValueError("引用在来源中重复，请缩小范围或人工消歧")
    if len(fragments) == 1 and text == pages[(page["doc_id"], page["pdf_page"])]["blocks"][
            next(i for i, b in enumerate(page["blocks"]) if b["block_id"] == fragments[0]["block_id"])]["text"]:
        fragments[0]["char_start"], fragments[0]["char_end"] = start, start + len(quote)
    ocr_used = any(pages[(page["doc_id"], loc["pdf_page"])]["blocks"][
        next(i for i, b in enumerate(pages[(page["doc_id"], loc["pdf_page"])]["blocks"])
             if b["block_id"] == loc["block_id"])].get("ocr_used") for loc in locations)
    risks = list(source.get("quality_issues", []))
    if ocr_used:
        risks.append("OCR 来源需对照 PDF 核实")
    return {"evidence_unit_id": f"EU-{uuid.uuid4().hex[:12]}",
        "evidence_schema_version": "evidence-v2", "parse_version": page.get("parse_version", "legacy"),
        "source_pdf_sha256": page.get("source_pdf_sha256"),
        "source_text_sha256": extraction_quality.digest_text(text),
        "source_fragments": fragments, "source_locations": locations,
        "quality_issue_ids": sorted(issue_ids), "unresolved_blockers": risks,
        "source_verification": {"status": "draft", "reviewer": None, "verified_at": None},
        "doc_id": page["doc_id"], "filename": page["filename"],
        "pdf_page": page["pdf_page"], "end_pdf_page": max(loc["pdf_page"] for loc in locations),
        "printed_page": page.get("printed_page"), "block_id": source["block_id"],
        "block_ids": source["block_ids"], "bbox": source["bbox"], "bboxes": source["bboxes"],
        "source_text": text, "raw_source_text": source.get("raw_source_text", text),
        "text_reconstructed": bool(source.get("text_reconstructed")),
        "quote": quote, "char_start": start, "char_end": start + len(quote),
        "topic": topic.strip(), "clause_group": clause_group.strip(), "split": split,
        "scope": "block_range" if len(fragments) > 1 else "block",
        "ocr_used": bool(ocr_used), "cross_page_merge": source.get("cross_page_merge"),
        "confirmed_by": reviewer,
        "confirmed_at": now(), "confirmation_mode": "automated" if reviewer.startswith("model:") else "human"}


def verify_evidence_source(job_id: str, evidence_unit_id: str, reviewer: str,
                           reason: str) -> dict:
    """Explicit human PDF verification; historical automated assessments never count."""
    if not reviewer.strip() or reviewer.startswith("model:") or not reason.strip():
        raise ValueError("请填写人工核对人和核对依据")
    p = job_dir(job_id)
    if (p / "evidence/splits.json").exists():
        raise ValueError("证据已冻结，请复制任务建立新版本")
    records = evidence_for(job_id)
    matches = [r for r in records if r["evidence_unit_id"] == evidence_unit_id]
    if len(matches) != 1:
        raise ValueError("证据不存在或不唯一")
    record = matches[0]
    if record.get("evidence_schema_version") != "evidence-v2":
        raise ValueError("旧证据需通过显式迁移建立新版本，不能直接标为来源已核实")
    document = next((d for d in load_job(job_id)["documents"] if d["doc_id"] == record["doc_id"]), None)
    if not document or sha256_file(p / "raw_pdf" / document["filename"]) != document["sha256"]:
        raise ValueError("原始 PDF 缺失或哈希不符")
    if (record.get("source_pdf_sha256") != document["sha256"]
            or extraction_quality.digest_text(record.get("source_text", "")) != record.get("source_text_sha256")):
        raise ValueError("证据来源文件或文字哈希不匹配")
    issues = {i["issue_id"]: i for i in read_jsonl(p / "parsed/issues.jsonl")}
    unresolved = [iid for iid in record.get("quality_issue_ids", [])
                  if issues.get(iid, {}).get("status") not in {"已排除", "已核实"}]
    if unresolved:
        raise ValueError("来源页面仍有未处理问题：" + ", ".join(unresolved))
    page_map = {(page["doc_id"], page["pdf_page"]): page for page in pages_for(job_id)}
    for fragment in record.get("source_fragments", []):
        origin = page_map.get((record["doc_id"], fragment["pdf_page"]))
        block = next((b for b in origin["blocks"] if b["block_id"] == fragment["block_id"]), None) if origin else None
        if (not block or origin.get("parse_version") != record.get("parse_version")
                or origin.get("extraction_candidate_id") != fragment.get("candidate_id")
                or extraction_quality.digest_text(block["text"]) != fragment["source_text_sha256"]):
            raise ValueError("来源文本版本已变化")
    record["unresolved_blockers"] = []
    record["source_verification"] = {"status": "source_verified", "reviewer": reviewer.strip(),
                                      "verified_at": now(), "reason": reason.strip(),
                                      "source_pdf_sha256": document["sha256"]}
    write_jsonl(p / "evidence/units.jsonl", records)
    append_jsonl(p / "evidence/verification_events.jsonl", {"evidence_unit_id": evidence_unit_id,
        "at": now(), "reviewer": reviewer.strip(), "reason": reason.strip(),
        "source_pdf_sha256": document["sha256"]})
    return record


def resolve_quality_issue(job_id: str, issue_id: str, status: str, reviewer: str,
                          reason: str) -> dict:
    if status not in {"已修正", "已排除", "已核实"} or not reviewer.strip() or not reason.strip():
        raise ValueError("必须填写有效处理状态、处理人和理由")
    p = job_dir(job_id)
    if (p / "evidence/splits.json").exists():
        raise ValueError("来源已冻结，请复制任务建立新版本")
    issues = read_jsonl(p / "parsed/issues.jsonl")
    matches = [i for i in issues if i["issue_id"] == issue_id]
    if len(matches) != 1:
        raise ValueError("问题不存在或不唯一")
    issue = matches[0]
    issue.update({"status": status, "resolved_by": reviewer.strip(),
                  "resolution_reason": reason.strip(), "resolved_at": now()})
    write_jsonl(p / "parsed/issues.jsonl", issues)
    append_jsonl(p / "parsed/issue_events.jsonl", dict(issue))
    return issue

def build_semantic_txt(job_id: str) -> dict:
    """Build an ordered, readable evidence-block corpus from every parsed PDF."""
    from aviation_ft import readable_txt
    p = job_dir(job_id)
    content, records, exclusions, report = readable_txt.build(job_id)
    target = p / "parsed/semantic_corpus.txt"
    fd, tmp = tempfile.mkstemp(prefix=".semantic-", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    report["sha256"] = sha256_file(target)
    report["pages_sha256"] = sha256_file(p / "parsed/pages.jsonl")
    issue_path = p / "parsed/issues.jsonl"
    report["issues_sha256"] = sha256_file(issue_path) if issue_path.is_file() else None
    decisions = p / "parsed/cross_page_decisions.jsonl"
    report["cross_page_decisions_sha256"] = sha256_file(decisions) if decisions.is_file() else None
    write_jsonl(p / "parsed/readable_units.jsonl", records)
    write_json(p / "parsed/txt_report.json", {**report, "exclusions": exclusions})
    return report


def txt_is_current(job_id: str) -> bool:
    root = job_dir(job_id)
    path = root / "parsed/semantic_corpus.txt"
    pages = root / "parsed/pages.jsonl"
    issues = root / "parsed/issues.jsonl"
    decisions = root / "parsed/cross_page_decisions.jsonl"
    report = read_json(root / "parsed/txt_report.json", {})
    return (path.is_file() and pages.is_file()
            and report.get("sha256") == sha256_file(path)
            and report.get("pages_sha256") == sha256_file(pages)
            and report.get("issues_sha256") == (sha256_file(issues) if issues.is_file() else None)
            and report.get("cross_page_decisions_sha256") == (sha256_file(decisions) if decisions.is_file() else None))


def add_evidence(job_id: str, block_id: str, topic: str, clause_group: str, split: str, quote: str | None = None) -> dict:
    p = job_dir(job_id)
    if (p / "evidence/splits.json").exists():
        raise ValueError("分区已冻结")
    if split not in SPLITS or not topic.strip() or not clause_group.strip():
        raise ValueError("必须填写主题、章节/相邻条款组及合法 split")
    found = [(page, b) for page in pages_for(job_id) for b in page["blocks"] if b["block_id"] == block_id]
    if len(found) != 1:
        raise ValueError("block_id 不存在或不唯一")
    page, block = found[0]
    text = block["text"]
    quote = (quote or text).strip()
    start = text.find(quote)
    if start < 0:
        raise ValueError("引用必须是原始 block 的精确子串")
    records = evidence_for(job_id)
    if any(r["block_id"] == block_id and r["quote"] == quote for r in records):
        raise ValueError("证据单元重复")
    source_record = {"block_id": block_id, "block_ids": [block_id], "bbox": block["bbox"],
                     "bboxes": [block["bbox"]], "source_text": text, "raw_source_text": text,
                     "ocr_used": block.get("ocr_used", False)}
    record = _build_evidence_record(job_id, page, source_record, quote, topic, clause_group, split, "manual:unknown")
    records.append(record)
    write_jsonl(p / "evidence/units.jsonl", records)
    return record


def page_block_range(page: dict, start_block_id: str, end_block_id: str) -> dict:
    blocks = page["blocks"]
    ids = [block["block_id"] for block in blocks]
    if start_block_id not in ids or end_block_id not in ids:
        raise ValueError("起止文本块不属于该页")
    start, end = ids.index(start_block_id), ids.index(end_block_id)
    if start >= end or end - start + 1 > 100:
        raise ValueError("请按页面顺序选择 2 至 100 个连续文本块")
    chosen = blocks[start:end + 1]
    kinds = {_block_kind(block) for block in chosen}
    raw_source_text = "\n".join(block["text"].rstrip("\n") for block in chosen).strip()
    source_text = raw_source_text if "table" in kinds else _prose_text(chosen)
    if len(source_text) > 12000:
        raise ValueError("合并证据超过 12000 字，请缩小文本块范围")
    bbox = [
        round(min(block["bbox"][0] for block in chosen), 2),
        round(min(block["bbox"][1] for block in chosen), 2),
        round(max(block["bbox"][2] for block in chosen), 2),
        round(max(block["bbox"][3] for block in chosen), 2),
    ]
    return {
        "block_id": f"{page['doc_id']}-p{page['pdf_page']:04d}-r{start + 1:04d}-{end + 1:04d}",
        "block_ids": [block["block_id"] for block in chosen],
        "bboxes": [block["bbox"] for block in chosen],
        "bbox": bbox,
        "source_text": source_text,
        "raw_source_text": raw_source_text,
        "text_reconstructed": source_text != raw_source_text,
        "ocr_used": any(block["ocr_used"] for block in chosen),
    }


def add_evidence_range(
    job_id: str, doc_id: str, pdf_page: int, start_block_id: str,
    end_block_id: str, topic: str, clause_group: str, split: str,
    reviewer: str, quote: str | None = None,
) -> dict:
    p = job_dir(job_id)
    if (p / "evidence/splits.json").exists():
        raise ValueError("分区已冻结")
    if split not in SPLITS or not topic.strip() or not clause_group.strip() or not reviewer.strip():
        raise ValueError("请填写主题、章节/相邻条款组、分区和核对人")
    matches = [
        page for page in pages_for(job_id)
        if page["doc_id"] == doc_id and page["pdf_page"] == pdf_page
    ]
    if len(matches) != 1:
        raise ValueError("文档页不存在或不唯一")
    page = matches[0]
    source = page_block_range(page, start_block_id, end_block_id)
    text = source["source_text"]
    quote = (quote or text).strip()
    start = text.find(quote)
    if start < 0:
        raise ValueError("引用必须是所选连续文本块的精确子串")
    records = evidence_for(job_id)
    if any(r["block_id"] == source["block_id"] and r["quote"] == quote for r in records):
        raise ValueError("证据单元重复")
    if any(
        r["doc_id"] == doc_id and r["clause_group"] == clause_group.strip() and r["split"] != split
        for r in records
    ):
        raise ValueError("同一章节/相邻条款组跨 split")
    record = _build_evidence_record(job_id, page, source, quote, topic, clause_group, split, reviewer.strip())
    records.append(record)
    write_jsonl(p / "evidence/units.jsonl", records)
    return record


def add_evidence_cross_page(job_id: str, start_semantic_id: str, end_semantic_id: str,
                            topic: str, clause_group: str, split: str,
                            reviewer: str, reason: str, quote: str | None = None) -> dict:
    """Human-confirm a proposed two-page continuation and save draft evidence."""
    root = job_dir(job_id)
    if (root / "evidence/splits.json").exists():
        raise ValueError("证据已冻结，请创建新任务")
    if not all(x.strip() for x in (topic, clause_group, reviewer, reason)) or split not in SPLITS:
        raise ValueError("需填写主题、章节组、分区、核对人及跨页依据")
    proposal = next((x for x in cross_page_merge_proposals(job_id)
                     if x["start_semantic_id"] == start_semantic_id and x["end_semantic_id"] == end_semantic_id), None)
    if not proposal or proposal["decision"] != "proposed":
        raise ValueError("跨页来源不是可人工确认的连续正文建议")
    doc_pages = [page for page in pages_for(job_id) if page["doc_id"] == proposal["doc_id"]]
    units = semantic_blocks_for_document(doc_pages)
    previous = next(unit for unit in units if unit["semantic_id"] == start_semantic_id)
    current = next(unit for unit in units if unit["semantic_id"] == end_semantic_id)
    if previous["kind"] != current["kind"] or previous["kind"] not in {"prose", "list_item"}:
        raise ValueError("当前仅支持人工确认连续正文；表格需核对列结构后另行处理")
    text = _prose_text([{"text": previous["text"]}, {"text": current["text"]}])
    quote = (quote or text).strip()
    if not quote or text.count(quote) != 1:
        raise ValueError("跨页引文必须是唯一精确子串")
    records = evidence_for(job_id)
    if any(r["doc_id"] == proposal["doc_id"] and r["clause_group"] == clause_group and r["split"] != split for r in records):
        raise ValueError("同一章节组跨 split")
    locations = previous["source_locations"] + current["source_locations"]
    source = {"block_id": start_semantic_id + "--" + end_semantic_id,
              "block_ids": previous["block_ids"] + current["block_ids"],
              "source_locations": locations,
              "bboxes": previous["bboxes"] + current["bboxes"],
              "bbox": previous["bbox"], "source_text": text,
              "raw_source_text": previous["raw_text"] + "\n" + current["raw_text"],
              "text_reconstructed": True, "ocr_used": False,
              "quality_issues": [],
              "cross_page_merge": {"decision": "confirmed", "rule_version": layout.LAYOUT_VERSION,
                                   "start_semantic_id": start_semantic_id,
                                   "end_semantic_id": end_semantic_id,
                                   "reviewer": reviewer.strip(), "reason": reason.strip(), "confirmed_at": now()}}
    if any(r["block_id"] == source["block_id"] and r["quote"] == quote for r in records):
        raise ValueError("证据单元重复")
    first_page = next(page for page in doc_pages if page["pdf_page"] == previous["pdf_page"])
    record = _build_evidence_record(job_id, first_page, source, quote,
                                    topic, clause_group, split, reviewer)
    records.append(record)
    write_jsonl(root / "evidence/units.jsonl", records)
    append_jsonl(root / "parsed/cross_page_decisions.jsonl", {**source["cross_page_merge"],
        "doc_id": proposal["doc_id"], "evidence_unit_id": record["evidence_unit_id"]})
    return record


def evidence_source_issues(source: dict) -> list[str]:
    """Return deterministic reasons a semantic source is unsafe to auto-approve."""
    issues = []
    if source.get("order_status") == "needs_review":
        issues.append("阅读顺序待人工核对")
    if source.get("quality_issue_ids"):
        issues.append("页面提取问题尚待核对")
    if source.get("possible_continuation_from_previous_page"):
        issues.append("语义段落从上一页延续，需合并后核实")
    if source.get("possible_continuation_to_next_page"):
        issues.append("语义段落在当前块后仍未结束，需合并后核实")
    text = str(source.get("source_text") or source.get("quote") or "").strip()
    if text.count(PDF_LEADER_DOT) >= 3:
        issues.append("文本包含目录引导符，不是正文证据")
    if (
        source.get("kind") in {"prose", "list_item"}
        and text
        and not _ends_terminal(text)
    ):
        issues.append("正文末尾缺少结束标点，可能是不完整句段")
    return issues


def _source_from_semantic_block(page: dict, semantic: dict) -> dict:
    # Re-slicing a raw range reintroduces excluded page furniture and may
    # classify continuation lines as a table. Preserve the canonical unit.
    return {
        **semantic,
        "source_text": semantic["text"],
        "raw_source_text": semantic["raw_text"],
        "text_reconstructed": semantic["text"] != semantic["raw_text"],
    }


def _semantic_page_sources(
    page: dict, max_chars: int = 4000, previous_page: dict | None = None,
) -> list[dict]:
    """Return one auditable source per complete clause or table fragment."""
    return [
        _source_from_semantic_block(page, semantic)
        for semantic in semantic_blocks_for_page(page, previous_page)
        if semantic["kind"] != "heading" and 40 <= len(semantic["text"].strip()) <= max_chars
    ]


def _suggested_page_source(
    page: dict, max_chars: int = 4000, previous_page: dict | None = None,
) -> dict | None:
    """Choose the strongest single clause when a caller explicitly needs one."""
    sources = _semantic_page_sources(page, max_chars, previous_page)
    if not sources:
        return None
    complete = [
        source for source in sources
        if not source["possible_continuation_from_previous_page"]
        and not source["possible_continuation_to_next_page"]
    ]
    return max(complete or sources, key=lambda source: len(source["source_text"].strip()))


def evidence_suggestions(job_id: str, doc_id: str, limit: int = 20) -> list[dict]:
    """Suggest individual clauses/tables while spreading the limited set across the document."""
    if not 2 <= limit <= 200:
        raise ValueError("候选数量应在 2 到 200 之间")
    document_pages = [page for page in pages_for(job_id) if page["doc_id"] == doc_id]
    if not document_pages:
        raise ValueError("文档不存在或尚未解析")
    existing_sources = {
        (item["doc_id"], tuple(item.get("block_ids") or [item["block_id"]]))
        for item in evidence_for(job_id)
    }
    page_lookup = {page["pdf_page"]: page for page in document_pages}
    all_candidates = []
    for semantic in semantic_blocks_for_document(document_pages, confirmed_merge_pairs(job_id)):
        if semantic["kind"] == "heading" or not 40 <= len(semantic["text"]) <= 4000:
            continue
        page = page_lookup[semantic["pdf_page"]]
        if page.get("quality_issue_ids"):
            semantic["quality_issues"] = list(dict.fromkeys(
                semantic.get("quality_issues", []) + ["来源页有待处理提取问题"]))
        all_candidates.append((page, _source_from_semantic_block(page, semantic)))
    def partition_group(page: dict, source: dict) -> str:
        section = source.get("section_path") or []
        if section:
            label = re.sub(r"\s+", " ", section[-1]).strip()
            suffix = extraction_quality.digest_text(label)[:8]
            return f"section-{suffix}-{label[:28]}"
        band = (page["pdf_page"] - 1) // 5
        return f"pages-{band * 5 + 1:04d}-{band * 5 + 5:04d}"
    all_groups = list(dict.fromkeys(partition_group(page, source) for page, source in all_candidates))
    candidates = [
        (page, source) for page, source in all_candidates
        if (page["doc_id"], tuple(source["block_ids"])) not in existing_sources
    ]
    if len(candidates) > limit:
        positions = [round(i * (len(candidates) - 1) / (limit - 1)) for i in range(limit)]
        candidates = [candidates[i] for i in positions]
    split_by_group = {
        group: ("train" if i < len(all_groups) * 0.6 else "validation" if i < len(all_groups) * 0.8 else "test")
        for i, group in enumerate(all_groups)
    }
    result = []
    for page, source in candidates:
        group = partition_group(page, source)
        result.append({
            "selected": False,
            "pdf_page": page["pdf_page"],
            "end_pdf_page": source["end_pdf_page"],
            "source_locations": source["source_locations"],
            "block_id": source["block_id"],
            "start_block_id": source["start_block_id"],
            "end_block_id": source["end_block_id"],
            "blocks": len(source["block_ids"]),
            "ocr_used": source["ocr_used"],
            "semantic_id": source["semantic_id"],
            "kind": source["kind"],
            "possible_continuation_from_previous_page": source["possible_continuation_from_previous_page"],
            "possible_continuation_to_next_page": source["possible_continuation_to_next_page"],
            "quality_issues": source["quality_issues"],
            "quote": source["source_text"].strip(),
            "topic": Path(page["filename"]).stem[:50],
            "clause_group": group,
            "split": split_by_group[group],
        })
    return result


def add_evidence_batch(job_id: str, items: list[dict], reviewer: str) -> list[dict]:
    """Validate a batch in memory and write it atomically."""
    p = job_dir(job_id)
    if (p / "evidence/splits.json").exists():
        raise ValueError("分区已冻结")
    if not reviewer.strip():
        raise ValueError("请填写批量核对人")
    if not items or len(items) > 200:
        raise ValueError("每批应选择 1 至 200 条证据")
    block_lookup = {
        block["block_id"]: (page, block)
        for page in pages_for(job_id)
        for block in page["blocks"]
    }
    records = evidence_for(job_id)
    seen_quotes = {(r["block_id"], r["quote"]) for r in records}
    group_splits = {(r["doc_id"], r["clause_group"]): r["split"] for r in records}
    additions = []
    for item in items:
        block_id = item.get("block_id")
        start_id = item.get("start_block_id") or block_id
        end_id = item.get("end_block_id") or block_id
        if start_id not in block_lookup or end_id not in block_lookup:
            raise ValueError(f"文本块不存在：{start_id} / {end_id}")
        page, block = block_lookup[start_id]
        end_page = block_lookup[end_id][0]
        if end_page is not page:
            semantic = next((
                unit for unit in semantic_blocks_for_document([
                    p for p in pages_for(job_id) if p["doc_id"] == page["doc_id"]
                ], confirmed_merge_pairs(job_id))
                if unit["start_block_id"] == start_id and unit["end_block_id"] == end_id
            ), None)
            if semantic is None or semantic["quality_issues"]:
                raise ValueError("跨页范围不是完整的同一条款，请核对原文")
            source = _source_from_semantic_block(page, semantic)
        elif start_id != end_id:
            semantic = next((
                unit for unit in semantic_blocks_for_page(page)
                if unit["start_block_id"] == start_id and unit["end_block_id"] == end_id
            ), None)
            source = (_source_from_semantic_block(page, semantic) if semantic
                      else page_block_range(page, start_id, end_id))
        else:
            semantic = next((
                semantic for semantic in semantic_blocks_for_page(page)
                if semantic["start_block_id"] == start_id and semantic["end_block_id"] == end_id
            ), None)
            source_text = semantic["text"] if semantic else block["text"]
            raw_source_text = semantic["raw_text"] if semantic else block["text"]
            source = {
                "block_id": block["block_id"], "block_ids": [block["block_id"]],
                "bbox": block["bbox"], "bboxes": [block["bbox"]],
                "source_text": source_text, "raw_source_text": raw_source_text,
                "text_reconstructed": source_text != raw_source_text,
                "ocr_used": bool(block["ocr_used"]),
            }
        if block_id != source["block_id"] or item.get("pdf_page", page["pdf_page"]) != page["pdf_page"]:
            raise ValueError("证据块 ID 或页码与原文不一致")
        topic = str(item.get("topic", "")).strip()
        group = str(item.get("clause_group", "")).strip()
        split = item.get("split")
        if not topic or not group or split not in SPLITS:
            raise ValueError(f"{block_id}：必须填写主题、页段/章节组和合法分区")
        quote = str(item.get("quote", "")).strip()
        text = source["source_text"]
        start = text.find(quote) if quote else -1
        if start < 0:
            raise ValueError(f"{block_id}：引用必须是该文本块的精确子串")
        key = (block_id, quote)
        if key in seen_quotes:
            raise ValueError(f"{block_id}：证据单元重复")
        seen_quotes.add(key)
        group_key = (page["doc_id"], group)
        if group_key in group_splits and group_splits[group_key] != split:
            raise ValueError(f"{block_id}：同一页段/章节组跨 split")
        group_splits[group_key] = split
        additions.append(_build_evidence_record(job_id, page, source, quote, topic, group, split, reviewer.strip()))
    write_jsonl(p / "evidence/units.jsonl", records + additions)
    return additions


def freeze_splits(job_id: str) -> dict:
    p = job_dir(job_id)
    if (p / "evidence/splits.json").exists():
        frozen = read_json(p / "evidence/splits.json")
        if (not frozen or not (p / "evidence/units.jsonl").is_file()
                or frozen.get("evidence_sha256") != sha256_file(p / "evidence/units.jsonl")
                or any(not review_policy.source_verified(unit) for unit in current_evidence_units(job_id))):
            raise ValueError("冻结证据与当前来源版本不一致，请显式迁移或建立新任务")
        return frozen
    evidence = current_evidence_units(job_id)
    if not evidence or {e["split"] for e in evidence} != SPLITS:
        raise ValueError("冻结前 train、validation、test 都必须有证据")
    pending = [e["evidence_unit_id"] for e in evidence if not review_policy.source_verified(e)]
    if pending:
        raise ValueError(f"仍有 {len(pending)} 条证据未对照 PDF 核实，不能冻结")
    groups = {}
    for e in evidence:
        key = (e["doc_id"], e["clause_group"])
        if key in groups and groups[key] != e["split"]:
            raise ValueError(f"同一章节/相邻条款组跨 split：{key}")
        groups[key] = e["split"]
    counts = {split: sum(e["split"] == split for e in evidence) for split in sorted(SPLITS)}
    manifest = {"frozen_at": now(), "evidence_sha256": sha256_file(p / "evidence/units.jsonl"),
                "parse_version": extraction_quality.PARSE_VERSION,
                "counts": counts, "groups": len(groups)}
    write_json(p / "evidence/splits.json", manifest)
    return manifest


def candidates_for(job_id: str) -> list[dict]:
    return read_jsonl(job_dir(job_id) / "candidates/records.jsonl")


def add_candidate(job_id: str, question: str, answer: str, evidence_ids: list[str], answerability: str, claim: str = "", origin: str = "manual", generator_model: str | None = None, prompt_version: str | None = None, generation_key: str | None = None, *, claims: list[dict] | None = None, schema_version: str | None = None) -> dict:
    p = job_dir(job_id)
    if not (p / "evidence/splits.json").exists():
        raise ValueError("必须先冻结证据分区")
    units = {e["evidence_unit_id"]: e for e in evidence_for(job_id)}
    if not evidence_ids or any(eid not in units for eid in evidence_ids):
        raise ValueError("至少选择一个存在的证据单元")
    splits = {units[eid]["split"] for eid in evidence_ids}
    if len(splits) != 1:
        raise ValueError("不能跨 split 组合证据")
    if answerability not in {"answerable", "unanswerable"}:
        raise ValueError("无效的可回答状态")
    refs = []
    for idx, eid in enumerate(evidence_ids, 1):
        e = units[eid]
        refs.append({"evidence_id": f"E{idx}", "evidence_unit_id": eid, "doc_id": e["doc_id"], "pdf_page": e["pdf_page"], "block_id": e["block_id"], "quote": e["quote"], "char_start": e["char_start"], "char_end": e["char_end"]})
    records = candidates_for(job_id)
    if generation_key and any(r.get("generation_key") == generation_key for r in records):
        raise ValueError("同一证据、提示词和变体的候选已存在")
    version = schema_version or ("qa-v2" if claims is not None else "qa-v1")
    if version not in {"qa-v1", "qa-v2"}:
        raise ValueError("未知问答结构版本")
    claim_records = claims if claims is not None else ([{"claim_id": "C1", "text": claim.strip(),
        "support_type": "direct" if len(refs) == 1 else "multi_evidence_derived",
        "evidence_ids": [r["evidence_id"] for r in refs]}] if answerability == "answerable" else [])
    if version == "qa-v2":
        structure_errors = claim_validation.validate_claim_structure(
            claim_records, answerability, refs,
            review_policy.config(ROOT).get("max_claims_per_answer", 8))
        if structure_errors:
            raise ValueError("；".join(structure_errors))
    coverage = (claim_validation.check_answer_coverage(answer, claim_records, answerability)
                if version == "qa-v2" else {"status": "not_checked", "reason": "旧单断言记录待迁移"})
    numeric = (numeric_validation.compare_numeric_facts(answer, "\n".join(ref["quote"] for ref in refs))
               if version == "qa-v2" else {"status": "not_checked"})
    record = {"sample_id": f"SFT-{uuid.uuid4().hex[:12]}", "split": next(iter(splits)),
        "task_type": "grounded_qa" if answerability == "answerable" else "unanswerable",
        "candidate_schema_version": version, "record_revision": 1,
        "question": question.strip(), "answerability": answerability, "answer": answer.strip(),
        "claims": claim_records, "coverage_check": coverage, "numeric_checks": numeric,
        "judge_assessments": [], "evidence": refs, "origin": origin,
        "generator_model": generator_model, "prompt_version": prompt_version,
        "generation_key": generation_key,
        "review": {"status": "pending", "reviewer_id": None, "comment": ""}, "created_at": now()}
    record["claim_spans"] = claim_validation.resolve_claim_spans(answer, claim_records, refs)[0] if version == "qa-v2" else []
    errors = validate_candidate(record, units)
    if errors:
        raise ValueError("；".join(errors))
    records.append(record)
    write_jsonl(p / "candidates/records.jsonl", records)
    return record


def validate_candidate(record: dict, units: dict[str, dict]) -> list[str]:
    errors = []
    if not record.get("question") or not record.get("answer"):
        errors.append("问题和答案不能为空")
    refs = record.get("evidence", [])
    if not refs:
        errors.append("缺少证据")
    seen = set()
    for ref in refs:
        rid = ref.get("evidence_id")
        unit = units.get(ref.get("evidence_unit_id"))
        if not unit:
            errors.append("引用的证据单元不存在")
            continue
        if rid in seen:
            errors.append("证据编号重复")
        seen.add(rid)
        for field in ("doc_id", "pdf_page", "block_id"):
            if ref.get(field) != unit[field]:
                errors.append(f"引用 {field} 与原文不一致")
        start, end = ref.get("char_start"), ref.get("char_end")
        if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end <= start or end > len(unit["source_text"]) or unit["source_text"][start:end] != ref.get("quote") or ref.get("quote") != unit["quote"]:
            errors.append("引用范围不能还原原文")
        if record.get("split") != unit["split"]:
            errors.append("引用跨 split")
    markers = set(re.findall(r"\[E\d+\]", record.get("answer", "")))
    allowed = {f"[{rid}]" for rid in seen}
    if markers - allowed:
        errors.append("答案引用了不存在的证据编号")
    if record.get("answerability") == "answerable":
        if not isinstance(record.get("claims"), list) or not record.get("claims"):
            errors.append("有证据答案缺少 claim")
        for claim in record.get("claims", []) if isinstance(record.get("claims"), list) else []:
            if not isinstance(claim, dict):
                errors.append("claim 格式无效")
                continue
            if not claim.get("text") or claim.get("support_type") == "unsupported" or not claim.get("evidence_ids"):
                errors.append("claim 内容或证据映射无效")
            if set(claim.get("evidence_ids", [])) - seen:
                errors.append("claim 指向不存在的证据编号")
            if not {f"[{x}]" for x in claim.get("evidence_ids", [])} <= markers:
                errors.append("claim 引用未在答案中出现")
    elif record.get("answerability") == "unanswerable":
        if record.get("claims") and record.get("candidate_schema_version") != "qa-v2":
            errors.append("不可回答样本不能含正式 claim")
        if record.get("candidate_schema_version") == "qa-v2":
            for claim in record.get("claims", []) if isinstance(record.get("claims"), list) else []:
                if isinstance(claim, dict) and not {f"[{x}]" for x in claim.get("evidence_ids", [])} <= markers:
                    errors.append("拒答中的事实断言引用未在答案中出现")
        if "不足" not in record.get("answer", "") and "无法确认" not in record.get("answer", ""):
            errors.append("拒答答案必须说明证据不足或无法确认")
    else:
        errors.append("无效 answerability")
    if record.get("candidate_schema_version") == "qa-v2":
        structure_errors = claim_validation.validate_claim_structure(
            record.get("claims", []), record.get("answerability"), refs,
            review_policy.config(ROOT).get("max_claims_per_answer", 8))
        errors.extend(structure_errors)
        if not structure_errors:
            resolved_spans, span_errors = claim_validation.resolve_claim_spans(
                record.get("answer", ""), record.get("claims", []), refs)
            errors.extend(span_errors)
            if record.get("claim_spans") != resolved_spans:
                errors.append("断言位置与答案或证据版本不一致")
    return list(dict.fromkeys(errors))


def candidate_content_sha256(record: dict) -> str:
    fields = {key: record.get(key) for key in ("sample_id", "record_revision", "question",
        "answer", "answerability", "claims", "claim_spans", "evidence", "coverage_check", "numeric_checks")}
    return extraction_quality.digest_text(json.dumps(fields, ensure_ascii=False, sort_keys=True))


def candidate_source_sha256(record: dict, units: dict[str, dict]) -> str:
    fields = [{"evidence_unit_id": ref["evidence_unit_id"],
               "source_text_sha256": units[ref["evidence_unit_id"]].get("source_text_sha256"),
               "source_pdf_sha256": units[ref["evidence_unit_id"]].get("source_pdf_sha256"),
               "parse_version": units[ref["evidence_unit_id"]].get("parse_version"),
               "source_fragments": units[ref["evidence_unit_id"]].get("source_fragments"),
               "source_locations": units[ref["evidence_unit_id"]].get("source_locations"),
               "quality_issue_ids": units[ref["evidence_unit_id"]].get("quality_issue_ids"),
               "source_verification": units[ref["evidence_unit_id"]].get("source_verification")}
              for ref in record["evidence"] if ref["evidence_unit_id"] in units]
    return extraction_quality.digest_text(json.dumps(fields, ensure_ascii=False, sort_keys=True))


def edit_candidate(job_id: str, sample_id: str, revised: dict, editor: str) -> dict:
    if not editor.strip() or revised.get("sample_id") != sample_id:
        raise ValueError("编辑人不能为空，且样本 ID 不可修改")
    p = job_dir(job_id)
    records = candidates_for(job_id)
    matches = [i for i, r in enumerate(records) if r["sample_id"] == sample_id]
    if len(matches) != 1:
        raise ValueError("样本不存在或 ID 不唯一")
    idx = matches[0]
    old = records[idx]
    if revised.get("record_revision", old.get("record_revision", 1)) != old.get("record_revision", 1):
        raise RevisionConflict("候选版本已变化，请刷新后重新编辑")
    protected = ("split", "origin", "generator_model", "prompt_version", "generation_key")
    if any(revised.get(field) != old.get(field) for field in protected):
        raise ValueError("split、候选来源和生成溯源字段不可通过编辑修改")
    units = {e["evidence_unit_id"]: e for e in evidence_for(job_id)}
    if revised.get("candidate_schema_version") == "qa-v2":
        structure_errors = claim_validation.validate_claim_structure(
            revised.get("claims", []), revised.get("answerability"),
            revised.get("evidence", []),
            review_policy.config(ROOT).get("max_claims_per_answer", 8))
        if structure_errors:
            raise ValueError("编辑后仍有错误：" + "；".join(structure_errors))
        revised["claim_spans"] = claim_validation.resolve_claim_spans(
            revised.get("answer", ""), revised.get("claims", []), revised.get("evidence", []))[0]
    errors = validate_candidate(revised, units)
    if errors:
        raise ValueError("编辑后仍有错误：" + "；".join(errors))
    revised["record_revision"] = old.get("record_revision", 1) + 1
    if revised.get("candidate_schema_version") == "qa-v2":
        revised["claim_spans"] = claim_validation.resolve_claim_spans(
            revised["answer"], revised["claims"], revised["evidence"])[0]
        revised["coverage_check"] = claim_validation.check_answer_coverage(
            revised["answer"], revised["claims"], revised["answerability"])
        revised["numeric_checks"] = numeric_validation.compare_numeric_facts(
            revised["answer"], "\n".join(ref["quote"] for ref in revised["evidence"]))
    else:
        revised["claim_spans"] = []
        revised["coverage_check"] = {"status": "not_checked"}
        revised["numeric_checks"] = {"status": "not_checked"}
    revised["judge_assessments"] = []
    revised["review"] = {"status": "pending", "reviewer_id": None, "comment": ""}
    revised["edited_at"] = now()
    append_jsonl(p / "reviewed/events.jsonl", {"sample_id": sample_id, "action": "edit", "editor": editor.strip(), "before": old, "after": revised, "at": now()})
    records[idx] = revised
    write_jsonl(p / "candidates/records.jsonl", records)
    return revised


def review_candidate(job_id: str, sample_id: str, decision: str, reviewer: str, comment: str = "", auto_assessment: dict | None = None, *, expected_revision: int | None = None, claim_reviews: list[dict] | None = None, source_reviews: list[dict] | None = None, numeric_review: dict | None = None) -> dict:
    if decision not in DECISIONS or not reviewer.strip():
        raise ValueError("请选择审核结论并填写审核人")
    p = job_dir(job_id)
    records = candidates_for(job_id)
    matches = [r for r in records if r["sample_id"] == sample_id]
    if len(matches) != 1:
        raise ValueError("样本不存在或 ID 不唯一")
    record = matches[0]
    if type(expected_revision) is not int or expected_revision != record.get("record_revision", 1):
        raise RevisionConflict("候选版本已变化，请刷新审核详情")
    units = {e["evidence_unit_id"]: e for e in current_evidence_units(job_id)}
    if decision in {"approved", "approved_after_edit"}:
        errors = validate_candidate(record, units)
        if errors:
            raise ValueError("审核未通过：" + "；".join(errors))
        gate = review_policy.evaluate_approval(record, units, auto_assessment,
            review_policy.config(ROOT, job_id), automated=auto_assessment is not None,
            claim_reviews=claim_reviews, source_reviews=source_reviews,
            numeric_review=numeric_review)
        if not gate["approved"]:
            raise ValueError("审核未通过：" + "；".join(gate["reasons"]))
    before = record["review"].copy()
    record["review"] = {"status": decision, "reviewer_id": reviewer.strip(),
        "reviewer": reviewer.strip(), "comment": comment.strip(), "reviewed_at": now(),
        "reviewed_record_sha256": candidate_content_sha256(record),
        "reviewed_source_sha256": candidate_source_sha256(record, units),
        "review_policy_version": review_policy.POLICY_VERSION,
        "claim_reviews": claim_reviews or [], "source_reviews": source_reviews or [],
        "numeric_review": numeric_review}
    if auto_assessment is not None:
        if not reviewer.startswith("model:"):
            raise ValueError("自动审核记录必须注明模型")
        record["review"]["review_mode"] = "automated"
        record["review"]["assessment"] = auto_assessment
        record.setdefault("judge_assessments", []).append(auto_assessment)
    append_jsonl(p / "reviewed/events.jsonl", {"sample_id": sample_id, "before": before, "after": record["review"], "at": now()})
    write_jsonl(p / "candidates/records.jsonl", records)
    return record


def review_candidates_batch(
    job_id: str, sample_ids: list[str], reviewer: str,
    decision: str = "approved", comment: str = "", *,
    expected_revisions: dict[str, int] | None = None,
    claim_reviews: dict[str, list[dict]] | None = None,
    source_reviews: dict[str, list[dict]] | None = None,
    numeric_reviews: dict[str, dict] | None = None,
) -> dict:
    if decision not in {"approved", "rejected", "pending_expert_review"} or not reviewer.strip():
        raise ValueError("请选择批量审核结论并填写审核人")
    if not sample_ids or len(sample_ids) > 500 or len(set(sample_ids)) != len(sample_ids):
        raise ValueError("请选择 1 至 500 条不重复的候选")
    p = job_dir(job_id)
    records = candidates_for(job_id)
    by_id = {record["sample_id"]: record for record in records}
    if len(by_id) != len(records) or any(sample_id not in by_id for sample_id in sample_ids):
        raise ValueError("候选 ID 不存在或不唯一")
    if not isinstance(expected_revisions, dict):
        raise RevisionConflict("批量审核缺少候选版本，请刷新列表")
    units = {item["evidence_unit_id"]: item for item in current_evidence_units(job_id)}
    for sample_id in sample_ids:
        record = by_id[sample_id]
        if type(expected_revisions.get(sample_id)) is not int or expected_revisions[sample_id] != record.get("record_revision", 1):
            raise RevisionConflict(f"{sample_id}：候选版本已变化")
        if record["review"]["status"] not in {"pending", "pending_expert_review"}:
            raise ValueError(f"{sample_id}：只能批量审核待审或待专家核对候选")
        if decision == "approved":
            errors = validate_candidate(record, units)
            if errors:
                raise ValueError(f"{sample_id}：" + "；".join(errors))
            gate = review_policy.evaluate_approval(record, units, None,
                review_policy.config(ROOT, job_id), automated=False,
                claim_reviews=(claim_reviews or {}).get(sample_id),
                source_reviews=(source_reviews or {}).get(sample_id),
                numeric_review=(numeric_reviews or {}).get(sample_id))
            if not gate["approved"]:
                raise ValueError(f"{sample_id}：" + "；".join(gate["reasons"]))
    batch_id = f"BR-{uuid.uuid4().hex[:12]}"
    timestamp = now()
    events = read_jsonl(p / "reviewed/events.jsonl")
    for sample_id in sample_ids:
        record = by_id[sample_id]
        before = record["review"].copy()
        record["review"] = {
            "status": decision, "reviewer_id": reviewer.strip(),
            "comment": comment.strip(), "reviewed_at": timestamp,
            "review_mode": "batch", "batch_id": batch_id,
            "reviewed_record_sha256": candidate_content_sha256(record),
            "reviewed_source_sha256": candidate_source_sha256(record, units),
            "review_policy_version": review_policy.POLICY_VERSION,
            "claim_reviews": (claim_reviews or {}).get(sample_id, []),
            "source_reviews": (source_reviews or {}).get(sample_id, []),
            "numeric_review": (numeric_reviews or {}).get(sample_id),
        }
        events.append({
            "sample_id": sample_id, "before": before,
            "after": record["review"], "at": timestamp, "batch_id": batch_id,
        })
    write_jsonl(p / "candidates/records.jsonl", records)
    write_jsonl(p / "reviewed/events.jsonl", events)
    return {"batch_id": batch_id, "updated": len(sample_ids), "decision": decision}


def quality_report(job_id: str) -> dict:
    meta = load_job(job_id)
    evidence = current_evidence_units(job_id)
    records = candidates_for(job_id)
    approved = [r for r in records if r["review"]["status"] in {"approved", "approved_after_edit"}]
    document_count = len(meta["documents"])
    evidence_target = 20 * document_count
    approved_target = 90 * document_count
    holdout_evidence_target = 4 * document_count
    issues = []
    source_issues = []
    semantic_issues = []
    for issue in read_jsonl(job_dir(job_id) / "parsed/issues.jsonl"):
        if (issue.get("severity") in {"warning", "blocker"}
                and issue.get("status") not in {"已核实", "已排除"}):
            source_issues.append(f"{issue['issue_id']} PDF 第 {issue['pdf_page']} 页提取问题尚未解决")
    if not document_count:
        issues.append("请至少上传一份 PDF")
    if pages_for(job_id) and not txt_is_current(job_id):
        issues.append("整理 TXT 与当前解析或问题版本不一致")
    if len(evidence) < evidence_target:
        issues.append(f"可追溯证据单元不足 {evidence_target}")
    for document in meta["documents"]:
        count = sum(e["doc_id"] == document["doc_id"] for e in evidence)
        if count < 20:
            issues.append(f"{document['filename']} 的证据单元不足 20")
    frozen = read_json(job_dir(job_id) / "evidence/splits.json")
    if not frozen:
        issues.append("证据分区未冻结")
    elif sha256_file(job_dir(job_id) / "evidence/units.jsonl") != frozen["evidence_sha256"]:
        issues.append("冻结后证据内容发生变化")
    if len({e["evidence_unit_id"] for e in evidence}) != len(evidence):
        issues.append("证据单元 ID 不唯一")
    for unit in evidence:
        if not review_policy.source_verified(unit):
            details = "；".join(unit.get("unresolved_blockers", [])) or "来源尚未对照 PDF 核实"
            source_issues.append(f"{unit['evidence_unit_id']} {details}")
    if len({r["sample_id"] for r in records}) != len(records):
        issues.append("样本 ID 不唯一")
    if len(approved) < approved_target:
        issues.append(f"已审核通过的回答微调记录不足 {approved_target}")
    split_counts = {split: sum(r["split"] == split for r in approved) for split in sorted(SPLITS)}
    if any(count == 0 for count in split_counts.values()):
        issues.append("train/validation/test 有空分区")
    evidence_split_counts = {split: sum(e["split"] == split for e in evidence) for split in sorted(SPLITS)}
    if evidence and any(evidence_split_counts[split] < holdout_evidence_target for split in ("validation", "test")):
        issues.append(f"validation/test 证据单元各不足 {holdout_evidence_target} 个")
    test_model_records = [r for r in records if r["split"] == "test" and r["origin"] == "model"]
    if test_model_records:
        recipe = read_json(job_dir(job_id) / "candidates/generation_recipe.json")
        generation_config = read_json(ROOT / "configs/generation.json", {})
        from aviation_ft import providers
        generation_config = providers.effective_config(job_id, generation_config)
        if not recipe:
            issues.append("测试集模型候选缺少冻结的生成配方")
        else:
            fingerprint = recipe["fingerprint"]
            expected_recipe = {key: generation_config.get(key) for key in (
                "model_id", "model_revision", "prompt_version", "temperature", "max_tokens"
            )}
            expected_recipe["candidate_schema_version"] = "qa-v2"
            prompt_path = ROOT / generation_config.get("prompt_file", "")
            expected_recipe["prompt_sha256"] = sha256_file(prompt_path) if prompt_path.is_file() else None
            if fingerprint != expected_recipe:
                issues.append("当前生成配方与测试集冻结配方不一致")
    units = {e["evidence_unit_id"]: e for e in evidence}
    active_review_policy = review_policy.config(ROOT, job_id)
    for record in approved:
        for err in validate_candidate(record, units):
            issues.append(f"{record['sample_id']}: {err}")
        review = record.get("review", {})
        gate = review_policy.evaluate_approval(record, units, review.get("assessment"),
            active_review_policy,
            automated=review.get("review_mode") == "automated",
            claim_reviews=review.get("claim_reviews"),
            source_reviews=review.get("source_reviews"),
            numeric_review=review.get("numeric_review"))
        semantic_issues.extend(f"{record['sample_id']} {reason}" for reason in gate["reasons"])
        if (review.get("reviewed_record_sha256") != candidate_content_sha256(record)
                or review.get("reviewed_source_sha256") != candidate_source_sha256(record, units)):
            semantic_issues.append(f"{record['sample_id']} 审核记录与当前内容或来源版本不一致")
    approved_by_document = {}
    approved_evidence_units_by_document = {}
    for document in meta["documents"]:
        doc_id = document["doc_id"]
        doc_units = {e["evidence_unit_id"] for e in evidence if e["doc_id"] == doc_id}
        used_units = {
            ref["evidence_unit_id"] for record in approved for ref in record["evidence"]
            if ref["evidence_unit_id"] in doc_units
        }
        approved_evidence_units_by_document[document["filename"]] = len(used_units)
        approved_by_document[document["filename"]] = sum(
            any(ref["evidence_unit_id"] in doc_units for ref in record["evidence"])
            for record in approved
        )
        if approved_by_document[document["filename"]] < 30:
            issues.append(f"{document['filename']} 获批问答不足 30 条")
        if len(used_units) < 10:
            issues.append(f"{document['filename']} 被获批问答实际引用的不同证据不足 10 条")

    def cross_split_duplicates(items):
        seen = {}
        conflicts = set()
        for split, value in items:
            normalized = re.sub(r"\s+", "", value).casefold()
            if not normalized:
                continue
            if normalized in seen and seen[normalized] != split:
                conflicts.add(normalized)
            else:
                seen[normalized] = split
        return len(conflicts)

    repeated_quotes = cross_split_duplicates((e["split"], e["quote"]) for e in evidence)
    if repeated_quotes:
        issues.append(f"跨分区重复原文证据 {repeated_quotes} 处")
    repeated_questions = cross_split_duplicates((r["split"], r["question"]) for r in approved)
    if repeated_questions:
        issues.append(f"跨分区重复获批问题 {repeated_questions} 处")
    review_modes = {mode: sum(r["review"].get("review_mode", "human") == mode for r in approved)
                    for mode in ("automated", "batch", "human")}
    structural_ready = not issues
    source_ready = not source_issues
    semantic_ready = not semantic_issues
    return {"documents": document_count, "evidence_units": len(evidence),
        "evidence_target": evidence_target, "approved_target": approved_target,
        "holdout_evidence_target": holdout_evidence_target, "candidates": len(records),
        "approved": len(approved), "approved_by_split": split_counts,
        "approved_by_document": approved_by_document,
        "approved_evidence_units_by_document": approved_evidence_units_by_document,
        "approved_by_review_mode": review_modes,
        "structural_ready": structural_ready, "source_verified": source_ready,
        "semantic_reviewed": semantic_ready,
        "semantic_validation": "policy_reviewed" if semantic_ready and approved else "not_independently_validated",
        "source_issues": source_issues, "semantic_issues": semantic_issues,
        "issues": issues + source_issues + semantic_issues,
        "final_ready": structural_ready and source_ready and semantic_ready}


def _jsonl_bytes(records: list[dict]) -> bytes:
    return "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in records).encode("utf-8")


def export_state_sha256(job_id: str) -> str:
    from aviation_ft import providers
    names = ("configs/settings.json", "configs/quality.json", "configs/generation.json",
             "configs/review.json", "prompts/generate_grounded_qa.md",
             "prompts/judge_grounded_qa.md", "prompts/judge_unanswerable_qa.md")
    state = {name: sha256_file(ROOT / name) if (ROOT / name).is_file() else None for name in names}
    state["provider_roles"] = {role: providers.public_config(job_id, role)
                               for role in ("generator", "judge_primary", "judge_countercheck")}
    return extraction_quality.config_hash(state)


def export_is_current(job_id: str, artifact: Path) -> bool:
    if not artifact.is_file():
        return False
    sidecar = read_json(artifact.with_suffix(".sha256.json"), {})
    if sidecar.get("status") == "historical_snapshot":
        return False
    root = job_dir(job_id)
    for key, relative in (("candidates_sha256", "candidates/records.jsonl"),
                          ("evidence_sha256", "evidence/units.jsonl"),
                          ("pages_sha256", "parsed/pages.jsonl"),
                          ("issues_sha256", "parsed/issues.jsonl"),
                          ("cross_page_decisions_sha256", "parsed/cross_page_decisions.jsonl"),
                          ("txt_report_sha256", "parsed/txt_report.json"),
                          ("review_events_sha256", "reviewed/events.jsonl"),
                          ("verification_events_sha256", "evidence/verification_events.jsonl")):
        path = root / relative
        if sidecar.get(key) != (sha256_file(path) if path.is_file() else None):
            return False
    if (sidecar.get("sha256") != sha256_file(artifact)
            or sidecar.get("state_sha256") != export_state_sha256(job_id)):
        return False
    documents = load_job(job_id)["documents"]
    if sidecar.get("pdf_sha256") != {d["doc_id"]: d["sha256"] for d in documents}:
        return False
    return all((root / "raw_pdf" / d["filename"]).is_file()
               and sha256_file(root / "raw_pdf" / d["filename"]) == d["sha256"]
               for d in documents)


def _preserve_export_snapshot(artifact: Path) -> None:
    """Keep a prior fixed-name export as an explicitly historical local snapshot."""
    if not artifact.is_file():
        return
    history = artifact.parent / "history"
    history.mkdir(exist_ok=True)
    old_sha = sha256_file(artifact)
    archived = history / f"{artifact.stem}-{old_sha[:16]}.zip"
    if not archived.exists():
        shutil.copy2(artifact, archived)
    sidecar = artifact.with_suffix(".sha256.json")
    metadata = read_json(sidecar, {})
    history_sidecar = archived.with_suffix(".sha256.json")
    if not history_sidecar.exists():
        write_json(history_sidecar, {
            **metadata, "filename": archived.name, "sha256": old_sha,
            "status": "historical_snapshot", "superseded_at": now()})


def export_job(job_id: str, final: bool = False, *, include_audit: bool = False) -> tuple[Path, str, dict]:
    p = job_dir(job_id)
    report = quality_report(job_id)
    if final and not report["final_ready"]:
        raise ValueError("正式导出门槛未达成：" + "；".join(report["issues"][:5]))
    meta = load_job(job_id)
    from aviation_ft import providers
    provider = providers.public_config(job_id)
    provider_audit = {
        "mode": provider["mode"],
        "model": provider["model"] if provider["mode"] == "external" else "aviation-generator",
        "base_url": provider["base_url"] if provider["mode"] == "external" else "http://127.0.0.1:8000/v1",
    }
    if final:
        for document in meta["documents"]:
            path = p / "raw_pdf" / document["filename"]
            if not path.exists() or sha256_file(path) != document["sha256"]:
                raise ValueError(f"原始 PDF 缺失或哈希变化：{document['filename']}")
    approved = [r for r in candidates_for(job_id) if r["review"]["status"] in {"approved", "approved_after_edit"}]
    def training_record(r):
        prompt = "\n".join(f"[{ref['evidence_id']}] {ref['quote']}" for ref in r["evidence"])
        return {"messages": [{"role": "system", "content": "只能依据给定证据回答；每个关键结论须引用证据编号；证据不足须说明边界。"}, {"role": "user", "content": f"证据：\n{prompt}\n\n问题：{r['question']}"}, {"role": "assistant", "content": r["answer"]}], "metadata": {"sample_id": r["sample_id"], "evidence_unit_ids": [x["evidence_unit_id"] for x in r["evidence"]], "answerability": r["answerability"], "origin": r["origin"]}}
    card = {"status": "final" if final else "preview_not_for_training", "generated_at": now(), "job_id": job_id, "source_job_id": meta.get("source_job_id"), "quality_report": report, "documents": [{k: d[k] for k in ("doc_id", "filename", "sha256", "pages")} for d in meta["documents"]], "parse_versions": sorted({page.get("parse_version", "legacy") for page in pages_for(job_id)}), "model_location": settings()["generator_model_dir"] if provider["mode"] == "local" else None, "model_provider": provider_audit, "semantic_validation": report["semantic_validation"], "auto_approval_enabled": review_policy.config(ROOT, job_id).get("auto_approve_enabled", False), "note": "原始 PDF 不在包内。正式包表示当前结构、来源与审核策略门槛达标，不代表真实准确率已经验证。训练前须在独立人工标注集验收。预览包不得用于训练。"}
    kind = "final" if final else "preview"
    target = p / "exports" / f"aviation-sft-{kind}.zip"
    trace_target = p / "exports" / f"aviation-sft-{kind}-trace.zip"
    readme = (
        "民航回答微调数据集\n"
        f"任务：{job_id}\n生成时间：{card['generated_at']}\n"
        f"状态：{card['status']}\n\n"
        f"train.jsonl：训练集，{report['approved_by_split']['train']} 条。\n"
        f"validation.jsonl：验证集，{report['approved_by_split']['validation']} 条。\n"
        f"test.jsonl：测试集，{report['approved_by_split']['test']} 条。\n\n"
        "格式：UTF-8 JSONL，每行一个样本；messages 为对话，metadata 为追溯标识。\n"
        "三个分区应分别使用，验证集和测试集不要混入训练集。\n"
        "训练包仅保留上述三个数据文件与本说明，完整追溯包额外包含证据、审核和运行记录。\n"
        "整理后的 TXT 单独下载；两个包中的训练、验证、测试数据完全一致。\n\n"
        + card["note"] + "\n"
    ).encode("utf-8")
    fd, tmp = tempfile.mkstemp(prefix=".export-", dir=target.parent)
    os.close(fd)
    try:
        contents = {f"{split}.jsonl": _jsonl_bytes([training_record(r) for r in approved if r["split"] == split]) for split in sorted(SPLITS)}
        training_contents = {**contents, "README.txt": readme}
        contents["README.txt"] = readme
        contents["master_approved.jsonl"] = _jsonl_bytes(approved)
        contents["evidence_units.jsonl"] = _jsonl_bytes(evidence_for(job_id))
        contents["review_events.jsonl"] = (p / "reviewed/events.jsonl").read_bytes() if (p / "reviewed/events.jsonl").exists() else b""
        contents["generation_attempts.jsonl"] = (p / "candidates/generation_attempts.jsonl").read_bytes() if (p / "candidates/generation_attempts.jsonl").exists() else b""
        for archive_name, relative_path in (
            ("parsed_pages.jsonl", "parsed/pages.jsonl"),
            ("extraction_candidates.jsonl", "parsed/extraction_candidates.jsonl"),
            ("page_quality.jsonl", "parsed/page_quality.jsonl"),
            ("quality_issues.jsonl", "parsed/issues.jsonl"),
            ("quality_issue_events.jsonl", "parsed/issue_events.jsonl"),
            ("readable_units.jsonl", "parsed/readable_units.jsonl"),
            ("txt_report.json", "parsed/txt_report.json"),
            ("evidence_verification_events.jsonl", "evidence/verification_events.jsonl"),
            ("evidence_auto_assessments.jsonl", "evidence/auto_assessments.jsonl"),
            ("evidence_human_queue.jsonl", "evidence/human_queue.jsonl"),
            ("automation_progress.jsonl", "automation/progress.jsonl"),
        ):
            source = p / relative_path
            if source.exists():
                contents[archive_name] = source.read_bytes()
        judge_prompt = ROOT / "prompts/judge_grounded_qa.md"
        if judge_prompt.is_file():
            contents["judge_prompt.md"] = judge_prompt.read_bytes()
        frozen_recipe_path = p / "candidates/generation_recipe.json"
        if frozen_recipe_path.exists():
            contents["generation_recipe.json"] = frozen_recipe_path.read_bytes()
        generation_config = ROOT / "configs/generation.json"
        if generation_config.exists():
            contents["generation_config.json"] = generation_config.read_bytes()
            prompt_file = read_json(generation_config).get("prompt_file")
            if prompt_file and (ROOT / prompt_file).is_file():
                contents["generation_prompt.md"] = (ROOT / prompt_file).read_bytes()
        contents["dataset_card.json"] = json.dumps(card, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        contents["sources.json"] = json.dumps(card["documents"], ensure_ascii=False, indent=2).encode("utf-8")
        checksums = "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in sorted(contents.items()))
        contents["SHA256SUMS.txt"] = checksums.encode()
        trace_fd, trace_tmp = tempfile.mkstemp(prefix=".export-trace-", dir=target.parent)
        os.close(trace_fd)
        try:
            with zipfile.ZipFile(trace_tmp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for name, data in sorted(contents.items()):
                    archive.writestr(name, data)
            _preserve_export_snapshot(trace_target)
            os.replace(trace_tmp, trace_target)
        finally:
            if os.path.exists(trace_tmp):
                os.unlink(trace_tmp)
        contents = training_contents
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as z:
            for name, data in sorted(contents.items()):
                z.writestr(name, data)
        _preserve_export_snapshot(target)
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    digests = {}
    for artifact, bundle in ((target, "training"), (trace_target, "trace")):
        digest = sha256_file(artifact)
        digests[bundle] = digest
        write_json(artifact.with_suffix(".sha256.json"), {
            "filename": artifact.name, "sha256": digest, "created_at": now(),
            "status": card["status"], "bundle": bundle,
            "candidates_sha256": sha256_file(p / "candidates/records.jsonl") if (p / "candidates/records.jsonl").exists() else None,
            "evidence_sha256": sha256_file(p / "evidence/units.jsonl") if (p / "evidence/units.jsonl").exists() else None,
            "pages_sha256": sha256_file(p / "parsed/pages.jsonl") if (p / "parsed/pages.jsonl").exists() else None,
            "issues_sha256": sha256_file(p / "parsed/issues.jsonl") if (p / "parsed/issues.jsonl").exists() else None,
            "cross_page_decisions_sha256": sha256_file(p / "parsed/cross_page_decisions.jsonl") if (p / "parsed/cross_page_decisions.jsonl").exists() else None,
            "txt_report_sha256": sha256_file(p / "parsed/txt_report.json") if (p / "parsed/txt_report.json").exists() else None,
            "review_events_sha256": sha256_file(p / "reviewed/events.jsonl") if (p / "reviewed/events.jsonl").exists() else None,
            "verification_events_sha256": sha256_file(p / "evidence/verification_events.jsonl") if (p / "evidence/verification_events.jsonl").exists() else None,
            "pdf_sha256": {d["doc_id"]: d["sha256"] for d in meta["documents"]},
            "state_sha256": export_state_sha256(job_id),
        })
    meta["status"] = "final_exported" if final else "preview_exported"
    meta["last_export_at"] = now()
    write_json(p / "job.json", meta)
    return (trace_target, digests["trace"], report) if include_audit else (target, digests["training"], report)


def _remove_job_tree(path: Path) -> None:
    """Windows marks the immutable uploaded PDFs read-only; explicit deletion clears it."""
    def clear_readonly(func, name, error):
        os.chmod(name, stat.S_IWRITE | stat.S_IREAD)
        func(name)
    shutil.rmtree(path, onerror=clear_readonly)


def _remove_provider_secrets(job_id: str) -> None:
    for role in ("", ".judge_primary", ".judge_countercheck"):
        secret = ROOT / "runtime/provider_secrets" / f"{job_id}{role}.json"
        if secret.is_file() and not secret.is_symlink():
            secret.unlink()


def delete_job(job_id: str, confirmation: str) -> dict:
    """Permanently delete a task after an exact job-id confirmation."""
    p = job_dir(job_id)
    if confirmation != job_id:
        raise ValueError("请输入完整任务 ID 确认删除")
    meta = load_job(job_id)
    audit = {
        "job_id": job_id,
        "label": meta.get("label", ""),
        "at": now(),
        "reason": "manual_task_delete",
        "documents": len(meta.get("documents", [])),
    }
    _remove_provider_secrets(job_id)
    _remove_job_tree(p)
    append_jsonl(ROOT / "runtime/cleanup_log.jsonl", audit)
    return audit


def cleanup_job(job_id: str, confirmation: str, expected_sha256: str, reason: str = "manual_after_download_confirmation") -> None:
    p = job_dir(job_id)
    if confirmation != job_id:
        raise ValueError("请输入完整任务 ID 确认")
    exports = sorted((p / "exports").glob("aviation-sft-*.sha256.json"))
    if not exports or expected_sha256 not in {read_json(x)["sha256"] for x in exports}:
        raise ValueError("请先下载数据包，并提供其 SHA-256 确认")
    meta = load_job(job_id)
    _remove_provider_secrets(job_id)
    _remove_job_tree(p)
    append_jsonl(ROOT / "runtime/cleanup_log.jsonl", {"job_id": job_id, "label": meta["label"], "at": now(), "reason": reason, "export_sha256": expected_sha256})


def expired_completed_jobs(days: int | None = None) -> list[str]:
    retention = days if days is not None else int(settings()["completed_job_retention_days"])
    threshold = datetime.now(timezone.utc) - timedelta(days=retention)
    result = []
    for meta in list_jobs():
        if meta.get("status") == "final_exported" and meta.get("last_export_at") and datetime.fromisoformat(meta["last_export_at"]) < threshold:
            result.append(meta["job_id"])
    return result
