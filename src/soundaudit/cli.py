"""CLI entry point using Typer with Rich output."""

from __future__ import annotations

import contextlib
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from soundaudit._version import __version__
from soundaudit.actuator.tags import (
    TagWriteError,
    resolved_metadata_to_tags,
    snapshot_tags,
    write_tags,
)
from soundaudit.analyzer.acoustid import (
    AcoustidDuplicateAnalyzer,
    DupType,
    analyze_acoustid_keepers,
)
from soundaudit.analyzer.duplicates import (
    DuplicateAnalyzer,
    DuplicateGroupResult,
    KeeperVerdict,
    analyze_keepers,
)
from soundaudit.analyzer.transcode import analyze_library_transcodes
from soundaudit.config import AppConfig
from soundaudit.db.store import AcoustidGroup, Database
from soundaudit.models import HashStrategy
from soundaudit.reporter import MarkdownSection, ReportExporter, infer_format
from soundaudit.resolver.musicbrainz import ResolvedMetadata
from soundaudit.scanner.walker import _shutdown, scan_directory
from soundaudit.tui import SoundAuditApp

app = typer.Typer(
    name="soundaudit",
    no_args_is_help=True,
    rich_markup_mode="rich",
    help="Music library health scanner and metadata repair tool",
)
console = Console()


@app.callback()
def main_callback(
    verbose: int = typer.Option(0, "--verbose", "-v", count=True, help="Increase verbosity (-v, -vv)"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Suppress non-error output"),
) -> None:
    """Global options."""
    if quiet:
        level = logging.ERROR
    elif verbose == 1:
        level = logging.INFO
    elif verbose >= 2:
        level = logging.DEBUG
    else:
        level = logging.WARNING

    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_time=False)],
    )


def _load_config(config_path: Path | None) -> AppConfig:
    if config_path and config_path.exists():
        return AppConfig.from_yaml(config_path)
    return AppConfig.from_yaml()


@app.command("scan")
def scan_cmd(
    paths: list[str] = typer.Argument(None, help="Directories to scan"),
    config: Path | None = typer.Option(None, "--config", "-c", help="Path to config YAML"),
    db: Path | None = typer.Option(None, "--db", help="SQLite database path"),
    workers: int = typer.Option(4, "--workers", "-j", help="Parallel workers", min=1, max=32),
    hash_strategy: str = typer.Option(
        "head-only",
        "--hash-strategy",
        help="Content hash strategy: head-only (default), head-tail, full, none",
    ),
    fingerprint: bool = typer.Option(False, "--fingerprint", help="Compute AcoustID fingerprints"),
    analyze_duplicates: bool = typer.Option(True, "--analyze-duplicates/--skip-analyze", help="Run duplicate analysis after scan"),
) -> None:
    """Scan audio files and store metadata in the database."""
    cfg = _load_config(config)
    if not paths:
        paths = cfg.scan.paths
    if not paths:
        console.print("[red]No scan paths provided. Pass directories as arguments or set scan.paths in config.yaml.[/red]")
        raise typer.Exit(1)
    if db:
        cfg.database.path = str(db)
    cfg.scan.workers = workers
    try:
        cfg.scan.hash_strategy = HashStrategy(hash_strategy)
    except ValueError:
        console.print(f"[red]Invalid hash strategy: {hash_strategy}. Use: head-only, head-tail, full, none[/red]")
        raise typer.Exit(1) from None
    cfg.fingerprinting.enabled = fingerprint

    database = Database(str(cfg.database.resolved()))
    existing = database.get_existing_paths()

    total_new = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        for root_path in paths:
            if _shutdown.is_set():
                break
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
                fingerprint=cfg.fingerprinting.enabled,
                fpcalc_path=cfg.fingerprinting.fpcalc_path,
            ):
                if _shutdown.is_set():
                    break
                database.upsert_file(info)
                count += 1
                total_new += 1

            console.print(f"  [green]{count} files scanned[/green]")

    console.print(f"\n[bold]Done.[/bold] {total_new} files scanned. Database: {cfg.database.path}")
    if _shutdown.is_set():
        console.print("[yellow]Scan was interrupted. Partial results saved.[/yellow]")
        raise typer.Exit(130)

    if analyze_duplicates and not _shutdown.is_set():
        DuplicateAnalyzer(database, console=console).run()


