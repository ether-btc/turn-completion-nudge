import os

import pytest


@pytest.fixture(autouse=True)
def isolated_test_fixtures(tmp_path, monkeypatch):
    """Isolated Test Fixtures: give each test its own HERMES_HOME."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    yield
