"""Pydantic schemas for Benchmarks, Test Groups, and Tests API."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


# ---- TestDefinition ----


class TestCreate(BaseModel):
    name: str
    prompt: str
    tags: list[str] = Field(default_factory=list)
    setup_files: dict[str, str] = Field(default_factory=dict)
    gold_files: dict[str, str] = Field(default_factory=dict)
    verifier_type: str = ""
    verifier_config: dict = Field(default_factory=dict)
    token_budget: int = -1
    timeout_seconds: int = 600


class TestUpdate(BaseModel):
    name: str | None = None
    prompt: str | None = None
    tags: list[str] | None = None
    setup_files: dict[str, str] | None = None
    gold_files: dict[str, str] | None = None
    verifier_type: str | None = None
    verifier_config: dict | None = None
    token_budget: int | None = None
    timeout_seconds: int | None = None


class TestOut(BaseModel):
    id: str
    group_id: str
    name: str
    prompt: str
    tags: list[str] = Field(default_factory=list)
    setup_files: dict = Field(default_factory=dict)
    gold_files: dict = Field(default_factory=dict)
    verifier_type: str = ""
    verifier_config: dict = Field(default_factory=dict)
    token_budget: int = -1
    timeout_seconds: int = 600
    source: str = "custom"
    builtin_task_id: str | None = None
    position: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


# ---- TestGroup ----


class GroupCreate(BaseModel):
    name: str
    description: str = ""
    default_token_budget: int = -1
    default_timeout: int = 600


class GroupUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    default_token_budget: int | None = None
    default_timeout: int | None = None


class GroupOut(BaseModel):
    id: str
    benchmark_id: str
    name: str
    description: str = ""
    position: int = 0
    default_token_budget: int = -1
    default_timeout: int = 600
    test_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class GroupDetailOut(GroupOut):
    tests: list[TestOut] = Field(default_factory=list)


# ---- Benchmark ----


class BenchmarkCreate(BaseModel):
    name: str
    description: str = ""


class BenchmarkUpdate(BaseModel):
    name: str | None = None
    description: str | None = None


class BenchmarkOut(BaseModel):
    id: str
    name: str
    description: str = ""
    group_count: int = 0
    total_tests: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class BenchmarkDetailOut(BenchmarkOut):
    groups: list[GroupDetailOut] = Field(default_factory=list)