@app.command("report")
def report_cmd(
    config: Path | None = typer.Option(None, "--config", "-c"),
    db: Path | None = typer.Option(None, "--db"),
    missing_tags: bool = typer.Option(False, "--missing-tags", help="Show files with incomplete tags"),
    duplicates: bool = typer.Option(False, "--duplicates", help="Show duplicate groups"),
    transcodes: bool = typer.Option(False, "--transcodes", help="Show suspected transcode files"),
    corrupt: bool = typer.Option(False, "--corrupt", help="Show corrupt/unreadable files"),
    output: Path | None = typer.Option(None, "--output", "-o", help="Write report to file (infer format from extension: .json, .csv, .md, .txt)"),
    fmt: str | None = typer.Option(None, "--format", help="Override format: json, csv, markdown"),
) -> None:
    """Generate reports from the scan database."""
    cfg = _load_config(config)
    if db:
        cfg.database.path = str(db)

    database = Database(str(cfg.database.resolved()))
    out_path = output
    out_format = fmt or (infer_format(out_path) if out_path else "console")

    if missing_tags:
        _report_missing_tags(database, out_path, out_format)
    elif duplicates:
        _report_duplicates(database, out_path, out_format)
    elif transcodes:
        _report_transcodes(database, out_path, out_format)
    elif corrupt:
        _report_corrupt(database, out_path, out_format)
    else:
        _report_summary(database, out_path, out_format)


def _report_summary(
    database: Database,
    out_path: Path | None = None,
    out_format: str = "console",
) -> None:
    from sqlalchemy import func

    from soundaudit.db.store import DBFile, DuplicateGroup

    with database.session() as s:
        total = s.query(func.count(DBFile.id)).scalar() or 0
        flac = s.query(func.count(DBFile.id)).filter(DBFile.format == "flac").scalar() or 0
        mp3 = s.query(func.count(DBFile.id)).filter(DBFile.format == "mp3").scalar() or 0
        corrupt = s.query(func.count(DBFile.id)).filter(DBFile.is_corrupt == 1).scalar() or 0
        no_tags = s.query(func.count(DBFile.id)).filter(DBFile.title.is_(None)).scalar() or 0
        no_artist = s.query(func.count(DBFile.id)).filter(DBFile.artist.is_(None)).scalar() or 0
        no_album = s.query(func.count(DBFile.id)).filter(DBFile.album.is_(None)).scalar() or 0
        dup_groups = s.query(func.count(DuplicateGroup.id)).scalar() or 0
        dup_files = (
            s.query(func.count(DBFile.id))
            .filter(DBFile.duplicate_group_id.is_not(None))
            .scalar()
            or 0
        )

        transcode_count = (
            s.query(func.count(DBFile.id))
            .filter(DBFile.is_transcode == 1)
            .scalar()
            or 0
        )
        transcode_high = (
            s.query(func.count(DBFile.id))
            .filter(
                DBFile.is_transcode == 1,
                DBFile.transcode_confidence >= 0.70,
            )
            .scalar()
            or 0
        )

    if out_path:
        data = {
            "report_type": "summary",
            "generated": datetime.now().isoformat(),
            "metrics": {
                "total_files": total,
                "flac": flac,
                "mp3": mp3,
                "corrupt": corrupt,
                "missing_title": no_tags,
                "missing_artist": no_artist,
                "missing_album": no_album,
                "duplicate_groups": dup_groups,
                "duplicate_files": dup_files,
                "transcode_suspects": transcode_count,
                "transcode_high_confidence": transcode_high,
            },
        }
        exporter = ReportExporter(out_path)
        if out_format == "json":
            exporter.write_json(data)
        elif out_format == "csv":
            exporter.write_csv([cast(dict[str, Any], data["metrics"])])
        else:
            sections = [
                MarkdownSection(
                    heading="Library Summary",
                    headers=["Metric", "Count"],
                    rows=[
                        ["Total files", str(total)],
                        ["FLAC", str(flac)],
                        ["MP3", str(mp3)],
                        ["Corrupt", str(corrupt)],
                        ["Missing title", str(no_tags)],
                        ["Missing artist", str(no_artist)],
                        ["Missing album", str(no_album)],
                        ["Duplicate groups", str(dup_groups)],
                        ["Duplicate files", str(dup_files)],
                        ["Transcode suspects", str(transcode_count)],
                        ["Transcode high conf", str(transcode_high)],
                    ],
                )
            ]
            exporter.write_markdown("SoundAudit Report", sections)
        console.print(f"[green]Saved to {out_path}[/green]")
        return

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
    if transcode_count:
        style = "[red]" if transcode_high > 0 else "[yellow]"
        table.add_row(
            "Transcode suspects",
            f"{style}{transcode_count} ({transcode_high} high conf)[/]",
        )

    console.print(table)
    console.print("\n[dim]Run with --missing-tags, --duplicates, or --corrupt for details.[/dim]")


