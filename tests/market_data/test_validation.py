"""Unit tests for canonical daily-bar invariants."""
from datetime import date, datetime
import unittest

import numpy as np
import pandas as pd

from market_data.schemas import ValidationConfig, ValidationError, parse_cutoff
from market_data.validation import validate_bars


def bars():
    return pd.DataFrame({
        "symbol": ["ATB", "ATB"],
        "timestamp": pd.to_datetime(["2026-10-01", "2026-10-05"]),
        "open": [3.9, 4.0], "high": [4.1, 4.2], "low": [3.8, 3.9],
        "close": [4.0, 4.1], "volume": [0.0, 100.0],
    })


class ValidationTests(unittest.TestCase):
    def test_valid_sparse_history_and_zero_volume(self):
        validate_bars(bars(), "ATB")

    def test_missing_columns_and_empty_history(self):
        for frame in (bars().drop(columns="volume"), bars().iloc[:0]):
            with self.subTest(frame=frame), self.assertRaises(ValidationError):
                validate_bars(frame, "ATB")

    def test_invalid_numbers_and_ohlc(self):
        cases = [
            ("open", 0), ("close", -1), ("volume", -1),
            ("high", 3.0), ("low", 5.0), ("close", np.nan),
            ("open", np.inf), ("volume", np.inf), ("close", "invalid"),
        ]
        for column, value in cases:
            frame = bars()
            if isinstance(value, str):
                frame[column] = frame[column].astype(object)
            frame.loc[0, column] = value
            with self.subTest(column=column, value=value), self.assertRaises(ValidationError):
                validate_bars(frame, "ATB")

    def test_wrong_or_missing_symbol(self):
        for value in ("BIAT", None):
            frame = bars()
            frame.loc[0, "symbol"] = value
            with self.subTest(value=value), self.assertRaises(ValidationError):
                validate_bars(frame, "ATB")

    def test_unsorted_duplicate_intraday_and_timezone_dates(self):
        frames = [bars().iloc[::-1], pd.concat([bars(), bars().iloc[:1]])]
        intraday = bars()
        intraday.loc[0, "timestamp"] += pd.Timedelta(hours=1)
        frames.append(intraday)
        aware = bars()
        aware["timestamp"] = aware.timestamp.dt.tz_localize("UTC")
        frames.append(aware)
        missing = bars()
        missing.loc[0, "timestamp"] = pd.NaT
        frames.append(missing)
        for frame in frames:
            with self.subTest(frame=frame), self.assertRaises(ValidationError):
                validate_bars(frame, "ATB")

    def test_data_config_rejects_invalid_settings(self):
        for kwargs in (
            {"as_of": datetime(2026, 10, 10)}, {"as_of": date(2026, 10, 10), "max_age_days": -1},
            {"as_of": date(2026, 10, 10), "max_age_days": True},
            {"as_of": date(2026, 10, 10), "source": ""},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ValidationConfig(**kwargs)
        for value in ("20261010", "10/10/2026", "2026-02-30", "2026-10-10T12:00:00"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_cutoff(value)
        self.assertEqual(parse_cutoff("2026-10-10"), date(2026, 10, 10))


if __name__ == "__main__":
    unittest.main()
