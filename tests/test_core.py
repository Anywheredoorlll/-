from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pymupdf as fitz
import pytest

from aviation_ft import core


@pytest.fixture
def sample_job(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "ROOT", tmp_path)
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs/settings.json").write_text(json.dumps({
        "generator_model_dir": str(tmp_path / "models/generator"),
        "max_file_mb": 10,
        "max_job_mb": 20,
        "min_free_gb_after_upload": 0,
        "completed_job_retention_days": 7,
        "expected_documents": ["expected-a.pdf", "expected-b.pdf", "expected-c.pdf"],
    }), encoding="utf-8")
    job_id = core.create_job("unit-test")
    doc = fitz.open()
    for n in range(3):
        page = doc.new_page()
        page.insert_text((72, 72), f"Civil aviation requirement {n + 1}: value {n + 10} applies under condition {n + 1}.")
    data = doc.tobytes()
    doc.close()
    core.upload_pdf(job_id, "test.pdf", io.BytesIO(data))
    core.parse_job(job_id)
    return job_id


def test_upload_parse_evidence_export_cleanup(sample_job):
    job_id = sample_job
    pages = core.pages_for(job_id)
    assert len(pages) == 3
    assert all(p["blocks"] for p in pages)
    ids = []
    for n, split in enumerate(("train", "validation", "test")):
        e = core.add_evidence(job_id, pages[n]["blocks"][0]["block_id"], f"topic-{n}", f"group-{n}", split)
        ids.append(e["evidence_unit_id"])
    frozen = core.freeze_splits(job_id)
    assert frozen["counts"] == {"test": 1, "train": 1, "validation": 1}
    r = core.add_candidate(job_id, "What is the first condition?", "The value is 10. [E1]", [ids[0]], "answerable", "The value is 10.")
    assert not core.validate_candidate(r, {e["evidence_unit_id"]: e for e in core.evidence_for(job_id)})
    core.review_candidate(job_id, r["sample_id"], "approved", "R01")
    report = core.quality_report(job_id)
    assert not report["final_ready"]
    assert report["evidence_target"] == 20
    assert report["approved_target"] == 90
    assert report["holdout_evidence_target"] == 4
    with pytest.raises(ValueError):
        core.export_job(job_id, final=True)
    path, digest, _ = core.export_job(job_id, final=False)
    with zipfile.ZipFile(path) as z:
        assert "train.jsonl" in z.namelist()
        assert "test.pdf" not in z.namelist()
        assert "preview_not_for_training" in z.read("README.txt").decode()
    with pytest.raises(ValueError):
        core.cleanup_job(job_id, "wrong", digest)
    core.cleanup_job(job_id, job_id, digest)
    assert not (core.jobs_root() / job_id).exists()
    assert "manual_after_download_confirmation" in (core.ROOT / "runtime/cleanup_log.jsonl").read_text()


def test_split_leakage_and_exact_quote(sample_job):
    job_id = sample_job
    pages = core.pages_for(job_id)
    with pytest.raises(ValueError):
        core.add_evidence(job_id, pages[0]["blocks"][0]["block_id"], "bad", "g", "train", "not-in-original")
    core.add_evidence(job_id, pages[0]["blocks"][0]["block_id"], "a", "same-group", "train")
    core.add_evidence(job_id, pages[1]["blocks"][0]["block_id"], "b", "same-group", "validation")
    core.add_evidence(job_id, pages[2]["blocks"][0]["block_id"], "c", "other-group", "test")
    with pytest.raises(ValueError, match="跨 split"):
        core.freeze_splits(job_id)


def test_invalid_pdf_is_not_registered(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "ROOT", tmp_path)
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs/settings.json").write_text(json.dumps({"max_file_mb": 1, "max_job_mb": 2, "min_free_gb_after_upload": 0}), encoding="utf-8")
    job_id = core.create_job()
    with pytest.raises(ValueError, match="PDF"):
        core.upload_pdf(job_id, "bad.pdf", io.BytesIO(b"not a PDF"))
    assert core.load_job(job_id)["documents"] == []


