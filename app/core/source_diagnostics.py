"""Aggregate data-source + dependency diagnostics for ops UI and agents.

Does not mutate config. Live probes are optional and best-effort (short timeouts).
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from core.config import load_config
from core.logger import get_logger
from core.metatube.state import metatube_state
from core.source_config import SourceConfig, render_name
from core.source_settings import get_enabled_source_ids

logger = get_logger(__name__)

def _build_placeholder_jpeg() -> bytes:
    """Small dark-gray JPEG used when remote covers fail (avoids broken-image icons)."""
    try:
        import io
        from PIL import Image
        img = Image.new("RGB", (40, 56), (40, 40, 44))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=70)
        return buf.getvalue()
    except Exception:
        # Minimal valid 1x1 JPEG fallback
        return bytes.fromhex(
            "ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707070909080a0c"
            "140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222c231c1c2837292c30313434341f27"
            "393d38323c2e333432ffc0000b080001000101011100ffc400140001000000000000000000000000"
            "000003ffc400141001000000000000000000000000000000ffda0008010100003f007fff d9".replace(" ", "")
        )


PLACEHOLDER_JPEG = _build_placeholder_jpeg()


def _check_curl_cffi() -> dict[str, Any]:
    try:
        from curl_cffi import requests as _r  # noqa: F401
        import curl_cffi
        return {
            "ok": True,
            "status": "ok",
            "detail": f"curl_cffi {getattr(curl_cffi, '__version__', '?')} "
                      f"({getattr(curl_cffi, '__curl_version__', '')})",
            "fix_hint": None,
        }
    except Exception as exc:
        return {
            "ok": False,
            "status": "missing_dependency",
            "detail": f"curl_cffi unavailable: {exc}",
            "fix_hint": "重新安装 curl_cffi，或确认嵌入式 Python 的 site-packages 含完整 dist-info 元数据。",
        }


def _install_root() -> Path:
    # app/core → app → OpenAver root
    return Path(__file__).resolve().parents[2]


def _check_ffmpeg() -> dict[str, Any]:
    path = shutil.which("ffmpeg")
    if path:
        return {"ok": True, "status": "ok", "detail": path, "fix_hint": None}
    return {
        "ok": False,
        "status": "missing_dependency",
        "detail": "ffmpeg not found on PATH",
        "fix_hint": "安装 FFmpeg 并加入系统 PATH，下载 m3u8 封装需要它。",
    }


def _check_ytdlp() -> dict[str, Any]:
    bundled = _install_root() / "tools" / "yt-dlp.exe"
    if bundled.is_file():
        return {"ok": True, "status": "ok", "detail": str(bundled), "fix_hint": None}
    path = shutil.which("yt-dlp") or shutil.which("yt-dlp.exe")
    if path:
        return {"ok": True, "status": "ok", "detail": path, "fix_hint": None}
    return {
        "ok": False,
        "status": "missing_dependency",
        "detail": "yt-dlp not found",
        "fix_hint": "将 yt-dlp.exe 放到 OpenAver/tools/ 或加入 PATH。",
    }


def _check_cf_transport() -> dict[str, Any]:
    try:
        from core.cf_transport import get_cf_transport
        t = get_cf_transport()
        if t is None:
            return {
                "ok": False,
                "status": "unavailable",
                "detail": "CF transport not registered (desktop-only)",
                "fix_hint": "JavLibrary 需要桌面版 WebView 反爬通道；网页/服务器模式不可用。",
            }
        return {"ok": True, "status": "ok", "detail": "CF transport registered", "fix_hint": None}
    except Exception as exc:
        return {
            "ok": False,
            "status": "error",
            "detail": str(exc),
            "fix_hint": "重启桌面版 OpenAver 以重新注册 CF 通道。",
        }


def _check_proxy_configured(config: dict) -> dict[str, Any]:
    proxy = (config.get("search") or {}).get("proxy_url") or ""
    if proxy.strip():
        return {"ok": True, "status": "ok", "detail": "proxy_url configured", "fix_hint": None}
    return {
        "ok": False,
        "status": "not_configured",
        "detail": "search.proxy_url is empty",
        "fix_hint": "在设置 → 搜索中配置 HTTP 代理后，DMM 等需代理来源才可使用。",
    }


def _probe_url(
    url: str,
    timeout: float = 6.0,
    use_curl_cffi: bool = False,
    proxy_url: str = "",
) -> dict[str, Any]:
    started = time.monotonic()
    proxies = None
    proxy = (proxy_url or "").strip()
    if proxy and proxy.lower() != "direct":
        proxies = {"http": proxy, "https": proxy}
    try:
        if use_curl_cffi:
            from curl_cffi import requests as curl_requests
            kwargs = {}
            if proxies:
                kwargs["proxies"] = proxies
            resp = curl_requests.get(
                url,
                impersonate="chrome120",
                timeout=timeout,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept-Language": "zh-TW,zh;q=0.9,ja;q=0.8,en;q=0.7",
                },
                **kwargs,
            )
            text = str(resp.text or "")
            code = resp.status_code
        else:
            import requests
            resp = requests.get(
                url,
                timeout=timeout,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                },
                proxies=proxies,
            )
            text = resp.text or ""
            code = resp.status_code
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if "禁止了你的網路所在國家" in text or "prohibited in the country" in text:
            return {
                "ok": False,
                "status": "geo_blocked",
                "http_status": code,
                "latency_ms": elapsed_ms,
                "detail": "站点按地区封锁了当前网络",
                "fix_hint": "配置可访问该站点的代理，并确保对应 scraper 支持走代理。",
            }
        if "Just a moment" in text or "cf-browser-verification" in text:
            return {
                "ok": False,
                "status": "cf_challenge",
                "http_status": code,
                "latency_ms": elapsed_ms,
                "detail": "Cloudflare challenge",
                "fix_hint": "需要浏览器 CF 通道或降低访问频率后重试。",
            }
        if code >= 400:
            return {
                "ok": False,
                "status": "http_error",
                "http_status": code,
                "latency_ms": elapsed_ms,
                "detail": f"HTTP {code}",
                "fix_hint": "检查网络、代理或站点是否可访问。",
            }
        return {
            "ok": True,
            "status": "ok",
            "http_status": code,
            "latency_ms": elapsed_ms,
            "detail": f"HTTP {code}",
            "fix_hint": None,
        }
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return {
            "ok": False,
            "status": "timeout" if "time" in str(exc).lower() else "error",
            "http_status": None,
            "latency_ms": elapsed_ms,
            "detail": str(exc)[:200],
            "fix_hint": "检查网络连通性、DNS 与代理设置。",
        }


_BUILTIN_PROBE_URLS = {
    "dmm": "https://www.dmm.co.jp/",
    "javbus": "https://www.javbus.com/",
    "jav321": "https://www.jav321.com/",
    "javdb": "https://javdb.com/",
    "heyzo": "https://www.heyzo.com/",
    "fc2": "https://adult.contents.fc2.com/",
    "avsox": "https://avsox.click/",
    "d2pass": "https://www.caribbeancom.com/",
    "javlibrary": "https://www.javlibrary.com/cn/",
}


def diagnose_dependencies(config: Optional[dict] = None) -> dict[str, Any]:
    config = config if config is not None else load_config()
    return {
        "curl_cffi": _check_curl_cffi(),
        "ffmpeg": _check_ffmpeg(),
        "yt_dlp": _check_ytdlp(),
        "cf_transport": _check_cf_transport(),
        "proxy": _check_proxy_configured(config),
    }


def diagnose_sources(probe: bool = False) -> dict[str, Any]:
    """Return all configured sources with runtime health + optional live probe."""
    config = load_config()
    deps = diagnose_dependencies(config)
    availability_map = metatube_state.availability_map()
    auto_ids = set(get_enabled_source_ids(availability_map))
    proxy_ok = deps["proxy"]["ok"]
    curl_ok = deps["curl_cffi"]["ok"]
    cf_ok = deps["cf_transport"]["ok"]

    sources_out: list[dict[str, Any]] = []
    raw_sources = config.get("sources") or []
    if not isinstance(raw_sources, list):
        raw_sources = []

    for s in raw_sources:
        if not isinstance(s, dict):
            continue
        try:
            sc = SourceConfig(**s)
        except Exception:
            continue

        sid = sc.id
        # Configured auto-pool membership (enabled + not manual_only + available map)
        in_auto_configured = sid in auto_ids and not sc.is_beta
        issues: list[str] = []
        runtime_status = "ok"
        fix_hint: Optional[str] = None

        if not sc.enabled:
            runtime_status = "disabled"
        elif sc.manual_only:
            runtime_status = "manual_only"
        elif sc.type == "metatube":
            if not metatube_state.is_connected:
                runtime_status = "disconnected"
                issues.append("MetaTube 未连接")
                fix_hint = "在设置中连接 MetaTube 服务。"
            elif not availability_map.get(sid, False):
                runtime_status = "probe_failed"
                issues.append("MetaTube provider 探测失败")
                fix_hint = "在 MetaTube 设置中重新探测 provider。"
        else:
            # builtin soft deps — these make fan-out skip the source even if "enabled"
            if sid == "javdb" and not curl_ok:
                runtime_status = "missing_dependency"
                issues.append("依赖 curl_cffi 不可用")
                fix_hint = deps["curl_cffi"].get("fix_hint")
            elif sid == "dmm" and sc.requires_proxy and not proxy_ok:
                # search_jav factory returns [] without proxy — not actually scraped
                runtime_status = "needs_proxy"
                issues.append("需要代理但未配置（不会进入实际抓取）")
                fix_hint = deps["proxy"].get("fix_hint")
            elif sid == "javdb" and not proxy_ok:
                issues.append("部分地区需代理；未配置 proxy 时可能被地理封锁")
                fix_hint = "若 JavDB 返回地区封锁，请在设置 → 搜索中配置 HTTP 代理（与 DMM 共用 search.proxy_url）。"
            elif sid == "javlibrary" and not cf_ok:
                runtime_status = "needs_cf"
                issues.append("需要桌面 CF 通道")
                fix_hint = deps["cf_transport"].get("fix_hint")

        # Effective auto-pool: configured AND runtime can actually create scrapers
        in_auto_effective = bool(
            in_auto_configured
            and runtime_status == "ok"
            and sc.enabled
            and not sc.manual_only
        )
        # Keep legacy key `in_auto_pool` = effective (what actually fans out)
        in_auto = in_auto_effective

        probe_result = None
        if probe and sc.enabled and sc.type == "builtin" and runtime_status in {
            "ok", "manual_only", "needs_proxy"
        }:
            # Skip probe when we already know hard blockers (except needs_proxy still useful)
            if runtime_status == "missing_dependency":
                pass
            else:
                url = _BUILTIN_PROBE_URLS.get(sid)
                if url:
                    use_cffi = sid == "javdb" and curl_ok
                    if sid == "dmm" and not proxy_ok:
                        probe_result = {
                            "ok": False,
                            "status": "skipped",
                            "detail": "no proxy configured",
                            "fix_hint": deps["proxy"].get("fix_hint"),
                        }
                    else:
                        # Use configured proxy for probes when available
                        proxy = (config.get("search") or {}).get("proxy_url") or ""
                        probe_result = _probe_url(
                            url,
                            use_curl_cffi=use_cffi,
                            proxy_url=proxy if (sid in {"javdb", "dmm"} and proxy) else "",
                        )
                        if not probe_result.get("ok"):
                            if runtime_status == "ok":
                                runtime_status = probe_result.get("status") or "error"
                            issues.append(probe_result.get("detail") or "probe failed")
                            fix_hint = fix_hint or probe_result.get("fix_hint")

        sources_out.append({
            "id": sid,
            "display_name": render_name(sc),
            "type": sc.type,
            "enabled": sc.enabled,
            "order": sc.order,
            "is_beta": sc.is_beta,
            "manual_only": sc.manual_only,
            "requires_proxy": sc.requires_proxy,
            "is_censored": sc.is_censored,
            "in_auto_pool_configured": in_auto_configured,
            "in_auto_pool_effective": in_auto_effective,
            "in_auto_pool": in_auto,  # alias of effective — actual fan-out
            "runtime_status": runtime_status,
            "issues": issues,
            "fix_hint": fix_hint,
            "probe": probe_result,
        })

    sources_out.sort(key=lambda x: x.get("order", 0))
    summary = {
        "total": len(sources_out),
        "enabled": sum(1 for s in sources_out if s["enabled"]),
        "in_auto_pool_configured": sum(1 for s in sources_out if s["in_auto_pool_configured"]),
        "in_auto_pool": sum(1 for s in sources_out if s["in_auto_pool_effective"]),
        "in_auto_pool_effective": sum(1 for s in sources_out if s["in_auto_pool_effective"]),
        "healthy": sum(1 for s in sources_out if s["runtime_status"] == "ok" and s["enabled"]),
        "degraded": sum(
            1 for s in sources_out
            if s["enabled"] and s["runtime_status"] not in {"ok", "disabled", "manual_only"}
        ),
    }
    return {
        "success": True,
        "dependencies": deps,
        "sources": sources_out,
        "summary": summary,
        "probed": probe,
    }
