"""Benchmarks, Groups, Tests API router."""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from web.api.deps import get_db
from web.db import crud
from web.db.models import TestSource
from web.schemas.benchmarks import (
    BenchmarkCreate,
    BenchmarkDetailOut,
    BenchmarkOut,
    BenchmarkUpdate,
    GroupCreate,
    GroupDetailOut,
    GroupOut,
    GroupUpdate,
    TestCreate,
    TestOut,
    TestUpdate,
)

router = APIRouter(tags=["benchmarks"])


# ---- helpers ----


def _bm_to_out(bm) -> BenchmarkOut:
    groups = bm.groups if hasattr(bm, "groups") and bm.groups else []
    return BenchmarkOut(
        id=bm.id,
        name=bm.name,
        description=bm.description,
        group_count=len(groups),
        total_tests=sum(len(g.tests) for g in groups if hasattr(g, "tests") and g.tests),
        created_at=bm.created_at,
        updated_at=bm.updated_at,
    )


def _bm_to_detail(bm) -> BenchmarkDetailOut:
    groups = []
    for g in (bm.groups or []):
        tests = [TestOut.model_validate(t) for t in (g.tests or [])]
        groups.append(GroupDetailOut(
            id=g.id,
            benchmark_id=g.benchmark_id,
            name=g.name,
            description=g.description,
            position=g.position,
            default_token_budget=g.default_token_budget,
            default_timeout=g.default_timeout,
            test_count=len(tests),
            created_at=g.created_at,
            updated_at=g.updated_at,
            tests=tests,
        ))
    base = _bm_to_out(bm)
    return BenchmarkDetailOut(**base.model_dump(), groups=groups)


def _group_to_out(g) -> GroupOut:
    tests = g.tests if hasattr(g, "tests") and g.tests else []
    return GroupOut(
        id=g.id,
        benchmark_id=g.benchmark_id,
        name=g.name,
        description=g.description,
        position=g.position,
        default_token_budget=g.default_token_budget,
        default_timeout=g.default_timeout,
        test_count=len(tests),
        created_at=g.created_at,
        updated_at=g.updated_at,
    )


# ==== Benchmarks ====


@router.get("/api/benchmarks", response_model=list[BenchmarkOut])
async def list_benchmarks(db: AsyncSession = Depends(get_db)):
    bms = await crud.get_benchmarks(db)
    return [_bm_to_out(b) for b in bms]


@router.get("/api/benchmarks/{benchmark_id}", response_model=BenchmarkDetailOut)
async def get_benchmark(benchmark_id: str, db: AsyncSession = Depends(get_db)):
    bm = await crud.get_benchmark(db, benchmark_id)
    if bm is None:
        raise HTTPException(404, "Benchmark not found")
    return _bm_to_detail(bm)


@router.post("/api/benchmarks", response_model=BenchmarkOut, status_code=201)
async def create_benchmark(body: BenchmarkCreate, db: AsyncSession = Depends(get_db)):
    bm = await crud.create_benchmark(db, name=body.name, description=body.description)
    return _bm_to_out(bm)


@router.put("/api/benchmarks/{benchmark_id}", response_model=BenchmarkOut)
async def update_benchmark(
    benchmark_id: str, body: BenchmarkUpdate, db: AsyncSession = Depends(get_db)
):
    bm = await crud.update_benchmark(db, benchmark_id, name=body.name, description=body.description)
    if bm is None:
        raise HTTPException(404, "Benchmark not found")
    return _bm_to_out(bm)


@router.delete("/api/benchmarks/{benchmark_id}", status_code=204)
async def delete_benchmark(benchmark_id: str, db: AsyncSession = Depends(get_db)):
    deleted = await crud.delete_benchmark(db, benchmark_id)
    if not deleted:
        raise HTTPException(404, "Benchmark not found")


