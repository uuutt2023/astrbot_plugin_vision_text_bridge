# 图片转文字插件

基于 MiniMax CLI（`mmx vision describe`）的图像理解服务。

启动后自动注册为 AstrBot provider，暴露 OpenAI 兼容接口。接口**模拟 AI 模型**：收到标准 `/v1/chat/completions` 请求后，从 messages 中提取图片，调用 MiniMax CLI 做图像理解，再把理解内容作为模型回复返回。

---

## 它能做什么

- **MiniMax CLI 图像理解** — 调用 `mmx vision describe` 把图片转成文字描述
- **OpenAI 兼容接口** — 注册为 AstrBot provider，其他插件可通过标准 `/v1/chat/completions` 调用，传入图片即返回理解内容
- **模拟 AI 模型** — 接口按 OpenAI ChatCompletion 协议应答，调用方无感知底层是 mmx

> 本插件专注于图像理解服务，不拦截对话 LLM 请求、不做缓存、无 WebUI。

---

## 适用场景

- 其他插件需要一个统一的 vision provider（纯文本模型无法看图时）
- 想省 token（文字描述通常 50–300 字，比 base64 小得多）
- 把 MiniMax 的 API-vlm 图片理解能力包装成标准 OpenAI 接口

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

`minimax_api_key` 是必填项。`openapi_key` 如果不填不影响接口本身，只是没法自动注册 provider。

### 3. 重启

改了 Key 之后重启 AstrBot，看到启动日志里有这些就是成功了：

```
[vision_text_bridge] mmx-cli 本地装成功
[vision_text_bridge] 预登录成功
[vision_text_bridge] 独立 OpenAI 兼容 server 启动: 127.0.0.1:2023
[vision_text_bridge] OpenAI 兼容 provider 注册成功
```

### 依赖

- **Node.js >= 18**（运行 mmx CLI）
- 首次启动自动下载 mmx 到插件本地目录，不用手动装

---

## 它是怎么工作的

```
外部插件 → POST /v1/chat/completions (127.0.0.1:2023)
         → 提取 messages 中的 image_url + prompt
         → mmx vision describe 图像理解
         → 返回 OpenAI ChatCompletion 格式的理解内容
```

接口对请求方完全模拟一个 OpenAI 兼容的 AI 模型：模型名、消息格式、响应结构都遵循标准协议，`choices[0].message.content` 即图片理解结果。

---

## 配置参考

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
| `max_description_length` | `800` | 描述超长截断，0 = 不限制 |
| `strip_mmx_markdown` | `true` | 去掉加粗/列表前缀/多余空行，省约 25% token |

### 并发

| 字段 | 默认 | 说明 |
|------|------|------|
| `max_concurrent_vision` | `3` | 单次请求里最多并行几张图，建议 1–4 |

### OpenAI 兼容接口

| 字段 | 默认 | 说明 |
|------|------|------|
| `enabled` | `true` | 暴露 `/v1/chat/completions` 端点 |
| `port` | `2023` | 独立 server 监听端口 |
| `dashboard_port` | `6185` | AstrBot Dashboard webui 端口（注册 provider 用） |
| `register_max_attempts` | `30` | 注册失败后每隔 5 秒重试的次数（启动时 Dashboard 可能未就绪） |
| `openapi_key` | — | Dashboard OpenAPI Key（推荐），注册必需 |
| `webui_username` | `admin` | Dashboard 登录用户名 |
| `webui_password` | — | Dashboard 登录密码 |
| `auto_register` | `true` | 启动时注册到 provider_manager |
| `api_key` | `placeholder` | 端点 API Key（本端点不校验，填占位值即可） |
| `model_name` | `vision-bridge` | 注册时的模型显示名 |

### 日志 / 脱敏

| 字段 | 默认 | 说明 |
|------|------|------|
| `verbose_logging` | `false` | 总开关，开启后子开关才生效 |
| `verbose_mmx_subprocess` | `false` | mmx 完整命令行和 stdout/stderr |
| `redact_sensitive` | `true` | 关闭后日志输出完整 URL |

---

## API 端点

### OpenAI 兼容接口

```
POST /v1/chat/completions
```

供其他插件作为 vision provider 调用。接收标准 OpenAI 格式的 messages（含 `image_url`），返回纯文本理解内容。

其他插件配置示例：

```json
{
  "type": "openai_chat_completion",
  "api_base": "http://127.0.0.1:2023/v1/chat/completions",
  "api_key": "placeholder",
  "model": "vision-bridge"
}
```

### 健康检查

```
GET /health
```

返回 `{"status": "ok"}`。

---

## 端口

| 端口 | 用途 |
|------|------|
| `2023` | 本插件 OpenAI 兼容 server（可用 `port` 配置修改） |
| `6185` | AstrBot Dashboard（注册 provider 时向这里发请求） |

---

## 常见问题

### 启动日志里显示 "port 2023 已被占用"

另一进程用了 2023 端口。检查并释放，或在配置里改 `port`：

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

### 启动日志关键字速查

| 日志内容 | 含义 |
|----------|------|
| `mmx-cli 本地装成功` | mmx 已就绪 |
| `预登录成功` | MiniMax 认证通过 |
| `独立 OpenAI 兼容 server 启动` | 接口已在 127.0.0.1:2023 监听 |
| `OpenAI 兼容 provider 注册成功` | provider 已注册 |
| `provider 注册失败` | 检查 openapi_key |

---

## 更新日志

### 2026-08-07

- **2.0.0**：大幅精简
  - 移除 WebUI 缓存管理面板
  - 移除图像理解缓存（内存 / SQLite）
  - 移除 AstrBot LLM 请求拦截（不再改写对话请求）
  - 保留 MiniMax CLI 图像理解、OpenAI 兼容接口与 provider 注册
  - 接口模拟 AI 模型：收到 `/v1/chat/completions` 后调 mmx 理解图片并返回内容
  - 配置精简为 6 组核心项

---

## 协议

本插件继承 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 的 [AGPL-3.0](https://www.gnu.org/licenses/agpl-3.0.html) 开源协议。
