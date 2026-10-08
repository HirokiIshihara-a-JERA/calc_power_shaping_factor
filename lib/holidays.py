from __future__ import annotations

from datetime import date

import pandas as pd


def japanese_holidays(
    start: pd.Timestamp,
    end: pd.Timestamp,
    additional_holidays: frozenset[date] = frozenset(),
) -> frozenset[date]:
    """Return Japanese public holidays and optional company holidays."""
    try:
        import holidays
    except ImportError as error:
        raise ImportError("Install holiday support with: pip install holidays") from error

    public = holidays.country_holidays("JP", years=range(start.year, end.year + 1))
    selected = {day for day in public if start.date() <= day <= end.date()}
    selected.update(day for day in additional_holidays if start.date() <= day <= end.date())
    return frozenset(selected)