def test_parse_scanned_pdf_keeps_native_text_and_ocr_bbox(tmp_path, monkeypatch):
    tessdata = Path(core.__file__).resolve().parents[2] / "models/ocr/tessdata"
    if not (tessdata / "chi_sim.traineddata").exists() or not (tessdata / "eng.traineddata").exists():
        pytest.skip("OCR language data is not installed")
    monkeypatch.setattr(core, "ROOT", tmp_path)
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs/settings.json").write_text(json.dumps({
        "max_file_mb": 10, "max_job_mb": 20, "min_free_gb_after_upload": 0,
        "ocr_tessdata": str(tessdata), "ocr_languages": "chi_sim+eng", "ocr_dpi": 200,
    }), encoding="utf-8")

    source = fitz.open()
    source_page = source.new_page(width=1000, height=220)
    source_page.insert_text((30, 80), "OCR scanned aviation rule value 12345", fontsize=36)
    source_page.insert_text((30, 160), "民用机场飞行区技术标准", fontsize=36, fontname="china-s")
    image = source_page.get_pixmap(matrix=fitz.Matrix(2, 2)).tobytes("png")
    source.close()

    pdf = fitz.open()
    digital = pdf.new_page()
    digital.insert_text((72, 72), "This digital text page contains enough native characters for parsing.")
    scanned = pdf.new_page(width=800, height=300)
    scanned.insert_text((30, 30), "Native header")
    scanned.insert_image(fitz.Rect(0, 80, 800, 256), stream=image)
    job_id = core.create_job()
    pdf_bytes = pdf.tobytes()
    core.upload_pdf(job_id, "mixed.pdf", io.BytesIO(pdf_bytes))
    pdf.close()

    report = core.parse_job(job_id)
    pages = core.pages_for(job_id)
    assert report["ocr_pages"] == 1
    assert report["ocr_blocks"] >= 1
    assert pages[0]["ocr_used"] is False
    assert pages[1]["ocr_used"] is True
    assert any(b["text"].strip() == "Native header" and not b["ocr_used"] for b in pages[1]["blocks"])
    ocr_blocks = [b for b in pages[1]["blocks"] if b["ocr_used"]]
    assert any("scanned aviation rule" in b["text"] for b in ocr_blocks)
    assert any("民用机场飞行区" in b["text"] for b in ocr_blocks)
    assert all(len(b["bbox"]) == 4 and b["bbox"][2] > b["bbox"][0] for b in ocr_blocks)
    evidence = core.add_evidence(job_id, ocr_blocks[0]["block_id"], "OCR", "rule", "train")
    assert evidence["pdf_page"] == 2
    assert evidence["quote"] in evidence["source_text"]

    bad_config = json.loads((tmp_path / "configs/settings.json").read_text(encoding="utf-8"))
    bad_config["ocr_tessdata"] = str(tmp_path / "missing-tessdata")
    (tmp_path / "configs/settings.json").write_text(json.dumps(bad_config), encoding="utf-8")
    failing_job = core.create_job()
    core.upload_pdf(failing_job, "mixed.pdf", io.BytesIO(pdf_bytes))
    core.parse_job(failing_job)
    failed = core.sources.get_page_source_comparison(failing_job, core.load_job(failing_job)["documents"][0]["doc_id"], 2)
    assert failed["candidates"]["ocr"]["status"] == "failed"
    assert failed["current"]["source_kind"] == "native"
    assert failed["requires_verification"] and not failed["source_verified"]


def test_batch_evidence_is_atomic_and_keeps_exact_sources(sample_job):
    job_id = sample_job
    doc_id = core.load_job(job_id)["documents"][0]["doc_id"]
    suggestions = core.evidence_suggestions(job_id, doc_id)
    assert len(suggestions) == 3
    assert len({row["pdf_page"] for row in suggestions}) == 3

    bad = [dict(suggestions[0]), dict(suggestions[1])]
    bad[1]["quote"] = "not in the PDF"
    with pytest.raises(ValueError, match="精确子串"):
        core.add_evidence_batch(job_id, bad, "reviewer-1")
    assert core.evidence_for(job_id) == []

    records = core.add_evidence_batch(job_id, suggestions[:2], "reviewer-1")
    assert len(records) == 2
    assert all(item["confirmed_by"] == "reviewer-1" for item in records)
    assert all(item["quote"] == item["source_text"][item["char_start"]:item["char_end"]] for item in records)
    with pytest.raises(ValueError, match="重复"):
        core.add_evidence_batch(job_id, suggestions[:2], "reviewer-1")
    assert len(core.evidence_for(job_id)) == 2


