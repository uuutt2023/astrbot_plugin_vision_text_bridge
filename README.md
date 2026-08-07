# 图片转文字 — 图像理解桥接

基于 MiniMax CLI（`mmx vision describe`）的图像理解服务，为 AstrBot 提供一个开箱即用的视觉 provider。

<!-- PROJECT SHIELDS -->

[![Contributors][contributors-shield]][contributors-url]
[![Forks][forks-shield]][forks-url]
[![Stargazers][stars-shield]][stars-url]
[![Issues][issues-shield]][issues-url]
[![AGPL License][license-shield]][license-url]
[![AstrBot][astrbot-shield]][astrbot-url]

<!-- PROJECT LOGO -->
<br />

<p align="center">
  <a href="https://github.com/uuutt2023/astrbot_plugin_vision_text_bridge/">
    <img src="logo.png" alt="Logo" width="80" height="80">
  </a>

  <h3 align="center">图片转文字 — 图像理解桥接</h3>
  <p align="center">
    一个让 AstrBot 拥有"看图说话"能力的插件
    <br />
    <a href="https://github.com/uuutt2023/astrbot_plugin_vision_text_bridge"><strong>探索本项目的文档 »</strong></a>
    <br />
    <br />
    <a href="https://github.com/uuutt2023/astrbot_plugin_vision_text_bridge/issues">报告 Bug</a>
    ·
    <a href="https://github.com/uuutt2023/astrbot_plugin_vision_text_bridge/issues">提出新特性</a>
  </p>
</p>

本插件暴露 OpenAI 兼容接口并注册为 AstrBot provider，**模拟 AI 模型**：收到标准 `/v1/chat/completions` 请求后，从 messages 中提取图片，调用 MiniMax CLI 做图像理解，再把理解内容作为模型回复返回。

> 本插件专注于图像理解服务，不拦截对话 LLM 请求、不做缓存、无 WebUI。

## 目录

