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
    BenchmarkType,
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


def parse_tokens_from_text(text: str) -> dict[str, int]:
    # Match various formats
    prompt_patterns = [
        r"(?:prompt|input)[_-]tokens\s*[:=]\s*(\d+)",
        r"(?:prompt|input)\s+tokens\s*[:=]?\s*(\d+)",
        r"(\d+)\s*(?:prompt|input)\s+tokens",
        r"(?:prompt|input)\s*[:=]\s*(\d+)\s*tokens",
    ]
    completion_patterns = [
        r"(?:completion|output)[_-]tokens\s*[:=]\s*(\d+)",
        r"(?:completion|output)\s+tokens\s*[:=]?\s*(\d+)",
        r"(\d+)\s*(?:completion|output)\s+tokens",
        r"(?:completion|output)\s*[:=]\s*(\d+)\s*tokens",
    ]
    total_patterns = [
        r"total[_-]tokens\s*[:=]\s*(\d+)",
        r"total\s+tokens\s*[:=]?\s*(\d+)",
        r"(\d+)\s*total\s+tokens",
        r"total\s*[:=]\s*(\d+)\s*tokens",
    ]
    step_patterns = [
        r"steps?\s*[:=]\s*(\d+)",
        r"(\d+)\s*steps?",
    ]
    tool_patterns = [
        r"tool[_-]calls?\s*[:=]\s*(\d+)",
        r"(\d+)\s*tool[_-]calls?",
    ]

    prompt = None
    completion = None
    total = None
    steps = None
    tools = None

    for p in prompt_patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            prompt = int(m.group(1))
            break
    for p in completion_patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            completion = int(m.group(1))
            break
    for p in total_patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            total = int(m.group(1))
            break
    for p in step_patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            steps = int(m.group(1))
            break
    for p in tool_patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            tools = int(m.group(1))
            break

    if total is None and (prompt is not None or completion is not None):
        total = (prompt or 0) + (completion or 0)

    res = {}
    if prompt is not None:
        res["agent_input_tokens"] = prompt
    if completion is not None:
        res["agent_output_tokens"] = completion
    if total is not None:
        res["agent_total_tokens"] = total
    if steps is not None:
        res["agent_steps"] = steps
    if tools is not None:
        res["agent_tool_calls"] = tools
    return res


