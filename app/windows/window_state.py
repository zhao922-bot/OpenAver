"""
OpenAver PyWebView 視窗狀態持久化
記住關閉前的視窗尺寸、位置、maximized 狀態，下次開啟還原。

儲存於 ~/OpenAver/window_state.json（與 logger 同目錄慣例）。
讀檔失敗、超出 sane bounds、或 pywebview 版本不支援某事件時，靜默 fallback 預設值。
"""
import json
from pathlib import Path
from core.logger import get_logger

logger = get_logger(__name__)

STATE_PATH = Path.home() / "OpenAver" / "window_state.json"

DEFAULT_WIDTH = 1200
DEFAULT_HEIGHT = 800

# Sane bounds（防止 corrupted state 或惡意檔案讓視窗變成 1x1 / 99999x99999）
MIN_W, MIN_H = 640, 480
MAX_W, MAX_H = 8000, 8000


def _default_state() -> dict:
    return {
        "width": DEFAULT_WIDTH,
        "height": DEFAULT_HEIGHT,
        "x": None,
        "y": None,
        "maximized": False,
    }


def load_state() -> dict:
    """讀取上次視窗狀態。失敗時回傳預設值。"""
    try:
        if not STATE_PATH.exists():
            return _default_state()
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"window_state load failed, using default: {e}")
        return _default_state()

    try:
        w = int(data.get("width", DEFAULT_WIDTH))
        h = int(data.get("height", DEFAULT_HEIGHT))
    except (TypeError, ValueError):
        return _default_state()

    if not (MIN_W <= w <= MAX_W and MIN_H <= h <= MAX_H):
        logger.warning(f"window_state out of bounds ({w}x{h}), using default")
        return _default_state()

    x = data.get("x")
    y = data.get("y")
    try:
        x = int(x) if x is not None else None
        y = int(y) if y is not None else None
    except (TypeError, ValueError):
        x, y = None, None

    # 多螢幕拔線後的舊座標可能落在不存在的監視器上 → pywebview 直接傳給 OS、視窗會開在畫面外
    # 超出保守範圍時丟棄座標，改用 OS 預設位置（尺寸/maximized 仍保留）
    if x is not None and not (-MAX_W <= x <= MAX_W):
        x = None
    if y is not None and not (-MAX_H <= y <= MAX_H):
        y = None
    if x is None or y is None:
        x, y = None, None

    return {
        "width": w,
        "height": h,
        "x": x,
        "y": y,
        "maximized": bool(data.get("maximized", False)),
    }


def save_state(state: dict) -> None:
    """寫入視窗狀態到 JSON。失敗時 warning 不 raise。"""
    try:
        clean = _default_state()
        try:
            w = int(state.get("width", DEFAULT_WIDTH))
            h = int(state.get("height", DEFAULT_HEIGHT))
            if MIN_W <= w <= MAX_W and MIN_H <= h <= MAX_H:
                clean["width"] = w
                clean["height"] = h
            else:
                logger.warning("window_state refused invalid size on save: %sx%s", w, h)
        except (TypeError, ValueError):
            logger.warning("window_state refused non-numeric size on save")

        x = state.get("x")
        y = state.get("y")
        try:
            x = int(x) if x is not None else None
            y = int(y) if y is not None else None
        except (TypeError, ValueError):
            x, y = None, None
        if x is not None and y is not None and -MAX_W <= x <= MAX_W and -MAX_H <= y <= MAX_H:
            clean["x"] = x
            clean["y"] = y
        clean["maximized"] = bool(state.get("maximized", False))

        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(
            json.dumps(clean, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning(f"window_state save failed: {e}")


def attach(window, initial_state: dict) -> dict:
    """訂閱 pywebview window events，關閉時寫入最終狀態。

    Args:
        window: pywebview window 實例
        initial_state: load_state() 回傳的初始 state（作為追蹤起點）

    Returns:
        live state dict（測試用；外部不需保留）
    """
    state = dict(initial_state)

    def on_resized(width, height):
        if state["maximized"]:
            return  # maximized 期間的尺寸不覆蓋還原尺寸
        try:
            state["width"] = int(width)
            state["height"] = int(height)
        except (TypeError, ValueError):
            pass

    def on_moved(x, y):
        if state["maximized"]:
            return
        try:
            state["x"] = int(x)
            state["y"] = int(y)
        except (TypeError, ValueError):
            pass

    def on_maximized():
        state["maximized"] = True

    def on_restored():
        state["maximized"] = False

    def on_closing():
        save_state(state)

    # pywebview 不同版本的 events 屬性名稱不完全一致；逐個 try
    _safe_subscribe(window, "resized", on_resized)
    _safe_subscribe(window, "moved", on_moved)
    _safe_subscribe(window, "maximized", on_maximized)
    _safe_subscribe(window, "restored", on_restored)
    _safe_subscribe(window, "closing", on_closing)

    return state


def _safe_subscribe(window, event_name: str, handler) -> None:
    try:
        evt = getattr(window.events, event_name, None)
        if evt is None:
            logger.debug(f"window_state: event '{event_name}' unavailable on this pywebview version")
            return
        evt += handler
    except Exception as e:
        logger.warning(f"window_state: failed to subscribe '{event_name}': {e}")
