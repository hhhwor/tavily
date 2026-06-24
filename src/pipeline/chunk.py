"""L3 文档分块:将长文本切成段落级 chunk,供段落级重排使用。

策略:按段落 → 句子 → 字符三级拆分,合并短段落,保留重叠。
"""
from __future__ import annotations

import re
from typing import List

# 中英文句子结束符
_SENT_END = re.compile(r"[。！？.!?]")


def chunk_text(
    text: str,
    max_chars: int = 400,
    overlap: int = 50,
) -> List[str]:
    """将文本切成不超过 *max_chars* 字符的 chunk 列表。

    策略:
      1. 按空行(\\n\\n)拆段落
      2. 段落超长 → 按句子再拆
      3. 句子仍超长 → 按字符硬切
      4. 合并相邻短段落直到接近 max_chars
      5. 相邻 chunk 保留 *overlap* 字符重叠(避免语义截断)

    始终返回 ≥1 个 chunk(空文本返回 [""])。
    """
    if not text or not text.strip():
        return [""]

    # --- 第一步:按段落拆 ---
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]

    # --- 第二步:段落超长则按句子拆 ---
    pieces: List[str] = []
    for para in paragraphs:
        if len(para) <= max_chars:
            pieces.append(para)
            continue
        # 按句子拆
        sentences = _split_sentences(para)
        pieces.extend(sentences)

    # --- 第三步:句子仍超长则按字符硬切 ---
    final_pieces: List[str] = []
    for p in pieces:
        if len(p) <= max_chars:
            final_pieces.append(p)
        else:
            for i in range(0, len(p), max_chars):
                final_pieces.append(p[i : i + max_chars])

    if not final_pieces:
        return [""]

    # --- 第四步:合并短段落(预留 overlap 空间) ---
    merge_limit = max(max_chars - overlap, max_chars // 2) if overlap > 0 else max_chars
    chunks = _merge_short(final_pieces, merge_limit)

    # --- 第五步:添加重叠 ---
    if overlap > 0 and len(chunks) > 1:
        chunks = _add_overlap(chunks, overlap)

    return chunks


def _split_sentences(text: str) -> List[str]:
    """按句子结束符拆分,保留标点在句尾。"""
    parts: List[str] = []
    start = 0
    for m in _SENT_END.finditer(text):
        end = m.end()
        parts.append(text[start:end])
        start = end
    # 尾部无标点的部分
    if start < len(text):
        tail = text[start:].strip()
        if tail:
            parts.append(tail)
    return parts or [text]


def _merge_short(pieces: List[str], max_chars: int) -> List[str]:
    """合并相邻短 piece 直到接近 max_chars。"""
    chunks: List[str] = []
    buf = ""
    for p in pieces:
        # 加上分隔符后的长度
        candidate = f"{buf}\n{p}" if buf else p
        if len(candidate) <= max_chars:
            buf = candidate
        else:
            if buf:
                chunks.append(buf)
            # 当前 piece 本身可能就超长(已被硬切过),直接入队
            buf = p if len(p) <= max_chars else p[:max_chars]
    if buf:
        chunks.append(buf)
    return chunks


def _add_overlap(chunks: List[str], overlap: int) -> List[str]:
    """给第 2+ 个 chunk 前面拼上前一个 chunk 的尾部 *overlap* 字符。"""
    result = [chunks[0]]
    for i in range(1, len(chunks)):
        prev_tail = chunks[i - 1][-overlap:]
        result.append(f"{prev_tail}{chunks[i]}")
    return result
