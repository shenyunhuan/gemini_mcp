---
paths:
  - "**/server.py"
  - "**/mcp-*"
description: Gemini MCP (ACP bridge) routing and delegation rules
---

# Gemini MCP Rules

## Tool Signature

```
mcp__gemini__gemini(PROMPT, cd, model?, sandbox?, image_path?, context?)
```

| Param | Key Values |
| ----- | ---------- |
| model | `gemini-3-flash-preview` (fast/cheap) · `gemini-3.1-pro-preview` (powerful) |
| sandbox | `false` → yolo · `true` → approval mode |
| image_path | Optional: image file path for vision analysis |
| context | Optional: text injected as ACP resource ContentBlock |

## Capabilities

- **Sub-agents**: Gemini auto-routes based on task description (don't name them):
  - *codebase_investigator*: triggers on vague requests, bug root-cause,
    architectural mapping, system-wide dependency analysis. Returns structured
    report with key file paths and symbols
  - *generalist*: triggers on turn-intensive or high-volume tasks — batch
    refactoring across files, commands with large output, speculative
    investigations. Keeps main session lean
- **chrome-devtools**: 30 browser tools directly available in session
- **Vision**: native image recognition via `image_path`
- **MCP passthrough**: auto-discovers user/project MCP servers (stdio/http/sse)
- **Session management**: per-workspace, auto-reuse, 8-turn eviction
- **429 fallback**: pro capacity error → auto-retry with flash
- **Plan mode**: read-only mode available via ACP `session/set_mode` (id: `plan`)

## When to Use

| Scenario | Pattern |
|----------|---------|
| File read / Q&A / second opinion | Single call, flash |
| Bug root-cause / dependency trace | Single call, pro (triggers codebase_investigator) |
| Batch refactor / multi-file fix | Single call, pro (triggers generalist) |
| Write / fix (clear scope) | Single call, pro |
| Multi-turn (depends on prior context) | 2-4 calls, same model |
| Image / vision analysis | Single call + `image_path`, flash |
| Browser automation | Single call, flash or pro |

**Never switch models mid-chain** — restarts ACP process, destroys session.

**Prompt style**: Describe the task naturally. Do NOT inject meta-instructions
like "use your codebase_investigator" — Gemini auto-routes to sub-agents
based on task content, not explicit commands.

## Error Recovery

| Error | Action |
| ----- | ------ |
| Timeout / rate-limit | Retry 1x with 2s backoff |
| Idle timeout (300s) | Process hung — escalate to user |
| Session error | Auto-retry with fresh session |
