<!-- Copy this file to ~/.claude/CLAUDE.md (or merge into your existing one) -->

## Gemini MCP Routing

- **Gemini** (ACP bridge via `mcp__gemini__gemini`):
  - Single call preferred; multi-turn only when next turn needs Gemini's internal state
  - flash for gather/review; pro for analysis/write. Never switch model mid-chain
  - Sub-agents auto-route: root-cause/architecture → codebase_investigator, batch/multi-file → generalist
  - chrome-devtools tools built-in (browser debugging/automation)
  - `sandbox: false` (default): yolo; `sandbox: true`: approval mode
  - `context`: pass text as resource ContentBlock (file contents, background info)
- **Vision**: native image analysis via `image_path`
- Prompt style: describe the task naturally — do NOT inject "use codebase_investigator"
  or other meta-instructions. Gemini auto-routes internally
