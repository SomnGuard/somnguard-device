import pytest


@pytest.fixture(autouse=True)
def default_test_environment(monkeypatch):
    """Tests default to development because their API fixtures use localhost HTTP."""
    monkeypatch.setenv("SOMNGUARD_ENV", "development")
