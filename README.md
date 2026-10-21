# `expensieve` (`sieve` CLI)

**`expen·sieve`** (*Expense + Sieve / Pensieve*) is a modular, strictly typed, app-agnostic **personal expense enrichment engine** and **multi-source reconciliation pipeline**.

It ingests raw bank/card statements (e.g., HDFC Bank) as the financial spine, cross-references exact timestamps and itemized details across multiple evidence collectors (**Android SMS via ADB**, **Google Maps `Timeline.json`**, **Gmail IMAP receipts & PDFs**, and **Chrome authenticated merchant sessions**), and pushes verified, rupee-accurate records to pluggable ledger backends (starting with **Wallet by BudgetBakers** via [`sourdough`](../sourdough)).

---

## Architecture (Hexagonal / Ports & Adapters)

```text
src/expensieve/
├── domain/
│   ├── models.py              # Immutable value objects (StatementRow, EnrichmentEvidence, CanonicalTransaction, DebtGroup)
│   ├── gazetteer.py           # 68 Namma Metro stations + personal landmarks + Haversine coordinate snapping
│   └── rules.py               # Declarative enrichment rules (trip windows, #Family tagging, recurring payees, Debt detection)
├── ports/
│   └── protocols.py           # Structural typing.Protocol interfaces (StatementSource, EvidenceCollector, GeoCoder, LedgerSink)
├── adapters/
│   ├── statements/
│   │   └── hdfc_delimited.py  # HDFC .txt / .csv statement parser with 12-digit RRN & opening/closing balance verification
│   ├── collectors/
│   │   ├── adb_sms.py         # Pixel SMS inbox collector (content://sms/inbox -> 12-digit RRN & exact UTC/IST second)
│   │   ├── timeline_geo.py    # Google Maps Timeline.json collector (IN_SUBWAY -> Metro stations, IN_PASSENGER_VEHICLE -> routes, VISIT -> places)
│   │   ├── gmail_imap.py      # Pluggable Gmail receipt & PDF extractor (Uber, Swiggy, Amazon, Rapido, Namma Yatri, IndMoney)
│   │   └── chrome_cdp.py      # Chrome session order collector (Zomato, Zepto, Amazon)
│   ├── geocoding/
│   │   └── nominatim_cache.py # Disk-cached OpenStreetMap Nominatim reverse-geocoder
│   └── sinks/
│       └── sourdough_sink.py  # Wallet by BudgetBakers CouchDB sink (via sourdough) with balance assertion & rollback journal
├── services/
│   ├── evidence_merger.py     # Priority-weighted merger joining StatementRow + EnrichmentEvidence
│   └── pipeline.py            # End-to-end sync orchestrator (Ingest -> Collect -> Merge & Categorize -> Stage/Commit)
└── cli/
    └── app.py                 # `./sieve` Typer CLI (`sync`, `inspect-timeline`, `verify-balance`)
```

---

## Quick Start

```bash
# Run CLI help
./sieve --help

# Dry-run a statement enrichment & balance verification across all available sources
./sieve sync hdfc --statement ~/Downloads/Acct_Statement_XXXXXXXX0122.txt --dry-run

# Execute live sync to Wallet by BudgetBakers (via sourdough)
./sieve sync hdfc --statement ~/Downloads/Acct_Statement_XXXXXXXX0122.txt
```
