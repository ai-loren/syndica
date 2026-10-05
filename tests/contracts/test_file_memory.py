from pathlib import Path

import pytest

from syndica.kernel.contracts.memory import MemoryStoreContract
from syndica.kernel.protocols.memory import MemoryStore
from syndica.kernel.stores.file_memory import FileMemoryStore


class TestFileMemoryStore(MemoryStoreContract):
    @pytest.fixture
    def store(self, tmp_path: Path) -> MemoryStore:
        return FileMemoryStore(tmp_path / '.syndica' / 'memory')
