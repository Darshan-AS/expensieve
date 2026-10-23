"""Typer CLI entrypoint (`./sieve`) for ExpenSieve."""

import json
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from expensieve.adapters.collectors.adb_sms import AdbSmsCollector
from expensieve.adapters.collectors.chrome_cdp import CachedMerchantOrderCollector
from expensieve.adapters.collectors.gmail_imap import GmailImapCollector
from expensieve.adapters.collectors.timeline_geo import TimelineGeoCollector
from expensieve.adapters.geocoding.nominatim_cache import CachedNominatimGeocoder
from expensieve.adapters.sinks.sourdough_sink import SourdoughLedgerSink
from expensieve.adapters.statements.hdfc_cc_pdf import (
    DEFAULT_CC_PDF_PASSWORDS,
    HdfcCreditCardPdfStatementSource,
)
from expensieve.adapters.statements.hdfc_delimited import HdfcDelimitedStatementSource
from expensieve.domain.architecture import compile_architecture_bundle
from expensieve.domain.gazetteer import Gazetteer
from expensieve.domain.models import GeoPoint
from expensieve.domain.rules import DeclarativeRuleCollector
from expensieve.ports.protocols import EvidenceCollector, StatementSource
from expensieve.services.pipeline import SyncPipeline

app = typer.Typer(
    name="sieve",
    help="ExpenSieve: App-agnostic personal expense enrichment engine & multi-source reconciliation pipeline.",
    no_args_is_help=True,
)
console = Console()


def _discover_wallet_state_gmail_credentials() -> tuple[str | None, str | None]:
    """Discover `email` and `gmailAppPassword` from local or sibling `.wallet_state.json` if present."""
    candidates = (
        Path.cwd() / ".wallet_state.json",
        Path.cwd().parent / "sourdough" / ".wallet_state.json",
        Path(__file__).resolve().parents[4] / "sourdough" / ".wallet_state.json",
    )
    for candidate in candidates:
        if candidate.is_file():
            try:
                raw = json.loads(candidate.read_text(encoding="utf-8"))
                user = raw.get("email")
                pwd = raw.get("gmailAppPassword")
                if isinstance(user, str) and isinstance(pwd, str) and user and pwd:
                    return user.strip(), pwd.strip()
            except OSError, ValueError:
                continue
    return None, None


