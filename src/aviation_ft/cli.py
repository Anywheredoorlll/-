from __future__ import annotations

import argparse
import json

from aviation_ft import automation, core, generation, migration, workflow


def main() -> None:
    parser = argparse.ArgumentParser(description="民航回答微调数据集构建")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="列出服务器任务")
    commands.add_parser("model-probe", help="检查本机模型推理服务")
    generate = commands.add_parser("generate", help="用已确认的证据生成一条待审核候选")
    generate.add_argument("job_id")
    generate.add_argument("evidence_unit_id")
    generate.add_argument("--variant", choices=list(generation.VARIANTS), default="fact")
    batch = commands.add_parser("generate-batch", help="批量生成待审候选，可中断后重试")
    batch.add_argument("job_id")
    batch.add_argument("--per-unit", type=int, default=8)
    batch.add_argument("--max-new", type=int, default=300)
    batch.add_argument("--split", choices=["development", "test"], default="development")
    freeze = commands.add_parser("freeze-recipe", help="锁定生成配方，之后可生成测试集候选")
    freeze.add_argument("job_id")
    status = commands.add_parser("status", help="查看任务进度和下一步操作")
    status.add_argument("job_id")
    autopilot = commands.add_parser("autopilot", help="自动选择证据、生成、核验并导出数据集")
    autopilot.add_argument("job_id")
    autopilot.add_argument("--until-ready", action="store_true", help="连续执行直到正式包可导出或需要人工介入")
    autopilot.add_argument("--max-new", type=int, default=200)
    quality = commands.add_parser("quality", help="查看质量门槛")
    quality.add_argument("job_id")
    cleanup = commands.add_parser("cleanup", help="列出到期的正式导出任务；默认只预览")
    cleanup.add_argument("--execute", action="store_true", help="真正清理到期任务")
    migrate = commands.add_parser("migrate", help="显式复制旧任务并重解析；默认 dry-run")
    migrate.add_argument("job_id")
    migrate.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.command == "list":
        print(json.dumps(core.list_jobs(), ensure_ascii=False, indent=2))
    elif args.command == "model-probe":
        print(json.dumps(generation.probe(), ensure_ascii=False, indent=2))
    elif args.command == "generate":
        print(json.dumps(generation.generate_candidate(args.job_id, args.evidence_unit_id, args.variant), ensure_ascii=False, indent=2))
    elif args.command == "generate-batch":
        def show_progress(summary):
            if summary["processed"] % 10 == 0 or summary["processed"] == summary["total_planned"]:
                print(json.dumps(summary, ensure_ascii=False), flush=True)

        print(json.dumps(generation.generate_batch(
            args.job_id, args.per_unit, args.max_new, show_progress, args.split,
        ), ensure_ascii=False, indent=2))
    elif args.command == "freeze-recipe":
        print(json.dumps(generation.freeze_recipe(args.job_id), ensure_ascii=False, indent=2))
    elif args.command == "status":
        print(json.dumps(workflow.task_progress(args.job_id), ensure_ascii=False, indent=2))
    elif args.command == "autopilot":
        for _ in range(30 if args.until_ready else 1):
            try:
                result = automation.advance(args.job_id, max_new=args.max_new)
            except Exception as exc:
                result = {"status": "failed", "reason": str(exc)}
            core.append_jsonl(core.job_dir(args.job_id) / "automation/progress.jsonl",
                              {"at": core.now(), "status": result["status"], "summary": result})
            print(json.dumps(result, ensure_ascii=False), flush=True)
            if result["status"] == "failed":
                raise SystemExit(1)
            if result["status"] != "in_progress":
                break
    elif args.command == "quality":
        print(json.dumps(core.quality_report(args.job_id), ensure_ascii=False, indent=2))
    elif args.command == "cleanup":
        ids = core.expired_completed_jobs()
        print(json.dumps({"expired_final_jobs": ids, "execute": args.execute}, ensure_ascii=False, indent=2))
        if args.execute:
            for job_id in ids:
                p = core.job_dir(job_id)
                info = core.read_json(p / "exports/aviation-sft-final.sha256.json")
                if info:
                    core.cleanup_job(job_id, job_id, info["sha256"], reason="retention_expired")
                    print(f"cleaned {job_id}")
    elif args.command == "migrate":
        print(json.dumps(migration.migrate_job(args.job_id, dry_run=not args.execute),
                         ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
