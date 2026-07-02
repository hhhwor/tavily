"""验证 OpenAlex OA 链接语义拆分:canonical 页面 vs OA 落地页 vs PDF 直链。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.providers.openalex import _extract_oa_links


def test_extract_oa_links_prefers_explicit_fields():
    canonical = "https://doi.org/10.1000/example"
    hit = {
        "best_oa_location": {
            "landing_page_url": "https://publisher.example/article",
            "pdf_url": "https://publisher.example/article.pdf",
        },
        "content_urls": {"pdf": "https://content.openalex.org/works/W1.pdf"},
        "open_access": {"oa_url": "https://publisher.example/article.pdf"},
    }
    oa_url, oa_landing_url, oa_pdf_url = _extract_oa_links(hit, canonical, True)
    assert oa_url == "https://publisher.example/article"
    assert oa_landing_url == "https://publisher.example/article"
    assert oa_pdf_url == "https://content.openalex.org/works/W1.pdf"


def test_extract_oa_links_falls_back_to_canonical_landing():
    canonical = "https://doi.org/10.1000/example"
    oa_url, oa_landing_url, oa_pdf_url = _extract_oa_links(
        {"is_oa": True}, canonical, True
    )
    assert oa_url == canonical
    assert oa_landing_url == canonical
    assert oa_pdf_url == ""


if __name__ == "__main__":
    test_extract_oa_links_prefers_explicit_fields()
    test_extract_oa_links_falls_back_to_canonical_landing()
    print("OK: OpenAlex OA 链接语义通过")
