"""Command-line entry points for downloading and processing market data."""

import argparse
from datetime import date, datetime
import json
from pathlib import Path
import sys

from .ingestion.ilboursa import download_history
from .schemas import ValidationConfig, parse_cutoff, validate_symbols


def read_config(path: Path) -> dict:
    """Read settings; relative paths are relative to the working directory."""
    with Path(path).open(encoding="utf-8-sig") as stream:
        config = json.load(stream)
    if not isinstance(config, dict):
        raise ValueError("Configuration must be a JSON object")
    return config


def _path(config: dict, name: str) -> Path:
    value = config.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty path string")
    return Path(value)


def load_config(path: Path) -> tuple[list[str], Path]:
    """Read ingestion settings without requiring processing configuration."""
    config = read_config(path)
    return validate_symbols(config.get("symbols")), _path(config, "raw_dir")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # Preserve the original --date ... download invocation.
    if argv and argv[0].startswith("-") and argv[0] not in {"-h", "--help"}:
        argv.insert(0, "download")
    parser = argparse.ArgumentParser(description="Download and validate daily market data.")
    commands = parser.add_subparsers(dest="command", required=True)
    download_parser = commands.add_parser("download", help="Download raw Ilboursa history")
    process_parser = commands.add_parser("process", help="Validate raw files and write canonical processed data")
    for command in (download_parser, process_parser):
        command.add_argument("--config", type=Path, default=Path("configs/market_data.json"))
        command.add_argument("--symbols", nargs="+", help="Override the configured symbols")
        command.add_argument("--output-dir", type=Path, help="Override the configured output directory")
    download_parser.add_argument("--date", required=True, help="Start date: dd-mm-yyyy or all (since 2010)")
    download_parser.add_argument("--browser", choices=["chrome", "msedge", "chromium"], default="chrome")
    download_parser.add_argument("--headless", action="store_true", help="Run without manual browser verification")
    process_parser.add_argument("--as-of", help="Exclusive YYYY-MM-DD cutoff; defaults to today's UTC date")
    process_parser.add_argument("--max-age-days", type=int, help="Freshness threshold; defaults to config or 14")
    args = parser.parse_args(argv)
    try:
        config = read_config(args.config)
        symbols = validate_symbols(args.symbols if args.symbols is not None else config.get("symbols"))
        raw_dir = _path(config, "raw_dir")
        if args.command == "process":
            output_dir = args.output_dir if args.output_dir is not None else _path(config, "processed_dir")
            validation = ValidationConfig(
                as_of=parse_cutoff(args.as_of if args.as_of is not None else config.get("as_of")),
                max_age_days=args.max_age_days if args.max_age_days is not None else config.get("max_age_days", 14),
                source=config.get("source", "ilboursa"),
            )
        else:
            output_dir = args.output_dir if args.output_dir is not None else raw_dir
    except (OSError, ValueError) as exc:
        parser.error(f"Invalid market-data configuration: {exc}")
    if args.command == "download":
        try:
            start = date(2010, 1, 1) if args.date.lower() == "all" else datetime.strptime(args.date, "%d-%m-%Y").date()
        except ValueError:
            parser.error("--date must be dd-mm-yyyy or all")
        end = date.today()
        if start > end:
            parser.error("--date must not be in the future")
    try:
        if args.command == "download":
            download_history(symbols=symbols, output_dir=output_dir, start=start, end=end,
                             browser=args.browser, headless=args.headless)
            return 0
        from .processing import process_market

        manifest = process_market(raw_dir, output_dir, symbols, validation)
        for symbol, entry in manifest["symbols"].items():
            detail = entry.get("error", f"{entry.get('rows', 0)} rows; {entry.get('freshness', '')}")
            print(f"{symbol}: {entry['status']} - {detail}")
        print(f"Manifest: {output_dir / 'manifest.json'}")
        return int(any(entry["status"] != "valid" for entry in manifest["symbols"].values()))
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"{args.command.capitalize()} failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
