from __future__ import annotations

import sys

import subprocess
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.responses import Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from aviation_ft import core, generation, providers, workflow
import pymupdf as fitz

STATIC_DIR = Path(__file__).with_name("static")


def response(data, status_code: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status_code, headers={"Cache-Control": "no-store"})


async def index(_: Request) -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


async def jobs(request: Request) -> JSONResponse:
    if request.method == "GET":
        return response(core.list_jobs())
    payload = await request.json()
    job_id = core.create_job(str(payload.get("label", "")))
    return response({"job_id": job_id}, 201)


def automation_running(job_id: str) -> bool:
    session = f"aviation-auto-{job_id[:8]}"
    result = subprocess.run(["screen", "-ls"], capture_output=True, text=True, check=False)
    return f".{session}" in result.stdout


async def dashboard(request: Request) -> JSONResponse:
    job_id = request.path_params["job_id"]
    meta = core.load_job(job_id)
    progress = workflow.task_progress(job_id)
    quality = core.quality_report(job_id)
    queue = core.read_jsonl(core.job_dir(job_id) / "evidence/human_queue.jsonl")
    auto_log = core.read_jsonl(core.job_dir(job_id) / "automation/progress.jsonl")
    txt_path = core.job_dir(job_id) / "parsed/semantic_corpus.txt"
    return response({
        "meta": meta,
        "progress": progress,
        "quality": quality,
        "human_queue_count": len(queue),
        "automation_running": automation_running(job_id),
        "latest_automation": auto_log[-1] if auto_log else None,
        "provider": providers.public_config(job_id),
        "provider_presets": providers.provider_presets(),
        "exports": {
            kind: {"ready": core.export_is_current(job_id, path) if path.is_file() else False,
                   "historical_snapshot": path.is_file() and not core.export_is_current(job_id, path),
                   "size_bytes": path.stat().st_size if path.is_file() else 0}
            for kind in ("preview", "final", "preview-trace", "final-trace")
            for path in [core.job_dir(job_id) / "exports" / f"aviation-sft-{kind}.zip"]
        },
        "semantic_txt": {
            "ready": txt_path.is_file() and core.txt_is_current(job_id),
            "size_bytes": txt_path.stat().st_size if txt_path.is_file() else 0,
        },
        "page_quality": core.read_jsonl(core.job_dir(job_id) / "parsed/page_quality.jsonl"),
    })


async def provider_config(request: Request) -> JSONResponse:
    job_id = request.path_params["job_id"]
    role = request.query_params.get("role", "generator")
    if request.method == "GET":
        return response(providers.public_config(job_id, role))
    if request.method == "DELETE":
        return response(providers.use_local(job_id, role))
    payload = await request.json()
    result = providers.save_external(
        job_id,
        str(payload.get("base_url", "")),
        str(payload.get("model", "")),
        str(payload.get("api_key", "")),
        bool(payload.get("privacy_acknowledged", False)),
        str(payload.get("preset_id", "")),
        role,
    )
    return response(result)


async def test_provider(request: Request) -> JSONResponse:
    job_id = request.path_params["job_id"]
    role = request.query_params.get("role", "generator")
    result = await run_in_threadpool(generation.probe, job_id, role)
    return response({
        "ready": result["ready"],
        "model": result["served_model_name"],
        "mode": providers.public_config(job_id, role)["mode"], "role": role,
    })


async def upload_pdf(request: Request) -> JSONResponse:
    job_id = request.path_params["job_id"]
    form = await request.form()
    uploads = form.getlist("files")
    if not uploads:
        raise ValueError("请选择至少一个 PDF")
    saved = []
    for upload in uploads:
        saved.append(await run_in_threadpool(core.upload_pdf, job_id, upload.filename, upload.file))
    return response({"saved": saved}, 201)


async def remove_document(request: Request) -> JSONResponse:
    job_id = request.path_params["job_id"]
    payload = await request.json()
    core.remove_unparsed_pdf(job_id, str(payload.get("filename", "")))
    return response({"ok": True})


async def parse_documents(request: Request) -> JSONResponse:
    result = await run_in_threadpool(core.parse_job, request.path_params["job_id"])
    return response(result)


