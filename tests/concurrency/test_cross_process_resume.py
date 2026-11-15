"""Two processes must not drive one execution at the same time."""

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import TextIO
from uuid import UUID

import pytest

from id42_work import PostgresStorage, SqliteStorage
from postgres_support import configured_dsn, create_database, drop_database
from resume_claim_worker import leave_running


def _diagnostics(root: Path) -> str:
    parts: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.stat().st_size:
            text = path.read_text(encoding="utf-8", errors="replace")
            parts.append(f"--- {path.name}\n{text}")
    return "\n".join(parts)


def _race(backend: str, target: str, execution_id: UUID, root: Path) -> None:
    gate = root / "gate"
    gate.mkdir(parents=True)
    log = root / "entered.log"
    results = root / "results"
    results.mkdir()
    env = os.environ.copy()
    tests = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = tests + os.pathsep + env.get("PYTHONPATH", "")
    command = "from resume_claim_worker import contend; import sys; contend(*sys.argv[1:])"
    processes: list[subprocess.Popen[str]] = []
    handles: list[TextIO] = []
    try:
        for index in range(2):
            handle = (root / f"proc-{index}.log").open("w", encoding="utf-8")
            handles.append(handle)
            processes.append(
                subprocess.Popen(  # noqa: S603
                    [
                        sys.executable,
                        "-c",
                        command,
                        backend,
                        target,
                        str(execution_id),
                        str(gate),
                        str(log),
                        str(results / f"{index}.txt"),
                    ],
                    env=env,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
            )
        (gate / "go").write_text("1", encoding="utf-8")
        deadline = time.monotonic() + 20
        loser: str | None = None
        while time.monotonic() < deadline:
            done = sorted(results.glob("*.txt"))
            if (gate / "holding").exists() and done:
                loser = done[0].read_text(encoding="utf-8").strip()
                break
            time.sleep(0.02)
        assert loser == "RUNNING", _diagnostics(root)
        (gate / "release").write_text("1", encoding="utf-8")
        for process in processes:
            process.wait(timeout=20)
        statuses = sorted(
            path.read_text(encoding="utf-8").strip() for path in results.glob("*.txt")
        )
        assert statuses == ["COMPLETED", "RUNNING"], _diagnostics(root)
        assert len(log.read_text(encoding="utf-8").splitlines()) == 1
        assert [process.returncode for process in processes] == [0, 0], _diagnostics(root)
    finally:
        (gate / "release").write_text("1", encoding="utf-8")
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        for handle in handles:
            handle.close()


async def test_two_sqlite_processes_cannot_resume_together(tmp_path: Path) -> None:
    database = tmp_path / "work.sqlite"
    storage = SqliteStorage(database)
    try:
        execution_id = await leave_running(storage)
    finally:
        storage.close()
    _race("sqlite", str(database), execution_id, tmp_path / "race")


@pytest.mark.postgres
async def test_two_postgres_processes_cannot_resume_together(tmp_path: Path) -> None:
    admin = configured_dsn()
    if admin is None:
        pytest.skip("ID42_POSTGRES_DSN is not set")
    dsn, name = create_database(admin)
    try:
        storage = PostgresStorage(dsn)
        try:
            execution_id = await leave_running(storage)
        finally:
            storage.close()
        _race("postgres", dsn, execution_id, tmp_path / "race")
    finally:
        drop_database(admin, name)
