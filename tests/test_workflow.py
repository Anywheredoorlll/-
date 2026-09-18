from __future__ import annotations

from aviation_ft import core, workflow
from test_core import sample_job


def test_task_progress_identifies_next_action(sample_job):
    job_id = sample_job
    before = workflow.task_progress(job_id)
    assert before["documents"] == 1
    assert before["parsed_pages"] == 3
    assert "证据" in before["next_step"]
    pages = core.pages_for(job_id)
    units = [core.add_evidence(job_id, page["blocks"][0]["block_id"], f"topic-{n}", f"group-{n}", split)
             for n, (page, split) in enumerate(zip(pages, ("train", "validation", "test")))]
    core.freeze_splits(job_id)
    frozen = workflow.task_progress(job_id)
    assert frozen["evidence_by_split"] == {"test": 1, "train": 1, "validation": 1}
    assert "证据分布不达标" in frozen["next_step"]
    core.add_candidate(job_id, "Question?", "Answer. [E1]", [units[0]["evidence_unit_id"]],
                       "answerable", "Answer.", origin="model")
    assert "证据分布不达标" in workflow.task_progress(job_id)["next_step"]


def test_review_queue_surfaces_ocr_and_invalid_references(sample_job):
    job_id = sample_job
    pages = core.pages_for(job_id)
    units = [core.add_evidence(job_id, page["blocks"][0]["block_id"], f"topic-{n}", f"group-{n}", split)
             for n, (page, split) in enumerate(zip(pages, ("train", "validation", "test")))]
    core.freeze_splits(job_id)
    a = core.add_candidate(job_id, "Ordinary?", "Answer. [E1]", [units[0]["evidence_unit_id"]], "answerable", "Answer.")
    b = core.add_candidate(job_id, "OCR question?", "Answer. [E1]", [units[1]["evidence_unit_id"]], "answerable", "Answer.")
    c = core.add_candidate(job_id, "Broken?", "Answer. [E1]", [units[2]["evidence_unit_id"]], "answerable", "Answer.")
    records = core.candidates_for(job_id)
    records[2]["evidence"][0]["quote"] = "not in source"
    units[1]["ocr_used"] = True
    queue = workflow.review_queue(records, units)
    assert [item["record"]["sample_id"] for item in queue] == [c["sample_id"], b["sample_id"], a["sample_id"]]
    assert queue[0]["errors"]
    assert "OCR 原文" in queue[1]["flags"]