async def start_automation(request: Request) -> JSONResponse:
    job_id = request.path_params["job_id"]
    core.job_dir(job_id)
    session = f"aviation-auto-{job_id[:8]}"
    if automation_running(job_id):
        return response({"running": True, "session": session})
    subprocess.run([
        "screen", "-dmS", session,
        sys.executable, "-m", "aviation_ft.cli",
        "autopilot", job_id, "--until-ready",
    ], check=True)
    return response({"running": True, "session": session}, 202)


async def evidence_overview(request: Request) -> JSONResponse:
    job_id = request.path_params["job_id"]
    queue = core.read_jsonl(core.job_dir(job_id) / "evidence/human_queue.jsonl")
    units = core.evidence_for(job_id)
    pages = core.pages_for(job_id)
    page_index = [
        {
            "doc_id": page["doc_id"],
            "filename": page["filename"],
            "pdf_page": page["pdf_page"],
            "ocr_used": bool(page.get("ocr_used")),
            "possible_scanned": bool(page.get("possible_scanned")),
            "blocks": len(page.get("blocks", [])),
            "quality_status": page.get("quality_status", "legacy_unknown"),
            "quality_issue_ids": page.get("quality_issue_ids", []),
        }
        for page in pages
    ]
    return response({
        "queue": queue,
        "units": units,
        "pages": page_index,
        "frozen": (core.job_dir(job_id) / "evidence/splits.json").exists(),
        "quality_issues": core.read_jsonl(core.job_dir(job_id) / "parsed/issues.jsonl"),
    })


async def page_detail(request: Request) -> JSONResponse:
    job_id = request.path_params["job_id"]
    doc_id = request.query_params.get("doc_id", "")
    try:
        page_no = int(request.query_params.get("page", "0"))
    except ValueError as exc:
        raise ValueError("页码无效") from exc
    pages = core.pages_for(job_id)
    matches = [
        page for page in pages
        if page["doc_id"] == doc_id and page["pdf_page"] == page_no
    ]
    if len(matches) != 1:
        raise ValueError("文档页面不存在")
    result = dict(matches[0])
    previous_page = next((
        page for page in pages
        if page["doc_id"] == doc_id and page["pdf_page"] == page_no - 1
    ), None)
    result["semantic_blocks"] = [
        unit for unit in core.semantic_blocks_for_document([
            page for page in pages if page["doc_id"] == doc_id
        ], core.confirmed_merge_pairs(job_id))
        if unit["pdf_page"] <= page_no <= unit["end_pdf_page"]
    ]
    return response(result)


async def source_pdf(request: Request) -> FileResponse:
    job_id = request.path_params["job_id"]
    doc_id = request.path_params["doc_id"]
    meta = core.load_job(job_id)
    document = next((item for item in meta["documents"] if item["doc_id"] == doc_id), None)
    if not document:
        raise ValueError("文档不存在")
    path = core.job_dir(job_id) / "raw_pdf" / document["filename"]
    if path.is_symlink() or not path.is_file() or core.sha256_file(path) != document["sha256"]:
        raise ValueError("原始 PDF 不可用")
    return FileResponse(path, media_type="application/pdf")


async def add_evidence(request: Request) -> JSONResponse:
    job_id = request.path_params["job_id"]
    payload = await request.json()
    record = core.add_evidence(
        job_id,
        str(payload.get("block_id", "")),
        str(payload.get("topic", "")),
        str(payload.get("clause_group", "")),
        str(payload.get("split", "")),
        str(payload.get("quote", "")) or None,
    )
    return response(record, 201)


async def evidence_suggestions(request: Request) -> JSONResponse:
    job_id = request.path_params["job_id"]
    doc_id = request.query_params.get("doc_id", "")
    return response(core.evidence_suggestions(job_id, doc_id))


async def add_evidence_batch(request: Request) -> JSONResponse:
    job_id = request.path_params["job_id"]
    payload = await request.json()
    saved = core.add_evidence_batch(
        job_id,
        list(payload.get("items", [])),
        str(payload.get("reviewer", "")),
    )
    return response({"saved": len(saved)}, 201)


async def cross_page_proposals(request: Request) -> JSONResponse:
    return response(core.cross_page_merge_proposals(request.path_params["job_id"]))


