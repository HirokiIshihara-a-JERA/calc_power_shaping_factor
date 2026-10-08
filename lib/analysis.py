from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any, Sequence

import numpy as np
import pandas as pd
from scipy import stats

from conf.batch_config import Config, RESIDUAL_ACF_LAGS, SHAPE_KEYS, WEEKDAY_CODES
from lib.holidays import japanese_holidays

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FiscalYearSplit:
    test_year: int
    windows: dict[str, tuple[int, ...]]


@dataclass
class OutputTables:
    window_ranking: pd.DataFrame
    oos_evaluation_factors: pd.DataFrame
    raw_shaping_factors: pd.DataFrame
    future_shaping_table: pd.DataFrame
    future_shaping_summary: pd.DataFrame

    def as_mapping(self) -> dict[str, pd.DataFrame]:
        """Return stable output names for CSV and database adapters."""
        return {
            "window_ranking": self.window_ranking,
            "oos_evaluation_factors": self.oos_evaluation_factors,
            "raw_shaping_factors": self.raw_shaping_factors,
            "future_shaping_table": self.future_shaping_table,
            "future_shaping_summary": self.future_shaping_summary,
        }


def discover_price_columns(list_selected_columns: Sequence[str], tuple_configured_price_columns: tuple[str, ...]) -> tuple[str, ...]:
    tuple_available_columns = tuple(list_selected_columns)
    if tuple_configured_price_columns:
        set_missing_columns = set(tuple_configured_price_columns).difference(tuple_available_columns)
        if set_missing_columns:
            raise ValueError(f"Missing price columns: {sorted(set_missing_columns)}")
        return tuple_configured_price_columns

    def is_price(column: str) -> bool:
        upper = column.upper()
        return (
            upper == "SYSTEM_PRICE_VALUE"
            or (upper.startswith("PRICE_") and upper.endswith("_VALUE"))
            or upper.endswith("_PRICE_VALUE")
        )

    tuple_discovered_price_columns = tuple(column for column in tuple_available_columns if is_price(column))
    if not tuple_discovered_price_columns:
        raise ValueError("No price columns were discovered.")
    return tuple_discovered_price_columns


def prepare_price_panel(df_raw: pd.DataFrame, config: Config) -> pd.DataFrame:
    """Convert wide JEPX observations to the long analytical price panel.

    Variables use type-oriented prefixes: ``df_`` for DataFrames, ``list_`` for lists, ``dict_`` for dictionaries, ``set_`` for sets, and ``tuple_`` for tuples. The
    temporary generic frame is named ``df`` to avoid the redundant name
    ``df_data``. This function changes shape and labels only; it does not
    alter valid source prices.
    """
    set_required_columns = {config.date_column, config.timeframe_column}
    set_missing_columns = set_required_columns.difference(df_raw.columns)
    if set_missing_columns:
        raise ValueError(f"Missing required columns: {sorted(set_missing_columns)}")

    tuple_price_columns = discover_price_columns(df_raw.columns, config.price_columns)
    logger.info("Preparing price panel: input_rows=%d, price_columns=%s", len(df_raw), tuple_price_columns)
    df = df_raw[[config.date_column, config.timeframe_column, *tuple_price_columns]].copy()
    df["date"] = pd.to_datetime(df[config.date_column], errors="raise").dt.normalize()
    df["timeframe"] = pd.to_numeric(
        df[config.timeframe_column], errors="raise"
    ).astype(int)
    if not df["timeframe"].between(1, 48).all():
        raise ValueError("timeframe values must be between 1 and 48.")

    df_price_panel = df.melt(
        id_vars=["date", "timeframe"],
        value_vars=list(tuple_price_columns),
        var_name="price_column",
        value_name="price",
    )
    df_price_panel["price"] = pd.to_numeric(df_price_panel["price"], errors="coerce")
    df_price_panel = df_price_panel.dropna(subset=["price"]).copy()
    df_price_panel["month"] = df_price_panel["date"].dt.month
    start_month = config.fiscal_year_start_month
    df_price_panel["fiscal_year"] = np.where(
        df_price_panel["month"] >= start_month,
        df_price_panel["date"].dt.year,
        df_price_panel["date"].dt.year - 1,
    ).astype(int)
    df_price_panel["weekday_code"] = df_price_panel["date"].dt.weekday.map(WEEKDAY_CODES)
    set_holiday_dates = japanese_holidays(
        df_price_panel["date"].min(), df_price_panel["date"].max(), config.additional_holidays
    )
    df_price_panel["is_holiday"] = df_price_panel["date"].dt.date.isin(set_holiday_dates)
    df_price_panel["day_type"] = np.where(
        df_price_panel["is_holiday"], "HOL", df_price_panel["weekday_code"]
    )
    df_price_panel["theta_group"] = df_price_panel["day_type"].map(config.theta)
    return df_price_panel.sort_values(["price_column", "date", "timeframe"]).reset_index(drop=True)


