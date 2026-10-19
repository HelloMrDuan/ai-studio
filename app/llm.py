"""统一文字模型客户端（OpenAI 兼容：llama.cpp / DeepSeek / 百炼 等）。"""
from __future__ import annotations

import json
import re
from typing import Any

import httpx

from .config import get_settings
from .gpu import gpu


def extract_json(text: str) -> Any:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        start, end = text.find(open_c), text.rfind(close_c)
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("模型输出不是合法 JSON")


async def _complete(messages: list[dict], *, json_mode: bool, temperature: float, max_tokens: int) -> str:
    s = get_settings()
    body: dict[str, Any] = {
        "model": s.llm_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    if s.llm_provider == "local":
        body["chat_template_kwargs"] = {"enable_thinking": False}
    headers = {"Authorization": f"Bearer {s.llm_api_key}"}
    async with httpx.AsyncClient(timeout=s.llm_timeout, trust_env=False) as c:
        r = await c.post(s.llm_base_url.rstrip("/") + "/chat/completions", json=body, headers=headers)
        if r.status_code >= 400:
            raise RuntimeError(f"文字模型返回错误 {r.status_code}：{r.text[:500]}")
        return r.json()["choices"][0]["message"]["content"] or ""


async def chat_text(system: str, user: str, *, temperature: float = 0.8, max_tokens: int = 8192) -> str:
    async with gpu.use("llm", "文字模型生成中"):
        out = await _complete([{"role": "system", "content": system}, {"role": "user", "content": user}],
                              json_mode=False, temperature=temperature, max_tokens=max_tokens)
    return re.sub(r"<think>.*?</think>", "", out, flags=re.S).strip()


async def chat_json(system: str, user: str, *, validate=None, temperature: float = 0.6,
                    max_tokens: int = 8192, retries: int = 2) -> Any:
    """要求模型输出 JSON；解析或校验失败时把错误反馈给模型重试。"""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    last_error = ""
    async with gpu.use("llm", "文字模型生成中"):
        for _ in range(retries + 1):
            raw = await _complete(messages, json_mode=True, temperature=temperature, max_tokens=max_tokens)
            try:
                data = extract_json(raw)
                if validate:
                    data = validate(data)
                return data
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                messages += [
                    {"role": "assistant", "content": raw[:6000]},
                    {"role": "user", "content": f"上一次输出有问题：{last_error}。请修正后只输出完整 JSON。"},
                ]
    raise RuntimeError(f"文字模型多次输出不合格：{last_error}")
