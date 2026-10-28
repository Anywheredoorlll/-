from __future__ import annotations

import asyncio
import copy
import io
import json
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pymupdf as fitz
import pytest

from aviation_ft import core, readable_txt, source_revision as sources


@pytest.fixture
def source_job(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "ROOT", tmp_path)
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs/settings.json").write_text(json.dumps({
        "max_file_mb": 10, "max_job_mb": 20, "min_free_gb_after_upload": 0,
        "generator_model_dir": "models/generator", "completed_job_retention_days": 7,
    }), encoding="utf-8")
    job = core.create_job("sources")
    with fitz.open() as pdf:
        for n in range(3):
            page = pdf.new_page()
            page.insert_text((72, 100), f"1.0.{n + 1} Aircraft must not exceed 10 km under this condition.")
        core.upload_pdf(job, "source.pdf", io.BytesIO(pdf.tobytes()))
    doc = core.load_job(job)["documents"][0]
    pages = []
    for n in range(1, 4):
        native = [{"block_id": f"{doc['doc_id']}-p{n:04d}-b0001", "bbox": [72, 72, 500, 120],
                   "text": f"1.0.{n} Aircraft must not exceed 10 km under this condition.", "ocr_used": False}]
        ocr = copy.deepcopy(native)
        ocr[0].update(text=f"1.0.{n} Aircraft must exceed 70 m under this condition.", ocr_used=True)
        pages.append({"doc_id": doc["doc_id"], "filename": doc["filename"], "pdf_page": n,
                      "printed_page": None, "width": 595, "height": 842, "blocks": native,
                      "ocr_used": False, "possible_scanned": False,
                      "_source_candidates": {"native": {"status": "available", "blocks": native},
                                             "ocr": {"status": "available", "blocks": ocr}}})
    core.write_jsonl(core.job_dir(job) / "parsed/pages.jsonl", [{k: v for k, v in p.items() if k != "_source_candidates"} for p in pages])
    sources.register_parsed(job, pages)
    core.write_json(core.job_dir(job) / "parsed/report.json", {"pages": 3, "blocks": 3, "possible_scanned_pages": 0})
    return job


def comparison(job, page=1):
    return sources.get_page_source_comparison(job, core.load_job(job)["documents"][0]["doc_id"], page)


def payload(job, page=1, **changes):
    data = comparison(job, page)
    result = {"doc_id": data["doc_id"], "pdf_page": page,
              "expected_revision": data["current"]["source_revision_id"] or data["legacy_token"],
              "operation_id": "op-" + core.uuid.uuid4().hex, "reviewer": "human-1", "reason": "对照 PDF 确认文字",
              "candidate_id": data["current"]["source_candidate_id"],
              "block_id": data["current"]["blocks"][0]["block_id"]}
    result.update(changes)
    return result


def correct(job, text="1.0.1 Aircraft must not exceed 15 km under this condition."):
    return sources.commit(job, payload(job, corrected_text=text), "correct_text")


def test_candidates_diff_and_normalization_mapping(source_job):
    data = comparison(source_job)
    assert data["candidates"]["native"]["status"] == data["candidates"]["ocr"]["status"] == "available"
    assert data["current"]["source_kind"] == "native"
    changes = data["comparison"]["changes"]
    assert {c["type"] for c in changes} >= {"replace", "delete"}
    assert {flag for c in changes for flag in c["highlights"]} >= {"number", "unit", "negation"}
    left = sources.joined(data["candidates"]["native"]["blocks"])
    for change in changes:
        part = change["left"]
        assert left[part["raw_start"]:part["raw_end"]] == part["text"]
        assert part["precision"] in {"block", "page"}
    block = data["current"]["blocks"][0]
    normalized = sources.compare([dict(block, text="a \n b")], [dict(block, text="ab")])
    assert normalized["difference_rate"] == 0
    assert normalized["left_offsets"] == [0, 4]


