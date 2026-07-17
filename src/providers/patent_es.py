"""专利 ES 检索 Provider(houdutech 只读专利集群)。

后端: 对外只读 ES 接口 https://search.houdutech.cn:9243(nginx 强制只读 + IP 白名单)
  - 访问:   POST {base}/{index}/_search(只读端点,落在 nginx 白名单内)
  - 鉴权:   无(访问控制 = AWS 安全组来源 IP 白名单;本机出口 IP 已放行)
  - 默认库: 读别名 epo_docdb_read(当前指向 epo_docdb_v2_20260620,EPO DOCDB,~1.72亿,
            全球多语种)。用别名而非固定版本号,蓝绿切换索引时本侧无需改动。多语种检索:
            通用 patent_name/abstract(icu)+ 分语种 title_zh/abstract_zh(ik_smart)+
            当事人 applicant/inventor 同时打分,中英文都能召回。
  - 字段坑: 标题字段是 `patent_name`,不是 `title`。当事人(applicant/inventor)自
            20260620 起为 **object** `{original, docdb, docdba}`——original 是原文名
            (中文等),docdb/docdba 是标准化罗马字名;旧版是扁平字符串(`; ` 分隔),
            `_extract_names` 两种都兼容,优先取原文。本库无 claims/grant_*/current_holder,
            有 country/status/cpc/family_id;ipc_main 较稀疏(cpc_main 更全)。

实现 SearchProvider 接口,返回 PatentResult(SearchResult 子类),
故可直接复用现有 cross-encoder reranker 对「专利名+摘要」打分。
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import requests

from src.infrastructure.http_errors import external_http_error
from src.models import PatentResult
from src.providers.base import SearchProvider

_DEFAULT_INDEX = "epo_docdb_read"  # 读别名(蓝绿切换时本侧无需改);也可固定版本号 epo_docdb_v2_YYYYMMDD

# _source 字段裁剪(只取映射需要的字段,省带宽;对齐 epo_docdb_v2 schema)
_SOURCE = [
    "patent_name", "abstract", "publication_number", "application_number",
    "applicant", "inventor", "ipc_main", "cpc_main", "country", "status",
    "family_id", "application_date", "publication_date", "patent_type",
    "citation_count",
]
# 检索字段与权重:通用标题最高,通用摘要次之,中文分词字段补 CJK 召回;
# 当事人(20260620+ 可检索)补「公司/机构名」召回(如「华为 折叠屏」按申请人命中)。
_MATCH_FIELDS = [
    "patent_name^3", "abstract^2", "title_zh^2", "abstract_zh",
    "applicant.original^2", "applicant.docdb",
]
# recency bucket → 天数(用于 application_date 范围过滤)
_RECENCY_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}
# 多值字段分隔符(旧版扁平 applicant/inventor 是 `; ` / `；` / `、` 分隔的字符串)
_NAME_SEP = re.compile(r"[;；、]")
# 当事人 object 取名优先级:原文(中文等)> 标准化罗马字 > 二次标准化
_NAME_KEYS = ("original", "docdb", "docdba")
# Google Patents 落地页用的公开号:去掉国别/种类码间的横线(US-2024030484-A1 → US2024030484A1)
_PUB_CLEAN = re.compile(r"[^A-Za-z0-9]")


def _split_names(value: Any) -> List[str]:
    """把 '汪宇; 陈圣立' 这类字符串切成 ['汪宇', '陈圣立'];已是 list 则原样清洗。"""
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [p.strip() for p in _NAME_SEP.split(value) if p.strip()]
    return []


def _extract_names(value: Any) -> List[str]:
    """提取当事人姓名列表,兼容新旧两种 schema。

    - 新(20260620+):object `{original, docdb, docdba}` —— 原文名优先,退化到罗马字名。
    - 旧:扁平字符串 / 列表(`; ` 分隔)。
    """
    if isinstance(value, dict):
        for k in _NAME_KEYS:
            names = _split_names(value.get(k))
            if names:
                return names
        return []
    return _split_names(value)


class PatentEsProvider(SearchProvider):
    name = "patent_es"

    def __init__(
        self,
        base_url: Optional[str] = None,
        index: str = _DEFAULT_INDEX,
        timeout: int = 15,
        verify_tls: bool = True,
        per_page: int = 25,
        http_session: Optional[requests.Session] = None,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.index = index or _DEFAULT_INDEX
        self.timeout = timeout
        self.verify_tls = verify_tls
        self.per_page = max(1, min(per_page, 100))
        self._http = http_session or requests

    def search(
        self, query: str, top_k: int = 10, recency: Optional[str] = None
    ) -> List[PatentResult]:
        query = (query or "").strip()
        if not query or not self.base_url:
            return []

        size = min(top_k or self.per_page, self.per_page)
        must: List[Dict[str, Any]] = [
            {"multi_match": {"query": query, "fields": _MATCH_FIELDS, "type": "best_fields"}}
        ]
        body: Dict[str, Any] = {
            "size": size,
            "_source": _SOURCE,
            "query": {"bool": {"must": must, "filter": self._recency_filter(recency)}},
            # highlight 给摘要片段当 snippet(去标签后用)
            "highlight": {"fields": {"abstract": {"fragment_size": 160, "number_of_fragments": 1}}},
        }

        try:
            resp = self._http.post(
                f"{self.base_url}/{self.index}/_search",
                json=body, timeout=self.timeout, verify=self.verify_tls,
            )
            resp.raise_for_status()
            hits = resp.json().get("hits", {}).get("hits", [])
        except Exception as exc:
            raise external_http_error(self.name, "search", exc) from exc

        return self._normalize(hits)[:size]

    def _recency_filter(self, recency: Optional[str]) -> List[Dict[str, Any]]:
        days = _RECENCY_DAYS.get(recency or "")
        if not days:
            return []
        since = (date.today() - timedelta(days=days)).isoformat()
        return [{"range": {"application_date": {"gte": since}}}]

    def _normalize(self, hits: List[Dict[str, Any]]) -> List[PatentResult]:
        results: List[PatentResult] = []
        for h in hits:
            if not isinstance(h, dict):
                continue
            try:
                results.append(self._to_result(h))
            except Exception:  # 单条异常不影响整体，也不记录不可信 payload
                continue
        return results

    def _to_result(self, hit: Dict[str, Any]) -> PatentResult:
        s = hit.get("_source", {}) or {}
        abstract = s.get("abstract", "") or ""
        pub = s.get("publication_number", "") or ""

        # snippet: 优先用 ES highlight 片段(去 <em> 标签),否则截摘要
        hl_list = (hit.get("highlight", {}) or {}).get("abstract") or []
        snippet = re.sub(r"</?em>", "", hl_list[0]) if hl_list else abstract[:160]

        # 专利无原生网页;用 Google Patents 落地页(公开号去横线,无则留空)
        clean_pub = _PUB_CLEAN.sub("", pub)
        url = f"https://patents.google.com/patent/{clean_pub}" if clean_pub else ""

        return PatentResult(
            url=url,
            title=s.get("patent_name", "") or "",
            snippet=snippet,
            content=abstract,
            date=s.get("application_date", "") or "",
            site="houdutech-patents",
            score=hit.get("_score"),
            source=self.name,
            publication_number=pub,
            application_number=s.get("application_number", "") or "",
            applicant=_extract_names(s.get("applicant")),
            inventor=_extract_names(s.get("inventor")),
            ipc_main=s.get("ipc_main", "") or "",
            cpc_main=s.get("cpc_main", "") or "",
            country=s.get("country", "") or "",
            status=s.get("status", "") or "",
            family_id=str(s.get("family_id", "") or ""),
            application_date=s.get("application_date", "") or "",
            publication_date=s.get("publication_date", "") or "",
            patent_type=s.get("patent_type", "") or "",
            citation_count=s.get("citation_count", 0) or 0,
            raw=s,
        )
