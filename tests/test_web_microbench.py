from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.pool import StaticPool

from web.main import app
from web.api.deps import get_db
from web.db.models import Base, BenchmarkType, TestSource
from web.db import crud

# Create an in-memory async SQLite engine for testing
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

engine = create_async_engine(
    TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def override_get_db():
    async with TestingSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise

app.dependency_overrides[get_db] = override_get_db


@pytest.fixture(autouse=True)
async def setup_test_db():
    # Create tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    # Drop tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.mark.anyio
async def test_create_microbench_benchmark() -> None:
    client = TestClient(app)
    
    # 1. Create a benchmark with type microbench
    response = client.post(
        "/api/benchmarks",
        json={
            "name": "MicroBench Suite",
            "description": "Tests microbench tasks",
            "benchmark_type": "microbench",
        },
    )
    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "MicroBench Suite"
    assert data["benchmark_type"] == "microbench"
    benchmark_id = data["id"]

    # 2. Import tasks using the import-microbench endpoint
    import_response = client.post(f"/api/benchmarks/{benchmark_id}/import-microbench")
    assert import_response.status_code == 200
    
    import_data = import_response.json()
    assert len(import_data["groups"]) > 0
    
    # Calculate total imported tests
    total_tests = sum(len(group["tests"]) for group in import_data["groups"])
    # There should be 16 tests in microbench_16
    assert total_tests == 16

    # Verify fields of one imported test
    first_group = import_data["groups"][0]
    assert len(first_group["tests"]) > 0
    first_test = first_group["tests"][0]
    assert first_test["microbench_task_id"] is not None
    assert first_test["grader_script"] is not None
    assert first_test["source"] == "builtin"
