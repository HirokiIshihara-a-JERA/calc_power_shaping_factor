from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date

DAY_TYPES = ("HOL", "SAT", "SUN", "MON", "TUE", "WED", "THU", "FRI")
WEEKDAY_CODES = dict(enumerate(day.upper() for day in calendar.day_abbr))
SHAPE_KEYS = ["month", "timeframe", "theta_group"]
RESIDUAL_ACF_LAGS = (1, 2, 48, 336)

DEFAULT_THETA = {
    "HOL": "G0",
    "SAT": "G0",
    "SUN": "G0",
    "MON": "G1",
    "FRI": "G1",
    "TUE": "G2",
    "WED": "G2",
    "THU": "G2",
}


@dataclass(frozen=True)
class Config:
    """Runtime settings required by the production calculation."""

    price_columns: tuple[str, ...] = ()
    date_column: str = "DELIVERYDATE_DATE"
    timeframe_column: str = "TIMEFRAME_CODE_CODE"
    fiscal_year_start_month: int = 4
    theta: dict[str, str] = field(default_factory=lambda: DEFAULT_THETA.copy())
    minimum_observations: int = 1
    future_horizon_years: int = 10
    additional_holidays: frozenset[date] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if set(self.theta) != set(DAY_TYPES):
            raise ValueError("theta must map all supported day types.")
        if not 1 <= self.fiscal_year_start_month <= 12:
            raise ValueError("fiscal_year_start_month must be between 1 and 12.")
        if self.minimum_observations < 1:
            raise ValueError("minimum_observations must be positive.")
        if self.future_horizon_years < 1:
            raise ValueError("future_horizon_years must be positive.")
