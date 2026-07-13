"""
通知中心後端 buffer — 53b

module-level globals:
  _notifications: deque  最多 10 筆，最新的排前面（appendleft）
  _read_ids: set         已讀 id 集合

thread safety: 所有 _notifications / _read_ids 讀寫**必須**經 _lock (RLock)
保護。GIL 只保證單次 C-level op atomic，不保護「迭代 + membership check」「len + clear
+ clear」這類多步驟組合，也無法保證 GET handler 跟 emit 拿到一致的 snapshot
（見 plan-53b CD-53B-1）。
"""

from collections import deque
from typing import Optional
import threading
import uuid
import time

from fastapi import APIRouter

from core.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api", tags=["notifications"])

_lock = threading.RLock()
_notifications: deque = deque(maxlen=10)
_read_ids: set = set()


def emit_notification(
    level: str,
    title_key: str,
    message: str = "",
    task_type: Optional[str] = None,
) -> None:
    """後端各處呼叫此函式新增一筆通知。
    設計為極度輕量（只做 deque.appendleft），不可拋出例外。
    level: "info" | "success" | "warn" | "error"
    """
    notif = {
        "id": str(uuid.uuid4()),
        "timestamp": time.time(),
        "level": level,
        "title_key": title_key,
        "message": message,
        "task_type": task_type,
    }
    with _lock:
        if len(_notifications) == _notifications.maxlen:
            evicted = _notifications[-1]
            _read_ids.discard(evicted["id"])
        _notifications.appendleft(notif)
    logger.debug("[notif] emit level=%s title_key=%s", level, title_key)


def _calc_highest_unread_level(items: list, read_ids: set) -> Optional[str]:
    """計算未讀通知中最高嚴重度。"""
    level_order = {"error": 3, "warn": 2, "success": 1, "info": 0}
    highest = None
    highest_score = -1
    for item in items:
        if item["id"] not in read_ids:
            score = level_order.get(item["level"], 0)
            if score > highest_score:
                highest_score = score
                highest = item["level"]
    return highest


@router.get("/notifications")
async def get_notifications():
    """查詢 buffer 所有通知 + 未讀摘要。"""
    with _lock:
        items = list(_notifications)
        read_snapshot = set(_read_ids)
    enriched = [
        {**item, "is_read": item["id"] in read_snapshot}
        for item in items
    ]
    unread_count = sum(1 for item in items if item["id"] not in read_snapshot)
    highest = _calc_highest_unread_level(items, read_snapshot)
    return {
        "items": enriched,
        "unread_count": unread_count,
        "highest_unread_level": highest,
    }


@router.post("/notifications/read")
async def mark_all_read():
    """把目前 buffer 裡所有通知標為已讀。"""
    with _lock:
        count = 0
        for item in _notifications:
            if item["id"] not in _read_ids:
                _read_ids.add(item["id"])
                count += 1
    return {"ok": True, "marked_count": count}


@router.delete("/notifications")
async def clear_notifications():
    """清空 buffer 所有記錄，同時清空 _read_ids。

    UX 註解：F6 決議——通知是 ephemeral notification buffer cleanup，
    手機通知抽屜般直接清空，不需確認。前端 UI 不彈 confirm。
    """
    with _lock:
        count = len(_notifications)
        _notifications.clear()
        _read_ids.clear()
    return {"ok": True, "cleared_count": count}
