# astrbot_plugin_vision_text_bridge

> 把 LLM 请求里的图片转成 MiniMax CLI 图像理解的文本，再交给对话模型。

## 它做了什么

在 AstrBot 把消息发给 LLM 之前，本插件会：

1. 拦截 `ProviderRequest`（基于 `astrbot_plugin_uni_nickname` 的 `@filter.on_llm_request()` 用法）；
2. 扫描 `req.image_urls`、`req.extra_user_content_parts`、`req.contexts` 三处可能藏图片的地方；
3. 对每张图片调用 `mmx vision describe --image <url> --prompt <...>`（参考 `astrbot_plugin_MiniMax_CLI`）；
4. 把返回的描述用 `【图片{n}：<描述>】` 模板回填到 `req.prompt`；
5. 把图片从原字段里删除，让 LLM 收到的是纯文本请求。

最终用户看到的是：LLM 能“看懂”图片内容（因为它读到了图片描述），但请求里没有真实的图片二进制，**纯文本多模态替代**。

## 为什么需要它

- **省钱 / 限流**：很多 LLM 在 Vision 通道要额外计费。文本 token 一般比 Vision 便宜。
- **老模型兼容**：不支持 Vision 的 LLM 也能“看图”（虽然理解质量取决于 mmx 返回的描述）。
- **统一格式**：所有图片都按 `【图片：...】` 格式喂给 LLM，prompt 模板更可控。

## 前置依赖