def test_multiblock_evidence_includes_table_rows_and_validates_reference(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "ROOT", tmp_path)
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs/settings.json").write_text(json.dumps({
        "max_file_mb": 10, "max_job_mb": 20, "min_free_gb_after_upload": 0,
    }), encoding="utf-8")
    job_id = core.create_job()
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text((72, 72), "Section 1.0.3 describes runway classification.")
    page.insert_text((72, 160), "Table 1: Code 1 means less than 800 metres.")
    core.upload_pdf(job_id, "table.pdf", io.BytesIO(pdf.tobytes()))
    pdf.close()
    core.parse_job(job_id)
    parsed = core.pages_for(job_id)[0]
    assert len(parsed["blocks"]) == 2
    start, end = (b["block_id"] for b in parsed["blocks"])
    source = core.page_block_range(parsed, start, end)
    assert "Section 1.0.3" in source["source_text"]
    assert "Table 1: Code 1" in source["source_text"]
    assert source["block_ids"] == [start, end]
    assert len(source["bboxes"]) == 2

    record = core.add_evidence_range(
        job_id, parsed["doc_id"], 1, start, end,
        "runway classification", "section-1.0.3", "train", "reviewer-1",
    )
    assert record["quote"] == source["source_text"]
    assert record["source_text"][record["char_start"]:record["char_end"]] == record["quote"]
    assert record["block_ids"] == [start, end]
    with pytest.raises(ValueError, match="重复"):
        core.add_evidence_range(
            job_id, parsed["doc_id"], 1, start, end,
            "runway classification", "section-1.0.3", "train", "reviewer-1",
        )
    with pytest.raises(ValueError, match="页面顺序"):
        core.page_block_range(parsed, end, start)


def test_batch_review_prevalidates_all_candidates(sample_job):
    job_id = sample_job
    pages = core.pages_for(job_id)
    units = [core.add_evidence(job_id, page["blocks"][0]["block_id"], f"topic-{n}", f"group-{n}", split)
             for n, (page, split) in enumerate(zip(pages, ("train", "validation", "test")))]
    core.freeze_splits(job_id)
    a = core.add_candidate(job_id, "Question A?", "Value 10. [E1]", [units[0]["evidence_unit_id"]], "answerable", "Value 10.")
    b = core.add_candidate(job_id, "Question B?", "Value 10. [E1]", [units[0]["evidence_unit_id"]], "answerable", "Value 10.")
    records_path = core.job_dir(job_id) / "candidates/records.jsonl"
    records = core.candidates_for(job_id)
    records[1]["evidence"][0]["quote"] = "invented quote"
    core.write_jsonl(records_path, records)
    with pytest.raises(ValueError, match="引用范围"):
        core.review_candidates_batch(job_id, [a["sample_id"], b["sample_id"]], "reviewer-1")
    assert all(r["review"]["status"] == "pending" for r in core.candidates_for(job_id))
    assert core.read_jsonl(core.job_dir(job_id) / "reviewed/events.jsonl") == []
    records[1]["evidence"][0]["quote"] = units[0]["quote"]
    core.write_jsonl(records_path, records)
    result = core.review_candidates_batch(job_id, [a["sample_id"], b["sample_id"]], "reviewer-1")
    assert result["updated"] == 2
    reviewed = core.candidates_for(job_id)
    assert all(r["review"]["status"] == "approved" for r in reviewed)
    assert all(r["review"]["review_mode"] == "batch" for r in reviewed)
    assert len(core.read_jsonl(core.job_dir(job_id) / "reviewed/events.jsonl")) == 2


