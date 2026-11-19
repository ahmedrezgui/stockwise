"""Command-line entry point for raw market-data ingestion."""

import argparse
from datetime import date, datetime
import json
from pathlib import Path
import sys

from .ingestion.ilboursa import download_history, validate_symbols


def load_config(path: Path) -> tuple[list[str], Path]:
    """Read ingestion settings; relative paths are relative to the working directory."""
    with Path(path).open(encoding="utf-8-sig") as stream:
        config = json.load(stream)
    if not isinstance(config, dict):
        raise ValueError("Configuration must be a JSON object")
    symbols = validate_symbols(config.get("symbols"))
    raw_dir = config.get("raw_dir")
    if not isinstance(raw_dir, str) or not raw_dir.strip():
        raise ValueError("raw_dir must be a nonempty path string")
    return symbols, Path(raw_dir)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Download raw Ilboursa stock history.")
    parser.add_argument("--config", type=Path, default=Path("configs/market_data.json"))
    parser.add_argument("--date", required=True, help="Start date: dd-mm-yyyy or all (since 2010)")
    parser.add_argument("--symbols", nargs="+", help="Override the symbols in the configuration")
    parser.add_argument("--output-dir", type=Path, help="Override raw_dir from the configuration")
    parser.add_argument("--browser", choices=["chrome", "msedge", "chromium"], default="chrome")
    parser.add_argument("--headless", action="store_true", help="Run without manual browser verification")
    args = parser.parse_args(argv)
    try:
        symbols, output_dir = load_config(args.config)
        if args.symbols is not None:
            symbols = validate_symbols(args.symbols)
    except (OSError, ValueError) as exc:
        parser.error(f"Invalid ingestion configuration: {exc}")
    try:
        start = date(2010, 1, 1) if args.date.lower() == "all" else datetime.strptime(args.date, "%d-%m-%Y").date()
    except ValueError:
        parser.error("--date must be dd-mm-yyyy or all")
    end = date.today()
    if start > end:
        parser.error("--date must not be in the future")
    try:
        download_history(
            symbols=symbols,
            output_dir=args.output_dir if args.output_dir is not None else output_dir,
            start=start,
            end=end,
            browser=args.browser,
            headless=args.headless,
        )
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Ingestion failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
