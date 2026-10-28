from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import unicodedata
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pymupdf as fitz

from aviation_ft import source_revision as sources

ROOT = Path(__file__).resolve().parents[2]
JOB_RE = re.compile(r"^[0-9a-f]{32}$")
SPLITS = {"train", "validation", "test"}
DECISIONS = {"approved", "approved_after_edit", "regenerate", "rejected", "pending_expert_review"}
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


@sources.locked
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
        if (source / "parsed/sources").is_dir():
            shutil.copytree(source / "parsed/sources", target / "parsed/sources")
            state = sources.manifest(new_id)
            state.setdefault("inherited_operations", []).append(state["operations"])
            state["operations"] = {}
            for page_entry in state["pages"].values():
                for candidate_entry in page_entry["candidates"].values():
                    if candidate_entry["status"] == "available":
                        path = target / "parsed/sources/candidates" / f"{candidate_entry['candidate_id']}.json"
                        candidate = read_json(path)
                        candidate["job_id"] = new_id
                        write_json(path, candidate)
                        candidate_entry["sha256"] = sha256_file(path)
            write_json(target / "parsed/sources/manifest.json", state)
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
    content_blocks = _content_blocks(page)
    if not content_blocks:
        return []
    groups: list[tuple[str, list[dict]]] = []
    for block_index, block in enumerate(content_blocks):
        kind = _block_kind(block)
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
    for index, (kind, blocks) in enumerate(groups, 1):
        raw_text = "\n".join(str(block["text"]).rstrip("\n") for block in blocks).strip()
        text = raw_text if kind == "table" else _prose_text(blocks)
        raw_positions = [raw_blocks.index(block) for block in blocks]
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
            "possible_continuation_from_previous_page": starts_mid_sentence,
            "possible_continuation_to_next_page": ends_mid_sentence,
            "ocr_used": any(bool(block.get("ocr_used")) for block in blocks),
        })
        result[-1]["quality_issues"] = evidence_source_issues({
            **result[-1], "source_text": text,
        })
    return result


