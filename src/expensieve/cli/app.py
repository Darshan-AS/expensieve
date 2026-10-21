"""Typer CLI entrypoint (`./sieve`) for ExpenSieve."""

import json
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from expensieve.adapters.collectors.adb_sms import AdbSmsCollector
from expensieve.adapters.collectors.timeline_geo import TimelineGeoCollector
from expensieve.adapters.geocoding.nominatim_cache import CachedNominatimGeocoder
from expensieve.adapters.sinks.sourdough_sink import SourdoughLedgerSink
from expensieve.adapters.statements.hdfc_delimited import HdfcDelimitedStatementSource
from expensieve.domain.gazetteer import Gazetteer
from expensieve.domain.models import GeoPoint
from expensieve.domain.rules import DeclarativeRuleCollector
from expensieve.ports.protocols import EvidenceCollector
from expensieve.services.pipeline import SyncPipeline

app = typer.Typer(
    name="sieve",
    help="ExpenSieve: App-agnostic personal expense enrichment engine & multi-source reconciliation pipeline.",
    no_args_is_help=True,
)
console = Console()


@app.command("sync")
def sync_command(
    statement: Annotated[
        Path,
        typer.Option("--statement", "-s", help="Path to HDFC delimited (.txt/.csv) statement file."),
    ],
    account: Annotated[
        str,
        typer.Option("--account", "-a", help="Target account name in ledger."),
    ] = "HDFC",
    cutoff_ref: Annotated[
        str | None,
        typer.Option("--cutoff-ref", "-c", help="Last already-recorded 12-digit RRN or reference number."),
    ] = None,
    sms_cache: Annotated[
        Path | None,
        typer.Option("--sms-cache", help="Optional path to cached SMS dump file."),
    ] = None,
    timeline: Annotated[
        Path | None,
        typer.Option("--timeline", "-t", help="Optional path to Google Maps Timeline.json file."),
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
    """Parse statement, collect multi-source evidence (SMS, Timeline, Rules), and stage or push."""
    source = HdfcDelimitedStatementSource(
        path=statement,
        account_name=account,
        cutoff_ref=cutoff_ref,
    )
    rows = source.parse()
    collectors: list[EvidenceCollector] = [DeclarativeRuleCollector()]

    sms_collector = AdbSmsCollector(cached_sms_path=sms_cache)
    sms_evs = sms_collector.collect(
        rows,
        rows[0].statement_date if rows else Path.cwd().stat().st_mtime,  # type: ignore[arg-type]
        rows[-1].statement_date if rows else Path.cwd().stat().st_mtime,  # type: ignore[arg-type]
    ) if rows else ()

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


def main() -> None:
    app()


if __name__ == "__main__":
    main()