def _report_missing_tags(
    database: Database,
    out_path: Path | None = None,
    out_format: str = "console",
) -> None:
    from soundaudit.db.store import DBFile

    with database.session() as s:
        files = (
            s.query(DBFile)
            .filter((DBFile.title.is_(None)) | (DBFile.artist.is_(None)) | (DBFile.album.is_(None)))
            .limit(50)
            .all()
        )

    if not files:
        if out_path:
            exporter = ReportExporter(out_path)
            if out_format == "json":
                exporter.write_json({"report_type": "missing_tags", "files": []})
            elif out_format == "csv":
                exporter.write_csv([])
            else:
                exporter.write_markdown("Missing Tags", [MarkdownSection("Missing Tags", ["File", "Missing"], [], "No files with missing tags.")])
            console.print(f"[green]Saved to {out_path}[/green]")
        else:
            console.print("[green]No files with missing tags found.[/green]")
        return

    if out_path:
        rows = []
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
            rows.append({"file": f.path, "missing": ", ".join(missing)})
        exporter = ReportExporter(out_path)
        if out_format == "json":
            exporter.write_json({"report_type": "missing_tags", "files": rows})
        elif out_format == "csv":
            exporter.write_csv(rows)
        else:
            md_rows = [[r["file"], r["missing"]] for r in rows]
            exporter.write_markdown(
                "Missing Tags",
                [MarkdownSection("Files with Missing Tags", ["File", "Missing"], md_rows)],
            )
        console.print(f"[green]Saved to {out_path}[/green]")
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


def _report_corrupt(
    database: Database,
    out_path: Path | None = None,
    out_format: str = "console",
) -> None:
    from soundaudit.db.store import DBFile

    with database.session() as s:
        files = s.query(DBFile).filter(DBFile.is_corrupt == 1).all()

    if not files:
        if out_path:
            exporter = ReportExporter(out_path)
            if out_format == "json":
                exporter.write_json({"report_type": "corrupt", "files": []})
            elif out_format == "csv":
                exporter.write_csv([])
            else:
                exporter.write_markdown("Corrupt Files", [MarkdownSection("Corrupt Files", ["File", "Reason"], [], "No corrupt files found.")])
            console.print(f"[green]Saved to {out_path}[/green]")
        else:
            console.print("[green]No corrupt files found.[/green]")
        return

    if out_path:
        rows = [{"file": f.path, "reason": f.corruption_reason or "unknown"} for f in files]
        exporter = ReportExporter(out_path)
        if out_format == "json":
            exporter.write_json({"report_type": "corrupt", "files": rows})
        elif out_format == "csv":
            exporter.write_csv(rows)
        else:
            md_rows = [[r["file"], r["reason"]] for r in rows]
            exporter.write_markdown(
                "Corrupt Files",
                [MarkdownSection("Corrupt / Unreadable Files", ["File", "Reason"], md_rows)],
            )
        console.print(f"[green]Saved to {out_path}[/green]")
        return

    table = Table(title="Corrupt / Unreadable Files", show_header=True)
    table.add_column("File", style="dim", max_width=60)
    table.add_column("Reason", style="red")

    for f in files:
        table.add_row(f.path, f.corruption_reason or "unknown")

    console.print(table)
    console.print(f"\nTotal: {len(files)} corrupt files")


@app.command("tui")
def tui_cmd(
    config: Path | None = typer.Option(None, "--config", "-c", help="Path to config YAML"),
    db: Path | None = typer.Option(None, "--db", help="SQLite database path"),
) -> None:
    """Launch the interactive TUI."""
    SoundAuditApp(
        db_path=str(db) if db else None,
        config_path=config,
    ).run()


@app.command("version")
def version_cmd() -> None:
    """Print version and exit."""
    console.print(f"SoundAudit [bold cyan]{__version__}[/bold cyan]")


@app.command("duplicates")
def duplicates_cmd(
    config: Path | None = typer.Option(None, "--config", "-c"),
    db: Path | None = typer.Option(None, "--db"),
    delete_prompt: bool = typer.Option(False, "--delete-prompt", help="Interactively prompt to delete lower-quality duplicates"),
    auto_select_best: bool = typer.Option(False, "--auto-select-best", help="Automatically select best keeper and mark others for deletion"),
    output: Path | None = typer.Option(None, "--output", "-o", help="Export duplicate report"),
    fmt: str | None = typer.Option(None, "--format", help="Override export format: json, csv, markdown"),
) -> None:
    """Find fuzzy duplicates via AcoustID fingerprints and optionally act on them."""
    cfg = _load_config(config)
    if db:
        cfg.database.path = str(db)

    database = Database(str(cfg.database.resolved()))

    # Run analysis
    analyzer = AcoustidDuplicateAnalyzer(database, console=console)
    results = analyzer.run()
    if not results:
        return

    # Print rich table
    _report_acoustid_duplicates(database, output, fmt)

    if auto_select_best:
        _auto_select_best_acoustid(database, results, dry_run=False)
    elif delete_prompt:
        _prompt_delete_acoustid(database, results)


@app.command("analyze")
def analyze_cmd(
    config: Path | None = typer.Option(None, "--config", "-c"),
    db: Path | None = typer.Option(None, "--db"),
    duplicates: bool = typer.Option(True, "--duplicates/--no-duplicates", help="Run content-hash duplicate detection"),
    acoustid: bool = typer.Option(False, "--acoustid", help="Run AcoustID fingerprint duplicate detection"),
    transcodes: bool = typer.Option(False, "--transcodes", help="Run spectral transcode detection (slow, ffmpeg required)"),
    workers: int = typer.Option(4, "--workers", "-j", help="Parallel workers for transcode analysis", min=1, max=16),
) -> None:
    """Run analysis passes on the scanned database."""
    cfg = _load_config(config)
    if db:
        cfg.database.path = str(db)

    database = Database(str(cfg.database.resolved()))

    if duplicates:
        DuplicateAnalyzer(database, console=console).run()
    if acoustid:
        AcoustidDuplicateAnalyzer(database, console=console).run()
    if transcodes:
        analyze_library_transcodes(database, workers=workers, console=console)


