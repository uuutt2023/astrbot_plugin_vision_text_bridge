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
