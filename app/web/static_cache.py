"""
NoCacheStaticFiles — StaticFiles subclass that injects Cache-Control: no-cache.

目的：根治 heuristic caching。Starlette 原生 StaticFiles 只送 ETag + Last-Modified；
瀏覽器對「有 Last-Modified 但無 Cache-Control」套用 heuristic freshness（約 (now − Last-Modified) × 10%），
在 freshness 窗口內不重驗 → 重新部署後同檔名仍吃舊 JS/CSS，直到窗口過期或使用者 hard-reload。

`no-cache` 不是「不快取」；意為「可存快取，但每次必須帶 If-None-Match / If-Modified-Since 重驗」。
Starlette 已產 ETag + Last-Modified → 未變回 304（空 body），有變才回 200 新版，免 hard-reload。

機制說明（post-construction mutation）：
  super().file_response(...) 回傳 200 FileResponse 或 304 NotModifiedResponse。
  對 200 路徑：直接是 FileResponse 物件，headers dict 可寫。
  對 304 路徑：是 NotModifiedResponse 物件，其 __init__ 在「建構時」已完成白名單過濾，
  建構後的 headers dict 仍可直接寫入（Python MutableHeaders 不設防）。
  我們在 super().file_response() 回傳後才 mutate，__init__ 早已執行完畢，
  白名單不再介入——header 直接寫入即生效。
  因此 200（FileResponse）與 304（NotModifiedResponse）兩條路均會帶 Cache-Control: no-cache。
"""
import os

from fastapi.staticfiles import StaticFiles


# issue #66：app 自己宣告 MIME，不交給 OS registry。
# 精簡版 Windows registry 會把 HKEY_CLASSES_ROOT\.js 的 Content Type 污染成
# text/plain，導致 ES module 被瀏覽器 strict-MIME 拒收。對 /static 下這幾個副檔名
# 一律以 WHATWG canonical MIME 回應，免疫於 OS registry / guess_type 的查表結果。
_FORCED_CONTENT_TYPES = {
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".css": "text/css",
}


class NoCacheStaticFiles(StaticFiles):
    """對 /static 的所有回應加 Cache-Control: no-cache。

    override file_response（同步方法，200 和 304 都經此）。
    在 super().file_response() 回傳後做 post-construction headers mutation，
    兩條回應路徑（200 FileResponse / 304 NotModifiedResponse）均有效。
    """

    def file_response(self, full_path, *args, **kwargs):
        # super 回傳 200 FileResponse 或 304 NotModifiedResponse；
        # 兩者在 __init__ 執行後 headers dict 仍可直接寫入（post-construction mutation）。
        # 注意：NotModifiedResponse.__init__ 的白名單過濾在「建構時」已完成；
        # 我們在建構後才 mutate，故白名單不介入——header 直接寫入即生效。
        response = super().file_response(full_path, *args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        # issue #66：依副檔名查表，命中才 post-construction 覆寫 Content-Type。
        # header 寫死後不再走 guess_type，對任何 mimetypes.init() / registry 重讀免疫。
        ext = os.path.splitext(str(full_path))[1].lower()
        forced = _FORCED_CONTENT_TYPES.get(ext)
        if forced is not None:
            response.headers["Content-Type"] = forced
        return response
