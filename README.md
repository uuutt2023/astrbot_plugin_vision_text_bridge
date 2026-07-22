# 图片转文字插件

把对话中收到的图片自动转成文字描述，让不支持图片的纯文本 LLM 也能"看到"图片。

同时也是一套完整的 vision 服务，内置 OpenAI 兼容接口 + WebUI 缓存管理 + 调用追踪，供其他插件集成调用。

---

## 它能做什么

### 核心功能

**图片自动转文字** — 有人在群里发图，插件拦住 LLM 请求，先调用 MiniMax `mmx vision describe` 把图变成文字描述，再把描述交给 LLM 处理。整个过程对用户透明。

**三级缓存** — 同一张图（不管 url 还是 file://，md5 相同就算同一张）不会重复调用 mmx：

```
内存 LRU（最快）→ SQLite 持久化 → mmx CLI（最慢）
```

**OpenAI 兼容 provider** — 启动时自动注册为 AstrBot provider，其他插件可以通过标准 `/v1/chat/completions` 接口调用。支持传入自定义 prompt，优先级高于默认值。

**WebUI 缓存管理** — Dashboard 内嵌面板：缩略图预览、全文搜索、表头排序、翻页、导出 JSON、重生成描述、过期清理。暗色/浅色主题热切换。

### 辅助能力

| 功能 | 说明 |
|------|------|
| 调用日志 | 面板实时显示最近 200 条 API 调用，来源/URL/状态/耗时一目了然，重启不丢失 |
| 权限控制 | 群白名单 / 用户白名单 / 仅私聊 |
| 工具过滤 | 白名单/黑名单模式，控制哪些 function tools 交给 LLM |
| 链末清理 | 低优先级钩子兜底，清除其他插件可能重填的 image_url 残留 |

---

## 适用场景

- 用纯文本模型（不支持图片）但又想让它处理图片
- 想省 token（文字描述通常 50–300 字，比 base64 小得多）
- 其他插件需要一个统一的 vision provider
- 需要看图片描述的调用记录和缓存状态

## 适用人群

