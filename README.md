# gemini_mcp — MCP-to-ACP Bridge

<div align="center">

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![MCP Compatible](https://img.shields.io/badge/MCP-Compatible-green.svg)](https://modelcontextprotocol.io)
[![ACP v1](https://img.shields.io/badge/ACP-v1-orange.svg)](https://agentclientprotocol.com)

将 Gemini CLI 通过 ACP 协议封装为 MCP 工具，供任何 MCP 客户端调用。

简体中文 | [English](./README_EN.md)

</div>

## 架构

```
MCP Client ──MCP/stdio──→ geminimcp (Python) ──ACP/JSON-RPC──→ gemini --acp (Node.js) ──→ Google API
              (FastMCP)        (AcpBridge)          (长驻子进程)
```

MCP Client: Claude Code, Codex, Cursor, VS Code, Claude Desktop 等。

## 起源

2025 年 8 月我就用 FastMCP 封装过 Gemini CLI 的 MCP，能通信但体验不佳。后来在论坛上看到 [GuDaStudio/geminimcp](https://github.com/GuDaStudio/geminimcp)（[原帖](https://linux.do/t/topic/1211767)），思路类似，感谢分享。

用了一段时间孙佬的 MCP 后，发现每次调用都要等挺久，翻了下源码才知道它底层是 `gemini --prompt -o stream-json`——每次请求都 spawn 一个新进程、解析文本输出，冷启动开销避不开。

然后某天跑 `gemini --help` 的时候，注意到有个 `--acp` 标志。查了一下发现这是 Gemini CLI 内置的 [Agent Client Protocol](https://agentclientprotocol.com)——一套完整的 JSON-RPC 协议，支持有状态会话、流式响应、权限管理和多模态输入。

**也就是说，不用每次"调命令行"了，可以起一个常驻进程，直接和 Gemini Agent 对话。**

于是我们基于 ACP 重新设计了整个 bridge：

- **长连接复用**: 常驻 `gemini --acp` 进程，消除冷启动开销
- **协议级通信**: JSON-RPC over stdin，不受 CLI 输出格式变更影响，无需 shell 转义
- **上下文隔离**: 复杂任务在子进程内闭环，不膨胀主 Agent 上下文
- **工具库内聚**: Gemini 自带的 30+ 工具在 ACP 内部调用，无需暴露给上层
- **自主容错**: ACP 内部处理命令失败、权限审批等异常
- **结构化返回**: 除文本外还收集 thought、tool_calls、plan
- **多模态**: 支持 image 和 resource ContentBlock
- **标准协议**: 任何 MCP 客户端都可直接对接

## ACP vs MCP

| 维度 | MCP (Model Context Protocol) | ACP (Agent Client Protocol) |
|------|-------|------|
| 层级 | 协议/连接层 | 代理/执行层 |
| 侧重 | Agent 能用什么外部工具 | Agent 如何自主执行任务 |
| 通信 | 单次工具调用 | 有状态会话（多轮交互） |
| 典型场景 | 读 GitHub issue 列表 | 自主修复一个 auth bug |

geminimcp 的作用就是在两层之间架桥：外部通过 MCP 发指令，内部通过 ACP 让 Gemini 自主执行。

## 技术栈

- **Python 3.12+** + [FastMCP](https://github.com/jlowin/fastmcp) (MCP server 框架)
- **[uv](https://docs.astral.sh/uv/)** — 打包、依赖管理、`uv tool install` 一键部署
- **Pydantic** — 参数验证和类型注解
- **threading + queue** — 子进程 I/O 跨平台超时控制

## 安装

前置依赖:

```bash
# uv (包管理)
# Windows
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
# Linux/macOS
curl -LsSf https://astral.sh/uv/install.sh | sh

# Gemini CLI
npm install -g @google/gemini-cli
```

### 快速安装

**Claude Code:**

```bash
claude mcp add gemini -s user --transport stdio -- uvx --from git+https://github.com/shenyunhuan/gemini_mcp.git geminimcp
```

此命令自动下载并注册，无需预装。

### 手动安装

```bash
# 从 GitHub 安装
uv tool install --from git+https://github.com/shenyunhuan/gemini_mcp.git geminimcp

# 或 clone 后本地安装
git clone https://github.com/shenyunhuan/gemini_mcp.git
uv tool install --from gemini_mcp geminimcp
```

注册到 Claude Code:

```bash
claude mcp add gemini -s user --transport stdio -- geminimcp
```

可选：将 [.claude/CLAUDE.md](.claude/CLAUDE.md) 合并到 `~/.claude/CLAUDE.md`，将 [.claude/rules/mcp-agents.md](.claude/rules/mcp-agents.md) 复制到 `~/.claude/rules/`，帮助 Claude 更好地使用 Gemini MCP。

**Codex** (`~/.codex/config.toml`):

```toml
[mcp_servers.gemini]
command = "geminimcp"
```

或执行:

```bash
codex mcp add gemini -- geminimcp
```

**更新:**

```bash
uv tool install --reinstall --force --from git+https://github.com/shenyunhuan/gemini_mcp.git geminimcp
```

### Cross-MCP Chaining

geminimcp 支持与其他 MCP agent 互调，实现 3 层链式调用。

**Gemini → Codex** (`~/.gemini/settings.json` → `mcpServers`):

```json
"codex": {
  "command": "codex",
  "args": ["mcp-server"]
}
```

或执行:

```bash
gemini mcp add --scope user codex codex mcp-server
```

**Codex → Gemini** (`~/.codex/config.toml`):

```toml
[mcp_servers.gemini]
command = "geminimcp"
```

或执行:

```bash
codex mcp add gemini -- geminimcp
```

配置双向后:
- `Client → Codex → Gemini`: Codex 内部调用 Gemini MCP
- `Client → Gemini → Codex`: Gemini 内部调用 Codex MCP

## 设计要点

- **跨平台 I/O**: 后台线程 + Queue 实现带超时的非阻塞管道读取（pipe readline 在所有平台都无原生 timeout）
- **会话管理**: per-workspace session, 8-turn eviction + session/load 恢复
- **429 降级**: pro 容量不足时自动重试 flash
- **MCP 透传**: 自动发现 user/project/extension 的 MCP server 配置，注入 ACP session（支持 stdio/http/sse）
- **多模态**: image ContentBlock (vision) + resource ContentBlock (context 注入)
- **权限自动审批**: 拦截 `session/request_permission`，自动选择首选项，防止子进程挂起

## 文档

| 文件 | 内容 |
|------|------|
| [CLAUDE.md](CLAUDE.md) | 开发维护指南 |
| [acp-boundary.md](acp-boundary.md) | ACP 协议边界（实现 vs 未实现） |
| [gemini-sandbox.md](gemini-sandbox.md) | 沙箱模式说明 |

## 许可证

[MIT License](LICENSE)

---

<div align="center">

如果觉得有用，请给个 Star 支持一下 :)

[![Star History Chart](https://api.star-history.com/svg?repos=shenyunhuan/gemini_mcp&type=date)](https://star-history.com/#shenyunhuan/gemini_mcp&Date)

</div>
