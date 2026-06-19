"""Database models for the benchmark web panel.

Uses SQLAlchemy 2.0 ORM with async support. Default backend is SQLite
(zero-config, single file), but PostgreSQL can be used via DATABASE_URL.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class RunStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class BenchmarkType(str, enum.Enum):
    HARNESS_BENCH = "harness_bench"
    MICROBENCH = "microbench"


class HarnessType(str, enum.Enum):
    DEEPAGENTS = "deepagents"
    OPENROUTER = "openrouter"
    CLI = "cli"
    PURE = "pure"
    MICROBENCH_CLI = "microbench_cli"


class TaskStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    TIMEOUT = "timeout"
    LOOP = "loop"
    TOKEN_LIMIT = "token_limit"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    CANCELLED = "cancelled"


class FailureReason(str, enum.Enum):
    VERIFIER_FAILED = "verifier_failed"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    TIMEOUT = "timeout"
    RECURSION_LOOP = "recursion_loop"
    TOKEN_LIMIT_EXCEEDED = "token_limit_exceeded"
    RUNTIME_ERROR = "runtime_error"
    CANCELLED = "cancelled"


class TestSource(str, enum.Enum):
    BUILTIN = "builtin"
    CUSTOM = "custom"


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Benchmark — a named collection of test groups
# ---------------------------------------------------------------------------


class Benchmark(Base):
    __tablename__ = "benchmarks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    benchmark_type: Mapped[BenchmarkType] = mapped_column(
        Enum(BenchmarkType), default=BenchmarkType.HARNESS_BENCH
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    groups: Mapped[list[TestGroup]] = relationship(
        back_populates="benchmark", cascade="all, delete-orphan", order_by="TestGroup.position"
    )
    runs: Mapped[list[Run]] = relationship(back_populates="benchmark")


# ---------------------------------------------------------------------------
# TestGroup — a logical group of tests inside a benchmark
# ---------------------------------------------------------------------------


class TestGroup(Base):
    __tablename__ = "test_groups"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    benchmark_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("benchmarks.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    position: Mapped[int] = mapped_column(Integer, default=0)
    default_token_budget: Mapped[int] = mapped_column(Integer, default=-1)
    default_timeout: Mapped[int] = mapped_column(Integer, default=600)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    benchmark: Mapped[Benchmark] = relationship(back_populates="groups")
    tests: Mapped[list[TestDefinition]] = relationship(
        back_populates="group", cascade="all, delete-orphan", order_by="TestDefinition.position"
    )


# ---------------------------------------------------------------------------
# TestDefinition — a single test/task
# ---------------------------------------------------------------------------


class TestDefinition(Base):
    __tablename__ = "test_definitions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    group_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("test_groups.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[dict | list] = mapped_column(JSON, default=list)
    setup_files: Mapped[dict] = mapped_column(JSON, default=dict)
    gold_files: Mapped[dict] = mapped_column(JSON, default=dict)
    verifier_type: Mapped[str] = mapped_column(String(100), default="")
    verifier_config: Mapped[dict] = mapped_column(JSON, default=dict)
    token_budget: Mapped[int] = mapped_column(Integer, default=-1)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=600)
    source: Mapped[TestSource] = mapped_column(
        Enum(TestSource), default=TestSource.CUSTOM
    )
    builtin_task_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    microbench_task_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    grader_script: Mapped[str | None] = mapped_column(Text, nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    group: Mapped[TestGroup] = relationship(back_populates="tests")
    task_results: Mapped[list[TaskResult]] = relationship(back_populates="test")


# ---------------------------------------------------------------------------
# Run — a single benchmark execution
# ---------------------------------------------------------------------------


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[RunStatus] = mapped_column(
        Enum(RunStatus), default=RunStatus.PENDING
    )
    benchmark_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("benchmarks.id", ondelete="SET NULL"), nullable=True
    )

    # Harness configuration
    harness_type: Mapped[HarnessType] = mapped_column(Enum(HarnessType), nullable=False)
    model: Mapped[str] = mapped_column(String(255), default="")
    base_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    cli_command: Mapped[str | None] = mapped_column(Text, nullable=True)
    env_vars: Mapped[dict] = mapped_column(JSON, default=dict)
    concurrency: Mapped[int] = mapped_column(Integer, default=5)
    recursion_limit: Mapped[int] = mapped_column(Integer, default=100)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=600)
    global_token_budget: Mapped[int] = mapped_column(Integer, default=-1)

    # Aggregated results
    total_tasks: Mapped[int] = mapped_column(Integer, default=0)
    completed_tasks: Mapped[int] = mapped_column(Integer, default=0)
    passed_tasks: Mapped[int] = mapped_column(Integer, default=0)
    failed_tasks: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    avg_tokens_per_second: Mapped[float] = mapped_column(Float, default=0.0)

    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    benchmark: Mapped[Benchmark | None] = relationship(back_populates="runs")
    task_results: Mapped[list[TaskResult]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="TaskResult.position"
    )


# ---------------------------------------------------------------------------
# TaskResult — outcome of one test in one run
# ---------------------------------------------------------------------------


class TaskResult(Base):
    __tablename__ = "task_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    test_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("test_definitions.id", ondelete="SET NULL"), nullable=True
    )
    builtin_task_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    task_name: Mapped[str] = mapped_column(String(255), default="")
    position: Mapped[int] = mapped_column(Integer, default=0)

    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus), default=TaskStatus.PENDING
    )
    message: Mapped[str] = mapped_column(Text, default="")
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    elapsed_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    agent_steps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    agent_tool_calls: Mapped[int | None] = mapped_column(Integer, nullable=True)
    agent_shell_commands: Mapped[int | None] = mapped_column(Integer, nullable=True)
    agent_llm_calls: Mapped[int | None] = mapped_column(Integer, nullable=True)
    agent_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    agent_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    agent_total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_per_second: Mapped[float | None] = mapped_column(Float, nullable=True)

    failure_reason: Mapped[FailureReason | None] = mapped_column(
        Enum(FailureReason), nullable=True
    )

    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    run: Mapped[Run] = relationship(back_populates="task_results")
    test: Mapped[TestDefinition | None] = relationship(back_populates="task_results")