def semantic_blocks_for_document(pages: list[dict]) -> list[dict]:
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
                        end_pdf_page=page["pdf_page"])
            part["source_locations"] = [
                {"pdf_page": page["pdf_page"], "block_id": bid, "bbox": box}
                for bid, box in zip(part["block_ids"], part["bboxes"])
            ]
            previous = result[-1] if result else None
            compatible = previous and (
                part["kind"] == previous["kind"]
                or (part["kind"] == "prose" and previous["kind"] == "list_item")
            )
            if (adjacent and index == 0 and compatible
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
    meta = load_job(job_id)
    if not meta["documents"]:
        raise ValueError("请先上传 PDF")
    if (p / "parsed/pages.jsonl").exists():
        raise ValueError("已有原始提取结果，不能覆盖解析；请创建新任务或建立历史来源基线")
    pages = []
    cfg = settings()
    ocr_languages = cfg.get("ocr_languages", "chi_sim+eng")
    ocr_dpi = int(cfg.get("ocr_dpi", 200))
    tessdata = Path(cfg.get("ocr_tessdata", "models/ocr/tessdata"))
    tessdata = str(tessdata if tessdata.is_absolute() else ROOT / tessdata)
    counts = {"pages": 0, "blocks": 0, "ocr_pages": 0, "ocr_blocks": 0, "possible_scanned_pages": 0}
    for entry in meta["documents"]:
        path = p / "raw_pdf" / entry["filename"]
        if sha256_file(path) != entry["sha256"]:
            raise ValueError(f"原始 PDF 哈希不匹配：{entry['filename']}")
        with fitz.open(path) as doc:
            previous_page = None
            for idx, page in enumerate(doc):
                def extract_blocks(textpage=None, ocr_block_numbers=frozenset()):
                    result = []
                    for raw in page.get_text("blocks", textpage=textpage):
                        if len(raw) < 7 or raw[6] != 0 or not str(raw[4]).strip():
                            continue
                        block_id = f"{entry['doc_id']}-p{idx + 1:04d}-b{len(result) + 1:04d}"
                        result.append({"block_id": block_id,
                            "bbox": [round(float(x), 2) for x in raw[:4]],
                            "text": str(raw[4]),
                            "ocr_used": raw[5] in ocr_block_numbers,
                        })
                    return result

                blocks = extract_blocks()
                native_blocks = [dict(b) for b in blocks]
                extraction = {"native": {"status": "available", "blocks": native_blocks}, "ocr": {"status": "not_generated"}}
                text_len = sum(len(b["text"].strip()) for b in blocks)
                ocr_used = text_len < 30 and bool(page.get_image_info())
                if ocr_used:
                    try:
                        textpage = page.get_textpage_ocr(language=ocr_languages, dpi=ocr_dpi, full=False, tessdata=tessdata)
                        ocr_block_numbers = {
                            block["number"]
                            for block in page.get_text("dict", textpage=textpage)["blocks"]
                            if any(
                                span["font"] == "GlyphLessFont"
                                for line in block.get("lines", [])
                                for span in line["spans"]
                            )
                        }
                        blocks = extract_blocks(textpage=textpage, ocr_block_numbers=ocr_block_numbers)
                        extraction["ocr"] = {"status": "available", "blocks": blocks}
                    except (RuntimeError, ValueError, fitz.mupdf.FzErrorLibrary) as exc:
                        extraction["ocr"] = {"status": "failed", "error": str(exc)}
                        blocks = native_blocks
                    text_len = sum(len(b["text"].strip()) for b in blocks)
                scanned = text_len < 30
                parsed_page = {
                    "doc_id": entry["doc_id"], "filename": entry["filename"],
                    "pdf_page": idx + 1, "printed_page": None,
                    "width": round(float(page.rect.width), 2),
                    "height": round(float(page.rect.height), 2),
                    "blocks": blocks, "ocr_used": ocr_used, "possible_scanned": scanned,
                    "_source_candidates": extraction,
                }
                parsed_page["semantic_blocks"] = semantic_blocks_for_page(parsed_page, previous_page)
                pages.append(parsed_page)
                previous_page = parsed_page
                counts["pages"] += 1
                counts["blocks"] += len(blocks)
                counts["ocr_pages"] += int(ocr_used)
                counts["ocr_blocks"] += sum(b["ocr_used"] for b in blocks)
                counts["possible_scanned_pages"] += int(scanned)
    with sources.task_lock(job_id):
        if (p / "evidence/units.jsonl").exists() or sources.manifest(job_id)["pages"]:
            raise ValueError("解析期间任务已变化，拒绝覆盖")
        write_jsonl(p / "parsed/pages.jsonl", [{k: v for k, v in page.items() if k != "_source_candidates"} for page in pages])
        sources.register_parsed(job_id, pages)
    txt_report = build_semantic_txt(job_id)
    counts["semantic_units"] = txt_report["semantic_units"]
    counts["semantic_txt_chars"] = txt_report["chars"]
    write_json(p / "parsed/report.json", counts)
    meta["status"] = "parsed"
    write_json(p / "job.json", meta)
    return counts


def pages_for(job_id: str) -> list[dict]:
    return sources.effective_pages(job_id, read_jsonl(job_dir(job_id) / "parsed/pages.jsonl"))


def evidence_for(job_id: str) -> list[dict]:
    return sources.annotate_evidence(job_id, read_jsonl(job_dir(job_id) / "evidence/units.jsonl"))


def active_evidence_for(job_id: str) -> list[dict]:
    return [u for u in evidence_for(job_id) if not u["source_issues"]]

def build_semantic_txt(job_id: str) -> dict:
    """Build an ordered, readable evidence-block corpus from every parsed PDF."""
    from aviation_ft import readable_txt
    p = job_dir(job_id)
    with sources.task_lock(job_id):
        expected_snapshot = sources.snapshot(job_id)
    content, records, exclusions, report = readable_txt.build(job_id)
    with sources.task_lock(job_id):
        if sources.snapshot(job_id) != expected_snapshot:
            raise sources.SourceError("TXT 生成期间来源已变化，请重新生成", 409)
        target = p / "parsed/semantic_corpus.txt"
        history = sources.archive_artifact(target)
        if history:
            for name in ("txt_report.json", "readable_units.jsonl"):
                if (target.parent / name).is_file():
                    shutil.copy2(target.parent / name, history / name)
        report["source_snapshot"] = expected_snapshot
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
        write_jsonl(p / "parsed/readable_units.jsonl", records)
        write_json(p / "parsed/txt_report.json", {**report, "exclusions": exclusions})
        return report


@sources.locked
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
    if start < 0 or text.find(quote, start + 1) >= 0:
        raise ValueError("引用必须是原始 block 中唯一的精确子串")
    records = evidence_for(job_id)
    if any(r["block_id"] == block_id and r["quote"] == quote and not any("已过期" in issue for issue in r["source_issues"]) for r in records):
        raise ValueError("证据单元重复")
    record = {"evidence_unit_id": f"EU-{uuid.uuid4().hex[:12]}", "doc_id": page["doc_id"], "filename": page["filename"], "pdf_page": page["pdf_page"], "printed_page": page["printed_page"], "block_id": block_id, "bbox": block["bbox"], "source_text": text, "quote": quote, "char_start": start, "char_end": start + len(quote), "topic": topic.strip(), "clause_group": clause_group.strip(), "split": split, "confirmed_at": now()}
    sources.bind_evidence(job_id, record)
    records.append(record)
    write_jsonl(p / "evidence/units.jsonl", [{k: v for k, v in u.items() if k != "source_issues"} for u in records])
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


@sources.locked
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
    if start < 0 or text.find(quote, start + 1) >= 0:
        raise ValueError("引用必须是所选连续文本块中唯一的精确子串")
    records = evidence_for(job_id)
    if any(r["block_id"] == source["block_id"] and r["quote"] == quote and not any("已过期" in issue for issue in r["source_issues"]) for r in records):
        raise ValueError("证据单元重复")
    if any(
        r["doc_id"] == doc_id and r["clause_group"] == clause_group.strip() and r["split"] != split
        for r in records
    ):
        raise ValueError("同一章节/相邻条款组跨 split")
    record = {
        "evidence_unit_id": f"EU-{uuid.uuid4().hex[:12]}",
        "doc_id": doc_id, "filename": page["filename"],
        "pdf_page": pdf_page, "printed_page": page["printed_page"],
        "block_id": source["block_id"], "block_ids": source["block_ids"],
        "bbox": source["bbox"], "bboxes": source["bboxes"],
        "source_text": text, "quote": quote,
        "raw_source_text": source.get("raw_source_text", text),
        "text_reconstructed": bool(source.get("text_reconstructed")),
        "char_start": start, "char_end": start + len(quote),
        "topic": topic.strip(), "clause_group": clause_group.strip(),
        "split": split, "scope": "block_range", "ocr_used": source["ocr_used"],
        "confirmed_by": reviewer.strip(), "confirmed_at": now(),
    }
    sources.bind_evidence(job_id, record)
    records.append(record)
    write_jsonl(p / "evidence/units.jsonl", [{k: v for k, v in u.items() if k != "source_issues"} for u in records])
    return record


def evidence_source_issues(source: dict) -> list[str]:
    """Return deterministic reasons a semantic source is unsafe to auto-approve."""
    issues = []
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
        for item in active_evidence_for(job_id)
    }
    page_lookup = {page["pdf_page"]: page for page in document_pages}
    all_candidates = []
    for semantic in semantic_blocks_for_document(document_pages):
        if semantic["kind"] == "heading" or not 40 <= len(semantic["text"]) <= 4000:
            continue
        page = page_lookup[semantic["pdf_page"]]
        all_candidates.append((page, _source_from_semantic_block(page, semantic)))
    all_bands = sorted({(page["pdf_page"] - 1) // 5 for page, _ in all_candidates})
    candidates = [
        (page, source) for page, source in all_candidates
        if (page["doc_id"], tuple(source["block_ids"])) not in existing_sources
    ]
    if len(candidates) > limit:
        positions = [round(i * (len(candidates) - 1) / (limit - 1)) for i in range(limit)]
        candidates = [candidates[i] for i in positions]
    split_by_band = {
        band: ("train" if i < len(all_bands) * 0.6 else "validation" if i < len(all_bands) * 0.8 else "test")
        for i, band in enumerate(all_bands)
    }
    result = []
    for page, source in candidates:
        band = (page["pdf_page"] - 1) // 5
        result.append({
            "source_snapshot": sources.snapshot(job_id),
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
            "clause_group": f"pages-{band * 5 + 1:04d}-{band * 5 + 5:04d}",
            "split": split_by_band[band],
        })
    return result


@sources.locked
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
    seen_quotes = {(r["block_id"], r["quote"]) for r in records if not any("已过期" in issue for issue in r["source_issues"])}
    group_splits = {(r["doc_id"], r["clause_group"]): r["split"] for r in records}
    additions = []
    for item in items:
        if "source_snapshot" in item and item["source_snapshot"] != sources.snapshot(job_id):
            raise sources.SourceError("证据预览来源已变化，请重新核对", 409)
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
                ])
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
        if start < 0 or text.find(quote, start + 1) >= 0:
            raise ValueError(f"{block_id}：引用必须是该文本块中唯一的精确子串")
        key = (block_id, quote)
        if key in seen_quotes:
            raise ValueError(f"{block_id}：证据单元重复")
        seen_quotes.add(key)
        group_key = (page["doc_id"], group)
        if group_key in group_splits and group_splits[group_key] != split:
            raise ValueError(f"{block_id}：同一页段/章节组跨 split")
        group_splits[group_key] = split
        additions.append({
            "evidence_unit_id": f"EU-{uuid.uuid4().hex[:12]}",
            "doc_id": page["doc_id"], "filename": page["filename"],
            "pdf_page": page["pdf_page"], "printed_page": page["printed_page"],
            "block_id": block_id, "block_ids": source["block_ids"],
            "bbox": source["bbox"], "bboxes": source["bboxes"],
            "source_text": text, "quote": quote,
            "end_pdf_page": source.get("end_pdf_page", page["pdf_page"]),
            "source_locations": source.get("source_locations", [
                {"pdf_page": page["pdf_page"], "block_id": bid, "bbox": box}
                for bid, box in zip(source["block_ids"], source["bboxes"])
            ]),
            "raw_source_text": source.get("raw_source_text", text),
            "text_reconstructed": bool(source.get("text_reconstructed")),
            "char_start": start, "char_end": start + len(quote),
            "topic": topic, "clause_group": group, "split": split,
            "scope": "block_range" if len(source["block_ids"]) > 1 else "block",
            "ocr_used": source["ocr_used"],
            "confirmed_by": reviewer.strip(), "confirmed_at": now(),
            "confirmation_mode": "automated" if reviewer.startswith("model:") else "human",
        })
    for unit in additions:
        sources.bind_evidence(job_id, unit)
    write_jsonl(p / "evidence/units.jsonl", [{k: v for k, v in u.items() if k != "source_issues"} for u in records + additions])
    return additions