@app.command("resolve")
def resolve_cmd(
    config: Path | None = typer.Option(None, "--config", "-c"),
    db: Path | None = typer.Option(None, "--db"),
    auto_write: bool = typer.Option(
        False,
        "--auto-write",
        help="Write resolved tags back to files immediately after resolving",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Preview only, do not modify database or files",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Re-resolve files that already have MusicBrainz data",
    ),
    workers: int = typer.Option(
        1, "--workers", "-j", help="Parallel workers", min=1, max=4
    ),
) -> None:
    """Resolve canonical metadata from MusicBrainz for unscanned files."""
    cfg = _load_config(config)
    if db:
        cfg.database.path = str(db)

    database = Database(str(cfg.database.resolved()))
    from soundaudit.resolver.musicbrainz import MusicBrainzResolver

    resolver = MusicBrainzResolver(
        database,
        cfg.resolvers,
        cfg.fingerprinting,
        console=console,
    )
    results = resolver.resolve_library(dry_run=dry_run, force=force, workers=workers)

    if auto_write and not dry_run and results:
        _auto_write_tags(database, results, fields=None, backup=cfg.actuator.backup_before_write)
    elif auto_write and dry_run:
        console.print("[yellow]--auto-write skipped because --dry-run is active.[/yellow]")


@app.command("fix")
def fix_cmd(
    config: Path | None = typer.Option(None, "--config", "-c"),
    db: Path | None = typer.Option(None, "--db"),
    fields: str = typer.Option(
        "artist,album,title,year",
        "--fields",
        help="Comma-separated fields to update",
    ),
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Actually write tags to files (default is dry-run)",
    ),
    backup: bool = typer.Option(
        True,
        "--backup/--no-backup",
        help="Backup original tags to database before writing",
    ),
    source: str = typer.Option(
        "musicbrainz",
        "--source",
        help="Tag source to use: musicbrainz",
    ),
    limit: int = typer.Option(
        0,
        "--limit",
        help="Maximum files to process (0 = unlimited)",
        min=0,
    ),
) -> None:
    """Write corrected tags back to audio files."""
    cfg = _load_config(config)
    if db:
        cfg.database.path = str(db)

    database = Database(str(cfg.database.resolved()))

    selected_fields = {f.strip().lower() for f in fields.split(",")}
    try:
        from soundaudit.actuator.tags import validate_fields
        selected_fields = validate_fields(selected_fields)
    except Exception as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    if source != "musicbrainz":
        console.print(f"[red]Unknown source: {source}. Only 'musicbrainz' is supported.[/red]")
        raise typer.Exit(1) from None

    if not apply and not cfg.actuator.dry_run:
        # If --apply not given but config says dry_run=False, still require --apply for safety
        pass

    dry_run = not apply

    from soundaudit.db.store import DBFile
    with database.session() as s:
        query = s.query(DBFile).filter(DBFile.mb_recording_id.is_not(None))
        if limit > 0:
            query = query.limit(limit)
        files = query.all()
        s.expunge_all()

    if not files:
        console.print("[green]No files with resolved MusicBrainz data found.[/green]")
        return

    console.print(
        f"{'[bold cyan]Preview[/bold cyan]' if dry_run else '[bold green]Applying[/bold green]'} "
        f"tag updates for {len(files)} file(s) — fields: {', '.join(sorted(selected_fields))}"
    )

    fixed = 0
    skipped = 0
    errors = 0

    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("File", style="dim", max_width=50)
    table.add_column("Field", style="yellow")
    table.add_column("Before", style="red")
    table.add_column("After", style="green")

    for db_file in files:
        tags = resolved_metadata_to_tags(
            db_file.mb_title,
            db_file.mb_artist,
            db_file.mb_album,
            db_file.mb_album_artist,
            db_file.mb_year,
            db_file.mb_genre,
        )
        path = Path(db_file.path)

        try:
            original = snapshot_tags(path)
        except Exception as exc:
            console.print(f"[red]Cannot read {path}: {exc}[/red]")
            errors += 1
            continue

        # Determine which fields would actually change
        changes: list[tuple[str, str, str]] = []
        for field in sorted(selected_fields):
            old_val = str(original.get(field) or "")
            new_val = str(getattr(tags, field) or "")
            if new_val and old_val != new_val:
                changes.append((field, old_val or "—", new_val))

        if not changes:
            skipped += 1
            if not dry_run:
                with contextlib.suppress(Exception):
                    database.save_written_tags(db_file.id, tags, selected_fields)
            continue

        for field, old_val, new_val in changes:
            table.add_row(str(path.name), field, old_val, new_val)

        if not dry_run:
            try:
                backup_snapshot = write_tags(path, tags, fields=selected_fields, backup=backup)
                if backup:
                    database.save_tag_backup(db_file.id, backup_snapshot)
                database.save_written_tags(db_file.id, tags, selected_fields)
                fixed += 1
            except TagWriteError as exc:
                console.print(f"[red]Write failed for {path}: {exc}[/red]")
                errors += 1

    console.print(table)
    if dry_run:
        console.print(f"\n[bold]Preview complete.[/bold] {fixed + skipped + errors} files — {fixed} would change, {skipped} unchanged, {errors} errors.")
        console.print("[dim]Add --apply to write these changes.[/dim]")
    else:
        console.print(f"\n[bold]Done.[/bold] {fixed} files updated, {skipped} unchanged, {errors} errors.")


