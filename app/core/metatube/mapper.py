"""metatube → Video mapper（spec §5.2）"""
import re

from core.logger import get_logger
from core.scrapers.models import Actress, Video

logger = get_logger(__name__)

# FC2 系 provider 名稱集合
_FC2_PROVIDERS = {"FC2", "fc2hub", "FC2PPVDB"}

# FC2 雜訊 marker regex（全 ASCII，無 \uXXXX 需求）
_FC2_NOISE_RE = re.compile(
    r"(<script|function\(|\{\{|[A-Za-z0-9+/]{40,}={0,2})"
)


def map_movie_info(info: dict) -> Video:
    """metatube MovieInfo dict → OpenAver Video（spec §5.2 完整映射）

    全程 info.get() 容缺：search 精簡結果欠缺欄位時不 raise。
    """
    provider = info.get("provider", "")
    number = info.get("number", "")

    logger.debug("map_movie_info provider=%s number=%s", provider, number)

    # actors：過濾空字串，避免 Actress min_length=1 ValidationError
    raw_actors = info.get("actors") or []
    actresses = [Actress(name=n) for n in raw_actors if n]

    # release_date：可能是 None（JSON null）或 RFC3339 "YYYY-MM-DDT00:00:00Z"
    raw_date = info.get("release_date") or ""
    date = raw_date.split("T")[0]

    # runtime 0 → None（無值不顯示，CD-63a-9 plan 拍板）
    runtime = info.get("runtime") or None

    # score 0.0 → None（無評分不顯示，spec §5.2）
    score = info.get("score") or None

    # summary 清理
    summary = clean_metatube_summary(provider, info.get("summary") or "")

    return Video(
        number=number,
        title=info.get("title", ""),
        maker=info.get("maker", ""),
        director=info.get("director", ""),
        label=info.get("label", ""),
        series=info.get("series", ""),
        actresses=actresses,
        date=date,
        cover_url=info.get("cover_url", ""),
        tags=info.get("genres") or [],
        detail_url=info.get("homepage", ""),
        duration=runtime,
        sample_images=info.get("preview_images") or [],
        rating=score,
        summary=summary,
        source=f"metatube:{provider}",
        # thumb_url / big_thumb_url / preview_video_url / preview_video_hls_url 不吸（defer，spec §6）
    )


def clean_metatube_summary(provider: str, raw: str) -> str:
    """清理 metatube summary 文字

    FC2 系（FC2 / fc2hub / FC2PPVDB）：
      - 截斷至首個雜訊 marker 前（<script、function(、{{、base64 ≥40 chars）
      - strip + 限長 500

    其他 provider：
      - strip + 限長 500

    空字串輸入 → ''
    """
    if not raw:
        return ""

    if provider in _FC2_PROVIDERS:
        m = _FC2_NOISE_RE.search(raw)
        text = raw[: m.start()] if m else raw
    else:
        text = raw

    # strip 前後空白 + 限長 500（Python str slice 是 codepoint-safe，CJK 正確）
    return text.strip()[:500]
