# ACP 协议边界 — geminimcp v2.3.0

ACP v1 (protocolVersion: 1) 实现覆盖。

规范: https://agentclientprotocol.com

Gemini CLI: v0.36.0-nightly (2026-03-18)

## 已实现的方法

| 方法 | 用途 | 备注 |
| ---- | ---- | ---- |
| `initialize` | 握手 | `protocolVersion: 1` 必需; clientCapabilities: `{}`（不声明 fs/terminal，避免回调死锁） |
| `session/new` | 创建会话 | cwd + mcpServers（支持 stdio/http/sse，失败时降级为空列表） |
| `session/load` | 恢复会话 | {sessionId, cwd, mcpServers} → 复用历史，重置 turn 计数 |
| `session/prompt` | 发送 prompt | ContentBlock[]: text + image + resource（视 agent 能力） |
| `session/cancel` | 取消操作 | 超时自动触发 + 手动取消 |
| `session/set_mode` | 设置审批模式 | yolo/autoEdit/default/plan |
| `session/request_permission` | 权限请求 | 自动批准首选项 `{"selected": id}` |

## 已实现的 sessionUpdate 类型

| 类型 | 映射到 | 备注 |
| ---- | ------ | ---- |
| `agent_message_chunk` | result.agent_messages | 流式文本拼接 |
| `agent_thought_chunk` | result.thought | Agent 推理过程 |
| `tool_call` | result.tool_calls[] | id, title, kind, status |
| `tool_call_update` | tool_calls[].status | 状态变更 |
| `plan` | result.plan | 执行计划条目 |
| `available_commands_update` | (drain) | 会话建立时消费 |
| `config_options_update` | (drain) | 会话建立时消费 |
| `current_mode_update` | (drain) | 模式切换确认 |

## ContentBlock 支持

| 类型 | 支持 | 备注 |
| ---- | ---- | ---- |
| `text` | 是 | 始终可用 |
| `image` | 是 | agentCapabilities.promptCapabilities.image = true |
| `resource` | 是 | 通过 `context` 参数注入文本。格式: `{type:"resource", resource:{uri, mimeType:"text/*", text}}` |
| `resource_link` | 否 | Gemini API 返回 400（不支持 octet-stream） |
| `audio` | 否（agent 支持） | Agent 报告 audio=true 但 bridge 未实现 |

## 未实现（ACP 规范存在但跳过）

| 功能 | 原因 |
| ---- | ---- |
| `session/fork` | RFD 状态 |
| `session/stop` | RFD 状态 |
| `session/set_config_option` | 模型切换需要重启进程 |
| fs 回调 (`fs/read_text_file` 等) | Gemini 使用自带工具；声明会导致死锁 |
| terminal 回调 | 同 fs |
| 斜杠命令 | MCP 层不需要 |

## MCP Server 透传

geminimcp 从三个来源发现 MCP server，通过 `session/new` 的 mcpServers 传递：

1. 用户级 `~/.gemini/settings.json` → `mcpServers`（`gemini mcp add --scope user`）
2. 项目级 `.gemini/settings.json` → `mcpServers`（`gemini mcp add`）
3. 扩展 `~/.gemini/extensions/*/gemini-extension.json` → `mcpServers`

**降级**: 如果带 mcpServers 的 session/new 失败，自动用空列表重试。

**传输格式**（通过 ACP 协议探测验证）:

- stdio: `{name, command, args, env:[]}`
- HTTP: `{name, type:"http", url, headers:[]}`
- SSE: `{name, type:"sse", url, headers:[]}`

## Gemini CLI 模式

| 模式 | id | 行为 |
| ---- | -- | ---- |
| 默认 | `default` | 每个操作需要审批 |
| 自动编辑 | `autoEdit` | 编辑自动批准，其他需审批 |
| YOLO | `yolo` | 全部自动批准 |
| 计划 | `plan` | 只读模式（0.34.0 新增） |

sandbox=false → yolo; sandbox=true → default

注意: CLI `--approval-mode` 使用 `auto_edit`（下划线），ACP `availableModes` 报告 `autoEdit`（驼峰）。Bridge 使用 ACP id。

## 可用模型（来自 session/new）

| modelId | 说明 |
| ------- | ---- |
| `auto-gemini-3` | 内部自动路由: pro / flash（CLI 默认值，不可作为 `--model` 参数） |
| `auto-gemini-2.5` | 内部自动路由: 2.5-pro / 2.5-flash（不可作为 `--model` 参数） |
| `gemini-3.1-pro-preview` | 最新 pro |
| `gemini-3-flash-preview` | 最新 flash |
| `gemini-2.5-pro` | 上一代 pro |
| `gemini-2.5-flash` | 上一代 flash |
| `gemini-2.5-flash-lite` | 轻量 flash |

## 认证方式（来自 initialize）

