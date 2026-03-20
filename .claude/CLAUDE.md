<!-- Copy this file to ~/.claude/CLAUDE.md (or merge into your existing one) -->

## Gemini MCP Routing (v2.3.0)

### Tools

| Tool | Purpose |
| ---- | ------- |
| `mcp__gemini__gemini` | Send prompt, collect response (main tool) |
| `mcp__gemini__list_models` | List available models and bridge status |
| `mcp__gemini__list_sessions` | List active ACP sessions |
| `mcp__gemini__reset_session` | Reset session for a workspace (or all) |

### gemini tool

- Single call preferred; multi-turn only when next turn needs Gemini's internal state
- flash for gather/review; pro for analysis/write. Never switch model mid-chain
- Sub-agents auto-route: root-cause/architecture → codebase_investigator, batch/multi-file → generalist
- chrome-devtools tools built-in (browser debugging/automation)
- `approval_mode`: yolo (default), auto_edit, default (safest), plan (read-only)
- `context`: pass text as resource ContentBlock (file contents, background info)
- `allowed_mcp_servers`: filter which MCP servers Gemini loads (None = all)
- **Vision**: native image analysis via `image_path`
- Prompt style: describe the task naturally — do NOT inject "use codebase_investigator"
  or other meta-instructions. Gemini auto-routes internally
