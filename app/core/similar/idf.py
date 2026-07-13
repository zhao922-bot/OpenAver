import math
from collections import Counter

IDF_HOT_THRESHOLD: float = 0.25


def build_idf(corpus: list[list[str]]) -> dict[str, float]:
    n = len(corpus)
    if n == 0:
        return {}
    df = Counter(t for tags in corpus for t in set(tags))
    if not df:
        return {}
    result: dict[str, float] = {}
    for tag, count in df.items():
        if count / n > IDF_HOT_THRESHOLD:
            result[tag] = 0.0
        else:
            result[tag] = math.log((n + 1) / (count + 1)) + 1
    return result


def idf_jaccard(a: set[str], b: set[str], idf_table: dict[str, float]) -> float:
    numerator = sum(idf_table.get(t, 0.0) for t in a & b)
    denominator = sum(idf_table.get(t, 0.0) for t in a | b)
    if denominator <= 0:
        return 0.0
    return numerator / denominator
