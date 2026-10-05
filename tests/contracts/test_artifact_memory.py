import pytest

from syndica.kernel.contracts.artifact import ArtifactStoreContract
from syndica.kernel.protocols.artifact import ArtifactStore
from syndica.kernel.stores.memory_artifact import InMemoryArtifactStore


class TestInMemoryArtifactStore(ArtifactStoreContract):
    @pytest.fixture
    def store(self) -> ArtifactStore:
        return InMemoryArtifactStore()