def _auto_write_tags(
    database: Database,
    results: list[tuple[int, ResolvedMetadata]],
    fields: set[str] | None,
    backup: bool = True,
) -> None:
    """Write MusicBrainz-resolved tags back to files."""
    fixed = 0
    errors = 0
    effective_fields = fields if fields is not None else set()
    for file_id, md in results:
        from soundaudit.db.store import DBFile
        with database.session() as s:
            db_file = s.query(DBFile).filter_by(id=file_id).first()
            if not db_file:
                continue
            path = Path(db_file.path)

        tags = resolved_metadata_to_tags(
            md.title, md.artist, md.album, md.album_artist, md.year, md.genre
        )
        try:
            backup_snapshot = write_tags(path, tags, fields=fields, backup=backup)
            if backup:
                database.save_tag_backup(file_id, backup_snapshot)
            database.save_written_tags(file_id, tags, effective_fields)
            fixed += 1
        except TagWriteError as exc:
            console.print(f"[red]Auto-write failed for {path}: {exc}[/red]")
            errors += 1

    console.print(f"[green]Auto-wrote tags to {fixed} file(s).[/green]" + (f" [red]{errors} errors.[/red]" if errors else ""))


def _report_transcodes(
    database: Database,
    out_path: Path | None = None,
    out_format: str = "console",
) -> None:
    from soundaudit.db.store import DBFile

    with database.session() as s:
        files = (
            s.query(DBFile)
            .filter(DBFile.is_transcode == 1)
            .order_by(DBFile.transcode_confidence.desc())
            .limit(100)
            .all()
        )

    if not files:
        msg = "[green]No transcode suspects found.[/green]"
        if out_path:
            exporter = ReportExporter(out_path)
            if out_format == "json":
                exporter.write_json({"report_type": "transcodes", "files": []})
            elif out_format == "csv":
                exporter.write_csv([])
            else:
                exporter.write_markdown(
                    "Transcodes",
                    [MarkdownSection("Transcode Suspects", ["File", "Confidence", "Reason"], [], "No transcode suspects found.")],
                )
            console.print(f"[green]Saved to {out_path}[/green]")
        else:
            console.print(msg)
        return

    if out_path:
        rows = [
            {
                "file": f.path,
                "confidence": f.transcode_confidence,
                "reason": f.transcode_reason or "",
                "cutoff_hz": f.spectral_cutoff_hz,
                "format": f.format,
                "bit_depth": f.bit_depth,
                "sample_rate_hz": f.sample_rate_hz,
            }
            for f in files
        ]
        exporter = ReportExporter(out_path)
        if out_format == "json":
            exporter.write_json({"report_type": "transcodes", "files": rows})
        elif out_format == "csv":
            exporter.write_csv(rows)
        else:
            md_rows = [
                [
                    str(Path(cast(str, r["file"])).name),
                    f"{r['confidence']:.0%}",
                    r["reason"] or "—",
                    f"{r['cutoff_hz']:,}Hz" if r["cutoff_hz"] else "—",
                ]
                for r in rows
            ]
            exporter.write_markdown(
                "Transcode Suspects",
                [MarkdownSection("Suspected Transcodes", ["File", "Confidence", "Reason", "Cutoff"], cast(list[list[str]], md_rows))],
            )
        console.print(f"[green]Saved to {out_path}[/green]")
        return

    table = Table(title="Suspected Transcodes", show_header=True)
    table.add_column("File", style="dim", max_width=60)
    table.add_column("Confidence", width=10)
    table.add_column("Cutoff", width=8)
    table.add_column("Reason", max_width=40)

    for f in files:
        conf_style = {
            (0.7, 1.0): "[bold red]",
            (0.4, 0.7): "[yellow]",
        }
        style = "[dim]"
        for (lo, hi), style_code in conf_style.items():
            if lo <= f.transcode_confidence <= hi:
                style = style_code
                break
        cutoff = f"{f.spectral_cutoff_hz:,}Hz" if f.spectral_cutoff_hz else "—"
        table.add_row(
            str(Path(f.path).name),
            f"{style}{f.transcode_confidence:.0%}[/]",
            cutoff,
            f.transcode_reason or "—",
        )

    console.print(table)
    console.print(f"\nShowing {len(files)} suspects (sorted by confidence).")


