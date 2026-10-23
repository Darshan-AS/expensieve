# `expensieve` (`./sieve` CLI)

**`expen·sieve`** (*Expense + Sieve / Pensieve*) is a modular, strictly typed, app-agnostic **personal expense enrichment engine** and **multi-source reconciliation pipeline** built on Python 3.14 and `uv`.

It ingests raw bank/card statements (e.g., HDFC Bank) as the immutable accounting spine, cross-references exact timestamps and itemized details across multiple evidence collectors (**Android SMS via ADB**, **Google Maps `Timeline.json`**, **Gmail IMAP receipts & PDFs**, and **Chrome authenticated merchant sessions**), and pushes verified, rupee-accurate records to pluggable ledger backends (starting with **Wallet by BudgetBakers** via [`sourdough`](../sourdough)).

> **Interactive Architecture & Topology:** See **[`ARCHITECTURE.md`](ARCHITECTURE.md)** and **[`architecture/topology.c4`](architecture/topology.c4)** (auto-compiled via `./sieve diagram` and published to GitHub Pages via [`.github/workflows/architecture.yml`](.github/workflows/architecture.yml)).

---

## Architecture (Hexagonal / Ports & Adapters)

```text
expensieve/
├── .github/workflows/
│   ├── architecture.yml           # LikeC4 validation, static site build & GitHub Pages deployment
│   ├── ci.yml                     # Python 3.14 lint (Ruff), strict typecheck (Mypy), Pytest & CLI smoke tests
│   └── reconcile-dry-run.yml      # Manual statement enrichment & staged JSON artifact export
├── architecture/
│   ├── likec4.config.json         # LikeC4 project configuration
│   └── topology.c4                # Auto-generated C4 architecture specification (8 static & dynamic views)
├── ARCHITECTURE.md                # Auto-generated Mermaid system diagrams & extractor reference tables
├── src/expensieve/
│   ├── domain/
│   │   ├── models.py              # Immutable value objects (StatementRow, EnrichmentEvidence, CanonicalTransaction, DebtLinkGroup)
│   │   ├── gazetteer.py           # 57 Namma Metro stations, 14 landmarks, Haversine snapping & address normalization
│   │   ├── rules.py               # Declarative regex & VPA classifier (UPI remarks, food/snacks, medical, investments)
│   │   └── architecture.py        # Pure functional LikeC4 (topology.c4) & ARCHITECTURE.md compiler
│   ├── ports/
│   │   └── protocols.py           # Structural PEP 544 interfaces (StatementSource, EvidenceCollector, ReverseGeocoder, LedgerSink)
│   ├── adapters/
│   │   ├── statements/
│   │   │   ├── hdfc_delimited.py  # Multiline HDFC .txt/.csv parser, UPI narration decomposer & cutoff_ref slicer
│   │   │   ├── hdfc_cc_pdf.py     # Encrypted HDFC Credit Card PDF statement parser
│   │   │   └── fi_federal.py      # Encrypted Fi Money (Federal Bank) savings & FD PDF statement parser
│   │   ├── collectors/
│   │   │   ├── adb_sms.py         # Pixel SMS inbox collector (content://sms/inbox -> 12-digit RRN & exact UTC second)
│   │   │   ├── timeline_geo.py    # Google Maps Timeline.json collector (IN_SUBWAY -> Metro routes, Auto/Cab routes, BharatPe/Vyapar QR venues)
│   │   │   ├── pdf_text.py        # Pure-Python 2D PDF layout & /ToUnicode CMap text extractor (zero external C deps)
│   │   │   ├── gmail_imap.py      # Gmail IMAP collector + 6 pluggable ReceiptExtractor implementations
│   │   │   └── chrome_cdp.py      # Merchant order collector (Zomato, Zepto, Blinkit, Amazon, Uber via Chrome AppleScript / JSON)
│   │   ├── geocoding/
│   │   │   └── nominatim_cache.py # SQLite-backed (~110m grid) OpenStreetMap Nominatim reverse geocoder
│   │   └── sinks/
│   │       └── sourdough_sink.py  # Wallet by BudgetBakers CouchDB sink (via sourdough) with balance assertion & rollback journal
│   ├── services/
│   │   ├── merger.py              # Deterministic EvidencePriority (10..100) resolver & mathematical balance verifier
│   │   └── pipeline.py            # End-to-end SyncPipeline orchestrator
│   └── cli/
│       └── app.py                 # `./sieve` Typer CLI (`sync`, `snap`, and `diagram`)
└── tests/
    ├── fixtures/
    │   └── sample_statement.txt   # Sample HDFC delimited statement for CI smoke tests
    ├── test_architecture.py       # Verifies LikeC4 & ARCHITECTURE.md compilation and sync status
    ├── test_expensieve.py         # Core domain, parser, SMS, Timeline & pipeline balance tests
    └── test_extractors.py         # Unit tests for all Gmail, PDF & merchant order extractors
```

---

## Quick Start

```bash
# Inspect CLI options
./sieve --help

# Regenerate or verify LikeC4 architecture topology & ARCHITECTURE.md
./sieve diagram
./sieve diagram --check

# Launch interactive LikeC4 web explorer locally
npx likec4 start architecture

# Test Namma Metro & Landmark Gazetteer coordinate snapping
./sieve snap "12.9962°, 77.6685°"

# Stage and enrich a bank statement with SMS, Timeline, and merchant caches (exports JSON without touching CouchDB)
./sieve sync \
  --statement tests/fixtures/sample_statement.txt \
  --output dist/staged_transactions.json

# Full enrichment with dry-run validation against Sourdough Wallet sink
./sieve sync \
  --statement ~/Downloads/Acct_Statement_XXXXXXXX0122.txt \
  --cutoff-ref 0000120185081905 \
  --timeline ~/Downloads/Timeline.json \
  --zomato-json .expensieve_cache/zomato_orders.json \
  --zepto-json .expensieve_cache/zepto_orders.json \
  --blinkit-json .expensieve_cache/blinkit_orders.json \
  --push --dry-run

# Live commit to Wallet by BudgetBakers (via Sourdough)
./sieve sync \
  --statement ~/Downloads/Acct_Statement_XXXXXXXX0122.txt \
  --cutoff-ref 0000120185081905 \
  --push --live
```
