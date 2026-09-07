"""Cost alerting: the exit code is the alert, so it has to be right.

A cron entry or CI step decides whether to shout based on this script's exit
status. If the ceiling comparison is wrong the failure is silent in the worst
direction — spend runs over and nothing says so.
"""

import asyncio

import pytest

from app.config import settings
from scripts import cost_report


@pytest.fixture(autouse=True)
def restore_rates():
    saved = (
        settings.cost_per_mtok_in,
        settings.cost_per_mtok_out,
        settings.cost_ceiling,
    )
    yield
    (
        settings.cost_per_mtok_in,
        settings.cost_per_mtok_out,
        settings.cost_ceiling,
    ) = saved


def test_no_rates_means_no_number():
    """An unconfigured install must not print a cost it made up."""
    settings.cost_per_mtok_in = 0.0
    settings.cost_per_mtok_out = 0.0
    assert cost_report.spend(1_000_000, 1_000_000) is None


def test_rates_apply_per_million_tokens():
    settings.cost_per_mtok_in = 0.30
    settings.cost_per_mtok_out = 2.50
    # 2M in, 1M out -> 2*0.30 + 1*2.50
    assert cost_report.spend(2_000_000, 1_000_000) == pytest.approx(3.10)


def test_one_sided_rate_still_counts():
    """Some providers bill output only; that must not read as 'unconfigured'."""
    settings.cost_per_mtok_in = 0.0
    settings.cost_per_mtok_out = 2.00
    assert cost_report.spend(5_000_000, 1_000_000) == pytest.approx(2.00)


def _run(monkeypatch, tokens_in, tokens_out) -> int:
    async def fake_collect(days):
        return {
            "events": 10,
            "cached": 2,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "pages": 0,
            "by_user": [],
        }

    class FakeEngine:
        async def dispose(self):
            return None

    monkeypatch.setattr(cost_report, "collect", fake_collect)
    # The engine itself is swapped out; AsyncEngine.dispose is read-only, and
    # this test must not open a database connection anyway.
    monkeypatch.setattr(cost_report, "engine", FakeEngine())
    monkeypatch.setattr("sys.argv", ["cost_report"])
    return asyncio.run(cost_report.main())


def test_over_ceiling_exits_nonzero(monkeypatch):
    settings.cost_per_mtok_in = 1.0
    settings.cost_per_mtok_out = 1.0
    settings.cost_ceiling = 1.0
    assert _run(monkeypatch, 1_000_000, 1_000_000) == 1  # 2.00 > 1.00


def test_under_ceiling_exits_zero(monkeypatch):
    settings.cost_per_mtok_in = 1.0
    settings.cost_per_mtok_out = 1.0
    settings.cost_ceiling = 10.0
    assert _run(monkeypatch, 1_000_000, 1_000_000) == 0


def test_no_ceiling_never_alerts(monkeypatch):
    """0 means 'do not alert', not 'alert on everything'."""
    settings.cost_per_mtok_in = 1.0
    settings.cost_per_mtok_out = 1.0
    settings.cost_ceiling = 0.0
    assert _run(monkeypatch, 999_000_000, 999_000_000) == 0