async def confirm_cross_page(request: Request) -> JSONResponse:
    payload = await request.json()
    return response(core.add_evidence_cross_page(
        request.path_params["job_id"], str(payload.get("start_semantic_id", "")),
        str(payload.get("end_semantic_id", "")), str(payload.get("topic", "")),
        str(payload.get("clause_group", "")), str(payload.get("split", "")),
        str(payload.get("reviewer", "")), str(payload.get("reason", "")),
        str(payload.get("quote", "")) or None), 201)


async def freeze_evidence(request: Request) -> JSONResponse:
    return response(core.freeze_splits(request.path_params["job_id"]))


async def verify_evidence(request: Request) -> JSONResponse:
    payload = await request.json()
    return response(core.verify_evidence_source(request.path_params["job_id"],
        request.path_params["evidence_unit_id"], str(payload.get("reviewer", "")),
        str(payload.get("reason", ""))))


async def resolve_issue(request: Request) -> JSONResponse:
    payload = await request.json()
    return response(core.resolve_quality_issue(request.path_params["job_id"],
        request.path_params["issue_id"], str(payload.get("status", "")),
        str(payload.get("reviewer", "")), str(payload.get("reason", ""))))


async def candidates(request: Request) -> JSONResponse:
    job_id = request.path_params["job_id"]
    try:
        page = max(1, int(request.query_params.get("page", "1")))
        page_size = int(request.query_params.get("page_size", "25"))
    except ValueError as exc:
        raise ValueError("分页参数无效") from exc
    if not 1 <= page_size <= 100:
        raise ValueError("每页只能显示 1 至 100 条")
    units = core.current_evidence_units(job_id)
    queue = workflow.review_queue(core.candidates_for(job_id), units, include_all=True)
    status = request.query_params.get("status", "all")
    doc_id = request.query_params.get("doc_id", "")
    risk = request.query_params.get("risk", "")
    queue = [item for item in queue
             if (status == "all" or item["record"]["review"]["status"] == status)
             and (not doc_id or any(ref["doc_id"] == doc_id for ref in item["record"]["evidence"]))
             and (not risk or risk in item["flags"])]
    total = len(queue)
    queue = queue[(page - 1) * page_size:page * page_size]
    items = []
    for item in queue:
        record = item["record"]
        items.append({
            "sample_id": record["sample_id"],
            "split": record["split"],
            "question": record["question"],
            "answer": record["answer"],
            "answerability": record["answerability"],
            "review_status": record["review"]["status"],
            "flags": item["flags"],
            "errors": item["errors"],
            "record_revision": record.get("record_revision", 1),
            "evidence": [ref.get("quote", "") for ref in record.get("evidence", [])],
        })
    return response({"items": items, "total": total, "page": page, "page_size": page_size})


async def candidate_detail(request: Request) -> JSONResponse:
    job_id = request.path_params["job_id"]
    sample_id = request.path_params["sample_id"]
    record = next((r for r in core.candidates_for(job_id) if r["sample_id"] == sample_id), None)
    if not record:
        raise ValueError("候选不存在")
    units = {u["evidence_unit_id"]: u for u in core.current_evidence_units(job_id)}
    item = next(x for x in workflow.review_queue([record], list(units.values()), include_all=True))
    return response({**record, "validation_errors": item["errors"],
        "quality_flags": item["flags"],
        "review_history": [event for event in core.read_jsonl(core.job_dir(job_id) / "reviewed/events.jsonl")
                           if event.get("sample_id") == sample_id],
        "source_units": [units.get(ref["evidence_unit_id"]) for ref in record["evidence"]]})


async def review_one(request: Request) -> JSONResponse:
    payload = await request.json()
    if type(payload.get("expected_revision")) is not int:
        raise ValueError("审核请求缺少候选版本 expected_revision")
    return response(core.review_candidate(request.path_params["job_id"],
        request.path_params["sample_id"], str(payload.get("decision", "")),
        str(payload.get("reviewer", "")), str(payload.get("comment", "")),
        expected_revision=payload.get("expected_revision"),
        claim_reviews=payload.get("claim_reviews"), source_reviews=payload.get("source_reviews"),
        numeric_review=payload.get("numeric_review")))


