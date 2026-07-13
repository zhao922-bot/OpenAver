from typing import Literal


def cast_bucket(actresses: list[str]) -> Literal["solo", "duo", "multi", "none"]:
    n = len(actresses)
    if n == 0:
        return "none"
    if n == 1:
        return "solo"
    if n == 2:
        return "duo"
    return "multi"
