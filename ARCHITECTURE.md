# 架构

## 部署视图

```
┌────────────────────────────────────────────────────────────────┐
│                         AstrBot framework                       │
│  ┌──────────────────┐    ┌────────────────────────────────┐    │
│  │ Provider          │    │ WebUI (port 6185)              │    │
│  │ provider_manager  │    │ - /api/v1/providers (webui)    │    │
│  │ pm.providers{}    │    │ - /api/auth/login              │    │
│  └─────────┬─────────┘    └─────────────┬──────────────────┘    │
│            │ mmx-vision-bridge           │                       │
│            │ (via PluginProvider        │                       │
│            │  openai_chat_completion     │                       │
│            │  type registered by        │                       │
│            │  webui HTTP API)            │                       │
└────────────┼────────────────────────────┼─────────────────────┘
             │                            │
             │ openai SDK POST            │ plugin install/install-all
             │ (Bearer placeholder)       │
             │                            │
   ┌─────────▼──────────────────────┐    │
   │ 独立 OpenAI 兼容 server          │    │
   │  127.0.0.1:2023                  │◄───┘
   │  (Python stdlib asyncio)         │      register_provider:
   │  POST /v1/chat/completions       │      POST /api/v1/providers
   │  GET  /health                    │      id=vision_text_bridge_compat
   │  (无需 JWT, 仅 loopback)         │      type=openai_chat_completion
   │                                  │      api_base=http://127.0.0.1:2023/...
   │  路由 /v1/chat/completions  →     │      api_key=placeholder
   │  plugin._describe_one() → mmx →   │      model=vision-bridge
   │  返 OpenAI ChatCompletion format │
   └─────────┬────────────────────────┘
             │
             │ Calls plugin._describe_one
             ▼
┌────────────────────────────────────────────────────────────────┐
│           vision_text_bridge 插件（独立进程）                  │
│                                                                 │
│  on_llm_request (priority=100) 拦截                             │
│   └─ extract image_urls from messages                           │
│   └─ check cache (memory LRU + SQLite)                          │
│   └─ if miss: mmx vision describe (subprocess)                  │
│   └─ inject description as text part                            │
│                                                                 │
│  on_llm_request (priority=-10000) 链尾清理                       │
│   └─ strip residual base64 (防 LLM hallucination)                │
│                                                                 │
│  caption_cache.py: md5(image_bytes) cache                       │
│  mmx_runner.py: subprocess wrapper                              │
│  image_utils.py: URL/bytes 提取                                  │
│  web_api.py: webui 路由（dashboard 内）                          │
│  main_server.py: 独立 OpenAI 兼容 server                         │
│  provider_registration.py: 通过 webui HTTP API 注册              │
│  vision_bridge_provider.py: 自定义 Provider class (兜底)         │
│  tool_filter.py: 工具调用过滤                                    │
│  chat_archive_integration.py: 跨插件协同                         │
└────────────────────────────────────────────────────────────────┘
```

## 数据流

### 1. 用户发图片

```
User: [图片] 这是什么?

↓ AstrBot framework 注入到 LLM request
req.messages = [
  {"role": "user", "content": [
    {"type": "image_url", "image_url": {"url": "https://..."}},
    {"type": "text", "text": "这是什么?"}
  ]}
]

↓ @filter.on_llm_request(priority=100)
plugin._bridge_vision_to_text(req)

  ├─ extract: image_urls = ["https://..."]
  ├─ for url in image_urls:
  │   ├─ check memory LRU (md5 hash)
  │   ├─ check SQLite (md5 hash)
  │   └─ if miss: plugin._describe_one(url)
  │       ├─ mmx_path = "/AstrBot/data/plugins/<plugin>/.mmx/node_modules/.bin/mmx"
  │       └─ subprocess: mmx vision describe <url>
  │           stdout: "一只小猫"
  ├─ captions = ["一只小猫"]
  └─ req.extra_user_content_parts.append(
       TextPart("【图片: 一只小猫】")
     )

↓ framework.LLMProvider(对话模型) 收到
messages = [
  {"role": "user", "content": "这是什么?\n【图片: 一只小猫】"}
]

↓ @filter.on_llm_request(priority=-10000)
plugin._strip_residual_base64(req)
  └─ remove any leftover base64 image data
```

### 2. 外部插件消费我方 endpoint

```
图片对话插件 config:
  type = openai_chat_completion
  api_base = http://127.0.0.1:2023/v1/chat/completions
  api_key = "placeholder"
  model = "vision-bridge"

↓ 图片对话插件 caption 流程
  framework creates ProviderOpenAIOfficial (type=openai_chat_completion)
  ProviderOpenAIOfficial.text_chat() calls openai SDK
  openai SDK POST http://127.0.0.1:2023/v1/chat/completions
    Authorization: Bearer placeholder  ← JWT middleware skip (loopback)

↓ 我方 server (main_server.py)
  parse HTTP request → JSON body
  extract image_urls from messages[].content[]
  plugin._describe_one(url) → mmx → description
  return OpenAI ChatCompletion JSON response

↓ 图片对话插件 收到 description 作为 caption
```

## 模块依赖图

```
main.py
  ├── _bridge_vision_to_text (核心业务)
  │   ├── caption_cache.py (LRU + SQLite)
  │   ├── image_utils.py (URL/bytes)
  │   ├── mmx_runner.py (subprocess)
  │   ├── tool_filter.py
  │   └── chat_archive_integration.py
  ├── _auto_register_sih_provider
  │   └── provider_registration.py
  │       └── webui API (httpx) - bypass framework JWT
  ├── _register_web_apis
  │   └── web_api.py (framework /api/plug/<plugin>/*)
  └── start_solo_server
      └── main_server.py (loopback server)

provider_registration.py (独立 register)
  └── httpx → framework /api/auth/login + /api/v1/providers

main_server.py (独立 server)
  └── plugin._describe_one

vision_bridge_provider.py (兜底)
  └── httpx → loopback server
```

## 设计原则

1. **不修改、不注入** — 不 mutate framework 内部状态（pm.providers 等），改用 framework 公开 API
2. **零外部依赖** — server 用 Python stdlib（asyncio.start_server），不需要 quart/hypercorn
3. **loopback isolation** — 独立 server 只绑 `127.0.0.1`，无外网暴露
4. **Bypass framework JWT** — 不用 framework `/api/plug/<plugin>/*`，自己起 server
5. **Schema-first config** — plugin 配置完全通过 `_conf_schema.json` 暴露给 webui（包括 webui_password）
6. **多层 cache** — 内存 LRU 热路径 + SQLite 冷路径，避免重复 mmx 调用
7. **graceful degrade** — 任何 step 失败不 crash plugin，仅 log warn

## 安全边界

| 层面 | 威胁 | 缓解 |
|---|---|---|
| WebUI | 密码泄露 | password 字段在 webui 不展示（security best practice）；本插件 `_conf_schema.json` 提供 webui_password field |
| API endpoint | 未授权访问 | server 只绑 `127.0.0.1`，外网不可达 |
| 缓存 | SQLite 数据泄露 | 缓存只含 mmx 视觉描述 + image hash，无原始图 |
| subprocess | 命令注入 | mmx 子进程参数都是列表，无 shell expand |
