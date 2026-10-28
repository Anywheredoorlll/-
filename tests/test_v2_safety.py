from __future__ import annotations

import asyncio
import io
import json

import pymupdf as fitz
import pytest
from starlette.requests import Request

from aviation_ft import (automation, claim_validation, core, extraction_quality,
                         generation, layout, migration, numeric_validation,
                         providers, review_policy)
from aviation_ft.web_app import server
from scripts.evaluate_review import evaluate
from test_core import add_v2_candidate, approve_v2, sample_job, verify_all_sources
from test_generation import configured_job


def test_damaged_long_text_triggers_full_page_ocr_but_blank_title_does_not():
    q = {"nonspace_char_count": 80, "replacement_char_ratio": .01,
         "private_use_char_ratio": 0, "image_area_ratio": 0,
         "text_area_ratio": .15}
    result = extraction_quality.decide_ocr(q, {})
    assert result["attempt"] and result["mode"] == "full_page"
    q.update(nonspace_char_count=4, replacement_char_ratio=0,
             image_area_ratio=0, text_area_ratio=.02)
    assert not extraction_quality.decide_ocr(q, {})["attempt"]
    q.update(nonspace_char_count=0, text_area_ratio=0)
    assert not extraction_quality.decide_ocr(q, {})["attempt"]


def test_native_ocr_conflict_is_review_only():
    native = {"candidate_id": "N", "method": "native", "text": "跑道长度不低于10米"}
    ocr = {"candidate_id": "O", "method": "ocr_full_page", "text": "跑道长度高于70米"}
    comparison = extraction_quality.compare_extractions(native, ocr, {})
    selected = extraction_quality.select_extraction(native, ocr, comparison, [])
    assert comparison["requires_source_review"]
    assert {item["kind"] for item in comparison["conflicts"]} >= {"numbers", "negations"}
    assert selected["selected_candidate_id"] == "N"
    assert selected["requires_source_review"]


def test_quality_area_uses_union_and_unknown_signals_are_explicit():
    assert extraction_quality.union_area([[0, 0, 5, 5], [2, 2, 7, 7]], 10, 10) == 41
    page = fitz.open().new_page(width=100, height=100)
    candidate = {"text": "title", "blocks": [{"bbox": [0, 0, 10, 10]}], "quality": {}}
    quality = extraction_quality.assess_page_extraction(page, candidate, {})
    assert quality["suspected_unextracted_regions"] == "unknown"
    assert quality["font_mapping_anomaly"] == "unknown"
    assert quality["text_area_ratio"] == .01


def test_ocr_failure_keeps_other_pages_and_blocks_source(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "ROOT", tmp_path)
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs/settings.json").write_text(json.dumps({
        "max_file_mb": 10, "max_job_mb": 20, "min_free_gb_after_upload": 0,
    }), encoding="utf-8")
    pdf = fitz.open()
    pdf.new_page().insert_text((72, 72), "Normal text is sufficiently long and complete for native extraction.")
    pdf.new_page().insert_text((72, 72), "scan")
    job = core.create_job()
    core.upload_pdf(job, "two.pdf", io.BytesIO(pdf.tobytes()))
    pdf.close()
    monkeypatch.setattr(extraction_quality, "decide_ocr", lambda q, _: {
        "attempt": q["nonspace_char_count"] < 30, "mode": "full_page", "reasons": ["test"]})
    monkeypatch.setattr(extraction_quality, "extract_ocr_page",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("simulated OCR error")))
    report = core.parse_job(job)
    assert report["pages"] == 2 and report["failed_pages"] == 1
    assert len(core.pages_for(job)) == 2
    unit = core.add_evidence(job, core.pages_for(job)[1]["blocks"][0]["block_id"],
                             "scan", "section", "train")
    with pytest.raises(ValueError, match="未处理问题"):
        core.verify_evidence_source(job, unit["evidence_unit_id"], "reviewer", "checked PDF")


