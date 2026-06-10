"""Benchmark run orchestrator — bridges web panel to original harness_bench runners."""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from web.api.ws import ws_manager
from web.db.database import async_session
from web.db import crud
from web.db.models import (
    FailureReason,
    Run,
    RunStatus,
    TaskResult,
    TaskStatus,
    TestSource,
)

logger = logging.getLogger(__name__)

_TRANSIENT_ERROR_PATTERN = re.compile(
    r"(?:status\s+[45]\d\d|ECONN|ETIMEDOUT|EAI_AGAIN|socket hang up"
    r"|connection\s+(?:refused|reset|timed out)|network\s+(?:error|timeout)"
    r"|request\s+(?:failed|timeout)|fetch\s+failed)",
    re.IGNORECASE,
)


class BenchmarkOrchestrator:
    """Manages background benchmark runs, reporting progress via WebSocket."""

    def __init__(self) -> None:
        self._active_runs: dict[str, threading.Event] = {}
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="bench-run")
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def start_run(self, run_id: str) -> None:
        """Launch a benchmark run in a background thread."""
        cancel_event = threading.Event()
        self._active_runs[run_id] = cancel_event
        self._executor.submit(self._run_worker, run_id, cancel_event)

    def cancel_run(self, run_id: str) -> None:
        """Signal a running benchmark to stop."""
        event = self._active_runs.get(run_id)
        if event:
            event.set()

    def _run_worker(self, run_id: str, cancel_event: threading.Event) -> None:
        """Worker thread that executes a benchmark run."""
        try:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(self._execute_run(run_id, cancel_event))
        except Exception:
            logger.exception(f"Run {run_id} failed with exception")
            loop2 = asyncio.new_event_loop()
            loop2.run_until_complete(self._mark_run_failed(run_id))
        finally:
            self._active_runs.pop(run_id, None)

    async def _execute_run(self, run_id: str, cancel_event: threading.Event) -> None:
        """Main run execution logic."""
        async with async_session() as session:
            run = await crud.get_run(session, run_id)
            if run is None:
                return

            # Update status to running
            await crud.update_run_status(
                session, run_id, RunStatus.RUNNING,
                started_at=datetime.now(timezone.utc),
            )
            await session.commit()

        # Collect all tasks from the benchmark
        async with async_session() as session:
            bm = await crud.get_benchmark(session, run.benchmark_id)
            if bm is None:
                return

            all_tests = []
            for group in bm.groups:
                for test in group.tests:
                    all_tests.append(test)

            # Create TaskResult entries for each test
            for i, test in enumerate(all_tests):
                await crud.create_task_result(
                    session,
                    run_id=run_id,
                    test_id=test.id,
                    builtin_task_id=test.builtin_task_id,
                    task_name=test.name,
                    position=i,
                )
            await session.commit()

        # Try to import and run the actual benchmark
        start_time = time.time()
        completed = 0
        passed = 0
        failed = 0
        total_tokens = 0

        try:
            from harness_bench.tasks import ALL_TASKS, get_task

            # Map builtin_task_id → harness task
            builtin_map = {t.id: t for t in ALL_TASKS}

            # Map test definitions to check budget
            test_map = {}
            for group in bm.groups:
                for test in group.tests:
                    test_map[test.id] = (test, group)

            # Get task results from DB
            async with async_session() as session:
                task_results = await crud.get_task_results_for_run(session, run_id)

            for tr in task_results:
                if cancel_event.is_set():
                    break

                # Check if we have already exceeded global token budget
                if run.global_token_budget > 0 and total_tokens >= run.global_token_budget:
                    async with async_session() as session:
                        await crud.update_task_result(
                            session, tr.id,
                            status=TaskStatus.TOKEN_LIMIT,
                            message=f"Global token budget exceeded ({total_tokens} >= {run.global_token_budget})",
                            failure_reason=FailureReason.TOKEN_LIMIT_EXCEEDED,
                            finished_at=datetime.now(timezone.utc),
                        )
                        await session.commit()
                    completed += 1
                    failed += 1
                    continue

                task_id = tr.builtin_task_id
                if not task_id or task_id not in builtin_map:
                    # Skip non-builtin tasks (custom tasks need different handling)
                    async with async_session() as session:
                        await crud.update_task_result(
                            session, tr.id,
                            status=TaskStatus.ERROR,
                            message="Custom task execution not yet supported",
                            failure_reason=FailureReason.RUNTIME_ERROR,
                            finished_at=datetime.now(timezone.utc),
                        )
                        await session.commit()
                    completed += 1
                    failed += 1
                    continue

                harness_task = builtin_map[task_id]

                # Mark as running
                async with async_session() as session:
                    await crud.update_task_result(
                        session, tr.id,
                        status=TaskStatus.RUNNING,
                        started_at=datetime.now(timezone.utc),
                    )
                    await session.commit()

                # Execute via the appropriate runner
                task_run = await self._run_single_task(
                    harness_task, run, cancel_event
                )

                # Find test-level token budget
                test, group = test_map.get(tr.test_id, (None, None))
                test_budget = -1
                if test:
                    if test.token_budget > 0:
                        test_budget = test.token_budget
                    elif group and group.default_token_budget > 0:
                        test_budget = group.default_token_budget

                # Classify result
                status, failure = self._classify_result(task_run, run, test_budget)
                task_tokens = getattr(task_run, "agent_total_tokens", None) or 0
                elapsed = getattr(task_run, "elapsed_seconds", 0.0)
                tps = task_tokens / elapsed if elapsed > 0 and task_tokens else None

                async with async_session() as session:
                    await crud.update_task_result(
                        session, tr.id,
                        status=status,
                        message=getattr(task_run, "message", ""),
                        error_detail=getattr(task_run, "error", None),
                        elapsed_seconds=elapsed,
                        agent_steps=getattr(task_run, "agent_steps", None),
                        agent_tool_calls=getattr(task_run, "agent_tool_calls", None),
                        agent_shell_commands=getattr(task_run, "agent_shell_commands", None),
                        agent_llm_calls=getattr(task_run, "agent_llm_calls", None),
                        agent_input_tokens=getattr(task_run, "agent_input_tokens", None),
                        agent_output_tokens=getattr(task_run, "agent_output_tokens", None),
                        agent_total_tokens=task_tokens or None,
                        tokens_per_second=tps,
                        failure_reason=failure,
                        finished_at=datetime.now(timezone.utc),
                    )
                    await session.commit()

                completed += 1
                if status == TaskStatus.PASSED:
                    passed += 1
                else:
                    failed += 1
                total_tokens += task_tokens

                elapsed_total = time.time() - start_time
                total_tasks = len(task_results)
                est_remaining = None
                if completed > 0:
                    est_remaining = (elapsed_total / completed) * (total_tasks - completed)

                avg_tps = total_tokens / elapsed_total if elapsed_total > 0 else 0

                # Update run stats
                async with async_session() as session:
                    await crud.update_run_stats(
                        session, run_id,
                        completed_tasks=completed,
                        passed_tasks=passed,
                        failed_tasks=failed,
                        total_tokens=total_tokens,
                        avg_tokens_per_second=avg_tps,
                    )
                    await session.commit()

                # Broadcast WebSocket update
                if self._loop:
                    asyncio.run_coroutine_threadsafe(
                        ws_manager.send_task_update(run_id, {
                            "task_name": tr.task_name,
                            "status": status.value,
                            "tokens": task_tokens,
                            "elapsed": elapsed,
                        }, {
                            "completed_tasks": completed,
                            "passed_tasks": passed,
                            "failed_tasks": failed,
                            "elapsed_seconds": elapsed_total,
                            "estimated_remaining_seconds": est_remaining,
                            "total_tokens": total_tokens,
                            "tokens_per_second": avg_tps,
                        }),
                        self._loop,
                    )

        except ImportError:
            logger.warning("harness_bench not installed, cannot run builtin tasks")
        except Exception:
            logger.exception(f"Error during run {run_id}")

        # Finalize
        final_status = RunStatus.CANCELLED if cancel_event.is_set() else RunStatus.COMPLETED
        async with async_session() as session:
            await crud.update_run_status(
                session, run_id, final_status,
                finished_at=datetime.now(timezone.utc),
            )
            await crud.update_run_stats(
                session, run_id,
                completed_tasks=completed,
                passed_tasks=passed,
                failed_tasks=failed,
                total_tokens=total_tokens,
            )
            await session.commit()

        if self._loop:
            asyncio.run_coroutine_threadsafe(
                ws_manager.send_run_completed(run_id, {
                    "status": final_status.value,
                    "completed_tasks": completed,
                    "passed_tasks": passed,
                }),
                self._loop,
            )

    async def _run_single_task(self, harness_task, run: Run, cancel_event: threading.Event):
        """Run a single task using the appropriate runner."""
        from harness_bench.runner import TaskRun

        if cancel_event.is_set():
            return TaskRun(
                task_id=harness_task.id,
                passed=False,
                message="cancelled",
                elapsed_seconds=0.0,
            )

        harness_type = run.harness_type.value if hasattr(run.harness_type, "value") else run.harness_type

        try:
            if harness_type == "cli":
                return await self._run_cli_task(harness_task, run)
            elif harness_type == "openrouter":
                return await self._run_openrouter_task(harness_task, run)
            else:
                return await self._run_deepagents_task(harness_task, run)
        except Exception as e:
            return TaskRun(
                task_id=harness_task.id,
                passed=False,
                message="",
                elapsed_seconds=0.0,
                error=str(e),
            )

    async def _run_cli_task(self, task, run: Run):
        """Run a task via CLI runner."""
        import shlex
        from tempfile import TemporaryDirectory
        from harness_bench.runner import TaskRun

        cli_cmd = run.cli_command or "free-code -p --model haiku --dangerously-skip-permissions"
        timeout = run.timeout_seconds

        with TemporaryDirectory(prefix=f"bench_{task.id}_") as tmpdir:
            from pathlib import Path
            workspace = Path(tmpdir)

            # Setup files
            task.setup(workspace)

            # Build command
            argv = shlex.split(cli_cmd) + [task.prompt]

            import subprocess
            start = time.time()
            try:
                result = subprocess.run(
                    argv, cwd=workspace, capture_output=True, text=True,
                    timeout=timeout, encoding="utf-8", errors="replace",
                )
            except subprocess.TimeoutExpired:
                elapsed = time.time() - start
                return TaskRun(
                    task_id=task.id, passed=False,
                    message="timeout", elapsed_seconds=elapsed,
                )

            elapsed = time.time() - start

            # Verify
            vr = task.verify(workspace)
            return TaskRun(
                task_id=task.id,
                passed=vr.passed,
                message=vr.message,
                elapsed_seconds=elapsed,
            )

    async def _run_openrouter_task(self, task, run: Run):
        """Run a task via OpenRouter runner."""
        from harness_bench.runner_openrouter import run_task as or_run_task
        import os

        # Apply env vars
        old_env = {}
        if run.env_vars:
            for k, v in run.env_vars.items():
                old_env[k] = os.environ.get(k)
                os.environ[k] = str(v)

        # Apply model base url override if specified
        if run.base_url:
            old_env["OPENROUTER_BASE_URL"] = os.environ.get("OPENROUTER_BASE_URL")
            os.environ["OPENROUTER_BASE_URL"] = run.base_url

        try:
            loop = asyncio.get_running_loop()
            task_run = await loop.run_in_executor(
                None,
                lambda: or_run_task(
                    task,
                    model_name=run.model or "qwen/qwen3.6-plus",
                    recursion_limit=run.recursion_limit or 80,
                )
            )
            return task_run
        finally:
            # Restore env vars
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    async def _run_deepagents_task(self, task, run: Run):
        """Run a task via DeepAgents/Pure GigaChat runner."""
        import os
        harness_type = run.harness_type.value if hasattr(run.harness_type, "value") else run.harness_type
        
        if harness_type == "pure":
            from harness_bench.runner_pure import run_task as pure_run_task
            run_fn = pure_run_task
        else:
            from harness_bench.runner import run_task as da_run_task
            run_fn = da_run_task

        # Apply env vars
        old_env = {}
        if run.env_vars:
            for k, v in run.env_vars.items():
                old_env[k] = os.getenv(k)
                os.environ[k] = str(v)

        if run.model:
            old_env["GIGACHAT_MODEL"] = os.getenv("GIGACHAT_MODEL")
            os.environ["GIGACHAT_MODEL"] = run.model

        if run.base_url:
            old_env["GIGACHAT_BASE_URL"] = os.getenv("GIGACHAT_BASE_URL")
            os.environ["GIGACHAT_BASE_URL"] = run.base_url

        try:
            loop = asyncio.get_running_loop()
            task_run = await loop.run_in_executor(
                None,
                lambda: run_fn(
                    task,
                    recursion_limit=run.recursion_limit or 80,
                )
            )
            return task_run
        finally:
            # Restore env vars
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def _classify_result(self, task_run, run: Run, test_budget: int = -1) -> tuple[TaskStatus, FailureReason | None]:
        """Classify task result into status + failure reason."""
        if getattr(task_run, "passed", False):
            return TaskStatus.PASSED, None

        msg = getattr(task_run, "message", "") or ""
        error = getattr(task_run, "error", "") or ""
        combined = f"{msg} {error}".lower()

        if "recursion limit" in combined or "loop" in combined:
            return TaskStatus.LOOP, FailureReason.RECURSION_LOOP

        if "timeout" in combined:
            return TaskStatus.TIMEOUT, FailureReason.TIMEOUT

        if "cancelled" in combined:
            return TaskStatus.CANCELLED, FailureReason.CANCELLED

        tokens = getattr(task_run, "agent_total_tokens", None) or 0
        budget = run.global_token_budget
        if budget > 0 and tokens >= budget:
            return TaskStatus.TOKEN_LIMIT, FailureReason.TOKEN_LIMIT_EXCEEDED

        if test_budget > 0 and tokens >= test_budget:
            return TaskStatus.TOKEN_LIMIT, FailureReason.TOKEN_LIMIT_EXCEEDED

        if _TRANSIENT_ERROR_PATTERN.search(combined):
            return TaskStatus.PROVIDER_UNAVAILABLE, FailureReason.PROVIDER_UNAVAILABLE

        if error:
            return TaskStatus.ERROR, FailureReason.RUNTIME_ERROR

        return TaskStatus.FAILED, FailureReason.VERIFIER_FAILED

    async def _mark_run_failed(self, run_id: str) -> None:
        async with async_session() as session:
            await crud.update_run_status(
                session, run_id, RunStatus.FAILED,
                finished_at=datetime.now(timezone.utc),
            )
            await session.commit()


# Singleton
orchestrator = BenchmarkOrchestrator()