@sources.locked
def freeze_splits(job_id: str) -> dict:
    p = job_dir(job_id)
    if (p / "evidence/splits.json").exists():
        return read_json(p / "evidence/splits.json")
    evidence = active_evidence_for(job_id)
    if any(u["source_issues"] and "当前来源版本待人工核实" in u["source_issues"] for u in evidence_for(job_id)):
        raise sources.SourceError("来源版本待核实，不能冻结", 409)
    if not evidence or {e["split"] for e in evidence} != SPLITS:
        raise ValueError("冻结前 train、validation、test 都必须有证据")
    groups = {}
    for e in evidence:
        key = (e["doc_id"], e["clause_group"])
        if key in groups and groups[key] != e["split"]:
            raise ValueError(f"同一章节/相邻条款组跨 split：{key}")
        groups[key] = e["split"]
    counts = {split: sum(e["split"] == split for e in evidence) for split in sorted(SPLITS)}
    manifest = {"frozen_at": now(), "evidence_sha256": sha256_file(p / "evidence/units.jsonl"), "counts": counts, "groups": len(groups)}
    write_json(p / "evidence/splits.json", manifest)
    return manifest


def candidates_for(job_id: str) -> list[dict]:
    return read_jsonl(job_dir(job_id) / "candidates/records.jsonl")


