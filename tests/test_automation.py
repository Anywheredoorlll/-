from __future__ import annotations

import io
import json
import zipfile
from collections import Counter

import pymupdf as fitz
import pytest

from aviation_ft import automation, core, generation
from test_core import sample_job
from test_generation import configured_job


def test_model_review_approves_only_when_both_checks_support(sample_job, monkeypatch):
    job_id, units = configured_job(sample_job, monkeypatch)
    (core.ROOT / "prompts/judge_grounded_qa.md").write_text("Judge only the evidence.", encoding="utf-8")
    evidence_id = units[0]["evidence_unit_id"]
    supported = core.add_candidate(job_id, "What is the value?", "The value is 10. [E1]",
                                   [evidence_id], "answerable", "The value is 10.")
    numeric_doubt = core.add_candidate(job_id, "Is value 999?", "The value is 999. [E1]",
                                       [evidence_id], "answerable", "The value is 999.")
    rejected = core.add_candidate(job_id, "Does rule apply?", "It applies. [E1]",
                                  [evidence_id], "answerable", "It applies.")
    uncertain = core.add_candidate(job_id, "What condition remains?", "The condition remains unclear. [E1]",
                                   [evidence_id], "answerable", "The condition remains unclear.")
    support_quote = "Civil aviation requirement"

    def fake_completion(system, user, max_tokens=320):
        verdict = ("unsupported" if user["question"] == "Does rule apply?" else
                   "uncertain" if user["question"] == "What condition remains?" else "supported")
        return {"verdict": verdict, "confidence": 0.98, "reason": "checked",
                "supporting_quote": support_quote}

    monkeypatch.setattr(automation, "_completion", fake_completion)
    counts = automation.review_pending(job_id)
    assert counts == {"processed": 4, "approved": 1, "pending_expert_review": 1, "rejected": 2}
    records = {r["sample_id"]: r for r in core.candidates_for(job_id)}
    assert records[supported["sample_id"]]["review"]["status"] == "approved"
    assert len(records[supported["sample_id"]]["review"]["assessment"]["passes"]) == 2
    assert records[numeric_doubt["sample_id"]]["review"]["status"] == "rejected"
    assert records[uncertain["sample_id"]]["review"]["status"] == "pending_expert_review"
    assert records[rejected["sample_id"]]["review"]["status"] == "rejected"
    assert all(r["review"]["review_mode"] == "automated" for r in records.values())
    report = core.quality_report(job_id)
    assert report["approved_by_review_mode"]["automated"] == 1
    archive_path, _, _ = core.export_job(job_id, final=False, include_audit=True)
    with zipfile.ZipFile(archive_path) as archive:
        assert "judge_prompt.md" in archive.namelist()
        assert "assessment" in archive.read("review_events.jsonl").decode()
    core.review_candidates_batch(job_id, [uncertain["sample_id"]], "expert-1", "rejected")
    assert core.candidates_for(job_id)[3]["review"]["status"] == "rejected"
    assert automation.review_pending(job_id) == {}


@pytest.mark.parametrize("doc_total", [1, 3])
def test_automatic_evidence_selection_balances_documents(tmp_path, monkeypatch, doc_total):
    monkeypatch.setattr(core, "ROOT", tmp_path)
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs/settings.json").write_text(json.dumps({
        "max_file_mb": 10, "max_job_mb": 20, "min_free_gb_after_upload": 0,
    }), encoding="utf-8")
    job_id = core.create_job()
    for doc_index in range(doc_total):
        pdf = fitz.open()
        for n in range(25):
            page = pdf.new_page()
            page.insert_text((72, 72), f"Aviation document {doc_index + 1} page {n + 1} defines a specific runway requirement and conditions.")
        core.upload_pdf(job_id, f"doc-{doc_index}.pdf", io.BytesIO(pdf.tobytes()))
        pdf.close()
    monkeypatch.setattr(generation, "config", lambda: {"model_id": "test-model", "served_model_name": "test-model"})
    monkeypatch.setattr(automation, "_evidence_suitability", lambda item: {
        "verdict": "suitable", "confidence": 0.99, "reason": "complete",
        "supporting_quote": "Aviation document",
    })
    result = automation.prepare_evidence(job_id)
    assert result["status"] == "frozen"
    assert result["added"] == 20 * doc_total
    units = core.evidence_for(job_id)
    assert len(units) == 20 * doc_total
    assert all(u["confirmation_mode"] == "automated" for u in units)
    assert Counter(u["split"] for u in units) == {"train": 12 * doc_total, "validation": 4 * doc_total, "test": 4 * doc_total}
    assert Counter(u["doc_id"] for u in units) == {d["doc_id"]: 20 for d in core.load_job(job_id)["documents"]}
    assert automation.prepare_evidence(job_id)["status"] == "frozen"


def test_number_guard_handles_chinese_units():
    assert automation._number_tokens("跑道长度为1800米。[E1]") == {"1800"}
    assert automation._number_tokens("宽度45.5米") == {"45.5"}

def test_automatic_evidence_selection_defers_deterministically_unsafe_blocks(
    sample_job, monkeypatch,
):
    page = core.pages_for(sample_job)[0]
    block = page["blocks"][0]
    proposal = {
        "selected": False,
        "pdf_page": page["pdf_page"],
        "block_id": block["block_id"],
        "start_block_id": block["block_id"],
        "end_block_id": block["block_id"],
        "blocks": 1,
        "ocr_used": False,
        "semantic_id": "unsafe-semantic",
        "kind": "prose",
        "possible_continuation_from_previous_page": False,
        "possible_continuation_to_next_page": True,
        "quality_issues": ["语义段落在当前块后仍未结束，需合并后核实"],
        "quote": block["text"].strip(),
        "topic": "unsafe",
        "clause_group": "pages-0001-0005",
        "split": "train",
    }
    monkeypatch.setattr(core, "evidence_suggestions", lambda *args, **kwargs: [proposal])
    monkeypatch.setattr(
        generation, "config",
        lambda: {"model_id": "test-model", "served_model_name": "test-model"},
    )

    def should_not_call_model(item):
        raise AssertionError("unsafe evidence must not reach the model")

    monkeypatch.setattr(automation, "_evidence_suitability", should_not_call_model)
    result = automation.prepare_evidence(sample_job)

    assert result["status"] == "needs_human"
    assert result["added"] == 0
    assert result["assessed"] == 0
    queue = core.read_jsonl(core.job_dir(sample_job) / "evidence/human_queue.jsonl")
    assert queue[0]["block_id"] == block["block_id"]

