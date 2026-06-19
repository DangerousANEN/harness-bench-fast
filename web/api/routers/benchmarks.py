"""Benchmarks, Groups, Tests API router."""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from web.api.deps import get_db
from web.db import crud
from web.db.models import BenchmarkType, TestSource
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
    from sqlalchemy import inspect
    insp = inspect(bm)
    groups = []
    if insp and "groups" not in insp.unloaded:
        groups = bm.groups

    total_tests = 0
    for g in groups:
        g_insp = inspect(g)
        if g_insp and "tests" not in g_insp.unloaded:
            total_tests += len(g.tests)

    return BenchmarkOut(
        id=bm.id,
        name=bm.name,
        description=bm.description,
        benchmark_type=bm.benchmark_type.value if hasattr(bm.benchmark_type, 'value') else str(bm.benchmark_type),
        group_count=len(groups),
        total_tests=total_tests,
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
    bm_type = BenchmarkType(body.benchmark_type) if body.benchmark_type else BenchmarkType.HARNESS_BENCH
    bm = await crud.create_benchmark(
        db, name=body.name, description=body.description,
        benchmark_type=bm_type,
    )
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
    db.expire_all()
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


# ==== MicroBench Import ====


# Mapping of microbench task slugs to language/category groups
_MICROBENCH_GROUPS = {
    "Python": [
        "py_rate_limiter_sliding_window",
        "py_shortest_path_with_path_reconstruction",
        "py_strict_chat_prompt_normalizer",
    ],
    "PyTorch / ML": [
        "pt_kv_cache_generate",
        "pt_logit_lens_layers",
        "pt_prompt_blend_prob_space",
        "pt_rope_gqa_layouts",
    ],
    "JAX / ML": [
        "jax_complex_lp_filtered_mrr",
    ],
    "C / C++": [
        "c_ring_buffer_overwrite_semantics",
        "cpp_csv_groupby_quoted_fields",
    ],
    "Rust": [
        "rs_arena_graph_storage",
    ],
    "SQL": [
        "sql_recursive_org_chart",
        "sql_retention_rolling_7d",
        "sql_sessionize_gap_30m",
    ],
    "Service / Repo": [
        "svc_boot_healthcheck",
        "repo_root_cause_trace_qna",
    ],
    "TypeScript": [
        "ts_topological_sort",
    ],
}


@router.post("/api/benchmarks/{benchmark_id}/import-microbench", response_model=BenchmarkDetailOut)
async def import_microbench_tasks(benchmark_id: str, db: AsyncSession = Depends(get_db)):
    """Import all 16 built-in tasks from microbench_16 into this benchmark."""
    bm = await crud.get_benchmark(db, benchmark_id)
    if bm is None:
        raise HTTPException(404, "Benchmark not found")

    try:
        from microbench12.tasks import list_task_ids, load_task, load_prompt
    except ImportError:
        raise HTTPException(500, "microbench12 package not installed. Install with: pip install -e /path/to/microbench_16")

    all_tasks = [load_task(tid) for tid in list_task_ids()]

    # Build slug → task map
    task_map = {t.id: t for t in all_tasks}

    for group_name, slugs in _MICROBENCH_GROUPS.items():
        matching = [task_map[s] for s in slugs if s in task_map]
        if not matching:
            continue

        group = await crud.create_group(
            db,
            benchmark_id=benchmark_id,
            name=group_name,
            description=f"{len(matching)} microbench tasks",
        )

        for task in matching:
            prompt_text = load_prompt(task.id)
            tags = getattr(task, "tags", []) or []
            if isinstance(tags, set):
                tags = list(tags)

            # Collect starter files as setup_files
            setup = {}
            starter_files = getattr(task, "starter_files", None)
            if starter_files and isinstance(starter_files, dict):
                setup = {k: v for k, v in starter_files.items() if isinstance(v, str)}

            await crud.create_test(
                db,
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

    # Also import any tasks not covered by the static groups
    covered = set()
    for slugs in _MICROBENCH_GROUPS.values():
        covered.update(slugs)
    uncovered = [t for t in all_tasks if t.id not in covered]
    if uncovered:
        group = await crud.create_group(
            db,
            benchmark_id=benchmark_id,
            name="Other",
            description=f"{len(uncovered)} microbench tasks",
        )
        for task in uncovered:
            prompt_text = load_prompt(task.id)
            await crud.create_test(
                db,
                group_id=group.id,
                name=task.id,
                prompt=prompt_text,
                verifier_type="microbench_grader",
                source=TestSource.BUILTIN,
                microbench_task_id=task.id,
                grader_script=f"tasks/{task.id}/grader/grade.py",
            )

    # Refresh
    db.expire_all()
    bm = await crud.get_benchmark(db, benchmark_id)
    return _bm_to_detail(bm)


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
