from pathlib import Path

from src.models import SearchResult
from src.pipeline.fusion import rrf_fuse, rrf_prepare


ROOT = Path(__file__).resolve().parents[1]


def test_legacy_rerank_module_is_only_a_compatibility_facade():
    source = (ROOT / "src" / "pipeline" / "rerank.py").read_text()

    assert "class " not in source
    assert "def " not in source
    assert len(source.splitlines()) < 60


def test_production_composition_does_not_import_legacy_rerank_module():
    for relative_path in ("src/bootstrap.py", "src/application/ranking_service.py"):
        source = (ROOT / relative_path).read_text()
        assert "src.pipeline.rerank" not in source


def test_rrf_prepare_and_fuse_share_the_same_prior_semantics():
    results = [
        SearchResult(
            url="https://example.com/a",
            title="short",
            content="x",
            source="one",
            provider_rank=0,
        ),
        SearchResult(
            url="https://example.com/a/",
            title="long",
            content="longer content",
            source="two",
            provider_rank=2,
        ),
    ]

    prepared = rrf_prepare(results)
    fused = rrf_fuse(results)

    assert len(prepared) == len(fused) == 1
    assert fused[0].rerank_score == prepared[0].raw["_rrf_prior"]
    assert prepared[0].raw["_rrf_first_idx"] == 0
    assert prepared[0].source == "two+one"
    assert results[0].raw == results[1].raw == {}