def infer_fiscal_year_split(df: pd.DataFrame, start_month: int = 4) -> FiscalYearSplit:
    df_year_coverage = df.groupby("fiscal_year").agg(
        first_date=("date", "min"),
        last_date=("date", "max"),
        month_count=("month", "nunique"),
    )
    list_complete_fiscal_years: list[int] = []
    for fiscal_year, row in df_year_coverage.iterrows():
        start = pd.Timestamp(int(fiscal_year), start_month, 1)
        end = pd.Timestamp(int(fiscal_year) + 1, start_month, 1) - pd.Timedelta(days=1)
        if row["month_count"] == 12 and row["first_date"] <= start and row["last_date"] >= end:
            list_complete_fiscal_years.append(int(fiscal_year))
    list_complete_fiscal_years = sorted(list_complete_fiscal_years)
    if len(list_complete_fiscal_years) < 2:
        raise ValueError("At least two complete fiscal years are required.")
    test_year = list_complete_fiscal_years[-1]
    logger.info("Fiscal-year split: complete_years=%s, test_year=%d", tuple(list_complete_fiscal_years), test_year)
    training_years_available = tuple(list_complete_fiscal_years[:-1])
    dict_windows = {f"E{length}": training_years_available[-length:] for length in range(1, len(training_years_available) + 1)}
    return FiscalYearSplit(test_year, dict_windows)


def estimate_shaping_factors(
    df: pd.DataFrame,
    training_years: Sequence[int],
    minimum_observations: int = 1,
) -> pd.DataFrame:
    """Estimate pooled monthly x timeframe x theta shaping factors.

    For training-year set E, month m, slot h, and group g:

    ``D(E,h,m,g) = mean(P | E,h,m,g) / mean(P | E,m)``.

    Observations are pooled before taking means; annual factors are not
    averaged. This preserves the numerical definition used by the prior code.
    """
    tuple_training_years = tuple(sorted(set(training_years)))
    df_training = df[df["fiscal_year"].isin(tuple_training_years)].copy()
    if df_training.empty:
        raise ValueError(f"No observations for {tuple_training_years}.")
    df_training["monthly_average"] = df_training.groupby("month")["price"].transform("mean")
    df_factors = df_training.groupby(SHAPE_KEYS, as_index=False).agg(
        conditional_average=("price", "mean"),
        monthly_average=("monthly_average", "first"),
        observation_count=("price", "size"),
    )
    df_factors = df_factors[df_factors["observation_count"] >= minimum_observations].copy()
    df_factors["shaping_factor"] = df_factors["conditional_average"] / df_factors["monthly_average"]
    df_factors["source_fiscal_years"] = str(tuple_training_years)
    return df_factors