本插件调用 MiniMax `mmx vision describe`，底层使用 [MCP API-vlm 模型](https://platform.minimaxi.com/docs/guides/pricing-paygo#mcp)进行图片理解。

| 方案 | 说明 |
|------|------|
| **Token Plan 用户** | 调用 API-vlm 时由套餐内 Token Plan 额度扣减，超出部分可由已购积分自动补充 |
| **非 Token Plan 用户** | 使用 MiniMax 普通 API Key，API-vlm 按量计费 0.025 元/次（从账户余额扣除） |

> **注意**：Token Plan 订阅 Key 和普通 API Key 是两套独立的账户体系。Token Plan 的积分仅限 MCP 工具调用，普通 Key 的余额覆盖全部 API 产品。详见 [MiniMax 按量计费文档](https://platform.minimaxi.com/docs/guides/pricing-paygo)。

---

## 快速开始

### 1. 安装

Dashboard → 插件管理 → 添加插件，填入：

```
https://github.com/uuutt2023/astrbot_plugin_vision_text_bridge.git
```

### 2. 配置

Dashboard → 插件管理 → 图片转文字 → 配置，填入：

| 字段 | 在哪获取 |
|------|----------|
| `minimax_api_key` | [MiniMax 开放平台](https://platform.minimax.io)，创建 sk- 开头的 Key |
| `openapi_key` | Dashboard → 设置 → OpenAPI → 创建 Key（勾选 provider scope，格式 `abk_xxx`） |

`minimax_api_key` 是必填项。`openapi_key` 如果不填不影响插件本身，只是其他插件没法通过 provider 接口调用它。

### 3. 重启

改了 Key 之后重启 AstrBot，看到启动日志里有这些就是成功了：

```
[vision_text_bridge] 描述缓存已初始化  (条目=0)
[vision_text_bridge] mmx-cli 本地装成功
[vision_text_bridge] 预登录成功
[vision_text_bridge] webui API 注册成功
```

### 4. 打开 WebUI

Dashboard → 插件管理 → 图片转文字 → 打开页面

### 依赖

- **Node.js >= 18**（运行 mmx CLI）
- 首次启动自动下载 mmx 到插件本地目录，不用手动装

---

## 它是怎么工作的

一条带图片的用户消息进来后，插件在两个阶段介入：

**阶段一（高优先级，处理图片）:**

1. 从 `image_urls` / `extra_parts` / `contexts` 中提取所有图片
2. 对每张图计算 md5，先查内存缓存，再查 SQLite
3. 缓存未命中就调 `mmx vision describe`，拿到文字描述后写入缓存
4. 把 `[Image 1 描述] ...` 注入到请求的 `extra_user_content_parts`
5. LLM 收到的不是原始图片，而是文字描述

**阶段二（低优先级，清理残留）:**

6. 兜底钩子（priority -10000）检查是否还有 `data:base64` 残留没被处理掉，有就清掉

**外部插件调用时**，直接走独立 server：

```
外部插件 → POST /v1/chat/completions (端口 2023)
         → 提取 messages 中的 image_url + prompt
         → mmx → 返回纯文本描述
```

---

## 配置参考

Dashboard → 插件管理 → 图片转文字 → 配置。以下是完整字段说明。

### 基础

| 字段 | 默认 | 说明 |
|------|------|------|
| `enabled` | `true` | 总开关，关掉后不拦截任何 LLM 请求 |
| `priority` | `100` | 钩子优先级，越大越靠前，重启生效 |

### MiniMax CLI

| 字段 | 默认 | 说明 |
|------|------|------|
| `minimax_api_key` | — | **必填**。MiniMax 开放平台 sk- 开头的 Key |
| `mmx_path` | — | mmx CLI 路径，留空自动从 PATH 找 |
| `auto_login` | `true` | 启动时自动用 Key 登录 mmx |
| `auto_install_cli` | `true` | 找不到 mmx 时自动装到插件本地 |
| `command_timeout` | `60` | 单次调用超时（秒），大图建议调高 |

### 图像理解

| 字段 | 默认 | 说明 |
|------|------|------|
| `vision_prompt` | 通用描述提示词 | 传给 mmx 的默认 prompt，调用方可覆盖 |
| `image_placeholder_template` | `[Image {index} 描述] {description}` | 注入到 LLM 请求中的占位符格式 |
| `max_description_length` | `800` | 描述超长截断，0 = 不限制 |
| `failure_message` | `[Image {index} 描述] 理解失败: {error}` | mmx 调用失败时的占位文本 |
| `strip_mmx_markdown` | `true` | 去掉加粗/列表前缀/多余空行，省约 25% token |

### 并发

| 字段 | 默认 | 说明 |
|------|------|------|
| `max_concurrent_vision` | `3` | 同一条消息里最多并行几张图，建议 1–4 |

### 缓存

| 字段 | 默认 | 说明 |
|------|------|------|
| `cache_descriptions` | `true` | 总开关 |
| `cache_file_paths` | `true` | 关掉后只缓存 http(s) 图片 |
| `max_b64_size_kb` | `2048` | 单张图 base64 存储上限，超限不存缩略图（描述仍存） |
| `memory_cache_ttl_seconds` | `300` | 内存缓存 TTL，0 = 不过期 |
| `memory_cache_max_size` | `500` | 内存 LRU 条数上限 |
| `sqlite_cache_ttl_days` | `7` | SQLite 保留天数 |
| `sqlite_clean_interval_hours` | `1` | 后台清理间隔 |

### 输入处理

| 字段 | 默认 | 说明 |
|------|------|------|
| `include_history` | `false` | 是否扫描历史消息中的图片 |
| `include_extra_parts` | `true` | 是否处理 extra_parts 中的图片 |
| `strip_all_image_urls_in_fallback` | `false` | 链末是否删除所有 image_url（默认只删 base64 残留） |

### LLM 提示

| 字段 | 默认 | 说明 |
|------|------|------|
| `inject_system_prompt_guidance` | `true` | 追加提示让 LLM 基于文字描述回答 |
| `inject_caption_text_to_system_prompt` | `false` | 把描述也注入 system_prompt（防其他插件覆盖） |

### 权限控制

| 字段 | 默认 | 说明 |
|------|------|------|
| `enable_group_whitelist` | `false` | 仅白名单群生效 |
| `group_whitelist` | `[]` | QQ 群号列表 |
| `enable_user_whitelist` | `false` | 仅白名单用户生效 |
| `user_whitelist` | `[]` | QQ 号列表 |
| `private_chat_only` | `false` | 群聊消息跳过 |

### 跨插件兼容

| 字段 | 默认 | 说明 |
|------|------|------|
| `tool_filter_mode` | `off` | `off` / `whitelist` / `blacklist` |
| `tool_filter_names` | — | 逗号分隔的 function tool 名单，支持通配符 `*` |
| `tool_filter_extra_key` | `_group_chat_plus_func_tool` | 要注入的工具集 key |
| `keep_provider_modality_as_is` | `false` | 不补 image 标签 |

### OpenAI 兼容 provider

| 字段 | 默认 | 说明 |
|------|------|------|
| `enabled` | `true` | 暴露 `/v1/chat/completions` 端点 |
| `webui_username` | `admin` | Dashboard 登录用户名 |
| `webui_password` | — | Dashboard 登录密码 |
| `openapi_key` | — | Dashboard OpenAPI Key（推荐），注册必需 |
| `auto_register` | `true` | 启动时注册到 provider_manager |
| `api_key` | `placeholder` | 端点 API Key（本端点不校验，填占位值即可） |
| `model_name` | `vision-bridge` | 注册时的模型显示名 |
| `caption_format` | `mmx` | 返回格式，`mmx`（推荐）或 `json` |

### 日志

| 字段 | 默认 | 说明 |
|------|------|------|
| `verbose_logging` | `false` | 总开关，开启后子开关才生效 |
| `verbose_hook_trace` | `false` | 钩子入口/出口日志 |
| `verbose_mmx_subprocess` | `false` | mmx 完整命令行和 stdout/stderr |
| `verbose_cache_trace` | `false` | 缓存命中/失效/写入日志 |
| `verbose_id_computation` | `false` | image_id md5 计算过程 |

### 脱敏

| 字段 | 默认 | 说明 |
|------|------|------|
| `redact_sensitive` | `true` | 关闭后日志输出完整 URL |

---

## API 端点

### OpenAI 兼容接口

```
POST /v1/chat/completions
```

供其他插件作为 vision provider 调用。接收标准 OpenAI 格式的 messages（含 `image_url`），返回纯文本描述。

其他插件配置示例：

```json
{
  "type": "openai_chat_completion",
  "api_base": "http://127.0.0.1:2023/v1/chat/completions",
  "api_key": "placeholder",
  "model": "vision-bridge"
}
```

### 图片描述接口

```
GET  /image/caption?url=<url>&prompt=<prompt>
POST /image/caption  { "url": "...", "prompt": "..." }
```

直接返回 mmx 的文字描述。`prompt` 可选，不传用默认值。

### 缓存管理

| 端点 | 方法 | 说明 |
|------|------|------|
| `/cache/list` | GET | 缓存列表，支持 `search` / `order_by` / 翻页 |
| `/cache/thumbnail/<id>` | GET | 缩略图 |
| `/cache/stats` | GET | 缓存统计（总数、大小、时间范围） |
| `/cache/call_log` | GET | 调用日志（最近 200 条） |
| `/cache/diag` | GET | 诊断信息（DB 路径、schema、最近条目） |

---

## 端口

| 端口 | 用途 |
|------|------|
| `2023` | 本插件 OpenAI 兼容 server |
| `6185` | AstrBot Dashboard（注册 provider 时向这里发请求） |

---

## WebUI

Dashboard → 插件管理 → 图片转文字 → 打开页面。

功能一览：

- **缩略图网格** — 懒加载，并发 6 路
- **搜索** — 全文匹配 URL 或描述
- **排序** — 点击表头按 URL、描述长度、尺寸、时间排序（支持升降序）
- **翻页** — 分页浏览
- **单条操作** — 查看大图、重新生成、删除
- **批量操作** — 导出 JSON、清空全部、清理过期
- **调用日志** — 底部面板显示最近 50 条调用记录，5 秒自动刷新，支持折叠
- **主题切换** — 暗色 / 浅色，自动保存偏好

---

## 常见问题

### 启动日志里显示 "port 2023 已被占用"

另一进程用了 2023 端口。检查并释放：

```bash
lsof -i :2023
```

### provider 注册返回 False

看启动日志里的 HTTP 状态码：

| 状态码 | 原因 | 处理 |
|--------|------|------|
| 403 | OpenAPI Key 没有 `provider` scope | 重新创建 Key，创建时勾选 provider |
| 401 | Key 无效或过期 | 重新创建 Key |
| 422 | AstrBot 版本太旧 | 升级到最新版 |
| 400 "already exists" | 实际已注册成功 | 不用管 |

### 改了配置不生效

清除缓存后重启：

```bash
find <插件目录> -name __pycache__ -exec rm -rf {} +
```

### WebUI 看不到缩略图

用 `/cache/diag` 端点确认 SQLite 里有没有 `image_b64`。如果 data URL 解码失败，描述仍会正常显示。

### 启动日志关键字速查

| 日志内容 | 含义 |
|----------|------|
| `mmx-cli 本地装成功` | mmx 已就绪 |
| `预登录成功` | MiniMax 认证通过 |
| `webui API 注册成功` | provider 已注册 |
| `已从 SQLite 恢复 N 条调用日志` | 调用日志从磁盘恢复 |
| `webui API 注册返回 False` | 检查 openapi_key |

---

## 更新日志

### 2026-07-21

- 调用日志 SQLite 持久化，重启不丢失最近 200 条记录
- WebUI 样式全面统一：所有硬编码 rgba 替换为 CSS 变量，light/dark 双主题完整适配
- 配置 schema 模块按用户配置流程重排（基础 → 认证 → 核心 → 性能 → 集成 → 调试）
- 调用日志面板 source 列改为双标签展示（插件名 + 调用方式）
- 优化 data:base64 日志截断，避免大图导致字符串膨胀
- 修复 mmx 命令参数超过 OS ARG_MAX 时改用文件上传路径

### 2026-07-16

- 初始发布
- mmx vision describe 图片转文字
- 三级缓存（内存 LRU + SQLite WAL + mmx CLI）
- OpenAI 兼容 `/v1/chat/completions` 端点
- WebUI 缓存管理面板
- 调用日志实时面板
- provider 自动注册
- 权限白名单 / 工具过滤

---

## 协议

本插件继承 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 的 [AGPL-3.0](https://www.gnu.org/licenses/agpl-3.0.html) 开源协议。
