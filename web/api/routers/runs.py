"""Runs API router — list, create, delete, cancel runs."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession

from web.api.deps import get_db
from web.api.ws import ws_manager
from web.db import crud
from web.db.models import RunStatus, TaskStatus
from web.engine.orchestrator import orchestrator
from web.schemas.runs import (
    CompareRequest,
    CompareSummary,
    CompareTaskRow,
    RunCreate,
    RunDetailOut,
    RunOut,
    TaskResultOut,
    TaskOverrideRequest,
)

router = APIRouter(prefix="/api/runs", tags=["runs"])


def _run_to_out(run) -> RunOut:
    return RunOut.model_validate(run)


def _run_to_detail(run) -> RunDetailOut:
    results = [TaskResultOut.model_validate(tr) for tr in (run.task_results or [])]
    d = RunOut.model_validate(run).model_dump()
    d["task_results"] = results
    d["benchmark_name"] = run.benchmark.name if run.benchmark else None
    return RunDetailOut(**d)


@router.get("", response_model=list[RunOut])
async def list_runs(
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    runs = await crud.get_runs(db, limit=limit, offset=offset)
    return [_run_to_out(r) for r in runs]


@router.get("/{run_id}", response_model=RunDetailOut)
async def get_run(run_id: str, db: AsyncSession = Depends(get_db)):
    run = await crud.get_run(db, run_id)
    if run is None:
        raise HTTPException(404, "Run not found")
    return _run_to_detail(run)


@router.post("", response_model=RunOut, status_code=201)
async def create_run(body: RunCreate, db: AsyncSession = Depends(get_db)):
    # Verify benchmark exists
    bm = await crud.get_benchmark(db, body.benchmark_id)
    if bm is None:
        raise HTTPException(404, "Benchmark not found")

    # Count total tasks
    total = sum(len(g.tests) for g in bm.groups)
    if total == 0:
        raise HTTPException(400, "Benchmark has no tests")

    name = body.name or f"Run — {body.model or body.harness_type} — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}"

    run = await crud.create_run(
        db,
        name=name,
        benchmark_id=body.benchmark_id,
        harness_type=body.harness_type,
        model=body.model,
        base_url=body.base_url,
        cli_command=body.cli_command,
        env_vars=body.env_vars,
        concurrency=body.concurrency,
        recursion_limit=body.recursion_limit,
        timeout_seconds=body.timeout_seconds,
        global_token_budget=body.global_token_budget,
    )
    # Set total tasks
    run.total_tasks = total
    await db.flush()
    await db.commit()

    # Start run in background
    orchestrator.start_run(run.id)

    return _run_to_out(run)


@router.delete("/{run_id}", status_code=204)
async def delete_run(run_id: str, db: AsyncSession = Depends(get_db)):
    # Cancel if running
    orchestrator.cancel_run(run_id)
    deleted = await crud.delete_run(db, run_id)
    if not deleted:
        raise HTTPException(404, "Run not found")


@router.post("/{run_id}/cancel", status_code=200)
async def cancel_run(run_id: str, db: AsyncSession = Depends(get_db)):
    run = await crud.get_run(db, run_id)
    if run is None:
        raise HTTPException(404, "Run not found")
    if run.status not in (RunStatus.PENDING, RunStatus.RUNNING):
        raise HTTPException(400, f"Cannot cancel run in status {run.status}")
    orchestrator.cancel_run(run_id)
    await crud.update_run_status(
        db, run_id, RunStatus.CANCELLED,
        finished_at=datetime.now(timezone.utc),
    )
    return {"status": "cancelled"}


# ---- Compare ----


@router.post("/compare", response_model=CompareSummary)
async def compare_runs(body: CompareRequest, db: AsyncSession = Depends(get_db)):
    if len(body.run_ids) < 2:
        raise HTTPException(400, "Need at least 2 runs to compare")

    runs = await crud.get_runs_for_compare(db, body.run_ids)
    if len(runs) < 2:
        raise HTTPException(404, "Some runs not found")

    # Build per-task comparison
    all_task_keys: dict[str, str] = {}  # task_name → builtin_task_id
    run_task_map: dict[str, dict[str, TaskResultOut]] = {}

    for run in runs:
        run_task_map[run.id] = {}
        for tr in run.task_results:
            key = tr.builtin_task_id or tr.task_name
            all_task_keys[key] = tr.builtin_task_id
            run_task_map[run.id][key] = TaskResultOut.model_validate(tr)

    per_task = []
    for key in sorted(all_task_keys.keys()):
        results = {}
        for run in runs:
            if key in run_task_map[run.id]:
                results[run.id] = run_task_map[run.id][key]
        per_task.append(CompareTaskRow(
            task_name=key,
            builtin_task_id=all_task_keys[key],
            results=results,
        ))

    return CompareSummary(
        runs=[_run_to_out(r) for r in runs],
        per_task=per_task,
    )


# ---- WebSocket ----


@router.get("/compare")
async def compare_runs_get(
    run_id_1: str,
    run_id_2: str,
    db: AsyncSession = Depends(get_db),
):
    runs = await crud.get_runs_for_compare(db, [run_id_1, run_id_2])
    if len(runs) < 2:
        raise HTTPException(404, "One or both runs not found")

    run_1, run_2 = (runs[0], runs[1]) if runs[0].id == run_id_1 else (runs[1], runs[0])

    tasks_1 = {tr.task_name: tr for tr in run_1.task_results}
    tasks_2 = {tr.task_name: tr for tr in run_2.task_results}

    all_names = sorted(list(set(tasks_1.keys()) | set(tasks_2.keys())))

    tasks_out = []
    for name in all_names:
        r1 = tasks_1.get(name)
        r2 = tasks_2.get(name)
        tasks_out.append({
            "task_name": name,
            "status_1": r1.status.value if r1 else None,
            "status_2": r2.status.value if r2 else None,
            "tokens_1": r1.agent_total_tokens if r1 else None,
            "tokens_2": r2.agent_total_tokens if r2 else None,
        })

    return {
        "run_1": _run_to_out(run_1),
        "run_2": _run_to_out(run_2),
        "tasks": tasks_out
    }


@router.post("/tasks/{task_result_id}/override-status")
async def override_task_status(
    task_result_id: str,
    body: TaskOverrideRequest,
    db: AsyncSession = Depends(get_db),
):
    try:
        new_status = TaskStatus(body.status.lower())
    except ValueError:
        raise HTTPException(400, f"Invalid status: {body.status}. Must be one of {[s.value for s in TaskStatus]}")

    updated_tr = await crud.override_task_result_status(db, task_result_id, new_status)
    if updated_tr is None:
        raise HTTPException(404, "Task result not found")

    await db.commit()

    run = await crud.get_run(db, updated_tr.run_id)
    if run:
        await ws_manager.broadcast(run.id, {
            "type": "task_completed",
            "task_result": {
                "task_name": updated_tr.task_name,
                "status": updated_tr.status.value,
                "tokens": updated_tr.agent_total_tokens,
                "elapsed": updated_tr.elapsed_seconds,
            },
            "run_stats": {
                "completed_tasks": run.completed_tasks,
                "passed_tasks": run.passed_tasks,
                "failed_tasks": run.failed_tasks,
                "total_tokens": run.total_tokens,
                "tokens_per_second": run.avg_tokens_per_second,
            }
        })

    return {"status": "ok", "task_status": updated_tr.status}


@router.websocket("/ws/{run_id}")
async def ws_run_progress(websocket: WebSocket, run_id: str):
    await ws_manager.connect(run_id, websocket)
    try:
        # Send initial snapshot
        async with get_db() as db_gen:
            pass
        # Keep connection alive, listen for client messages
        while True:
            try:
                await websocket.receive_text()
            except WebSocketDisconnect:
                break
    finally:
        await ws_manager.disconnect(run_id, websocket)