def reconstruct_out_of_sample(
    df: pd.DataFrame,
    training_years: Sequence[int],
    test_year: int,
    minimum_observations: int,
) -> tuple[pd.DataFrame, pd.DataFrame, int]:
    """Apply both raw and calendar-normalized factors to the held-out year.

    Existing raw columns are retained without changing their definitions:
    ``shaping_factor``, ``calculated_price``, and ``residual``.  Additional
    columns hold the OOS-calendar normalization and normalized evaluation.
    Normalization is performed by test-year calendar month, using only rows
    with an available raw factor.  Consequently, the normalized factor has
    an arithmetic mean of one in each evaluated month.
    """
    df_factors = estimate_shaping_factors(df, training_years, minimum_observations)
    df_evaluated = df[df["fiscal_year"] == test_year].copy()
    if df_evaluated.empty:
        raise ValueError(f"No observations for test year {test_year}.")

    df_evaluated["test_monthly_average"] = df_evaluated.groupby("month")["price"].transform("mean")
    df_evaluated = df_evaluated.merge(
        df_factors[[*SHAPE_KEYS, "shaping_factor"]],
        on=SHAPE_KEYS,
        how="left",
        validate="many_to_one",
    )
    missing_count = int(df_evaluated["shaping_factor"].isna().sum())
    df_evaluated = df_evaluated.dropna(subset=["shaping_factor"]).copy()

    # Raw OOS path: preserved exactly for backward-compatible values.
    df_evaluated["calculated_price"] = df_evaluated["test_monthly_average"] * df_evaluated["shaping_factor"]
    df_evaluated["residual"] = df_evaluated["price"] - df_evaluated["calculated_price"]
    df_evaluated["absolute_residual"] = df_evaluated["residual"].abs()
    df_evaluated["squared_residual"] = df_evaluated["residual"].pow(2)

    # Evaluation path: normalize the same raw factors by the OOS month mix.
    df_evaluated["evaluation_factor_mean"] = df_evaluated.groupby("month")[
        "shaping_factor"
    ].transform("mean")
    if df_evaluated["evaluation_factor_mean"].eq(0.0).any():
        raise ValueError("OOS monthly factor mean must not be zero.")
    df_evaluated["evaluation_normalization_multiplier"] = (
        1.0 / df_evaluated["evaluation_factor_mean"]
    )
    df_evaluated["evaluation_shaping_factor"] = (
        df_evaluated["shaping_factor"]
        * df_evaluated["evaluation_normalization_multiplier"]
    )
    df_evaluated["normalized_calculated_price"] = (
        df_evaluated["test_monthly_average"]
        * df_evaluated["evaluation_shaping_factor"]
    )
    df_evaluated["normalized_residual"] = (
        df_evaluated["price"] - df_evaluated["normalized_calculated_price"]
    )
    df_evaluated["normalized_absolute_residual"] = df_evaluated[
        "normalized_residual"
    ].abs()
    df_evaluated["normalized_squared_residual"] = df_evaluated[
        "normalized_residual"
    ].pow(2)

    return (
        df_evaluated.sort_values(["date", "timeframe"]).reset_index(drop=True),
        df_factors,
        missing_count,
    )


def newey_west_summary(residuals: Sequence[float], bandwidth: int = 48) -> dict[str, float]:
    values = np.asarray(residuals, dtype=float)
    values = values[np.isfinite(values)]
    size = len(values)
    if size < 2:
        return {name: float("nan") for name in (
            "hac_mean", "hac_standard_error", "hac_z_statistic", "hac_p_value"
        )} | {"hac_bandwidth": 0.0}
    bandwidth = min(max(int(bandwidth), 0), size - 1)
    centered = values - values.mean()
    long_run_variance = float(np.dot(centered, centered) / size)
    for lag in range(1, bandwidth + 1):
        autocovariance = float(np.dot(centered[lag:], centered[:-lag]) / size)
        long_run_variance += 2.0 * (1.0 - lag / (bandwidth + 1.0)) * autocovariance
    standard_error = float(np.sqrt(max(long_run_variance, 0.0) / size))
    mean = float(values.mean())
    z_statistic = mean / standard_error if standard_error > 0 else float("nan")
    p_value = float(2.0 * stats.norm.sf(abs(z_statistic))) if standard_error > 0 else float("nan")
    return {
        "hac_mean": mean,
        "hac_standard_error": standard_error,
        "hac_z_statistic": float(z_statistic),
        "hac_p_value": p_value,
        "hac_bandwidth": float(bandwidth),
    }


