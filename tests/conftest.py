import pytest


@pytest.fixture(autouse=True)
def _isolate_subfleet_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a caller's Subfleet state root out of temporary test homes.

    Discovery honors ``SUBFLEET_HOME`` for the current user's home, and some
    tests patch ``Path.home`` to a temporary directory. A value inherited from
    a Subfleet-launched shell would otherwise point those tests at real lanes.
    """

    monkeypatch.delenv("SUBFLEET_HOME", raising=False)
