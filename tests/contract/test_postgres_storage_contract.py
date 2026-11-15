"""``PostgresStorage`` passes the published storage contract."""

import pytest

from id42_work.storage.postgres import PostgresStorage
from id42_work.storage.protocols import Storage
from id42_work.testing.contract import StorageContractSuite
from postgres_support import configured_dsn, create_database, drop_database

pytestmark = pytest.mark.postgres


class TestPostgresStorage(StorageContractSuite):
    def setup_method(self) -> None:
        admin = configured_dsn()
        if admin is None:
            pytest.skip("ID42_POSTGRES_DSN is not set")
        self._admin = admin
        self._names: list[str] = []
        self._open: list[PostgresStorage] = []

    def teardown_method(self) -> None:
        for storage in self._open:
            storage.close()
        for name in self._names:
            drop_database(self._admin, name)

    def make_storage(self) -> Storage:
        dsn, name = create_database(self._admin)
        self._names.append(name)
        storage = PostgresStorage(dsn)
        self._open.append(storage)
        return storage