def test_clone_parsed_job_keeps_original_and_resets_evidence(sample_job):
    original = sample_job
    pages = core.pages_for(original)
    core.add_evidence(original, pages[0]["blocks"][0]["block_id"], "topic", "group", "train")
    cloned = core.clone_parsed_job(original)
    assert cloned != original
    assert core.evidence_for(cloned) == []
    assert len(core.evidence_for(original)) == 1
    assert core.load_job(cloned)["source_job_id"] == original
    assert core.load_job(cloned)["documents"] == core.load_job(original)["documents"]
    assert core.pages_for(cloned) == pages
    for document in core.load_job(cloned)["documents"]:
        assert core.sha256_file(core.job_dir(cloned) / "raw_pdf" / document["filename"]) == document["sha256"]


def test_full_page_suggestion_preserves_contiguous_table_content(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "ROOT", tmp_path)
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs/settings.json").write_text(json.dumps({
        "max_file_mb": 10, "max_job_mb": 20, "min_free_gb_after_upload": 0,
    }), encoding="utf-8")
    job_id = core.create_job()
    pdf = fitz.open()
    page = pdf.new_page()
    for n, text in enumerate((
        "Section 1: Runway categories depend on measured airplane dimensions.",
        "Table 1: Code A corresponds to wing spans below 15 metres.",
        "Table 1: Code B corresponds to wing spans from 15 to 24 metres.",
    )):
        page.insert_text((70, 65 + n * 65), text)
    core.upload_pdf(job_id, "table.pdf", io.BytesIO(pdf.tobytes()))
    pdf.close()
    core.parse_job(job_id)
    doc_id = core.load_job(job_id)["documents"][0]["doc_id"]
    suggestions = core.evidence_suggestions(job_id, doc_id)
    assert len(suggestions) == 3
    proposal = suggestions[0]
    assert proposal["blocks"] == 1
    assert proposal["quote"].count("\n") <= 1
    assert proposal["start_block_id"] == proposal["end_block_id"]
    bad = dict(proposal, quote="invented")
    with pytest.raises(ValueError, match="精确子串"):
        core.add_evidence_batch(job_id, [bad], "reviewer")
    saved = core.add_evidence_batch(job_id, [proposal], "reviewer")[0]
    assert saved["scope"] == "block"
    assert len(saved["block_ids"]) == 1
    assert saved["quote"] == saved["source_text"]
    assert saved["source_text"][saved["char_start"]:saved["char_end"]] == proposal["quote"]


def test_quality_report_flags_cross_split_leakage_and_document_coverage(sample_job):
    job_id = sample_job
    pages = core.pages_for(job_id)
    units = []
    for n, split in enumerate(("train", "validation", "test")):
        units.append(core.add_evidence(
            job_id, pages[n]["blocks"][0]["block_id"], f"topic-{n}", f"group-{n}",
            split, "Civil aviation requirement",
        ))
    core.freeze_splits(job_id)
    for unit in units:
        candidate = core.add_candidate(
            job_id, "What is the aviation requirement?", "The evidence mentions an aviation requirement. [E1]",
            [unit["evidence_unit_id"]], "answerable", "The evidence mentions an aviation requirement.",
        )
        core.review_candidate(job_id, candidate["sample_id"], "approved", "reviewer-1")
    report = core.quality_report(job_id)
    assert report["approved_by_document"] == {"test.pdf": 3}
    assert report["approved_evidence_units_by_document"] == {"test.pdf": 3}
    assert report["semantic_validation"] == "not_independently_validated"
    assert any("跨分区重复原文证据" in issue for issue in report["issues"])
    assert any("跨分区重复获批问题" in issue for issue in report["issues"])
    assert any("获批问答不足 30 条" in issue for issue in report["issues"])
    assert any("被获批问答实际引用的不同证据不足 10 条" in issue for issue in report["issues"])
    path, _, _ = core.export_job(job_id, include_audit=True)
    with zipfile.ZipFile(path) as archive:
        card = json.loads(archive.read("dataset_card.json"))
    assert card["semantic_validation"] == "not_independently_validated"


def test_remove_mistaken_pdf_only_before_parsing(sample_job):
    job_id = sample_job
    with pytest.raises(ValueError, match="PDF 已解析"):
        core.remove_unparsed_pdf(job_id, "test.pdf")
    assert (core.job_dir(job_id) / "raw_pdf/test.pdf").is_file()


