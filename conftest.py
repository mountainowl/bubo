"""Keep product analytics isolated across all pytest suites."""

from unittest.mock import Mock

import pytest


@pytest.fixture(autouse=True)
def disable_product_analytics(monkeypatch):
    from bubo import analytics

    monkeypatch.setenv("BUBO_ANALYTICS", "0")
    monkeypatch.setattr(analytics, "_pending_events", [])
    transport = Mock(side_effect=AssertionError("Unexpected analytics HTTP request"))
    monkeypatch.setattr(analytics, "urlopen", transport)
    yield
    # flush() swallows transport errors, so assert after the test as well.
    transport.assert_not_called()
