"""System 1 决策嗅探器:Drex-Max 主力 + OpenJev 仲裁,经 SystemOne 原生协议调用

云端不可用或调用失败时降级为本地规则,不抛错。
"""
import json
import os
import urllib.request
from typing import Optional, Tuple

PHASE_CATEGORY = {
    "decision_approved": "approval",
    "rejection": "rejection",
    "exploration": "exploration",
    "chitchat": "chitchat",
}

_REJECTION_KEYWORDS = ("不对", "记错了", "推翻", "作废", "不行", "否定")
_APPROVAL_KEYWORDS = ("就按这个", "定案", "拍板", "就这么定", "明天开搞", "就这样做")
_EXPLORATION_KEYWORDS = ("怎么", "能不能", "是否", "探讨", "思路", "如果", "?", "?")

_USER_AGENT = "Baize/Gate-v2.0 (TypeSafe-Client)"
_AMBIGUOUS_NOUL = (0.4, 0.6)
_SHOULD_MEMORIZE = {
    "type": "noul",
    "instructions": (
        "这段文本是否包含需要永久记住的用户个人档案、硬件配置、网络拓扑、否定修正或长期偏好?"
        "重要事实答 1,纯日常闲聊答 0。"
    ),
}
_CATEGORY = {
    "type": "choice",
    "criteria": {
        "decision_approved": "明确拍板定案、就按这个做、确认方案、明天开搞",
        "rejection": "推翻旧定案、否定、作废、错误纠正",
        "exploration": "思路探讨、假设分析、疑问反问、如何实现",
        "chitchat": "日常口水、纯情绪吐槽、临时指令或一次性琐事",
    },
}


def _systemone_call(
    url: str,
    key: str,
    model: str,
    text: str,
    timeout: float,
    proxy: Optional[str],
) -> Tuple[str, float]:
    body = json.dumps({
        "model": model,
        "state": text,
        "questions": {"should_memorize": _SHOULD_MEMORIZE, "category": _CATEGORY},
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            "User-Agent": _USER_AGENT,
        },
        method="POST",
    )
    handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy else []
    opener = urllib.request.build_opener(*handlers)
    with opener.open(req, timeout=timeout) as resp:
        answers = json.loads(resp.read().decode("utf-8"))["answers"]
    phase = answers["category"]["choice"]
    if phase not in PHASE_CATEGORY:
        raise ValueError(f"unknown phase_type: {phase}")
    return phase, float(answers["should_memorize"]["noul"])


class OpenAICompatClient:
    def __init__(self, api_url: str, api_key: str, model: str, timeout: float = 30.0):
        self.api_url = api_url
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def chat(self, system: str, user: str) -> str:
        body = json.dumps({
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }).encode("utf-8")
        req = urllib.request.Request(
            self.api_url,
            data=body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))["choices"][0]["message"]["content"]


def llm_settings_from_env() -> Optional[Tuple[str, str, str]]:
    url = os.environ.get("CORTEX_LLM_URL")
    key = os.environ.get("CORTEX_LLM_KEY")
    if not url or not key:
        return None
    return url, key, os.environ.get("CORTEX_REASONING_MODEL", "cbcn/deepseek-v4.1-flash")


class PhaseDetector:
    def __init__(
        self,
        drex_url: Optional[str] = None,
        drex_key: Optional[str] = None,
        openjev_url: Optional[str] = None,
        openjev_key: Optional[str] = None,
        proxy: Optional[str] = None,
        timeout: float = 5.0,
        drex_model: str = "drex-max",
        openjev_model: str = "openjev-latest",
    ):
        self.drex_url = drex_url
        self.drex_key = drex_key
        self.openjev_url = openjev_url
        self.openjev_key = openjev_key
        self.proxy = proxy
        self.timeout = timeout
        self.drex_model = drex_model
        self.openjev_model = openjev_model

    @classmethod
    def from_env(cls) -> "PhaseDetector":
        return cls(
            drex_url=os.environ.get("CORTEX_SYSTEMONE_DREX_URL"),
            drex_key=os.environ.get("CORTEX_SYSTEMONE_DREX_KEY"),
            openjev_url=os.environ.get("CORTEX_SYSTEMONE_OPENJEV_URL"),
            openjev_key=os.environ.get("CORTEX_SYSTEMONE_OPENJEV_KEY"),
            proxy=os.environ.get("CORTEX_SYSTEMONE_PROXY"),
        )

    def detect_phase(self, text: str) -> Tuple[str, float, str]:
        drex = self._try(self.drex_url, self.drex_key, self.drex_model, text)
        if drex is not None and not _is_ambiguous(drex[1]):
            return _result(*drex)
        openjev = self._try(self.openjev_url, self.openjev_key, self.openjev_model, text)
        if openjev is not None:
            return _result(*openjev)
        if drex is not None:
            return _result(*drex)
        return self._detect_local(text)

    def _try(self, url: Optional[str], key: Optional[str], model: str, text: str) -> Optional[Tuple[str, float]]:
        if not url or not key:
            return None
        try:
            return _systemone_call(url, key, model, text, self.timeout, self.proxy)
        except Exception:
            return None

    def _detect_local(self, text: str) -> Tuple[str, float, str]:
        if any(k in text for k in _REJECTION_KEYWORDS):
            phase, confidence = "rejection", 0.8
        elif any(k in text for k in _APPROVAL_KEYWORDS):
            phase, confidence = "decision_approved", 0.9
        elif any(k in text for k in _EXPLORATION_KEYWORDS):
            phase, confidence = "exploration", 0.6
        else:
            phase, confidence = "chitchat", 0.5
        return phase, confidence, PHASE_CATEGORY[phase]


def _is_ambiguous(noul: float) -> bool:
    return _AMBIGUOUS_NOUL[0] <= noul <= _AMBIGUOUS_NOUL[1]


def _result(phase: str, noul: float) -> Tuple[str, float, str]:
    return phase, noul, PHASE_CATEGORY[phase]
