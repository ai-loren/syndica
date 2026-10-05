from pathlib import Path

import pytest

from syndica.kernel.contracts.rubric import RubricStoreContract
from syndica.kernel.protocols.rubric import RubricStore
from syndica.kernel.stores.file_rubric import FileRubricStore


class TestFileRubricStore(RubricStoreContract):
    @pytest.fixture
    def store(self, tmp_path: Path) -> RubricStore:
        return FileRubricStore(tmp_path / '.syndica' / 'rubrics')
