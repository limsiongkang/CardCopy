"""
Central configuration. Everything the other files need to know about paths,
credentials and settings lives here, so there is exactly one place to change.

Nothing secret is written in this file. Secrets are read from .env at runtime.
"""

import os
from pathlib import Path
from urllib.parse import unquote

from dotenv import load_dotenv

# ---------------------------------------------------------------- paths
PROJECT_DIR = Path(__file__).resolve().parent

# Where secrets live. Defaults to the project folder, but can be pointed
# somewhere outside OneDrive by setting CARDCOPY_SECRETS_DIR in the environment.
SECRETS_DIR = Path(os.getenv("CARDCOPY_SECRETS_DIR", PROJECT_DIR))

DATA_DIR = PROJECT_DIR / "data"
LOG_DIR = PROJECT_DIR / "logs"
COMPETITORS_FILE = PROJECT_DIR / "competitors.txt"
SELECTORS_FILE = PROJECT_DIR / "selectors.json"
SNAPSHOT_FILE = DATA_DIR / "last_run.json"

# Scrapling's adaptive-selector memory (an SQLite file it manages itself).
ADAPTIVE_STORAGE = DATA_DIR / "adaptive_selectors.db"

for _d in (DATA_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

load_dotenv(SECRETS_DIR / ".env")


# ---------------------------------------------------------------- proxy
def proxy_config():
    """
    Build the proxy argument for Scrapling from .env.

    Returns None when no proxy is configured, which makes the fetcher use the
    machine's normal connection.

    Providers hand out credentials in several shapes, so all of these work:

        PROXY_SERVER=http://gate.provider.com:7000   (+ USERNAME / PASSWORD)
        PROXY_SERVER=gate.provider.com:7000          (+ USERNAME / PASSWORD)
        PROXY_SERVER=http://user:pass@gate.provider.com:7000
        PROXY_SERVER=user:pass@gate.provider.com:7000

    Scrapling accepts a URL string or a dict of {server, username, password}.
    The dict form is used so the password never sits inside a URL that might
    end up in a log line.
    """
    server = os.getenv("PROXY_SERVER", "").strip()
    if not server:
        return None

    username = os.getenv("PROXY_USERNAME", "").strip()
    password = os.getenv("PROXY_PASSWORD", "").strip()

    scheme = "http://"
    for known in ("http://", "https://", "socks5://", "socks5h://", "socks4://"):
        if server.lower().startswith(known):
            scheme, server = known, server[len(known):]
            break

    # Split out credentials if they were pasted inline as user:pass@host:port.
    if "@" in server:
        credentials, _, host = server.rpartition("@")
        if credentials:
            inline_user, _, inline_password = credentials.partition(":")
            username = username or unquote(inline_user)
            password = password or unquote(inline_password)
        server = host

    cfg = {"server": scheme + server}
    if username:
        cfg["username"] = username
    if password:
        cfg["password"] = password
    return cfg


# ---------------------------------------------------------------- email
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
# 465 (implicit SSL) is the default rather than 587 (STARTTLS) because
# antivirus mail-scanning and some networks silently break STARTTLS, which
# shows up as a connection timeout rather than a clear error.
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("GMAIL_ADDRESS", "").strip()
# Google shows app passwords as four groups of four ("abcd efgh ijkl mnop").
# The spaces are for readability only and are not part of the password.
SMTP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
ALERT_RECIPIENT = os.getenv("ALERT_RECIPIENT", SMTP_USER).strip()

# ---------------------------------------------------------------- sheets
GOOGLE_CREDENTIALS_FILE = SECRETS_DIR / os.getenv(
    "GOOGLE_CREDENTIALS_FILENAME", "google-service-account.json"
)
CLIENT_NAME = os.getenv("CLIENT_NAME", "Cardcopy").strip()
SHEET_NAME = f"{CLIENT_NAME} - Competitor Intel"
RESULTS_TAB = "Results"
ALERTS_TAB = "Alerts"

# ---------------------------------------------------------------- scraping
# Milliseconds. Real product pages with heavy JS sometimes need the full 60s.
PAGE_TIMEOUT = int(os.getenv("PAGE_TIMEOUT_MS", "60000"))
# Seconds to pause between URLs, so we are not hammering anyone's server.
DELAY_BETWEEN_URLS = float(os.getenv("DELAY_BETWEEN_URLS", "3"))
# How many times to retry a URL that fails outright.
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "2"))
# Solve Cloudflare interstitials. Slower, so it is opt-in.
SOLVE_CLOUDFLARE = os.getenv("SOLVE_CLOUDFLARE", "false").lower() == "true"

# Skip downloading images, fonts, media and stylesheets. We never read them,
# and they are the bulk of a page's weight - so this cuts proxy bandwidth
# (which is billed per gigabyte) by roughly 80% and makes each page faster.
# Scripts and data requests still run, so JavaScript-rendered prices are
# unaffected. Set to false for a site that renders oddly without its styles.
DISABLE_RESOURCES = os.getenv("DISABLE_RESOURCES", "true").lower() == "true"


def missing_requirements():
    """Return a list of human-readable setup problems, empty if all is well."""
    problems = []
    if not COMPETITORS_FILE.exists():
        problems.append(f"competitors.txt not found at {COMPETITORS_FILE}")
    if not GOOGLE_CREDENTIALS_FILE.exists():
        problems.append(
            f"Google service-account key not found at {GOOGLE_CREDENTIALS_FILE}"
        )
    if not SMTP_USER or not SMTP_PASSWORD:
        problems.append("GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set in .env")
    return problems
