# astrbot_plugin_vision_text_bridge

AstrBot 图片转文字桥接插件：把对话中收到的图片自动转成文字描述（MiniMax `mmx vision describe`），再交给对话 LLM 处理；同时暴露 OpenAI 兼容接口，供其他插件作为 vision provider 调用。

## 功能

| 功能 | 说明 |
|------|------|
| 图片拦截转文字 | 钩子拦截 AstrBot LLM 请求中的图片（image_urls / parts / contexts），调 mmx 生成文字描述后注入到请求中 |
| OpenAI 兼容 Provider | 暴露 `/v1/chat/completions` 端点（独立 server 端口 2023），其他插件可将其配置为 vision provider 调用 |
| 图片描述端点 | `/image/caption` 接受 `url` + 可选 `prompt` 参数，直接返回 mmx 文字描述 |
| 三级缓存 | 内存 LRU → SQLite WAL → mmx CLI，同图不同 URL 自动复用 |
| WebUI 缓存管理 | 缩略图预览、搜索、表头排序（URL/描述长度/尺寸/时间）、翻页、导出 JSON、重生成、清空、过期清理 |
| 调用日志面板 | WebUI 中实时查看最近 200 条 API 调用记录（时间/来源/URL/状态/耗时） |
| 提示词优先级 | 调用方传入的 prompt 优先于插件配置默认值 |
| 自动注册 Provider | 启动时通过 Dashboard API 自动注册为 AstrBot provider |
| 权限控制 | 群白名单 / 用户白名单 / 仅私聊 |
| 工具过滤 | 可在 LLM 请求前移除指定的 function tools |
| 链末清理 | 低优先级钩子清理其他插件可能重填的 image_url 残留 |

## 适用场景

- LLM 不支持图片（纯文本模型）
- 降低 token 消耗（文字描述通常 50–300 字，远小于 base64）
- 其他插件需要统一 vision provider 接口
- 需要缓存管理、调用追踪

## 安装

1. AstrBot → 插件管理 → 添加插件，填仓库地址：
   ```
   https://github.com/uuutt2023/astrbot_plugin_vision_text_bridge.git
   ```
2. 首次启动会自动下载 `mmx` CLI 到插件本地目录（需 Node.js >= 18）。
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
| 视觉 | `vision_prompt` | | 通用描述 | 传给 mmx 的默认提示词（调用方可覆盖） |
| 缓存 | `memory_cache_ttl_seconds` | | `300` | 内存缓存 TTL（秒） |
| 缓存 | `memory_cache_max_size` | | `500` | 内存 LRU 条数上限 |
| 缓存 | `sqlite_cache_ttl_days` | | `7` | SQLite 缓存天数 |
| Provider | `openapi_key` | | | Dashboard OpenAPI Key，用于自动注册 provider（推荐） |
| Provider | `webui_username` / `webui_password` | | | 备选，用户名密码登录 |

### 自动注册 Provider

启动时插件会把自身注册为 AstrBot provider，供其他插件通过 OpenAI 接口调用。

推荐方式：
1. Dashboard → 设置 → OpenAPI → 创建 Key（格式 `abk_xxx`）
2. 创建时必须勾选 `provider` scope（否则注册返回 403）
3. 填入 `openapi_key`
4. 重启

不填也能用插件自身功能，只是没法被其他插件作为 provider 调用。

## 端口

| 端口 | 用途 |
|------|------|
| `6185` | AstrBot Dashboard（插件向这里发 provider 注册请求） |
| `2023` | 本插件独立 OpenAI 兼容 server（注册后供其他插件调用） |

外部插件配置示例：

```json
{
  "type": "openai_chat_completion",
  "api_base": "http://127.0.0.1:2023/v1/chat/completions",
  "api_key": "placeholder",
  "model": "vision-bridge"
}
```

## WebUI 缓存管理

路径：Dashboard → 插件管理 → 图片转文字 → 打开页面

功能：
- 缩略图网格预览（懒加载，并发池 6 路）
- 全文搜索 URL / 描述
- 表头点击排序：URL（字母）、描述（长度）、尺寸（像素面积）、创建时间（asc/desc）
- 翻页浏览
- 查看大图、重新生成描述、删除单条
- 一键导出 JSON、清空全部、手动清理过期
- 底部实时调用日志面板（自动刷新）
- 暗色/浅色主题热切换

## 架构

```
用户消息
   │
   ▼
on_llm_request (priority=100)
   │  提取 image_url / parts / contexts
   │  查缓存 (内存 LRU → SQLite WAL)
   │  未命中 → mmx vision describe
   │  注入描述到 extra_user_content_parts
   ▼
对话 LLM（基于描述回答）
   │
   ▼
on_llm_request (priority=-10000)  链末清理残留 base64


外部插件调用
   │
   ▼
POST /v1/chat/completions (独立 server :2023)
   │  提取 messages 中的 image_url + prompt
   │  调用 mmx → 返回纯文本描述
   ▼
外部插件拿到描述继续处理
```

## API 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/v1/chat/completions` | POST | OpenAI 兼容接口，输入 messages（含 image_url），返回纯文本描述 |
| `/image/caption` | GET/POST | 简单描述接口，传 `url` + 可选 `prompt`，返回 JSON `{caption}` |
| `/cache/list` | GET | 缓存列表（支持 search / order_by / 翻页） |
| `/cache/thumbnail/<id>` | GET | 缩略图 |
| `/cache/stats` | GET | 缓存统计 |
| `/cache/diag` | GET | 诊断（DB 路径 / schema / 最近 3 条） |

## 缓存

- 内存 LRU：热路径，默认 500 条，TTL 300 秒
- SQLite WAL：`<plugin_data>/caption_cache.sqlite3`，默认 7 天
- Key：`md5(image_bytes)`，同图不同 URL 自动复用
- 后台清理：每小时扫过期条目

## 常见问题

**Q: 启动日志里看到 `port 2023 已被占用`**
A: 另一进程占用了 2023。检查 `lsof -i :2023`，kill 掉或改端口。

**Q: `provider 注册返回 False`**
A: 看启动日志：
- **403** → OpenAPI Key 缺少 `provider` scope
- **401** → Key 无效或过期
- **422** → 升级到最新版本
- **400 "already exists"** → 实际注册成功

**Q: 改了配置不生效**
A: 清缓存后重启：`find . -name __pycache__ -exec rm -rf {} +`

**Q: WebUI 看不到缩略图**
A: 确认 data URL 图片已正常解码；查看 `/cache/diag` 检查 SQLite 是否有 image_b64。

## 调试日志

| 日志 | 含义 |
|------|------|
| `mmx-cli 本地装成功` | mmx 已就绪 |
| `预登录成功` | MiniMax 认证通过 |
| `webui API 注册成功` | provider 已注册 |
| `webui API 注册返回 False` | 检查 openapi_key |
| `port X 已被占用` | 端口冲突 |

## 协议

MIT