@sources.locked
def add_candidate(job_id: str, question: str, answer: str, evidence_ids: list[str], answerability: str, claim: str = "", origin: str = "manual", generator_model: str | None = None, prompt_version: str | None = None, generation_key: str | None = None) -> dict:
    p = job_dir(job_id)
    if not (p / "evidence/splits.json").exists():
        raise ValueError("必须先冻结证据分区")
    units = {e["evidence_unit_id"]: e for e in evidence_for(job_id)}
    if not evidence_ids or any(eid not in units for eid in evidence_ids):
        raise ValueError("至少选择一个存在的证据单元")
    sources.require_evidence(job_id, [units[eid] for eid in evidence_ids])
    splits = {units[eid]["split"] for eid in evidence_ids}
    if len(splits) != 1:
        raise ValueError("不能跨 split 组合证据")
    if answerability not in {"answerable", "unanswerable"}:
        raise ValueError("无效的可回答状态")
    refs = []
    for idx, eid in enumerate(evidence_ids, 1):
        e = units[eid]
        refs.append({"evidence_id": f"E{idx}", "evidence_unit_id": eid, "doc_id": e["doc_id"], "pdf_page": e["pdf_page"], "block_id": e["block_id"], "quote": e["quote"], "char_start": e["char_start"], "char_end": e["char_end"], "source_revisions": e.get("source_revisions", {})})
    records = candidates_for(job_id)
    if generation_key and any(r.get("generation_key") == generation_key for r in records):
        raise ValueError("同一证据、提示词和变体的候选已存在")
    record = {"sample_id": f"SFT-{uuid.uuid4().hex[:12]}", "split": next(iter(splits)), "task_type": "grounded_qa" if answerability == "answerable" else "unanswerable", "question": question.strip(), "answerability": answerability, "answer": answer.strip(), "claims": [{"claim_id": "C1", "text": claim.strip(), "support_type": "direct" if len(refs) == 1 else "multi_evidence_derived", "evidence_ids": [r["evidence_id"] for r in refs]}] if answerability == "answerable" else [], "evidence": refs, "origin": origin, "generator_model": generator_model, "prompt_version": prompt_version, "generation_key": generation_key, "review": {"status": "pending", "reviewer_id": None, "comment": ""}, "created_at": now()}
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
        errors.extend(unit.get("source_issues", []))
        if rid in seen:
            errors.append("证据编号重复")
        seen.add(rid)
        if ref.get("source_revisions", {}) != unit.get("source_revisions", {}):
            errors.append("引用来源版本与证据不一致")
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
        if not record.get("claims"):
            errors.append("有证据答案缺少 claim")
        for claim in record.get("claims", []):
            if not claim.get("text") or claim.get("support_type") == "unsupported" or not claim.get("evidence_ids"):
                errors.append("claim 内容或证据映射无效")
            if set(claim.get("evidence_ids", [])) - seen:
                errors.append("claim 指向不存在的证据编号")
            if not {f"[{x}]" for x in claim.get("evidence_ids", [])} <= markers:
                errors.append("claim 引用未在答案中出现")
    elif record.get("answerability") == "unanswerable":
        if record.get("claims"):
            errors.append("不可回答样本不能含正式 claim")
        if "不足" not in record.get("answer", "") and "无法确认" not in record.get("answer", ""):
            errors.append("拒答答案必须说明证据不足或无法确认")
    else:
        errors.append("无效 answerability")
    return list(dict.fromkeys(errors))


