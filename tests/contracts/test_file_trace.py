from __future__ import annotations

from pathlib import Path

import pytest

from syndica.kernel.contracts.trace import TraceStoreContract
from syndica.kernel.protocols.trace import TraceStore
from syndica.kernel.stores.file_trace import FileTraceStore


class TestFileTraceStore(TraceStoreContract):
    @pytest.fixture
    def store(self, tmp_path: Path) -> TraceStore:
        return FileTraceStore(tmp_path / 'traces')


def test_the_file_store_is_a_trace_store(tmp_path: Path):
    assert isinstance(FileTraceStore(tmp_path), TraceStore)
