"""Command-line interface: python -m src.cli <command>."""
from __future__ import annotations

import sys
from pathlib import Path

import typer
from loguru import logger
from rich.console import Console
from rich.table import Table

from src.config import ConfigError, load_config
from src.db.session import init_db, make_engine
from src.scrapers.detector import detect_platform
from src.scrapers.http import PoliteClient
from src.scrapers.runner import run_scrape

app = typer.Typer(help="Competitor price intelligence tool.", no_args_is_help=True, add_completion=False)
console = Console()
ConfigOpt = typer.Option("config.yaml", "--config", "-c", help="Path to config file.")


def _setup_logging(verbose: bool) -> None:
    logger.remove()
    logger.add(sys.stderr, level="DEBUG" if verbose else "INFO", format="<dim>{time:HH:mm:ss}</dim> {level: <7} {message}")
    Path("data/logs").mkdir(parents=True, exist_ok=True)
    logger.add("data/logs/run.log", level="DEBUG", rotation="5 MB", retention=5)


def _load(config: Path):
    try:
        return load_config(config)
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc


@app.command()
def init(config: Path = ConfigOpt) -> None:
    """Create data folders and the database tables."""
    _setup_logging(False)
    for d in ("data", "data/cache", "data/logs", "reports"):
        Path(d).mkdir(parents=True, exist_ok=True)
    cfg = _load(config)
    init_db(make_engine(cfg.db_url))
    console.print(f"[green]Ready.[/green] Database: {cfg.db_url}. Edit {config} with your real URLs, then run `detect`.")


@app.command()
def detect(config: Path = ConfigOpt, verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    """Check which platform each configured site runs on and whether its product feed is reachable."""
    _setup_logging(verbose)
    cfg = _load(config)
    table = Table("Site", "Role", "Platform", "Feed", "Evidence")
    with PoliteClient(cfg.scraping) as client:
        for site, is_source in cfg.all_sites():
            d = detect_platform(client, site.url)
            platform = d.platform.value if d.reachable else "[red]unreachable[/red]"
            table.add_row(site.name, "source" if is_source else "competitor", platform,
                          "yes" if d.feed_available else "no", "; ".join(d.evidence))
    console.print(table)


def _scrape(config: Path, site: list[str] | None, fresh: bool, verbose: bool) -> int:
    _setup_logging(verbose)
    cfg = _load(config)
    engine = make_engine(cfg.db_url)
    init_db(engine)
    only = {s.lower() for s in site} if site else None
    with PoliteClient(cfg.scraping, fresh=fresh) as client:
        run_id, results = run_scrape(cfg, engine, client, only)
    table = Table("Site", "Platform", "Method", "Products", "Status", "Detail")
    for r in results:
        status = "[green]ok[/green]" if r.status == "ok" else "[red]failed[/red]"
        table.add_row(r.site.name, r.platform or "-", r.method or "-", str(len(r.products)), status, r.error or "")
    console.print(f"Run #{run_id}")
    console.print(table)
    return sum(r.status == "ok" for r in results)


@app.command()
def scrape(
    config: Path = ConfigOpt,
    site: list[str] = typer.Option(None, "--site", help="Only scrape this site (repeatable)."),
    fresh: bool = typer.Option(False, "--fresh", help="Ignore cached responses."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Scrape products from the source and competitor sites and store a snapshot."""
    ok = _scrape(config, site, fresh, verbose)
    raise typer.Exit(0 if ok else 1)


@app.command()
def run(
    config: Path = ConfigOpt,
    fresh: bool = typer.Option(False, "--fresh"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Full pipeline. Phase 1: scraping only (matching, metrics, LLM and PDF arrive in later phases)."""
    ok = _scrape(config, None, fresh, verbose)
    console.print("[dim]Pipeline steps after scraping are added in Phases 3-6.[/dim]")
    raise typer.Exit(0 if ok else 1)


def _todo(phase: int, name: str) -> None:
    console.print(f"[yellow]`{name}` is not implemented yet (Phase {phase}).[/yellow]")
    raise typer.Exit(2)


@app.command()
def match() -> None:
    """Match competitor products to yours (Phase 3)."""
    _todo(3, "match")


@app.command("review-matches")
def review_matches() -> None:
    """Confirm or reject low-confidence matches (Phase 3)."""
    _todo(3, "review-matches")


@app.command()
def analyze() -> None:
    """Compute metrics and LLM insights (Phases 4 and 6)."""
    _todo(4, "analyze")


@app.command()
def report() -> None:
    """Generate the PDF report (Phase 5)."""
    _todo(5, "report")


if __name__ == "__main__":
    app()