def test_layout_columns_footnote_and_new_clause_stay_separate():
    page = {"width": 600, "height": 800, "blocks": [
        {"block_id": "L1", "bbox": [50, 100, 250, 120], "text": "1.0.1 left first."},
        {"block_id": "R1", "bbox": [350, 100, 550, 120], "text": "2.0.1 right first."},
        {"block_id": "L2", "bbox": [50, 170, 250, 190], "text": "left second."},
        {"block_id": "R2", "bbox": [350, 170, 550, 190], "text": "right second."},
        {"block_id": "F", "bbox": [50, 720, 250, 740], "text": "注 footnote."},
    ]}
    ordered, status = layout.infer_reading_order(layout.detect_regions(page))
    assert status == "proposed"
    assert [b["block_id"] for b in ordered] == ["L1", "L2", "R1", "R2", "F"]
    groups = layout.propose_semantic_groups(ordered)
    assert groups[0][0]["block_id"] == "L1" and groups[-1][0]["block_id"] == "F"
    proposal = layout.evaluate_cross_page_merge(
        {"doc_id": "D", "pdf_page": 1, "kind": "prose", "column_id": 0, "text": "continued"},
        {"doc_id": "D", "pdf_page": 2, "kind": "prose", "column_id": 0, "text": "2.0.1 new clause"}, {})
    assert proposal["decision"] == "separate" and "新条款开始" in proposal["reasons"]


def test_evidence_entry_points_share_draft_provenance(sample_job):
    pages = core.pages_for(sample_job)
    a = core.add_evidence(sample_job, pages[0]["blocks"][0]["block_id"], "a", "g1", "train")
    item = core.evidence_suggestions(sample_job, pages[1]["doc_id"])[1]
    b = core.add_evidence_batch(sample_job, [item], "human-reviewer")[0]
    for unit in (a, b):
        assert unit["evidence_schema_version"] == "evidence-v2"
        assert unit["source_verification"]["status"] == "draft"
        assert unit["source_pdf_sha256"] and unit["source_text_sha256"]
        assert unit["source_fragments"] and unit["source_locations"]


def test_confirmed_cross_page_evidence_preserves_both_page_fragments(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "ROOT", tmp_path)
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs/settings.json").write_text(json.dumps({
        "max_file_mb": 10, "max_job_mb": 20, "min_free_gb_after_upload": 0,
    }), encoding="utf-8")
    pdf = fitz.open()
    pdf.new_page().insert_text((72, 720),
        "1.0.1 The runway design shall continue on the next page and")
    pdf.new_page().insert_text((72, 72),
        "the required clearance is documented in this continuation.")
    job = core.create_job()
    core.upload_pdf(job, "continuation.pdf", io.BytesIO(pdf.tobytes()))
    pdf.close()
    core.parse_job(job)
    proposals = [p for p in core.cross_page_merge_proposals(job) if p["decision"] == "proposed"]
    assert proposals
    proposal = proposals[0]
    unit = core.add_evidence_cross_page(job, proposal["start_semantic_id"],
        proposal["end_semantic_id"], "runway", "1.0.1", "train", "reviewer",
        "对照构造 PDF 的连续句与相邻页面")
    assert {fragment["pdf_page"] for fragment in unit["source_fragments"]} == {1, 2}
    assert unit["quote"] == unit["source_text"][unit["char_start"]:unit["char_end"]]
    assert unit["cross_page_merge"]["decision"] == "confirmed"
    assert not core.txt_is_current(job)
    core.verify_evidence_source(job, unit["evidence_unit_id"], "reviewer", "对照两个 PDF 页面")
    core.build_semantic_txt(job)
    assert core.txt_is_current(job)
    assert core.read_json(core.job_dir(job) / "parsed/txt_report.json")["merged_cross_page"] == 1


def test_answer_coverage_and_unanswerable_facts_cannot_disappear():
    claim = {"claim_id": "C1", "answer_quote": "跑道宽度为45米"}
    result = claim_validation.check_answer_coverage("跑道宽度为45米，长度为1800米。[E1]", [claim], "answerable")
    assert result["status"] == "needs_human"
    refusal = claim_validation.check_answer_coverage("证据不足，无法确认；跑道长度为1800米。", [], "unanswerable")
    assert refusal["status"] == "needs_human"


def test_unanswerable_factual_claim_needs_exact_source(sample_job):
    pages = core.pages_for(sample_job)
    units = [core.add_evidence(sample_job, page["blocks"][0]["block_id"], str(i), str(i), split)
             for i, (page, split) in enumerate(zip(pages, ("train", "validation", "test")))]
    verify_all_sources(sample_job)
    core.freeze_splits(sample_job)
    answer = "证据不足，无法确认范围。Civil aviation requirement. [E1]"
    claim = {"claim_id": "C1", "text": "Civil aviation requirement",
             "answer_quote": "Civil aviation requirement", "evidence_ids": ["E1"],
             "supporting_quotes": [{"evidence_id": "E1", "quote": "Civil aviation requirement"}]}
    record = core.add_candidate(sample_job, "What is confirmed?", answer,
                                [units[0]["evidence_unit_id"]], "unanswerable",
                                claims=[claim], schema_version="qa-v2")
    assert record["claims"] and record["claim_spans"]
    without_claim = claim_validation.check_answer_coverage(answer, [], "unanswerable")
    assert without_claim["status"] == "needs_human"


