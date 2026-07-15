# 用户指令记忆

本文件记录了用户的指令、偏好和教导，用于在未来的交互中提供参考。

## 格式

### 用户指令条目
用户指令条目应遵循以下格式：

[用户指令摘要]
- Date: [YYYY-MM-DD]
- Context: [提及的场景或时间]
- Instructions:
  - [用户教导或指示的内容，逐行描述]

### 项目知识条目
Agent 在任务执行过程中发现的条目应遵循以下格式：

[项目知识摘要]
- Date: [YYYY-MM-DD]
- Context: Agent 在执行 [具体任务描述] 时发现
- Category: [运维部署|构建方法|测试方法|排错调试|工作流协作|环境配置]
- Instructions:
  - [具体的知识点，逐行描述]

## 去重策略
- 添加新条目前，检查是否存在相似或相同的指令
- 若发现重复，跳过新条目或与已有条目合并
- 合并时，更新上下文或日期信息
- 这有助于避免冗余条目，保持记忆文件整洁

## 条目

### Git 提交作者配置
- Date: 2026-07-13
- Context: 用户要求所有 Git 提交使用特定作者
- Instructions:
  - 所有 Git 提交必须使用 `--author="uuutt2023 <584193570@qq.com>"`
  - 远程仓库: `https://github.com/uuutt2023/astrbot_plugin_vision_text_bridge`

### Provider 注册 API 格式与流程
- Date: 2026-07-14
- Context: Agent 在修复 provider 注册失败问题时发现
- Category: 排错调试
- Instructions:
  - AstrBot v4 ProviderConfigRequest 的 `to_dashboard_config()` 排除 `config` 和 `provider_config` 字段，payload 必须平铺到根级别（`id`, `type`, `provider_type`, `key`, `api_key`, `api_base`, `model` 等）
  - 注册流程：先 POST /api/v1/providers，若返回 400 "already exists" 则 fallback PUT /api/v1/providers/by-id?provider_id=xxx
  - Dashboard API 端口 6185，认证用 `X-API-Key` header（需 OpenAPI Key 含 provider scope）
  - Dashboard 启动有竞态，需加重试机制（指数退避 1s-30s，最多 10 次）
  - base_url 用 `http://127.0.0.1:{port}` 而非 `localhost`，避免 DNS 解析问题
  - payload 示例: `{"provider_id": "xxx", "provider_source_id": "openai_source", "id": "xxx", "enable": true, "type": "openai_chat_completion", "provider_type": "chat_completion", "key": [...], "api_key": "...", "api_base": "...", "model": "..."}`

### 插件端口分配
- Date: 2026-07-13
- Context: Agent 在开发过程中确认端口配置
- Category: 环境配置
- Instructions:
  - 6185 = AstrBot Dashboard（provider 注册目标）
  - 2023 = 插件独立 OpenAI 兼容 server（被调用端点 api_base）
  - 默认值定义在 `constants.py`: `DEFAULT_DASHBOARD_PORT=6185`, `DEFAULT_OPENAI_COMPAT_PORT=2023`

### Git 远程仓库
- Date: 2026-07-13
- Context: Agent 在执行构建验证时确认
- Category: 运维部署
- Instructions:
  - 远程: `origin/main`, URL: `https://github.com/uuutt2023/astrbot_plugin_vision_text_bridge`
  - 插件名称: `astrbot_plugin_vision_text_bridge`
  - 每次修复后必须 push 到 origin/main