def get_hermes_tokens_for_prompt(prompt: str, start_time: float, end_time: float) -> dict[str, int]:
    import os
    import sqlite3
    db_path = os.path.expanduser("~/.hermes/state.db")
    if not os.path.exists(db_path):
        return {}
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # 1. Search by message content matching the prompt (safest/robust for concurrency)
        prompt_sub = prompt.strip()[:100]
        cursor.execute("""
            SELECT s.input_tokens, s.output_tokens, s.tool_call_count, s.message_count
            FROM sessions s
            JOIN messages m ON m.session_id = s.id
            WHERE m.role IN ('user', 'human') AND m.content LIKE ?
            ORDER BY s.started_at DESC LIMIT 1
        """, (f"%{prompt_sub}%",))
        row = cursor.fetchone()
        if row:
            return {
                "agent_input_tokens": row[0],
                "agent_output_tokens": row[1],
                "agent_total_tokens": (row[0] or 0) + (row[1] or 0),
                "agent_tool_calls": row[2],
                "agent_steps": row[3],
            }
        
        # 2. Fallback: search for a session that started during execution timeframe
        cursor.execute("""
            SELECT input_tokens, output_tokens, tool_call_count, message_count
            FROM sessions
            WHERE started_at >= ? AND started_at <= ?
            ORDER BY started_at DESC LIMIT 1
        """, (start_time - 10, end_time + 10))
        row = cursor.fetchone()
        if row:
            return {
                "agent_input_tokens": row[0],
                "agent_output_tokens": row[1],
                "agent_total_tokens": (row[0] or 0) + (row[1] or 0),
                "agent_tool_calls": row[2],
                "agent_steps": row[3],
            }
    except Exception as e:
        logger.warning(f"Failed to query hermes database for stats: {e}")
    finally:
        if conn is not None:
            conn.close()
    return {}


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
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self._execute_run(run_id, cancel_event))
        except Exception:
            logger.exception(f"Run {run_id} failed with exception")
            try:
                loop.run_until_complete(self._mark_run_failed(run_id))
            except Exception:
                logger.exception(f"Failed to mark run {run_id} as failed")
        finally:
            try:
                loop.close()
            except Exception:
                pass
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

        # Collect all tasks from the benchmark and build budget maps
        test_map = {}
        is_microbench = False
        async with async_session() as session:
            bm = await crud.get_benchmark(session, run.benchmark_id)
            if bm is None:
                return

            # Check benchmark type
            bm_type = getattr(bm, 'benchmark_type', None)
            is_microbench = (
                bm_type == BenchmarkType.MICROBENCH
                or (hasattr(bm_type, 'value') and bm_type.value == 'microbench')
            )
            harness_type_val = run.harness_type.value if hasattr(run.harness_type, 'value') else run.harness_type
            if harness_type_val == 'microbench_cli':
                is_microbench = True

            all_tests = []
            for group in bm.groups:
                group_default_budget = group.default_token_budget or -1
                for test in group.tests:
                    all_tests.append(test)
                    test_map[test.id] = {
                        "token_budget": test.token_budget or -1,
                        "default_token_budget": group_default_budget
                    }

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
            if is_microbench:
                await self._execute_microbench_run(
                    run_id, run, None, cancel_event,
                    start_time, completed, passed, failed, total_tokens,
                )
                return

            from harness_bench.tasks import ALL_TASKS, get_task

            # Map builtin_task_id → harness task
            builtin_map = {t.id: t for t in ALL_TASKS}

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
                test_info = test_map.get(tr.test_id)
                test_budget = -1
                if test_info:
                    if test_info["token_budget"] > 0:
                        test_budget = test_info["token_budget"]
                    elif test_info["default_token_budget"] > 0:
                        test_budget = test_info["default_token_budget"]

                # Classify result
                status, failure = self._classify_result(task_run, run, test_budget)
                task_tokens = getattr(task_run, "agent_total_tokens", None) or 0
                elapsed = getattr(task_run, "elapsed_seconds", 0.0)
                tps = task_tokens / elapsed if elapsed > 0 and task_tokens else None

                error_detail = getattr(task_run, "error", None) or ""
                transcript = getattr(task_run, "agent_transcript", None) or ""
                if transcript:
                    if error_detail:
                        combined_detail = f"Execution Logs:\n{error_detail}\n\n=== AGENT TRANSCRIPT ===\n{transcript}"
                    else:
                        combined_detail = transcript
                else:
                    combined_detail = error_detail or None

                async with async_session() as session:
                    await crud.update_task_result(
                        session, tr.id,
                        status=status,
                        message=getattr(task_run, "message", ""),
                        error_detail=combined_detail,
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

    async def _execute_microbench_run(
        self,
        run_id: str,
        run: Run,
        bm,
        cancel_event: threading.Event,
        start_time: float,
        completed: int,
        passed: int,
        failed: int,
        total_tokens: int,
    ) -> None:
        """Execute a full benchmark run for MicroBench-16 tasks."""
        from web.engine.orchestrator_microbench import MicroBenchRunner

        runner = MicroBenchRunner()

        # Get task results and benchmark info from DB
        test_map = {}
        async with async_session() as session:
            task_results = await crud.get_task_results_for_run(session, run_id)
            bm_db = await crud.get_benchmark(session, run.benchmark_id)
            if bm_db:
                for group in bm_db.groups:
                    for test in group.tests:
                        test_map[test.id] = getattr(test, 'microbench_task_id', None) or test.builtin_task_id

        for tr in task_results:
            if cancel_event.is_set():
                break

            # Find the microbench task ID
            microbench_task_id = test_map.get(tr.test_id) or tr.builtin_task_id

            if not microbench_task_id:
                async with async_session() as session:
                    await crud.update_task_result(
                        session, tr.id,
                        status=TaskStatus.ERROR,
                        message="No microbench task ID found",
                        failure_reason=FailureReason.RUNTIME_ERROR,
                        finished_at=datetime.now(timezone.utc),
                    )
                    await session.commit()
                completed += 1
                failed += 1
                continue

            # Mark as running
            async with async_session() as session:
                await crud.update_task_result(
                    session, tr.id,
                    status=TaskStatus.RUNNING,
                    started_at=datetime.now(timezone.utc),
                )
                await session.commit()

            # Execute via MicroBenchRunner
            cli_cmd = run.cli_command or "hermes"
            env_vars = run.env_vars or {}

            mb_result = await runner.run_task(
                task_id=microbench_task_id,
                cli_command=cli_cmd,
                model=run.model or "",
                base_url=run.base_url,
                timeout=run.timeout_seconds,
                env_vars=env_vars,
            )

            # Classify result
            if mb_result.passed:
                status = TaskStatus.PASSED
                failure = None
            else:
                msg = (mb_result.message or "").lower()
                if "timeout" in msg:
                    status = TaskStatus.TIMEOUT
                    failure = FailureReason.TIMEOUT
                elif mb_result.error:
                    status = TaskStatus.ERROR
                    failure = FailureReason.RUNTIME_ERROR
                else:
                    status = TaskStatus.FAILED
                    failure = FailureReason.VERIFIER_FAILED

            task_tokens = mb_result.agent_total_tokens or 0
            tps = task_tokens / mb_result.elapsed_seconds if mb_result.elapsed_seconds > 0 and task_tokens else None

            async with async_session() as session:
                await crud.update_task_result(
                    session, tr.id,
                    status=status,
                    message=mb_result.message,
                    error_detail=mb_result.agent_transcript,
                    elapsed_seconds=mb_result.elapsed_seconds,
                    agent_steps=mb_result.agent_steps,
                    agent_tool_calls=mb_result.agent_tool_calls,
                    agent_input_tokens=mb_result.agent_input_tokens,
                    agent_output_tokens=mb_result.agent_output_tokens,
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

            if self._loop:
                asyncio.run_coroutine_threadsafe(
                    ws_manager.send_task_update(run_id, {
                        "task_name": tr.task_name,
                        "status": status.value,
                        "tokens": task_tokens,
                        "elapsed": mb_result.elapsed_seconds,
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

    async def _run_cli_task(self, task, run: Run):
        """Run a task via CLI runner."""
        import shlex
        import os
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
            resolved_cmd = cli_cmd.replace("{workspace}", str(workspace))
            if run.model:
                resolved_cmd = resolved_cmd.replace("{model}", run.model)
            argv = shlex.split(resolved_cmd) + [task.prompt]

            import subprocess
            start = time.time()
            # Pass panel custom env vars to subprocess
            run_env = {**os.environ}
            if run.env_vars:
                run_env.update({k: str(v) for k, v in run.env_vars.items()})
            try:
                result = subprocess.run(
                    argv, cwd=workspace, capture_output=True, text=True,
                    timeout=timeout, encoding="utf-8", errors="replace",
                    env=run_env,
                )
            except subprocess.TimeoutExpired:
                elapsed = time.time() - start
                return TaskRun(
                    task_id=task.id, passed=False,
                    message="timeout", elapsed_seconds=elapsed,
                )

            elapsed = time.time() - start
            end = time.time()

            # Query stats from Hermes DB or fallback to regex
            stats = {}
            if os.path.exists(os.path.expanduser("~/.hermes/state.db")):
                stats = get_hermes_tokens_for_prompt(task.prompt, start, end)
            
            if not stats or "agent_total_tokens" not in stats:
                stats = parse_tokens_from_text(result.stdout + "\n" + result.stderr)

            # Verify
            vr = task.verify(workspace)
            
            # Format clean log
            console_log = f"--- STDOUT ---\n{result.stdout}\n--- STDERR ---\n{result.stderr}"
            
            return TaskRun(
                task_id=task.id,
                passed=vr.passed,
                message=vr.message,
                elapsed_seconds=elapsed,
                error=console_log,
                **stats
            )

    async def _run_openrouter_task(self, task, run: Run):
        """Run a task via OpenRouter runner in a subprocess for env isolation."""
        import sys
        import os
        import json
        import subprocess
        from tempfile import NamedTemporaryFile
        from harness_bench.runner import TaskRun

        run_env = {**os.environ}
        if run.env_vars:
            run_env.update({k: str(v) for k, v in run.env_vars.items()})

        # Apply model base url override if specified
        if run.base_url:
            run_env["OPENROUTER_BASE_URL"] = run.base_url

        # Generate a unique path for the JSON results
        with NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            argv = [
                sys.executable, "-m", "harness_bench", "run-openrouter",
                "--task", task.id,
                "--model", run.model or "qwen/qwen3.6-plus",
                "--recursion-limit", str(run.recursion_limit or 80),
                "--json-output", tmp_path,
            ]
            
            # Run in subprocess
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    argv, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", env=run_env
                )
            )
            
            # Read output JSON
            with open(tmp_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                
            task_data = data["tasks"][0]
            return TaskRun(
                task_id=task_data["task_id"],
                passed=task_data["passed"],
                message=task_data["message"],
                elapsed_seconds=task_data["elapsed_seconds"],
                error=task_data.get("error"),
                agent_steps=task_data.get("agent_steps"),
                agent_tool_calls=task_data.get("agent_tool_calls"),
                agent_shell_commands=task_data.get("agent_shell_commands"),
                agent_events=task_data.get("agent_events"),
                agent_llm_calls=task_data.get("agent_llm_calls"),
                agent_input_tokens=task_data.get("agent_input_tokens"),
                agent_output_tokens=task_data.get("agent_output_tokens"),
                agent_total_tokens=task_data.get("agent_total_tokens"),
            )
        except Exception as e:
            return TaskRun(
                task_id=task.id,
                passed=False,
                message="",
                elapsed_seconds=0.0,
                error=f"Subprocess run failed: {e}",
            )
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    async def _run_deepagents_task(self, task, run: Run):
        """Run a task via DeepAgents/Pure GigaChat runner in a subprocess for env isolation."""
        import sys
        import os
        import json
        import subprocess
        from tempfile import NamedTemporaryFile
        from harness_bench.runner import TaskRun

        harness_type = run.harness_type.value if hasattr(run.harness_type, "value") else run.harness_type
        subcmd = "run-pure" if harness_type == "pure" else "run"

        run_env = {**os.environ}
        if run.env_vars:
            run_env.update({k: str(v) for k, v in run.env_vars.items()})

        if run.model:
            run_env["GIGACHAT_MODEL"] = run.model

        if run.base_url:
            run_env["GIGACHAT_BASE_URL"] = run.base_url

        with NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            argv = [
                sys.executable, "-m", "harness_bench", subcmd,
                "--task", task.id,
                "--recursion-limit", str(run.recursion_limit or 80),
                "--json-output", tmp_path,
            ]
            
            # Run in subprocess
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    argv, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", env=run_env
                )
            )
            
            # Read output JSON
            with open(tmp_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                
            task_data = data["tasks"][0]
            return TaskRun(
                task_id=task_data["task_id"],
                passed=task_data["passed"],
                message=task_data["message"],
                elapsed_seconds=task_data["elapsed_seconds"],
                error=task_data.get("error"),
                agent_steps=task_data.get("agent_steps"),
                agent_tool_calls=task_data.get("agent_tool_calls"),
                agent_shell_commands=task_data.get("agent_shell_commands"),
                agent_events=task_data.get("agent_events"),
                agent_llm_calls=task_data.get("agent_llm_calls"),
                agent_input_tokens=task_data.get("agent_input_tokens"),
                agent_output_tokens=task_data.get("agent_output_tokens"),
                agent_total_tokens=task_data.get("agent_total_tokens"),
            )
        except Exception as e:
            return TaskRun(
                task_id=task.id,
                passed=False,
                message="",
                elapsed_seconds=0.0,
                error=f"Subprocess run failed: {e}",
            )
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

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