def test_remove_unparsed_pdf_updates_job(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "ROOT", tmp_path)
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs/settings.json").write_text(json.dumps({
        "max_file_mb": 10, "max_job_mb": 20, "min_free_gb_after_upload": 0,
    }), encoding="utf-8")
    job_id = core.create_job()
    pdf = fitz.open()
    pdf.new_page().insert_text((72, 72), "Mistaken document")
    core.upload_pdf(job_id, "mistake.pdf", io.BytesIO(pdf.tobytes()))
    pdf.close()
    core.remove_unparsed_pdf(job_id, "mistake.pdf")
    assert core.load_job(job_id)["documents"] == []
    assert core.load_job(job_id)["status"] == "created"
    assert not (core.job_dir(job_id) / "raw_pdf/mistake.pdf").exists()


def test_upload_accepts_arbitrary_names_over_multiple_batches_and_locks_after_parse(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "ROOT", tmp_path)
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs/settings.json").write_text(json.dumps({
        "max_file_mb": 10, "max_job_mb": 20, "min_free_gb_after_upload": 0,
    }), encoding="utf-8")
    job_id = core.create_job()
    pdf = fitz.open()
    pdf.new_page().insert_text((72, 72), "A document uploaded in several visits")
    data = pdf.tobytes()
    pdf.close()
    core.upload_pdf(job_id, "first arbitrary.pdf", io.BytesIO(data))
    with pytest.raises(ValueError, match="相同内容"):
        core.upload_pdf(job_id, "same content.pdf", io.BytesIO(data))
    second = fitz.open()
    second.new_page().insert_text((72, 72), "A different document uploaded later")
    second_data = second.tobytes()
    second.close()
    core.upload_pdf(job_id, "second arbitrary.pdf", io.BytesIO(second_data))
    assert {d["filename"] for d in core.load_job(job_id)["documents"]} == {
        "first arbitrary.pdf", "second arbitrary.pdf"
    }
    assert core.quality_report(job_id)["approved_target"] == 180
    core.parse_job(job_id)
    with pytest.raises(ValueError, match="开始构建前"):
        core.upload_pdf(job_id, "third arbitrary.pdf", io.BytesIO(data))


def test_semantic_blocks_rebuild_clauses_keep_tables_and_flag_page_breaks():
    def block(number, y, text, x=90):
        return {
            "block_id": f"DOC-test-p0002-b{number:04d}",
            "bbox": [x, y, 505, y + 14],
            "text": text,
            "ocr_used": False,
        }

    page = {
        "doc_id": "DOC-test",
        "filename": "rules.pdf",
        "pdf_page": 2,
        "printed_page": None,
        "ocr_used": False,
        "possible_scanned": False,
        "blocks": [
            block(1, 70, "1 总则\n", 280),
            block(2, 100, "1.0.1 为规范机场规划、设计、建设和运行，本着安全适用、标准统一、科学\n"),
            block(3, 124, "合理、国际通用的原则，制定本标准。\n"),
            block(4, 154, "1.0.2 本标准适用于陆地民用机场，在适当情况下也适用于直升机场，但不适用于短距起\n"),
            block(5, 178, "降机场和水上机场。\n"),
            block(6, 210, "表1.0.2-1 飞行区指标I\n", 240),
            block(7, 234, "1\n<800\n", 190),
            block(8, 266, "表1.0.2-2 飞行区指标II\n", 240),
            block(9, 290, "A\n<15\n", 190),
            block(10, 330, "【条文说明】这是一个很长的说明片段，但句子在本页末尾尚未结束并将在下一页继续"),
        ],
    }

    semantic = core.semantic_blocks_for_page(page)
    assert len(semantic) == 6
    assert semantic[0]["kind"] == "heading"
    assert semantic[1]["block_ids"] == [
        "DOC-test-p0002-b0002", "DOC-test-p0002-b0003"
    ]
    assert "科学合理、国际通用" in semantic[1]["text"]
    assert semantic[3]["kind"] == "table"
    assert semantic[3]["block_ids"] == [
        "DOC-test-p0002-b0006", "DOC-test-p0002-b0007"
    ]
    assert semantic[4]["block_ids"] == [
        "DOC-test-p0002-b0008", "DOC-test-p0002-b0009"
    ]
    assert semantic[-1]["possible_continuation_to_next_page"] is True

    source = core.page_block_range(
        page, "DOC-test-p0002-b0002", "DOC-test-p0002-b0003"
    )
    assert source["source_text"].endswith("制定本标准。")
    assert "\n" not in source["source_text"]
    assert "\n" in source["raw_source_text"]
    assert source["text_reconstructed"] is True

    suggestion = core._suggested_page_source(page)
    assert suggestion["possible_continuation_to_next_page"] is False
    assert suggestion["source_text"].startswith(("1.0.1", "1.0.2"))

    next_page = {
        **page,
        "pdf_page": 3,
        "blocks": [block(1, 78, "上一页条文在这里结束。\n")],
    }
    next_page["blocks"][0]["block_id"] = "DOC-test-p0003-b0001"
    first = core.semantic_blocks_for_page(next_page, page)[0]
    assert first["possible_continuation_from_previous_page"] is True

