"""Internal document-reader result."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class PdfTextPage(BaseModel):
    work_id: str
    status: str
    chunk_index: Optional[int] = None
    page_from: Optional[int] = None
    page_to: Optional[int] = None
    text: Optional[str] = None
    returned_chars: int = 0
    next_cursor: Optional[str] = None
    partial: bool = False
    error_code: Optional[str] = None
    error_message: Optional[str] = None