@sources.locked
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
    protected = ("split", "origin", "generator_model", "prompt_version", "generation_key")
    if any(revised.get(field) != old.get(field) for field in protected):
        raise ValueError("split、候选来源和生成溯源字段不可通过编辑修改")
    units = {e["evidence_unit_id"]: e for e in evidence_for(job_id)}
    errors = validate_candidate(revised, units)
    if errors:
        raise ValueError("编辑后仍有错误：" + "；".join(errors))
    revised["review"] = {"status": "pending", "reviewer_id": None, "comment": ""}
    revised["edited_at"] = now()
    append_jsonl(p / "reviewed/events.jsonl", {"sample_id": sample_id, "action": "edit", "editor": editor.strip(), "before": old, "after": revised, "at": now()})
    records[idx] = revised
    write_jsonl(p / "candidates/records.jsonl", records)
    return revised


@sources.locked
def review_candidate(job_id: str, sample_id: str, decision: str, reviewer: str, comment: str = "", auto_assessment: dict | None = None) -> dict:
    if decision not in DECISIONS or not reviewer.strip():
        raise ValueError("请选择审核结论并填写审核人")
    p = job_dir(job_id)
    records = candidates_for(job_id)
    matches = [r for r in records if r["sample_id"] == sample_id]
    if len(matches) != 1:
        raise ValueError("样本不存在或 ID 不唯一")
    record = matches[0]
    if decision in {"approved", "approved_after_edit"}:
        units = {e["evidence_unit_id"]: e for e in evidence_for(job_id)}
        errors = validate_candidate(record, units)
        if errors:
            raise ValueError("审核未通过：" + "；".join(errors))
    before = record["review"].copy()
    record["review"] = {"status": decision, "reviewer_id": reviewer.strip(), "comment": comment.strip(), "reviewed_at": now()}
    if auto_assessment is not None:
        if not reviewer.startswith("model:"):
            raise ValueError("自动审核记录必须注明模型")
        record["review"]["review_mode"] = "automated"
        record["review"]["assessment"] = auto_assessment
    append_jsonl(p / "reviewed/events.jsonl", {"sample_id": sample_id, "before": before, "after": record["review"], "at": now()})
    write_jsonl(p / "candidates/records.jsonl", records)
    return record


