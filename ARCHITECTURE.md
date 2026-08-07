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
│            │                            │                       │
│            │ openai SDK POST            │ plugin install        │
│            │ (Bearer placeholder)       │                       │
│            │                            │                       │
│   ┌─────────▼──────────────────────┐    │                       │
│   │ 独立 OpenAI 兼容 server          │◄───┘ register_provider:   │
│   │  127.0.0.1:2023                  │      POST /api/v1/providers
│   │  (Python stdlib asyncio)         │      id=vision_text_bridge_compat
│   │  POST /v1/chat/completions       │      type=openai_chat_completion
│   │  GET  /health                    │      api_base=http://127.0.0.1:2023/...
│   │  (无需 JWT, 仅 loopback)         │      api_key=placeholder
│   │                                  │      model=vision-bridge
│   │  路由 /v1/chat/completions  →     │
│   │  plugin.describe_images() → mmx  │
│   │  返 OpenAI ChatCompletion format │
│   └─────────┬────────────────────────┘
│             │
│             │ Calls plugin.describe_images
│             ▼
┌────────────────────────────────────────────────────────────────┐
│           vision_text_bridge 插件（独立进程）                  │
│                                                                 │
│  mmx_runner.py: mmx 子进程封装                                   │
│  main_server.py: 独立 OpenAI 兼容 server（模拟 AI 模型）        │
│  provider_registration.py: 通过 webui HTTP API 注册 provider    │
│                                                                 │
│  无 LLM 拦截、无缓存、无 WebUI                                  │
└────────────────────────────────────────────────────────────────┘
```

## 数据流

### 外部插件调用我方 endpoint（模拟 AI 模型）

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
  plugin.describe_images(image_urls, prompt)
    → mmx vision describe（并发，信号量限流）
    → 文字描述
  return OpenAI ChatCompletion JSON 响应
  choices[0].message.content = 图片理解内容

↓ 图片对话插件 收到理解内容作为 caption
```

## 模块依赖图

```
main.py
  ├── _ensure_mmx_cli / _login_mmx_if_configured
  │   └── mmx_runner.py (subprocess + 自动安装)
  ├── _start_openai_compat_server
  │   └── main_server.py (loopback server, 模拟 AI 模型)
  ├── _auto_register_provider
  │   └── provider_registration.py (webui HTTP API)
  └── describe_images / describe_one
      └── mmx_runner.py (mmx vision describe)

main_server.py (独立 server)
  └── plugin.describe_images → mmx_runner
```

## 设计原则

1. **不修改、不注入** — 不 mutate framework 内部状态（pm.providers 等），改用 framework 公开 API
2. **零外部依赖** — server 用 Python stdlib（asyncio.start_server），不需要 quart/hypercorn
3. **loopback isolation** — 独立 server 只绑 `127.0.0.1`，无外网暴露
4. **Bypass framework JWT** — 不用 framework `/api/plug/<plugin>/*`，自己起 server
5. **Schema-first config** — 插件配置完全通过 `_conf_schema.json` 暴露给 webui
6. **graceful degrade** — 任何 step 失败不 crash plugin，仅 log warn
7. **模拟 AI 模型** — 对外只暴露标准 OpenAI 协议，底层实现与调用方解耦

## 安全边界

| 层面 | 威胁 | 缓解 |
|---|---|---|
| API endpoint | 未授权访问 | server 只绑 `127.0.0.1`，外网不可达 |
| subprocess | 命令注入 | mmx 子进程参数都是列表，无 shell expand |
| 日志 | Key 泄露 | `sk-` / token 自动脱敏，`redact_sensitive` 可关闭 |
