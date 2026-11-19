"""Convert provider CSV snapshots into validated, unscaled canonical bars."""

import csv
from datetime import datetime
import io
from pathlib import Path
import re

import pandas as pd

from .manifest import digest, write_bytes, write_manifest
from .schemas import (
    CANONICAL_COLUMNS, COLUMN_ALIASES, PROCESSING_VERSION,
    ValidationConfig, ValidationError, ValidationResult, validate_symbols,
)
from .validation import validate_bars


def read_csv(content: bytes) -> pd.DataFrame:
    """Parse provider semicolon CSV or canonical comma CSV without type inference."""
    text = content.decode("utf-8-sig")
    first_line = text.splitlines()[0] if text.splitlines() else ""
    delimiter = ";" if ";" in first_line else ","
    try:
        rows = list(csv.reader(io.StringIO(text), delimiter=delimiter, strict=True))
    except csv.Error as exc:
        raise ValidationError(f"Malformed CSV: {exc}") from exc
    rows = [row for row in rows if row]
    if not rows:
        raise ValidationError("Empty CSV")
    headers = [name.strip().lower() for name in rows[0]]
    if len(set(headers)) != len(headers):
        raise ValidationError("Duplicate CSV column names")
    if any(len(row) != len(headers) for row in rows[1:]):
        raise ValidationError("CSV row has an unexpected number of columns")
    return pd.DataFrame(rows[1:], columns=headers)


def _session_date(value):
    """Accept only explicit ISO dates or the provider's day/month/year dates."""
    value = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        pattern = "%Y-%m-%d"
    elif re.fullmatch(r"\d{2}/\d{2}/\d{4}", value):
        pattern = "%d/%m/%Y"
    else:
        raise ValidationError(f"Invalid session date: {value!r}; expected YYYY-MM-DD or DD/MM/YYYY")
    try:
        return datetime.strptime(value, pattern)
    except ValueError as exc:
        raise ValidationError(f"Invalid session date: {value!r}") from exc


def normalize_bars(raw: pd.DataFrame, symbol: str, config: ValidationConfig) -> ValidationResult:
    """Normalize representations, filter the cutoff, then validate observed bars."""
    symbol = validate_symbols([symbol])[0]
    frame = raw.copy()
    frame.columns = [COLUMN_ALIASES.get(str(name).strip().lower(), str(name).strip().lower()) for name in frame.columns]
    if not frame.columns.is_unique:
        raise ValidationError("Multiple source columns map to the same canonical column")
    missing = set(CANONICAL_COLUMNS) - set(frame.columns)
    if missing:
        raise ValidationError(f"Missing columns: {', '.join(sorted(missing))}")
    frame = frame[list(CANONICAL_COLUMNS)].copy()
    frame["symbol"] = frame.symbol.astype("string").str.strip().str.upper()
    if frame.symbol.isna().any() or not frame.symbol.eq(symbol).all():
        raise ValidationError("Dataset must contain exactly the requested symbol")
    frame["timestamp"] = pd.to_datetime(frame.timestamp.map(_session_date))
    retained = frame.timestamp < pd.Timestamp(config.as_of)
    excluded = int((~retained).sum())
    frame = frame.loc[retained].copy()
    for column in CANONICAL_COLUMNS[2:]:
        values = frame[column].astype("string").str.strip().str.replace(",", ".", regex=False)
        frame[column] = pd.to_numeric(values, errors="coerce").astype(float)
    duplicates = int(frame.duplicated().sum())
    frame = frame.drop_duplicates().sort_values("timestamp").reset_index(drop=True)
    validate_bars(frame, symbol)
    age_days = (config.as_of - frame.timestamp.iloc[-1].date()).days
    quality = {
        "input_rows": len(raw),
        "rows": len(frame),
        "excluded_by_date": excluded,
        "duplicate_rows_removed": duplicates,
        "start": frame.timestamp.iloc[0].date().isoformat(),
        "end": frame.timestamp.iloc[-1].date().isoformat(),
        "age_days": age_days,
        "freshness": "stale" if age_days > config.max_age_days else "fresh",
        "zero_volume_rows": int(frame.volume.eq(0).sum()),
        "warnings": ["Latest observation exceeds max_age_days"] if age_days > config.max_age_days else [],
    }
    return ValidationResult(frame, quality)


def _archive_output(path: Path) -> None:
    """Keep invalidated old output outside the current processed-data directory."""
    if not path.exists():
        return
    content = path.read_bytes()
    archive = path.parent.parent / "archive" / path.parent.name / f"{path.stem}.{digest(content)}.csv"
    archive.parent.mkdir(parents=True, exist_ok=True)
    if archive.exists():
        if archive.read_bytes() != content:
            raise OSError("Archive checksum collision")
        path.unlink()
    else:
        path.replace(archive)


def process_market(
    raw_dir: Path,
    processed_dir: Path,
    symbols: list[str],
    config: ValidationConfig,
) -> dict:
    """Process each requested symbol; publish a manifest even when some inputs fail."""
    raw_dir, processed_dir = Path(raw_dir), Path(processed_dir)
    symbols = sorted(validate_symbols(symbols))
    raw_root, processed_root = raw_dir.resolve(), processed_dir.resolve()
    if raw_root == processed_root or raw_root in processed_root.parents or processed_root in raw_root.parents:
        raise ValueError("Raw and processed directories must be separate, non-nested directories")
    archive_root = (processed_dir.parent / "archive" / processed_dir.name).resolve()
    if archive_root == raw_root or raw_root in archive_root.parents or archive_root in raw_root.parents:
        raise ValueError("Archive storage must not overlap raw data")
    processed_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1,
        "processing_version": PROCESSING_VERSION,
        "as_of": config.as_of.isoformat(),
        "max_age_days": config.max_age_days,
        "source": config.source,
        "price_adjustment": "unverified",
        "calendar_verified": False,
        "date_convention": "exchange-local session date; dates on or after as_of excluded",
        "requested_symbols": symbols,
        "symbols": {},
    }
    for symbol in symbols:
        source, destination = raw_dir / f"{symbol}.csv", processed_dir / f"{symbol}.csv"
        entry = {"status": "invalid", "raw_file": source.name}
        manifest["symbols"][symbol] = entry
        try:
            content = source.read_bytes()
            entry["raw_sha256"] = digest(content)
            result = normalize_bars(read_csv(content), symbol, config)
            canonical = result.frame.to_csv(index=False, date_format="%Y-%m-%d", lineterminator="\n").encode("utf-8")
            write_bytes(destination, canonical)
            entry.update(
                result.quality,
                status="valid",
                processed_file=destination.name,
                processed_sha256=digest(canonical),
            )
        except (OSError, ValueError, UnicodeError) as exc:
            entry["error"] = str(exc)
            _archive_output(destination)
    write_manifest(processed_dir / "manifest.json", manifest)
    return manifest
