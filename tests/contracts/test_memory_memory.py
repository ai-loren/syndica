import pytest

from syndica.kernel.contracts.memory import MemoryStoreContract
from syndica.kernel.protocols.memory import MemoryStore
from syndica.kernel.stores.memory_memory import InMemoryMemoryStore


class TestInMemoryMemoryStore(MemoryStoreContract):
    @pytest.fixture
    def store(self) -> MemoryStore:
        return InMemoryMemoryStore()
