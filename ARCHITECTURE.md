# ExpenSieve — Live System Architecture & Topology

> **Auto-Generated Architecture-as-Code:** This file and [`architecture/topology.c4`](architecture/topology.c4) are deterministically compiled from [`src/expensieve/domain/architecture.py`](src/expensieve/domain/architecture.py) via `./sieve diagram` and verified in CI by [`.github/workflows/architecture.yml`](.github/workflows/architecture.yml).

## Interactive Drill-Down Explorer (LikeC4 — 8 Views)

For the interactive, Google-Maps-style zoomable graph with **8 perspectives** (`index`, `hexagonal_view`, `collectors_view`, `gmail_pdf_view`, `domain_merger_view`, `sink_view`, `sync_pipeline_flow`, and `geo_snapping_flow`):

```bash
# Regenerate topology and ARCHITECTURE.md at any time:
./sieve diagram

# Verify architecture files are in sync (used in CI):
./sieve diagram --check

# Launch the interactive LikeC4 web explorer locally:
npx likec4 start architecture

# Or build a standalone static HTML site (automated via GitHub Actions -> GitHub Pages):
npx likec4 build architecture --base /expensieve/ -o dist/architecture
```

---

## 1. Overarching System Landscape (Level 1–2)

```mermaid
flowchart LR
    subgraph Sources["☁️ Financial & Telemetry Sources"]
        HDFC[("HDFC Bank NetBanking\nDelimited Statement (.txt / .csv)")]
        PIXEL["📱 Google Pixel (ADB)\ncontent://sms/inbox + Timeline.json"]
        GMAIL[("📧 Gmail IMAP SSL\nPDF & HTML Receipts")]
        CHROME["🌐 Google Chrome\nAuthenticated Order History"]
        OSM[("🗺️ OpenStreetMap Nominatim\nReverse Geocoding")]
    end

    subgraph Engine["⚙️ ExpenSieve (./sieve CLI)"]
        CLI["Typer CLI (./sieve)\nsync | snap | diagram"]
        PIPE["SyncPipeline\nOrchestrator"]
        MERGER["EvidenceMerger\nPriority 10..95 + Balance Math"]
        GAZ["Gazetteer\n55 Metro Stations + 14 Landmarks"]
        CLI --> PIPE --> MERGER
        PIPE --> GAZ
    end

    subgraph Sink["📒 Target Ledger Sink"]
        DOUGH["Sourdough SDK (./dough)\nResolver + Rollback Journal"]
        WALLET[("Wallet by BudgetBakers\nCouchDB (_bulk_docs)")]
        DOUGH ==> WALLET
    end

    HDFC ==> PIPE
    PIXEL & GMAIL & CHROME --> PIPE
    GAZ -. "Cache Miss" .-> OSM
    PIPE ==> DOUGH
```

---

## 2. Multi-Source Evidence Priority Hierarchy (`EvidenceMerger`)

```mermaid
flowchart TB
    ROW["StatementRow Spine\n(Date, Amount in Paise, 12-digit RRN, UPI VPA, Closing Balance)"]
    P95["Priority 95: EMAIL_PDF_INVOICE\nGmailImapCollector + Pure-Python 2D PDF Engine"]
    P85["Priority 85: MERCHANT_WEB_SCRAPE\nCachedMerchantOrderCollector (Zomato, Zepto, Blinkit, Amazon, Uber)"]
    P75["Priority 75: TIMELINE_GEO\nTimelineGeoCollector + Gazetteer (55 Metro Stations)"]
    P60["Priority 60: EXPLICIT_UPI_REMARK\nDecomposed Human UPI Remark"]
    P40["Priority 40: DECLARATIVE_RULE / ADB_SMS\nSecond-Level UTC Timestamp + VPA/Keyword Rules"]
    P10["Priority 10: STATEMENT_FALLBACK\nTitle-Cased Payee + 'Needs Review' Tag (<0.75 Confidence)"]
    CANON["CanonicalTransaction\nVerified: Opening + Σ(Signed Deltas) == Closing"]

    ROW --> P95 & P85 & P75 & P60 & P40 & P10
    P95 & P85 & P75 & P60 & P40 & P10 ==> CANON
```