def residual_diagnostics(df_evaluated: pd.DataFrame) -> dict[str, float]:
    residuals = df_evaluated["residual"].dropna().astype(float)
    if len(residuals) < 2:
        raise ValueError("At least two residual observations are required.")
    values = residuals.to_numpy()
    first, second = np.array_split(values, 2)
    first_variance = float(np.var(first, ddof=1))
    variance_ratio = float(np.var(second, ddof=1) / first_variance) if first_variance > 0 else float("nan")
    jarque_bera = stats.jarque_bera(values)
    dict_diagnostics = {
        "residual_standard_deviation": float(residuals.std(ddof=1)),
        "residual_median": float(residuals.median()),
        "residual_skewness": float(stats.skew(values, bias=False)),
        "residual_excess_kurtosis": float(stats.kurtosis(values, fisher=True, bias=False)),
        "residual_q01": float(residuals.quantile(0.01)),
        "residual_q05": float(residuals.quantile(0.05)),
        "residual_q95": float(residuals.quantile(0.95)),
        "residual_q99": float(residuals.quantile(0.99)),
        "maximum_absolute_residual": float(residuals.abs().max()),
        "positive_residual_fraction": float((residuals > 0).mean()),
        "second_to_first_half_variance_ratio": variance_ratio,
        "jarque_bera_statistic": float(jarque_bera.statistic),
        "jarque_bera_p_value": float(jarque_bera.pvalue),
    }
    for lag in RESIDUAL_ACF_LAGS:
        dict_diagnostics[f"residual_acf_lag_{lag}"] = (
            float(residuals.autocorr(lag=lag)) if len(residuals) > lag else float("nan")
        )
    dict_diagnostics.update(newey_west_summary(values))
    return dict_diagnostics


def evaluate_window(
    df: pd.DataFrame,
    training_years: Sequence[int],
    test_year: int,
    minimum_observations: int,
) -> dict[str, Any]:
    """Evaluate one lookback window and retain raw and normalized metrics."""
    df_evaluated, df_factors, missing_count = reconstruct_out_of_sample(
        df, training_years, test_year, minimum_observations
    )
    raw_residuals = df_evaluated["residual"]
    normalized_residuals = df_evaluated["normalized_residual"]
    tuple_training_years = tuple(training_years)
    raw_rmse = float(np.sqrt(np.mean(np.square(raw_residuals))))
    raw_mae = float(np.mean(np.abs(raw_residuals)))
    raw_bias = float(np.mean(raw_residuals))
    dict_diagnostics: dict[str, Any] = {
        "test_fiscal_year": test_year,
        "training_fiscal_years": str(tuple_training_years),
        "lookback_years": len(tuple_training_years),
        # Existing names and values remain the raw OOS metrics.
        "rmse": raw_rmse,
        "mae": raw_mae,
        "bias": raw_bias,
        "raw_rmse": raw_rmse,
        "raw_mae": raw_mae,
        "raw_bias": raw_bias,
        "normalized_rmse": float(np.sqrt(np.mean(np.square(normalized_residuals)))),
        "normalized_mae": float(np.mean(np.abs(normalized_residuals))),
        "normalized_bias": float(np.mean(normalized_residuals)),
        "maximum_absolute_monthly_normalized_bias": float(
            df_evaluated.groupby("month")["normalized_residual"].mean().abs().max()
        ),
        "maximum_absolute_normalization_multiplier_deviation": float(
            (df_evaluated["evaluation_normalization_multiplier"] - 1.0).abs().max()
        ),
        "evaluation_count": len(df_evaluated),
        "missing_factor_count": missing_count,
        "factor_count": len(df_factors),
    }
    dict_diagnostics.update(residual_diagnostics(df_evaluated))
    return dict_diagnostics


def rank_rolling_windows(df: pd.DataFrame, split: FiscalYearSplit, minimum_observations: int) -> pd.DataFrame:
    list_ranking_rows = [
        {"window": name, **evaluate_window(df, tuple_training_years, split.test_year, minimum_observations)}
        for name, tuple_training_years in split.windows.items()
    ]
    df_window_ranking = pd.DataFrame(list_ranking_rows).sort_values(["rmse", "mae", "lookback_years"]).reset_index(drop=True)
    df_window_ranking.insert(0, "rank", np.arange(1, len(df_window_ranking) + 1))
    return df_window_ranking


def roll_window_forward(lookback_years: int, latest_year: int) -> tuple[int, ...]:
    return tuple(range(latest_year - lookback_years + 1, latest_year + 1))


