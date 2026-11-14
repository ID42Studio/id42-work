"""``SqliteStorage`` passes the published storage contract."""

import tempfile
from pathlib import Path

from id42_work.storage import SqliteStorage, Storage
from id42_work.testing.contract import StorageContractSuite


class TestSqliteStorage(StorageContractSuite):
    def setup_method(self) -> None:
        self._dirs: list[tempfile.TemporaryDirectory[str]] = []
        self._open: list[SqliteStorage] = []

    def teardown_method(self) -> None:
        for storage in self._open:
            storage.close()
        for directory in self._dirs:
            directory.cleanup()

    def make_storage(self) -> Storage:
        directory = tempfile.TemporaryDirectory(prefix="id42-sqlite-")
        self._dirs.append(directory)
        storage = SqliteStorage(Path(directory.name) / "work.sqlite")
        self._open.append(storage)
        return storage