def _report_duplicates(
    database: Database,
    out_path: Path | None = None,
    out_format: str = "console",
) -> None:
    from soundaudit.db.store import DBFile, DuplicateGroup

    with database.session() as s:
        groups = s.query(DuplicateGroup).all()

    if not groups:
        if out_path:
            exporter = ReportExporter(out_path)
            if out_format == "json":
                exporter.write_json({"report_type": "duplicates", "groups": []})
            elif out_format == "csv":
                exporter.write_csv([])
            else:
                exporter.write_markdown(
                    "Duplicates",
                    [MarkdownSection("Duplicate Groups", ["Group", "File", "Verdict", "Tech", "Why"], [], "No duplicates found.")],
                )
            console.print(f"[green]Saved to {out_path}[/green]")
        else:
            console.print("[green]No duplicate groups found.[/green]")
        return

    total_wasted = 0
    total_groups = 0
    group_data: list[dict] = []
    csv_rows: list[dict] = []
    md_sections: list[MarkdownSection] = []

    for db_group in groups:
        with database.session() as s:
            files = (
                s.query(DBFile)
                .filter_by(duplicate_group_id=db_group.id)
                .order_by(DBFile.path)
                .all()
            )
        if not files or len(files) < 2:
            continue

        total_groups += 1
        group_result = DuplicateGroupResult(
            content_hash=db_group.acoustid or "",
            file_count=len(files),
            total_size_bytes=sum(f.size_bytes for f in files),
            files=files,
            group_id=db_group.id,
        )
        verdict = analyze_keepers(group_result)
        total_wasted += verdict.wasted_bytes

        file_entries = []
        md_rows: list[list[str]] = []
        for fv in verdict.file_verdicts:
            f = fv.db_file
            entry = {
                "path": f.path,
                "verdict": fv.verdict.value,
                "score": round(fv.score, 1),
                "reasons": fv.reasons,
                "album": f.album or "",
                "format": f.format or "",
                "bit_depth": f.bit_depth,
                "sample_rate_hz": f.sample_rate_hz,
                "size_bytes": f.size_bytes,
                "lossless": bool(f.lossless),
            }
            file_entries.append(entry)
            csv_rows.append({
                "group_id": db_group.id,
                **entry,
            })
            md_rows.append([
                fv.verdict.value,
                str(Path(f.path).name),
                f.album or "—",
                fv.tech_summary,
                ", ".join(fv.reasons[:3]),
            ])

        group_data.append({
            "group_id": db_group.id,
            "content_hash": db_group.acoustid or "",
            "total_files": len(files),
            "wasted_bytes": verdict.wasted_bytes,
            "files": file_entries,
        })

        md_sections.append(
            MarkdownSection(
                heading=f"Group {db_group.id} — {_human_size(verdict.wasted_bytes)} wasted",
                headers=["Verdict", "File", "Album", "Tech", "Why"],
                rows=md_rows,
                paragraph=f"Content hash: `{db_group.acoustid or 'n/a'}`  |  {len(files)} files",
            )
        )

    if out_path:
        exporter = ReportExporter(out_path)
        if out_format == "json":
            exporter.write_json({
                "report_type": "duplicates",
                "total_groups": total_groups,
                "total_wasted_bytes": total_wasted,
                "groups": group_data,
            })
        elif out_format == "csv":
            exporter.write_csv(csv_rows)
        else:
            md_sections.append(
                MarkdownSection(
                    heading="Summary",
                    headers=["Metric", "Value"],
                    rows=[
                        ["Total groups", str(total_groups)],
                        ["Total wasted", _human_size(total_wasted)],
                    ],
                )
            )
            exporter.write_markdown("Duplicate Groups — Smart Keeper Recommendations", md_sections)
        console.print(f"[green]Saved to {out_path}[/green]")
        return

    from rich.table import Table
    table = Table(title="Duplicate Groups — Smart Keeper Recommendations", show_header=True)
    table.add_column("Verdict", justify="center", width=8)
    table.add_column("File", style="dim", max_width=50)
    table.add_column("Album", max_width=20)
    table.add_column("Tech", width=16)
    table.add_column("Why", max_width=30)

    for db_group in groups:
        with database.session() as s:
            files = (
                s.query(DBFile)
                .filter_by(duplicate_group_id=db_group.id)
                .order_by(DBFile.path)
                .all()
            )
        if not files or len(files) < 2:
            continue

        group_result = DuplicateGroupResult(
            content_hash=db_group.acoustid or "",
            file_count=len(files),
            total_size_bytes=sum(f.size_bytes for f in files),
            files=files,
            group_id=db_group.id,
        )
        verdict = analyze_keepers(group_result)

        table.add_row(
            f"[bold cyan]Group {db_group.id}[/bold cyan]",
            f"[bold]{len(files)} files[/bold]",
            "",
            _human_size(verdict.wasted_bytes),
            "[yellow]wasted[/yellow]",
            end_section=True,
        )

        for fv in verdict.file_verdicts:
            f = fv.db_file
            style = {
                KeeperVerdict.KEEP: "[bold green]",
                KeeperVerdict.DELETE: "[red]",
                KeeperVerdict.REVIEW: "[yellow]",
            }[fv.verdict]
            reset = "[/]"

            path = str(Path(f.path).name) if len(files) > 3 else str(Path(f.path))
            album = fv.album_context
            tech = fv.tech_summary
            why = ", ".join(fv.reasons[:3])

            table.add_row(
                f"{style}{fv.verdict.value}{reset}",
                path,
                album,
                tech,
                why,
            )

        table.add_row("", "", "", "", "", end_section=True)

    console.print(table)
    console.print(
        f"\n[bold]{total_groups} groups[/bold] — "
        f"Total wasted space: [bold red]{_human_size(total_wasted)}[/bold red]"
    )


