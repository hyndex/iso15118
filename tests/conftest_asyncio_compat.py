import asyncio
import pytest


@pytest.fixture
def event_loop():
    """Provide an asyncio event loop fixture for tests expecting it.

    This mirrors the legacy pytest-asyncio 'event_loop' fixture to
    maintain compatibility across plugin versions.
    """
    loop = asyncio.new_event_loop()
    try:
        yield loop
    finally:
        loop.close()