async def review_batch(request: Request) -> JSONResponse:
    job_id = request.path_params["job_id"]
    payload = await request.json()
    if not isinstance(payload.get("expected_revisions"), dict):
        raise ValueError("批量审核请求缺少各候选 expected_revisions")
    result = core.review_candidates_batch(
        job_id,
        list(payload.get("sample_ids", [])),
        str(payload.get("reviewer", "")),
        str(payload.get("decision", "approved")),
        str(payload.get("comment", "")),
        expected_revisions=payload.get("expected_revisions"),
        claim_reviews=payload.get("claim_reviews"), source_reviews=payload.get("source_reviews"),
        numeric_reviews=payload.get("numeric_reviews"),
    )
    return response(result)


async def page_image(request: Request) -> Response:
    job_id = request.path_params["job_id"]
    doc_id = request.path_params["doc_id"]
    page_no = int(request.path_params["page_no"])
    document = next((d for d in core.load_job(job_id)["documents"] if d["doc_id"] == doc_id), None)
    if not document:
        raise ValueError("文档不存在")
    path = core.job_dir(job_id) / "raw_pdf" / document["filename"]
    if core.sha256_file(path) != document["sha256"]:
        raise ValueError("原始 PDF 哈希不符")
    with fitz.open(path) as pdf:
        if not 1 <= page_no <= len(pdf):
            raise ValueError("PDF 页码无效")
        pix = pdf[page_no - 1].get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
        return Response(pix.tobytes("png"), media_type="image/png")


async def page_geometry(request: Request) -> JSONResponse:
    job_id = request.path_params["job_id"]
    doc_id = request.path_params["doc_id"]
    page_no = int(request.path_params["page_no"])
    document = next((d for d in core.load_job(job_id)["documents"] if d["doc_id"] == doc_id), None)
    if not document:
        raise ValueError("文档不存在")
    path = core.job_dir(job_id) / "raw_pdf" / document["filename"]
    if core.sha256_file(path) != document["sha256"]:
        raise ValueError("原始 PDF 哈希不符")
    with fitz.open(path) as pdf:
        if not 1 <= page_no <= len(pdf):
            raise ValueError("PDF 页码无效")
        page = pdf[page_no - 1]
        locations = []
        for unit in core.evidence_for(job_id):
            if unit["doc_id"] != doc_id:
                continue
            for loc in unit.get("source_locations", []):
                if loc["pdf_page"] == page_no:
                    rect = fitz.Rect(loc["bbox"]) * page.rotation_matrix
                    locations.append({"evidence_unit_id": unit["evidence_unit_id"],
                        "block_id": loc["block_id"], "precision": "block",
                        "rect": [rect.x0 / page.rect.width, rect.y0 / page.rect.height,
                                 rect.width / page.rect.width, rect.height / page.rect.height]})
        return response({"rotation": page.rotation, "width": page.rect.width,
                         "height": page.rect.height, "locations": locations})


async def export_dataset(request: Request) -> JSONResponse:
    job_id = request.path_params["job_id"]
    payload = await request.json()
    final = bool(payload.get("final", False))
    path, digest, report = await run_in_threadpool(core.export_job, job_id, final)
    return response({
        "kind": "final" if final else "preview",
        "filename": path.name,
        "sha256": digest,
        "quality": report,
        "trace_filename": path.stem + "-trace.zip",
    })


async def download_export(request: Request) -> FileResponse:
    job_id = request.path_params["job_id"]
    kind = request.path_params["kind"]
    if kind not in {"final", "preview", "final-trace", "preview-trace"}:
        raise ValueError("导出类型无效")
    path = core.job_dir(job_id) / "exports" / f"aviation-sft-{kind}.zip"
    if not path.is_file():
        raise ValueError("数据包尚未生成")
    if not core.export_is_current(job_id, path):
        raise ValueError("导出包是历史快照；请重新生成当前版本")
    return FileResponse(path, media_type="application/zip", filename=path.name)

async def semantic_txt(request: Request):
    job_id = request.path_params["job_id"]
    path = core.job_dir(job_id) / "parsed/semantic_corpus.txt"
    if request.method == "POST":
        return response(await run_in_threadpool(core.build_semantic_txt, job_id))
    if not core.txt_is_current(job_id):
        raise ValueError("整体 TXT 尚未生成或是历史版本；请重新生成")
    return FileResponse(
        path, media_type="text/plain; charset=utf-8",
        filename=f"{job_id}-semantic-corpus.txt",
    )


