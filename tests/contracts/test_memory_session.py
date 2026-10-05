import pytest

from syndica.kernel.contracts.session import SessionStoreContract
from syndica.kernel.protocols.session import SessionStore
from syndica.kernel.stores.memory_session import InMemorySessionStore


class TestInMemorySessionStore(SessionStoreContract):
    @pytest.fixture
    def store(self) -> SessionStore:
        return InMemorySessionStore()
