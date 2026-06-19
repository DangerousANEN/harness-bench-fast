"""CLI Test Runner — script to manage and test benchmarks and runs directly via terminal.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from sqlalchemy.orm import selectinload

# Add parent directory to sys.path to allow imports from web/
sys.path.append(str(Path(__file__).resolve().parent.parent))

from web.db.database import init_db, async_session, close_db
from web.db import crud
from web.db.models import (
    Benchmark,
    BenchmarkType,
    Run,
    RunStatus,
    TaskResult,
    TaskStatus,
    HarnessType,
    TestSource,
    TestGroup,
)
from web.engine.orchestrator import orchestrator
from web.engine.patch_microbench import patch_microbench


# Apply patch at startup
patch_microbench()


async def list_suites():
    """List all benchmark suites in the database."""
    async with async_session() as session:
        result = await session.execute(
            select(Benchmark)
            .options(selectinload(Benchmark.groups).selectinload(TestGroup.tests))
            .order_by(Benchmark.created_at.desc())
        )
        benchmarks = result.scalars().all()
        
        print("\n=== BENCHMARK SUITES ===")
        if not benchmarks:
            print("No suites found. Run --create-suite to add one.")
            return

        for bm in benchmarks:
            # Count tests
            total_tests = 0
            for g in bm.groups:
                total_tests += len(g.tests)
            print(f"ID: {bm.id}")
            print(f"Name: {bm.name}")
            print(f"Type: {bm.benchmark_type.value if hasattr(bm.benchmark_type, 'value') else bm.benchmark_type}")
            print(f"Total tasks: {total_tests}")
            print("-" * 40)
        print()


async def create_suite(name: str, suite_type: str, task_filter: str | None = None):
    """Create a new suite and import tasks."""
    async with async_session() as session:
        # Resolve type
        bt = BenchmarkType.MICROBENCH if suite_type == "microbench" else BenchmarkType.HARNESS_BENCH
        
        bm = await crud.create_benchmark(session, name=name, description=f"CLI created suite of type {suite_type}")
        bm.benchmark_type = bt
        await session.flush()

        print(f"Created suite '{name}' with ID: {bm.id}")

        if bt == BenchmarkType.MICROBENCH:
            print("Importing MicroBench tasks...")
            from web.api.routers.benchmarks import _MICROBENCH_GROUPS
            try:
                from microbench12.tasks import list_task_ids, load_task, load_prompt
            except ImportError:
                print("Error: microbench12 package not installed.")
                await session.rollback()
                return

            all_tasks = [load_task(tid) for tid in list_task_ids()]
            task_map = {t.id: t for t in all_tasks}

            slugs_filter = [task_filter] if task_filter else None

            for group_name, slugs in _MICROBENCH_GROUPS.items():
                if slugs_filter:
                    slugs = [s for s in slugs if s in slugs_filter]
                matching = [task_map[s] for s in slugs if s in task_map]
                if not matching:
                    continue

                group = await crud.create_group(
                    session,
                    benchmark_id=bm.id,
                    name=group_name,
                    description=f"{len(matching)} microbench tasks",
                )
                for task in matching:
                    prompt_text = load_prompt(task.id)
                    tags = getattr(task, "tags", []) or []
                    if isinstance(tags, set):
                        tags = list(tags)

                    setup = {}
                    starter_files = getattr(task, "starter_files", None)
                    if starter_files and isinstance(starter_files, dict):
                        setup = {k: v for k, v in starter_files.items() if isinstance(v, str)}

                    await crud.create_test(
                        session,
                        group_id=group.id,
                        name=task.id,
                        prompt=prompt_text,
                        tags=tags,
                        setup_files=setup,
                        verifier_type="microbench_grader",
                        source=TestSource.BUILTIN,
                        microbench_task_id=task.id,
                        grader_script=f"tasks/{task.id}/grader/grade.py",
                    )
            print("Successfully imported MicroBench tasks!")

        else:
            print("Importing built-in Harness Bench tasks...")
            from harness_bench.tasks import ALL_TASKS
            from web.api.routers.benchmarks import _task_group_name, _verifier_type_name

            task_groups = {}
            for task in ALL_TASKS:
                group_name = _task_group_name(task)
                task_groups.setdefault(group_name, []).append(task)

            for group_name, tasks in task_groups.items():
                if task_filter:
                    tasks = [t for t in tasks if t.id == task_filter or t.name == task_filter]
                if not tasks:
                    continue

                group = await crud.create_group(
                    session,
                    benchmark_id=bm.id,
                    name=group_name,
                    description=f"{len(tasks)} built-in tasks",
                )
                for task in tasks:
                    tags = list(task.tags) if task.tags else []
                    setup = {k: v for k, v in (task.setup_files or {}).items() if isinstance(v, str)}
                    gold = {k: v for k, v in (task.gold_files or {}).items() if isinstance(v, str)}

                    await crud.create_test(
                        session,
                        group_id=group.id,
                        name=task.name,
                        prompt=task.prompt,
                        tags=tags,
                        setup_files=setup,
                        gold_files=gold,
                        verifier_type=_verifier_type_name(task),
                        verifier_config={},
                        source=TestSource.BUILTIN,
                        builtin_task_id=task.id,
                    )
            print("Successfully imported Harness Bench tasks!")

        await session.commit()


async def run_suite(suite_id: str, harness: str, model: str, command: str, base_url: str | None, env_vars_text: str):
    """Launch a run and monitor it."""
    async with async_session() as session:
        bm = await crud.get_benchmark(session, suite_id)
        if bm is None:
            print(f"Error: Suite with ID {suite_id} not found.")
            return

        # Parse env vars
        env_vars = {}
        if env_vars_text:
            try:
                env_vars = json.loads(env_vars_text)
            except json.JSONDecodeError as e:
                print(f"Error parsing environment variables JSON: {e}")
                return

        # Resolve harness type
        try:
            ht = HarnessType(harness)
        except ValueError:
            print(f"Error: Invalid harness type. Must be one of {[h.value for h in HarnessType]}")
            return

        total = sum(len(g.tests) for g in bm.groups)
        if total == 0:
            print("Error: Suite has no tasks.")
            return

        run_name = f"CLI Run — {model or harness} — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}"
        run = await crud.create_run(
            session,
            name=run_name,
            benchmark_id=suite_id,
            harness_type=ht,
            model=model,
            base_url=base_url,
            cli_command=command,
            env_vars=env_vars,
            concurrency=1,
            recursion_limit=80,
            timeout_seconds=900,
        )
        run.total_tasks = total
        await session.commit()

        run_id = run.id
        print(f"Launched Run ID: {run_id}")
        print("Starting orchestrator runner...")
        
        # Start the run
        orchestrator.start_run(run_id)

    # Monitor loop
    completed_task_ids = set()
    while True:
        await asyncio.sleep(2)
        async with async_session() as session:
            run = await crud.get_run(session, run_id)
            if not run:
                print("Run deleted or not found.")
                break

            # Print updates for newly finished tasks
            for tr in run.task_results:
                if tr.status != TaskStatus.PENDING and tr.status != TaskStatus.RUNNING:
                    if tr.id not in completed_task_ids:
                        completed_task_ids.add(tr.id)
                        print(f"Task '{tr.task_name}' finished: [{tr.status.value.upper()}] - {tr.message}")
                        if tr.error_detail:
                            print(f"Log Detail:\n{tr.error_detail[:500]}...\n")

            print(f"Progress: {run.completed_tasks}/{run.total_tasks} tasks (Passed: {run.passed_tasks}, Failed: {run.failed_tasks})")

            if run.status in (RunStatus.COMPLETED, RunStatus.CANCELLED, RunStatus.FAILED):
                print(f"\nRun finished with status: {run.status.value.upper()}")
                break


async def main():
    parser = argparse.ArgumentParser(description="Harness Bench CLI Test Runner")
    parser.add_argument("--list", action="store_true", help="List all benchmark suites")
    parser.add_argument("--create-suite", help="Name of new suite to create")
    parser.add_argument("--type", choices=["harness", "microbench"], default="microbench", help="Type of suite to create")
    parser.add_argument("--task", help="Optional: import only a specific task ID")
    parser.add_argument("--run", help="Suite ID to run")
    parser.add_argument("--harness", choices=["cli", "microbench_cli", "openrouter", "deepagents", "pure"], default="microbench_cli", help="Harness runner type")
    parser.add_argument("--model", default="openai/gpt-4o-mini", help="Model name / override")
    parser.add_argument("--command", help="Harness command template (e.g. 'hermes chat -m {model} -q')")
    parser.add_argument("--url", help="Base URL override")
    parser.add_argument("--env", default="{}", help="Environment variables as JSON string")

    args = parser.parse_args()

    await init_db()

    try:
        if args.list:
            await list_suites()
        elif args.create_suite:
            await create_suite(args.create_suite, args.type, args.task)
        elif args.run:
            # Default commands if not specified
            cmd = args.command
            if not cmd:
                if args.harness == "microbench_cli":
                    cmd = "hermes chat -m {model} -q"
                else:
                    cmd = "free-code -p --model {model}"
            await run_suite(args.run, args.harness, args.model, cmd, args.url, args.env)
        else:
            parser.print_help()
    finally:
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())