@pytest.mark.parametrize("status,blocks", [("not_generated", None), ("failed", None), ("available", [])])
def test_ocr_states_are_distinct(source_job, status, blocks):
    state = sources.manifest(source_job)
    key = next(iter(state["pages"]))
    entry = state["pages"][key]["candidates"]["ocr"]
    if blocks is not None:
        path = sources.root(source_job) / "candidates" / f"{entry['candidate_id']}.json"
        value = core.read_json(path)
        value.update(blocks=blocks, source_text_sha256=sources.text_hash(""))
        core.write_json(path, value)
        entry["sha256"] = core.sha256_file(path)
    else:
        state["pages"][key]["candidates"]["ocr"] = {"status": status, "error": "test failure" if status == "failed" else None}
    core.write_json(sources.root(source_job) / "manifest.json", state)
    data = comparison(source_job)
    assert data["candidates"]["ocr"]["status"] == status
    assert ("blocks" in data["candidates"]["ocr"]) == (status == "available")
    if status != "available":
        assert data["comparison"] is None


def test_select_uses_saved_candidate_preserves_raw_and_needs_verification(source_job):
    data = comparison(source_job)
    before = {p: p.read_bytes() for p in (sources.root(source_job) / "candidates").glob("*.json")}
    parsed_path = core.job_dir(source_job) / "parsed/pages.jsonl"
    parsed = parsed_path.read_bytes()
    cid = data["candidates"]["ocr"]["candidate_id"]
    body = payload(source_job, candidate_id=cid, text="forged", blocks=[], bbox=[0, 0, 0, 0])
    preview = sources.preview_source_change(source_job, dict(body, operation_type="select_source"))
    assert preview["blocks"] == data["candidates"]["ocr"]["blocks"]
    result = sources.commit(source_job, body, "select_source")
    after = comparison(source_job)
    assert result["changed"] and after["current"]["source_kind"] == "ocr"
    assert after["current"]["blocks"] == preview["blocks"]
    assert not after["source_verified"] and after["requires_verification"]
    assert core.pages_for(source_job)[0]["blocks"] == preview["blocks"]
    assert parsed_path.read_bytes() == parsed
    assert all(p.read_bytes() == content for p, content in before.items())
    same = sources.commit(source_job, payload(source_job, candidate_id=cid), "select_source")
    assert not same["changed"] and same["current_revision"] == result["current_revision"]


def test_foreign_page_task_candidate_and_hash_are_rejected(source_job):
    other_page = comparison(source_job, 2)["candidates"]["ocr"]["candidate_id"]
    with pytest.raises(sources.SourceError) as error:
        sources.commit(source_job, payload(source_job, candidate_id=other_page), "select_source")
    assert error.value.status_code == 404
    cloned = core.clone_parsed_job(source_job)
    foreign = comparison(cloned)["candidates"]["ocr"]["candidate_id"]
    # A copied candidate is valid only in its own manifest and task scope.
    state = sources.manifest(source_job)
    entry = state["pages"][next(iter(state["pages"]))]["candidates"]["ocr"]
    path = sources.root(source_job) / "candidates" / f"{entry['candidate_id']}.json"
    content = core.read_json(path)
    content["job_id"] = cloned
    core.write_json(path, content)
    with pytest.raises(sources.SourceError, match="哈希"):
        sources.commit(source_job, payload(source_job, candidate_id=foreign), "select_source")


def test_correction_records_exact_block_version_and_conflicts(source_job):
    before = comparison(source_job)["current"]
    body = payload(source_job, corrected_text="已依据 PDF 校正：不得超过 15 km。", before_text="forged")
    result = sources.commit(source_job, body, "correct_text")
    current = comparison(source_job)["current"]
    operation = current["operation"]
    assert current["source_kind"] == "manual" and current["parent_revision_id"] == before["source_revision_id"]
    assert current["blocks"][0]["bbox"] == before["blocks"][0]["bbox"]
    assert operation["before_text"] == before["blocks"][0]["text"]
    assert operation["before_text_sha256"] == sources.text_hash(operation["before_text"])
    assert operation["new_revision"] == result["current_revision"]
    assert operation["location_precision"] == "block"
    assert not comparison(source_job)["source_verified"]
    assert sources.commit(source_job, body, "correct_text") == result
    with pytest.raises(sources.SourceError) as error:
        sources.commit(source_job, dict(body, operation_id="different-op"), "correct_text")
    assert error.value.status_code == 409
    with pytest.raises(sources.SourceError, match="操作 ID"):
        sources.commit(source_job, dict(body, corrected_text="other"), "correct_text")
    unchanged = sources.commit(source_job, payload(source_job, corrected_text=current["blocks"][0]["text"]), "correct_text")
    assert not unchanged["changed"]


