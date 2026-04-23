"""CLI entry point using Typer with Rich output."""

from __future__ import annotations

import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from soundaudit._version import __version__
from soundaudit.config import AppConfig
from soundaudit.db.store import Database
from soundaudit.models import HashStrategy
from soundaudit.scanner.walker import scan_directory

app = typer.Typer(
    name="soundaudit",
    no_args_is_help=True,
    rich_markup_mode="rich",
    help="Music library health scanner and metadata repair tool",
)
console = Console()


def _load_config(config_path: Path | None) -> AppConfig:
    if config_path and config_path.exists():
        return AppConfig.from_yaml(config_path)
    return AppConfig()


@app.command("scan")
def scan_cmd(
    paths: list[str] = typer.Argument(..., help="Directories to scan"),
    config: Path | None = typer.Option(None, "--config", "-c", help="Path to config YAML"),
    db: Path | None = typer.Option(None, "--db", help="SQLite database path"),
    workers: int = typer.Option(4, "--workers", "-j", help="Parallel workers", min=1, max=32),
    hash_strategy: str = typer.Option(
        "head-only",
        "--hash-strategy",
        help="Content hash strategy: head-only (default), head-tail, full, none",
    ),
    fingerprint: bool = typer.Option(False, "--fingerprint", help="Compute AcoustID fingerprints"),
) -> None:
    """Scan audio files and store metadata in the database."""
    cfg = _load_config(config)
    if db:
        cfg.database.path = str(db)
    cfg.scan.workers = workers
    try:
        cfg.scan.hash_strategy = HashStrategy(hash_strategy)
    except ValueError:
        console.print(f"[red]Invalid hash strategy: {hash_strategy}. Use: head-only, head-tail, full, none[/red]")
        raise typer.Exit(1)
    cfg.fingerprinting.enabled = fingerprint

    database = Database(str(cfg.database.resolved()))
    existing = database.get_existing_paths()

    total_new = 0
    total_skipped = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        for root_path in paths:
            root = Path(root_path).expanduser().resolve()
            if not root.exists():
                console.print(f"[red]Path does not exist: {root}[/red]")
                raise typer.Exit(1)

            console.print(f"[bold cyan]Scanning {root} ...[/bold cyan]")
            console.print(f"[dim]  hash strategy: {cfg.scan.hash_strategy.value}[/dim]")
            count = 0
            for info in scan_directory(
                root,
                extensions=set(cfg.scan.extensions),
                workers=cfg.scan.workers,
                existing=existing,
                progress=progress,
                hash_strategy=cfg.scan.hash_strategy,
            ):
                database.upsert_file(info)
                count += 1
                total_new += 1

            console.print(f"  [green]{count} files scanned[/green]")

    console.print(f"\n[bold]Done.[/bold] {total_new} files scanned. Database: {cfg.database.path}")


@app.command("report")
def report_cmd(
    config: Path | None = typer.Option(None, "--config", "-c"),
    db: Path | None = typer.Option(None, "--db"),
    missing_tags: bool = typer.Option(False, "--missing-tags", help="Show files with incomplete tags"),
    duplicates: bool = typer.Option(False, "--duplicates", help="Show duplicate groups"),
    corrupt: bool = typer.Option(False, "--corrupt", help="Show corrupt/unreadable files"),
) -> None:
    """Generate reports from the scan database."""
    cfg = _load_config(config)
    if db:
        cfg.database.path = str(db)

    database = Database(str(cfg.database.resolved()))

    if missing_tags:
        _report_missing_tags(database)
    elif duplicates:
        console.print("[yellow]Duplicate detection not yet implemented — run with --fingerprint during scan.[/yellow]")
    elif corrupt:
        _report_corrupt(database)
    else:
        _report_summary(database)


def _report_summary(database: Database) -> None:
    from sqlalchemy import func
    from soundaudit.db.store import DBFile

    with database.session() as s:
        total = s.query(func.count(DBFile.id)).scalar() or 0
        flac = s.query(func.count(DBFile.id)).filter(DBFile.format == "flac").scalar() or 0
        mp3 = s.query(func.count(DBFile.id)).filter(DBFile.format == "mp3").scalar() or 0
        corrupt = s.query(func.count(DBFile.id)).filter(DBFile.is_corrupt == 1).scalar() or 0
        no_tags = s.query(func.count(DBFile.id)).filter(DBFile.title.is_(None)).scalar() or 0
        no_artist = s.query(func.count(DBFile.id)).filter(DBFile.artist.is_(None)).scalar() or 0
        no_album = s.query(func.count(DBFile.id)).filter(DBFile.album.is_(None)).scalar() or 0

    table = Table(title="SoundAudit Library Summary", show_header=True, header_style="bold magenta")
    table.add_column("Metric", style="cyan")
    table.add_column("Count", justify="right", style="green")

    table.add_row("Total files", str(total))
    table.add_row("FLAC", str(flac))
    table.add_row("MP3", str(mp3))
    table.add_row("Corrupt/unreadable", f"[red]{corrupt}[/red]" if corrupt else "0")
    table.add_row("Missing title", str(no_tags))
    table.add_row("Missing artist", str(no_artist))
    table.add_row("Missing album", str(no_album))

    console.print(table)
    console.print("\n[dim]Run with --missing-tags, --duplicates, or --corrupt for details.[/dim]")


def _report_missing_tags(database: Database) -> None:
    from soundaudit.db.store import DBFile

    with database.session() as s:
        files = (
            s.query(DBFile)
            .filter((DBFile.title.is_(None)) | (DBFile.artist.is_(None)) | (DBFile.album.is_(None)))
            .limit(50)
            .all()
        )

    if not files:
        console.print("[green]No files with missing tags found.[/green]")
        return

    table = Table(title="Files with Missing Tags", show_header=True)
    table.add_column("File", style="dim", max_width=60)
    table.add_column("Missing", style="red")

    for f in files:
        missing = []
        if not f.title:
            missing.append("title")
        if not f.artist:
            missing.append("artist")
        if not f.album:
            missing.append("album")
        if not f.track_number:
            missing.append("track#")
        table.add_row(f.path, ", ".join(missing))

    console.print(table)
    console.print(f"\nShowing first {len(files)} of [yellow]...[/yellow] total.")


def _report_corrupt(database: Database) -> None:
    from soundaudit.db.store import DBFile

    with database.session() as s:
        files = s.query(DBFile).filter(DBFile.is_corrupt == 1).all()

    if not files:
        console.print("[green]No corrupt files found.[/green]")
        return

    table = Table(title="Corrupt / Unreadable Files", show_header=True)
    table.add_column("File", style="dim", max_width=60)
    table.add_column("Reason", style="red")

    for f in files:
        table.add_row(f.path, f.corruption_reason or "unknown")

    console.print(table)
    console.print(f"\nTotal: {len(files)} corrupt files")


@app.command("version")
def version_cmd() -> None:
    """Print version and exit."""
    console.print(f"SoundAudit [bold cyan]{__version__}[/bold cyan]")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
