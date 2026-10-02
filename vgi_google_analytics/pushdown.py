"""Applying a DuckDB ``WHERE`` to emitted rows, and translating part of it for GA4.

Two separate jobs, with very different stakes:

* :func:`build_filtered` **applies** every pushed predicate to the rows a
  function emits. That is not optional. Once a function declares
  ``filter_pushdown`` the engine drops its own filter above the scan, so a
  predicate the worker receives and ignores is not applied by anyone — the
  query silently returns rows that fail it.

* :func:`dimension_filters` **translates** the subset of predicates that GA4
  can evaluate itself into a ``dimensionFilter``, so the API returns fewer rows.
  That is a pure optimisation, and it declines whenever it is unsure: pushing
  too little costs bandwidth, pushing too much drops rows nothing downstream can
  recover.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
from vgi.table_function import ProcessParams

from vgi_google_analytics.schemas import batch_from_rows


def _filters(params: ProcessParams[Any]) -> Any:
    return getattr(params, "current_pushdown_filters", None)


def equality(params: ProcessParams[Any], column: str) -> str | None:
    """The constant from ``WHERE <column> = '...'``, when there is exactly one string."""
    filters = _filters(params)
    if filters is None:
        return None
    scalar = filters.get_column_constant(column)
    value = scalar.as_py() if scalar is not None else None
    return value if isinstance(value, str) and value else None


def dimension_filters(params: ProcessParams[Any], columns: Sequence[str]) -> list[dict[str, Any]]:
    """GA4 ``FilterExpression``s equivalent to pushed predicates on string dimensions.

    Only ``=`` and ``IN`` (including an ``OR`` of equalities on one column) are
    translated, as case-sensitive exact matches — the same comparison DuckDB
    makes. Anything else (``LIKE``, ranges, ``IS NULL``, a cross-column ``OR``)
    is left for :func:`build_filtered` to apply after the fact.

    A predicate on a requested dimension means the same thing whether GA4
    applies it before aggregation or DuckDB applies it after: each report row is
    already grouped by that dimension's value, so the row either matches or it
    does not.
    """
    filters = _filters(params)
    if filters is None:
        return []
    expressions: list[dict[str, Any]] = []
    for column in columns:
        values = filters.get_column_values(column)
        if values is None:
            continue
        strings = [v for v in values.to_pylist() if isinstance(v, str)]
        if not strings or len(strings) != len(values):
            continue
        if len(strings) == 1:
            expressions.append(
                {
                    "filter": {
                        "fieldName": column,
                        "stringFilter": {"matchType": "EXACT", "value": strings[0], "caseSensitive": True},
                    }
                }
            )
        else:
            expressions.append(
                {"filter": {"fieldName": column, "inListFilter": {"values": strings, "caseSensitive": True}}}
            )
    return expressions


def build_filtered(params: ProcessParams[Any], rows: Sequence[dict[str, Any]]) -> pa.RecordBatch:
    """Build the projected output batch, with every pushed filter applied to it.

    Everything is evaluated against ``params.output_schema`` — the *projected*
    schema — and that is load-bearing: a pushed filter carries a column index
    relative to the projection DuckDB asked for, and evaluating it against any
    other column order compares the wrong column. DuckDB never projects away a
    column it filters on, so the projected batch always holds what the predicate
    references.
    """
    batch = batch_from_rows(rows, params.output_schema)
    filters = _filters(params)
    if filters is None or batch.num_rows == 0:
        return batch
    return pc.filter(batch, filters.evaluate(batch))
