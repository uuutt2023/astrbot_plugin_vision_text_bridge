"""vision_bridge_provider.py - 自定义 AstrBot LLM provider, text_chat 走 httpx 调本插件 endpoint。

为什么不用 openai SDK:
  - AstrBot 内置 ProviderOpenAIOfficial 用 openai SDK
  - openai SDK 1.x 校验 api_key 必填, 不接受 "placeholder" 占位
  - 用户环境报 "Missing credentials" 崩插件启动

本模块方案:
  - 自定义 Provider class (继承 astrbot.api.provider.Provider)
  - text_chat 用 httpx.async_client 发 HTTP POST 到本插件 /v1/chat/completions
  - 不依赖 openai SDK, api_key 校验不复存在, 占位 "placeholder" 直接过
  - 用户可以通过 AstrBot -> 模型提供商 -> 对话 -> 选 vision_text_bridge_compat 调用

注册流程:
  - auto_register_provider() 直接 instantiate 本 class + add to provider_manager.provider_insts
  - 不走 load_provider(provider_config) 路径 (会触发 openai SDK 校验)

作者: uuutt
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# 仿 OpenAI 协议 LLMResponse (不依赖 astrbot.core.provider.entities 避免循环 import)
class _SimpleLLMResponse:
    """: 极简 LLMResponse - 只暴露 text_chat 调用方需要的字段."""

    def __init__(self, completion_text: str) -> None:
        self.role = "assistant"
        self.completion_text = completion_text
        self._completion_text = completion_text
        self.result_chain = None
        self.tools_call_args: list = []
        self.tools_call_name: list = []
        self.tools_call_ids: list = []
        self.tools_call_extra_content: dict = {}
        self.reasoning_content = None
        self.reasoning_signature = None
        self.raw_completion = None
        self.is_chunk = False
        self.id = f"vision-bridge-{uuid.uuid4().hex[:24]}"
        self.usage = None

    def __getattr__(self, name: str) -> Any:
        # 兜底: 任何 AstrBot 框架访问的字段都返 None 或 []
        if name.startswith("_"):
            raise AttributeError(name)
        return None


class VisionBridgeProvider:
    """: 自定义 LLM provider - text_chat 走 httpx 调本插件 /v1/chat/completions.

    接口仿 AstrBot Provider 抽象类, 但不继承 (避免 import 循环).
    text_chat 是关键方法 - 其他框架 (smart_imagechat_hub 等) 调它.

    支持的 provider_config 字段:
      - api_base: str (必填) - 本插件 endpoint, e.g. http://localhost:6185/api/plug/.../v1/chat/completions
      - api_key: str (占位, 不校验) - 任何字符串
      - model: str - 任意字符串
    """

    provider_type = "chat_completion"

    def __init__(self, provider_config: dict, provider_settings: dict | None = None) -> None:
        self.provider_config = provider_config
        self.provider_settings = provider_settings or {}
        self.api_base = (provider_config.get("api_base") or "").rstrip("/")
        # 取列表的第一个 key, 没有就用占位
        keys = provider_config.get("key") or []
        self.api_key = keys[0] if keys else (provider_config.get("api_key") or "placeholder")
        self.model = provider_config.get("model") or "vision-bridge"
        # 模型可换 (set_model)
        self._current_model = self.model
        # 内部 httpx client (懒初始化)
        self._client: httpx.AsyncClient | None = None
        # 超时 (秒)
        self.timeout = provider_config.get("timeout", 120)

    @property
    def current_model(self) -> str:
        return self._current_model

    def set_model(self, model: str) -> None:
        self._current_model = model

    def get_current_key(self) -> str:
        return self.api_key

    def set_key(self, key: str) -> None:
        self.api_key = key

    async def get_models(self) -> list[str]:
        """: 返本 provider 支持的模型 (固定 vision-bridge)."""
        return ["vision-bridge"]

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                },
            )
        return self._client

    async def text_chat(
        self,
        prompt: str | None = None,
        session_id: str | None = None,
        image_urls: list[str] | None = None,
        audio_urls: list[str] | None = None,
        func_tool: Any = None,
        contexts: list | None = None,
        system_prompt: str | None = None,
        tool_calls_result: Any = None,
        model: str | None = None,
        extra_user_content_parts: list | None = None,
        tool_choice: str = "auto",
        request_max_retries: int | None = None,
        **kwargs: Any,
    ) -> _SimpleLLMResponse:
        """: 调本插件 /v1/chat/completions 拿 mmx 描述.

        输入:
          - prompt / contexts: 文本 prompt (smart_imagechat_hub 通常传 prompt + image_urls)
          - image_urls: 图片 URL 列表
          - system_prompt: 系统 prompt

        输出:
          - _SimpleLLMResponse: completion_text 字段是 mmx 描述
        """
        # 构造 OpenAI ChatCompletion request body
        messages: list[dict] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        if contexts:
            for c in contexts:
                if isinstance(c, dict):
                    role = c.get("role", "user")
                    content = c.get("content", "")
                    messages.append({"role": role, "content": content})
        # prompt + image_urls 合并成最新一条 user message
        user_content: list[dict] = []
        if image_urls:
            for url in image_urls:
                user_content.append({"type": "image_url", "image_url": {"url": url}})
        if prompt:
            user_content.append({"type": "text", "text": prompt})
        if extra_user_content_parts:
            for part in extra_user_content_parts:
                if isinstance(part, dict):
                    user_content.append(part)
        # : P0 兜底 - 即使空 prompt + 空 image_urls 也要保证 messages 非空 (后端 400)
        if not user_content:
            user_content.append({"type": "text", "text": "(空请求)"})
        if len(user_content) == 1 and user_content[0].get("type") == "text":
            # 纯文本 - 简化为 string
            messages.append({"role": "user", "content": user_content[0]["text"]})
        else:
            messages.append({"role": "user", "content": user_content})

        body = {
            "model": model or self._current_model or self.model,
            "messages": messages,
        }

        # 调本插件 endpoint
        try:
            client = self._get_client()
            resp = await client.post(self.api_base, json=body)
        except Exception as e:
            logger.error("[vision_text_bridge] provider text_chat HTTP 失败: %s", e)
            return _SimpleLLMResponse(f"[vision_text_bridge error] {e}")

        if resp.status_code != 200:
            err_text = resp.text[:200]
            logger.warning(
                "[vision_text_bridge] provider text_chat 非 200 响应: status=%d body=%s",
                resp.status_code, err_text,
            )
            return _SimpleLLMResponse(
                f"[vision_text_bridge error {resp.status_code}] {err_text}"
            )

        try:
            data = resp.json()
        except Exception as e:
            logger.error("[vision_text_bridge] provider text_chat 解析 JSON 失败: %s", e)
            return _SimpleLLMResponse(f"[vision_text_bridge error] JSON 解析失败: {e}")

        # 提取 OpenAI ChatCompletion 响应
        try:
            completion_text = (
                data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
            ) or ""
        except (IndexError, AttributeError, KeyError):
            completion_text = ""

        return _SimpleLLMResponse(completion_text=completion_text)

    async def text_chat_stream(self, *args: Any, **kwargs: Any) -> Any:
        """: 流式暂不支持, 退到非流式."""
        return await self.text_chat(*args, **kwargs)

    async def terminate(self) -> None:
        """: 关闭 httpx client."""
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    # 兼容 AstrBot Provider 基类可能用到的其他方法
    def get_keys(self) -> list[str]:
        return [self.api_key]

    async def get_model(self) -> str:
        return self._current_model


# 暴露 register 用的 metadata
# : 共享常量从 constants.py 导入 (单一定义来源, 避免 main.py 改值后脱节)
try:
    from constants import (
        PROVIDER_ID, DEFAULT_MODEL, PLACEHOLDER_API_KEY,
    )
except ImportError:
    # 沙箱 fallback (constants.py 缺失)
    PROVIDER_ID = "vision_text_bridge_compat"
    DEFAULT_MODEL = "vision-bridge"
    PLACEHOLDER_API_KEY = "placeholder"

PROVIDER_TYPE = "chat_completion"
