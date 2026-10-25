"""Start the website:  python -m src.web   (add --demo to use built-in fake stores)."""
from __future__ import annotations

import argparse
import os

import uvicorn
from dotenv import load_dotenv

from src.web.app import create_app


def main() -> None:
    load_dotenv()  # picks up GEMINI_API_KEY etc. from .env
    ap = argparse.ArgumentParser(description="Competitor price intelligence website")
    ap.add_argument("--host", default="127.0.0.1", help="Use 0.0.0.0 only behind authentication/a firewall")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--demo", action="store_true", help="Start 6 fake local stores so you can try the site without live sites")
    ap.add_argument("--db", default=os.environ.get("DATABASE_URL", "sqlite:///data/competitor_intel.db"))
    args = ap.parse_args()

    allow_private = args.demo or os.environ.get("ALLOW_PRIVATE_URLS") == "1"
    if args.demo:
        from src.demo.mock_sites import start_servers

        start_servers()
        print("Demo stores running on http://localhost:8101 ... :8106")
        print("Use these in the form -> source: localhost:8101   competitors: localhost:8102 .. localhost:8106")
    uvicorn.run(create_app(db_url=args.db, allow_private=allow_private, scheduler=True, demo=args.demo), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
