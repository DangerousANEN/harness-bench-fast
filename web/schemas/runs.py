"""Pydantic schemas for Runs API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ---- TaskResult ----


class TaskResultOut(BaseModel):
    id: str
    run_id: str
    test_id: str | None = None
    builtin_task_id: str | None = None
    task_name: str = ""
    position: int = 0
    status: str = "pending"
    message: str = ""
    error_detail: str | None = None
    elapsed_seconds: float = 0.0
    agent_steps: int | None = None
    agent_tool_calls: int | None = None
    agent_shell_commands: int | None = None
    agent_llm_calls: int | None = None
    agent_input_tokens: int | None = None
    agent_output_tokens: int | None = None
    agent_total_tokens: int | None = None
    tokens_per_second: float | None = None
    failure_reason: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    model_config = {"from_attributes": True}


# ---- Run ----


class RunCreate(BaseModel):
    name: str = ""
    benchmark_id: str
    harness_type: str  # deepagents | openrouter | cli | pure
    model: str = ""
    base_url: str | None = None
    cli_command: str | None = None
    env_vars: dict[str, str] = Field(default_factory=dict)
    concurrency: int = 5
    recursion_limit: int = 100
    timeout_seconds: int = 600
    global_token_budget: int = -1


class RunOut(BaseModel):
    id: str
    name: str
    status: str
    benchmark_id: str | None = None
    harness_type: str
    model: str = ""
    base_url: str | None = None
    cli_command: str | None = None
    concurrency: int = 5
    recursion_limit: int = 100
    timeout_seconds: int = 600
    global_token_budget: int = -1
    total_tasks: int = 0
    completed_tasks: int = 0
    passed_tasks: int = 0
    failed_tasks: int = 0
    total_tokens: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    avg_tokens_per_second: float = 0.0
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


class RunDetailOut(RunOut):
    task_results: list[TaskResultOut] = Field(default_factory=list)
    benchmark_name: str | None = None


# ---- Compare ----


class CompareRequest(BaseModel):
    run_ids: list[str]


class CompareTaskRow(BaseModel):
    task_name: str
    builtin_task_id: str | None = None
    results: dict[str, TaskResultOut]  # run_id → result


class CompareSummary(BaseModel):
    runs: list[RunOut]
    per_task: list[CompareTaskRow]


# ---- Override ----


class TaskOverrideRequest(BaseModel):
    status: str