- [它能做什么](#它能做什么)
- [上手指南](#上手指南)
  - [开发前的配置要求](#开发前的配置要求)
  - [安装步骤](#安装步骤)
- [文件目录说明](#文件目录说明)
- [开发的架构](#开发的架构)
- [配置参考](#配置参考)
- [API 端点](#api-端点)
- [部署](#部署)
- [使用到的框架](#使用到的框架)
- [常见问题](#常见问题)
- [更新日志](#更新日志)
- [贡献者](#贡献者)
  - [如何参与开源项目](#如何参与开源项目)
- [版本控制](#版本控制)
- [作者](#作者)
- [版权说明](#版权说明)
- [鸣谢](#鸣谢)

## 它能做什么

- **MiniMax CLI 图像理解** — 调用 `mmx vision describe` 把图片转成文字描述
- **OpenAI 兼容接口** — 注册为 AstrBot provider，其他插件可通过标准 `/v1/chat/completions` 调用，传入图片即返回理解内容
- **模拟 AI 模型** — 接口按 OpenAI ChatCompletion 协议应答，调用方无感知底层是 mmx

### 适用场景

- 其他插件需要一个统一的 vision provider（纯文本模型无法看图时）
- 想省 token（文字描述通常 50–300 字，比 base64 小得多）
- 把 MiniMax 的 API-vlm 图片理解能力包装成标准 OpenAI 接口

### 适用人群

本插件调用 MiniMax `mmx vision describe`，底层使用 [MCP API-vlm 模型](https://platform.minimaxi.com/docs/guides/pricing-paygo#mcp)进行图片理解。

| 方案 | 说明 |
|------|------|
| **Token Plan 用户** | 调用 API-vlm 时由套餐内 Token Plan 额度扣减，超出部分可由已购积分自动补充 |
| **非 Token Plan 用户** | 使用 MiniMax 普通 API Key，API-vlm 按量计费 0.025 元/次（从账户余额扣除） |

> **注意**：Token Plan 订阅 Key 和普通 API Key 是两套独立的账户体系。Token Plan 的积分仅限 MCP 工具调用，普通 Key 的余额覆盖全部 API 产品。详见 [MiniMax 按量计费文档](https://platform.minimaxi.com/docs/guides/pricing-paygo)。

## 上手指南

### 开发前的配置要求

1. AstrBot >= 4.0.0
2. Node.js >= 18（运行 mmx CLI，首次启动会自动安装到插件本地目录，无需手动装）

### 安装步骤

> 如需完整 WebUI + 缓存 + LLM 拦截，请使用 [full-version 分支安装链接](https://github.com/uuutt2023/astrbot_plugin_vision_text_bridge/tree/full-version)。

1. 获取 MiniMax API Key 于 [https://platform.minimax.io](https://platform.minimax.io)（创建 sk- 开头的 Key）
2. 获取 OpenAPI Key 于 Dashboard → 设置 → OpenAPI（创建 abk_ 开头的 Key，勾选 provider scope）

Dashboard → 插件管理 → 添加插件，填入：

```sh
https://github.com/uuutt2023/astrbot_plugin_vision_text_bridge.git
```

然后到 插件管理 → 图片转文字 → 配置，填入两个 Key：

| 字段 | 在哪获取 |
|------|----------|
| `minimax_api_key` | [MiniMax 开放平台](https://platform.minimax.io)，创建 sk- 开头的 Key |
| `openapi_key` | Dashboard → 设置 → OpenAPI → 创建 Key（勾选 provider scope，格式 `abk_xxx`） |

`minimax_api_key` 是必填项。`openapi_key` 如果不填不影响接口本身，只是没法自动注册 provider。

重启后看到启动日志里有这些就是成功了：

```
[vision_text_bridge] mmx-cli 本地装成功
[vision_text_bridge] 预登录成功
[vision_text_bridge] 独立 OpenAI 兼容 server 启动: 127.0.0.1:2023
[vision_text_bridge] OpenAI 兼容 provider 注册成功
```

## 文件目录说明

```
filetree
├── main.py                    # 插件入口：mmx 就绪 + 接口启动 + provider 注册 + 图像理解
├── main_server.py             # 独立 OpenAI 兼容 server（模拟 AI 模型返回理解内容）
├── mmx_runner.py              # MiniMax CLI subprocess 包装
├── provider_registration.py   # 通过 webui HTTP API 注册 provider（仅 API_KEY 认证）
├── constants.py               # 常量（端口、provider id、URL 前缀）
├── _conf_schema.json          # 可视化配置 JSON（API 密钥 → 图像理解 → CLI → 并发 → 接口 → 日志 → 脱敏）
├── ARCHITECTURE.md            # 架构说明
├── CHANGELOG.md               # 更新日志
├── metadata.yaml              # 插件元数据
├── requirements.txt           # Python 依赖
├── logo.png                   # 插件 Logo
└── LICENSE                    # AGPL-3.0 开源协议
```

## 开发的架构

请阅读 [ARCHITECTURE.md](ARCHITECTURE.md) 查阅为该项目的架构。

简要流程：

```
外部插件 → POST /v1/chat/completions (127.0.0.1:2023)
         → 提取 messages 中的 image_url + prompt
         → mmx vision describe 图像理解
         → 返回 OpenAI ChatCompletion 格式的理解内容
```

接口对请求方完全模拟一个 OpenAI 兼容的 AI 模型：模型名、消息格式、响应结构都遵循标准协议，`choices[0].message.content` 即图片理解结果。

## 配置参考

配置按使用顺序重排：先填 API Key，再配图像理解提示词，其余按需调整。

### API 密钥

| 字段 | 默认 | 说明 |
|------|------|------|
| `minimax_api_key` | — | **必填，第一步**。MiniMax 开放平台 sk- 开头的 Key |
| `openapi_key` | — | **第一步**。Dashboard OpenAPI Key（`abk_` 开头），注册 provider 的唯一认证方式 |

### 图像理解

| 字段 | 默认 | 说明 |
|------|------|------|
| `vision_prompt` | 通用描述提示词 | **第二步**。传给 mmx 的默认 prompt，调用方可覆盖 |
| `max_description_length` | `800` | 描述超长截断，0 = 不限制 |
| `strip_mmx_markdown` | `true` | 去掉加粗/列表前缀/多余空行，省约 25% token |

### MiniMax CLI

| 字段 | 默认 | 说明 |
|------|------|------|
| `mmx_path` | — | mmx CLI 路径，留空自动从 PATH 找 |
| `auto_login` | `true` | 启动时自动用 Key 登录 mmx |
| `auto_install_cli` | `true` | 找不到 mmx 时自动装到插件本地 |
| `command_timeout` | `60` | 单次调用超时（秒），大图建议调高 |

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
| `auto_register` | `true` | 启动时注册到 provider_manager |
| `api_key` | `placeholder` | 端点 API Key（本端点不校验，填占位值即可） |
| `model_name` | `vision-bridge` | 注册时的模型显示名 |

### 日志 / 脱敏

| 字段 | 默认 | 说明 |
|------|------|------|
| `verbose_logging` | `false` | 总开关，开启后子开关才生效 |
| `verbose_mmx_subprocess` | `false` | mmx 完整命令行和 stdout/stderr |
| `redact_sensitive` | `true` | 关闭后日志输出完整 URL |

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

### 端口

| 端口 | 用途 |
|------|------|
| `2023` | 本插件 OpenAI 兼容 server（可用 `port` 配置修改） |
| `6185` | AstrBot Dashboard（注册 provider 时向这里发请求） |

## 部署

本插件在 AstrBot 内以插件形式运行，无需独立部署。

## 使用到的框架

- [AstrBot](https://github.com/AstrBotDevs/AstrBot) — 插件宿主框架，webui HTTP API 注册 provider
- [MiniMax](https://www.minimax.io) — MCP API-vlm 图片理解模型，经 mmx CLI 调用
- [mmx-cli](https://www.npmjs.com/package/mmx-cli) — MiniMax 官方 CLI，执行 `mmx vision describe`

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

## 更新日志

### 2026-08-07

- **2.0.1**：注册认证只保留 OpenAPI Key；配置 JSON 重排（API 密钥置于头部）
- **2.0.0**：大幅精简
  - 移除 WebUI 缓存管理面板
  - 移除图像理解缓存（内存 / SQLite）
  - 移除 AstrBot LLM 请求拦截（不再改写对话请求）
  - 保留 MiniMax CLI 图像理解、OpenAI 兼容接口与 provider 注册
  - 接口模拟 AI 模型：收到 `/v1/chat/completions` 后调 mmx 理解图片并返回内容
  - 配置精简为核心配置组

完整历史见 [CHANGELOG.md](CHANGELOG.md)。

## 贡献者

请阅读 **CHANGELOG.md** 查阅为该项目做出贡献的记录。你所作的任何贡献都是**非常感谢**的。

### 如何参与开源项目

贡献使开源社区成为一个学习、激励和创造的绝佳场所。

1. Fork the Project
2. Create your Feature Branch (`git checkout -b feature/AmazingFeature`)
3. Commit your Changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the Branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

## 版本控制

该项目使用 Git 进行版本管理，版本记录见 [CHANGELOG.md](CHANGELOG.md)。

## 作者

[uuutt](https://github.com/uuutt2023)

*您也可以在贡献者名单中参看所有参与该项目的开发者。*

## 版权说明

该项目签署了 AGPL-3.0 授权许可，详情请参阅 [LICENSE](LICENSE)。本插件继承 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 的 [AGPL-3.0](https://www.gnu.org/licenses/agpl-3.0.html) 开源协议。

## 鸣谢

- [MiniMax](https://www.minimax.io) — 提供图片理解模型与 CLI
- [AstrBot](https://github.com/AstrBotDevs/AstrBot) — 插件框架与 webui API
- [Img Shields](https://shields.io) — 项目徽章
- [Choose an Open Source License](https://choosealicense.com) — 开源协议参考

<!-- links -->
[contributors-shield]: https://img.shields.io/github/contributors/uuutt2023/astrbot_plugin_vision_text_bridge.svg?style=flat-square
[contributors-url]: https://github.com/uuutt2023/astrbot_plugin_vision_text_bridge/graphs/contributors
[forks-shield]: https://img.shields.io/github/forks/uuutt2023/astrbot_plugin_vision_text_bridge.svg?style=flat-square
[forks-url]: https://github.com/uuutt2023/astrbot_plugin_vision_text_bridge/network/members
[stars-shield]: https://img.shields.io/github/stars/uuutt2023/astrbot_plugin_vision_text_bridge.svg?style=flat-square
[stars-url]: https://github.com/uuutt2023/astrbot_plugin_vision_text_bridge/stargazers
[issues-shield]: https://img.shields.io/github/issues/uuutt2023/astrbot_plugin_vision_text_bridge.svg?style=flat-square
[issues-url]: https://github.com/uuutt2023/astrbot_plugin_vision_text_bridge/issues
[license-shield]: https://img.shields.io/github/license/uuutt2023/astrbot_plugin_vision_text_bridge.svg?style=flat-square
[license-url]: https://github.com/uuutt2023/astrbot_plugin_vision_text_bridge/blob/main/LICENSE
[astrbot-shield]: https://img.shields.io/badge/AstrBot-%3E%3D4.0.0-blue.svg?style=flat-square
[astrbot-url]: https://github.com/AstrBotDevs/AstrBot
