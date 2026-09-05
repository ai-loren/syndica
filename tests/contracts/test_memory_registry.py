import pytest

from syndica.kernel.contracts.registry import AgentRegistryContract
from syndica.kernel.protocols.registry import AgentRegistry
from syndica.kernel.stores.memory_registry import InMemoryAgentRegistry


class TestInMemoryAgentRegistry(AgentRegistryContract):
    @pytest.fixture
    def registry(self) -> AgentRegistry:
        return InMemoryAgentRegistry()
