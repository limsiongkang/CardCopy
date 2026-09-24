"""
Google Sheets output.

Authentication uses a *service account* rather than a normal Google login.
A service account is a robot account with its own email address; you share
the spreadsheet with that address exactly as you would with a colleague.
This matters because a scheduled 7am job has nobody sitting there to click
through a browser consent screen, and service-account keys do not expire
the way an interactive login token does.
"""

import gspread
from google.oauth2.service_account import Credentials

import config

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

RESULTS_HEADER = [
    "date", "competitor", "product", "price", "sale price", "in stock", "URL",
]
ALERTS_HEADER = [
    "date", "competitor", "product", "change", "was", "now", "URL",
]


def _client():
    creds = Credentials.from_service_account_file(
        str(config.GOOGLE_CREDENTIALS_FILE), scopes=SCOPES
    )
    return gspread.authorize(creds)


def _service_account_email():
    """The robot account's address, for sharing a sheet with it."""
    import json
    try:
        with open(config.GOOGLE_CREDENTIALS_FILE, encoding="utf-8") as handle:
            return json.load(handle).get("client_email", "(unknown)")
    except Exception:
        return "(could not read the key file)"


def _ensure_tab(spreadsheet, title, header):
    """Return the named worksheet, creating it with its header if absent."""
    try:
        tab = spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        tab = spreadsheet.add_worksheet(title=title, rows=1000, cols=len(header))
        tab.append_row(header, value_input_option="USER_ENTERED")
        return tab

    # Tab exists; make sure row 1 is actually the header.
    existing = tab.row_values(1)
    if [c.strip().lower() for c in existing] != [c.lower() for c in header]:
        if not existing:
            tab.append_row(header, value_input_option="USER_ENTERED")
    return tab


def open_workbook():
    """
    Open the spreadsheet, creating it on first run.

    Returns (spreadsheet, results_tab, alerts_tab).
    """
    client = _client()
    try:
        spreadsheet = client.open(config.SHEET_NAME)
        created = False
    except gspread.SpreadsheetNotFound:
        try:
            spreadsheet = client.create(config.SHEET_NAME)
            created = True
        except gspread.exceptions.APIError as exc:
            # A service account has no Drive storage of its own, so creating a
            # file fails with a storage-quota error that reads as though your
            # own Drive were full. It is not. The fix is for a human account
            # to own the file and share it with the service account.
            if "quota" in str(exc).lower():
                raise RuntimeError(
                    f"The spreadsheet '{config.SHEET_NAME}' does not exist yet, and "
                    f"the service account cannot create it: a service account has no "
                    f"Drive storage of its own.\n\n"
                    f"To fix this, once:\n"
                    f"  1. Go to https://sheets.new and create a blank spreadsheet\n"
                    f"  2. Name it exactly: {config.SHEET_NAME}\n"
                    f"  3. Click Share, and share it as Editor with:\n"
                    f"     {_service_account_email()}\n"
                ) from exc
            raise

    results = _ensure_tab(spreadsheet, config.RESULTS_TAB, RESULTS_HEADER)
    alerts = _ensure_tab(spreadsheet, config.ALERTS_TAB, ALERTS_HEADER)

    if created:
        # A brand-new spreadsheet has a leftover empty "Sheet1"; remove it.
        for sheet in spreadsheet.worksheets():
            if sheet.title not in (config.RESULTS_TAB, config.ALERTS_TAB):
                try:
                    spreadsheet.del_worksheet(sheet)
                except Exception:
                    pass

        # The service account created this file, so the service account owns
        # it - and a service account has no Drive anyone can browse. Without
        # this the sheet would exist but be invisible to you, so hand over
        # edit access immediately.
        if config.ALERT_RECIPIENT:
            share_with(spreadsheet, config.ALERT_RECIPIENT)

    return spreadsheet, results, alerts


def append_rows(tab, rows):
    """Append rows in one API call. Silently does nothing for an empty list."""
    if not rows:
        return
    tab.append_rows(rows, value_input_option="USER_ENTERED")


def share_with(spreadsheet, email):
    """Give a human account edit access to a sheet the robot account owns."""
    try:
        spreadsheet.share(email, perm_type="user", role="writer", notify=False)
        return True
    except Exception:
        return False
