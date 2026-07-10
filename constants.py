"""constants.py - 共享常量模块 (避免跨模块 import + fallback 重复定义).

所有需要 PLUGIN_ROUTE_PREFIX / DEFAULT_DASHBOARD_PORT / OPENAI_COMPAT_PATH /
PROVIDER_ID / DEFAULT_MODEL / PLACEHOLDER_API_KEY 的模块统一从这里 import.

: uuutt
"""
from __future__ import annotations

# : AstrBot dashboard 默认端口 — 用户可通过 schema "dashboard_port" 覆盖
DEFAULT_DASHBOARD_PORT = 6185

# : 插件路由前缀 + 端点路径
PLUGIN_NAME = "astrbot_plugin_vision_text_bridge"
PLUGIN_ROUTE_PREFIX = "/api/plug/" + PLUGIN_NAME
OPENAI_COMPAT_PATH = "/v1/chat/completions"
IMAGE_CAPTION_PATH = "/image/caption"

# : OpenAI compatible provider 配置
PROVIDER_ID = "vision_text_bridge_compat"
PROVIDER_TYPE = "vision_bridge_compat"  # : custom type, 避开 openai SDK api_key 校验
DEFAULT_MODEL = "vision-bridge"
PLACEHOLDER_API_KEY = "placeholder"

# : 默认完整 API Base URL
DEFAULT_API_BASE = f"http://localhost:{DEFAULT_DASHBOARD_PORT}{PLUGIN_ROUTE_PREFIX}{OPENAI_COMPAT_PATH}"


# : 独立 OpenAI 兼容 endpoint 端口 (bypass framework legacy_router JWT middleware)
#   独立 aiohttp server on 127.0.0.1:<OPENAI_COMPAT_PORT>
#   framework ProviderOpenAIOfficial + openai SDK call goes here directly, 不走 /api/plug
DEFAULT_OPENAI_COMPAT_PORT = 6188

# : 独立 OpenAI 兼容 endpoint URL — 供框架 openai_chat_completion 实例使用
#   直接走 127.0.0.1:<port>/v1/chat/completions
#   bypass framework legacy_router JWT 验证 (JWT 总是 401 'Token 无效')
DEFAULT_OPENAI_COMPAT_ENDPOINT = f"http://127.0.0.1:{DEFAULT_OPENAI_COMPAT_PORT}/v1/chat/completions"
