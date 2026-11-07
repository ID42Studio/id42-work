"""``MemoryStorage`` passes the published storage contract."""

from id42_work import MemoryStorage
from id42_work.storage import Storage
from id42_work.testing.contract import StorageContractSuite


class TestMemoryStorage(StorageContractSuite):
    def make_storage(self) -> Storage:
        return MemoryStorage()
