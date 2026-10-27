"""Quiet hours on chosen days: Harvey's own work on nights and weekends."""

from datetime import datetime, timedelta

import pytest
import pytz

from harvey.config import QuietHoursConfig, load_config
from harvey.main import in_quiet_hours

PARIS = pytz.timezone("Europe/Paris")


def _config(**quiet):
    config = load_config()
    qh = QuietHoursConfig(timezone="Europe/Paris", **quiet)
    return config.model_copy(update={"usage": config.usage.model_copy(update={"quiet_hours": qh})})


def at(day: int, hour: int, minute: int = 0):
    """2026-10-26 is a Monday; day 0 = that Monday."""
    return PARIS.localize(datetime(2026, 10, 26, hour, minute) + timedelta(days=day))


def test_weekday_office_hours_only():
    config = _config(start="08:00", end="20:00", days=["mon", "tue", "wed", "thu", "fri"])
    assert in_quiet_hours(config, at(0, 10))          # Monday morning: quiet
    assert not in_quiet_hours(config, at(0, 21))      # Monday night: Harvey works
    assert not in_quiet_hours(config, at(5, 10))      # Saturday: Harvey works
    assert not in_quiet_hours(config, at(6, 15))      # Sunday: Harvey works


def test_a_period_crossing_midnight_belongs_to_the_day_it_starts():
    config = _config(start="22:00", end="07:00", days=["fri"])
    assert in_quiet_hours(config, at(4, 23))          # Friday 23:00
    assert in_quiet_hours(config, at(5, 6))           # Saturday 06:00, Friday's night
    assert not in_quiet_hours(config, at(5, 23))      # Saturday 23:00
    assert not in_quiet_hours(config, at(4, 6))       # Friday 06:00, Thursday's night


def test_every_day_by_default_and_days_validated():
    assert QuietHoursConfig().days == ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    assert QuietHoursConfig(days=["Friday", "MON"]).days == ["mon", "fri"]
    with pytest.raises(Exception):
        QuietHoursConfig(days=["someday"])
    assert in_quiet_hours(_config(start="22:00", end="07:00"), at(2, 23))