1. 安装 [mmx-cli](https://www.npmjs.com/package/mmx-cli)（MiniMax 官方 CLI 工具）：
   ```bash
   npm install -g mmx-cli
   ```
2. AstrBot ≥ 4.0.0。
3. MiniMax API Key（在 [MiniMax 开放平台](https://platform.MiniMax.io/) Token Plan 页面获取，`sk-` 开头）。

如果不想手动安装，插件支持 `auto_install_cli: true` 自动 `npm install -g mmx-cli`。

## 登录 MiniMax CLI

插件支持两种方式预登录 mmx（**二选一即可**）：

| 方式 | 配置 | 说明 |
| --- | --- | --- |
| 插件自动登录 | `minimax_api_key` + `auto_login: true` | 插件 `initialize()` 时执行 `mmx auth login --api-key <key>`，推荐 |
| 手动登录 | 留空 `minimax_api_key` | 在服务器上执行 `mmx auth login --api-key <your-key>`，插件直接用已登录的会话 |

`minimax_api_key` 在插件配置面板中是密码型输入框，**不会以明文写入 AstrBot 配置文件**。

## 安装

1. 把整个 `astrbot_plugin_vision_text_bridge/` 目录复制到 AstrBot 的 `data/plugins/` 下。
2. 重启 AstrBot，插件会自动加载。
3. 在 AstrBot 管理面板的插件配置里，按需调整参数（详见下方“配置项”）。

## 配置项

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enabled` | bool | `true` | 总开关 |
| `priority` | int | `100` | 拦截优先级。值越大，on_llm_request 钩子越先执行。修改后需重启 AstrBot 生效，详情见下方「拦截优先级」 |
| `mmx_path` | string | `""` | mmx 可执行文件绝对路径，留空时从 `PATH` 找 |
| `minimax_api_key` | string (password) | `""` | MiniMax API Key（`sk-` 开头）。填写后插件初始化时自动 `mmx auth login` |
| `auto_login` | bool | `true` | 插件初始化时是否使用 `minimax_api_key` 执行 `mmx auth login` |
| `auto_install_cli` | bool | `false` | 找不到 mmx 时自动 `npm install -g mmx-cli` |
| `command_timeout` | int | `60` | 单次 mmx vision describe 超时（秒） |
| `max_concurrent_vision` | int | `3` | 单条消息最多并发图像理解数 |
| `vision_prompt` | text | `"请用中文详细描述这张图片..."` | 传给 mmx 的提示词 |
| `image_placeholder_template` | string | `【图片{index}：{description}】` | 占位符模板 |
| `max_description_length` | int | `800` | 单图描述最大字符数（0 不限制） |
| `include_history` | bool | `false` | 是否处理 `req.contexts` 历史中的图片 |
| `include_extra_parts` | bool | `true` | 是否处理 `extra_user_content_parts` 中的图片 |
| `failure_message` | string | `【图片{index}：理解失败：{error}】` | mmx 调用失败时的占位文本 |
| `redact_sensitive` | bool | `true` | 日志中脱敏 API Key 等 |
| `cache_descriptions` | bool | `true` | 缓存同 URL 的图像理解结果 |

## 效果示例

用户发：`"帮我看看这只猫"` + 一张橘猫图片

经过插件后，LLM 实际收到的 prompt（简化）：

```
帮我看看这只猫

【图片1：图片中是一只橘色的猫正趴在米色沙发上，背景有一扇窗户，猫的眼睛是绿色的】
```

`image_urls` 字段已被清空，LLM 不再以多模态方式处理图片，而是基于文本描述回答。

## 注意事项

- mmx 调用是同步阻塞的，**首次发图会有一段延迟**（通常 5~20 秒）。
- 缓存仅对 `http(s)://` URL 生效，base64 / `file://` / 本地路径不缓存（避免文件已删除后缓存悬空）。
- 历史图片默认不处理（`include_history: false`）。开启会增加上下文 token 消耗，请评估成本。
- 失败的图片会保留在 `image_urls` 里，并插入失败占位文本，避免 LLM 不知道有图。

## 拦截优先级

AstrBot 的 `on_llm_request` 钩子按 **priority 降序** 执行：值越大越先运行。
本插件默认 priority=100，高于多数常见插件（多为 0，如 `uni_nickname`），
能保证抢在它们处理图片之前先把图片转成文本。

### 为什么需要高优先级？

如果你看到类似这样的日志：

```
[ERRO] [v4.25.2] [core.conversation_ledger:651]: 图片转述失败:
[Errno 2] No such file or directory: 'data:image/webp;base64,UklGRk...'
```

或者：

```
[ERRO] [v4.25.2] [astrbot_plugin_sylanne.main:1031]: ... error
```

说明有别的插件在**本插件之前**碰到图片，并且处理方式不当（比如把 base64 当成文件路径）。
本插件的 priority 提到 100 之后，会在它们之前把图片转成纯文本，避免下游插件踩到原始图片。

### 调整方式

**方式一：插件配置项（推荐）**

在 AstrBot 管理面板修改本插件的 `priority` 值（例如设为 `500` 或 `1000`），重启 AstrBot。
启动日志会提示：

```
[WARN] [vision_text_bridge] priority 配置=500，但当前注册的 priority=100。
AstrBot 的 on_llm_request priority 在 import 时锁定，
需要重启 AstrBot / 重新加载本插件后新值才会生效。
```

重启后新值生效。

**方式二：直接编辑源码**

打开 `main.py` 顶部，修改：

```python
DEFAULT_PRIORITY = 100
```

改为期望值（`500`、`1000` 等），然后重启 AstrBot。适合需要长期保留某个 priority 值的场景。

### 建议值

| 场景 | 建议 priority |
| --- | --- |
| 默认（够用） | 100 |
| 还有插件抢在前面 | 500 ~ 1000 |
| 调试/排错 | 10000 |
| 故意让别的插件先处理图片 | 0 或负值 |

## 离线测试

```bash
cd astrbot_plugin_vision_text_bridge
python3 test.py
```

应看到：

```
PASS: 20/20
```

测试不依赖 AstrBot 真实运行环境，使用 stub 模拟 `astrbot.api` 模块与 `ProviderRequest`。

## 参考

- [`astrbot_plugin_uni_nickname`](https://github.com/Hakuin123/astrbot_plugin_uni_nickname) — `@filter.on_llm_request()` 拦截 `ProviderRequest` 的标准用法
- [`astrbot_plugin_MiniMax_CLI`](https://github.com/tanggetian/astrbot_plugin_MiniMax_CLI) — `mmx vision describe` 子进程调用与命令构建方式
- [AstrBot 文档](https://docs.astrbot.app/) — `ProviderRequest` 字段说明
