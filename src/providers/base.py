"""SearchProvider 抽象基类 —— 所有搜索源实现此接口,输出统一的 SearchResult。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional

from src.models import SearchResult


class SearchProvider(ABC):
    name: str = "base"

    @abstractmethod
    def search(
        self, query: str, top_k: int = 10, recency: Optional[str] = None
    ) -> List[SearchResult]:
        """执行搜索,返回归一化结果列表。

        recency: None / day / week / month / year —— 由 L0 判定,实现需映射到
        各来源自己的时效过滤参数。实现还需自行处理来源特有的限制。
        """
        raise NotImplementedError
