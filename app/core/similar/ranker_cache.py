"""
core/similar/ranker_cache.py
SimilarRankerCache singleton — 雙重檢查 + RLock pattern

CD-57b-1 / CD-57b-2（plan-57b.md）
"""
from __future__ import annotations

import threading

from core.database import VideoRepository
from core.similar.ranker import SimilarRanker
from core.logger import get_logger

logger = get_logger(__name__)


class SimilarRankerCache:
    _instance: SimilarRanker | None = None
    _lock: threading.RLock = threading.RLock()

    @classmethod
    def get(cls) -> SimilarRanker:
        """Lazy build + 雙重檢查 fast path（無鎖）。

        重建期間 block request（< 1s 可接受）。
        穩態 99.9% 走 fast path，不爭鎖。
        """
        if cls._instance is not None:
            return cls._instance  # fast path，穩態走此分支

        with cls._lock:
            if cls._instance is not None:
                return cls._instance  # 雙重檢查：等鎖期間別人已 build

            corpus = VideoRepository().get_all()
            cls._instance = SimilarRanker(corpus)
            logger.debug(
                "SimilarRankerCache: built corpus with %d videos", len(corpus)
            )
            return cls._instance

    @classmethod
    def invalidate(cls) -> None:
        """清空 cache，下次 get() 重建。立即釋放舊 corpus 記憶體。"""
        with cls._lock:
            cls._instance = None