def test_numeric_subject_swap_and_boundary_change_require_review():
    source = "跑道长度不低于1800米。跑道宽度不低于45米。"
    swapped = numeric_validation.compare_numeric_facts("跑道长度不低于45米。跑道宽度不低于1800米。", source)
    boundary = numeric_validation.compare_numeric_facts("跑道长度高于1800米。", source)
    assert swapped["status"] == "needs_human"
    assert boundary["status"] == "needs_human"
    assert "数值比较关系冲突" in boundary["issues"]
    assert numeric_validation.compare_numeric_facts("跑道长度不低于1800米。", source)["status"] == "structure_checked"


def test_unverified_source_blocks_even_two_supporting_models(sample_job):
    page = core.pages_for(sample_job)[0]
    unit = core.add_evidence(sample_job, page["blocks"][0]["block_id"], "a", "g1", "train")
    record = {"candidate_schema_version": "qa-v2", "coverage_check": {"status": "structure_checked"},
              "numeric_checks": {"status": "not_applicable"}, "claims": [{"claim_id": "C1"}],
              "evidence": [{"evidence_unit_id": unit["evidence_unit_id"]}]}
    assessment = {"passes": [{"verdict": "supported", "coverage": "supported",
                              "model_identity": model, "claim_results": {"C1": {"verdict": "supported"}}}
                             for model in ("A@1", "B@1")]}
    policy = {"auto_approve_enabled": True, "calibration_id": "claimed", "_calibration": {
        "scope_groups": [{"doc_id": unit["doc_id"], "clause_group": unit["clause_group"]}]}}
    result = review_policy.evaluate_approval(record, {unit["evidence_unit_id"]: unit},
                                             assessment, policy, automated=True)
    assert not result["approved"]
    assert any("对照 PDF" in reason for reason in result["reasons"])


def test_automatic_review_requires_exact_claim_support_quotes():
    record = {"candidate_schema_version": "qa-v2", "answerability": "answerable",
              "coverage_check": {"status": "structure_checked"},
              "numeric_checks": {"status": "not_applicable"},
              "claims": [{"claim_id": "C1", "evidence_ids": ["E1"]}],
              "evidence": [{"evidence_unit_id": "U1", "evidence_id": "E1", "quote": "跑道长度1800米"}]}
    unit = {"parse_version": "parse-v2", "source_pdf_sha256": "hash",
            "source_verification": {"status": "source_verified", "source_pdf_sha256": "hash",
                                    "reviewer": "human", "verified_at": "2026-01-01"},
            "doc_id": "D1", "clause_group": "G1"}
    record_hash = core.candidate_content_sha256(record)
    passes = [{"verdict": "supported", "coverage": "supported", "model_identity": model,
               "provider": "mock", "model_id": model, "prompt_sha256": "prompt",
               "request_settings": {"temperature": 0}, "response_sha256": "response",
               "judged_record_sha256": record_hash, "supporting_quote": "跑道长度1800米",
               "claim_results": {"C1": {"verdict": "supported"}}}
              for model in ("A@1", "B@1")]
    policy = {"auto_approve_enabled": True, "_calibration": {
        "scope_groups": [{"doc_id": "D1", "clause_group": "G1"}]}}
    result = review_policy.evaluate_approval(record, {"U1": unit},
        {"verdict": "supported", "passes": passes}, policy, automated=True)
    assert not result["approved"]
    assert "评审支持片段无法精确定位到对应证据" in result["reasons"]