@router.post("/api/benchmarks/{benchmark_id}/import-builtin", response_model=BenchmarkDetailOut)
async def import_builtin_tasks(benchmark_id: str, db: AsyncSession = Depends(get_db)):
    """Import all 298 built-in tasks from harness_bench into this benchmark."""
    bm = await crud.get_benchmark(db, benchmark_id)
    if bm is None:
        raise HTTPException(404, "Benchmark not found")

    try:
        from harness_bench.tasks import ALL_TASKS
    except ImportError:
        raise HTTPException(500, "harness_bench package not installed")

    # Group tasks by their module source
    task_groups: dict[str, list] = {}
    for task in ALL_TASKS:
        # Determine group from task tags or module
        group_name = _task_group_name(task)
        task_groups.setdefault(group_name, []).append(task)

    for group_name, tasks in task_groups.items():
        group = await crud.create_group(
            db,
            benchmark_id=benchmark_id,
            name=group_name,
            description=f"{len(tasks)} built-in tasks",
        )
        for i, task in enumerate(tasks):
            tags = list(task.tags) if task.tags else []
            # Serialize setup/gold files (text only)
            setup = {}
            if task.setup_files:
                setup = {k: v for k, v in task.setup_files.items() if isinstance(v, str)}
            gold = {}
            if task.gold_files:
                gold = {k: v for k, v in task.gold_files.items() if isinstance(v, str)}

            await crud.create_test(
                db,
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

    # Refresh
    bm = await crud.get_benchmark(db, benchmark_id)
    return _bm_to_detail(bm)


def _task_group_name(task) -> str:
    """Derive a group name from built-in task metadata."""
    tags = set(task.tags) if task.tags else set()
    task_id = task.id or ""

    if "memory" in tags or "memory" in task_id:
        return "Memory Tasks"
    if "agentic" in tags or "agentic" in task_id:
        return "Agentic Tasks"
    if "vcs" in tags or "git" in tags:
        return "VCS Tasks"
    if "extreme" in tags or any(x in task_id for x in ("extreme", "xlsx", "sqlite")):
        return "Extreme Tasks"
    if "hard" in tags or "pipeline" in tags:
        return "Hard Tasks"
    if "diagnostic" in tags:
        return "Diagnostic Tasks"
    if any(t in tags for t in ("create", "edit", "json", "text", "python", "csv")):
        return "Core Tasks"
    return "Other Tasks"


def _verifier_type_name(task) -> str:
    """Best-effort name for the verifier."""
    v = task.verifier
    name = getattr(v, "__name__", "") or type(v).__name__
    return name or "builtin"


# ==== Groups ====


@router.get("/api/benchmarks/{benchmark_id}/groups", response_model=list[GroupOut])
async def list_groups(benchmark_id: str, db: AsyncSession = Depends(get_db)):
    groups = await crud.get_groups(db, benchmark_id)
    return [_group_to_out(g) for g in groups]


@router.post("/api/benchmarks/{benchmark_id}/groups", response_model=GroupOut, status_code=201)
async def create_group(
    benchmark_id: str, body: GroupCreate, db: AsyncSession = Depends(get_db)
):
    bm = await crud.get_benchmark(db, benchmark_id)
    if bm is None:
        raise HTTPException(404, "Benchmark not found")
    group = await crud.create_group(
        db,
        benchmark_id=benchmark_id,
        name=body.name,
        description=body.description,
        default_token_budget=body.default_token_budget,
        default_timeout=body.default_timeout,
    )
    return _group_to_out(group)


@router.put("/api/groups/{group_id}", response_model=GroupOut)
async def update_group(
    group_id: str, body: GroupUpdate, db: AsyncSession = Depends(get_db)
):
    group = await crud.update_group(
        db, group_id, **body.model_dump(exclude_none=True)
    )
    if group is None:
        raise HTTPException(404, "Group not found")
    return _group_to_out(group)


@router.delete("/api/groups/{group_id}", status_code=204)
async def delete_group(group_id: str, db: AsyncSession = Depends(get_db)):
    deleted = await crud.delete_group(db, group_id)
    if not deleted:
        raise HTTPException(404, "Group not found")


# ==== Tests ====


@router.get("/api/groups/{group_id}/tests", response_model=list[TestOut])
async def list_tests(group_id: str, db: AsyncSession = Depends(get_db)):
    tests = await crud.get_tests(db, group_id)
    return [TestOut.model_validate(t) for t in tests]


@router.post("/api/groups/{group_id}/tests", response_model=TestOut, status_code=201)
async def create_test(group_id: str, body: TestCreate, db: AsyncSession = Depends(get_db)):
    test = await crud.create_test(
        db,
        group_id=group_id,
        name=body.name,
        prompt=body.prompt,
        tags=body.tags,
        setup_files=body.setup_files,
        gold_files=body.gold_files,
        verifier_type=body.verifier_type,
        verifier_config=body.verifier_config,
        token_budget=body.token_budget,
        timeout_seconds=body.timeout_seconds,
    )
    return TestOut.model_validate(test)


@router.put("/api/tests/{test_id}", response_model=TestOut)
async def update_test(
    test_id: str, body: TestUpdate, db: AsyncSession = Depends(get_db)
):
    test = await crud.update_test(db, test_id, **body.model_dump(exclude_none=True))
    if test is None:
        raise HTTPException(404, "Test not found")
    return TestOut.model_validate(test)


@router.delete("/api/tests/{test_id}", status_code=204)
async def delete_test(test_id: str, db: AsyncSession = Depends(get_db)):
    deleted = await crud.delete_test(db, test_id)
    if not deleted:
        raise HTTPException(404, "Test not found")
