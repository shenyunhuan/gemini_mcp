# gemini_mcp — MCP-to-ACP Bridge

<div align="center">

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![MCP Compatible](https://img.shields.io/badge/MCP-Compatible-green.svg)](https://modelcontextprotocol.io)
[![ACP v1](https://img.shields.io/badge/ACP-v1-orange.svg)](https://agentclientprotocol.com)

Wraps Gemini CLI via ACP protocol as an MCP tool for any MCP client.

[中文](./README.md) | English

</div>

## Architecture

```
MCP Client ──MCP/stdio──→ geminimcp (Python) ──ACP/JSON-RPC──→ gemini --acp (Node.js) ──→ Google API
              (FastMCP)        (AcpBridge)        (long-lived subprocess)
```

MCP Client: Claude Code, Codex, Cursor, VS Code, Claude Desktop, etc.

## Origin

Back in August 2025, I built a FastMCP wrapper around Gemini CLI — it could communicate but the experience was rough. Later I came across [GuDaStudio/geminimcp](https://github.com/GuDaStudio/geminimcp) ([original post](https://linux.do/t/topic/1211767)), which took a similar approach. Thanks for sharing.

After using the original author's MCP for a while, I noticed every call took quite long. Digging into the source, I found it uses `gemini --prompt -o stream-json` under the hood — spawning a new process per request and parsing text output, so cold-start overhead was unavoidable.

Then one day while running `gemini --help`, I spotted an `--acp` flag. Turns out this is Gemini CLI's built-in [Agent Client Protocol](https://agentclientprotocol.com) — a full JSON-RPC protocol with stateful sessions, streaming responses, permission management, and multimodal input.

**In other words, instead of "shelling out" each time, you can spin up a persistent process and talk directly to the Gemini Agent.**

So we redesigned the entire bridge on top of ACP:

- **Persistent connection**: Long-lived `gemini --acp` process, no cold-start overhead
- **Protocol-level communication**: JSON-RPC over stdin, immune to CLI output format changes, no shell escaping
- **Context isolation**: Complex tasks run in a subprocess loop without bloating the main agent's context
- **Tool encapsulation**: Gemini's 30+ built-in tools stay inside ACP, no need to expose them upstream
- **Self-healing**: ACP handles command failures and permission approvals internally
- **Structured output**: Collects thought, tool_calls, and plan in addition to text
- **Multimodal**: Supports image and resource ContentBlocks
- **Standard protocol**: Any MCP client can plug in directly

## ACP vs MCP

| Dimension | MCP (Model Context Protocol) | ACP (Agent Client Protocol) |
|-----------|-------|------|
| Layer | Protocol / connection | Agent / execution |
| Focus | What external tools the agent can use | How the agent autonomously executes tasks |
| Communication | Single tool call | Stateful sessions (multi-turn) |
| Typical use | Read a GitHub issue list | Autonomously fix an auth bug |

geminimcp bridges the two layers: the outside sends instructions via MCP, the inside lets Gemini execute autonomously via ACP.

## Tech Stack

- **Python 3.12+** + [FastMCP](https://github.com/jlowin/fastmcp) (MCP server framework)
- **[uv](https://docs.astral.sh/uv/)** — packaging, dependency management, one-step deploy
- **Pydantic** — parameter validation and type annotations
- **threading + queue** — cross-platform subprocess I/O timeout control

## Installation

Prerequisites:

```bash
# uv (package manager)
# Windows
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
# Linux/macOS
curl -LsSf https://astral.sh/uv/install.sh | sh

# Gemini CLI
npm install -g @google/gemini-cli
```

### Quick Install

**Claude Code:**

```bash
claude mcp add gemini -s user --transport stdio -- uvx --from git+https://github.com/shenyunhuan/gemini_mcp.git geminimcp
```

Auto-downloads and registers, no pre-installation needed.

### Manual Install

```bash
# Install from GitHub
uv tool install --from git+https://github.com/shenyunhuan/gemini_mcp.git geminimcp

# Or clone and install locally
git clone https://github.com/shenyunhuan/gemini_mcp.git
uv tool install --from gemini_mcp geminimcp
```

Register with Claude Code:

```bash
claude mcp add gemini -s user --transport stdio -- geminimcp
```

Optional: merge [.claude/CLAUDE.md](.claude/CLAUDE.md) into `~/.claude/CLAUDE.md` and copy [.claude/rules/mcp-agents.md](.claude/rules/mcp-agents.md) to `~/.claude/rules/` for better Claude integration.

**Codex** (`~/.codex/config.toml`):

```toml
[mcp_servers.gemini]
command = "geminimcp"
```

Or run:

```bash
codex mcp add gemini -- geminimcp
```

**Update:**

```bash
uv tool install --reinstall --force --from git+https://github.com/shenyunhuan/gemini_mcp.git geminimcp
```

### Cross-MCP Chaining

geminimcp supports bidirectional chaining with other MCP agents, enabling 3-layer call chains.

**Gemini → Codex** (`~/.gemini/settings.json` → `mcpServers`):

```json
"codex": {
  "command": "codex",
  "args": ["mcp-server"]
}
```

Or run:

```bash
gemini mcp add --scope user codex codex mcp-server
```

**Codex → Gemini** (`~/.codex/config.toml`):

```toml
[mcp_servers.gemini]
command = "geminimcp"
```

Or run:

```bash
codex mcp add gemini -- geminimcp
```

With both configured:
- `Client → Codex → Gemini`: Codex internally calls Gemini MCP
- `Client → Gemini → Codex`: Gemini internally calls Codex MCP

## MCP Tools

| Tool | Purpose |
|------|---------|
| `gemini` | Send prompt, collect Gemini response (main tool) |
| `list_models` | List available models, approval modes, and bridge status |
| `list_sessions` | List active ACP sessions |
| `reset_session` | Reset specific or all sessions |

### `gemini` Key Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `PROMPT` | (required) | Instruction to send to Gemini |
| `cd` | (required) | Workspace root directory |
| `model` | `gemini-3.1-pro-preview` | Model selection (flash / pro) |
| `approval_mode` | `yolo` | Tool approval mode: `yolo` / `auto_edit` / `default` / `plan` |
| `image_path` | `""` | Image file path (vision analysis) |
| `context` | `""` | Text context injected as ACP resource ContentBlock |
| `allowed_mcp_servers` | `None` | Filter which MCP servers Gemini loads (None=all) |

## Design Highlights

- **Cross-platform I/O**: Background thread + Queue for non-blocking pipe reads with timeout (no native timeout on any platform)
- **Session management**: Per-workspace sessions, 8-turn eviction + session/load recovery
- **Approval modes**: 4 approval modes (yolo/auto_edit/default/plan) with fallback support
- **429 fallback**: Auto-retry with flash when pro hits capacity limits
- **MCP passthrough**: Auto-discovers user/project/extension MCP server configs, injects into ACP sessions (stdio/http/sse)
- **MCP filtering**: `allowed_mcp_servers` parameter filters passthrough MCP servers by name
- **Multimodal**: image ContentBlock (vision) + resource ContentBlock (context injection)
- **Auto-approval**: Intercepts `session/request_permission`, auto-selects first option to prevent subprocess hangs

## Documentation

| File | Content |
|------|---------|
| [CLAUDE.md](CLAUDE.md) | Development & maintenance guide |
| [acp-boundary.md](acp-boundary.md) | ACP protocol boundary (implemented vs not) |
| [gemini-sandbox.md](gemini-sandbox.md) | Sandbox mode guide |

## License

[MIT License](LICENSE)

---

<div align="center">

If you find this useful, please give it a Star :)

[![Star History Chart](https://api.star-history.com/svg?repos=shenyunhuan/gemini_mcp&type=date)](https://star-history.com/#shenyunhuan/gemini_mcp&Date)

</div>