def build_future_calendar(start_year: int, end_year: int, config: Config) -> pd.DataFrame:
    start_month = config.fiscal_year_start_month
    start = pd.Timestamp(start_year, start_month, 1)
    end = pd.Timestamp(end_year + 1, start_month, 1) - pd.Timedelta(days=1)
    dates = pd.date_range(start, end, freq="D")
    set_holiday_dates = japanese_holidays(start, end, config.additional_holidays)
    df_future = pd.DataFrame({"date": dates})
    df_future["fiscal_year"] = np.where(
        df_future["date"].dt.month >= start_month,
        df_future["date"].dt.year,
        df_future["date"].dt.year - 1,
    ).astype(int)
    df_future["month"] = df_future["date"].dt.month
    df_future["weekday_code"] = df_future["date"].dt.weekday.map(WEEKDAY_CODES)
    df_future["is_holiday"] = df_future["date"].dt.date.isin(set_holiday_dates)
    df_future["day_type"] = np.where(df_future["is_holiday"], "HOL", df_future["weekday_code"])
    df_future["theta_group"] = df_future["day_type"].map(config.theta)
    return df_future


def build_future_shaping_table(df_factors: pd.DataFrame, calendar: pd.DataFrame) -> pd.DataFrame:
    """Assign raw factors and normalize them to each future calendar month."""
    df_timeframes = pd.DataFrame({"timeframe": np.arange(1, 49, dtype=int)})
    df_future = calendar.merge(df_timeframes, how="cross").merge(
        df_factors, on=SHAPE_KEYS, how="left", validate="many_to_one"
    )
    if df_future["shaping_factor"].isna().any():
        raise ValueError("Future calendar contains unmatched shaping factors.")

    list_future_month_keys = ["fiscal_year", "month"]
    df_future["production_factor_mean"] = df_future.groupby(
        list_future_month_keys
    )["shaping_factor"].transform("mean")
    if df_future["production_factor_mean"].eq(0.0).any():
        raise ValueError("Future monthly factor mean must not be zero.")
    df_future["production_normalization_multiplier"] = (
        1.0 / df_future["production_factor_mean"]
    )
    df_future["production_shaping_factor"] = (
        df_future["shaping_factor"]
        * df_future["production_normalization_multiplier"]
    )
    df_future["delivery_datetime"] = df_future["date"] + pd.to_timedelta(
        (df_future["timeframe"] - 1) * 30, unit="minutes"
    )
    df_future["delivdate"] = (
       df_future["date"].dt.to_period("M").dt.to_timestamp().dt.date
    )
    list_selected_columns = [
        "date", "delivery_datetime", "delivdate", "fiscal_year", "month", 
        "timeframe", "weekday_code", "is_holiday", "day_type", "theta_group",
        "shaping_factor", "production_factor_mean",
        "production_normalization_multiplier", "production_shaping_factor",
        "source_fiscal_years", "conditional_average", "monthly_average",
        "observation_count",
    ]
    return df_future[list_selected_columns].sort_values(
        ["date", "timeframe"]
    ).reset_index(drop=True)


def summarize_future_shaping_table(df_future: pd.DataFrame) -> pd.DataFrame:
    """Summarize raw and production-normalized future factors."""
    list_summary_keys = ["fiscal_year", "month", "timeframe", "theta_group"]
    return df_future.groupby(list_summary_keys, as_index=False).agg(
        delivdate=("delivdate", "first"),
        shaping_factor=("shaping_factor", "first"),
        production_factor_mean=("production_factor_mean", "first"),
        production_normalization_multiplier=(
            "production_normalization_multiplier", "first"
        ),
        production_shaping_factor=("production_shaping_factor", "first"),
        conditional_average=("conditional_average", "first"),
        monthly_average=("monthly_average", "first"),
        source_observation_count=("observation_count", "first"),
        future_observation_count=("date", "size"),
        future_date_count=("date", "nunique"),
    ).sort_values(list_summary_keys).reset_index(drop=True)


