"""Issue #95/#103:事实归卡用到的纯函数(锚点解析与归一比对、疑问措辞判断)。不碰数据库与模型。"""
import json
import os
import re
import unicodedata
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

RESERVED_ENTITIES = frozenset({"proj:general_decisions"})

ROUTING_ENV = "CORTEX_FACT_ROUTING"
ROUTING_APPLY = "apply"
ROUTING_LOG = "log"

NAME_MIN_CHARS = 4
NAME_REPEAT_CAP = 3
AUTO_MIN_SCORE = 6
AUTO_DOMINANCE = 2.0
ROUTE_ANSWER_MAX_CHARS = 80
ANCHOR_PREFIX_MIN_CHARS = 2
PAIR_MAX_CHARS = 200

_ANCHOR_RE = re.compile(r"^[\s\[\(【「《\"'“‘`]*(proj:[A-Za-z0-9_\-]+)")
_ANCHOR_NAME_RE = re.compile(r"^[\s\[\(【「《\"'“‘`]*proj:([A-Za-z0-9_\-㐀-鿿]+)")

_FENCE_RE = re.compile(r"```.*?```", re.S)
_INLINE_CODE_RE = re.compile(r"`[^`]*`")
_URL_RE = re.compile(r"(?:https?|ftp)://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%\-]+", re.I)
_QUERY_MARK_RE = re.compile(r"(?<=[A-Za-z0-9/._%\-])\?(?=[A-Za-z0-9_%\-]+=|&)")
_SQL_PLACEHOLDER_RE = re.compile(
    r"(?:[=<>(,]|\b(?:like|in|limit|offset|values|between|and|or|set)\b)\s*\?", re.I
)
_QUESTION_MARK_RE = re.compile(r"[？?]")
_EXPLORATION_KEYWORDS_RE = re.compile(r"假设|如果|要不|探讨|尚未批准|呢$")
_PAIRED_OPEN_TO_CLOSE = {"「": "」", "『": "』", "“": "”", "‘": "’", "《": "》", "〈": "〉", "（": "）", "(": ")"}
_PAIRED_CLOSE_TO_OPEN = {close: opening for opening, close in _PAIRED_OPEN_TO_CLOSE.items()}


def leading_anchor(text: str) -> Optional[str]:
    match = _ANCHOR_RE.match(unicodedata.normalize("NFKC", text or ""))
    return match.group(1) if match else None


def parse_anchor(text: str) -> Optional[str]:
    anchor = leading_anchor(text)
    return None if anchor in RESERVED_ENTITIES else anchor


def anchor_key(name: str) -> str:
    return unicodedata.normalize("NFKC", name or "").casefold().replace("-", "_")


def leading_anchor_name(text: str) -> Optional[str]:
    """开头 proj: 后面的名字(不含前缀,NFKC 后原样保留,可含中文)。没有开头锚点返回 None。"""
    match = _ANCHOR_NAME_RE.match(unicodedata.normalize("NFKC", text or ""))
    return match.group(1) if match else None


def anchor_map_of(entries: Iterable[Tuple[str, Iterable[str]]]) -> Dict[str, Set[str]]:
    """(实体ID, 可作锚点的名字们) -> {归一名: 实体ID 集合}。实体ID 自身的名字与每个别名都参与;保留名不进表。"""
    anchor_map: Dict[str, Set[str]] = {}
    for entity_id, names in entries:
        if entity_id in RESERVED_ENTITIES:
            continue
        own = entity_id[len("proj:"):] if entity_id.startswith("proj:") else entity_id
        for name in (own, *names):
            key = anchor_key(name) if isinstance(name, str) else ""
            if not key or f"proj:{key}" in RESERVED_ENTITIES:
                continue
            anchor_map.setdefault(key, set()).add(entity_id)
    return anchor_map


def _anchor_candidates(name: str) -> Iterable[str]:
    """先整串;再依次取以中文字符为切点的前缀,最长的先试(「demo_node本尊拓扑」先试 demo_node本尊拓扑,再试 demo_node本尊……)。"""
    yield name
    for cut in range(len(name) - 1, ANCHOR_PREFIX_MIN_CHARS - 1, -1):
        if not name[cut].isascii():
            yield name[:cut]


def resolve_anchor(name: str, anchor_map: Dict[str, Set[str]]) -> Tuple[str, Optional[str]]:
    """锚点名对卡。返回 (reserved | matched | ambiguous | unknown, 实体ID)。
    纯 ASCII 名不截取前缀,所以 proj:tailscale2 不会被当成 tailscale。"""
    for candidate in _anchor_candidates(name):
        key = anchor_key(candidate)
        if f"proj:{key}" in RESERVED_ENTITIES:
            return "reserved", None
        hits = anchor_map.get(key)
        if hits:
            if len(hits) == 1:
                return "matched", next(iter(hits))
            return "ambiguous", None
    return "unknown", None


def _blank_paired_spans(text: str) -> str:
    """成对符号之内(同一行、不超过 PAIR_MAX_CHARS 字)整段抹掉。未闭合的开符号不吞后文;嵌套从内层先闭合。"""
    chars = list(text)
    stack: List[Tuple[str, int]] = []
    for index, char in enumerate(chars):
        if char == "\n":
            stack.clear()
            continue
        if char in _PAIRED_OPEN_TO_CLOSE:
            stack.append((char, index))
        elif char in _PAIRED_CLOSE_TO_OPEN or char == '"':
            opener = _PAIRED_CLOSE_TO_OPEN.get(char, '"')
            depth = next((d for d in range(len(stack) - 1, -1, -1) if stack[d][0] == opener), None)
            if depth is not None:
                del stack[depth + 1:]
                _close_span(chars, stack, index)
            elif char == '"':
                stack.append((char, index))
    return "".join(chars)


def _close_span(chars: List[str], stack: List[Tuple[str, int]], end: int) -> None:
    _, start = stack.pop()
    if end - start + 1 <= PAIR_MAX_CHARS:
        for position in range(start, end + 1):
            chars[position] = " "


def _without_non_question_marks(text: str) -> str:
    text = _FENCE_RE.sub(" ", text)
    text = _INLINE_CODE_RE.sub(" ", text)
    text = _URL_RE.sub(" ", text)
    text = _QUERY_MARK_RE.sub(" ", text)
    text = _blank_paired_spans(text)
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

def parse_route_answer(raw: Any, index: Dict[str, str]) -> Tuple[Optional[str], Optional[str]]:
    """模型选卡的返回 -> (目标实体, 可记入报告的答案)。非 JSON、非对象、空串、不在索引里、保留名一律返回 target=None。

    答案只留 entity 字段的字符串并截断,模型的其他输出不外传。
    """
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None, None
    entity = data.get("entity") if isinstance(data, dict) else None
    if not isinstance(entity, str):
        return None, None
    answer = entity.strip()
    shown = answer[:ROUTE_ANSWER_MAX_CHARS] or None
    target = index.get(answer) if answer else None
    if target is None or answer in RESERVED_ENTITIES or target in RESERVED_ENTITIES:
        return None, shown
    return target, shown
