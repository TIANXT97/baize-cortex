"""Issue #95:事实归卡用到的纯函数(锚点解析、疑问措辞判断)。不碰数据库与模型。"""
import os
import re
import unicodedata
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

RESERVED_ENTITIES = frozenset({"proj:general_decisions"})

ROUTING_ENV = "CORTEX_FACT_ROUTING"
ROUTING_APPLY = "apply"
ROUTING_LOG = "log"

NAME_MIN_CHARS = 4
NAME_REPEAT_CAP = 3
AUTO_MIN_SCORE = 6
AUTO_DOMINANCE = 2.0

_ANCHOR_RE = re.compile(r"^[\s\[\(【「《\"'“‘`]*(proj:[A-Za-z0-9_\-]+)")

_FENCE_RE = re.compile(r"```.*?```", re.S)
_INLINE_CODE_RE = re.compile(r"`[^`]*`")
_URL_RE = re.compile(r"(?:https?|ftp)://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%\-]+", re.I)
_QUERY_MARK_RE = re.compile(r"(?<=[A-Za-z0-9/._%\-])\?(?=[A-Za-z0-9_%\-]+=|&)")
_SQL_PLACEHOLDER_RE = re.compile(
    r"(?:[=<>(,]|\b(?:like|in|limit|offset|values|between|and|or|set)\b)\s*\?", re.I
)
_QUESTION_MARK_RE = re.compile(r"[?？]")
_EXPLORATION_KEYWORDS_RE = re.compile(r"假设|如果|要不|探讨|尚未批准|呢$")


def leading_anchor(text: str) -> Optional[str]:
    match = _ANCHOR_RE.match(unicodedata.normalize("NFKC", text or ""))
    return match.group(1) if match else None


def parse_anchor(text: str) -> Optional[str]:
    anchor = leading_anchor(text)
    return None if anchor in RESERVED_ENTITIES else anchor


def _without_non_question_marks(text: str) -> str:
    text = _FENCE_RE.sub(" ", text)
    text = _INLINE_CODE_RE.sub(" ", text)
    text = _URL_RE.sub(" ", text)
    text = _QUERY_MARK_RE.sub(" ", text)
    return _SQL_PLACEHOLDER_RE.sub(lambda m: m.group(0)[:-1] + " ", text).rstrip()


def is_exploratory(text: str) -> bool:
    text = text or ""
    return bool(
        _QUESTION_MARK_RE.search(_without_non_question_marks(text))
        or _EXPLORATION_KEYWORDS_RE.search(text.rstrip())
    )


def fact_routing_mode() -> str:
    return ROUTING_APPLY if os.environ.get(ROUTING_ENV) == ROUTING_APPLY else ROUTING_LOG


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").lower()


@lru_cache(maxsize=512)
def _ascii_name_re(name: str) -> "re.Pattern[str]":
    return re.compile(r"(?<![a-z0-9])" + re.escape(name) + r"(?![a-z0-9])")


def _occurrences(text: str, name: str) -> int:
    if name.isascii():
        return len(_ascii_name_re(name).findall(text))
    return text.count(name)


def rank_entities(text: str, names_by_entity: Dict[str, List[str]]) -> List[Tuple[str, int]]:
    """按卡名/别名在正文里的命中给各实体打分(名字长度 x 出现次数,单个名字最多算 NAME_REPEAT_CAP 次),高分在前。

    不足 NAME_MIN_CHARS 个字符的名字不参与:两三个字的别名在日常对话里太常见,区分不了卡。
    """
    folded = _fold(text)
    ranked: List[Tuple[str, int]] = []
    for entity_id, names in names_by_entity.items():
        score = 0
        for name in {_fold(n).strip() for n in names}:
            if len(name) >= NAME_MIN_CHARS:
                score += len(name) * min(_occurrences(folded, name), NAME_REPEAT_CAP)
        if score:
            ranked.append((entity_id, score))
    ranked.sort(key=lambda item: (-item[1], item[0]))
    return ranked


def pick_winner(ranked: List[Tuple[str, int]]) -> Optional[str]:
    if not ranked:
        return None
    top = ranked[0][1]
    second = ranked[1][1] if len(ranked) > 1 else 0
    if top >= AUTO_MIN_SCORE and top >= AUTO_DOMINANCE * second:
        return ranked[0][0]
    return None
