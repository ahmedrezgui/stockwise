"""Offline tests for raw-to-processed publication and lineage."""
import contextlib
from datetime import date
from hashlib import sha256
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from market_data.cli import main
from market_data.manifest import write_bytes
from market_data.processing import normalize_bars, process_market, read_csv
from market_data.schemas import CANONICAL_COLUMNS, ValidationConfig, ValidationError

HEADER = "symbole;date;ouverture;haut;bas;cloture;volume"
FIRST = "ATB;01/10/2026;3,9;4,1;3,8;4;0"
SECOND = "ATB;05/10/2026;4;4,2;3,9;4,1;100"
CONFIG = ValidationConfig(as_of=date(2026, 10, 10))


class ProcessingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.raw = self.root / "raw"
        self.processed = self.root / "processed"
        self.raw.mkdir()
        self.content = f"{HEADER}\n{SECOND}\n{FIRST}\n{FIRST}\n".encode("utf-8-sig")
        (self.raw / "ATB.csv").write_bytes(self.content)

    def run_pipeline(self, symbols=None, config=CONFIG):
        return process_market(self.raw, self.processed, symbols or ["ATB"], config)

    def test_normalization_preserves_raw_and_records_lineage(self):
        result = self.run_pipeline()["symbols"]["ATB"]
        self.assertEqual((self.raw / "ATB.csv").read_bytes(), self.content)
        frame = pd.read_csv(self.processed / "ATB.csv")
        self.assertEqual(list(frame.columns), list(CANONICAL_COLUMNS))
        self.assertEqual(frame.timestamp.tolist(), ["2026-10-01", "2026-10-05"])
        self.assertEqual(frame.close.tolist(), [4.0, 4.1])
        self.assertEqual(result["duplicate_rows_removed"], 1)
        self.assertEqual(result["zero_volume_rows"], 1)
        self.assertEqual(result["raw_sha256"], sha256(self.content).hexdigest())
        self.assertEqual(result["processed_sha256"], sha256((self.processed / "ATB.csv").read_bytes()).hexdigest())

    def test_repeated_processing_is_byte_identical(self):
        self.run_pipeline()
        csv_before = (self.processed / "ATB.csv").read_bytes()
        manifest_before = (self.processed / "manifest.json").read_bytes()
        self.run_pipeline()
        self.assertEqual((self.processed / "ATB.csv").read_bytes(), csv_before)
        self.assertEqual((self.processed / "manifest.json").read_bytes(), manifest_before)

    def test_cutoff_excludes_current_and_future_observations_without_filling(self):
        content = self.content + b"ATB;10/10/2026;;;;;\nATB;11/10/2026;5;6;4;5;20\n"
        result = normalize_bars(read_csv(content), "ATB", CONFIG)
        self.assertEqual(result.quality["excluded_by_date"], 2)
        self.assertEqual(result.frame.timestamp.dt.day.tolist(), [1, 5])

    def test_staleness_is_valid_and_reported(self):
        result = self.run_pipeline(config=ValidationConfig(as_of=date(2026, 11, 10)))["symbols"]["ATB"]
        self.assertEqual(result["status"], "valid")
        self.assertEqual(result["freshness"], "stale")
        self.assertTrue(result["warnings"])

    def test_conflicting_dates_are_rejected(self):
        content = self.content + f"{FIRST.replace(';4;0', ';3,95;0')}\n".encode()
        with self.assertRaisesRegex(ValidationError, "Conflicting"):
            normalize_bars(read_csv(content), "ATB", CONFIG)

    def test_invalid_dates_symbols_columns_and_rows(self):
        payloads = [
            self.content.decode("utf-8-sig").replace("01/10/2026", "2026-10-01T12:00:00"),
            self.content.decode("utf-8-sig").replace("01/10/2026", "31/02/2026"),
            self.content.decode("utf-8-sig").replace("ATB", "OTHER"),
            "symbol,symbol\nATB,ATB\n",
            f"{HEADER};timestamp\n{FIRST};2026-10-01\n",
            f"{HEADER}\nATB;missing\n",
            "symbol,timestamp\nATB,2026-10-01\n",
            "",
        ]
        for payload in payloads:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                normalize_bars(read_csv(payload.encode()), "ATB", CONFIG)

    def test_no_completed_rows_is_invalid(self):
        with self.assertRaisesRegex(ValidationError, "No completed"):
            normalize_bars(read_csv(self.content), "ATB", ValidationConfig(as_of=date(2026, 1, 1)))

    def test_invalidated_output_is_archived_and_other_symbols_continue(self):
        self.run_pipeline()
        previous = (self.processed / "ATB.csv").read_bytes()
        (self.raw / "ATB.csv").write_text(f"{HEADER}\n{FIRST.replace(';4;0', ';0;0')}\n")
        (self.raw / "BIAT.csv").write_bytes(self.content.replace(b"ATB", b"BIAT"))
        result = self.run_pipeline(["ATB", "BIAT", "MISSING"])
        self.assertEqual(result["symbols"]["ATB"]["status"], "invalid")
        self.assertEqual(result["symbols"]["BIAT"]["status"], "valid")
        self.assertEqual(result["symbols"]["MISSING"]["status"], "invalid")
        self.assertNotIn("processed_sha256", result["symbols"]["ATB"])
        self.assertFalse((self.processed / "ATB.csv").exists())
        archive = self.root / "archive" / "processed" / f"ATB.{sha256(previous).hexdigest()}.csv"
        self.assertEqual(archive.read_bytes(), previous)

    def test_missing_source_invalidates_previous_output(self):
        self.run_pipeline()
        (self.raw / "ATB.csv").unlink()
        result = self.run_pipeline()["symbols"]["ATB"]
        self.assertEqual(result["status"], "invalid")
        self.assertFalse((self.processed / "ATB.csv").exists())

    def test_path_overlap_is_rejected(self):
        for destination in (self.raw, self.raw / "processed", self.root):
            with self.subTest(destination=destination), self.assertRaisesRegex(ValueError, "non-nested"):
                process_market(self.raw, destination, ["ATB"], CONFIG)

    def test_archive_cannot_overlap_raw_directory(self):
        raw_archive = self.root / "archive"
        raw_archive.mkdir()
        with self.assertRaisesRegex(ValueError, "Archive"):
            process_market(raw_archive, self.processed, ["ATB"], CONFIG)

    def test_atomic_write_failure_preserves_old_content(self):
        output = self.root / "output.csv"
        output.write_bytes(b"previous")
        with patch.object(Path, "replace", side_effect=OSError("write blocked")):
            with self.assertRaises(OSError):
                write_bytes(output, b"replacement")
        self.assertEqual(output.read_bytes(), b"previous")
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_canonical_csv_can_be_processed_again(self):
        self.run_pipeline()
        result = normalize_bars(read_csv((self.processed / "ATB.csv").read_bytes()), "ATB", CONFIG)
        self.assertEqual(result.frame.close.tolist(), [4.0, 4.1])

    def test_cli_process_is_separate_and_returns_failure_for_invalid_symbols(self):
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps({
            "symbols": ["ATB"], "raw_dir": str(self.raw), "processed_dir": str(self.processed),
        }))
        args = ["process", "--config", str(config_path), "--as-of", "2026-10-10"]
        with patch("market_data.cli.download_history") as download, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(args), 0)
            self.assertEqual(main(args + ["--symbols", "ATB", "MISSING"]), 1)
            download.assert_not_called()

    def test_cli_invalid_cutoff_and_missing_output_setting(self):
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps({
            "symbols": ["ATB"], "raw_dir": str(self.raw), "processed_dir": str(self.processed),
        }))
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as exc:
                main(["process", "--config", str(config_path), "--as-of", "not-a-date"])
            self.assertEqual(exc.exception.code, 2)
        config_path.write_text(json.dumps({"symbols": ["ATB"], "raw_dir": str(self.raw)}))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            main(["process", "--config", str(config_path)])


if __name__ == "__main__":
    unittest.main()
