"""Explicit, non-destructive legacy task migration into a new parse-v2 task."""
from __future__ import annotations

import shutil

from aviation_ft import core


def migrate_job(job_id: str, *, dry_run: bool = True) -> dict:
    source = core.job_dir(job_id)
    meta = core.load_job(job_id)
    legacy = {
        "documents": len(meta["documents"]),
        "pages": len(core.pages_for(job_id)),
        "evidence": len(core.evidence_for(job_id)),
        "candidates": len(core.candidates_for(job_id)),
        "frozen": (source / "evidence/splits.json").exists(),
    }
    plan = {"source_job_id": job_id, "dry_run": dry_run, "legacy": legacy,
            "strategy": "copy original PDFs and legacy audit into a new task, then parse-v2; all evidence and answers require new verification"}
    if dry_run:
        return plan
    for document in meta["documents"]:
        pdf = source / "raw_pdf" / document["filename"]
        if not pdf.is_file() or pdf.is_symlink() or core.sha256_file(pdf) != document["sha256"]:
            raise ValueError("旧任务原始 PDF 缺失或哈希变化")
    new_id = core.create_job(f"{meta.get('label', '')} · parse-v2 migration")
    target = core.job_dir(new_id)
    try:
        for document in meta["documents"]:
            shutil.copy2(source / "raw_pdf" / document["filename"],
                         target / "raw_pdf" / document["filename"])
        new_meta = core.load_job(new_id)
        new_meta["documents"] = [dict(d) for d in meta["documents"]]
        new_meta["source_job_id"] = job_id
        new_meta["migration_status"] = "needs_source_and_answer_review"
        core.write_json(target / "job.json", new_meta)
        audit = target / "migration/legacy"
        audit.mkdir(parents=True)
        for relative in ("job.json", "parsed/pages.jsonl", "parsed/report.json",
                         "evidence/units.jsonl", "evidence/splits.json",
                         "candidates/records.jsonl", "reviewed/events.jsonl"):
            old_file = source / relative
            if old_file.is_file():
                destination = audit / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(old_file, destination)
        core.parse_job(new_id)
        core.write_json(target / "migration/manifest.json", {**plan, "dry_run": False,
            "new_job_id": new_id, "at": core.now(), "legacy_snapshot": "migration/legacy",
            "evidence_status": "must_rebuild_and_verify", "candidate_status": "historical_only"})
        return {**plan, "new_job_id": new_id, "dry_run": False}
    except Exception:
        # Keep the partially created task for inspection; never touch the source task.
        raise
