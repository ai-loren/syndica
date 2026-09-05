import pytest

from syndica.kernel.contracts.broker import MessageBrokerContract
from syndica.kernel.protocols.broker import MessageBroker
from syndica.kernel.stores.memory_broker import InMemoryMessageBroker


class TestInMemoryMessageBroker(MessageBrokerContract):
    @pytest.fixture
    def broker(self) -> MessageBroker:
        return InMemoryMessageBroker()
