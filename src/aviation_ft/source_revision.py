"""Page source revisions, provenance and delivery validity shared by all entrypoints."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import threading
import uuid
from contextlib import contextmanager
from difflib import SequenceMatcher
from functools import wraps

from aviation_ft import core

_locks = {}
_guard = threading.Lock()
_local = threading.local()


class SourceError(ValueError):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def text_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def page_key(doc_id, pdf_page):
    return f"{doc_id}:{pdf_page}"


def root(job_id):
    try:
        return core.job_dir(job_id) / "parsed/sources"
    except ValueError as exc:
        raise SourceError(str(exc), 404 if "不存在" in str(exc) else 400) from exc


def manifest(job_id: str) -> dict:
    return core.read_json(root(job_id) / "manifest.json", {"schema_version": 1, "pages": {}, "revisions": {}, "operations": {}})


@contextmanager
def task_lock(job_id: str):
    path = core.job_dir(job_id) / ".source.lock"
    name = str(path)
    with _guard:
        lock = _locks.setdefault(name, threading.RLock())
    with lock:
        held = getattr(_local, "held", set())
        if name in held:
            yield
            return
        with path.open("a+b") as handle:
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            _local.held = held | {name}
            try:
                yield
            finally:
                _local.held = held
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def locked(fn):
    @wraps(fn)
    def call(job_id, *args, **kwargs):
        with task_lock(job_id):
            return fn(job_id, *args, **kwargs)
    return call


def raw_page(job_id, doc_id, pdf_page):
    root(job_id)
    if not isinstance(doc_id, str) or not doc_id:
        raise SourceError("文档 ID 不能为空")
    if not isinstance(pdf_page, int) or isinstance(pdf_page, bool) or pdf_page < 1:
        raise SourceError("页码必须是正整数")
    matches = [p for p in core.read_jsonl(core.job_dir(job_id) / "parsed/pages.jsonl")
               if p["doc_id"] == doc_id and p["pdf_page"] == pdf_page]
    if len(matches) != 1:
        raise SourceError("文档页面不存在或不唯一", 404)
    return matches[0]


def check_pdf(job_id, page):
    docs = [d for d in core.load_job(job_id)["documents"] if d["doc_id"] == page["doc_id"]]
    if len(docs) != 1:
        raise SourceError("文档不存在", 404)
    doc = docs[0]
    path = core.job_dir(job_id) / "raw_pdf" / doc["filename"]
    if path.is_symlink() or not path.is_file() or core.sha256_file(path) != doc["sha256"]:
        raise SourceError("原始 PDF 缺失或哈希不匹配", 409)
    return doc["sha256"]


def joined(blocks):
    return "\n".join(b["text"] for b in blocks)


def _save_revision(job_id, state, page, blocks, kind, candidate_id, parent, pdf_sha, operation=None):
    revision_id = uuid.uuid4().hex
    if operation is not None:
        operation["new_revision"] = revision_id
    revision = {"schema_version": 1, "source_revision_id": revision_id,
                "parent_revision_id": parent, "source_candidate_id": candidate_id,
                "source_text_sha256": text_hash(joined(blocks)), "pdf_sha256": pdf_sha,
                "doc_id": page["doc_id"], "pdf_page": page["pdf_page"],
                "source_kind": kind, "blocks": copy.deepcopy(blocks), "created_at": core.now(),
                "location_precision": "block", "operation": operation}
    path = root(job_id) / "revisions" / f"{revision_id}.json"
    core.write_json(path, revision)
    state["revisions"][revision_id] = core.sha256_file(path)
    return revision


def revision(job_id, revision_id, state=None):
    state = state if state is not None else manifest(job_id)
    if revision_id not in state["revisions"]:
        raise SourceError("来源版本不存在", 404)
    path = root(job_id) / "revisions" / f"{revision_id}.json"
    if not path.is_file() or core.sha256_file(path) != state["revisions"][revision_id]:
        raise SourceError("来源版本内容校验失败", 409)
    value = core.read_json(path)
    if text_hash(joined(value["blocks"])) != value["source_text_sha256"]:
        raise SourceError("来源文字哈希不一致", 409)
    return value


def candidate(job_id, state, page, candidate_id):
    entry = state["pages"].get(page_key(page["doc_id"], page["pdf_page"]), {})
    matches = [c for c in entry.get("candidates", {}).values() if c.get("candidate_id") == candidate_id]
    if len(matches) != 1:
        raise SourceError("候选不属于当前任务、文档和页面", 404)
    meta = matches[0]
    if meta["status"] != "available":
        raise SourceError("该来源候选不可用")
    path = root(job_id) / "candidates" / f"{candidate_id}.json"
    if not path.is_file() or core.sha256_file(path) != meta["sha256"]:
        raise SourceError("候选内容哈希不匹配", 409)
    value = core.read_json(path)
    if (value["job_id"] != job_id or value["doc_id"] != page["doc_id"]
            or value["pdf_page"] != page["pdf_page"] or value["pdf_sha256"] != check_pdf(job_id, page)
            or value["source_text_sha256"] != text_hash(joined(value["blocks"]))):
        raise SourceError("候选来源身份不匹配", 409)
    return value


def register_parsed(job_id: str, pages: list[dict]) -> None:
    """Called only for a newly parsed task, before a source manifest exists."""
    state = manifest(job_id)
    if state["pages"]:
        raise SourceError("已有来源版本，不能重新解析", 409)
    for page in pages:
        candidates = {}
        pdf_sha = check_pdf(job_id, page)
        extraction = page.pop("_source_candidates")
        for kind, item in extraction.items():
            meta = {"status": item["status"], "error": item.get("error")}
            if item["status"] == "available":
                cid = uuid.uuid4().hex
                value = {"schema_version": 1, "job_id": job_id, "doc_id": page["doc_id"],
                         "pdf_page": page["pdf_page"], "pdf_sha256": pdf_sha, "candidate_id": cid,
                         "source_kind": kind, "blocks": item["blocks"],
                         "source_text_sha256": text_hash(joined(item["blocks"]))}
                path = root(job_id) / "candidates" / f"{cid}.json"
                core.write_json(path, value)
                meta.update(candidate_id=cid, sha256=core.sha256_file(path))
            candidates[kind] = meta
        kind = "ocr" if page["ocr_used"] and candidates["ocr"]["status"] == "available" else "native"
        cid = candidates[kind]["candidate_id"]
        rev = _save_revision(job_id, state, page, page["blocks"], kind, cid, None, pdf_sha)
        state["pages"][page_key(page["doc_id"], page["pdf_page"])] = {
            "current": rev["source_revision_id"], "candidates": candidates,
            "requires_verification": bool(page["ocr_used"] or page["possible_scanned"] or candidates["ocr"]["status"] == "failed"),
            "verified_revision": None}
    core.write_json(root(job_id) / "manifest.json", state)


def effective_pages(job_id: str, pages: list[dict]) -> list[dict]:
    state = manifest(job_id)
    for page in pages:
        entry = state["pages"].get(page_key(page["doc_id"], page["pdf_page"]))
        if entry:
            rev = revision(job_id, entry["current"], state)
            page.update(blocks=copy.deepcopy(rev["blocks"]), source_revision_id=entry["current"],
                        source_candidate_id=rev["source_candidate_id"], source_kind=rev["source_kind"],
                        source_verified=entry["verified_revision"] == entry["current"],
                        requires_verification=entry["requires_verification"])
            page["ocr_used"] = any(b.get("ocr_used", False) for b in page["blocks"])
            page.pop("semantic_blocks", None)
        else:
            page.update(source_revision_id=None, source_kind="legacy", source_verified=False,
                        requires_verification=False)
    return pages


def snapshot(job_id: str) -> dict:
    return {page_key(p["doc_id"], p["pdf_page"]): p.get("source_revision_id") or "legacy:" + digest(p["blocks"])
            for p in core.pages_for(job_id)}


def evidence_pages(unit):
    locations = unit.get("source_locations")
    return sorted({loc["pdf_page"] for loc in locations}) if locations else list(range(unit["pdf_page"], unit.get("end_pdf_page", unit["pdf_page"]) + 1))


def bind_evidence(job_id: str, unit: dict) -> dict:
    current = snapshot(job_id)
    unit["source_revisions"] = {page_key(unit["doc_id"], n): current[page_key(unit["doc_id"], n)] for n in evidence_pages(unit)}
    unit["source_location_precision"] = "block"
    return unit


def evidence_issues(job_id, unit, state=None, current=None):
    state = state if state is not None else manifest(job_id)
    current = current if current is not None else snapshot(job_id)
    issues = []
    for n in evidence_pages(unit):
        key = page_key(unit["doc_id"], n)
        entry = state["pages"].get(key)
        bound = unit.get("source_revisions", {}).get(key)
        if key not in current or (bound != current[key] and (bound is not None or entry is not None)):
            issues.append("证据来源已过期，需按当前版本重建")
        if entry and entry["requires_verification"] and entry["verified_revision"] != entry["current"]:
            issues.append("当前来源版本待人工核实")
    return list(dict.fromkeys(issues))


def annotate_evidence(job_id, units):
    state, current = manifest(job_id), snapshot(job_id)
    for unit in units:
        unit["source_issues"] = evidence_issues(job_id, unit, state, current)
    return units


def require_evidence(job_id, units):
    for unit in units:
        issues = evidence_issues(job_id, unit)
        if issues:
            raise SourceError("；".join(issues), 409)


def normalized(text):
    chars, offsets = [], []
    for index, char in enumerate(text):
        if not char.isspace():
            chars.append(char)
            offsets.append(index)
    return "".join(chars), offsets


def compare(left_blocks: list[dict], right_blocks: list[dict]) -> dict:
    left, right = joined(left_blocks), joined(right_blocks)
    a, amap = normalized(left)
    b, bmap = normalized(right)
    matcher = SequenceMatcher(None, a, b, autojunk=False)

    def fragment(text, mapping, start, end, blocks):
        lo = mapping[start] if start < len(mapping) else len(text)
        hi = mapping[end - 1] + 1 if end > start else lo
        locations, offset = [], 0
        for block in blocks:
            stop = offset + len(block["text"])
            if (lo < stop and hi > offset) or (lo == hi and offset <= lo <= stop):
                locations.append({"block_id": block["block_id"], "bbox": block["bbox"], "precision": "block"})
            offset = stop + 1
        return {"text": text[lo:hi], "raw_start": lo, "raw_end": hi,
                "normalized_start": start, "normalized_end": end,
                "locations": locations, "precision": "block" if locations else "page"}

    changes = []
    for tag, i, j, k, l in matcher.get_opcodes():
        if tag == "equal":
            continue
        left_part, right_part = fragment(left, amap, i, j, left_blocks), fragment(right, bmap, k, l, right_blocks)
        value = left_part["text"] + right_part["text"]
        flags = []
        for name, pattern in (("number", r"\d"), ("unit", r"(?:mm|cm|km|kg|m²|m³|℃|°C|米|毫米|厘米|千米|公斤|小时|分钟|秒|%|％)"),
                              ("negation", r"(?:不|未|无|禁止|不得|严禁|否|\bnot\b|\bno\b|\bnever\b)")):
            if re.search(pattern, value, re.I):
                flags.append(name)
        context = left[max(0, left_part["raw_start"] - 8):left_part["raw_end"] + 8] + " " + right[max(0, right_part["raw_start"] - 8):right_part["raw_end"] + 8]
        if "unit" not in flags and re.search(r"(?:\d\s*(?:mm|cm|km|kg|m|s|h|%|米|秒)|℃|°C|²|³)", context, re.I):
            flags.append("unit")
        changes.append({"type": tag, "left": left_part, "right": right_part, "highlights": flags})
    return {"difference_rate": 1 - matcher.ratio(), "left_normalized": a, "right_normalized": b,
            "left_offsets": amap, "right_offsets": bmap, "changes": changes}


@locked
def get_page_source_comparison(job_id: str, doc_id: str, pdf_page: int) -> dict:
    page = raw_page(job_id, doc_id, pdf_page)
    check_pdf(job_id, page)
    state = manifest(job_id)
    entry = state["pages"].get(page_key(doc_id, pdf_page))
    candidates = {}
    if entry:
        rev = revision(job_id, entry["current"], state)
        for kind, meta in entry["candidates"].items():
            candidates[kind] = dict(meta)
            if meta["status"] == "available":
                candidates[kind]["blocks"] = candidate(job_id, state, page, meta["candidate_id"])["blocks"]
    else:
        rev = {"source_revision_id": None, "source_kind": "legacy", "source_candidate_id": None,
               "blocks": page["blocks"], "source_text_sha256": text_hash(joined(page["blocks"]))}
    diff = None
    if all(candidates.get(k, {}).get("status") == "available" for k in ("native", "ocr")):
        diff = compare(candidates["native"]["blocks"], candidates["ocr"]["blocks"])
    verified = bool(entry and entry["verified_revision"] == entry["current"])
    structural_risks = list(dict.fromkeys(issue for unit in core.semantic_blocks_for_document([p for p in core.pages_for(job_id) if p["doc_id"] == doc_id]) if unit["pdf_page"] <= pdf_page <= unit["end_pdf_page"] for issue in unit["quality_issues"]))
    return {"doc_id": doc_id, "pdf_page": pdf_page, "width": page.get("width"), "height": page.get("height"),
            "current": rev, "candidates": candidates, "comparison": diff,
            "legacy_token": "legacy:" + digest(page["blocks"]) if not entry else None,
            "missing_historical_candidates": not bool(candidates), "source_verified": verified,
            "requires_verification": bool(entry and entry["requires_verification"]),
            "risks": ([] if verified else ["当前来源未核实"]) + (["OCR 生成失败：" + str(candidates["ocr"].get("error", ""))] if candidates.get("ocr", {}).get("status") == "failed" else []) + (["少文本页，请核对原 PDF"] if page["possible_scanned"] else []) + structural_risks,
            "frozen": (core.job_dir(job_id) / "evidence/splits.json").exists()}


def get_source_change_impact(job_id: str, doc_id: str, pdf_page: int) -> dict:
    units = [u for u in core.evidence_for(job_id) if u["doc_id"] == doc_id and pdf_page in evidence_pages(u)]
    ids = {u["evidence_unit_id"] for u in units}
    records = [r["sample_id"] for r in core.candidates_for(job_id) if any(e["evidence_unit_id"] in ids for e in r["evidence"])]
    semantics = [s["semantic_id"] for s in core.semantic_blocks_for_document([p for p in core.pages_for(job_id) if p["doc_id"] == doc_id])
                 if s["pdf_page"] <= pdf_page <= s["end_pdf_page"]]
    p = core.job_dir(job_id)
    return {"precision": "page", "policy": "页级保守失效（含跨页语义单元）", "semantic_ids": semantics,
            "evidence_ids": sorted(ids), "sample_ids": records,
            "evidence_count": len(ids), "sample_count": len(records),
            "txt": (p / "parsed/semantic_corpus.txt").is_file(),
            "exports": [f.name for f in (p / "exports").glob("*.zip")]}


def _prepare(job_id, payload, operation_type):
    doc_id, number = payload.get("doc_id"), payload.get("pdf_page")
    page = raw_page(job_id, doc_id, number)
    pdf_sha = check_pdf(job_id, page)
    if (core.job_dir(job_id) / "evidence/splits.json").exists():
        raise SourceError("分区已冻结，请复制任务后修改来源", 403)
    state = manifest(job_id)
    entry = state["pages"].get(page_key(doc_id, number))
    expected = entry["current"] if entry else "legacy:" + digest(page["blocks"])
    if payload.get("expected_revision") != expected:
        raise SourceError("来源版本已变化，请保留草稿并重新载入比较", 409)
    current = revision(job_id, entry["current"], state) if entry else None
    if current and current["pdf_sha256"] != pdf_sha:
        raise SourceError("来源版本与原始 PDF 身份不一致", 409)
    before = current["blocks"] if current else page["blocks"]
    blocks = copy.deepcopy(before)
    kind, cid = (current["source_kind"], current["source_candidate_id"]) if current else ("legacy", None)
    if operation_type == "initialize":
        if entry:
            raise SourceError("页面已有来源版本", 409)
    elif not entry:
        raise SourceError("缺少历史提取候选，请先显式建立来源基线", 409)
    elif operation_type == "select_source":
        chosen = candidate(job_id, state, page, payload.get("candidate_id"))
        blocks, kind, cid = chosen["blocks"], chosen["source_kind"], chosen["candidate_id"]
    elif operation_type == "correct_text":
        if payload.get("candidate_id") != cid:
            raise SourceError("校正目标来源候选已变化", 409)
        if cid is not None:
            candidate(job_id, state, page, cid)
        found = [b for b in blocks if b["block_id"] == payload.get("block_id")]
        if len(found) != 1:
            raise SourceError("原始块不存在或定位不唯一", 404)
        text = payload.get("corrected_text")
        if not isinstance(text, str) or not text.strip():
            raise SourceError("校正文字不能为空，不能隐式排除内容")
        found[0]["text"] = text
        kind = "manual" if blocks != before else kind
    elif operation_type != "verify":
        raise SourceError("无效来源操作")
    return state, entry, page, current, before, blocks, kind, cid, pdf_sha


@locked
def preview_source_change(job_id: str, payload: dict) -> dict:
    operation_type = payload.get("operation_type")
    _, _, page, current, before, blocks, kind, cid, _ = _prepare(job_id, payload, operation_type)
    return {"base_revision": current["source_revision_id"] if current else None,
            "source_kind": kind, "source_candidate_id": cid, "blocks": blocks,
            "comparison": compare(before, blocks),
            "impact": get_source_change_impact(job_id, page["doc_id"], page["pdf_page"])}


@locked
def commit(job_id: str, payload: dict, operation_type: str) -> dict:
    operation_id = payload.get("operation_id")
    if not isinstance(operation_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", operation_id):
        raise SourceError("必须提供合法操作 ID")
    if operation_type == "verify" and str(payload.get("reviewer", "")).startswith("model:"):
        raise SourceError("来源核实必须由人工完成")
    if any(not isinstance(payload.get(k), str) or not payload[k].strip() for k in ("reviewer", "reason")):
        raise SourceError("操作人和处理依据不能为空")
    request_hash = digest({"operation_type": operation_type, "payload": payload})
    old = manifest(job_id)["operations"].get(operation_id)
    if old:
        if old["request_sha256"] != request_hash:
            raise SourceError("操作 ID 已用于其他内容", 409)
        return old["result"]
    state, entry, page, current, before, blocks, kind, cid, pdf_sha = _prepare(job_id, payload, operation_type)
    base = current["source_revision_id"] if current else None
    changed = operation_type == "initialize" or (operation_type != "verify" and (blocks != before or kind != current["source_kind"] or cid != current["source_candidate_id"]))
    impact = get_source_change_impact(job_id, page["doc_id"], page["pdf_page"])
    operation = {"operation_id": operation_id, "operation_type": operation_type,
                 "doc_id": page["doc_id"], "pdf_page": page["pdf_page"], "base_revision": base,
                 "source_candidate_id": cid, "block_id": payload.get("block_id"),
                 "before_text_sha256": text_hash(joined(before)), "after_text_sha256": text_hash(joined(blocks)),
                 "reviewer": payload["reviewer"].strip(), "reason": payload["reason"].strip(), "created_at": core.now()}
    if changed:
        if operation_type == "correct_text":
            bid = payload["block_id"]
            old_text = next(b["text"] for b in before if b["block_id"] == bid)
            new_text = next(b["text"] for b in blocks if b["block_id"] == bid)
            operation.update(before_text=old_text, after_text=new_text, location_precision="block",
                             raw_text_mapping=[{"type": tag, "before_start": i, "before_end": j, "after_start": k, "after_end": l, "pdf_precision": "block"} for tag, i, j, k, l in SequenceMatcher(None, old_text, new_text, autojunk=False).get_opcodes()],
                             text_mapping=compare([{"text": old_text, "block_id": bid, "bbox": next(b["bbox"] for b in before if b["block_id"] == bid)}],
                                                  [{"text": new_text, "block_id": bid, "bbox": next(b["bbox"] for b in blocks if b["block_id"] == bid)}]))
            operation["before_text_sha256"], operation["after_text_sha256"] = text_hash(old_text), text_hash(new_text)
        rev = _save_revision(job_id, state, page, blocks, kind, cid, base, pdf_sha, operation)
        entry = dict(entry or {"candidates": {}})
        entry.update(current=rev["source_revision_id"], requires_verification=True, verified_revision=None)
        state["pages"][page_key(page["doc_id"], page["pdf_page"])] = entry
    elif operation_type == "verify":
        entry["verified_revision"] = base
    new_id = entry["current"]
    operation["new_revision"] = new_id
    result = {"changed": changed, "current_revision": new_id, "operation_id": operation_id,
              "summary": "来源已核实" if operation_type == "verify" else "已保存新来源版本" if changed else "内容及来源未变化",
              "impact": impact, "next_step": "重新建立受影响证据，并主动重新生成 TXT 和导出" if operation_type == "verify" else "对照 PDF 核实当前版本；本操作不代表完成核实"}
    state["operations"][operation_id] = {"request_sha256": request_hash, "operation": operation, "result": result}
    core.write_json(root(job_id) / "manifest.json", state)
    return result


def artifact_status(job_id, path, metadata):
    exists = path.is_file()
    current = snapshot(job_id)
    has_versions = bool(manifest(job_id)["pages"])
    stale = exists and (metadata.get("source_snapshot") != current if has_versions or "source_snapshot" in metadata else False)
    if exists and metadata.get("sha256"):
        stale |= metadata["sha256"] != core.sha256_file(path)
    if exists and path.suffix == ".zip":
        p = core.job_dir(job_id)
        stale |= any(metadata.get(f"{name}_sha256") != (core.sha256_file(p / file) if (p / file).is_file() else None)
                     for name, file in (("candidates", "candidates/records.jsonl"), ("evidence", "evidence/units.jsonl")))
    return {"exists": exists, "stale": bool(stale), "ready": exists and not stale,
            "size_bytes": path.stat().st_size if exists else 0}


def require_artifact(job_id, path, metadata):
    status = artifact_status(job_id, path, metadata)
    if not status["exists"]:
        raise SourceError("成果尚未生成", 404)
    if status["stale"]:
        raise SourceError("成果来源已过期，请主动重新生成", 409)
    return path


def archive_artifact(path):
    if path.is_file():
        folder = path.parent / "history" / uuid.uuid4().hex
        folder.mkdir(parents=True)
        shutil.copy2(path, folder / path.name)
        sidecar = path.with_suffix(".sha256.json")
        if sidecar.is_file():
            shutil.copy2(sidecar, folder / sidecar.name)
        return folder


def build_fingerprint(job_id):
    p = core.job_dir(job_id)
    result = {"source_snapshot": snapshot(job_id)}
    for name, relative in (("candidates", "candidates/records.jsonl"), ("evidence", "evidence/units.jsonl"),
                           ("source_manifest", "parsed/sources/manifest.json"), ("splits", "evidence/splits.json")):
        path = p / relative
        result[name + "_sha256"] = core.sha256_file(path) if path.is_file() else None
    return result
