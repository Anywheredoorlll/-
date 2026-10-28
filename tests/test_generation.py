from __future__ import annotations

import json
import zipfile

import pytest

from aviation_ft import core, generation
from test_core import sample_job, verify_all_sources


def configured_job(sample_job, monkeypatch):
    root = core.ROOT
    (root / "models/generator").mkdir(parents=True)
    (root / "models/generator/model.safetensors.index.json").write_text("{}", encoding="utf-8")
    (root / "prompts").mkdir()
    (root / "prompts/generate_grounded_qa.md").write_text("Use only E1.", encoding="utf-8")
    (root / "configs/generation.json").write_text(json.dumps({
        "enabled": True, "backend": "openai_compatible",
        "model_dir": str(root / "models/generator"),
        "model_id": "test-model", "model_revision": "test-revision",
        "prompt_version": "test-v1", "prompt_file": "prompts/generate_grounded_qa.md",
        "base_url": "http://127.0.0.1:8000/v1", "served_model_name": "test-served",
        "temperature": 0.2, "max_tokens": 100, "timeout_seconds": 5,
        "allowed_splits": ["train", "validation"],
    }), encoding="utf-8")
    pages = core.pages_for(sample_job)
    units = [core.add_evidence(sample_job, page["blocks"][0]["block_id"], f"topic-{n}", f"group-{n}", split)
             for n, (page, split) in enumerate(zip(pages, ("train", "validation", "test")))]
    verify_all_sources(sample_job)
    core.freeze_splits(sample_job)
    return sample_job, units


def test_generated_candidate_is_pending_and_idempotent(sample_job, monkeypatch):
    job_id, units = configured_job(sample_job, monkeypatch)
    calls = []

    def fake_request(url, payload=None, timeout=5):
        calls.append((url, payload))
        if url.endswith("/models"):
            return {"data": [{"id": "test-served"}]}
        return {"choices": [{"message": {"content": json.dumps({
            "question": "What value applies?", "answerability": "answerable",
            "answer": "Civil aviation requirement. [E1]", "claims": [{
                "claim_id": "C1", "text": "Civil aviation requirement",
                "answer_quote": "Civil aviation requirement", "evidence_ids": ["E1"],
                "supporting_quotes": [{"evidence_id": "E1", "quote": "Civil aviation requirement"}]
            }]
        })}}]}

    monkeypatch.setattr(generation, "_request", fake_request)
    assert generation.probe()["ready"]
    result = generation.generate_candidate(job_id, units[0]["evidence_unit_id"])
    assert result["status"] == "candidate_saved_pending_review"
    record = core.candidates_for(job_id)[0]
    assert record["review"]["status"] == "pending"
    assert record["origin"] == "model"
    assert record["generator_model"] == "test-model"
    assert record["evidence"][0]["quote"] == units[0]["quote"]
    assert not core.validate_candidate(record, {u["evidence_unit_id"]: u for u in units})
    duplicate = generation.generate_candidate(job_id, units[0]["evidence_unit_id"])
    assert duplicate["status"] == "already_exists"
    assert len(calls) == 2
    assert len(core.candidates_for(job_id)) == 1
    log = core.read_jsonl(core.job_dir(job_id) / "candidates/generation_attempts.jsonl")
    assert log[0]["status"] == "candidate_saved_pending_review"
    assert log[0]["prompt_sha256"]
    assert log[0]["raw_output"]
    export_path, _, _ = core.export_job(job_id, final=False, include_audit=True)
    with zipfile.ZipFile(export_path) as archive:
        assert "generation_attempts.jsonl" in archive.namelist()
        assert "generation_prompt.md" in archive.namelist()
        assert "generation_config.json" in archive.namelist()
    revised = dict(record)
    revised["generator_model"] = "other-model"
    with pytest.raises(ValueError, match="生成溯源字段"):
        core.edit_candidate(job_id, record["sample_id"], revised, "reviewer")