def test_empty_or_ambiguous_correction_and_note_only(source_job):
    with pytest.raises(sources.SourceError, match="不能为空"):
        sources.commit(source_job, payload(source_job, corrected_text=" \n"), "correct_text")
    current = comparison(source_job)["current"]
    same = sources.commit(source_job, payload(source_job, corrected_text=current["blocks"][0]["text"], reason="采用 OCR"), "correct_text")
    assert not same["changed"] and comparison(source_job)["current"]["source_kind"] == "native"
    with pytest.raises(sources.SourceError, match="不存在或定位不唯一"):
        sources.commit(source_job, payload(source_job, block_id="missing", corrected_text="some"), "correct_text")


def test_effective_source_invalidates_evidence_qa_txt_exports(source_job):
    data = comparison(source_job)
    unit = core.add_evidence(source_job, data["current"]["blocks"][0]["block_id"], "topic", "group", "train")
    core.build_semantic_txt(source_job)
    root = core.job_dir(source_job)
    txt_path = root / "parsed/semantic_corpus.txt"
    old_txt = txt_path.read_bytes()
    ref = {"evidence_id": "E1", "evidence_unit_id": unit["evidence_unit_id"], "doc_id": unit["doc_id"],
           "pdf_page": 1, "block_id": unit["block_id"], "quote": unit["quote"], "char_start": unit["char_start"],
           "char_end": unit["char_end"], "source_revisions": unit["source_revisions"]}
    qa = {"sample_id": "SFT-history", "question": "value?", "answer": "10 km [E1]", "answerability": "answerable",
          "split": "train", "origin": "manual", "evidence": [ref], "claims": [{"text": "10 km", "support_type": "direct", "evidence_ids": ["E1"]}],
          "review": {"status": "approved"}}
    core.write_jsonl(root / "candidates/records.jsonl", [qa])
    evidence_bytes = (root / "evidence/units.jsonl").read_bytes()
    archive, _, _ = core.export_job(source_job)
    metadata = core.read_json(archive.with_suffix(".sha256.json"))
    assert sources.artifact_status(source_job, archive, metadata)["ready"]
    preview = sources.preview_source_change(source_job, payload(source_job, operation_type="correct_text", corrected_text="1.0.1 不得超过 15 km。"))
    assert preview["impact"]["evidence_count"] == preview["impact"]["sample_count"] == 1
    correct(source_job)
    assert sources.artifact_status(source_job, archive, metadata)["stale"]
    with pytest.raises(sources.SourceError, match="过期"):
        sources.require_artifact(source_job, archive, metadata)
    assert txt_path.read_bytes() == old_txt
    assert sources.artifact_status(source_job, txt_path, core.read_json(root / "parsed/txt_report.json"))["stale"]
    units = {u["evidence_unit_id"]: u for u in core.evidence_for(source_job)}
    assert core.validate_candidate(qa, units)
    assert not core.active_evidence_for(source_job)
    assert (root / "evidence/units.jsonl").read_bytes() == evidence_bytes
    sources.commit(source_job, payload(source_job), "verify")
    assert comparison(source_job)["source_verified"]
    assert core.validate_candidate(qa, {u["evidence_unit_id"]: u for u in core.evidence_for(source_job)})
    replacement = core.add_evidence(source_job, data["current"]["blocks"][0]["block_id"], "topic", "group", "train")
    assert replacement["source_revisions"] != unit["source_revisions"]
    core.build_semantic_txt(source_job)
    assert b"15 km" in txt_path.read_bytes()
    assert sources.artifact_status(source_job, txt_path, core.read_json(root / "parsed/txt_report.json"))["ready"]
    assert list((root / "parsed/history").rglob("semantic_corpus.txt"))