@app.command("sync")
def sync_command(
    statement: Annotated[
        Path | None,
        typer.Option(
            "--statement",
            "-s",
            help="Path to HDFC delimited (.txt/.csv) statement file, or HDFC Credit Card PDF file/directory.",
        ),
    ] = None,
    account: Annotated[
        str,
        typer.Option("--account", "-a", help="Target account name in ledger."),
    ] = "HDFC",
    cutoff_ref: Annotated[
        str | None,
        typer.Option(
            "--cutoff-ref", "-c", help="Last already-recorded 12-digit RRN, reference, or YYYY-MM-DD."
        ),
    ] = None,
    fetch_cc_gmail: Annotated[
        bool,
        typer.Option(
            "--fetch-cc-gmail",
            help="Fetch monthly HDFC Credit Card statement PDFs directly from Gmail IMAP.",
        ),
    ] = False,
    pdf_password: Annotated[
        list[str] | None,
        typer.Option(
            "--pdf-password",
            help="Password(s) for decrypting PDF statements (defaults to DARS3835, DARS5631, 128789591).",
        ),
    ] = None,
    sms_cache: Annotated[
        Path | None,
        typer.Option("--sms-cache", help="Optional path to cached SMS dump file."),
    ] = None,
    timeline: Annotated[
        Path | None,
        typer.Option("--timeline", "-t", help="Optional path to Google Maps Timeline.json file."),
    ] = None,
    zomato_json: Annotated[
        Path | None,
        typer.Option("--zomato-json", help="Optional path to cached Zomato orders JSON file."),
    ] = None,
    zepto_json: Annotated[
        Path | None,
        typer.Option("--zepto-json", help="Optional path to cached Zepto orders JSON file."),
    ] = None,
    blinkit_json: Annotated[
        Path | None,
        typer.Option("--blinkit-json", help="Optional path to cached Blinkit orders JSON file."),
    ] = None,
    amazon_json: Annotated[
        Path | None,
        typer.Option("--amazon-json", help="Optional path to cached Amazon orders JSON file."),
    ] = None,
    uber_json: Annotated[
        Path | None,
        typer.Option("--uber-json", help="Optional path to cached Uber trips JSON file."),
    ] = None,
    live_chrome: Annotated[
        bool,
        typer.Option("--live-chrome", help="Fetch live order history from open Google Chrome tabs."),
    ] = False,
    gmail_user: Annotated[
        str | None,
        typer.Option(
            "--gmail-user", envvar="EXPENSIEVE_GMAIL_USER", help="Gmail address for IMAP receipt extraction."
        ),
    ] = None,
    gmail_app_password: Annotated[
        str | None,
        typer.Option(
            "--gmail-app-password",
            envvar="EXPENSIEVE_GMAIL_APP_PASSWORD",
            help="Gmail App Password for IMAP receipt extraction.",
        ),
    ] = None,
    output_json: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Write staged CanonicalTransactions to JSON file."),
    ] = None,
    push: Annotated[
        bool,
        typer.Option("--push", help="Push enriched transactions to Sourdough Wallet sink."),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run/--live", help="Validate against Sourdough sink without writing to CouchDB."),
    ] = True,
) -> None:
    """Parse statement, collect multi-source evidence (SMS, Timeline, Gmail, Rules), and stage or push."""
    if not gmail_user or not gmail_app_password:
        disc_user, disc_pwd = _discover_wallet_state_gmail_credentials()
        gmail_user = gmail_user or disc_user
        gmail_app_password = gmail_app_password or disc_pwd

    passwords = tuple(pdf_password) if pdf_password else DEFAULT_CC_PDF_PASSWORDS
    source: StatementSource
    if fetch_cc_gmail:
        if not gmail_user or not gmail_app_password:
            raise typer.BadParameter(
                "--fetch-cc-gmail requires Gmail credentials via --gmail-user/--gmail-app-password or .wallet_state.json."
            )
        target_acc = "HDFC Credit Card" if account == "HDFC" else account
        source = HdfcCreditCardPdfStatementSource.from_gmail_imap(
            email_address=gmail_user,
            app_password=gmail_app_password,
            account_name=target_acc,
            cutoff_ref=cutoff_ref,
            passwords=passwords,
        )
    elif statement is not None:
        if statement.is_dir() or statement.suffix.lower() == ".pdf":
            target_acc = "HDFC Credit Card" if account == "HDFC" else account
            source = HdfcCreditCardPdfStatementSource(
                path=statement,
                account_name=target_acc,
                cutoff_ref=cutoff_ref,
                passwords=passwords,
            )
        else:
            source = HdfcDelimitedStatementSource(
                path=statement,
                account_name=account,
                cutoff_ref=cutoff_ref,
            )
    else:
        raise typer.BadParameter("Provide --statement <path> or --fetch-cc-gmail.")
    rows = source.parse()
    collectors: list[EvidenceCollector] = [DeclarativeRuleCollector()]

    sms_collector = AdbSmsCollector(cached_sms_path=sms_cache)
    sms_evs = (
        sms_collector.collect(
            rows,
            rows[0].statement_date if rows else Path.cwd().stat().st_mtime,  # type: ignore[arg-type]
            rows[-1].statement_date if rows else Path.cwd().stat().st_mtime,  # type: ignore[arg-type]
        )
        if rows
        else ()
    )

    row_ts_map = {
        ev.row_index: ev.timestamp_iso
        for ev in sms_evs
        if ev.row_index is not None and ev.timestamp_iso is not None
    }
    collectors.append(sms_collector)

    if timeline and timeline.exists():
        geocoder = CachedNominatimGeocoder(network_enabled=False)
        collectors.append(
            TimelineGeoCollector(
                timeline_path=timeline,
                gazetteer=Gazetteer(),
                geocoder=geocoder,
                row_timestamps_utc=row_ts_map,
            )
        )

    if any((zomato_json, zepto_json, blinkit_json, amazon_json, uber_json, live_chrome)):
        collectors.append(
            CachedMerchantOrderCollector(
                zomato_json_path=zomato_json,
                zepto_json_path=zepto_json,
                blinkit_json_path=blinkit_json,
                amazon_json_path=amazon_json,
                uber_json_path=uber_json,
                enable_live_chrome=live_chrome,
            )
        )

    if gmail_user and gmail_app_password:
        collectors.append(
            GmailImapCollector(
                email_address=gmail_user,
                app_password=gmail_app_password,
            )
        )

    sink = SourdoughLedgerSink() if push else None
    pipeline = SyncPipeline(
        statement_source=source,
        collectors=collectors,
        sink=sink,
    )
    summary = pipeline.run(push_to_sink=push, dry_run=dry_run)

    table = Table(title=f"ExpenSieve Sync Summary — {summary.account_name}")
    table.add_column("Metric", style="bold cyan")
    table.add_column("Value", justify="right")
    table.add_row("New Statement Rows", str(summary.row_count))
    table.add_row("Opening Balance", f"₹{summary.opening_balance_minor / 100:,.2f}")
    table.add_row("Net Statement Delta", f"₹{summary.net_delta_minor / 100:+,.2f}")
    table.add_row("Closing Balance", f"₹{summary.closing_balance_minor / 100:,.2f}")
    table.add_row("Auto-Enriched (High Confidence)", str(summary.row_count - summary.needs_review_count))
    table.add_row("Tagged 'Needs Review'", str(summary.needs_review_count))
    if summary.journal_id:
        table.add_row("Sink Journal / Validation ID", summary.journal_id)
    console.print(table)

    if output_json:
        output_json.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "account": summary.account_name,
            "row_count": summary.row_count,
            "opening_balance_minor": summary.opening_balance_minor,
            "closing_balance_minor": summary.closing_balance_minor,
            "needs_review_count": summary.needs_review_count,
            "transactions": [asdict(t) for t in summary.transactions],
        }
        output_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        console.print(f"[green]✔ Saved staged transactions to {output_json}[/green]")


