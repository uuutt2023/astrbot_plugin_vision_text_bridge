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
| `verbose_logging` | bool | `false` | 冗余日志：on_llm_request 触发时输出图片计数。调试不生效时开启 |

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

## 与 AngelHeart 插件的兼容性

AngelHeart（[astrbot_plugin_angel_heart](https://github.com/kawayiYokami/astrbot_plugin_angel_heart)）有自己的图片转述逻辑。
日志中看到这类错误是 AngelHeart 内部调用、**与本插件无关**：

```
[ERRO] [v4.25.2] [core.conversation_ledger:651]: AngelHeart: 图片转述失败:
[Errno 36] File name too long: 'data:image/webp;base64,UklGRkAFAAB...'
```

### AngelHeart 内部究竟在做什么？

AngelHeart 在两处会处理图片：

1. **秘书决策后** （`roles.front_desk`）→ `caption_provider.text_chat(image_urls=[base64_data_url])`：
   AngelHeart 把图片压缩为 webp、编码成 `data:image/webp;base64,xxx` 形式的 data URL，
   再作为 `image_urls` 列表传给 caption provider。caption provider 内部可能会把第一个 URL 当文件路径打开，
   于是 `os.open` 拿到一个几百 KB 的 base64 字符串作为路径 → `File name too long`。

2. **on_llm_request 钩子 (priority=50)** → `rewrite_prompt_for_llm`：
   AngelHeart 会重写 `req.contexts`，把每条消息中的 Image 组件用 `convert_to_base64()` 转成
   `data:image/jpeg;base64,xxx` 后重新塞进 OpenAI 格式 content 里。**这条路径不走 caption provider**，
   所以 base64 会原封不动地出现在最终 LLM 请求中。

### 为什么本插件拦不住？

- **“决策后”那次 caption 调用** 是 AngelHeart **直接调** `caption_provider.text_chat()`，
  **不走任何 AstrBot 钩子**。本插件 priority 调多高都管不到这段代码。
- **“on_llm_request”那次重写** 确实是钩子，但本插件 priority=100 先跑、清理 `req.contexts` 后，
  AngelHeart priority=50 会**重新往里塞 base64**，把本插件的清理覆盖。

### 链末兜底机制

为缓解问题，本插件除了主钩子（priority=100）外，**还注册了一个 priority=-10000 的链末兜底钩子**。
它不做图像理解，只做一件事：**删除所有 `data:image/...;base64,...` 形式的残留**。
这样无论中间有什么插件往 req 里塞 base64，到最后一个钩子都会被拦下丟除。
代价是：这些 base64 图片在最终 LLM 请求中会以“图片：[已省略]”占位，
**不会被转成可读文本**。如果需要转述，请按下面的步骤完全解决问题。

### 完全解决步骤

按顺序执行：

1. **在 AngelHeart 配置中禁用图片转述**：
   在 AstrBot 管理面板打开 AngelHeart 的设置，将 `image_caption_provider_id` **留空**。
   保存后 AngelHeart 不会再调 caption，也就不会再出 `File name too long`。

2. **确认本插件 priority 足够高**：
   本插件默认 priority=100，高于 AngelHeart 的 priority=0 和 priority=50，
   所以**本插件会先于 AngelHeart 跑**。如果看不到 `[vision_text_bridge] on_llm_request 触发` 日志，
   请打开 `verbose_logging: true`。

3. **重启 AstrBot** 让 priority 配置生效。

4. **验证**：发一张图，预期看到这些日志：
   - `[vision_text_bridge] on_llm_request 触发: image_urls=N, ...`
   - `[vision_text_bridge] 图像理解完成: ..., 耗时=...s, 长度=...`
   - `[vision_text_bridge] 链末兜底: 删除了 0 个 data:base64 image_url 残留`
     （非 0 说明 AngelHeart 仍往里塞，需检查 AngelHeart 配置）
   - LLM 回复中能读出图片描述（如 “图片中是一只… ”）

### 调试技巧

打开 `verbose_logging: true`，每次 LLM 请求都会多一行：

```
[vision_text_bridge] on_llm_request 触发: image_urls=2, extra_parts_images=0,
contexts_with_images=3, priority=100
```

如果这行完全看不到 → 插件未被加载，请检查 AstrBot 启动日志。
如果看到但 `image_urls=0` 且 `contexts_with_images=0` → 图片在别的地方（AngelHeart 内部存储），
请按上面“完全解决步骤”处理。

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