def test_parse_builds_downloadable_semantic_txt(sample_job):
    path = core.job_dir(sample_job) / "parsed/semantic_corpus.txt"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "# 文档：test.pdf" in text
    assert "## 证据块 0001 | PDF 页 1" in text
    assert "Civil aviation requirement 1" in text
    report = core.build_semantic_txt(sample_job)
    assert report["documents"] == 1
    assert report["pages"] == 3
    assert report["semantic_units"] == 3
    assert report["sha256"] == core.sha256_file(path)


def test_semantic_txt_merges_cross_page_clause_only_in_txt_branch(sample_job):
    meta = core.load_job(sample_job)
    doc = meta["documents"][0]
    first = {
        "doc_id": doc["doc_id"], "filename": doc["filename"], "pdf_page": 1,
        "printed_page": None, "ocr_used": False, "possible_scanned": False,
        "blocks": [{
            "block_id": f"{doc['doc_id']}-p0001-b0001",
            "bbox": [72, 72, 500, 90],
            "text": "1.0.1 本条内容在页末继续",
            "ocr_used": False,
        }],
    }
    second = {
        "doc_id": doc["doc_id"], "filename": doc["filename"], "pdf_page": 2,
        "printed_page": None, "ocr_used": False, "possible_scanned": False,
        "blocks": [{
            "block_id": f"{doc['doc_id']}-p0002-b0001",
            "bbox": [72, 72, 500, 90],
            "text": "并在下一页结束。",
            "ocr_used": False,
        }],
    }
    (core.job_dir(sample_job) / "parsed/sources/manifest.json").unlink(missing_ok=True)  # legacy fixture without source revisions
    core.write_jsonl(core.job_dir(sample_job) / "parsed/pages.jsonl", [first, second])
    report = core.build_semantic_txt(sample_job)
    text = (core.job_dir(sample_job) / "parsed/semantic_corpus.txt").read_text(encoding="utf-8")
    assert "页末继续并在下一页结束。" in text
    assert "## 证据块 0001 | PDF 页 1-2" in text
    assert report["merged_cross_page"] == 1
    assert core.evidence_for(sample_job) == []


def test_semantic_blocks_separate_numbered_list_items():
    page = {
        "doc_id": "DOC-list", "filename": "list.pdf", "pdf_page": 1,
        "printed_page": None, "ocr_used": False, "possible_scanned": False,
        "blocks": [
            {"block_id": "DOC-list-p0001-b0001", "bbox": [72, 72, 500, 90],
             "text": "（1）第一项内容在下一行", "ocr_used": False},
            {"block_id": "DOC-list-p0001-b0002", "bbox": [72, 94, 500, 112],
             "text": "继续并在此结束。", "ocr_used": False},
            {"block_id": "DOC-list-p0001-b0003", "bbox": [72, 118, 500, 136],
             "text": "（2）第二项内容。", "ocr_used": False},
        ],
    }
    semantic = core.semantic_blocks_for_page(page)
    assert [item["kind"] for item in semantic] == ["list_item", "list_item"]
    assert semantic[0]["block_ids"] == [
        "DOC-list-p0001-b0001", "DOC-list-p0001-b0002",
    ]
    assert semantic[0]["text"].endswith("继续并在此结束。")