def _report_acoustid_duplicates(
    database: Database,
    out_path: Path | None = None,
    out_format: str | None = None,
) -> None:
    """Print or export AcoustID duplicate report."""
    from soundaudit.db.store import DBFile

    out_format = out_format or (infer_format(out_path) if out_path else "console")

    with database.session() as s:
        groups = s.query(AcoustidGroup).all()

    if not groups:
        msg = "[green]No AcoustID duplicate groups found.[/green]"
        if out_path:
            exporter = ReportExporter(out_path)
            if out_format == "json":
                exporter.write_json({"report_type": "acoustid_duplicates", "groups": []})
            elif out_format == "csv":
                exporter.write_csv([])
            else:
                exporter.write_markdown(
                    "AcoustID Duplicates",
                    [MarkdownSection("AcoustID Duplicates", [], [], "No AcoustID duplicates found.")],
                )
            console.print(f"[green]Saved to {out_path}[/green]")
        else:
            console.print(msg)
        return

    total_wasted = 0
    total_groups = 0
    csv_rows: list[dict] = []
    md_sections: list[MarkdownSection] = []

    for db_group in groups:
        with database.session() as s:
            files = (
                s.query(DBFile)
                .filter_by(acoustid_group_id=db_group.id)
                .order_by(DBFile.path)
                .all()
            )
        if not files or len(files) < 2:
            continue

        total_groups += 1
        group_result = DuplicateGroupResult(
            content_hash=db_group.fingerprint,
            file_count=len(files),
            total_size_bytes=sum(f.size_bytes for f in files),
            files=files,
            group_id=db_group.id,
        )
        verdict = analyze_acoustid_keepers(group_result)
        total_wasted += verdict.wasted_bytes

        md_rows: list[list[str]] = []
        for fv in verdict.file_verdicts:
            f = fv.db_file
            csv_rows.append({
                "group_id": db_group.id,
                "path": f.path,
                "verdict": fv.verdict.value,
                "dup_type": fv.dup_type,
                "score": round(fv.score, 1),
                "reasons": ", ".join(fv.reasons),
                "album": f.album or "",
                "format": f.format or "",
                "bit_depth": f.bit_depth,
                "sample_rate_hz": f.sample_rate_hz,
                "size_bytes": f.size_bytes,
                "lossless": bool(f.lossless),
            })
            md_rows.append([
                fv.verdict.value,
                fv.dup_type,
                str(Path(f.path).name),
                f.album or "—",
                " ".join(fv.reasons[:3]),
            ])

        md_sections.append(
            MarkdownSection(
                heading=f"Group {db_group.id} — {_human_size(verdict.wasted_bytes)} wasted",
                headers=["Verdict", "Type", "File", "Album", "Why"],
                rows=md_rows,
                paragraph=f"Fingerprint: `{db_group.fingerprint[:32]}...`  |  {len(files)} files",
            )
        )

    if out_path:
        exporter = ReportExporter(out_path)
        if out_format == "json":
            group_meta = []
            for db_group in groups:
                with database.session() as s:
                    files = (
                        s.query(DBFile)
                        .filter_by(acoustid_group_id=db_group.id)
                        .all()
                    )
                group_meta.append({
                    "group_id": db_group.id,
                    "fingerprint": db_group.fingerprint[:64],
                    "total_files": len(files),
                })
            exporter.write_json({
                "report_type": "acoustid_duplicates",
                "total_groups": total_groups,
                "total_wasted_bytes": total_wasted,
                "groups": group_meta,
                "files": csv_rows,
            })
        elif out_format == "csv":
            exporter.write_csv(csv_rows)
        else:
            md_sections.append(
                MarkdownSection(
                    heading="Summary",
                    headers=["Metric", "Value"],
                    rows=[
                        ["Total groups", str(total_groups)],
                        ["Total wasted", _human_size(total_wasted)],
                    ],
                )
            )
            exporter.write_markdown("AcoustID Duplicate Recommendations", md_sections)
        console.print(f"[green]Saved to {out_path}[/green]")
        return

    table = Table(title="AcoustID Duplicate Groups", show_header=True)
    table.add_column("Verdict", justify="center", width=8)
    table.add_column("Type", width=10)
    table.add_column("File", style="dim", max_width=50)
    table.add_column("Album", max_width=20)
    table.add_column("Why", max_width=30)

    for db_group in groups:
        with database.session() as s:
            files = (
                s.query(DBFile)
                .filter_by(acoustid_group_id=db_group.id)
                .order_by(DBFile.path)
                .all()
            )
        if not files or len(files) < 2:
            continue

        group_result = DuplicateGroupResult(
            content_hash=db_group.fingerprint,
            file_count=len(files),
            total_size_bytes=sum(f.size_bytes for f in files),
            files=files,
            group_id=db_group.id,
        )
        verdict = analyze_acoustid_keepers(group_result)

        bfb_count = sum(1 for v in verdict.file_verdicts if v.dup_type == DupType.BIT_FOR_BIT.value)
        trans_count = len(verdict.file_verdicts) - bfb_count
        dup_summary = []
        if bfb_count > 1:
            dup_summary.append(f"{bfb_count} bit-for-bit")
        if trans_count > 0:
            dup_summary.append(f"{trans_count} transcode")

        table.add_row(
            f"[bold cyan]Group {db_group.id}[/bold cyan]",
            "",
            f"[bold]{len(files)} files[/bold]",
            ", ".join(dup_summary),
            f"[yellow]{_human_size(verdict.wasted_bytes)} wasted[/yellow]",
            end_section=True,
        )

        for fv in verdict.file_verdicts:
            f = fv.db_file
            style = {
                KeeperVerdict.KEEP: "[bold green]",
                KeeperVerdict.DELETE: "[red]",
                KeeperVerdict.REVIEW: "[yellow]",
            }[fv.verdict]

            path = str(Path(f.path).name) if len(files) > 3 else str(Path(f.path))
            album = fv.db_file.album or "[dim]—[/dim]"
            why = ", ".join(fv.reasons[:3])

            type_style = {
                DupType.BIT_FOR_BIT.value: "[green]",
                DupType.TRANSCODE.value: "[yellow]",
            }[fv.dup_type]

            table.add_row(
                f"{style}{fv.verdict.value}[/]",
                f"{type_style}{fv.dup_type}[/]",
                path,
                album,
                why,
            )
        table.add_row("", "", "", "", "", end_section=True)

    console.print(table)
    console.print(
        f"\n[bold]{total_groups} groups[/bold] — "
        f"Total wasted space: [bold red]{_human_size(total_wasted)}[/bold red]"
    )


