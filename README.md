# astrbot_plugin_vision_text_bridge

拦截 AstrBot LLM 请求中的图片 → 用 MiniMax CLI (`mmx vision describe`) 视觉理解 → 替换为 `【图片: 描述】` 文本 → 发给对话 LLM。

## 安装

AstrBot 插件管理填仓库地址：

```
https://github.com/uuutt2023/astrbot_plugin_vision_text_bridge.git
```

首次启动自动装 `mmx` CLI 到插件本地目录（需 Node.js/npm）。

## 主要特性

- 图片拦截替换：on_llm_request 钩子 (priority=100)
- 视觉理解：本地 mmx CLI，自动安装
- 描述缓存：内存 LRU + SQLite WAL，以 md5(image_bytes) 为 key
- 链末清理：priority=-10000 钩子清除 base64 残留
- OpenAI 兼容 endpoint：独立 HTTP server 提供 `/v1/chat/completions`
- 自动注册：启动时将本插件注册为 AstrBot provider，供 smart_imagechat_hub 等调用
- 权限控制：群/用户白名单、仅私聊、工具过滤
- WebUI 管理页：缓存统计、诊断、清理

## 配置

Dashboard → 插件管理 → 图片转文字 → 配置。

### 关键配置项

| 分组 | 字段 | 说明 |
|------|------|------|
| 基础 | `enabled` | 总开关 |
| 基础 | `priority` | 钩子优先级，默认 100 |
| MiniMax CLI | `minimax_api_key` | MiniMax API Key |
| MiniMax CLI | `auto_install_cli` | 自动装 mmx-cli |
| 图像理解 | `vision_prompt` | mmx 提示词 |
| 缓存 | `memory_cache_max_size` | 内存缓存条数 |
| 缓存 | `sqlite_cache_ttl_days` | SQLite 缓存天数 |
| 跨插件兼容 | `tool_filter_mode` | 工具过滤 off/whitelist/blacklist |

### Provider 自动注册

`openapi_key` 是推荐方式：

1. Dashboard → 设置 → OpenAPI → 创建 Key
2. 填入插件配置 `openapi_key`
3. 重启，插件自动注册为 provider

也支持填 `webui_password`（username/password 登录），优先级低于 `openapi_key`。

## 架构

```
用户消息 ──► on_llm_request(priority=100)
              │
              ├─ 提取 image_url / parts / contexts
              ├─ 查缓存 (内存 → SQLite)
              ├─ 未命中 → mmx vision describe
              ├─ 注入描述到 req.extra_user_content_parts
              │
              ▼
         对话 LLM 基于描述回答
              │
              ▼
         on_llm_request(priority=-10000) 链末清理 base64 残留
```

## OpenAI 兼容 endpoint

插件启动独立 HTTP server on `127.0.0.1:2023`：

- 绕过 AstrBot JWT，loopback only
- 接收 OpenAI 格式请求 → 调 mmx → 返 ChatCompletion 响应

注意两个端口的区别：

- **6185** = AstrBot Dashboard（向这里发请求注册 provider）
- **2023** = 插件自己的 OpenAI server（注册成功后供外部插件调用）

外部插件配置：

```json
{
  "type": "openai_chat_completion",
  "api_base": "http://127.0.0.1:2023/v1/chat/completions",
  "api_key": "placeholder",
  "model": "vision-bridge"
}
```

## 缓存

- 内存 LRU：热路径，TTL 默认 300s
- SQLite WAL：`<plugin_data>/caption_cache.sqlite3`，TTL 默认 7 天
- key：`md5(image_bytes)`，同图不同 URL 复用
- 后台清理：每小时扫过期条目

## 调试

```python
[AstrBot root logger]
  level = DEBUG
```

关键日志：

| 日志 | 含义 |
|------|------|
| `mmx-cli 本地装成功` | mmx 就绪 |
| `预登录成功` | MiniMax 认证通过 |
| `webui API 注册成功` | provider 已注册 |
| `webui API 注册返回 False` | 检查 openapi_key 或 webui_password |
| `port X 已被占用` | 改 dashboard_port |

## 协议

MIT
