"""Phase 1 候选陈述规范化与保守原子化。"""
from __future__ import annotations

import re
from typing import List, Sequence

from src.models import CandidateClaim

_CLAIM_BREAK = re.compile(r"[；;\n]+|(?<=。)")
_VALUE_UNIT = re.compile(
    r"(?P<value>[-+]?\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<unit>亿美元|亿元|万元|million|billion|GWh|MWh|kWh|Wh/kg|mAh/g|"
    r"kg|mg|km|cm|mm|Hz|°C|％|%|元|g|m|V|A|℃)",
    re.I,
)
_TIME = re.compile(r"(?:19|20)\d{2}(?:年|[-/.]\d{1,2}(?:[-/.]\d{1,2})?)?")
_JURISDICTIONS = {
    "中国": "CN", "美国": "US", "欧洲": "EP", "欧盟": "EU", "日本": "JP",
    "韩国": "KR", "英国": "GB", "德国": "DE", "法国": "FR",
}


def _atomic_parts(text: str) -> List[str]:
    parts = []
    for part in _CLAIM_BREAK.split(text or ""):
        cleaned = part.strip(" \t。")
        if cleaned:
            parts.append(cleaned)
    return parts


def _fill_structured_fields(claim: CandidateClaim) -> CandidateClaim:
    data = claim.model_dump()
    if not claim.value:
        match = _VALUE_UNIT.search(claim.text)
        if match:
            data["value"] = match.group("value").replace(",", "")
            data["unit"] = claim.unit or match.group("unit")
    if not claim.time_scope:
        match = _TIME.search(claim.text)
        if match:
            data["time_scope"] = match.group(0)
    if not claim.jurisdiction:
        for label, code in _JURISDICTIONS.items():
            if label in claim.text:
                data["jurisdiction"] = code
                break
    return CandidateClaim(**data)


def decompose_claims(
    claims: Sequence[CandidateClaim],
    *,
    max_claims: int = 20,
) -> List[CandidateClaim]:
    """按明确句界拆复合陈述；不在规则不足时猜测主谓宾。"""
    if not claims:
        raise ValueError("claims 不能为空")
    output: List[CandidateClaim] = []
    seen_ids: set[str] = set()
    for claim in claims:
        parts = _atomic_parts(claim.text)
        if not parts:
            raise ValueError(f"claim {claim.id!r} 文本为空")
        for index, part in enumerate(parts, 1):
            claim_id = claim.id if len(parts) == 1 else f"{claim.id}.{index}"
            if claim_id in seen_ids:
                raise ValueError(f"claim id 重复: {claim_id}")
            seen_ids.add(claim_id)
            data = claim.model_dump()
            data.update({
                "id": claim_id,
                "text": part,
                "parent_id": claim.parent_id or (claim.id if len(parts) > 1 else None),
            })
            output.append(_fill_structured_fields(CandidateClaim(**data)))
            if len(output) > max_claims:
                raise ValueError(f"原子陈述超过上限 {max_claims}")
    return output
