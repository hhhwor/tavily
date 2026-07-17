from pathlib import Path

import pytest
from pydantic import ValidationError

from src.domain.evidence import Answerability, Evidence, EvidencePassage
from src.domain.search import SearchPlan, SearchResult
from src.models import Evidence as LegacyEvidence
from src.models import SearchResult as LegacySearchResult


ROOT = Path(__file__).resolve().parents[1]


def test_legacy_models_module_is_only_a_compatibility_facade():
    source = (ROOT / "src" / "models.py").read_text()

    assert "class " not in source
    assert len(source.splitlines()) < 50
    assert LegacyEvidence is Evidence
    assert LegacySearchResult is SearchResult


def test_production_modules_import_the_owning_model_package():
    offenders = []
    for path in (ROOT / "src").rglob("*.py"):
        if path.name == "models.py":
            continue
        source = path.read_text()
        if "src.models" in source:
            offenders.append(str(path.relative_to(ROOT)))

    assert offenders == []


def test_search_plan_freezes_collections_and_fields():
    plan = SearchPlan(
        raw_query="raw",
        normalized_query="normalized",
        providers=["one", "two"],
        failures=[],
    )

    assert plan.providers == ("one", "two")
    assert plan.failures == ()
    with pytest.raises(ValidationError):
        plan.top_k = 20  # type: ignore[misc]


def test_closed_domain_statuses_reject_invalid_strings():
    with pytest.raises(ValidationError):
        Evidence(
            id="e1",
            result_id="r1",
            type="blog",
            passage=EvidencePassage(text="text"),
        )
    with pytest.raises(ValidationError):
        Answerability(status="maybe")
