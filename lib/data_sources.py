from __future__ import annotations

from dataclasses import dataclass, field
import logging
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CsvDataSource:
    path: Path

    def load(self) -> pd.DataFrame:
        df = pd.read_csv(self.path)
        logger.info("Loaded CSV: path=%s, rows=%d, columns=%d", self.path, len(df), len(df.columns))
        return df


class JerarmDb:
    """Small JERARM wrapper; imported only when a DB query is executed."""

    def fetch_df(self, sql: str) -> pd.DataFrame:
        from jerarm import Jerarm  # type: ignore[import-not-found]

        return Jerarm.querySfAna(sql)


def read_sql(path: Path, **parameters: object) -> str:
    template = path.read_text(encoding="utf-8")
    dict_template_values = {f"__{key}__": value for key, value in parameters.items()}
    return template.format(**dict_template_values)


@dataclass(frozen=True)
class JerarmSpotDataSource:
    sql_path: Path
    start_date: Any
    end_date: Any
    db_client: Any = field(default_factory=JerarmDb)

    def load(self) -> pd.DataFrame:
        start = pd.Timestamp(self.start_date).date()
        end = pd.Timestamp(self.end_date).date()
        if start > end:
            raise ValueError("start_date must not be after end_date.")

        sql = read_sql(
            self.sql_path,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
        )
        logger.info("Executing Snowflake query: sql_path=%s, start=%s, end=%s", self.sql_path, start, end)
        df = self.db_client.fetch_df(sql).copy()
        set_required_columns = {"DELIVERYDATE_DATE", "TIMEFRAME_CODE_CODE"}
        set_missing_columns = set_required_columns.difference(df.columns)
        if set_missing_columns:
            raise ValueError(f"Missing query-result columns: {sorted(set_missing_columns)}")

        df["DELIVERYDATE_DATE"] = pd.to_datetime(
            df["DELIVERYDATE_DATE"], errors="raise"
        )
        df["TIMEFRAME_CODE_CODE"] = pd.to_numeric(
            df["TIMEFRAME_CODE_CODE"], errors="raise"
        ).astype(int)
        logger.info("Loaded Snowflake rows=%d, columns=%d", len(df), len(df.columns))
        return df.sort_values(
            ["DELIVERYDATE_DATE", "TIMEFRAME_CODE_CODE"]
        ).reset_index(drop=True)