def test_test_split_and_external_endpoint_are_rejected(sample_job, monkeypatch):
    job_id, units = configured_job(sample_job, monkeypatch)
    monkeypatch.setattr(generation, "_request", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not call model")))
    with pytest.raises(ValueError, match="测试集"):
        generation.generate_candidate(job_id, units[2]["evidence_unit_id"])
    cfg_path = core.ROOT / "configs/generation.json"
    cfg = json.loads(cfg_path.read_text())
    cfg["base_url"] = "https://remote.example/v1"
    cfg_path.write_text(json.dumps(cfg))
    with pytest.raises(ValueError, match="本机 HTTP"):
        generation.config()


def test_bad_model_json_logged_not_saved(sample_job, monkeypatch):
    job_id, units = configured_job(sample_job, monkeypatch)
    monkeypatch.setattr(generation, "_request", lambda *a, **kw: {"choices": [{"message": {"content": "not-json"}}]})
    with pytest.raises(ValueError, match="JSON"):
        generation.generate_candidate(job_id, units[0]["evidence_unit_id"])
    assert core.candidates_for(job_id) == []
    log = core.read_jsonl(core.job_dir(job_id) / "candidates/generation_attempts.jsonl")
    assert log[0]["status"] == "failed"
    assert log[0]["raw_output"] == "not-json"


def test_batch_generation_resumes_and_test_requires_frozen_recipe(sample_job, monkeypatch):
    job_id, units = configured_job(sample_job, monkeypatch)
    generated = 0

    def fake_request(url, payload=None, timeout=5):
        nonlocal generated
        if url.endswith("/models"):
            return {"data": [{"id": "test-served"}]}
        generated += 1
        is_unanswerable = "任务类型：" + generation.VARIANTS["unanswerable"] in payload["messages"][1]["content"]
        return {"choices": [{"message": {"content": json.dumps({
            "question": f"Question {generated}?",
            "answerability": "unanswerable" if is_unanswerable else "answerable",
            "answer": "证据不足，无法确认。" if is_unanswerable else "Civil aviation requirement. [E1]",
            "claims": [] if is_unanswerable else [{
                "claim_id": "C1", "text": "Civil aviation requirement",
                "answer_quote": "Civil aviation requirement", "evidence_ids": ["E1"],
                "supporting_quotes": [{"evidence_id": "E1", "quote": "Civil aviation requirement"}]
            }],
        })}}]}

    monkeypatch.setattr(generation, "_request", fake_request)
    with pytest.raises(ValueError, match="冻结且保持相同"):
        generation.generate_batch(job_id, split="test")
    progress = []
    first = generation.generate_batch(job_id, per_unit=3, max_new=4, progress=lambda state: progress.append(state))
    assert first["created"] == 4
    assert first["failed"] == 0
    assert len(progress) == first["processed"]
    second = generation.generate_batch(job_id, per_unit=3, max_new=4)
    assert second["existing"] == 4
    assert second["created"] == 2
    assert len(core.candidates_for(job_id)) == 6
    frozen = generation.freeze_recipe(job_id)
    assert frozen["fingerprint"]["prompt_sha256"]
    test_result = generation.generate_batch(job_id, per_unit=3, split="test")
    assert test_result["created"] == 3
    assert sum(r["split"] == "test" for r in core.candidates_for(job_id)) == 3
    assert generation.generate_batch(job_id, per_unit=3, split="test")["existing"] == 3
    preview_path, _, _ = core.export_job(job_id, final=False, include_audit=True)
    with zipfile.ZipFile(preview_path) as archive:
        assert json.loads(archive.read("generation_recipe.json"))["fingerprint"] == frozen["fingerprint"]
    monkeypatch.setattr(generation, "recipe_fingerprint", lambda c: {"changed": True})
    with pytest.raises(ValueError, match="冻结且保持相同"):
        generation.generate_batch(job_id, per_unit=3, split="test")
    with pytest.raises(ValueError, match="已冻结"):
        generation.freeze_recipe(job_id)
