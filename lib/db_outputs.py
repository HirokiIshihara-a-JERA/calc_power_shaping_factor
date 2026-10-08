"""Snowflake output helpers kept separate from numerical calculations."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pandas as pd


def _insert_db(
    df: pd.DataFrame,
    table_name: str,
    *,
    database_name: str | None = None,
    schema_name: str | None = None,
    overwrite: bool = False,
    insert_function: Callable[..., Any] | None = None,
) -> Any:
    """Insert one result DataFrame into a Snowflake table through JERARM.

    Parameters
    ----------
    df:
        Result DataFrame to insert. The object is passed without modifying its
        values, index, column names, or dtypes.
    table_name:
        Destination table name.
    database_name, schema_name:
        Optional Snowflake namespace arguments. Omit these when the JERARM
        environment already supplies the default database and schema.
    overwrite:
        When true, request replacement of the destination table. The default
        is append mode.
    insert_function:
        Optional callable for tests. In production, the function defaults to
        ``Jerarm.insertSfAna``.

    Notes
    -----
    Database insertion is deliberately outside ``lib.analysis``. Therefore,
    enabling or disabling Snowflake output cannot affect shaping factors,
    residuals, rankings, or future-table values.
    """
    if df.empty:
        raise ValueError(f"Cannot insert an empty DataFrame into {table_name}.")
    if not table_name.strip():
        raise ValueError("table_name must not be empty.")

    if insert_function is None:
        from jerarm import Jerarm  # type: ignore[import-not-found]

        insert_function = Jerarm.insertSfAna

    dict_insert_kwargs: dict[str, Any] = {
        "table_name": table_name,
        "overwrite": overwrite,
    }
    if database_name is not None:
        dict_insert_kwargs["database_name"] = database_name
    if schema_name is not None:
        dict_insert_kwargs["schema_name"] = schema_name

    return insert_function(df, **dict_insert_kwargs)


def insert_output_tables(
    output_tables: Any,
    dict_table_names: dict[str, str],
    **insert_kwargs: Any,
) -> None:
    """Insert the three output DataFrames using an explicit name mapping."""
    df_mapping = output_tables.as_mapping()
    set_missing_output_names = set(df_mapping).difference(dict_table_names)
    if set_missing_output_names:
        raise ValueError(f"Missing Snowflake table names: {sorted(set_missing_output_names)}")
    for output_name, df_output in df_mapping.items():
        _insert_db(df_output, dict_table_names[output_name], **insert_kwargs)