def test_delete_job_requires_full_id_and_removes_task_secret(sample_job):
    secret_dir = core.ROOT / "runtime/provider_secrets"
    secret_dir.mkdir(parents=True)
    secret = secret_dir / f"{sample_job}.json"
    secret.write_text('{"api_key":"secret"}', encoding="utf-8")
    with pytest.raises(ValueError, match="完整任务 ID"):
        core.delete_job(sample_job, "wrong")
    assert core.job_dir(sample_job).is_dir()
    audit = core.delete_job(sample_job, sample_job)
    assert audit["reason"] == "manual_task_delete"
    assert not (core.jobs_root() / sample_job).exists()
    assert not secret.exists()
    assert "manual_task_delete" in (
        core.ROOT / "runtime/cleanup_log.jsonl"
    ).read_text(encoding="utf-8")

def test_private_pdf_glyphs_join_overlapping_lines_and_filter_page_furniture():
    dot = core.PDF_CLAUSE_DOT

    def block(number, y, text, x=78):
        return {
            "block_id": f"DOC-private-p0010-b{number:04d}",
            "bbox": [x, y, 517, y + 24],
            "text": text,
            "ocr_used": False,
        }

    page = {
        "doc_id": "DOC-private", "filename": "private.pdf", "pdf_page": 10,
        "printed_page": None, "width": 595, "height": 842,
        "ocr_used": False, "possible_scanned": False,
        "blocks": [
            block(1, 260, f"１{dot}２{dot}１ 本规范适用于机场规划、设计"),
            block(2, 280, "和建设ꎮ"),
            block(3, 330, f"１{dot}２{dot}２ 其他要求应符合现行标准ꎮ"),
            block(4, 56, "重复页眉内容"),
            block(5, 762, "— １ —", 280),
        ],
    }

    semantic = core.semantic_blocks_for_page(page)
    assert len(semantic) == 2
    assert semantic[0]["block_ids"] == [
        "DOC-private-p0010-b0001", "DOC-private-p0010-b0002",
    ]
    assert semantic[0]["text"].endswith("规划、设计和建设ꎮ")
    assert semantic[1]["text"].startswith(f"１{dot}２{dot}２")
    assert all("页眉" not in item["text"] for item in semantic)
    assert all(not core.evidence_source_issues({**item, "source_text": item["text"]})
               for item in semantic)


def test_table_stops_before_private_glyph_clause():
    dot = core.PDF_CLAUSE_DOT
    page = {
        "doc_id": "DOC-table", "filename": "table.pdf", "pdf_page": 31,
        "printed_page": None, "width": 595, "height": 842,
        "ocr_used": False, "possible_scanned": False,
        "blocks": [
            {"block_id": "DOC-table-p0031-b0001", "bbox": [230, 320, 370, 340],
             "text": f"表４{dot}１{dot}１ 月度检查内容", "ocr_used": False},
            {"block_id": "DOC-table-p0031-b0002", "bbox": [90, 350, 510, 400],
             "text": "区域/设备\n检查焊缝有无开裂", "ocr_used": False},
            {"block_id": "DOC-table-p0031-b0003", "bbox": [78, 430, 517, 454],
             "text": f"４{dot}１{dot}２ 应开展年度检查并记录检查", "ocr_used": False},
            {"block_id": "DOC-table-p0031-b0004", "bbox": [78, 450, 180, 474],
             "text": "结果ꎮ", "ocr_used": False},
        ],
    }

    semantic = core.semantic_blocks_for_page(page)
    assert [item["kind"] for item in semantic] == ["table", "prose"]
    assert semantic[0]["block_ids"] == [
        "DOC-table-p0031-b0001", "DOC-table-p0031-b0002",
    ]
    assert semantic[1]["text"].endswith("检查结果ꎮ")


