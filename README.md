# `expensieve` (`./sieve` CLI)

**`expen·sieve`** (*Expense + Sieve / Pensieve*) is a modular, strictly typed, app-agnostic **personal expense enrichment engine** and **multi-source reconciliation pipeline** built on Python 3.14 and `uv`.

It ingests raw bank/card statements (e.g., HDFC Bank) as the immutable accounting spine, cross-references exact timestamps and itemized details across multiple evidence collectors (**Android SMS via ADB**, **Google Maps `Timeline.json`**, **Gmail IMAP receipts & PDFs**, and **Chrome authenticated merchant sessions**), and pushes verified, rupee-accurate records to pluggable ledger backends (starting with **Wallet by BudgetBakers** via [`sourdough`](../sourdough)).

---

## Architecture (Hexagonal / Ports & Adapters)

```text
expensieve/
├── .github/workflows/
│   ├── ci.yml                     # Python 3.14 lint/typecheck/pytest + LikeC4 build & GitHub Pages deploy
│   └── reconcile-dry-run.yml      # Manual statement enrichment & staged JSON artifact export
├── docs/architecture/
│   ├── likec4.config.json         # LikeC4 project configuration
│   └── expensieve.c4              # Full C4 architecture specification (5 static & dynamic views)
├── src/expensieve/
│   ├── domain/
│   │   ├── models.py              # Immutable value objects (StatementRow, EnrichmentEvidence, CanonicalTransaction, DebtLinkGroup)
│   │   ├── gazetteer.py           # 55+ Namma Metro stations, personal landmarks, Haversine snapping & address normalization
│   │   └── rules.py               # Declarative regex & VPA classifier (UPI remarks, food/snacks, medical, investments)
│   ├── ports/
│   │   └── protocols.py           # Structural PEP 544 interfaces (StatementSource, EvidenceCollector, ReverseGeocoder, LedgerSink)
│   ├── adapters/
│   │   ├── statements/
│   │   │   └── hdfc_delimited.py  # Multiline HDFC .txt/.csv parser, UPI narration decomposer & cutoff_ref slicer
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
│       └── app.py                 # `./sieve` Typer CLI (`sync` and `snap`)
└── tests/
    ├── fixtures/
    │   └── sample_statement.txt   # Sample HDFC delimited statement for CI smoke tests
    ├── test_expensieve.py         # Core domain, parser, SMS, Timeline & pipeline balance tests
    └── test_extractors.py         # Unit tests for all Gmail, PDF & merchant order extractors
```

---

## Multi-Source Evidence Priority Hierarchy

`EvidenceMerger` resolves competing evidence for every `StatementRow` using deterministic priority tiers:

| Priority | Enum | Collector / Source | What It Provides |
| :--- | :--- | :--- | :--- |
| **`95`** | `EMAIL_PDF_INVOICE` | `GmailImapCollector` + `pdf_text` | **Namma Yatri** (`invoice_*.pdf`), **Rapido** (`AUTO/CAB_RECEIPT_*.pdf`), **Swiggy** dishes, **Amazon** shipments/pharmacy, **ConfirmTkt/IRCTC/MakeMyTrip** bookings, **Shriram Finance FD & IPO** advices |
| **`85`** | `MERCHANT_WEB_SCRAPE` | `CachedMerchantOrderCollector` | **Zomato** restaurant + dish names, **Zepto** & **Blinkit** itemized groceries/pharmacy, **Amazon** order cache, **Uber** (`Auto` vs `Cab` routes & cancellation fees) |
| **`75`** | `TIMELINE_GEO` | `TimelineGeoCollector` | **Namma Metro** station-to-station routes (`Metro, <Origin> to <Dest>`), Auto/Cab GPS routes, and local QR merchant place names via `Gazetteer` + `Nominatim` |
| **`60`** | `EXPLICIT_UPI_REMARK` | `DeclarativeRuleCollector` | Human-written UPI remarks decomposed from statement narrations |
| **`40`** | `EXACT_TIMESTAMP_SMS` / `RULE_HEURISTIC` | `AdbSmsCollector` / `DeclarativeRuleCollector` | Exact second-level UTC timestamps via 12-digit UPI RRN matching + VPA/keyword categorization |
| **`10`** | `STATEMENT_FALLBACK` | `HdfcDelimitedStatementSource` | Raw payee title-casing and `Needs Review` tag when confidence `< 0.75` |

---

## Interactive Architecture Diagrams (LikeC4)

The full system architecture is modeled in [`docs/architecture/expensieve.c4`](docs/architecture/expensieve.c4) across **5 interactive views**:

1. **`index` (`System Context`)**: `ExpenSieve` ecosystem across `HDFC Bank`, `Google Pixel (ADB)`, `Gmail IMAP`, `Google Chrome`, `Nominatim`, `Sourdough SDK`, and `Wallet by BudgetBakers (CouchDB)`.
2. **`architecture` (`Hexagonal Architecture`)**: Internal layers, PEP 544 `typing.Protocol` ports, pure domain core, and driving/driven adapters.
3. **`collectors` (`Multi-Source Collectors & Extractors`)**: Detailed view of `GmailImapCollector`, the pure-Python `pdf_text` 2D layout engine, and all 6 pluggable extractors.
4. **`domainAndMerger` (`Domain Core & Priority Resolution`)**: Zero-I/O domain models, `Gazetteer` geodesic snapping, and `EvidenceMerger`.
5. **`syncWorkflow` (`Dynamic Pipeline View`)**: 12-step execution trace of `./sieve sync` from statement ingestion to atomic CouchDB commit.

```bash
# Preview the interactive architecture diagrams locally in your browser
npx likec4 start docs/architecture

# Validate the LikeC4 DSL model
npx likec4 validate docs/architecture

# Build static HTML architecture portal
npx likec4 build docs/architecture -o dist/architecture
```

---

## Quick Start

```bash
# Inspect CLI options
./sieve --help

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

---

## Development & CI Quality Gates

Every push and pull request is verified by [`.github/workflows/ci.yml`](.github/workflows/ci.yml):

```bash
# Run linter & formatter check
uv run ruff check src tests
uv run ruff format --check src tests

# Run strict static type checking
uv run mypy src tests

# Run unit & integration test suite
uv run pytest -v
```
