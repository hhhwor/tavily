"""跨来源去重:URL 归一化后按 URL 合并,保留信息更全的一条。"""
from __future__ import annotations

from typing import List, Tuple
from urllib.parse import urlsplit, urlunsplit

from src.domain.search import SearchResult

# 常见跟踪参数(归一化时剔除)
_TRACKING_PREFIXES = ("utm_", "spm", "wfr", "for")
_TRACKING_KEYS = {"from", "ref", "source"}


def normalize_url(url: str) -> str:
    """归一化 URL:去 scheme 差异、小写 host、去末尾斜杠、剔除跟踪参数。"""
    if not url:
        return url
    try:
        parts = urlsplit(url if "://" in url else "http://" + url)
    except ValueError:
        return url
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/") or "/"
    # 过滤查询参数中的跟踪项
    kept = []
    for kv in parts.query.split("&"):
        if not kv:
            continue
        key = kv.split("=", 1)[0].lower()
        if key in _TRACKING_KEYS or key.startswith(_TRACKING_PREFIXES):
            continue
        kept.append(kv)
    query = "&".join(kept)
    return urlunsplit(("", host, path, query, ""))


def _richer(a: SearchResult, b: SearchResult) -> SearchResult:
    """合并同 URL 的两条,保留正文更长的那条,并合并来源标记。"""
    keep, other = (a, b) if len(a.content) >= len(b.content) else (b, a)
    if other.source and other.source not in keep.source:
        keep.source = f"{keep.source}+{other.source}"
    return keep


def dedup(results: List[SearchResult]) -> List[SearchResult]:
    """按归一化 URL 去重,保留首次出现顺序。"""
    index: dict[str, int] = {}
    out: List[SearchResult] = []
    for original in results:
        r = original.model_copy(deep=True)
        key = normalize_url(r.url)
        if not key:
            out.append(r)
            continue
        if key in index:
            i = index[key]
            out[i] = _richer(out[i], r)
        else:
            index[key] = len(out)
            out.append(r)
    return out
