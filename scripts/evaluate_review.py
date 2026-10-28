"""Offline human-label evaluation; never calls a model or uploads a document.

JSONL fields: sample_id, doc_id, clause_group, phase (calibration/acceptance),
gold (correct/incorrect), decision (approved/rejected/needs_human), reviewer.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path


def interval(successes: int, total: int) -> dict:
    if not total:
        return {"numerator": successes, "denominator": total, "rate": None,
                "ci95": None}
    z = 1.959963984540054
    p = successes / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    radius = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return {"numerator": successes, "denominator": total, "rate": p,
            "ci95": [max(0, center - radius), min(1, center + radius)]}


def evaluate(rows: list[dict]) -> dict:
    required = {"sample_id", "doc_id", "clause_group", "phase", "gold", "decision", "reviewer"}
    seen, groups = set(), {"calibration": set(), "acceptance": set()}
    for row in rows:
        if (set(row) != required or not all(isinstance(row[key], str) and row[key].strip() for key in required)
                or row["phase"] not in groups or row["gold"] not in {"correct", "incorrect"}
                or row["decision"] not in {"approved", "rejected", "needs_human"}
                or row["sample_id"] in seen):
            raise ValueError("标注行字段、枚举或 sample_id 无效")
        seen.add(row["sample_id"])
        groups[row["phase"]].add((row["doc_id"], row["clause_group"]))
    if groups["calibration"] & groups["acceptance"]:
        raise ValueError("校准集与独立验收集存在同文档同条款组重叠")
    reports = {}
    for phase in groups:
        items = [r for r in rows if r["phase"] == phase]
        approved = [r for r in items if r["decision"] == "approved"]
        incorrect = [r for r in items if r["gold"] == "incorrect"]
        correct = [r for r in items if r["gold"] == "correct"]
        reports[phase] = {"samples": len(items), "documents": len({r["doc_id"] for r in items}),
            "clause_groups": len(groups[phase]),
            "auto_approval_error_rate": interval(sum(r["gold"] == "incorrect" for r in approved), len(approved)),
            "incorrect_release_rate": interval(sum(r["decision"] == "approved" for r in incorrect), len(incorrect)),
            "correct_false_reject_rate": interval(sum(r["decision"] == "rejected" for r in correct), len(correct)),
            "human_referral_rate": interval(sum(r["decision"] == "needs_human" for r in items), len(items)),
            "auto_coverage_rate": interval(len(approved), len(items))}
    acceptance = reports["acceptance"]
    calibration = reports["calibration"]
    accepted = (all(any(r["phase"] == phase and r["gold"] == gold for r in rows)
                    for phase in ("calibration", "acceptance")
                    for gold in ("correct", "incorrect"))
                and acceptance["auto_approval_error_rate"]["ci95"] is not None
                and acceptance["auto_approval_error_rate"]["ci95"][1] <= 0.01)
    return {"calibration": calibration, "acceptance": acceptance,
            "accepted": accepted, "target": "独立验收集自动通过错误率 95% Wilson 上界 ≤ 1%；仍需真实人工标注"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("labels", type=Path)
    parser.add_argument("--job-id")
    parser.add_argument("--calibration-id")
    parser.add_argument("--write-certificate", action="store_true")
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.labels.read_text(encoding="utf-8").splitlines() if line.strip()]
    result = evaluate(rows)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.write_certificate:
        if not result["accepted"] or not args.job_id or not args.calibration_id or not args.calibration_id.isidentifier():
            raise ValueError("验收门槛未达到，或缺少有效任务与校准 ID；不生成证书")
        from aviation_ft import core, review_policy
        core.load_job(args.job_id)
        scope = [r for r in rows if r["phase"] == "acceptance"]
        certificate = {"calibration_id": args.calibration_id, "accepted": True,
            "job_id": args.job_id, "created_at": core.now(),
            "labels_sha256": core.sha256_file(args.labels),
            "recipe_fingerprint": review_policy.recipe_fingerprint(core.ROOT, args.job_id),
            "scope_doc_ids": sorted({r["doc_id"] for r in scope}),
            "scope_clause_groups": sorted({r["clause_group"] for r in scope}),
            "scope_groups": sorted([{"doc_id": doc, "clause_group": group}
                                    for doc, group in {(r["doc_id"], r["clause_group"]) for r in scope}],
                                   key=lambda x: (x["doc_id"], x["clause_group"])),
            "evaluation": result}
        target = core.ROOT / "runtime/calibrations" / f"{args.calibration_id}.json"
        if target.exists():
            raise ValueError("同名校准证书已存在，不能覆盖")
        target.parent.mkdir(parents=True, exist_ok=True)
        core.write_json(target, certificate)
        print(f"校准证书：{target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
