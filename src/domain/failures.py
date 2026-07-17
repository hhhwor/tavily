"""Stable recoverable failure contract."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class SearchFailure(BaseModel):
    stage: str
    source: str = ""
    type: Optional[str] = None
    code: str = ""
    message: str = ""
    recoverable: bool = True