| Priority | Enum | Adapter / Collector | Extracted Enrichment |
| :--- | :--- | :--- | :--- |
| **`95`** | `EMAIL_PDF_INVOICE` | `GmailImapCollector` + `pdf_text` | **Namma Yatri** (`invoice_*.pdf`), **Rapido** (`AUTO/CAB_RECEIPT_*.pdf`), **Swiggy** dishes, **Amazon** shipments/pharmacy, **ConfirmTkt/IRCTC/MakeMyTrip** bookings, **Shriram Finance FD & IPO** advices, **HDFC Credit Card** InstaAlerts & encrypted PDF statements |
| **`85`** | `MERCHANT_WEB_SCRAPE` | `CachedMerchantOrderCollector` | **Zomato** restaurant + dish names, **Zepto** & **Blinkit** itemized groceries/pharmacy, **Amazon** order cache, **Uber** (`Auto` vs `Cab` routes & cancellation fees) |
| **`75`** | `TIMELINE_GEO` | `TimelineGeoCollector` | **Namma Metro** station-to-station routes (`Metro, <Origin> to <Dest>`), Auto/Cab GPS routes, and local QR merchant place names via `Gazetteer` + `Nominatim` |
| **`60`** | `EXPLICIT_UPI_REMARK` | `DeclarativeRuleCollector` | Human-written UPI remarks decomposed from statement narrations |
| **`40`** | `DECLARATIVE_RULE` | `AdbSmsCollector` / `DeclarativeRuleCollector` | Exact second-level UTC timestamps via 12-digit UPI RRN matching + VPA/keyword categorization |
| **`10`** | `STATEMENT_FALLBACK` | `HdfcDelimitedStatementSource` / `HdfcCreditCardPdfStatementSource` | Raw payee title-casing and `Needs Review` tag when confidence `< 0.75` |

---

## 3. Gmail IMAP & Pure-Python 2D PDF Extractor Pipeline

```mermaid
flowchart LR
    IMAP[("Gmail IMAP SSL\nimap.gmail.com:993")]
    COL["GmailImapCollector\nMIME Decoder & Dispatcher"]
    PDF["pdf_text.py\nRC4-128 Decrypt + zlib FlateDecode + /ToUnicode CMap\n2D (Page, -Y, X) Line Clustering"]
    GAZ["Gazetteer.normalize_address\nLandmark Snapping"]

    subgraph Extractors["7 Pluggable ReceiptExtractor Strategies"]
        NY["NammaYatriPdfExtractor\nfrom:nammayatri.in"]
        RAP["RapidoReceiptExtractor\nfrom:rapido.bike"]
        SWG["SwiggyReceiptExtractor\nfrom:noreply@swiggy.in"]
        AMZ["AmazonEmailExtractor\nfrom:auto-confirm@amazon.in"]
        TRV["TravelBookingExtractor\nConfirmTkt / IRCTC / MMT"]
        INV["InvestmentEmailExtractor\nGullak / INDmoney / CDSL / KFintech / MUFG / Shriram"]
        HCC["HdfcCreditCardExtractor\nalerts@hdfcbank.net / Emailstatements.cards"]
    end

    IMAP ==> COL
    COL --> NY & RAP & SWG & AMZ & TRV & INV & HCC
    NY & RAP & INV & HCC --> PDF --> GAZ
```

| Extractor | Query Filter | Attachment / Body Format | Output Note Format & Category |
| :--- | :--- | :--- | :--- |
| `NammaYatriPdfExtractor` | `from:nammayatri.in` | `invoice_*.pdf` (Multi-page PDF) | `Auto, <Origin> to <Dest>` (`Taxi, Cab`) |
| `RapidoReceiptExtractor` | `from:rapido.bike` | `AUTO/CAB_RECEIPT_*.pdf` + HTML | `Auto/Cab, <Origin> to <Dest>` (`Taxi, Cab`) |
| `SwiggyReceiptExtractor` | `from:noreply@swiggy.in` | HTML / Plaintext table | `<Dish 1>, <Dish 2> (<Restaurant>)` (`Restaurant, fast-food`) |
| `AmazonEmailExtractor` | `from:auto-confirm@amazon.in` | Shipment confirmation emails | `<Item 1>, <Item 2>` (`Pharmacy`, `Groceries`, `Electronics`, `Shopping`) |
| `TravelBookingExtractor` | `ConfirmTkt / IRCTC / MakeMyTrip` | Booking confirmation emails | `Train to <Dest> (<Train>, <Class>, PNR <PNR>)` (`Holiday, trips, hotels`) |
| `InvestmentEmailExtractor` | `Gullak / INDmoney / CDSL / KFintech / MUFG / Shriram` | Gold certs, US Stocks `.xls`, Contract Note `.pdf`, MF & IPO/REIT advices | Transfer to `Investments` (`Gullak Gold+`, `US Stocks`, `Indian Stocks`, `Mutual Fund`, `IPO/REIT`) & `Interests, dividends` |
| `HdfcCreditCardExtractor` | `alerts@hdfcbank.net / Emailstatements.cards` | Encrypted RC4-128 CC PDF + InstaAlerts | Exact UTC timestamps + Transfer to `HDFC Credit Card` |
