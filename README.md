# astrbot_plugin_vision_text_bridge

把 AstrBot LLM 请求里的图片**统一拦截** → 用 MiniMax CLI (`mmx vision describe`) 跑视觉理解 → 把图片替换为「【图片: 描述内容】」格式文本 → 再发给对话 LLM。

支持多种图片来源：用户发的图、AI 工具调用返回的图、引用上下文的图、`@` 机器人头像等。

## 主要特性

- **图片拦截**：on_llm_request 钩子 (priority=100) 拦截后注入图片 URL
- **视觉理解**：本地装 `mmx` CLI；首次运行自动 `npm install --prefix <plugin>/.mmx`
- **描述缓存**：SQLite WAL + 内存 LRU；md5(image_bytes) 为 key；TTL+清理
- **链尾清理**：priority=-10000 钩子把残留 base64 转写为占位文本
- **OpenAI 兼容 endpoint**：插件提供 `/v1/chat/completions`，让其他插件（图片对话插件等）能直接消费我方的图片理解结果
- **跨插件兼容**：自动检测已安装的图片对话插件并走对应的 endpoint
- **权限控制**：群白名单 / 用户白名单 / 仅私聊 / 工具调用过滤
- **WebUI 管理页**：缓存统计、诊断、清理、媒体完整性扫描、调试模式

## 安装

AstrBot dashboard → 插件管理 → 填仓库地址安装：

```
https://github.com/uuutt2023/astrbot_plugin_vision_text_bridge.git
```

或在 AstrBot 控制台：

```
/plugin install https://github.com/uuutt2023/astrbot_plugin_vision_text_bridge.git
```

首次启动插件会尝试装 `mmx` CLI 到本地目录 `<plugin_dir>/.mmx`（不污染全局 PATH，需 Node.js/npm）。

## 配置

AstrBot dashboard → 我的插件 → **图片转文字** → 配置（详见 `_conf_schema.json`）：

### OpenAI 兼容 provider（如外部插件要消费我方的图片理解）

| 字段 | 说明 |
|---|---|
| `enabled` | 是否启用 OpenAI 兼容 mode |
| `webui_username` | AstrBot dashboard 用户名（用于 webui API login） |
| `webui_password` | AstrBot dashboard 密码（同步到 framework `dashboard.password`） |
| `dashboard_port` | 独立 OpenAI 兼容 server 监听端口（默认 `6188`） |
| `api_key` | OpenAI 兼容接口的 API key（占位字符串即可） |
| `model_name` | 模型昵称（默认 `vision-bridge`） |

### 行为

| 字段 | 说明 |
|---|---|
| `priority` | on_llm_request 钩子优先级（默认 `100`） |
| `enabled` | 总开关 |

### 缓存

| 字段 | 说明 |
|---|---|
| `memory_cache_max_size` | LRU 内存缓存最大条数 |
| `memory_cache_ttl_seconds` | 内存缓存 TTL |
| `sqlite_cache_ttl_days` | SQLite 缓存 TTL（`0` = 禁用过期） |
| `sqlite_clean_interval_hours` | SQLite 清理任务间隔 |

### 兼容性

| 字段 | 说明 |
|---|---|
| `auto_register_openai_compat_provider` | 启动时自动注册 OpenAI 兼容 provider |
| `tool_filter_mode` | 工具调用过滤：`off` / `block_tool_image_generation` / `mute_known_image_tools` |

### 权限

| 字段 | 说明 |
|---|---|
| `enable_group_whitelist` / `group_whitelist` | 群白名单 |
| `enable_user_whitelist` / `user_whitelist` | 用户白名单 |
| `private_chat_only` | 仅私聊 |

### Auto-tagging

| 字段 | 说明 |
|---|---|
| `auto_tag` | 是否启用自动标签生成 |
| `auto_tag_prompt` | 自动标签提示词 |
| `auto_tag_dry_run` | 试运行（不写库） |

## 架构

```
┌────────────────────────────────────────────────────────────────┐
│                         AstrBot framework                       │
└────────────────────────────────────────────────────────────────┘
                            │
   user msg ──► @filter.on_llm_request (priority=100)
                            ▼
   ┌──────────────────────────────────────────────────┐
   │             vision_text_bridge 插件              │
   │                                                  │
   │  1. 提取消息里的所有 image_url / image_bytes      │
   │  2. 查内存 LRU + SQLite (md5) — 命中直接复用     │
   │  3. 未命中: 异步调 mmx vision describe            │
   │  4. 把 description 注入 req.extra_user_content   │
   └──────────────────────────────────────────────────┘
                            ▼
   framework.LLMProvider(对话模型) 直接看 description
                            ▼
   @filter.on_llm_request (priority=-10000) 链尾清理
   strip_residual_base64() 防 LLM hallucination
```

详细架构见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## OpenAI 兼容 endpoint

插件启动时同时跑一个 **独立 HTTP server** on `127.0.0.1:<dashboard_port>`（默认 `6188`）：
- **不需要任何 token / JWT**（loopback only）
- 完全 bypass framework `/api/plug/<plugin>/` 路径上的 JWT middleware
- 这就是为什么外部插件走 OpenAI SDK 注册调用本插件时不再返回 `401 Token 无效`

外部插件（图片对话插件等）配置：
```json
{
  "type": "openai_chat_completion",
  "api_base": "http://127.0.0.1:6188/v1/chat/completions",
  "api_key": "placeholder",
  "model": "vision-bridge"
}
```

我方 endpoint 收到 OpenAI 格式请求 → 解析 image_url → 调 mmx → 包装成 OpenAI ChatCompletion response 返。

如果端口 `6188` 被占用，在配置里改 `dashboard_port`，plugin log 会清晰指示。

## 缓存

- **内存 LRU**（`memory_cache`）：热路径，超快访问
- **SQLite WAL**（`<data_dir>/caption_cache.sqlite3`）：冷路径，跨重启持久化
- **key**：`md5(image_bytes)` —— 同一张图无论 URL 多少次都复用
- **TTL**：内存默认 300s、SQLite 默认 7 天，配置可调
- **后台清理**：默认每 1 小时扫一遍 SQLite 删除过期条目

## 路径

```
data/plugin_data/astrbot_plugin_vision_text_bridge/
  caption_cache.sqlite3           # 描述缓存 DB
  
data/plugins/astrbot_plugin_vision_text_bridge/
  .mmx/node_modules/.bin/mmx      # mmx CLI (本地装)
```

## 调试

Plugin log 配置：

```python
[AstrBot root logger]
  level = DEBUG      # 详情看 [vision_text_bridge] prefix
```

常查日志关键字：

| log line | 含义 |
|---|---|
| `mmx-cli 本地装成功` | mmx 已就位可调用 |
| `✓ solo openai-compat server 启动: http://127.0.0.1:6188/...` | 独立 server 启动 OK |
| `⚠ port X 已被占用` | 端口冲突，调 dashboard_port |
| `✓ 通过 webui API 注册 provider 成功: id=vision_text_bridge_compat` | 外部插件可用 |
| `webui API 注册返回 False — 请检查 webui password` | plugin 没读到 dashboard 密码 |

## 更新日志

详见 [CHANGELOG.md](CHANGELOG.md)。

## 协议

MIT