async def delete_task(request: Request) -> JSONResponse:
    job_id = request.path_params["job_id"]
    if automation_running(job_id):
        raise ValueError("自动构建正在运行，不能删除任务")
    payload = await request.json()
    result = await run_in_threadpool(
        core.delete_job, job_id, str(payload.get("confirmation", "")),
    )
    return response(result)


async def cleanup(request: Request) -> JSONResponse:
    job_id = request.path_params["job_id"]
    payload = await request.json()
    core.cleanup_job(
        job_id,
        str(payload.get("confirmation", "")),
        str(payload.get("sha256", "")),
    )
    return response({"ok": True})


async def value_error(_: Request, exc: ValueError) -> JSONResponse:
    return response({"error": str(exc)}, 400)


async def revision_conflict(_: Request, exc: core.RevisionConflict) -> JSONResponse:
    return response({"error": str(exc)}, 409)


async def unexpected_error(_: Request, exc: Exception) -> JSONResponse:
    return response({"error": f"服务器操作失败：{exc}"}, 500)


routes = [
    Route("/", index),
    Route("/api/jobs", jobs, methods=["GET", "POST"]),
    Route("/api/jobs/{job_id}", dashboard, methods=["GET"]),
    Route("/api/jobs/{job_id}", delete_task, methods=["DELETE"]),
    Route("/api/jobs/{job_id}/provider", provider_config, methods=["GET", "POST", "DELETE"]),
    Route("/api/jobs/{job_id}/provider/test", test_provider, methods=["POST"]),
    Route("/api/jobs/{job_id}/upload", upload_pdf, methods=["POST"]),
    Route("/api/jobs/{job_id}/remove-document", remove_document, methods=["POST"]),
    Route("/api/jobs/{job_id}/parse", parse_documents, methods=["POST"]),
    Route("/api/jobs/{job_id}/semantic-txt", semantic_txt, methods=["GET", "POST"]),
    Route("/api/jobs/{job_id}/autopilot", start_automation, methods=["POST"]),
    Route("/api/jobs/{job_id}/evidence", evidence_overview, methods=["GET"]),
    Route("/api/jobs/{job_id}/evidence/item", add_evidence, methods=["POST"]),
    Route("/api/jobs/{job_id}/evidence/suggestions", evidence_suggestions, methods=["GET"]),
    Route("/api/jobs/{job_id}/evidence/batch", add_evidence_batch, methods=["POST"]),
    Route("/api/jobs/{job_id}/evidence/cross-page-proposals", cross_page_proposals, methods=["GET"]),
    Route("/api/jobs/{job_id}/evidence/cross-page-confirm", confirm_cross_page, methods=["POST"]),
    Route("/api/jobs/{job_id}/evidence/freeze", freeze_evidence, methods=["POST"]),
    Route("/api/jobs/{job_id}/evidence/{evidence_unit_id}/verify", verify_evidence, methods=["POST"]),
    Route("/api/jobs/{job_id}/quality-issues/{issue_id}", resolve_issue, methods=["POST"]),
    Route("/api/jobs/{job_id}/page", page_detail, methods=["GET"]),
    Route("/api/jobs/{job_id}/pdf/{doc_id}", source_pdf, methods=["GET"]),
    Route("/api/jobs/{job_id}/candidates", candidates, methods=["GET"]),
    Route("/api/jobs/{job_id}/candidates/{sample_id}", candidate_detail, methods=["GET"]),
    Route("/api/jobs/{job_id}/candidates/{sample_id}/review", review_one, methods=["POST"]),
    Route("/api/jobs/{job_id}/page-image/{doc_id}/{page_no}", page_image, methods=["GET"]),
    Route("/api/jobs/{job_id}/page-geometry/{doc_id}/{page_no}", page_geometry, methods=["GET"]),
    Route("/api/jobs/{job_id}/review-batch", review_batch, methods=["POST"]),
    Route("/api/jobs/{job_id}/export", export_dataset, methods=["POST"]),
    Route("/api/jobs/{job_id}/exports/{kind}", download_export, methods=["GET"]),
    Route("/api/jobs/{job_id}/cleanup", cleanup, methods=["POST"]),
    Mount("/static", StaticFiles(directory=STATIC_DIR), name="static"),
]

app = Starlette(
    debug=False,
    routes=routes,
    exception_handlers={core.RevisionConflict: revision_conflict, ValueError: value_error,
                        Exception: unexpected_error},
)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8501, log_level="info")

