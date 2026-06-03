# astrbot_plugin_vision_text_bridge

> 把 LLM 请求里的图片转成 MiniMax CLI 图像理解的文本，再交给对话模型。
>
> **纯文本多模态替代**——LLM 能"看懂"图片内容（因为它读到了图片描述），但请求里没有真实的图片二进制。

[![AstrBot](https://img.shields.io/badge/AstrBot-%E2%89%A54.0.0-blue)](https://docs.astrbot.app/)
[![Python](https://img.shields.io/badge/Python-%E2%89%A53.10-green)](https://www.python.org)
[![License](https://img.shields.io/badge/license-AGPL--3.0-orange)](LICENSE)
[![Version](https://img.shields.io/badge/version-0.8.2-brightgreen)](CHANGELOG.md)

[更新日志](CHANGELOG.md) · [问题反馈](https://github.com/uuutt2023/astrbot_plugin_vision_text_bridge/issues) · [AstrBot 文档](https://docs.astrbot.app/)

## 它做了什么

在 AstrBot 把消息发给 LLM 之前，本插件会：

1. 拦截 `ProviderRequest`（基于 `filter.on_llm_request` 钩子，priority 默认 100）；
2. 扫描 `req.image_urls`、`req.extra_user_content_parts`、`req.contexts` 三处可能藏图片的地方；
3. 对每张图片调用 `mmx vision describe --image <url> --prompt <...>`；
4. 把描述以 `[Image N 描述] xxx` 格式注入到 `req.extra_user_content_parts`（user message 的 content block，**不被其他插件重写**）；
5. 清空 `req.image_urls`（防 LLM 同时看图 + 图说，浪费 token）；
6. **持久化描述到 SQLite**（跨重启保留），下次发同图直接命中；
7. **提供内置页面** 用于查看 / 搜索 / 删除 / 重新生成缓存；
8. **链末兜底** 删除被中间插件塞回来的 `data:image/...;base64,...` 残留。

## 为什么需要它

- **省钱 / 限流**：Vision 通道额外计费，文本 token 一般更便宜。
- **老模型兼容**：不支持 Vision 的 LLM 也能"看图"（理解质量取决于 mmx 描述质量）。
- **统一格式**：所有图说都按 `[Image N 描述] xxx` 格式注入，prompt 模板更可控。
- **不切 fallback**：插件会**骗** AstrBot 主 provider "支持图"（实际只发文本），让 minimax 之类的 provider 不会因为检测到图就切到 deepseek 之类质量差的 fallback。

## 前置依赖

1. **mmx-cli**（MiniMax 官方 CLI）：
   ```bash
   npm install -g mmx-cli
   ```
2. **AstrBot ≥ 4.0.0**。
3. **MiniMax API Key**（`sk-` 开头，[MiniMax 开放平台](https://platform.MiniMax.io/) Token Plan 页面获取）。

插件支持 `auto_install_cli: true` 自动 `npm install -g mmx-cli`。

## 安装

1. 复制 `astrbot_plugin_vision_text_bridge/` 到 `<AstrBot>/data/plugins/`。
2. 重启 AstrBot，插件自动加载。
3. 在 AstrBot 管理面板的插件配置里填写 `minimax_api_key`（开启 `auto_login` 后插件启动时自动 `mmx auth login`）。
4. **页面访问**：AstrBot Dashboard → 插件 → Vision → Text Bridge → 「缓存管理」。

## 配置项

| 配置项 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | 总开关 |
| `priority` | int | `100` | 拦截优先级（越大越先执行）。修改后需重启 AstrBot |
| `mmx_path` | string | `""` | mmx 可执行文件绝对路径，留空时从 `PATH` 找 |
| `minimax_api_key` | password | `""` | MiniMax API Key。填写后 `auto_login: true` 时自动登录 mmx |
| `auto_login` | bool | `true` | 插件初始化时是否使用 `minimax_api_key` 执行 `mmx auth login` |
| `auto_install_cli` | bool | `false` | 找不到 mmx 时自动 `npm install -g mmx-cli` |
| `command_timeout` | int | `60` | 单次 mmx vision describe 超时（秒） |
| `max_concurrent_vision` | int | `3` | 单条消息最多并发图像理解数 |
| `vision_prompt` | text | 保守描述模板 | 传给 mmx 的提示词。默认要求 mmx 客观列元素、**严禁猜测**游戏/番剧/品牌名 |
| `image_placeholder_template` | string | `[Image {index} 描述] {description}` | 图说格式（v0.7 起注入到 user message content block） |
| `max_description_length` | int | `800` | 单图描述最大字符数（0 不限制） |
| `include_history` | bool | `false` | 是否处理 `req.contexts` 历史中的图片 |
| `include_extra_parts` | bool | `true` | 是否处理 `extra_user_content_parts` 中的图片 |
| `failure_message` | string | `[Image {index} 描述] 理解失败：{error}` | mmx 失败时的占位文本 |
| `redact_sensitive` | bool | `true` | 日志中脱敏 API Key 等 |
| `cache_descriptions` | bool | `true` | 缓存图像理解结果（v0.8.2 起 key 用图片内容 md5） |
| `cache_file_paths` | bool | `true` | 缓存 `file://` 本地路径（v0.8.2 新增，QQ 群聊场景下 AstrBot 把图存为本地临时文件） |
| `verbose_logging` | bool | `false` | 冗余日志。调试不生效时开启 |
| `inject_system_prompt_guidance` | bool | `true` | 向 system_prompt 注入"严格引用图说"指令 |
| `inject_caption_text_to_system_prompt` | bool | `false` | 同时把图说本身也注入到 system_prompt（冗余防覆盖，默认关） |
| `strip_all_image_urls_in_fallback` | bool | `false` | 链末兜底删除**所有** image_url（不仅是 base64） |
| `keep_provider_modality_as_is` | bool | `false` | 不修改 provider modalities（取消"骗 AstrBot"行为） |

完整说明见 `_conf_schema.json`。

## 缓存机制（v0.8.2）

**v0.8.2 重要修复**：之前缓存用图片 URL 字符串作 key，但 AstrBot 每次压缩图都生成**新文件名**（带 hash），同一张图 → 不同路径 → **永不命中**。

现在缓存 key = **`md5(图片内容)`**：

| URL | 内容 | 缓存 key |
| --- | --- | --- |
| `file:///AstrBot/data/temp/compressed_aaa.jpg` | 图 X | `md5:abc123` |
| `file:///AstrBot/data/temp/compressed_bbb.jpg` | 图 X（**同一张**） | `md5:abc123` ← **命中** |
| `https://x.com/y.jpg` | 图 Y | `md5:def456` |

如果读图片字节失败（http 下载超时、文件被删），退到用 URL 字符串作 key。

缓存对 `http(s)://`、`file://` 都生效（默认）。`data:` base64 URL 不缓存（base64 字符串每次都不同）。

## 防止 LLM 改写 / 猜游戏名（3 层防御）

1. **保守的 mmx prompt**：明确告诉 mmx「严禁猜测未明确显示的游戏/番剧/品牌/角色名称，不确定时明说"无法确定"」。
2. **自然格式 `[Image N 描述] xxx`**：让 LLM 把它当作用户描述，而不是"prompt 中的占位符"。
3. **system_prompt 严格引用指令**（默认开启）：告诉 LLM 严格基于图说回答，不要凭印象补充背景知识。

实测：之前 LLM 把"抖音评论区+云南野生菌梗"误判为"永劫无间"——v0.7 之后这个错误不再出现。

## 与 AngelHeart 插件的兼容性

AngelHeart 的 `on_llm_request` 钩子（priority=50）会**重写 `req.prompt`**——之前我的图说注在 prompt 字符串里，会被覆盖丢失。

**v0.7 修复**：图说改为注入到 `req.extra_user_content_parts`（user message content block），不被 AngelHeart 触碰。

**v0.5 链末兜底**：AngelHeart 在 `req.contexts` 里重新塞 `data:image/jpeg;base64,...`。本插件 priority=-10000 的兜底钩子清掉所有 data:base64 残留。

**v0.8 主钩子入口清空**：在 priority=100 入口就清空 image_urls / extra_parts / contexts 中的 image_url——AngelHeart 即使有图片也是空。

## 拦截优先级

| 场景 | 建议 priority |
| --- | --- |
| 默认（够用） | 100 |
| 还有插件抢在前面 | 500 ~ 1000 |
| 调试 / 排错 | 10000 |
| 故意让别的插件先处理图片 | 0 或负值 |

priority 配置在 `import` 时锁定，**修改后需重启 AstrBot**。详见 `main.py` 顶部的 `DEFAULT_PRIORITY` 常量。

## 缓存管理页面

启动后 AstrBot Dashboard → 插件 → Vision → Text Bridge → 「缓存管理」：

- **统计卡片**：总条目、命中总数、DB 大小、内存缓存大小、Chat Archive 联动状态
- **搜索框**：按 URL / 描述模糊匹配
- **排序**：最新 / 最旧 / 命中最多 / 命中最少
- **操作**：单条删除、重新生成、导出 JSON、清空全部（含 VACUUM）
- **Chat Archive 联动刷新**

后端 API（自动注册）：

| 路径 | 方法 | 作用 |
| --- | --- | --- |
| `/cache/stats` | GET | 缓存统计 |
| `/cache/list` | GET | 分页列表 + 搜索 + 排序 |
| `/cache/delete` | POST | 删除单条 |
| `/cache/clear` | POST | 清空全部 |
| `/cache/regenerate` | POST | 重新调 mmx 生成 |
| `/cache/export` | GET | 导出全部为 JSON |
| `/chat-archive/refresh` | POST | 重新检测 Chat Archive |

## 常见问题

### Q1: 看到 `mmx 图像理解失败: ..., error=insufficient balance` 怎么办？

**别只看面板余额**。先手动验证 mmx：

```bash
mmx --version
mmx auth status
mmx quota
mmx vision describe --image /path/to/any.png --prompt "描述"
```

| 1~3 成功、第 4 步报 `insufficient balance` | mmx 版本/路由错 | `npm update -g mmx-cli` |
| --- | --- | --- |
| 第 4 步报 `unauthenticated` / `unauthorized` | API key 权限问题 | 换 key 或检查 key 绑定的环境 |
| 第 4 步报 `model not found` / `unknown model` | mmx 版本过旧 | 同上 |
| 全部成功 | 插件代码 bug | 打开 `verbose_logging: true` + 重发图 + 把日志贴给我 |

开启 `verbose_logging: true` 会输出 mmx 完整 stdout/stderr。

### Q2: AstrBot 报 `'dict' object has no attribute 'model_dump_for_context'`

**v0.7 → v0.8.1 已修复**。拉最新代码重载插件即可。v0.7 之前会有这个崩溃。

### Q3: LLM 还是把图说猜错（比如把"抖音截图"猜成"永劫无间"）

- **先看日志里的 `描述预览:`**（默认开启，不依赖 verbose_logging）—— 这是 mmx **实际**返回的描述。
  - 如果 `描述预览:` 就是错的（mmx 猜错）→ 调 `vision_prompt` 配置，或换 mmx 模型
  - 如果 `描述预览:` 是对的、LLM 仍然猜错 → LLM 模型问题（deepseek-v4-flash 质量差），建议换主 provider

### Q4: 缓存页面显示 `0 条` 但插件在工作

**v0.8.2 之前**：`_is_cacheable_url` 拒绝 `file://`，QQ 群聊场景下从不缓存。
**v0.8.2 之后**：用 md5 作 key，file:// 也缓存。拉最新代码。

### Q5: AstrBot 报 `Chat provider ... does not support image input, switching to fallback`

**v0.8.1 已修复**——插件 `initialize()` 时**骗** AstrBot 主 provider "支持图"（补 `"image"` modality 标签），AstrBot 不会切 fallback。

**前提**：插件在 on_llm_request 钩子入口**已清空** image_urls，主 provider **不会**实际收到图（只是名义"支持"）。

## 离线测试

```bash
cd astrbot_plugin_vision_text_bridge
python3 test.py
```

应看到 `PASS: 85/85`。测试不依赖 AstrBot 真实运行环境，使用 stub 模拟 `astrbot.api`。

## 插件结构

```
astrbot_plugin_vision_text_bridge/
├── main.py                  # 主插件（on_llm_request 钩子、web API 注册）
├── caption_cache.py         # SQLite 描述缓存（独立模块）
├── chat_archive_link.py     # Chat Archive 联动检测
├── _conf_schema.json        # AstrBot 配置 schema
├── metadata.yaml            # AstrBot 插件元数据
├── pages/
│   └── cache-manager/       # AstrBot 内置页面（HTML/JS/CSS）
├── test.py                  # 离线测试（85 个测试）
├── README.md
└── CHANGELOG.md
```

## 参考

- [`astrbot_plugin_uni_nickname`](https://github.com/Hakuin123/astrbot_plugin_uni_nickname) — `@filter.on_llm_request()` 拦截 `ProviderRequest` 的标准用法
- [`astrbot_plugin_MiniMax_CLI`](https://github.com/tanggetian/astrbot_plugin_MiniMax_CLI) — `mmx vision describe` 子进程调用
- [AstrBot 文档](https://docs.astrbot.app/) — `ProviderRequest` 字段说明

## 许可

AGPL-3.0
