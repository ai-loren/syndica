from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from syndica.kernel.contracts.workspace import WorkspaceContract
from syndica.kernel.local_workspace import LocalWorkspace
from syndica.kernel.workspace import Workspace
from syndica.kernel.workspace_router import WorkspaceRouter


class TestWorkspaceRouter(WorkspaceContract):
    @pytest.fixture
    async def workspace(self, tmp_path: Path) -> AsyncIterator[Workspace]:
        router = WorkspaceRouter(LocalWorkspace(str(tmp_path), shell='/bin/sh', env={}))
        yield router
        await router.close()
