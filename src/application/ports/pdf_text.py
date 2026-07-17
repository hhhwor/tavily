"""PDF 正文访问 Port。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional, Sequence

from src.application.outcomes import PdfEnrichmentOutcome
from src.models import AcademicResult, PdfTextResponse


class PdfTextGateway(ABC):
    """学术结果 PDF 富化与已抽取正文分页读取边界。"""

    @abstractmethod
    def enrich(
        self,
        papers: Sequence[AcademicResult],
        *,
        include_pdf_text: bool,
        pdf_text_mode: Optional[str] = None,
        pdf_max_results: Optional[int] = None,
        pdf_max_chars_per_result: Optional[int] = None,
        pdf_timeout_ms: Optional[int] = None,
    ) -> PdfEnrichmentOutcome:
        """返回富化后的论文序列及逐篇失败；不得要求调用方依赖输入副作用。"""
        raise NotImplementedError

    @abstractmethod
    def read_page(
        self,
        work_id: str,
        cursor: Optional[str] = None,
        max_chars: Optional[int] = None,
    ) -> PdfTextResponse:
        """读取已经抽取并缓存的 PDF 正文分页，不触发新抽取。"""
        raise NotImplementedError