def test_revision_conflict_and_pagination_beyond_200(sample_job):
    page = core.pages_for(sample_job)[0]
    unit = core.add_evidence(sample_job, page["blocks"][0]["block_id"], "a", "g1", "train")
    other = core.pages_for(sample_job)
    for n, split in ((1, "validation"), (2, "test")):
        core.add_evidence(sample_job, other[n]["blocks"][0]["block_id"], str(n), str(n), split)
    verify_all_sources(sample_job)
    core.freeze_splits(sample_job)
    record = add_v2_candidate(sample_job, unit["evidence_unit_id"])
    with pytest.raises(core.RevisionConflict):
        core.review_candidate(sample_job, record["sample_id"], "rejected", "reviewer", expected_revision=0)
    sent = []
    payload = json.dumps({"decision": "rejected", "reviewer": "reviewer", "expected_revision": 0}).encode()
    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}
    async def send(message):
        sent.append(message)
    scope = {"type": "http", "method": "POST", "path": f"/api/jobs/{sample_job}/candidates/{record['sample_id']}/review",
             "root_path": "", "query_string": b"", "headers": [(b"content-type", b"application/json")],
             "http_version": "1.1", "scheme": "http", "server": ("test", 80), "client": ("test", 1234)}
    asyncio.run(server.app(scope, receive, send))
    assert next(x["status"] for x in sent if x["type"] == "http.response.start") == 409
    rows = [dict(record, sample_id=f"SFT-{n:012d}", question=f"Q {n}?") for n in range(225)]
    core.write_jsonl(core.job_dir(sample_job) / "candidates/records.jsonl", rows)
    request = Request({"type": "http", "method": "GET", "path": "/",
                       "path_params": {"job_id": sample_job},
                       "query_string": b"page=9&page_size=25", "headers": []})
    response = asyncio.run(server.candidates(request))
    result = json.loads(response.body)
    assert result["total"] == 225 and len(result["items"]) == 25


def test_upstream_parse_change_invalidates_source_and_review(sample_job):
    pages = core.pages_for(sample_job)
    units = [core.add_evidence(sample_job, page["blocks"][0]["block_id"], str(i), str(i), split)
             for i, (page, split) in enumerate(zip(pages, ("train", "validation", "test")))]
    verify_all_sources(sample_job)
    core.freeze_splits(sample_job)
    record = add_v2_candidate(sample_job, units[0]["evidence_unit_id"])
    approve_v2(sample_job, record)
    assert review_policy.source_verified(core.current_evidence_units(sample_job)[0])
    pages[0]["blocks"][0]["text"] += " changed upstream"
    core.write_jsonl(core.job_dir(sample_job) / "parsed/pages.jsonl", pages)
    assert not review_policy.source_verified(core.current_evidence_units(sample_job)[0])
    report = core.quality_report(sample_job)
    assert not report["source_verified"] and not report["final_ready"]


def test_batch_api_cannot_approve_uncovered_fact(sample_job):
    pages = core.pages_for(sample_job)
    units = [core.add_evidence(sample_job, page["blocks"][0]["block_id"], str(i), str(i), split)
             for i, (page, split) in enumerate(zip(pages, ("train", "validation", "test")))]
    verify_all_sources(sample_job)
    core.freeze_splits(sample_job)
    claim = {"claim_id": "C1", "text": "Civil aviation requirement",
             "answer_quote": "Civil aviation requirement", "evidence_ids": ["E1"],
             "supporting_quotes": [{"evidence_id": "E1", "quote": "Civil aviation requirement"}]}
    record = core.add_candidate(sample_job, "What else applies?",
        "Civil aviation requirement. An unrelated extra condition also applies. [E1]",
        [units[0]["evidence_unit_id"]], "answerable", claims=[claim], schema_version="qa-v2")
    assert record["coverage_check"]["status"] == "needs_human"
    with pytest.raises(ValueError, match="覆盖"):
        core.review_candidates_batch(sample_job, [record["sample_id"]], "reviewer", "approved",
            expected_revisions={record["sample_id"]: 1},
            claim_reviews={record["sample_id"]: [{"claim_id": "C1", "verdict": "supported"}]},
            source_reviews={record["sample_id"]: [{"evidence_unit_id": units[0]["evidence_unit_id"], "verified": True}]})
    assert core.candidates_for(sample_job)[0]["review"]["status"] == "pending"


def test_pdf_overlay_geometry_stays_inside_rotated_crop(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "ROOT", tmp_path)
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs/settings.json").write_text(json.dumps({
        "max_file_mb": 10, "max_job_mb": 20, "min_free_gb_after_upload": 0,
    }), encoding="utf-8")
    pdf = fitz.open()
    page = pdf.new_page(width=600, height=800)
    page.insert_text((120, 200), "Rotated crop aviation source text that remains traceable.")
    page.set_cropbox(fitz.Rect(60, 100, 560, 700))
    page.set_rotation(90)
    job = core.create_job()
    core.upload_pdf(job, "rotated.pdf", io.BytesIO(pdf.tobytes()))
    pdf.close()
    core.parse_job(job)
    parsed = core.pages_for(job)[0]
    unit = core.add_evidence(job, parsed["blocks"][0]["block_id"], "rotation", "g", "train")
    request = Request({"type": "http", "method": "GET", "path": "/",
                       "path_params": {"job_id": job, "doc_id": parsed["doc_id"], "page_no": "1"},
                       "query_string": b"", "headers": []})
    response = asyncio.run(server.page_geometry(request))
    geometry = json.loads(response.body)
    assert geometry["rotation"] == 90
    rect = next(x["rect"] for x in geometry["locations"] if x["evidence_unit_id"] == unit["evidence_unit_id"])
    assert all(0 <= coordinate <= 1 for coordinate in rect)
    assert rect[2] > 0 and rect[3] > 0


