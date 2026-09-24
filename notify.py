"""
Email alerts over Gmail SMTP.

This uses a Gmail *App Password*, not your normal Google password. An app
password is a 16-character code you generate specifically for one program;
it only works for mail, and you can revoke it without touching your account
password. Google requires 2-step verification to be on before it will let
you create one.
"""

import smtplib
import ssl
from email.message import EmailMessage

import config


def _money(value, currency=None):
    if value is None:
        return "-"
    symbol = {"USD": "$", "GBP": "£", "EUR": "€"}.get(currency or "", "")
    return f"{symbol}{value:,.2f}"


def build_summary(alerts, checked, failures):
    """Return (subject, plain_text, html) for the daily digest."""
    drops = [a for a in alerts if a["kind"] == "price_drop"]
    stock_outs = [a for a in alerts if a["kind"] == "out_of_stock"]
    restocks = [a for a in alerts if a["kind"] == "back_in_stock"]

    bits = []
    if drops:
        bits.append(f"{len(drops)} price drop{'s' if len(drops) != 1 else ''}")
    if stock_outs:
        bits.append(f"{len(stock_outs)} out of stock")
    if restocks:
        bits.append(f"{len(restocks)} restocked")
    headline = ", ".join(bits) if bits else "no changes"
    subject = f"Competitor Intel: {headline}"

    lines = [
        f"Checked {checked} product page(s).",
        "",
    ]

    def section(title, items, renderer):
        if not items:
            return
        lines.append(title)
        lines.append("-" * len(title))
        for a in items:
            lines.append("  " + renderer(a))
            lines.append("    " + a["url"])
        lines.append("")

    section("PRICE DROPS", drops, lambda a: (
        f"{a['competitor']} - {a['product']}: "
        f"{_money(a['was'], a.get('currency'))} -> {_money(a['now'], a.get('currency'))}"
        f"  ({a['pct']:+.1f}%)"
    ))
    section("NOW OUT OF STOCK", stock_outs,
            lambda a: f"{a['competitor']} - {a['product']}")
    section("BACK IN STOCK", restocks,
            lambda a: f"{a['competitor']} - {a['product']}")

    if failures:
        lines.append("COULD NOT BE READ")
        lines.append("-----------------")
        for f in failures:
            lines.append(f"  {f['url']}")
            lines.append(f"    {f['error']}")
        lines.append("")

    if not alerts and not failures:
        lines.append("No price drops or stock changes since the last run.")

    text = "\n".join(lines)
    html = "<pre style=\"font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px\">" \
           + text.replace("&", "&amp;").replace("<", "&lt;") + "</pre>"
    return subject, text, html


def send(subject, text, html=None):
    """Send one email. Returns (ok, message)."""
    if not config.SMTP_USER or not config.SMTP_PASSWORD:
        return False, "Gmail credentials not configured in .env"

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config.SMTP_USER
    message["To"] = config.ALERT_RECIPIENT
    message.set_content(text)
    if html:
        message.add_alternative(html, subtype="html")

    context = ssl.create_default_context()

    def via_ssl(port):
        with smtplib.SMTP_SSL(config.SMTP_HOST, port, timeout=45,
                              context=context) as server:
            server.login(config.SMTP_USER, config.SMTP_PASSWORD)
            server.send_message(message)

    def via_starttls(port):
        with smtplib.SMTP(config.SMTP_HOST, port, timeout=45) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(config.SMTP_USER, config.SMTP_PASSWORD)
            server.send_message(message)

    # Try the configured port first, then the other style. Antivirus mail
    # scanning commonly breaks STARTTLS on 587 while leaving 465 alone, and
    # some corporate networks do the reverse, so falling back covers both.
    if config.SMTP_PORT == 587:
        attempts = [("STARTTLS:587", via_starttls, 587), ("SSL:465", via_ssl, 465)]
    else:
        attempts = [("SSL:465", via_ssl, 465), ("STARTTLS:587", via_starttls, 587)]

    errors = []
    for label, sender, port in attempts:
        try:
            sender(port)
            return True, f"sent to {config.ALERT_RECIPIENT} via {label}"
        except smtplib.SMTPAuthenticationError:
            return False, (
                "Gmail rejected the login. GMAIL_APP_PASSWORD must be the "
                "16-character app password from "
                "https://myaccount.google.com/apppasswords, not your normal "
                "Google password, and 2-step verification must be switched on."
            )
        except Exception as exc:
            errors.append(f"{label} -> {type(exc).__name__}: {exc}")

    return False, "all send methods failed: " + "; ".join(errors)
