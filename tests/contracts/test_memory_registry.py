import pytest

from syndica.kernel import contracts
from syndica.kernel.contracts.registry import AgentRegistryContract
from syndica.kernel.protocols import registry as registry_protocol
from syndica.kernel.protocols.registry import AgentRegistry
from syndica.kernel.stores.memory_registry import InMemoryAgentRegistry


class TestInMemoryAgentRegistry(AgentRegistryContract):
    @pytest.fixture
    def registry(self) -> AgentRegistry:
        return InMemoryAgentRegistry()


def test_contracts_reexports_the_three_update_sets() -> None:
    assert contracts.UPDATE_WRITABLE_FIELDS is registry_protocol.UPDATE_WRITABLE_FIELDS
    assert contracts.UPDATE_PROTECTED_FIELDS is registry_protocol.UPDATE_PROTECTED_FIELDS
    assert (
        contracts.UPDATE_PROTECTED_FIELD_OWNERS is registry_protocol.UPDATE_PROTECTED_FIELD_OWNERS
    )