| id | 名称 | 备注 |
| -- | ---- | ---- |
| `oauth-personal` | Google 登录 | 默认 |
| `gemini-api-key` | Gemini API 密钥 | Developer API |
| `vertex-ai` | Vertex AI | GenAI API |
| `gateway` | AI API 网关 | 自定义网关（0.34.0 新增） |

## Gemini CLI 子代理

通过 `.gemini/settings.json` → `experimental.enableAgents: true` 启用。

| 子代理 | 用途 | 配置 |
| ------ | ---- | ---- |
| `codebase_investigator` | 代码分析、依赖追踪 | `codebaseInvestigatorSettings.enabled` |
| `generalist_agent` | 任务路由到专家 | 始终可用 |
| `browser_agent` | Chrome 自动化 | `agents.overrides.browser_agent.enabled` |
| `cli_help` | CLI 功能/配置查询 | 始终可用 |

自定义子代理: `.gemini/agents/*.md`，YAML frontmatter 格式。

## Gemini CLI 新功能 (0.34.0-preview.0)

| 功能 | CLI 标志/命令 | ACP 影响 |
| ---- | ------------ | -------- |
| 策略引擎 | `--policy <file/dir>` | 替代已弃用的 `--allowed-tools` |
| 会话持久化 | `--resume`, `--list-sessions`, `--delete-session` | CLI 级别; ACP 有 loadSession=true |
| 技能管理 | `gemini skills list/install/link/enable/disable/uninstall` | 自动发现 `~/.agents/skills/` |
| Hook 迁移 | `gemini hooks migrate --from-claude` | 从 Claude Code hooks 迁移 |
| MCP 开关 | `gemini mcp enable/disable <name>` | 单个 server 开关 |
| 多目录工作区 | `--include-directories` | 额外工作目录 |
| 结构化输出 | `-o json/stream-json` | CLI 输出格式 |
| 计划模式 | `--approval-mode plan` | 只读，反映在 ACP availableModes |
| `--experimental-acp` | 已弃用 | 使用 `--acp` 替代 |

## MCP Tools (v2.3.0)

| Tool | 用途 | 备注 |
| ---- | ---- | ---- |
| `gemini` | 发送 prompt 并收集响应 | 主工具，支持 vision/context/MCP 过滤 |
| `list_models` | 列出可用模型和 bridge 状态 | 只读，含当前模型/进程状态 |
| `list_sessions` | 列出活跃 ACP session | 只读，含 workspace/turn count |
| `reset_session` | 重置指定或全部 session | 下次调用创建新 session |

## Approval Modes (v2.3.0)

| 参数值 | ACP mode ID | 行为 |
| ------ | ----------- | ---- |
| `yolo` | `yolo` | 全部自动批准（默认） |
| `auto_edit` | `autoEdit` | 编辑自动批准，其他需审批 |
| `default` | `default` | 每个操作需要审批 |
| `plan` | `plan` | 只读模式 |

替代旧版 `sandbox: bool` 参数。支持 fallback: yolo 不可用时降级到 autoEdit。

## MCP Server 过滤 (v2.3.0)

`gemini` tool 的 `allowed_mcp_servers` 参数可按名称过滤透传的 MCP server。
传 `None` 加载所有发现的 server（默认行为）；传名称列表只加载匹配的 server。

## 已知限制

1. **模型启动时固定**: `--model` 仅在进程启动时生效
2. **会话恢复**: session/load 已实现用于 eviction 恢复；完整持久化待定
3. **单进程**: 一个 `gemini --acp` 实例，切换模型 = 重启进程
4. **无 token 统计**: ACP v1 响应不包含用量数据
5. **音频**: Agent 报告支持但 bridge 未实现
6. **resource_link**: Gemini API 以 400 拒绝（不支持 octet-stream）
7. **`--include-directories`**: CLI 级别标志，ACP session/new 未暴露对应参数

## 升级检查清单

- [ ] `protocolVersion` 升级（当前: 1）
- [x] 新增 `availableModes` — `plan` 模式，通过 ACP 探测确认
- [x] 新增 `availableModels` — 7 个模型; `auto-gemini-*` 为内部路由 ID（不可作为 CLI --model 值）
- [x] `agentCapabilities` 扩展 — audio, embeddedContext, mcpCapabilities
- [x] `session/load` — 用于 eviction 恢复（v2.2.0）
- [x] `resource` ContentBlock — 通过 `context` 参数注入文本（v2.2.0）
- [x] HTTP/SSE MCP 传输透传 — 三种格式全支持（v2.2.0）
- [x] 多 tool 拆分 — list_models, list_sessions, reset_session（v2.3.0）
- [x] 细粒度 approval mode — 4 种模式替代 sandbox bool（v2.3.0）
- [x] MCP server 过滤 — allowed_mcp_servers 参数（v2.3.0）
- [ ] Audio ContentBlock 支持
- [ ] 策略引擎 ACP 集成
- [ ] `--include-directories` 透传（待 ACP 支持）
- [ ] ACP 响应中的 token 用量
