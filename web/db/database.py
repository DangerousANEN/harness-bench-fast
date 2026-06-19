"""Database engine and session management."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from web.db.models import Base

_DEFAULT_DB_URL = "sqlite+aiosqlite:///./bench_panel.db"


def get_database_url() -> str:
    url = os.getenv("DATABASE_URL", _DEFAULT_DB_URL)
    # Convert postgres:// to postgresql+asyncpg:// if needed
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


engine = create_async_engine(
    get_database_url(),
    echo=os.getenv("DB_ECHO", "").lower() in ("1", "true"),
    future=True,
)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    """Create all tables and seed with the built-in 298 tasks benchmark if empty."""
    async with engine.begin() as conn:
        from sqlalchemy import inspect, text
        def run_migrations(sync_conn):
            inspector = inspect(sync_conn)
            if "benchmarks" in inspector.get_table_names():
                cols = [c["name"] for c in inspector.get_columns("benchmarks")]
                if "benchmark_type" not in cols:
                    print("Migrating benchmarks table: adding benchmark_type column...")
                    sync_conn.execute(text("ALTER TABLE benchmarks ADD COLUMN benchmark_type VARCHAR(50) DEFAULT 'harness_bench'"))
            if "test_definitions" in inspector.get_table_names():
                cols = [c["name"] for c in inspector.get_columns("test_definitions")]
                if "microbench_task_id" not in cols:
                    print("Migrating test_definitions table: adding microbench_task_id column...")
                    sync_conn.execute(text("ALTER TABLE test_definitions ADD COLUMN microbench_task_id VARCHAR(255)"))
                if "grader_script" not in cols:
                    print("Migrating test_definitions table: adding grader_script column...")
                    sync_conn.execute(text("ALTER TABLE test_definitions ADD COLUMN grader_script TEXT"))

        await conn.run_sync(run_migrations)
        await conn.run_sync(Base.metadata.create_all)

    from web.db import crud
    from web.db.models import TestSource
    from web.api.routers.benchmarks import _task_group_name, _verifier_type_name

    async with async_session() as session:
        bms = await crud.get_benchmarks(session)
        if not bms:
            print("Seeding database with default 298-task built-in benchmark...")
            try:
                from harness_bench.tasks import ALL_TASKS
                
                bm = await crud.create_benchmark(
                    session,
                    name="Built-in Harness Bench",
                    description="298-task agent benchmark (file ops, code edits, pipelines, memory, agentic tasks)"
                )

                task_groups = {}
                for task in ALL_TASKS:
                    group_name = _task_group_name(task)
                    task_groups.setdefault(group_name, []).append(task)

                for group_name, tasks in task_groups.items():
                    group = await crud.create_group(
                        session,
                        benchmark_id=bm.id,
                        name=group_name,
                        description=f"{len(tasks)} built-in tasks",
                    )
                    for task in tasks:
                        tags = list(task.tags) if task.tags else []
                        setup = {k: v for k, v in (task.setup_files or {}).items() if isinstance(v, str)}
                        gold = {k: v for k, v in (task.gold_files or {}).items() if isinstance(v, str)}

                        await crud.create_test(
                            session,
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
                await session.commit()
                print("Successfully seeded 298 built-in tasks!")
            except Exception as e:
                print(f"Failed to seed default benchmark: {e}")
                await session.rollback()


async def close_db() -> None:
    await engine.dispose()


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