def test_migration_dry_run_and_export_staleness(sample_job):
    root = core.job_dir(sample_job)
    before = (root / "parsed/pages.jsonl").read_bytes()
    jobs_before = {x["job_id"] for x in core.list_jobs()}
    plan = migration.migrate_job(sample_job)
    assert plan["dry_run"] and plan["legacy"]["pages"] == 3
    assert (root / "parsed/pages.jsonl").read_bytes() == before
    assert {x["job_id"] for x in core.list_jobs()} == jobs_before
    artifact, _, _ = core.export_job(sample_job)
    assert core.export_is_current(sample_job, artifact)
    pages = core.pages_for(sample_job)
    pages[0]["blocks"][0]["text"] += " altered"
    core.write_jsonl(root / "parsed/pages.jsonl", pages)
    assert not core.export_is_current(sample_job, artifact)


def test_missing_judge_role_bad_output_and_truncation_are_conservative(sample_job, monkeypatch):
    job, units = configured_job(sample_job, monkeypatch)
    with pytest.raises(ValueError, match="judge_primary"):
        generation.config(job, role="judge_primary")
    with pytest.raises(ValueError, match="规定字段"):
        automation._assessment({"verdict": "supported"}, {"supported", "unsupported", "uncertain"})
    (core.ROOT / "prompts/judge_grounded_qa.md").write_text("Judge", encoding="utf-8")
    record = add_v2_candidate(job, units[0]["evidence_unit_id"])
    monkeypatch.setattr(automation, "_completion", lambda *a, **kw: {
        "verdict": "supported", "claim_results": {"UNKNOWN": {"verdict": "supported",
            "supporting_quote": "Civil aviation requirement"}}, "coverage": "supported",
        "confidence_self_reported": .99, "reason": "checked",
        "supporting_quote": "Civil aviation requirement"})
    with pytest.raises(ValueError, match="未知或缺失"):
        automation._judge_candidate(record)
    monkeypatch.setattr(generation, "_request", lambda *a, **kw: {
        "choices": [{"finish_reason": "length", "message": {"content": "{}"}}]})
    with pytest.raises(ValueError, match="截断"):
        generation.generate_candidate(job, units[0]["evidence_unit_id"])
    assert len(core.candidates_for(job)) == 1


def test_judge_roles_have_separate_configuration_and_calibration_id_is_not_enough(sample_job, monkeypatch):
    job, _ = configured_job(sample_job, monkeypatch)
    providers.save_external(job, "https://example.com/v1", "judge-a", "fake-key-A", True,
                            role="judge_primary")
    providers.save_external(job, "https://example.com/v1", "judge-b", "fake-key-B", True,
                            role="judge_countercheck")
    assert providers.public_config(job, "judge_primary")["model"] == "judge-a"
    assert providers.public_config(job, "judge_countercheck")["model"] == "judge-b"
    assert providers.public_config(job, "generator")["mode"] == "local"
    (core.ROOT / "configs/review.json").write_text(json.dumps({
        "auto_approve_enabled": True, "calibration_id": "fake_calibration"}), encoding="utf-8")
    assert "_calibration" not in review_policy.config(core.ROOT, job)


def test_offline_evaluation_detects_group_leakage_and_reports_uncertainty():
    rows = [
        {"sample_id": "a", "doc_id": "D1", "clause_group": "G1", "phase": "calibration",
         "gold": "correct", "decision": "approved", "reviewer": "R1"},
        {"sample_id": "b", "doc_id": "D2", "clause_group": "G2", "phase": "acceptance",
         "gold": "incorrect", "decision": "needs_human", "reviewer": "R2"},
    ]
    report = evaluate(rows)
    assert not report["accepted"]
    assert report["acceptance"]["human_referral_rate"]["rate"] == 1
    assert report["acceptance"]["correct_false_reject_rate"]["rate"] is None
    rows[1]["doc_id"], rows[1]["clause_group"] = "D1", "G1"
    with pytest.raises(ValueError, match="重叠"):
        evaluate(rows)
