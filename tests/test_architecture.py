"""Unit tests for the pure LikeC4 (`architecture/topology.c4`) and `ARCHITECTURE.md` compiler."""

from pathlib import Path

from expensieve.cli.app import sync_architecture_diagrams
from expensieve.domain.architecture import compile_architecture_bundle


def test_compile_architecture_bundle_contains_all_views_and_extractors() -> None:
    bundle = compile_architecture_bundle()

    assert "specification {" in bundle.likec4_dsl
    assert "model {" in bundle.likec4_dsl
    assert "views {" in bundle.likec4_dsl

    # Verify all 8 views
    assert "view index {" in bundle.likec4_dsl
    assert "view hexagonal_view of expensieve {" in bundle.likec4_dsl
    assert "view collectors_view of expensieve.collectorAdapters {" in bundle.likec4_dsl
    assert "view gmail_pdf_view of expensieve.collectorAdapters.gmailImapCollector {" in bundle.likec4_dsl
    assert "view domain_merger_view of expensieve.domainLayer {" in bundle.likec4_dsl
    assert "view sink_view of expensieve.sinkAdapters {" in bundle.likec4_dsl
    assert "dynamic view sync_pipeline_flow {" in bundle.likec4_dsl
    assert "dynamic view geo_snapping_flow {" in bundle.likec4_dsl

    # Verify nested Gmail PDF engine & extractors
    assert "extractor pdfTextEngine" in bundle.likec4_dsl
    assert "extractor nammaYatriExtractor" in bundle.likec4_dsl
    assert "extractor rapidoExtractor" in bundle.likec4_dsl
    assert "extractor swiggyExtractor" in bundle.likec4_dsl
    assert "extractor amazonEmailExtractor" in bundle.likec4_dsl
    assert "extractor travelExtractor" in bundle.likec4_dsl
    assert "extractor investmentExtractor" in bundle.likec4_dsl
    assert "extractor hdfcCcExtractor" in bundle.likec4_dsl
    assert "component hdfcCcPdfSource" in bundle.likec4_dsl

    # Verify ARCHITECTURE.md sections
    assert "## 1. Overarching System Landscape" in bundle.architecture_markdown
    assert "## 2. Multi-Source Evidence Priority Hierarchy" in bundle.architecture_markdown
    assert "## 3. Gmail IMAP & Pure-Python 2D PDF Extractor Pipeline" in bundle.architecture_markdown


def test_committed_architecture_files_are_in_sync() -> None:
    root_dir = Path(__file__).resolve().parents[1]
    out_of_sync = sync_architecture_diagrams(root_dir, check_only=True)
    assert out_of_sync == (), (
        f"Architecture diagrams are out of sync: {out_of_sync}. Run `./sieve diagram` to update them."
    )
