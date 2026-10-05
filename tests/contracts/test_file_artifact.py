from pathlib import Path

import pytest

from syndica.kernel.contracts.artifact import ArtifactStoreContract
from syndica.kernel.protocols.artifact import ArtifactStore
from syndica.kernel.stores.file_artifact import FileArtifactStore


class TestFileArtifactStore(ArtifactStoreContract):
    @pytest.fixture
    def store(self, tmp_path: Path) -> ArtifactStore:
        return FileArtifactStore(tmp_path / '.syndica' / 'artifacts')