def test_frozen_rejects_changes_clone_keeps_history(source_job):
    for number, split in enumerate(("train", "validation", "test"), 1):
        data = comparison(source_job, number)
        core.add_evidence(source_job, data["current"]["blocks"][0]["block_id"], "topic", f"group-{number}", split)
    core.freeze_splits(source_job)
    files = {p: p.read_bytes() for p in (core.job_dir(source_job) / "evidence").glob("*")}
    for operation in ("select_source", "correct_text", "initialize"):
        with pytest.raises(sources.SourceError) as error:
            sources.commit(source_job, payload(source_job, corrected_text="new"), operation)
        assert error.value.status_code == 403
    cloned = core.clone_parsed_job(source_job)
    assert not core.evidence_for(cloned)
    assert comparison(cloned)["current"] == comparison(source_job)["current"]
    correct(cloned)
    assert comparison(cloned)["current"]["source_revision_id"] != comparison(source_job)["current"]["source_revision_id"]
    assert all(p.read_bytes() == b for p, b in files.items())


def test_failed_commit_leaves_old_current_and_retry_recovers(source_job, monkeypatch):
    body = payload(source_job, corrected_text="changed block")
    before = comparison(source_job)["current"]["source_revision_id"]
    write = core.write_json

    def fail_manifest(path, value):
        if path.name == "manifest.json":
            raise OSError("simulated fsync/replace failure")
        return write(path, value)

    with monkeypatch.context() as patch:
        patch.setattr(core, "write_json", fail_manifest)
        with pytest.raises(OSError):
            sources.commit(source_job, body, "correct_text")
    assert comparison(source_job)["current"]["source_revision_id"] == before
    assert body["operation_id"] not in sources.manifest(source_job)["operations"]
    assert sources.commit(source_job, body, "correct_text")["changed"]


def test_concurrent_updates_have_one_winner(source_job):
    first = payload(source_job, corrected_text="first correction")
    second = dict(first, corrected_text="second correction", operation_id="second")

    def save(body):
        try:
            return sources.commit(source_job, body, "correct_text")["changed"]
        except sources.SourceError as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(save, [first, second]))
    assert outcomes.count(True) == 1 and outcomes.count(409) == 1


def test_legacy_initialization_and_frozen_legacy(source_job):
    (sources.root(source_job) / "manifest.json").unlink()
    data = comparison(source_job)
    assert data["missing_historical_candidates"] and not data["source_verified"]
    assert not data["current"]["source_revision_id"]
    with pytest.raises(sources.SourceError, match="先显式建立"):
        correct(source_job)
    result = sources.commit(source_job, payload(source_job), "initialize")
    data = comparison(source_job)
    assert result["changed"] and data["current"]["source_candidate_id"] is None
    assert data["current"]["source_kind"] == "legacy" and not data["source_verified"]
    assert data["missing_historical_candidates"]
    assert correct(source_job)["changed"]


def test_txt_manual_table_uses_effective_text_without_pdf_extraction():
    class ForbiddenPdf:
        def __getitem__(self, index):
            raise AssertionError("must not re-extract protected source")

    unit = {"text": "人工校正表格 15 km", "raw_text": "人工校正表格 15 km", "effective_text_only": True,
            "pdf_page": 1, "end_pdf_page": 1, "source_locations": []}
    text, tables, warnings = readable_txt.PdfLayout(ForbiddenPdf()).render_table(unit)
    assert "15 km" in text and not tables and warnings


def test_stale_suggestion_cannot_write_even_if_text_matches(source_job):
    old = sources.snapshot(source_job)
    correct(source_job)
    block = core.pages_for(source_job)[0]["blocks"][0]
    item = {"block_id": block["block_id"], "source_snapshot": old, "quote": block["text"],
            "topic": "topic", "clause_group": "group", "split": "train"}
    with pytest.raises(sources.SourceError, match="预览来源已变化"):
        core.add_evidence_batch(source_job, [item], "human")


