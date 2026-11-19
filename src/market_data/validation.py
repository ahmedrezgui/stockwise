"""Strict checks for normalized daily bars; no filling, fitting or scaling."""

import numpy as np
import pandas as pd
from pandas.api.types import is_datetime64_any_dtype, is_numeric_dtype

from .schemas import CANONICAL_COLUMNS, PRICE_COLUMNS, ValidationError, validate_symbols


def validate_bars(frame: pd.DataFrame, symbol: str) -> None:
    """Raise ValidationError unless a canonical frame contains valid observed bars."""
    symbol = validate_symbols([symbol])[0]
    if not frame.columns.is_unique:
        raise ValidationError("Duplicate column names")
    missing = set(CANONICAL_COLUMNS) - set(frame.columns)
    if missing:
        raise ValidationError(f"Missing columns: {', '.join(sorted(missing))}")
    if frame.empty:
        raise ValidationError("No completed observations before the cutoff")
    if frame.symbol.isna().any() or not frame.symbol.eq(symbol).all():
        raise ValidationError("Dataset must contain exactly the requested symbol")
    if not is_datetime64_any_dtype(frame.timestamp) or frame.timestamp.dt.tz is not None:
        raise ValidationError("Dates must be timezone-free exchange session dates")
    if frame.timestamp.isna().any() or not frame.timestamp.eq(frame.timestamp.dt.normalize()).all():
        raise ValidationError("Invalid or intraday session dates")
    if frame.timestamp.duplicated().any():
        raise ValidationError("Conflicting duplicate sessions; reconcile the source")
    if not frame.timestamp.is_monotonic_increasing:
        raise ValidationError("Session dates must be sorted")
    for column in (*PRICE_COLUMNS, "volume"):
        if not is_numeric_dtype(frame[column]) or not np.isfinite(frame[column].to_numpy(dtype=float)).all():
            raise ValidationError(f"{column} must contain finite numeric values")
    prices = frame[list(PRICE_COLUMNS)]
    if (prices <= 0).any().any():
        raise ValidationError("OHLC prices must be strictly positive")
    if (frame.volume < 0).any():
        raise ValidationError("Volume must be nonnegative")
    if ((frame.high < prices.max(axis=1)) | (frame.low > prices.min(axis=1))).any():
        raise ValidationError("OHLC range inconsistency")
