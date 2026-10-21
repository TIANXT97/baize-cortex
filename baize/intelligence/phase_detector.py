"""System 1 决策嗅探器:识别拍板、推翻、探讨与闲聊

配置了云端且调用成功时使用云端结果;否则降级为本地规则,不抛错。
云端接口按 OpenAI 兼容的 chat/completions 格式调用。
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

_SYSTEM_PROMPT = (
    "你是白泽的决策嗅探器。判断用户文本属于哪一类:"
    "decision_approved(拍板定案)、rejection(推翻否定)、exploration(探讨)、chitchat(闲聊)。"
    '只输出 JSON: {"phase_type": ..., "confidence": 0 到 1 之间的小数}'
)


def chat_completion(
    api_url: str,
    api_key: str,
    model: str,
    system: str,
    user: str,
    timeout: float = 5.0,
) -> str:
    body = json.dumps({
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }).encode("utf-8")
    req = urllib.request.Request(
        api_url,
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))["choices"][0]["message"]["content"]


class OpenAICompatClient:
    def __init__(self, api_url: str, api_key: str, model: str, timeout: float = 30.0):
        self.api_url = api_url
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def chat(self, system: str, user: str) -> str:
        return chat_completion(self.api_url, self.api_key, self.model, system, user, self.timeout)


def llm_settings_from_env() -> Optional[Tuple[str, str, str, str]]:
    url = os.environ.get("CORTEX_LLM_URL")
    key = os.environ.get("CORTEX_LLM_KEY")
    if not url or not key:
        return None
    default_model = "cbcn/deepseek-v4.1-flash"
    return (
        url,
        key,
        os.environ.get("CORTEX_DECISION_MODEL", default_model),
        os.environ.get("CORTEX_REASONING_MODEL", default_model),
    )


class PhaseDetector:
    def __init__(
        self,
        api_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: str = "drex-max",
        timeout: float = 5.0,
    ):
        self.api_url = api_url
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> "PhaseDetector":
        settings = llm_settings_from_env()
        if settings is None:
            return cls()
        url, key, decision_model, _ = settings
        return cls(api_url=url, api_key=key, model=decision_model)

    def detect_phase(self, text: str) -> Tuple[str, float, str]:
        if self.api_url and self.api_key:
            try:
                return self._detect_remote(text)
            except Exception:
                pass
        return self._detect_local(text)

    def _detect_remote(self, text: str) -> Tuple[str, float, str]:
        content = chat_completion(self.api_url, self.api_key, self.model, _SYSTEM_PROMPT, text, self.timeout)
        result = json.loads(content)
        phase = result["phase_type"]
        if phase not in PHASE_CATEGORY:
            raise ValueError(f"unknown phase_type: {phase}")
        return phase, float(result.get("confidence", 0.0)), PHASE_CATEGORY[phase]

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