def test_source_api_status_codes_and_download_gate(source_job):
    from aviation_ft.web_app import server

    async def call(path, body=None):
        sent = []
        delivered = False

        async def receive():
            nonlocal delivered
            if delivered:
                return {"type": "http.disconnect"}
            delivered = True
            return {"type": "http.request", "body": json.dumps(body).encode() if body is not None else b"", "more_body": False}

        async def send(message):
            sent.append(message)

        await server.app({"type": "http", "http_version": "1.1", "method": "POST" if body is not None else "GET",
                          "path": f"/api/jobs/{source_job}/" + path, "root_path": "", "query_string": b"",
                          "scheme": "http", "server": ("test", 80), "client": ("test", 123),
                          "headers": [(b"content-type", b"application/json")]}, receive, send)
        return next(m["status"] for m in sent if m["type"] == "http.response.start")

    body = payload(source_job, corrected_text="new actual text")
    assert asyncio.run(call("page-source/correct", body)) == 200
    assert asyncio.run(call("page-source/correct", dict(body, operation_id="stale"))) == 409
    assert asyncio.run(call("page-source/correct", payload(source_job, corrected_text=""))) == 400
    foreign = comparison(source_job, 2)["candidates"]["ocr"]["candidate_id"]
    assert asyncio.run(call("page-source/select", payload(source_job, candidate_id=foreign))) == 404
    archive = core.job_dir(source_job) / "exports/aviation-sft-preview.zip"
    archive.write_bytes(b"historical archive")
    assert asyncio.run(call("exports/preview")) == 409
    core.write_json(core.job_dir(source_job) / "evidence/splits.json", {"frozen_at": core.now()})
    assert asyncio.run(call("page-source/correct", payload(source_job, corrected_text="another"))) == 403


def test_frontend_response_ownership_and_draft_guards():
    static = Path(core.__file__).parent / "web_app/static"
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js 不可用；未执行前端异步归属验证")
    script = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const folder = process.argv[1];
new vm.Script(fs.readFileSync(folder + '/app.js', 'utf8'));
const host = {innerHTML: '', textContent: ''};
const doc = {value:'DOC-A'}, page = {value:'1'};
let resolve;
const pending = new Promise(r => resolve = r);
const ctx = {state:{jobId:'A',view:'evidence'}, pageRequestSequence:0,
  loading:()=> 'loading', api:()=>pending,
  $:(selector)=>({'#page-doc':doc,'#page-number':page,'#page-workspace':host}[selector])};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(folder + '/source_review.js', 'utf8'), ctx);
