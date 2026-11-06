"""Send a test email and/or Telegram message using the settings in your .env, so you know alerts will work BEFORE relying on a monitor.

    python scripts/test_alerts.py --email you@example.com
    python scripts/test_alerts.py --telegram 123456789
    python scripts/test_alerts.py --email you@example.com --telegram 123456789
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:  # python-dotenv is optional here: real environment variables work too
    pass

from src.alerts.notify import NotifyError, send_email, send_telegram  # noqa: E402

HINTS = {
    "SMTPAuthenticationError": "The server rejected the login. For Gmail you must use an APP PASSWORD (16 letters), not your normal password, and 2-Step Verification must be on.",
    "SMTPServerDisconnected": "The server closed the connection. Check SMTP_HOST and SMTP_PORT (587 for STARTTLS, 465 for SSL).",
    "gaierror": "The SMTP_HOST name could not be found. Check the spelling (Gmail: smtp.gmail.com).",
    "TimeoutError": "No answer from the mail server. Your network or firewall may block the SMTP port; try port 465 or another network.",
    "ConnectionRefusedError": "The server refused the connection on that port. Check SMTP_PORT.",
    "SMTPSenderRefused": "The sender address was refused. Set SMTP_FROM to your own address (same as SMTP_USER for Gmail).",
    "SMTPRecipientsRefused": "The recipient address was refused. Check the spelling of the address you typed.",
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--email", help="address to send a test email to")
    ap.add_argument("--telegram", help="Telegram chat id to send a test message to")
    args = ap.parse_args()
    if not args.email and not args.telegram:
        ap.print_help()
        return 2
    ok = True
    if args.email:
        shown = {k: ("(set)" if os.environ.get(k) else "(MISSING)") for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD")}
        print(f"Email settings: SMTP_HOST={os.environ.get('SMTP_HOST') or '(MISSING)'} SMTP_PORT={os.environ.get('SMTP_PORT') or '587 (default)'} "
              f"SMTP_USER={shown['SMTP_USER']} SMTP_PASSWORD={shown['SMTP_PASSWORD']}")
        try:
            send_email(args.email, "Test alert from your competitor price tool", "If you can read this, email alerts are set up correctly.")
            print(f"OK: test email sent to {args.email} (check spam too).")
        except NotifyError as exc:
            ok = False
            print(f"FAILED: {exc}")
            print("Hint:", next((h for k, h in HINTS.items() if k in str(exc)), "Check the values in your .env (see README: Alerts)."))
    if args.telegram:
        try:
            send_telegram(args.telegram, "Test alert from your competitor price tool: Telegram alerts are set up correctly.")
            print("OK: test Telegram message sent.")
        except NotifyError as exc:
            ok = False
            print(f"FAILED: {exc}")
            print("Hint: check TELEGRAM_BOT_TOKEN, and that you pressed START in a chat with your bot before using its chat id.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