def run_analysis(data_source: Any, config: Config) -> OutputTables:
    """Run raw ranking, OOS normalization, current refit, and future assignment.

    Raw factor values and the existing raw OOS metrics remain unchanged.
    Window ranking continues to use raw ``rmse``, ``mae``, and lookback length
    so the selected model is backward compatible.  Normalized metrics and
    coefficient tables are additional, separately named outputs.
    """
    df_price_panel = prepare_price_panel(data_source.load(), config)
    list_rankings: list[pd.DataFrame] = []
    list_oos_factors: list[pd.DataFrame] = []
    list_raw_factors: list[pd.DataFrame] = []
    list_future_tables: list[pd.DataFrame] = []
    list_future_summaries: list[pd.DataFrame] = []

    for price_column, df_price_data in df_price_panel.groupby(
        "price_column", sort=True
    ):
        logger.info(
            "Analyzing price column: %s (rows=%d)",
            price_column,
            len(df_price_data),
        )
        split = infer_fiscal_year_split(
            df_price_data, config.fiscal_year_start_month
        )
        df_window_ranking = rank_rolling_windows(
            df_price_data, split, config.minimum_observations
        )
        lookback = int(df_window_ranking.iloc[0]["lookback_years"])
        logger.info(
            "Selected raw-RMSE window: price_column=%s, lookback_years=%d, "
            "raw_rmse=%.12g, normalized_rmse=%.12g",
            price_column,
            lookback,
            float(df_window_ranking.iloc[0]["raw_rmse"]),
            float(df_window_ranking.iloc[0]["normalized_rmse"]),
        )

        # Persist OOS raw/evaluation factors for every candidate window.
        for window, tuple_training_years in split.windows.items():
            df_evaluated, _, _ = reconstruct_out_of_sample(
                df_price_data,
                tuple_training_years,
                split.test_year,
                config.minimum_observations,
            )
            df_oos = df_evaluated.groupby(
                ["month", "timeframe", "theta_group"], as_index=False
            ).agg(
                shaping_factor=("shaping_factor", "first"),
                evaluation_factor_mean=("evaluation_factor_mean", "first"),
                evaluation_normalization_multiplier=(
                    "evaluation_normalization_multiplier", "first"
                ),
                evaluation_shaping_factor=(
                    "evaluation_shaping_factor", "first"
                ),
                evaluation_calendar_count=("date", "size"),
            )
            df_oos.insert(0, "training_fiscal_years", str(tuple_training_years))
            df_oos.insert(0, "test_fiscal_year", split.test_year)
            df_oos.insert(0, "window", window)
            df_oos.insert(0, "price_column", price_column)
            list_oos_factors.append(df_oos)

        current_years = roll_window_forward(lookback, split.test_year)
        df_factors = estimate_shaping_factors(
            df_price_data, current_years, config.minimum_observations
        )
        df_raw_factors = df_factors.copy()
        df_raw_factors.insert(0, "selected_lookback_years", lookback)
        df_raw_factors.insert(0, "price_column", price_column)
        list_raw_factors.append(df_raw_factors)

        future_start = split.test_year + 1
        future_end = future_start + config.future_horizon_years - 1
        df_future = build_future_shaping_table(
            df_factors,
            build_future_calendar(future_start, future_end, config),
        )
        df_future_summary = summarize_future_shaping_table(df_future)
        logger.info(
            "Built future tables: price_column=%s, detail_rows=%d, "
            "summary_rows=%d, maximum_normalization_deviation=%.12g",
            price_column,
            len(df_future),
            len(df_future_summary),
            float(
                (df_future["production_normalization_multiplier"] - 1.0)
                .abs()
                .max()
            ),
        )
        for table in (df_window_ranking, df_future, df_future_summary):
            table.insert(0, "price_column", price_column)
        list_rankings.append(df_window_ranking)
        list_future_tables.append(df_future)
        list_future_summaries.append(df_future_summary)

    return OutputTables(
        window_ranking=pd.concat(list_rankings, ignore_index=True),
        oos_evaluation_factors=pd.concat(list_oos_factors, ignore_index=True),
        raw_shaping_factors=pd.concat(list_raw_factors, ignore_index=True),
        future_shaping_table=pd.concat(list_future_tables, ignore_index=True),
        future_shaping_summary=pd.concat(list_future_summaries, ignore_index=True),
    )
