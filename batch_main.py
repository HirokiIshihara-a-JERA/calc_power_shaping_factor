"""Command-line entry point for the JEPX shaping-factor batch."""
from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from conf.batch_config import Config
from lib.analysis import run_analysis
from lib.data_sources import CsvDataSource, JerarmSpotDataSource

APP_HOME = Path(__file__).resolve().parent
LOG_DIR = APP_HOME / "log"


def _build_logger() -> logging.Logger:
    """Create the project logger, preferring the JERARM logger in production.

    The fallback keeps CSV execution and unit tests independent of the internal
    ``jerarm`` package while preserving the same ``logging.Logger`` interface.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        from jerarm import Jerarm  # type: ignore[import-not-found]

        return Jerarm.get_logger(
            module_name=__name__,
            log_file_name=os.path.basename(__file__),
            log_dir=str(LOG_DIR),
        )
    except ImportError:
        logger_fallback = logging.getLogger(__name__)
        if not logger_fallback.handlers:
            logger_fallback.setLevel(logging.INFO)
            formatter = logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s %(message)s"
            )
            handler_file = logging.FileHandler(
                LOG_DIR / f"{Path(__file__).stem}.log", encoding="utf-8"
            )
            handler_file.setFormatter(formatter)
            logger_fallback.addHandler(handler_file)
        return logger_fallback


logger = _build_logger()


def parse_args() -> argparse.Namespace:
    """Parse batch arguments without changing existing defaults."""
    parser = argparse.ArgumentParser(description="Calculate JEPX shaping factors")
    parser.add_argument("input_csv", type=Path, nargs="?")
    parser.add_argument("--data-source", choices=("csv", "jerarm"), default="csv")
    parser.add_argument("--sql-path", type=Path, default=Path("sql/jepx_spot_price_koma.sql"))
    parser.add_argument("--start-date", default="2020-04-01")
    parser.add_argument("--end-date", default="2026-03-31")
    parser.add_argument("--output-directory", type=Path, default=Path("data"))
    parser.add_argument("--output-prefix", default="shaping")
    parser.add_argument("--price-columns", nargs="*", default=())
    parser.add_argument("--future-horizon-years", type=int, default=10)
    return parser.parse_args()


def build_data_source(args: argparse.Namespace):
    """Build the selected input adapter and log the executed branch."""
    if args.data_source == "csv":
        if args.input_csv is None:
            logger.error("CSV branch selected without input_csv.")
            raise ValueError("input_csv is required when --data-source=csv.")
        logger.info("Selected CSV data source: %s", args.input_csv)
        return CsvDataSource(args.input_csv)

    logger.info(
        "Selected JERARM/Snowflake data source: sql=%s, start=%s, end=%s",
        args.sql_path,
        args.start_date,
        args.end_date,
    )
    return JerarmSpotDataSource(args.sql_path, args.start_date, args.end_date)


def main() -> None:
    """Execute the complete batch and log branch decisions and output metadata."""
    logger.info("========================= START =========================")
    args = parse_args()
    logger.info(
        "Batch parameters: data_source=%s, future_horizon_years=%d, output_directory=%s",
        args.data_source,
        args.future_horizon_years,
        args.output_directory,
    )
    config = Config(
        price_columns=tuple(args.price_columns),
        future_horizon_years=args.future_horizon_years,
    )
    if config.price_columns:
        logger.info("Using explicitly configured price columns: %s", config.price_columns)
    else:
        logger.info("Price columns will be discovered from the input schema.")

    output_tables = run_analysis(build_data_source(args), config)
    df_window_ranking = output_tables.window_ranking
    for price_column, df_price_ranking in df_window_ranking.groupby("price_column", sort=True):
        s_selected = df_price_ranking.iloc[0]
        logger.info(
            "Selected model: price_column=%s, window=%s, lookback_years=%d, test_fiscal_year=%d, raw_rmse=%.12g, normalized_rmse=%.12g",
            price_column,
            s_selected["window"],
            int(s_selected["lookback_years"]),
            int(s_selected["test_fiscal_year"]),
            float(s_selected["raw_rmse"]),
            float(s_selected["normalized_rmse"]),
        )
    args.output_directory.mkdir(parents=True, exist_ok=True)
    for output_name, df_output in output_tables.as_mapping().items():
        path_output = args.output_directory / f"{args.output_prefix}_{output_name}.csv"
        df_output.to_csv(path_output, index=False, encoding="utf-8-sig")
        logger.info(
            "Wrote output: name=%s, rows=%d, columns=%d, path=%s",
            output_name,
            len(df_output),
            len(df_output.columns),
            path_output,
        )
    logger.info("========================== END ==========================")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.exception("Batch terminated with an unhandled exception.")
        raise
