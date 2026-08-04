from src.application.document_identity import independent_work_id
from src.application.research_runner import ResearchRunner
from src.domain.evidence import (
    Evidence,
    EvidenceCitation,
    EvidenceLocator,
    EvidencePassage,
    EvidencePatent,
    EvidenceProvenance,
)


def _academic(evidence_id: str, doi: str, version: str) -> Evidence:
    return Evidence(
        id=evidence_id,
        result_id=evidence_id.split(":pdf", 1)[0],
        type="academic",
        passage=EvidencePassage(
            text="same work version",
            snippet_type="pdf_text",
            page_from=1,
            chunk_index=0,
        ),
        citation=EvidenceCitation(doi=doi, work_id="W1"),
        locator=EvidenceLocator(
            document_id="W1",
            version_id=version,
            page_from=1,
            char_start=0,
            char_end=17,
            chunk_index=0,
        ),
    )


def test_version_dedup_and_independent_work_identity_are_separate():
    version_one = _academic(
        "academic:W1:pdf:v1", "https://doi.org/10.1000/EXAMPLE", "v1"
    )
    version_one_duplicate = _academic(
        "academic:W1:pdf:v1-copy", "doi:10.1000/example", "v1"
    )
    version_two = _academic(
        "academic:W1:pdf:v2", "10.1000/example", "v2"
    )
    current = [version_one]

    assert ResearchRunner._merge(current, [version_one_duplicate]) == 0
    assert ResearchRunner._merge(current, [version_two]) == 1
    assert len(current) == 2
    assert independent_work_id(version_one) == independent_work_id(version_two)


def test_patent_family_and_web_syndication_do_not_inflate_independence():
    patents = [
        Evidence(
            id=f"patent:{publication}",
            result_id=f"patent:{publication}",
            type="patent",
            passage=EvidencePassage(text="claim", snippet_type="patent_claim"),
            patent=EvidencePatent(
                publication_number=publication,
                family_id="family-1",
            ),
        )
        for publication in ("CN1A", "US1A")
    ]
    web = [
        Evidence(
            id=f"web:{index}",
            result_id=f"web:{index}",
            type="web",
            passage=EvidencePassage(text="syndicated article"),
            provenance=EvidenceProvenance(
                canonical_url=f"https://site{index}.test/article",
                ownership_group=f"owner-{index}",
                syndication_group="web-content:shared-hash",
                retrieved_at="2026-08-04T00:00:00Z",
            ),
        )
        for index in (1, 2)
    ]

    assert len({independent_work_id(item) for item in patents}) == 1
    assert len({independent_work_id(item) for item in web}) == 1
