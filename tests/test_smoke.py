from __future__ import annotations

import numpy as np
import pandas as pd

from conf.batch_config import Config
from lib import analysis


class FrameSource:
    def __init__(self, data: pd.DataFrame) -> None:
        self.data = data

    def load(self) -> pd.DataFrame:
        return self.data.copy()


def raw_data() -> pd.DataFrame:
    dates = pd.date_range("2023-04-01", "2025-03-31", freq="D")
    grid = pd.MultiIndex.from_product(
        [dates, range(1, 49)], names=["DELIVERYDATE_DATE", "TIMEFRAME_CODE_CODE"]
    ).to_frame(index=False)
    day = (grid["DELIVERYDATE_DATE"] - pd.Timestamp("2023-04-01")).dt.days
    slot = grid["TIMEFRAME_CODE_CODE"]
    grid["SYSTEM_PRICE_VALUE"] = 10.0 + grid["DELIVERYDATE_DATE"].dt.month + slot / 10.0 + day / 10000.0
    return grid


def no_holidays(*args, **kwargs):
    return frozenset()


def test_core_logic(monkeypatch):
    monkeypatch.setattr(analysis, "japanese_holidays", no_holidays)
    config = Config(price_columns=("SYSTEM_PRICE_VALUE",), future_horizon_years=1)
    panel = analysis.prepare_price_panel(raw_data(), config)
    split = analysis.infer_fiscal_year_split(panel)
    assert split.test_year == 2024
    assert split.windows == {"E1": (2023,)}

    factors = analysis.estimate_shaping_factors(panel, (2023,))
    first = factors.iloc[0]
    expected = first["conditional_average"] / first["monthly_average"]
    assert np.isclose(first["shaping_factor"], expected, rtol=0.0, atol=1e-12)

    tables = analysis.run_analysis(FrameSource(raw_data()), config)
    assert len(tables.window_ranking) == 1
    assert tables.future_shaping_table.groupby("date")["timeframe"].nunique().eq(48).all()
    assert tables.future_shaping_table["year"].eq(
        tables.future_shaping_table["date"].dt.to_period("M").dt.to_timestamp()
    ).all()
    assert not tables.future_shaping_table["shaping_factor"].isna().any()
    assert set(tables.as_mapping()) == {
        "window_ranking",
        "oos_evaluation_factors",
        "raw_shaping_factors",
        "future_shaping_table",
        "future_shaping_summary",
    }
    assert np.allclose(
        tables.window_ranking["rmse"],
        tables.window_ranking["raw_rmse"],
        rtol=0.0,
        atol=0.0,
    )
    df_oos = tables.oos_evaluation_factors.copy()
    df_oos["weighted_factor"] = (
        df_oos["evaluation_shaping_factor"]
        * df_oos["evaluation_calendar_count"]
    )
    df_oos_monthly = df_oos.groupby(
        ["price_column", "window", "month"]
    ).agg(
        weighted_sum=("weighted_factor", "sum"),
        calendar_count=("evaluation_calendar_count", "sum"),
    )
    assert (
        df_oos_monthly["weighted_sum"]
        .div(df_oos_monthly["calendar_count"])
        .sub(1.0).abs().max() < 1e-12
    )
    assert tables.future_shaping_table.groupby(
        ["price_column", "fiscal_year", "month"]
    )["production_shaping_factor"].mean().sub(1.0).abs().max() < 1e-12


def test_insert_db_passes_dataframe_without_modification():
    from lib.db_outputs import _insert_db

    df_input = pd.DataFrame({"A": [1.0, 2.0], "B": ["x", "y"]})
    df_before = df_input.copy(deep=True)
    received = {}

    def fake_insert(df, **kwargs):
        received["df"] = df
        received["kwargs"] = kwargs
        return "ok"

    result = _insert_db(
        df_input,
        "JEPX_FUTURE_SHAPING_TABLE",
        database_name="ANALYTICS",
        schema_name="RISK",
        insert_function=fake_insert,
    )

    assert result == "ok"
    assert received["df"] is df_input
    pd.testing.assert_frame_equal(df_input, df_before)
    assert received["kwargs"] == {
        "table_name": "JEPX_FUTURE_SHAPING_TABLE",
        "overwrite": False,
        "database_name": "ANALYTICS",
        "schema_name": "RISK",
    }