@app.command("snap")
def snap_coordinate(
    latlng: Annotated[str, typer.Argument(help="Latitude, Longitude string (e.g. '12.9962°, 77.6685°').")],
) -> None:
    """Test snapping a WGS-84 coordinate against the Namma Metro & Landmark Gazetteer."""
    pt = GeoPoint.from_latlng_str(latlng)
    gaz = Gazetteer()
    metro = gaz.snap_metro_station(pt)
    landmark = gaz.snap_landmark(pt)
    console.print(f"[bold]Coordinate:[/bold] {pt.lat:.5f}, {pt.lng:.5f}")
    console.print(f"[bold]Nearest Metro Station:[/bold] {metro or 'None (<900m)'}")
    console.print(f"[bold]Nearest Landmark:[/bold]      {landmark or 'None'}")


def sync_architecture_diagrams(root_dir: Path, check_only: bool = False) -> tuple[str, ...]:
    """Write or verify `architecture/likec4.config.json`, `architecture/topology.c4`, and `ARCHITECTURE.md`."""
    bundle = compile_architecture_bundle()
    targets: tuple[tuple[Path, str], ...] = (
        (root_dir / "architecture" / "likec4.config.json", bundle.likec4_config_json),
        (root_dir / "architecture" / "topology.c4", bundle.likec4_dsl),
        (root_dir / "ARCHITECTURE.md", bundle.architecture_markdown),
    )
    out_of_sync: list[str] = []
    for path, expected in targets:
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current != expected:
            rel = str(path.relative_to(root_dir))
            out_of_sync.append(rel)
            if not check_only:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(expected, encoding="utf-8")
    return tuple(out_of_sync)


@app.command("diagram")
def diagram_command(
    check: Annotated[
        bool,
        typer.Option("--check", help="Verify that architecture/topology.c4 and ARCHITECTURE.md are in sync."),
    ] = False,
) -> None:
    """Compile or verify deterministic LikeC4 (`architecture/topology.c4`) and `ARCHITECTURE.md` diagrams."""
    root_dir = Path(__file__).resolve().parents[3]
    changed = sync_architecture_diagrams(root_dir, check_only=check)
    if check:
        if changed:
            console.print(f"[red]✘ Architecture files out of sync: {', '.join(changed)}[/red]")
            raise typer.Exit(code=1)
        console.print("[green]✔ LikeC4 topology & ARCHITECTURE.md are in sync.[/green]")
        return

    if changed:
        console.print(f"[green]✔ Updated architecture artifacts: {', '.join(changed)}[/green]")
    else:
        console.print("[green]✔ Architecture artifacts already up to date.[/green]")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
