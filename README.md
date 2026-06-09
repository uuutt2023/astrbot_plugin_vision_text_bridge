# astrbot_plugin_vision_text_bridge

> 在消息发往大模型之前，把图片换成 MiniMax 图像理解服务返回的描述文本。
>
> 简单说：让 LLM 「看懂」图片，但请求里**没有**真实图片——只有一段人话描述。

[![AstrBot](https://img.shields.io/badge/AstrBot-%E2%89%A54.0.0-blue)](https://docs.astrbot.app/)
[![Python](https://img.shields.io/badge/Python-%E2%89%A53.10-green)](https://www.python.org)
[![License](https://img.shields.io/badge/license-AGPL--3.0-orange)](LICENSE)

[更新日志](CHANGELOG.md) · [问题反馈](https://github.com/uuutt2023/astrbot_plugin_vision_text_bridge/issues) · [AstrBot 文档](https://docs.astrbot.app/)

## 它做了什么

当用户在群里发了一张图、AstrBot 准备把消息转给大模型时，本插件会**拦在中间**做这些事：

1. 抓住这条发往 LLM 的请求（用「拦截 LLM 请求」这个标准入口，优先级 100，越大越先执行）；
2. 把请求里**所有可能藏图片的地方**都翻一遍——主字段、用户消息片段、历史聊天记录，一个不漏；
3. 对每张图片，调用 MiniMax 图像理解服务，让它用中文描述图里有什么；
4. 把描述以「『第 1 张图的描述是：xxx』」这种自然人话格式，塞进用户消息正文；
5. 把请求里原本的真实图片**全部清空**——大模型只会看到文字描述，不会真的去解析图片二进制；
6. 描述结果**存到本地 SQLite 数据库**——下次发同一张图直接命中缓存，不再调一次 MiniMax；
7. **自带可视化页面**，可以搜索 / 删除 / 重新生成缓存；
8. 在消息链**最末端**兜底一次：万一有别的插件在中间把图片又塞回来，这里统一清掉。

## 为什么需要它

- **省钱 / 限流**：图像理解通道要额外计费，文本便宜很多。同一个图描述一次，后面都吃缓存。
- **老模型也能用**：不支持图像输入的老模型，吃到文字描述一样能「看图」（质量取决于 MiniMax 描述得多准）。
- **统一格式**：所有图描述都按同一段话模板塞进去，prompt 写起来更可控。
- **不切低质量备用模型**：插件会**骗** AstrBot 说主模型「支持图」（实际只发文本），主模型就不会因为「检测到图」而自动切到效果更差的备用模型。

## 前置条件

1. 安装 MiniMax 官方命令行工具（`mmx`，是调用 MiniMax 图像理解服务的小工具）：
   ```bash
   npm install -g mmx-cli
   ```
2. AstrBot 版本 **≥ 4.0.0**。
3. 一把 MiniMax API Key（`sk-` 开头），在 [MiniMax 开放平台](https://platform.MiniMax.io/) 的 Token Plan 页面申请。

如果开了「找不到 mmx 时自动安装」，插件会用 npm 自动装上 mmx。

## 安装步骤

1. 把 `astrbot_plugin_vision_text_bridge/` 整个目录复制到 AstrBot 的插件目录 `<AstrBot>/data/plugins/` 下。
2. 重启 AstrBot，插件自动加载。
3. 在 AstrBot 管理面板的插件配置里填上 MiniMax API Key（开了「启动时自动登录」的话，插件启动时会自动登录 mmx）。
4. **打开缓存页面**：AstrBot 控制台 → 插件 → Vision → Text Bridge → 「缓存管理」。

## 配置项一览

下面这些是在 AstrBot 插件配置页面里能调的所有开关，按使用频率分块列出。

### 总开关

| 开关 | 默认 | 啥意思 |
| --- | --- | --- |
| 总开关 | 开启 | 关掉之后插件就完全罢工了。 |
| 拦截优先级 | 100 | 数字越大越先执行。如果有别的插件抢在前面，可以调大点。**改完要重启 AstrBot 才生效。** |
| 找不到 mmx 时自动安装 | 关闭 | 开了之后，检测不到 mmx 就自动用 npm 装上。 |

### MiniMax 图像理解调用

| 开关 | 默认 | 啥意思 |
| --- | --- | --- |
| mmx 可执行文件路径 | 空 | 留空就自动从系统环境变量里找。 |
| MiniMax API Key | 空 | 你的 MiniMax 钥匙。 |
| 启动时自动登录 mmx | 开启 | 插件启动时自动用 API Key 登录 mmx 一次。 |
| 单次图像理解超时（秒） | 60 | 调 MiniMax 时等多久算「卡死了，超时吧」。 |
| 单条消息最多并发处理几张图 | 3 | 群里一次发 10 张图的话，最多 3 张同时处理。 |
| 提示词 | 保守描述模板 | 传给 MiniMax 的要求，**严禁它瞎猜**游戏/番剧/品牌名。 |
| 图描述格式 | 「『第 N 张图的描述是：xxx』」 | 塞进 LLM 用户消息里的格式。**别瞎改**，LLM 识别这个格式才引用。 |
| 单图描述最大字符数 | 800 | 太长的描述会截断。设 0 = 不限制。 |
| 描述失败时的占位文本 | 「『第 N 张图的描述是：理解失败：xxx』」 | MiniMax 报错时塞给 LLM 的话。 |

### 缓存

| 开关 | 默认 | 啥意思 |
| --- | --- | --- |
| 启用缓存 | 开启 | 同一张图不重复调 MiniMax。**强烈建议开**。 |
| 缓存本地文件路径 | 开启 | 群聊里 AstrBot 经常把图存成本地临时文件，开了才缓存得上。 |
| 缓存键 | 图片内容指纹 | 同一张图不管它 URL 怎么变都能命中——靠的是图片自己的 md5 哈希。 |
| 缓存保留天数 | 30 | 超过这个天数的缓存定期清理。 |

### LLM 提示词

| 开关 | 默认 | 啥意思 |
| --- | --- | --- |
| 向系统提示词注入「严格引用图描述」指令 | 开启 | 提醒 LLM：老老实实根据图描述回答，别自己脑补游戏名/番剧名。 |
| 同时把图描述也塞进系统提示词 | 关闭 | 双重保险——再塞一份到系统提示词。默认关，因为会污染提示词风格。 |
| 强制不切到备用模型 | 开启 | 骗 AstrBot 说「主模型支持图」，主模型就不会因为看到图就切到更差的备用。**强烈建议开**。 |
| 清理所有图片字段 | 关闭 | 在消息链最末端把请求里**所有**图片字段全清空（不仅是 base64 内嵌）。 |

### 调试 / 日志

| 开关 | 默认 | 啥意思 |
| --- | --- | --- |
| 总日志开关 | 关闭 | 调试不生效时先开这个。 |
| 详细日志：钩子拦截阶段 | 关闭 | 想知道「插件有没有真的拦到这条请求」时开。 |
| 详细日志：mmx 子进程 | 关闭 | mmx 调用失败、不知道报什么错时开（带脱敏）。 |
| 详细日志：缓存命中 | 关闭 | 想知道「同一张图为啥没命中缓存」时开。 |
| 详细日志：图片 ID 计算 | 关闭 | 想知道「同一张图被算成了不同 ID」时开。 |
| 日志脱敏 API Key | 开启 | **默认开**——避免把 API Key 打到日志里泄露。 |

完整字段说明见配置文件 `_conf_schema.json`。

## 缓存机制

**核心问题**：之前缓存用图片 URL 字符串当索引，但 AstrBot 每次压缩图片都会生成**新文件名**（带哈希后缀）。同一张图 → 不同的临时文件路径 → 缓存**永远命中不了**，每次都重调 MiniMax。

**现在的做法**：缓存索引 = **图片内容自己的 md5 哈希**。同一张图不管 URL/路径怎么变，md5 一样就命中。

举例：

| 图片 URL 字段 | 图片内容 | 缓存索引 |
| --- | --- | --- |
| `/AstrBot/临时文件/压缩版_aaa.jpg` | 图 X | `图片 X 的指纹 abc123` |
| `/AstrBot/临时文件/压缩版_bbb.jpg` | 图 X（**同一张**） | `图片 X 的指纹 abc123` ← **命中** |
| `https://某图床/y.jpg` | 图 Y | `图片 Y 的指纹 def456` |

如果因为网络/文件问题读不到图片字节，就临时退回用 URL 字符串当索引（这种 fallback 命中率低，但至少不报错）。

支持缓存的 URL 类型：
- 网络图片（`http://` / `https://`）— 默认缓存
- 本地文件（`file://` 开头或裸路径）— 默认缓存
- 内嵌的 base64 图片（`data:image/...`）— **不缓存**（base64 字符串每次都不同，缓存没意义）

## 防止 LLM 瞎猜游戏名（3 层防御）

之前最常见的翻车：用户发了一张「抖音评论区 + 云南野生菌梗」的截图，LLM 直接脑补成「永劫无间」游戏截图（因为它脑子里有「云南 + 二次元」的关联）。本插件从三个层面防御这种情况：

1. **MiniMax 提示词**：明确告诉 MiniMax「**严禁**猜游戏/番剧/品牌名，没看清就说『无法确定』」。
2. **自然语言格式**：「『第 1 张图的描述是：xxx』」让 LLM 把它**当作用户的描述**，而不是「prompt 里的占位符」。LLM 对占位符会自动脑补，对真实人话反而老实。
3. **系统提示词指令**（默认开）：再额外提醒 LLM「严格基于图描述回答，不要凭印象补充背景知识」。

实测：v0.7 之后这个错误就再没出现过。

## 与 AngelHeart 插件的兼容性

AngelHeart 是个很常用的群聊记忆插件，但它有个习惯：**把发给 LLM 的提示词整个改写一遍**。如果图描述是塞在提示词字符串里的，会被它改写得面目全非。

**修复方案**：图描述改塞到用户消息的「内容片段」里（这是种不会被其它插件重写的位置），AngelHeart 拿不到。

**消息链最末端兜底**：AngelHeart 喜欢在历史消息里塞「内嵌的 base64 格式图片」。本插件在消息链最末端（优先级 -10000，最后一个跑）会做一次清理，把所有 base64 内嵌图删掉。

**主钩子入口清空**：优先级 100 的一进来，就把请求里所有图片字段全清空——AngelHeart 即便想塞也塞不回来。

## 拦截优先级

| 你要的场景 | 建议优先级 |
| --- | --- |
| 默认（绝大多数情况够用） | 100 |
| 还有别的插件抢在前面 | 500 ~ 1000 |
| 调试 / 排错 | 10000 |
| 故意让别的插件先处理图片 | 0 或负数 |

优先级在插件加载时就锁定了，**改完要重启 AstrBot**。详见 `main.py` 顶部的 `DEFAULT_PRIORITY` 常量。

## 缓存管理页面

启动后 AstrBot 控制台 → 插件 → Vision → Text Bridge → 「缓存管理」：

- **顶部统计卡片**：总条目、命中总数、数据库大小、内存缓存大小
- **搜索框**：按 URL / 描述模糊匹配
- **排序**：最新 / 最旧 / 命中最多 / 命中最少
- **缩略图**：每条记录都从 SQLite 里读图片二进制出图，点击看大图弹窗
- **操作**：单条删除、重新生成、导出 JSON、清空全部（含数据库整理）
- **快捷键**：`R` 刷新列表

页面通过 AstrBot 提供的页面通信通道和后端对话，**不需要单独的端口**——访问页面的 URL 是 AstrBot Dashboard 里的插件页面入口。

后端 API（插件自动注册）：

| 接口 | 作用 |
| --- | --- |
| `GET /astrbot_plugin_vision_text_bridge/cache/stats` | 缓存统计 |
| `GET /astrbot_plugin_vision_text_bridge/cache/list` | 分页列表 + 搜索 + 排序 |
| `POST /astrbot_plugin_vision_text_bridge/cache/delete` | 删除单条 |
| `POST /astrbot_plugin_vision_text_bridge/cache/clear` | 清空全部 |
| `POST /astrbot_plugin_vision_text_bridge/cache/regenerate` | 重新调 MiniMax 生成 |
| `GET /astrbot_plugin_vision_text_bridge/cache/export` | 导出全部为 JSON |
| `GET /astrbot_plugin_vision_text_bridge/cache/thumbnail` | 缩略图接口，参数是图片 ID |

## Webui 设计参考

缓存管理页面的**视觉风格**参考了另一个 AstrBot 插件的样式：

- Inter 字体 + 暗色玻璃卡片（带模糊和饱和度增强效果）
- 主题色用 CSS 变量驱动，主色靛蓝、副色翠绿、底色深蓝黑
- 三个氛围光晕（径向渐变 + 模糊）在背景里营造空间感
- 顶部品牌栏：图标 + 渐变文字 + 等宽字体小标签

**但本插件和那个插件完全独立**——本插件的页面**不**引用它的任何资源，单独装本插件也照样能用。

## 常见问题

### Q1: 看到 `MiniMax 图像理解失败: 余额不足` 怎么办？

**别只看面板余额**。先手动验证 MiniMax 命令行：

```bash
mmx --version
mmx auth status
mmx quota
mmx vision describe --image /path/to/any.png --prompt "描述"
```

| 验证结果 | 原因 | 怎么修 |
| --- | --- | --- |
| 前面 3 步成功、第 4 步报「余额不足」 | mmx 版本太老 / 路由错了 | 升级 mmx：`npm update -g mmx-cli` |
| 第 4 步报「未登录」/「无权限」 | API Key 权限问题 | 换 Key 或检查 Key 绑定的环境 |
| 第 4 步报「找不到模型」 | mmx 版本太老 | 同上 |
| 全部成功 | 插件代码 bug | 打开「总日志开关」+ 重发图 + 把日志贴出来分析 |

### Q2: AstrBot 报 `'dict' object has no attribute 'model_dump_for_context'`

之前的版本有这个崩溃，**已经修复**。拉最新代码重载插件即可。

### Q3: LLM 还是把图描述猜错（比如把"抖音截图"猜成"永劫无间"）

- **先看日志里的「描述预览」**（默认开启，不依赖详细日志）—— 这是 MiniMax **实际**返回的描述。
  - 如果「描述预览」本身就是错的（MiniMax 自己猜错）→ 调「提示词」配置，或换 MiniMax 模型
  - 如果「描述预览」是对的、LLM 仍然猜错 → LLM 模型问题（部分质量差的小模型就是这样），建议换主模型

### Q4: 缓存页面显示 `0 条` 但插件在工作

之前的版本有个 bug：缓存判定只认网络图片，本地文件从来不缓存。**已经修复**——现在本地文件也缓存。拉最新代码。

### Q5: AstrBot 报 `Chat provider ... does not support image input, switching to fallback`

主模型说「我不支持图」，AstrBot 自动切到了更差的备用模型。**已经修复**——插件启动时会**骗**主模型说「支持图」（补一个「支持图片」标签），主模型就不会切。

**前提**：插件在入口已经清空了图片字段，主模型**不会**真的收到图——只是名义上「支持」。

## 详细日志怎么开

调试不生效时，按下面 5 个开关按需打开。**默认全关**——拆成 4 个细粒度是为了定位到具体阶段后只开对应项，避免日志爆炸。

| 开关 | 作用 | 啥时候开 |
| --- | --- | --- |
| 总日志开关 | 打开后下面 4 个细粒度全部生效 | 不知从哪看起时先开这个 |
| 详细日志：钩子拦截阶段 | 「拦截 LLM 请求」这个钩子的入口/出口 + 处理的图片数 | 「插件是不是拦到这条请求了」 |
| 详细日志：mmx 子进程 | mmx 完整命令（脱敏）+ 完整输出 | **MiniMax 调失败**、不知道报什么错 |
| 详细日志：缓存命中 | 内存 / SQLite 缓存命中 + SQLite 写 | **同一张图为啥没命中**、写缓存是否报错 |
| 详细日志：图片 ID 计算 | 图片指纹 (md5) 计算过程 + 退路原因 | **同一张图被算成不同 ID** |

**总开关**（最常用）：
```json
{ "verbose_logging": true }
```

**精确定位**（以 MiniMax 调不通为例）：
```json
{ "verbose_mmx_subprocess": true }
```

**叠加使用**（查「同一张图为啥不命中」可能要看缓存 + ID 两边）：
```json
{ "verbose_cache_trace": true, "verbose_id_computation": true }
```

**推荐流程**：
1. 开总日志看总体。
2. 定位到阶段后，关掉总开关，只留对应细粒度。
3. 调完后**全关**，避免影响生产日志。

## 离线测试

```bash
cd astrbot_plugin_vision_text_bridge
python3 test.py
```

应看到 `PASS: 175/175`。测试不依赖 AstrBot 真实运行环境，用桩模块模拟 `astrbot.api`，可以在没装 AstrBot 的开发机上跑。

## 插件结构

经过一次大重构后，插件从单个 1700 多行的大文件拆成 8 个单一职责的小模块：

```
astrbot_plugin_vision_text_bridge/
├── main.py                  # 插件主入口：生命周期 + 拦截钩子 + 业务核心（1121 行）
├── web_api.py               # 缓存管理页面的 10 个后端接口（独立模块，0 嵌套）
├── mmx_runner.py            # MiniMax 命令行调用 + 错误诊断 + 日志脱敏（独立模块）
├── caption_cache.py         # SQLite 描述缓存（带图片二进制 + 元信息）
├── image_utils.py           # 从消息片段里提取图片 URL 的工具
├── image_meta.py            # 图片元信息嗅探（mime / 宽高）+ 缓存策略判断
├── image_fetch.py           # 异步读图片字节（支持网络、本地、文件协议）
├── tool_filter.py           # 工具调用过滤（2 阶段过滤跨插件的工具集合）
├── config_helpers.py        # 配置读取小工具（数字 / 字符串）
├── _conf_schema.json        # AstrBot 配置 schema
├── metadata.yaml            # AstrBot 插件元数据
├── pages/
│   └── cache-manager/       # AstrBot 内置页面（HTML/JS/CSS，玻璃拟态风格）
├── test.py                  # 离线测试（175 个测试用例）
├── README.md
└── CHANGELOG.md
```

每个模块聚焦一件事，handler 嵌套深度从 4 层降到 1 层，新加功能知道往哪个文件加。

## 参考资料

- [`astrbot_plugin_uni_nickname`](https://github.com/Hakuin123/astrbot_plugin_uni_nickname) — 「拦截 LLM 请求」钩子的标准用法参考
- [`astrbot_plugin_MiniMax_CLI`](https://github.com/tanggetian/astrbot_plugin_MiniMax_CLI) — 调用 `mmx vision describe` 子进程的参考实现
- [AstrBot 文档](https://docs.astrbot.app/) — 拦截 LLM 请求的字段说明

## 许可

AGPL-3.0
