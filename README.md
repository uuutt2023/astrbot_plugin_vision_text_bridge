# 图片转文字 · Mavis 图像理解

AstrBot 插件。在消息发往大模型前，将图片替换为 MiniMax 图像理解服务返回的描述文本，让 LLM 通过文字描述「理解」图片内容，请求中不再包含真实图片二进制。

[![AstrBot](https://img.shields.io/badge/AstrBot-%E2%89%A54.0.0-blue)](https://docs.astrbot.app/)
[![Python](https://img.shields.io/badge/Python-%E2%89%A53.10-green)](https://www.python.org)
[![License](https://img.shields.io/badge/license-AGPL--3.0-orange)](LICENSE)

[更新日志](CHANGELOG.md) · [问题反馈](https://github.com/uuutt2023/astrbot_plugin_vision_text_bridge/issues) · [AstrBot 文档](https://docs.astrbot.app/)

## 概述

> 详细文件归类 + 模块依赖图 + 重构方向见 [ARCHITECTURE.md](ARCHITECTURE.md)。

AstrBot 转发消息给 LLM 时，本插件拦截请求，对每张图片调用 MiniMax 获取中文描述，将描述注入用户消息正文，同时清空请求中的真实图片字段。LLM 仅看到文字描述，按描述回答用户。

**核心价值**

- **成本**：图像理解通道按次计费，文本 token 便宜。同一张图描述一次后命中本地缓存。
- **兼容性**：不支持视觉输入的模型也能「看图」（效果取决于 MiniMax 描述质量）。
- **稳定性**：阻止 AstrBot 因「检测到图」切到效果较差的备用模型。

## 工作流程

```
用户发图 → AstrBot 拦截 LLM 请求
       → 扫描请求所有图片来源（主字段、用户片段、历史）
       → 调 mmx vision describe 获取中文描述
       → 缓存到本地 SQLite (md5 图片内容)
       → 描述以「第 N 张图的描述是：xxx」格式注入用户消息
       → 清空请求里所有真实图片字段
       → 转发给 LLM
```

## 前置条件

| 依赖 | 说明 |
| --- | --- |
| AstrBot | ≥ 4.0.0 |
| MiniMax CLI (`mmx`) | MiniMax 官方命令行工具，调用图像理解服务 |
| MiniMax API Key | `sk-` 开头，在 [MiniMax 开放平台](https://platform.MiniMax.io/) 申请 |

安装 mmx：

```bash
npm install -g mmx-cli
```

插件支持检测到 mmx 缺失时自动 `npm install -g mmx-cli`（需开启 `auto_install_cli`）。

## 安装

1. 复制插件目录到 AstrBot 插件目录：`<AstrBot>/data/plugins/astrbot_plugin_vision_text_bridge/`
2. 重启 AstrBot，插件自动加载
3. 在 AstrBot 管理面板的插件配置中填入 MiniMax API Key
4. 打开缓存页面：控制台 → 插件 → 图片转文字 · Mavis → 「缓存管理」

## 配置

所有选项在 AstrBot 后台「插件设置」页面可视化配置。配置按 10 个分组展示, 可逐一展开。

### 基础

| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | 关闭后插件完全停用 |
| `priority` | int | `100` | on_llm_request 钩子优先级, 值越大越靠前. 修改后需重启 AstrBot |

### MiniMax CLI

| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `mmx_path` | string | `""` | mmx 可执行文件绝对路径, 留空时从 `PATH` 查找 |
| `minimax_api_key` | password | `""` | MiniMax API Key. 加载时用 mmx auth login --api-key 自动登录 |
| `auto_login` | bool | `true` | 启动时用 API Key 自动登录 mmx |
| `auto_install_cli` | bool | `false` | 找不到 mmx 时自动 `npm install -g mmx-cli` (需 Node.js + 全局安装权限) |
| `command_timeout` | int | `60` | 单次图像理解超时（秒） |

### 并发

| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `max_concurrent_vision` | int | `3` | 单条消息最大并发图像处理数. 超过串行处理. 建议 1-4 避免 API 限流 |

### 图像理解

| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `vision_prompt` | text | 保守描述模板 | 传给 MiniMax 的提示词, 严格禁止猜测游戏/番剧/品牌名 |
| `image_placeholder_template` | string | `[Image {index} 描述] {description}` | 注入用户消息的格式. 支持 `{index}` + `{description}` 变量 |
| `max_description_length` | int | `800` | 单图描述最大字符数, `0` 不限制 |
| `failure_message` | string | `[Image {index} 描述] 理解失败: {error}` | MiniMax 调用失败时的占位文本 |
| `strip_mmx_markdown` | bool | `true` | 清理 mmx 返回的 markdown 噪音. 典型响应 520 字符可压到 380 字符, 省 25-30% token |

### 缓存

| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `cache_descriptions` | bool | `true` | 启用描述缓存. 使用图片内容 md5 作为缓存 key |
| `cache_file_paths` | bool | `true` | 缓存 `file://` 本地临时文件路径（群聊场景 AstrBot 经常存临时文件） |
| `max_b64_size_kb` | int | `2048` | SQLite 存图片 base64 的上限（KB）, 超过不存. 检测到 chat_archive 时缩略图走 chat_archive web_cache |
| `memory_cache_ttl_seconds` | int | `300` | 内存热缓存有效期（秒）, 0 = 不过期 |
| `memory_cache_max_size` | int | `500` | 内存热缓存最大条数（LRU 淘汰）, 0 = 不限制 |
| `sqlite_cache_ttl_days` | int | `7` | SQLite 缓存有效期（天）, 0 = 不过期 |
| `sqlite_clean_interval_hours` | int | `1` | SQLite 过期清理任务间隔（小时）, 0 = 启动时清一次后关闭后台任务 |

缓存键使用 `md5(图片字节)`。同一张图无论 URL/路径如何变化都能命中。

### 与 chat_archive 协同

如果检测到 [astrbot_plugin_chat_archive](https://github.com/YukiNo420/astrbot_plugin_chat_archive) 已安装, 本插件:

- **不存 image_b64** (省 SQLite 空间, 避免重复缓存同一张图)
- **webui 缩略图** 从 chat_archive 的 `web_cache/<sha256(url)[:32]><ext>` 读
- **过期清理** 交给 chat_archive 负责 (它每天扫 `web_cache/` 删除 mtime > N 天的文件)
- 文本描述仍由本插件 SQLite 缓存 (描述 vs 图片是不同生命周期)

### 输入处理

| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `include_history` | bool | `false` | 处理历史对话 (req.contexts) 中的图片. 关闭时只处理当前用户消息 |
| `include_extra_parts` | bool | `true` | 处理 extra_user_content_parts 中的图片. 多数场景图片在 image_urls, 可关闭以提升性能 |
| `strip_all_image_urls_in_fallback` | bool | `false` | 链末兜底删除**所有** image_url (不仅是 base64). 避免 LLM 报 `unknown variant image_url` 400 错误 |

### LLM 提示

| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `inject_system_prompt_guidance` | bool | `true` | 向 system_prompt 注入「严格引用图描述」指令, 防止 LLM 凭印象补充背景 |
| `inject_caption_text_to_system_prompt` | bool | `false` | 同时将图描述塞入 system_prompt（双重保险, 极端情况） |

### 跨插件兼容

| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `tool_filter_mode` | string | `off` | 工具过滤模式: `off` / `whitelist` / `blacklist` |
| `tool_filter_names` | string | `""` | 工具过滤名单（逗号分隔）, 支持通配符 `*` |
| `tool_filter_extra_key` | string | `_group_chat_plus_func_tool` | 从 event.get_extra() 取的待注入工具集 key（chat_plus 内部） |
| `keep_provider_modality_as_is` | bool | `false` | 不修改 provider modalities（关闭「主模型支持图」的兼容性修复） |

### 日志

| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `verbose_logging` | bool | `false` | 总开关. 开启后下面 4 个细粒度全部生效 |
| `verbose_hook_trace` | bool | `false` | 拦截钩子入口/出口日志 |
| `verbose_mmx_subprocess` | bool | `false` | mmx 子进程完整命令与输出 |
| `verbose_cache_trace` | bool | `false` | 内存 / SQLite 缓存命中/写入日志 |
| `verbose_id_computation` | bool | `false` | image_id (md5) 计算过程日志 |

### 脱敏

| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `redact_sensitive` | bool | `true` | 日志中对 API Key、URL token 等敏感字段脱敏. 关闭后日志会输出完整 URL |

## 缓存机制

**历史问题**：旧版本使用图片 URL 字符串作为缓存键。AstrBot 每次压缩图片生成新文件名（带哈希后缀），导致同一张图的不同临时路径永远命中不了缓存，每次都重调 MiniMax。

**当前实现**：缓存键 = `md5(图片内容)`，与 URL/路径无关。

| URL | 内容 | 缓存键 |
| --- | --- | --- |
| `/AstrBot/temp/compressed_aaa.jpg` | 图 X | `md5(图片 X 字节)` |
| `/AstrBot/temp/compressed_bbb.jpg` | 图 X（同一张） | `md5(图片 X 字节)` ← 命中 |
| `https://cdn.example.com/y.jpg` | 图 Y | `md5(图片 Y 字节)` |

读取图片字节失败时（网络超时、文件被清理）退回到 URL 字符串作为键，保证不报错。

**缓存 URL 类型**：`http://`、`https://` 默认缓存；`file://` 与本地裸路径默认缓存；`data:image/...` base64 不缓存（字符串每次都不同，无意义）。

## 防止 LLM 误判

常见误判：用户发送「抖音评论区 + 云南野生菌」截图，LLM 脑补为「永劫无间」游戏截图（基于「云南 + 二次元」的关联记忆）。本插件通过三层防御降低误判：

1. **MiniMax 提示词**：要求 MiniMax 仅描述可见元素，禁止猜测游戏/番剧/品牌名，无法确定时明说「无法确定」
2. **自然语言格式**：「第 N 张图的描述是：xxx」让 LLM 视为用户描述而非 prompt 占位符（LLM 对占位符倾向脑补，对真实人话较保守）
3. **系统提示词指令**：默认向 system_prompt 注入「严格基于图描述回答，不要补充背景知识」

## 兼容性

### AngelHeart 群聊记忆插件

AngelHeart（priority=50）会重写完整 prompt 字符串。旧版本将图描述塞在 prompt 中被改写丢失。修复方案：图描述改注入到 `req.extra_user_content_parts`（用户消息内容片段），AngelHeart 拿不到。

同时在消息链最末端（priority=-10000）兜底清理 AngelHeart 塞回的 base64 内嵌图片。

### chat_plus 群聊插件

chat_plus（priority=-1）会在主钩子之后 merge 工具集合到 `req.func_tool`。本插件在主钩子入口（priority=100）预先清理待合并的工具集，使 chat_plus merge 进去的为干净版。

### smart_imagechat_hub 图文理解插件

[astrbot_plugin_smart_imagechat_hub](https://github.com/QingchenWait/astrbot_plugin_smart_imagechat_hub) 用 LLM 多模态给图片打标签（默认走 MiniMax/ OpenAI 多模态 API）。本插件提供**完全接管其 image caption 请求**的兼容方案。

**重要背景**：smart_imagechat_hub 调 LLM 时用 `direct_provider_call=True` 直接调 `provider.text_chat(image_urls=...)`，**绕过我方 `on_llm_request` 钩子**——所以无法在钩子层拦截。

**本插件提供的兼容方案**：

1. 启动时自动检测是否装了 smart_imagechat_hub，装了就在日志和 webui 顶部 banner 提示
2. 暴露 OpenAI 兼容的 `POST /api/plug/astrbot_plugin_vision_text_bridge/v1/chat/completions` endpoint——smart_imagechat_hub 调这个 endpoint 时，本插件走 mmx 流程返回自然语言描述
3. 简单 `GET/POST /api/plug/astrbot_plugin_vision_text_bridge/image/caption?url=...` 拿 mmx 描述

**配置方法（v1.1.2+ 默认自动注册）**：

启动时本插件**自动**在 AstrBot provider_manager 注册一个 OpenAI compatible provider：
- provider id: `vision_text_bridge_compat`
- type: `openai_chat_completion`
- api_base: 自动从 AstrBot dashboard 拿 host:port（默认 `http://localhost:6185/api/plug/astrbot_plugin_vision_text_bridge/v1/chat/completions`）
- api_key: 留空（我方不校验）
- model: `vision-bridge`

用户只需要在 smart_imagechat_hub 配 `default_image_caption_provider_id` = `vision_text_bridge_compat` 即可，不用手动加 OpenAI compatible provider。

**高级用户**（想覆盖默认值）可在插件配置里改：
- `smart_imagechat_hub_api_base`：自定义 API Base URL（留空用默认）
- `smart_imagechat_hub_api_key`：自定义 API Key（我方不校验，留空即可）
- `smart_imagechat_hub_model_name`：注册到 provider 的模型名（默认 `vision-bridge`）

**手动配置方法**（如果自动注册被关掉 `smart_imagechat_hub_auto_register_provider=False`）：

1. 在 AstrBot 控制台加一个 OpenAI compatible provider：
   - provider id: `vision_text_bridge_compat`（或任意名字）
   - type: `openai_chat_completion`
   - api_base: `http://127.0.0.1:6185/api/plug/astrbot_plugin_vision_text_bridge/v1/chat/completions`
   - api_key: 留空（我方不校验）
   - model: `vision-bridge`
2. smart_imagechat_hub 配 `default_image_caption_provider_id` = 这个 provider id
3. 它的 image caption 请求会走本插件的 mmx 流程，不再调 LLM 多模态

**配置开关**（插件 → 图片转文字 · Mavis → 智能图像聊天 兼容）：

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `enable_smart_imagechat_hub_compat` | `true` | 暴露 `/v1/chat/completions` 和 `/image/caption` endpoint |
| `smart_imagechat_hub_auto_register_provider` | `true` | 启动时自动注册 OpenAI compatible provider (id=vision_text_bridge_compat) |
| `smart_imagechat_hub_caption_format` | `mmx` | 返回格式：`mmx`（原样 mmx 描述）/ `json`（包装成 `[tag, tag]` 数组） |
| `smart_imagechat_hub_api_base` | `""` | 自定义 API Base URL，留空用默认 `http://localhost:6185/api/plug/...` |
| `smart_imagechat_hub_api_key` | `""` | 自定义 API Key（我方不校验，留空） |
| `smart_imagechat_hub_model_name` | `vision-bridge` | 注册到 provider 的模型名 |

**限制**：
- smart_imagechat_hub 的 prompt 要求输出 `["tag1", "tag2"]` JSON 格式——我方返回的是 mmx 自然语言描述，它的 `_extract_tags` 可能要适配
- 建议先用 `mmx` 格式观察 smart_imagechat_hub 收到描述后的行为，如果它能解析就 OK，不能再切 `json` 模式

## 缓存管理页面

路径：控制台 → 插件 → 图片转文字 · Mavis → 「缓存管理」

| 功能 | 说明 |
| --- | --- |
| 统计卡片 | 总条目、数据库大小、内存缓存大小 |
| 搜索 | 按 URL / 描述模糊匹配 |
| 排序 | 最新 / 最旧 |
| 缩略图 | 从 SQLite 读取图片二进制，点击查看大图 |
| 操作 | 单条删除、重新生成、导出 JSON、清空全部（含 VACUUM） |
| 主题 | 暗色 / 浅色切换，localStorage 持久化 |
| 快捷键 | `R` 刷新列表 |

页面通过 AstrBot 提供的页面通信机制与后端交互，不占用独立端口。

后端 API（插件自动注册）：

| 接口 | 方法 | 作用 |
| --- | --- | --- |
| `/astrbot_plugin_vision_text_bridge/cache/stats` | GET | 缓存统计 |
| `/astrbot_plugin_vision_text_bridge/cache/stats/timeline` | GET | 按天创建量（柱状图数据） |
| `/astrbot_plugin_vision_text_bridge/cache/list` | GET | 分页列表 + 搜索 + 排序 |
| `/astrbot_plugin_vision_text_bridge/cache/delete` | POST | 删除单条（body 含 `key`） |
| `/astrbot_plugin_vision_text_bridge/cache/regenerate` | POST | 重新生成（body 含 `key`） |
| `/astrbot_plugin_vision_text_bridge/cache/clear` | POST | 清空全部 |
| `/astrbot_plugin_vision_text_bridge/cache/clean_expired` | POST | 清理过期条目 |
| `/astrbot_plugin_vision_text_bridge/cache/export` | GET | 导出全部为 JSON |
| `/astrbot_plugin_vision_text_bridge/cache/thumbnail/<image_id>` | GET | 缩略图，路径参数为图片 ID |
| `/astrbot_plugin_vision_text_bridge/cache/diag` | GET | 诊断：DB 路径 / schema / 最近 3 条 |

## 常见问题

### mmx 报「余额不足」

不要只看面板余额。手动验证 mmx：

```bash
mmx --version
mmx auth status
mmx quota
mmx vision describe --image /path/to/any.png --prompt "描述"
```

| 第 1-3 步成功，第 4 步报余额不足 | mmx 版本/路由问题 | `npm update -g mmx-cli` |
| --- | --- | --- |
| 第 4 步报未登录/无权限 | API Key 权限或环境问题 | 更换 Key 或检查 Key 绑定环境 |
| 第 4 步报找不到模型 | mmx 版本过旧 | `npm update -g mmx-cli` |
| 全部成功 | 插件逻辑问题 | 开启 `verbose_mmx_subprocess`，复现后查看日志 |

### LLM 误判图描述

查看日志中的「描述预览」（默认开启），确认是 MiniMax 描述错误还是 LLM 误判：

- 描述预览错误：调整 `vision_prompt` 或更换 MiniMax 模型
- 描述预览正确但 LLM 误判：主模型能力问题，建议更换

### 缓存页面显示 0 条但插件工作

检查 `cache_file_paths` 是否开启。旧版本仅缓存网络图片，本地文件路径不缓存。

## 离线测试

```bash
cd astrbot_plugin_vision_text_bridge
python3 test.py
```

期望输出 `PASS: 176/176`。测试使用桩模块模拟 `astrbot.api`，无需 AstrBot 真实环境。

主题测试：

```bash
python3 test_theme.py
```

期望输出 `ALL THEME TESTS PASSED`（12 个测试）。

## 目录结构

重构后拆分 8 个单一职责模块：

```
astrbot_plugin_vision_text_bridge/
├── main.py                  # 插件生命周期 + 拦截钩子 + 业务核心 (1121 行)
├── web_api.py               # 缓存管理后端接口 (10 个 handler)
├── mmx_runner.py            # MiniMax CLI 调用 + 错误诊断 + 日志脱敏
├── caption_cache.py         # SQLite 描述缓存 (含图片二进制)
├── image_utils.py           # 消息片段图片 URL 提取
├── image_meta.py            # 图片元信息嗅探 (mime / 宽高) + 缓存策略
├── image_fetch.py           # 异步读取图片字节
├── tool_filter.py           # 跨插件工具集合过滤
├── config_helpers.py        # 配置读取辅助
├── _conf_schema.json        # AstrBot 配置 schema
├── metadata.yaml            # AstrBot 插件元数据
├── pages/
│   └── cache-manager/       # 内置页面 (HTML / JS / CSS, 玻璃拟态)
├── test.py                  # 主测试套件 (176 个测试)
├── test_theme.py            # 主题测试 (12 个测试)
├── README.md
└── CHANGELOG.md
```

## 参考

- [`astrbot_plugin_uni_nickname`](https://github.com/Hakuin123/astrbot_plugin_uni_nickname) — 拦截 LLM 请求钩子的标准用法
- [`astrbot_plugin_MiniMax_CLI`](https://github.com/tanggetian/astrbot_plugin_MiniMax_CLI) — mmx vision describe 子进程调用参考
- [AstrBot 文档](https://docs.astrbot.app/) — `ProviderRequest` 字段说明

## 许可

AGPL-3.0
