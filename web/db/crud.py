"""CRUD operations for the benchmark web panel."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from web.db.models import (
    Benchmark,
    FailureReason,
    Run,
    RunStatus,
    TaskResult,
    TaskStatus,
    TestDefinition,
    TestGroup,
    TestSource,
)


# ---- Benchmarks ----


async def get_benchmarks(session: AsyncSession) -> Sequence[Benchmark]:
    result = await session.execute(
        select(Benchmark).options(selectinload(Benchmark.groups)).order_by(Benchmark.created_at.desc())
    )
    return result.scalars().all()


async def get_benchmark(session: AsyncSession, benchmark_id: str) -> Benchmark | None:
    result = await session.execute(
        select(Benchmark)
        .where(Benchmark.id == benchmark_id)
        .options(
            selectinload(Benchmark.groups).selectinload(TestGroup.tests)
        )
    )
    return result.scalar_one_or_none()


async def create_benchmark(
    session: AsyncSession, *, name: str, description: str = ""
) -> Benchmark:
    bm = Benchmark(name=name, description=description)
    session.add(bm)
    await session.flush()
    return bm


async def update_benchmark(
    session: AsyncSession,
    benchmark_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
) -> Benchmark | None:
    bm = await get_benchmark(session, benchmark_id)
    if bm is None:
        return None
    if name is not None:
        bm.name = name
    if description is not None:
        bm.description = description
    bm.updated_at = datetime.now(timezone.utc)
    await session.flush()
    return bm


async def delete_benchmark(session: AsyncSession, benchmark_id: str) -> bool:
    result = await session.execute(
        delete(Benchmark).where(Benchmark.id == benchmark_id)
    )
    return result.rowcount > 0


# ---- Test Groups ----


async def get_groups(
    session: AsyncSession, benchmark_id: str
) -> Sequence[TestGroup]:
    result = await session.execute(
        select(TestGroup)
        .where(TestGroup.benchmark_id == benchmark_id)
        .options(selectinload(TestGroup.tests))
        .order_by(TestGroup.position)
    )
    return result.scalars().all()


async def create_group(
    session: AsyncSession,
    *,
    benchmark_id: str,
    name: str,
    description: str = "",
    default_token_budget: int = -1,
    default_timeout: int = 600,
) -> TestGroup:
    # Get next position
    result = await session.execute(
        select(func.coalesce(func.max(TestGroup.position), -1) + 1).where(
            TestGroup.benchmark_id == benchmark_id
        )
    )
    position = result.scalar() or 0
    group = TestGroup(
        benchmark_id=benchmark_id,
        name=name,
        description=description,
        position=position,
        default_token_budget=default_token_budget,
        default_timeout=default_timeout,
    )
    session.add(group)
    await session.flush()
    return group


async def update_group(
    session: AsyncSession,
    group_id: str,
    **kwargs,
) -> TestGroup | None:
    result = await session.execute(
        select(TestGroup).where(TestGroup.id == group_id)
    )
    group = result.scalar_one_or_none()
    if group is None:
        return None
    for key, value in kwargs.items():
        if hasattr(group, key) and value is not None:
            setattr(group, key, value)
    group.updated_at = datetime.now(timezone.utc)
    await session.flush()
    return group


async def delete_group(session: AsyncSession, group_id: str) -> bool:
    result = await session.execute(
        delete(TestGroup).where(TestGroup.id == group_id)
    )
    return result.rowcount > 0


# ---- Test Definitions ----


async def get_tests(
    session: AsyncSession, group_id: str
) -> Sequence[TestDefinition]:
    result = await session.execute(
        select(TestDefinition)
        .where(TestDefinition.group_id == group_id)
        .order_by(TestDefinition.position)
    )
    return result.scalars().all()


async def get_test(session: AsyncSession, test_id: str) -> TestDefinition | None:
    result = await session.execute(
        select(TestDefinition).where(TestDefinition.id == test_id)
    )
    return result.scalar_one_or_none()


async def create_test(
    session: AsyncSession,
    *,
    group_id: str,
    name: str,
    prompt: str,
    tags: list[str] | None = None,
    setup_files: dict | None = None,
    gold_files: dict | None = None,
    verifier_type: str = "",
    verifier_config: dict | None = None,
    token_budget: int = -1,
    timeout_seconds: int = 600,
    source: TestSource = TestSource.CUSTOM,
    builtin_task_id: str | None = None,
) -> TestDefinition:
    result = await session.execute(
        select(func.coalesce(func.max(TestDefinition.position), -1) + 1).where(
            TestDefinition.group_id == group_id
        )
    )
    position = result.scalar() or 0
    test = TestDefinition(
        group_id=group_id,
        name=name,
        prompt=prompt,
        tags=tags or [],
        setup_files=setup_files or {},
        gold_files=gold_files or {},
        verifier_type=verifier_type,
        verifier_config=verifier_config or {},
        token_budget=token_budget,
        timeout_seconds=timeout_seconds,
        source=source,
        builtin_task_id=builtin_task_id,
        position=position,
    )
    session.add(test)
    await session.flush()
    return test


async def update_test(
    session: AsyncSession, test_id: str, **kwargs
) -> TestDefinition | None:
    result = await session.execute(
        select(TestDefinition).where(TestDefinition.id == test_id)
    )
    test = result.scalar_one_or_none()
    if test is None:
        return None
    for key, value in kwargs.items():
        if hasattr(test, key) and value is not None:
            setattr(test, key, value)
    test.updated_at = datetime.now(timezone.utc)
    await session.flush()
    return test


async def delete_test(session: AsyncSession, test_id: str) -> bool:
    result = await session.execute(
        delete(TestDefinition).where(TestDefinition.id == test_id)
    )
    return result.rowcount > 0


# ---- Runs ----


async def get_runs(
    session: AsyncSession, *, limit: int = 50, offset: int = 0
) -> Sequence[Run]:
    result = await session.execute(
        select(Run)
        .options(selectinload(Run.benchmark))
        .order_by(Run.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return result.scalars().all()


async def get_run(session: AsyncSession, run_id: str) -> Run | None:
    result = await session.execute(
        select(Run)
        .where(Run.id == run_id)
        .options(
            selectinload(Run.task_results),
            selectinload(Run.benchmark),
        )
    )
    return result.scalar_one_or_none()


async def create_run(
    session: AsyncSession,
    *,
    name: str,
    benchmark_id: str,
    harness_type: str,
    model: str = "",
    base_url: str | None = None,
    cli_command: str | None = None,
    env_vars: dict | None = None,
    concurrency: int = 5,
    recursion_limit: int = 100,
    timeout_seconds: int = 600,
    global_token_budget: int = -1,
) -> Run:
    run = Run(
        name=name,
        benchmark_id=benchmark_id,
        harness_type=harness_type,
        model=model,
        base_url=base_url,
        cli_command=cli_command,
        env_vars=env_vars or {},
        concurrency=concurrency,
        recursion_limit=recursion_limit,
        timeout_seconds=timeout_seconds,
        global_token_budget=global_token_budget,
    )
    session.add(run)
    await session.flush()
    return run


async def update_run_status(
    session: AsyncSession,
    run_id: str,
    status: RunStatus,
    **extra,
) -> None:
    values = {"status": status, **extra}
    await session.execute(
        update(Run).where(Run.id == run_id).values(**values)
    )
    await session.flush()


async def update_run_stats(
    session: AsyncSession,
    run_id: str,
    *,
    completed_tasks: int | None = None,
    passed_tasks: int | None = None,
    failed_tasks: int | None = None,
    total_tokens: int | None = None,
    total_input_tokens: int | None = None,
    total_output_tokens: int | None = None,
    avg_tokens_per_second: float | None = None,
) -> None:
    values = {}
    for key, val in {
        "completed_tasks": completed_tasks,
        "passed_tasks": passed_tasks,
        "failed_tasks": failed_tasks,
        "total_tokens": total_tokens,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "avg_tokens_per_second": avg_tokens_per_second,
    }.items():
        if val is not None:
            values[key] = val
    if values:
        await session.execute(
            update(Run).where(Run.id == run_id).values(**values)
        )
        await session.flush()


async def delete_run(session: AsyncSession, run_id: str) -> bool:
    result = await session.execute(delete(Run).where(Run.id == run_id))
    return result.rowcount > 0


# ---- Task Results ----


async def create_task_result(
    session: AsyncSession,
    *,
    run_id: str,
    test_id: str | None = None,
    builtin_task_id: str | None = None,
    task_name: str = "",
    position: int = 0,
) -> TaskResult:
    tr = TaskResult(
        run_id=run_id,
        test_id=test_id,
        builtin_task_id=builtin_task_id,
        task_name=task_name,
        position=position,
    )
    session.add(tr)
    await session.flush()
    return tr


async def update_task_result(
    session: AsyncSession, task_result_id: str, **kwargs
) -> None:
    await session.execute(
        update(TaskResult).where(TaskResult.id == task_result_id).values(**kwargs)
    )
    await session.flush()


async def get_task_results_for_run(
    session: AsyncSession, run_id: str
) -> Sequence[TaskResult]:
    result = await session.execute(
        select(TaskResult)
        .where(TaskResult.run_id == run_id)
        .order_by(TaskResult.position)
    )
    return result.scalars().all()


async def get_runs_for_compare(
    session: AsyncSession, run_ids: list[str]
) -> Sequence[Run]:
    result = await session.execute(
        select(Run)
        .where(Run.id.in_(run_ids))
        .options(selectinload(Run.task_results))
    )
    return result.scalars().all()
