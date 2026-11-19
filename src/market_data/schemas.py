"""Data-only contracts shared by ingestion, validation and processing."""

from dataclasses import dataclass
from datetime import date, datetime, timezone
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd

CANONICAL_COLUMNS = ("symbol", "timestamp", "open", "high", "low", "close", "volume")
PRICE_COLUMNS = ("open", "high", "low", "close")
COLUMN_ALIASES = {
    "symbole": "symbol",
    "date": "timestamp",
    "ouverture": "open",
    "haut": "high",
    "bas": "low",
    "cloture": "close",
}
PROCESSING_VERSION = "canonical-daily-v1"


class ValidationError(ValueError):
    """A source cannot be published as a canonical daily-bar dataset."""


def validate_symbols(symbols) -> list[str]:
    """Normalize a nonempty symbol list and reject unsafe URL/file components."""
    if not isinstance(symbols, list) or not symbols:
        raise ValueError("symbols must be a nonempty list of stock symbols")
    if any(not isinstance(symbol, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,19}", symbol)
           for symbol in symbols):
        raise ValueError("Each symbol must contain 1-20 letters, digits, dots, underscores or hyphens, "
                         "starting with a letter or digit")
    return list(dict.fromkeys(symbol.upper() for symbol in symbols))


def parse_cutoff(value: str | None) -> date:
    """Return an exclusive session-date cutoff; default to today's UTC date."""
    if value is None:
        return datetime.now(timezone.utc).date()
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("as_of must be a date formatted YYYY-MM-DD")
    return date.fromisoformat(value)


@dataclass(frozen=True)
class ValidationConfig:
    as_of: date
    max_age_days: int = 14
    source: str = "ilboursa"

    def __post_init__(self):
        if type(self.as_of) is not date:
            raise ValueError("as_of must be a date, not an intraday timestamp")
        if type(self.max_age_days) is not int or self.max_age_days < 0:
            raise ValueError("max_age_days must be a nonnegative integer")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("source must be a nonempty string")


@dataclass
class ValidationResult:
    frame: "pd.DataFrame"
    quality: dict
