import pytest

from syndica.kernel.contracts.rubric import RubricStoreContract
from syndica.kernel.protocols.rubric import RubricStore
from syndica.kernel.stores.memory_rubric import InMemoryRubricStore


class TestInMemoryRubricStore(RubricStoreContract):
    @pytest.fixture
    def store(self) -> RubricStore:
        return InMemoryRubricStore()