let rendered = 0;
ctx.renderSourceEditor = () => ++rendered;
(async()=>{
  const opening = ctx.openSourceReview();
  ctx.state.jobId = 'B';
  resolve({current:{blocks:[]}});
  await opening;
  assert.equal(rendered, 0, 'late task response rendered into another task');
  ctx.state.jobId = 'A';
  let resolvePage;
  ctx.api = () => new Promise(r => resolvePage = r);
  const switching = ctx.openSourceReview();
  page.value = '2';
  resolvePage({current:{blocks:[]}});
  await switching;
  assert.equal(rendered, 0, 'late page response rendered into another page');
})().catch(error => {console.error(error);process.exitCode=1;});
'''
    result = subprocess.run([node, "-e", script, str(static)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_cross_page_evidence_binds_both_versions_and_invalidates(source_job):
    pages = core.pages_for(source_job)
    unit = {"evidence_unit_id": "EU-cross", "doc_id": pages[0]["doc_id"], "pdf_page": 1,
            "end_pdf_page": 2, "block_id": pages[0]["blocks"][0]["block_id"],
            "source_locations": [{"pdf_page": p["pdf_page"], "block_id": p["blocks"][0]["block_id"],
                                  "bbox": p["blocks"][0]["bbox"]} for p in pages[:2]]}
    sources.bind_evidence(source_job, unit)
    assert len(unit["source_revisions"]) == 2
    core.write_jsonl(core.job_dir(source_job) / "evidence/units.jsonl", [unit])
    impact = sources.get_source_change_impact(source_job, unit["doc_id"], 2)
    assert impact["evidence_ids"] == ["EU-cross"] and impact["precision"] == "page"
    sources.commit(source_job, payload(source_job, page=2, corrected_text="2.0.1 corrected second page."), "correct_text")
    assert any("已过期" in issue for issue in sources.evidence_issues(source_job, unit))


def test_freeze_race_cannot_freeze_unverified_change(source_job):
    for number, split in enumerate(("train", "validation", "test"), 1):
        core.add_evidence(source_job, comparison(source_job, number)["current"]["blocks"][0]["block_id"], "topic", f"group-{number}", split)
    body = payload(source_job, corrected_text="changed during freeze")

    def change():
        try:
            return sources.commit(source_job, body, "correct_text")["changed"]
        except sources.SourceError as exc:
            return exc.status_code

    def freeze():
        try:
            core.freeze_splits(source_job)
            return True
        except sources.SourceError as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        changed, frozen = pool.submit(change), pool.submit(freeze)
        outcomes = changed.result(), frozen.result()
    assert outcomes in ((True, 409), (403, True))


def test_original_pdf_hash_and_revision_write_failures(source_job, monkeypatch):
    body = payload(source_job, corrected_text="new text")
    sha = core.sha256_file
    with monkeypatch.context() as patch:
        patch.setattr(core, "sha256_file", lambda path: "tampered" if path.suffix == ".pdf" else sha(path))
        with pytest.raises(sources.SourceError, match="PDF.*哈希"):
            sources.commit(source_job, body, "correct_text")
    before = sources.snapshot(source_job)
    write = core.write_json

    def fail_revision(path, value):
        if path.parent.name == "revisions":
            raise OSError("simulated revision write failure")
        return write(path, value)

    with monkeypatch.context() as patch:
        patch.setattr(core, "write_json", fail_revision)
        with pytest.raises(OSError):
            sources.commit(source_job, body, "correct_text")
    assert sources.snapshot(source_job) == before


@pytest.mark.parametrize("outcome", ["available", "failed", "library_failed", "empty"])
def test_parser_retains_native_candidate_and_mocked_ocr(source_job, monkeypatch, outcome):
    job = core.create_job("mock OCR")
    with fitz.open() as pdf:
        page = pdf.new_page()
        page.insert_text((72, 100), "Native header")
        core.upload_pdf(job, "scan.pdf", io.BytesIO(pdf.tobytes()))
    original_text = fitz.Page.get_text
    monkeypatch.setattr(fitz.Page, "get_image_info", lambda self: [{"number": 0}])

    def mocked_ocr(self, **kwargs):
        if outcome == "failed":
            raise RuntimeError("mock OCR failure")
        if outcome == "library_failed":
            raise fitz.mupdf.FzErrorLibrary("mock Tesseract language initialisation failed")
        return self.get_textpage()

    def mocked_text(self, option="text", **kwargs):
        if kwargs.get("textpage") is not None:
            if option == "blocks":
                return [] if outcome == "empty" else [(72, 100, 400, 120, "OCR 70 km", 0, 0)]
            if option == "dict":
                return {"blocks": [{"number": 0, "lines": [{"spans": [{"font": "GlyphLessFont"}]}]}]}
        return original_text(self, option, **kwargs)

    monkeypatch.setattr(fitz.Page, "get_textpage_ocr", mocked_ocr)
    monkeypatch.setattr(fitz.Page, "get_text", mocked_text)
    core.parse_job(job)
    data = comparison(job)
    assert "Native header" in sources.joined(data["candidates"]["native"]["blocks"])
    failed = outcome in {"failed", "library_failed"}
    assert data["candidates"]["ocr"]["status"] == ("failed" if failed else "available")
    if outcome == "available":
        assert sources.joined(data["current"]["blocks"]) == "OCR 70 km"
    elif outcome == "empty":
        assert data["candidates"]["ocr"]["blocks"] == []
    else:
        assert data["current"]["source_kind"] == "native"
        assert data["current"]["blocks"] == data["candidates"]["native"]["blocks"]
        assert data["candidates"]["ocr"]["error"]
    assert data["requires_verification"] and not data["source_verified"]


def test_txt_rechecks_snapshot_before_publish(source_job, monkeypatch):
    build = readable_txt.build
    target = core.job_dir(source_job) / "parsed/semantic_corpus.txt"

    def concurrent_build(job_id):
        result = build(job_id)
        correct(job_id)
        return result

    monkeypatch.setattr(readable_txt, "build", concurrent_build)
    with pytest.raises(sources.SourceError, match="生成期间来源已变化"):
        core.build_semantic_txt(source_job)
    assert not target.exists()
