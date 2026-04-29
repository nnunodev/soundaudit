"""CLI entry point using Typer with Rich output."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from soundaudit._version import __version__
from soundaudit.analyzer.acoustid import (
    AcoustidDuplicateAnalyzer,
    AcoustidGroupVerdict,
    DupType,
    analyze_acoustid_keepers,
)
from soundaudit.analyzer.duplicates import (
    DuplicateAnalyzer,
    DuplicateGroupResult,
    KeeperVerdict,
    analyze_keepers,
)
from soundaudit.config import AppConfig
from soundaudit.db.store import AcoustidGroup, Database
from soundaudit.models import HashStrategy
from soundaudit.reporter import ReportExporter, infer_format, MarkdownSection
from soundaudit.scanner.walker import scan_directory
from soundaudit.tui import SoundAuditApp

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
    analyze_duplicates: bool = typer.Option(True, "--analyze-duplicates/--skip-analyze", help="Run duplicate analysis after scan"),
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
                fingerprint=cfg.fingerprinting.enabled,
                fpcalc_path=cfg.fingerprinting.fpcalc_path,
            ):
                database.upsert_file(info)
                count += 1
                total_new += 1

            console.print(f"  [green]{count} files scanned[/green]")

    console.print(f"\n[bold]Done.[/bold] {total_new} files scanned. Database: {cfg.database.path}")

    if analyze_duplicates:
        DuplicateAnalyzer(database, console=console).run()


@app.command("report")
def report_cmd(
    config: Path | None = typer.Option(None, "--config", "-c"),
    db: Path | None = typer.Option(None, "--db"),
    missing_tags: bool = typer.Option(False, "--missing-tags", help="Show files with incomplete tags"),
    duplicates: bool = typer.Option(False, "--duplicates", help="Show duplicate groups"),
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
            },
        }
        exporter = ReportExporter(out_path)
        if out_format == "json":
            exporter.write_json(data)
        elif out_format == "csv":
            exporter.write_csv([data["metrics"]])
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
            f"[yellow]wasted[/yellow]",
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
                "dup_type": fv.dup_type.value,
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
                fv.dup_type.value,
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

        bfb_count = sum(1 for v in verdict.file_verdicts if v.dup_type == DupType.BIT_FOR_BIT)
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
                DupType.BIT_FOR_BIT: "[green]",
                DupType.TRANSCODE: "[yellow]",
            }[fv.dup_type]

            table.add_row(
                f"{style}{fv.verdict.value}[/]",
                f"{type_style}{fv.dup_type.value}[/]",
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


def _human_size(size_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size_bytes) < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} PB"


def main() -> None:
    app()


if __name__ == "__main__":
    main()
