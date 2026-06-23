"""Tests for the E2E opt-in gate itself.

These are NOT e2e tests — they verify the gating mechanism:
  - an ``e2e``-marked item is deselected (not failed) by default,
  - it runs only when ``W2A_E2E_RUN=1``.

The mechanism lives in ``tests/conftest.py``.
"""


import pytest

import tests.conftest as conftest


def test_e2e_enabled_flag_reads_env(monkeypatch):
    """``e2e_enabled()`` is True iff W2A_E2E_RUN == '1'."""
    monkeypatch.delenv("W2A_E2E_RUN", raising=False)
    assert conftest.e2e_enabled() is False

    monkeypatch.setenv("W2A_E2E_RUN", "1")
    assert conftest.e2e_enabled() is True

    monkeypatch.setenv("W2A_E2E_RUN", "0")
    assert conftest.e2e_enabled() is False


def test_deselect_items_helper_filters_when_disabled(monkeypatch):
    """When disabled, e2e-marked items are dropped from the session; others stay."""
    pytestmark = pytest.mark.usefixtures  # noqa  (placeholder, not used)
    monkeypatch.delenv("W2A_E2E_RUN", raising=False)

    # Build two fake items: one e2e-marked, one plain.
    class FakeMarker:
        def __init__(self, name):
            self.name = name

    class FakeItem:
        def __init__(self, has_e2e, own_marker):
            self.has_e2e = has_e2e
            self.own_marker = own_marker

        def get_closest_marker(self, name):
            if name == "e2e" and self.has_e2e:
                return FakeMarker("e2e")
            return None

    e2e_item = FakeItem(has_e2e=True, own_marker=None)
    plain_item = FakeItem(has_e2e=False, own_marker=None)
    items = [plain_item, e2e_item]

    conftest.pytest_collection_modifyitems(items)

    # The e2e item must have been deselected (removed from items).
    assert e2e_item not in items
    assert plain_item in items


def test_deselect_keeps_e2e_items_when_enabled(monkeypatch):
    """When W2A_E2E_RUN=1, e2e-marked items stay in the session."""
    monkeypatch.setenv("W2A_E2E_RUN", "1")

    class FakeMarker:
        def __init__(self, name):
            self.name = name

    class FakeItem:
        def __init__(self, has_e2e):
            self.has_e2e = has_e2e

        def get_closest_marker(self, name):
            if name == "e2e" and self.has_e2e:
                return FakeMarker("e2e")
            return None

    e2e_item = FakeItem(has_e2e=True)
    items = [e2e_item]
    conftest.pytest_collection_modifyitems(items)
    assert e2e_item in items  # kept when enabled