def test_semantic_txt_merges_cross_page_tables(sample_job):
    meta = core.load_job(sample_job)
    doc = meta["documents"][0]
    first = {
        "doc_id": doc["doc_id"], "filename": doc["filename"], "pdf_page": 1,
        "printed_page": None, "width": 595, "height": 842,
        "ocr_used": False, "possible_scanned": False,
        "blocks": [
            {"block_id": f"{doc['doc_id']}-p0001-b0001", "bbox": [220, 640, 380, 665],
             "text": "表1.1 年度检查内容", "ocr_used": False},
            {"block_id": f"{doc['doc_id']}-p0001-b0002", "bbox": [90, 680, 510, 720],
             "text": "第一部分检查内容", "ocr_used": False},
        ],
    }
    second = {
        "doc_id": doc["doc_id"], "filename": doc["filename"], "pdf_page": 2,
        "printed_page": None, "width": 595, "height": 842,
        "ocr_used": False, "possible_scanned": False,
        "blocks": [
            {"block_id": f"{doc['doc_id']}-p0002-b0001", "bbox": [470, 100, 510, 120],
             "text": "续表", "ocr_used": False},
            {"block_id": f"{doc['doc_id']}-p0002-b0002", "bbox": [90, 130, 510, 170],
             "text": "第二部分检查内容", "ocr_used": False},
        ],
    }
    (core.job_dir(sample_job) / "parsed/sources/manifest.json").unlink(missing_ok=True)  # legacy fixture without source revisions
    core.write_jsonl(core.job_dir(sample_job) / "parsed/pages.jsonl", [first, second])

    report = core.build_semantic_txt(sample_job)
    text = (core.job_dir(sample_job) / "parsed/semantic_corpus.txt").read_text(encoding="utf-8")
    assert "第一部分检查内容\n续表\n第二部分检查内容" in text
    assert "## 证据块 0001 | PDF 页 1-2" in text
    assert report["merged_cross_page"] == 1



def test_training_and_trace_exports_preserve_identical_samples(sample_job):
    import hashlib

    job = sample_job
    block = core.pages_for(job)[0]["blocks"][0]
    unit = core.add_evidence(job, block["block_id"], "topic", "group", "train")
    for page, split in zip(core.pages_for(job)[1:], ("validation", "test")):
        core.add_evidence(job, page["blocks"][0]["block_id"], "topic", split, split)
    core.freeze_splits(job)
    candidate = core.add_candidate(job, "What applies?", "Value 10 applies. [E1]",
                                   [unit["evidence_unit_id"]], "answerable", "Value 10 applies.")
    core.review_candidate(job, candidate["sample_id"], "approved", "reviewer")
    evidence_before = (core.job_dir(job) / "evidence/units.jsonl").read_bytes()
    candidates_before = (core.job_dir(job) / "candidates/records.jsonl").read_bytes()
    path, digest, _ = core.export_job(job)
    trace = path.with_name(path.stem + "-trace.zip")
    assert digest == core.sha256_file(path)
    with zipfile.ZipFile(path) as training, zipfile.ZipFile(trace) as audit:
        assert set(training.namelist()) == {"train.jsonl", "validation.jsonl", "test.jsonl", "README.txt"}
        assert {"evidence_units.jsonl", "master_approved.jsonl", "dataset_card.json", "review_events.jsonl"} <= set(audit.namelist())
        for split in core.SPLITS:
            assert training.read(f"{split}.jsonl") == audit.read(f"{split}.jsonl")
        rows = [json.loads(line) for line in training.read("train.jsonl").splitlines()]
        assert [row["metadata"]["sample_id"] for row in rows] == [candidate["sample_id"]]
        for line in audit.read("SHA256SUMS.txt").decode().splitlines():
            expected, filename = line.split("  ", 1)
            assert hashlib.sha256(audit.read(filename)).hexdigest() == expected
    assert core.read_json(trace.with_suffix(".sha256.json"))["sha256"] == core.sha256_file(trace)
    assert (core.job_dir(job) / "evidence/units.jsonl").read_bytes() == evidence_before
    assert (core.job_dir(job) / "candidates/records.jsonl").read_bytes() == candidates_before
