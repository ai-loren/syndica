from pathlib import Path

import pytest

from syndica.kernel.contracts.session import SessionStoreContract
from syndica.kernel.protocols.session import SessionStore
from syndica.kernel.stores.file_session import FileSessionStore


class TestFileSessionStore(SessionStoreContract):
    @pytest.fixture
    def store(self, tmp_path: Path) -> SessionStore:
        return FileSessionStore(tmp_path / '.syndica' / 'sessions')
