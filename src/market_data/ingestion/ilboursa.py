"""Ilboursa raw-history downloader; paths and symbols are supplied by the caller."""

import csv
from datetime import date, timedelta
from pathlib import Path
import sys
import tempfile
import time

from ..schemas import validate_symbols


DOWNLOAD_BASE_URL = "https://www.ilboursa.com/marches/download/"
EXPECTED_COLUMNS = ["symbole", "date", "ouverture", "haut", "bas", "cloture", "volume"]


def date_ranges(start, end):
    """Yield inclusive ranges of at most 84 days, never exceeding end."""
    while start <= end:
        stop = min(start + timedelta(days=83), end)
        yield start, stop
        start = stop + timedelta(days=1)


def append_csv(content, destination, symbol):
    """Merge provider rows atomically; conflicting dates are left for later validation."""
    rows = list(csv.reader(content.decode("utf-8-sig").splitlines(), delimiter=";"))
    if not rows or rows[0] != EXPECTED_COLUMNS:
        raise RuntimeError(f"{symbol}: expected stock CSV, received another response. No data saved.")
    records = [row for row in rows[1:] if row]
    if any(len(row) != len(EXPECTED_COLUMNS) or row[0] != symbol for row in records):
        raise RuntimeError(f"{symbol}: unexpected CSV columns or stock symbol. No data saved.")

    destination = Path(destination)
    existing_rows = []
    has_header = destination.exists() and destination.stat().st_size > 0
    if has_header:
        with destination.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle, delimiter=";")
            if next(reader, None) != EXPECTED_COLUMNS:
                raise RuntimeError(f"Existing file has an unexpected header: {destination}")
            existing_rows = [row for row in reader if row]
        if any(len(row) != len(EXPECTED_COLUMNS) or row[0] != symbol for row in existing_rows):
            raise RuntimeError(f"Existing file contains unexpected columns or symbols: {destination}")
    existing = {tuple(row) for row in existing_rows}
    new_rows = []
    for row in records:
        if tuple(row) not in existing:
            new_rows.append(row)
            existing.add(tuple(row))
    if has_header and not new_rows:
        return 0

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=destination.parent,
            prefix=destination.name + ".", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            writer = csv.writer(handle, delimiter=";", lineterminator="\n")
            writer.writerow(EXPECTED_COLUMNS)
            writer.writerows(existing_rows)
            writer.writerows(new_rows)
        temporary.replace(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return len(new_rows)


def open_form(page, url, interactive=True):
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    page.goto(url, wait_until='domcontentloaded', timeout=60000)
    form = page.locator('form').filter(has=page.locator('input[name="dtFrom"]')).first
    try:
        form.locator('input[name="dtFrom"]').wait_for(state='visible', timeout=15000)
    except PlaywrightTimeoutError as error:
        if not interactive:
            raise RuntimeError(f'Download form unavailable at {url}. Retry in visible browser mode.') from error
        print(f'Form unavailable at {url}. Browser page: {page.title()}', flush=True)
        try:
            input('Complete any verification/login in the browser, then press Enter here. Ctrl+C cancels: ')
        except EOFError as input_error:
            raise RuntimeError('Manual browser interaction requires an interactive terminal.') from input_error
        try:
            form.locator('input[name="dtFrom"]').wait_for(state='visible', timeout=15000)
        except PlaywrightTimeoutError as retry_error:
            raise RuntimeError(
                f'The form is still unavailable at {url}. The site may be blocking this browser, '
                'or the form has changed. No download was submitted.'
            ) from retry_error
    return form


def download(page, form, start, end, destination, symbol):
    for name, value in [('dtFrom', start), ('dtTo', end)]:
        field = form.locator(f'input[name="{name}"]')
        # HTML date controls require ISO dates; text controls use the site's French format.
        formatted = value.isoformat() if field.get_attribute('type') == 'date' else value.strftime('%d/%m/%Y')
        field.fill(formatted)
    with page.expect_download(timeout=60000) as pending:
        form.get_by_role('button', name='Télécharger', exact=True).click()
    result = pending.value
    failure = result.failure()
    if failure:
        raise RuntimeError(f'{symbol}: browser download failed: {failure}')
    try:
        return append_csv(Path(result.path()).read_bytes(), destination, symbol)
    finally:
        result.delete()


def download_history(
    symbols: list[str],
    output_dir: Path,
    start: date,
    end: date,
    browser: str = "chrome",
    headless: bool = False,
):
    """Download inclusive date windows; fail the run if any window was incomplete."""
    symbols = validate_symbols(symbols)
    output_dir = Path(output_dir)
    if start > end:
        raise ValueError("Start date must not be after end date")
    if browser not in {"chrome", "msedge", "chromium"}:
        raise ValueError("Unsupported browser")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Playwright is missing. Install it with: python -m pip install playwright") from exc

    with sync_playwright() as playwright:
        options = {'headless': headless}
        if browser != 'chromium':
            options['channel'] = browser
        browser = playwright.chromium.launch(**options)
        try:
            context = browser.new_context(accept_downloads=True, locale='fr-FR')
            page = context.new_page()
            failed_periods = []
            completed_periods = 0
            for symbol in symbols:
                url = f'{DOWNLOAD_BASE_URL}{symbol}'
                for period_start, period_end in date_ranges(start, end):
                    try:
                        form = open_form(page, url, interactive=not headless)
                        count = download(page, form, period_start, period_end,
                                         output_dir / f'{symbol}.csv', symbol)
                    except Exception as error:
                        failure = f'{symbol}: {period_start} to {period_end}: {error}'
                        failed_periods.append(failure)
                        print(f'Skipping failed period — {failure}', file=sys.stderr, flush=True)
                    else:
                        completed_periods += 1
                        print(f'{symbol}: {period_start} to {period_end}: saved {count} new rows', flush=True)
                    time.sleep(2)  # Space out requests across ranges and symbols.
            print(f'Finished: {completed_periods} periods completed, {len(failed_periods)} periods skipped.', flush=True)
            if failed_periods:
                print('Failed periods:', file=sys.stderr)
                for failure in failed_periods:
                    print(f'  {failure}', file=sys.stderr)
                raise RuntimeError('Some download periods failed; do not publish incomplete refreshed data.')
        finally:
            browser.close()

