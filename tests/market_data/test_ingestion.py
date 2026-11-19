"""Offline ingestion checks: no requests to Ilboursa."""
import contextlib
from datetime import date
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from market_data.cli import load_config, main
from market_data.ingestion import ilboursa

HEADER = ";".join(ilboursa.EXPECTED_COLUMNS)
ROW = "ATB;01/10/2026;3,9;4;3,8;3,95;100"


class IngestionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "market_data.json"
        self.config.write_text(json.dumps({"symbols": ["atb", "BIAT", "ATB"], "raw_dir": "data/raw"}))

    def test_config_preserves_cwd_relative_paths_and_normalizes_symbols(self):
        symbols, output = load_config(self.config)
        self.assertEqual(symbols, ["ATB", "BIAT"])
        self.assertEqual(output, Path("data/raw"))

    def test_invalid_config_is_rejected_before_browser_start(self):
        invalid = [[], {}, {"symbols": []}, {"symbols": ["../escape"], "raw_dir": "data/raw"},
                   {"symbols": "ATB", "raw_dir": "data/raw"},
                   {"symbols": [1], "raw_dir": "data/raw"},
                   {"symbols": ["ATB"], "raw_dir": ""}]
        for value in invalid:
            with self.subTest(value=value):
                self.config.write_text(json.dumps(value))
                with patch("market_data.cli.download_history") as download, contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as error:
                        main(["--config", str(self.config), "--date", "01-10-2026"])
                self.assertEqual(error.exception.code, 2)
                download.assert_not_called()

    def test_cli_passes_config_and_overrides_to_downloader(self):
        with patch("market_data.cli.download_history") as download:
            self.assertEqual(main(["--config", str(self.config), "--date", "01-10-2026"]), 0)
            self.assertEqual(download.call_args.kwargs["symbols"], ["ATB", "BIAT"])
            self.assertEqual(download.call_args.kwargs["output_dir"], Path("data/raw"))
            self.assertEqual(main(["--config", str(self.config), "--date", "all", "--symbols", "ARTES",
                                   "--output-dir", str(self.root / "raw"), "--headless",
                                   "--browser", "chromium"]), 0)
            self.assertEqual(download.call_args.kwargs["symbols"], ["ARTES"])
            self.assertEqual(download.call_args.kwargs["start"], date(2010, 1, 1))
            self.assertEqual(download.call_args.kwargs["output_dir"], self.root / "raw")
            self.assertTrue(download.call_args.kwargs["headless"])

    def test_cli_failure_is_nonzero(self):
        with patch("market_data.cli.download_history", side_effect=RuntimeError("download failed")):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(["--config", str(self.config), "--date", "01-10-2026"]), 1)

    def test_date_ranges_are_contiguous_and_bounded(self):
        periods = list(ilboursa.date_ranges(date(2024, 1, 1), date(2024, 7, 1)))
        self.assertEqual(periods[0][0], date(2024, 1, 1))
        self.assertEqual(periods[-1][1], date(2024, 7, 1))
        self.assertTrue(all((end - start).days <= 83 for start, end in periods))
        self.assertTrue(all((right[0] - left[1]).days == 1 for left, right in zip(periods, periods[1:])))

    def test_csv_deduplication_bom_and_missing_final_newline(self):
        output = self.root / "ATB.csv"
        output.write_text(HEADER, encoding="utf-8")
        payload = f"{HEADER}\n{ROW}\n{ROW}\n".encode("utf-8-sig")
        self.assertEqual(ilboursa.append_csv(payload, output, "ATB"), 1)
        before = output.read_bytes()
        self.assertEqual(ilboursa.append_csv(payload, output, "ATB"), 0)
        self.assertEqual(output.read_bytes(), before)
        self.assertEqual(output.read_text(encoding="utf-8"), f"{HEADER}\n{ROW}\n")

    def test_invalid_download_preserves_existing_file(self):
        output = self.root / "ATB.csv"
        output.write_text(f"{HEADER}\n{ROW}\n", encoding="utf-8")
        before = output.read_bytes()
        for payload in [b"<html>Blocked</html>", f"{HEADER}\nATB;missing".encode(),
                        f"{HEADER}\n{ROW.replace('ATB', 'OTHER')}".encode()]:
            with self.subTest(payload=payload), self.assertRaises(RuntimeError):
                ilboursa.append_csv(payload, output, "ATB")
            self.assertEqual(output.read_bytes(), before)

    def test_failed_replacement_preserves_file_and_cleans_temporary(self):
        output = self.root / "ATB.csv"
        output.write_text(HEADER, encoding="utf-8")
        with patch.object(Path, "replace", side_effect=OSError("write blocked")):
            with self.assertRaises(OSError):
                ilboursa.append_csv(f"{HEADER}\n{ROW}\n".encode(), output, "ATB")
        self.assertEqual(output.read_text(encoding="utf-8"), HEADER)
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_failed_periods_continue_and_browser_closes(self):
        manager = MagicMock()
        browser = manager.__enter__.return_value.chromium.launch.return_value
        with patch("playwright.sync_api.sync_playwright", return_value=manager):
            with patch.object(ilboursa, "open_form"), patch.object(ilboursa.time, "sleep"):
                with patch.object(ilboursa, "download", side_effect=[RuntimeError("failed"), 1, 1, 1]) as download:
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        with self.assertRaisesRegex(RuntimeError, "Some download periods failed"):
                            ilboursa.download_history(["ATB", "BIAT"], self.root, date(2024, 1, 1),
                                                     date(2024, 4, 1), headless=True)
                    self.assertEqual(download.call_count, 4)
                    browser.close.assert_called_once()

    def test_keyboard_interrupt_closes_browser(self):
        manager = MagicMock()
        browser = manager.__enter__.return_value.chromium.launch.return_value
        with patch("playwright.sync_api.sync_playwright", return_value=manager):
            with patch.object(ilboursa, "open_form", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    ilboursa.download_history(["ATB"], self.root, date(2024, 1, 1), date(2024, 1, 1))
        browser.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