def _auto_select_best_acoustid(
    database: Database,
    results: list[DuplicateGroupResult],
    *,
    dry_run: bool = True,
) -> None:
    from pathlib import Path as Path_

    deleted = 0
    saved = 0
    for group in results:
        verdict = analyze_acoustid_keepers(group)
        for fv in verdict.file_verdicts:
            if fv.verdict == KeeperVerdict.DELETE:
                deleted += 1
                saved += fv.db_file.size_bytes
                if not dry_run:
                    try:
                        Path_(fv.db_file.path).unlink()
                    except OSError as exc:
                        console.print(f"[red]Failed to delete {fv.db_file.path}: {exc}[/red]")
    action = "Would delete" if dry_run else "Deleted"
    console.print(
        f"[green]{action} {deleted} files, freeing {_human_size(saved)}[/green]"
    )


def _prompt_delete_acoustid(
    database: Database,
    results: list[DuplicateGroupResult],
) -> None:
    from pathlib import Path as Path_

    for group in results:
        verdict = analyze_acoustid_keepers(group)
        console.print(f"\n[bold cyan]Group {group.group_id}[/bold cyan] — {group.content_hash[:32]}...")
        for fv in verdict.file_verdicts:
            label = {
                KeeperVerdict.KEEP: "[green]KEEP[/]",
                KeeperVerdict.DELETE: "[red]DELETE[/]",
                KeeperVerdict.REVIEW: "[yellow]REVIEW[/]",
            }[fv.verdict]
            console.print(f"  {label} {fv.db_file.path} ({', '.join(fv.reasons[:3])})")

        deletions = [fv for fv in verdict.file_verdicts if fv.verdict == KeeperVerdict.DELETE]
        if not deletions:
            continue

        answer = console.input("Delete marked files? [y/N] ")
        if answer.strip().lower() == "y":
            for fv in deletions:
                try:
                    Path_(fv.db_file.path).unlink()
                    console.print(f"[dim]Deleted {fv.db_file.path}[/dim]")
                except OSError as exc:
                    console.print(f"[red]Failed to delete {fv.db_file.path}: {exc}[/red]")


def _human_size(size_bytes: int | float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size_bytes) < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} PB"


def main() -> None:
    app()


if __name__ == "__main__":
    main()