@sources.locked
def review_candidates_batch(
    job_id: str, sample_ids: list[str], reviewer: str,
    decision: str = "approved", comment: str = "",
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
    units = {item["evidence_unit_id"]: item for item in evidence_for(job_id)}
    for sample_id in sample_ids:
        record = by_id[sample_id]
        if record["review"]["status"] not in {"pending", "pending_expert_review"}:
            raise ValueError(f"{sample_id}：只能批量审核待审或待专家核对候选")
        if decision == "approved":
            errors = validate_candidate(record, units)
            if errors:
                raise ValueError(f"{sample_id}：" + "；".join(errors))
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
    evidence = active_evidence_for(job_id)
    records = candidates_for(job_id)
    unit_map = {u["evidence_unit_id"]: u for u in evidence_for(job_id)}
    approved = [r for r in records if r["review"]["status"] in {"approved", "approved_after_edit"} and not validate_candidate(r, unit_map)]
    document_count = len(meta["documents"])
    evidence_target = 20 * document_count
    approved_target = 90 * document_count
    holdout_evidence_target = 4 * document_count
    issues = []
    if not document_count:
        issues.append("请至少上传一份 PDF")
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
            prompt_path = ROOT / generation_config.get("prompt_file", "")
            expected_recipe["prompt_sha256"] = sha256_file(prompt_path) if prompt_path.is_file() else None
            if fingerprint != expected_recipe:
                issues.append("当前生成配方与测试集冻结配方不一致")
    units = {e["evidence_unit_id"]: e for e in evidence}
    for record in approved:
        for err in validate_candidate(record, units):
            issues.append(f"{record['sample_id']}: {err}")
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
    return {"documents": document_count, "evidence_units": len(evidence), "evidence_target": evidence_target, "approved_target": approved_target, "holdout_evidence_target": holdout_evidence_target, "candidates": len(records), "approved": len(approved), "approved_by_split": split_counts, "approved_by_document": approved_by_document, "approved_evidence_units_by_document": approved_evidence_units_by_document, "approved_by_review_mode": review_modes, "semantic_validation": "not_independently_validated", "issues": issues, "final_ready": not issues}


def _jsonl_bytes(records: list[dict]) -> bytes:
    return "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in records).encode("utf-8")


def export_job(job_id: str, final: bool = False, *, include_audit: bool = False) -> tuple[Path, str, dict]:
    p = job_dir(job_id)
    with sources.task_lock(job_id):
        expected_inputs = sources.build_fingerprint(job_id)
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
    units = {u["evidence_unit_id"]: u for u in evidence_for(job_id)}
    approved = [r for r in candidates_for(job_id) if r["review"]["status"] in {"approved", "approved_after_edit"} and not validate_candidate(r, units)]
    def training_record(r):
        prompt = "\n".join(f"[{ref['evidence_id']}] {ref['quote']}" for ref in r["evidence"])
        return {"messages": [{"role": "system", "content": "只能依据给定证据回答；每个关键结论须引用证据编号；证据不足须说明边界。"}, {"role": "user", "content": f"证据：\n{prompt}\n\n问题：{r['question']}"}, {"role": "assistant", "content": r["answer"]}], "metadata": {"sample_id": r["sample_id"], "evidence_unit_ids": [x["evidence_unit_id"] for x in r["evidence"]], "answerability": r["answerability"], "origin": r["origin"]}}
    card = {"status": "final" if final else "preview_not_for_training", "generated_at": now(), "job_id": job_id, "source_job_id": meta.get("source_job_id"), "quality_report": report, "documents": [{k: d[k] for k in ("doc_id", "filename", "sha256", "pages")} for d in meta["documents"]], "model_location": settings()["generator_model_dir"] if provider["mode"] == "local" else None, "model_provider": provider_audit, "semantic_validation": report["semantic_validation"], "note": "原始 PDF 不在包内。正式包仅表示结构、数量、覆盖与引用门槛达标；生成与核验使用同一模型，语义质量未经独立验证。训练前应抽样验收。预览包不得用于训练。"}
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
    contents = {f"{split}.jsonl": _jsonl_bytes([training_record(r) for r in approved if r["split"] == split]) for split in sorted(SPLITS)}
    training_contents = {**contents, "README.txt": readme}
    contents["README.txt"] = readme
    contents["master_approved.jsonl"] = _jsonl_bytes(approved)
    contents["evidence_units.jsonl"] = _jsonl_bytes(evidence_for(job_id))
    contents["review_events.jsonl"] = (p / "reviewed/events.jsonl").read_bytes() if (p / "reviewed/events.jsonl").exists() else b""
    contents["generation_attempts.jsonl"] = (p / "candidates/generation_attempts.jsonl").read_bytes() if (p / "candidates/generation_attempts.jsonl").exists() else b""
    for archive_name, relative_path in (
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
    if (p / "parsed/sources/manifest.json").is_file():
        source_state = sources.manifest(job_id)
        contents["source_history/manifest.json"] = (p / "parsed/sources/manifest.json").read_bytes()
        source_paths = {f"revisions/{rid}.json" for rid in source_state["revisions"]}
        source_paths.update(f"candidates/{entry['candidate_id']}.json" for page in source_state["pages"].values()
                            for entry in page["candidates"].values() if entry["status"] == "available")
        for relative in sorted(source_paths):
            contents["source_history/" + relative] = (p / "parsed/sources" / relative).read_bytes()
    contents["dataset_card.json"] = json.dumps(card, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
    contents["sources.json"] = json.dumps(card["documents"], ensure_ascii=False, indent=2).encode("utf-8")
    checksums = "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in sorted(contents.items()))
    contents["SHA256SUMS.txt"] = checksums.encode()
    fd, tmp = tempfile.mkstemp(prefix=".export-", dir=target.parent)
    os.close(fd)
    trace_fd, trace_tmp = tempfile.mkstemp(prefix=".export-trace-", dir=target.parent)
    os.close(trace_fd)
    try:
        with zipfile.ZipFile(trace_tmp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in sorted(contents.items()):
                archive.writestr(name, data)
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in sorted(training_contents.items()):
                archive.writestr(name, data)
        with sources.task_lock(job_id):
            if sources.build_fingerprint(job_id) != expected_inputs:
                raise sources.SourceError("导出期间来源、证据或审核已变化，请重新导出", 409)
            sources.archive_artifact(target)
            sources.archive_artifact(trace_target)
            os.replace(trace_tmp, trace_target)
            os.replace(tmp, target)
            digests = {}
            for artifact, bundle in ((target, "training"), (trace_target, "trace")):
                digest = sha256_file(artifact)
                digests[bundle] = digest
                write_json(artifact.with_suffix(".sha256.json"), {
                    "filename": artifact.name, "sha256": digest, "created_at": now(),
                    "status": card["status"], "bundle": bundle,
                    "source_snapshot": expected_inputs["source_snapshot"],
                    "candidates_sha256": expected_inputs["candidates_sha256"],
                    "evidence_sha256": expected_inputs["evidence_sha256"],
                })
            meta = load_job(job_id)
            meta["status"] = "final_exported" if final else "preview_exported"
            meta["last_export_at"] = now()
            write_json(p / "job.json", meta)
        return (trace_target, digests["trace"], report) if include_audit else (target, digests["training"], report)
    finally:
        for temporary in (tmp, trace_tmp):
            if os.path.exists(temporary):
                os.unlink(temporary)


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
    secret = ROOT / "runtime/provider_secrets" / f"{job_id}.json"
    if secret.is_file() and not secret.is_symlink():
        secret.unlink()
    shutil.rmtree(p)
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
    shutil.rmtree(p)
    append_jsonl(ROOT / "runtime/cleanup_log.jsonl", {"job_id": job_id, "label": meta["label"], "at": now(), "reason": reason, "export_sha256": expected_sha256})


def expired_completed_jobs(days: int | None = None) -> list[str]:
    retention = days if days is not None else int(settings()["completed_job_retention_days"])
    threshold = datetime.now(timezone.utc) - timedelta(days=retention)
    result = []
    for meta in list_jobs():
        if meta.get("status") == "final_exported" and meta.get("last_export_at") and datetime.fromisoformat(meta["last_export_at"]) < threshold:
            result.append(meta["job_id"])
    return result
