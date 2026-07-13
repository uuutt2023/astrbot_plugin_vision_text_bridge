# astrbot_plugin_vision_text_bridge

把 AstrBot 对话里收到的图片自动转成文字描述（用 MiniMax `mmx vision describe`），再交给对话 LLM 处理。

## 适用场景

- LLM 不支持图片（纯文本模型）
- 想降低 token 消耗（图片描述通常 50–200 字，比 base64 小几个数量级）
- 想让 smart_imagechat_hub 等插件通过统一 OpenAI 接口调用视觉理解

## 安装

1. AstrBot → 插件管理 → 添加插件，填仓库地址：
   ```
   https://github.com/uuutt2023/astrbot_plugin_vision_text_bridge.git
   ```
2. 首次启动会自动下载 `mmx` CLI 到插件本地目录（需 Node.js ≥ 18）。
3. 填入 `minimax_api_key`（必需）。
4. 重启 AstrBot，启用插件。

## 配置

Dashboard → 插件管理 → 图片转文字 → 配置。

| 分组 | 字段 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| 基础 | `enabled` | | `true` | 总开关 |
| 基础 | `priority` | | `100` | 钩子优先级 |
| 鉴权 | `minimax_api_key` | ✅ | | MiniMax API Key |
| 鉴权 | `auto_install_cli` | | `true` | 首次启动自动装 mmx |
| 视觉 | `vision_prompt` | | 通用描述 | 传给 mmx 的提示词 |
| 缓存 | `memory_cache_max_size` | | `128` | 内存 LRU 条数 |
| 缓存 | `sqlite_cache_ttl_days` | | `7` | SQLite 缓存天数 |
| Provider | `openapi_key` | | | Dashboard OpenAPI Key，用于自动注册 provider（推荐） |
| Provider | `webui_username` / `webui_password` | | | 备选，用户名密码登录（优先级低于 openapi_key） |

### 自动注册 Provider

启动时插件会把自己注册为 AstrBot provider，供 smart_imagechat_hub 等其他插件通过 OpenAI 接口调用。

推荐方式：
1. Dashboard → 设置 → OpenAPI → 创建 Key（格式 `abk_xxx`）
2. **创建时必须勾选 `provider` scope**（否则注册返回 403）
3. 填入 `openapi_key`
4. 重启

不填也能用插件自身功能，只是没法被其他插件作为 provider 调用。

## 两个端口

| 端口 | 用途 |
|------|------|
| `6185` | AstrBot Dashboard（插件向这里发注册请求） |
| `2023` | 本插件启动的 OpenAI 兼容 server（注册后供其他插件调用） |

外部插件配置示例：

```json
{
  "type": "openai_chat_completion",
  "api_base": "http://127.0.0.1:2023/v1/chat/completions",
  "api_key": "placeholder",
  "model": "vision-bridge"
}
```

## 架构

```
用户消息
   │
   ▼
on_llm_request(priority=100)
   │  提取 image_url / parts / contexts
   │  查缓存 (内存 LRU → SQLite WAL)
   │  未命中 → mmx vision describe
   │  注入描述到 req.extra_user_content_content_parts
   ▼
对话 LLM（基于描述回答）
   │
   ▼
on_llm_request(priority=-10000)  链末清理残留 base64
```

## 缓存

- 内存 LRU：热路径，默认 128 条
- SQLite WAL：`<plugin_data>/caption_cache.sqlite3`，默认 7 天
- key：`md5(image_bytes)`，同图不同 URL 复用
- 后台清理：每小时扫过期条目

## 常见问题

**Q: 启动日志里看到 `port 2023 已被占用`**
A: 另一进程占用了 2023。检查 `lsof -i :2023`，kill 掉或改端口（需要改 `main_server.py` 和 `provider_registration.py`）。

**Q: `provider 注册返回 False`**
A: 看启动日志里 `POST /api/v1/providers 返回 4xx — body=...` 这一行的响应内容：
- **403** → OpenAPI Key 缺少 `provider` scope，去 Dashboard「设置 → OpenAPI」编辑 Key 勾选
- **401** → Key 无效或过期，重新创建
- **422** → payload 校验失败，升级到最新版本
- **400 "already exists"** → 实际注册成功，重启后可用

**Q: 改了配置不生效**
A: 清 `__pycache__` 后重启：`find . -name __pycache__ -exec rm -rf {} +`（在插件目录下）。

**Q: 看不到任何日志**
A: AstrBot 日志级别设为 `DEBUG`：`[root] level = DEBUG`。

## 调试日志

| 日志 | 含义 |
|------|------|
| `mmx-cli 本地装成功` | mmx 已就绪 |
| `预登录成功` | MiniMax 认证通过 |
| `webui API 注册成功` | provider 已注册 |
| `webui API 注册返回 False` | 检查 openapi_key |
| `port X 已被占用` | 端口冲突，换端口 |

## 协议

MIT