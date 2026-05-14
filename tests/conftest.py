"""Project-wide pytest configuration.

Coerces pytest's exit code 5 (NO_TESTS_COLLECTED) to 0 so that a fresh scaffold,
or an unrelated subset run, does not break CI gates that treat any non-zero
exit as failure. Real test failures still propagate normally.
"""

from __future__ import annotations

import pytest


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Treat 'no tests collected' as success."""
    if exitstatus == pytest.ExitCode.NO_TESTS_COLLECTED:
        session.exitstatus = pytest.ExitCode.OK
