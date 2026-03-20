"""FastMCP server — Gemini ACP bridge.

v2.0: Replaces raw subprocess + stream-json parsing with ACP (Agent Client
Protocol) as the internal transport layer.  The outer MCP interface stays the
same so Claude Code sees no change.

Architecture:
  Claude Code ──MCP──→ geminimcp ──ACP──→ gemini --acp
                       (thin bridge)       (native protocol)

Threading model:
  A background reader thread reads stdout lines into a queue.  All read
  operations use queue.get(timeout=...) which never blocks indefinitely.
  Cross-platform: avoids pipe readline() blocking (no native timeout on any OS).
"""

from __future__ import annotations

import base64
import json
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Annotated, Any, Dict, List, Optional, Tuple

_MIME_MAP = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

mcp = FastMCP("Gemini MCP Server")

VERSION = "2.3.0"

# Known models (from ACP session/new discovery + CLI docs).
# auto-* are internal routing IDs, not valid --model values.
_KNOWN_MODELS = [
    {
        "id": "auto-gemini-3",
        "description": "Auto-routes between pro and flash (CLI default)",
        "auto": True,
    },
    {
        "id": "auto-gemini-2.5",
        "description": "Auto-routes between 2.5-pro and 2.5-flash",
        "auto": True,
    },
    {"id": "gemini-3.1-pro-preview", "description": "Latest pro model", "auto": False},
    {
        "id": "gemini-3-flash-preview",
        "description": "Latest flash model",
        "auto": False,
    },
    {"id": "gemini-2.5-pro", "description": "Previous gen pro", "auto": False},
    {"id": "gemini-2.5-flash", "description": "Previous gen flash", "auto": False},
    {"id": "gemini-2.5-flash-lite", "description": "Lightweight flash", "auto": False},
]

# Valid approval modes (ACP uses camelCase internally)
_APPROVAL_MODES = {
    "default": "default",  # Prompt for approval
    "auto_edit": "autoEdit",  # Auto-approve edits only
    "yolo": "yolo",  # Auto-approve all
    "plan": "plan",  # Read-only mode
}

# Evict session after N turns to prevent context quality degradation.
_MAX_TURNS_PER_SESSION = 8

# Timeouts (seconds)
_INIT_TIMEOUT = 20
_SESSION_NEW_TIMEOUT = 35  # extra time for extension MCP server startup
_PROMPT_TIMEOUT = 300
_DRAIN_TIMEOUT = 1.5


# ============================================================================
# ACP Bridge — manages a long-lived gemini --acp subprocess
# ============================================================================


class AcpBridge:
    """Single long-lived gemini --acp process with JSON-RPC communication.

    Uses a background reader thread + queue so that all reads respect timeouts
    (pipe readline has no native timeout on any platform).
    """

    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None
        self._msg_queue: queue.Queue[Optional[str]] = queue.Queue()
        self._reader_thread: Optional[threading.Thread] = None
        self._id = 0
        self._lock = threading.Lock()
        self._initialized = False
        self._current_model = ""
        self._agent_info: Dict[str, Any] = {}
        self._agent_caps: Dict[str, Any] = {}
        # workspace path -> {session_id, turn_count}
        self._sessions: Dict[str, Dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Background reader
    # ------------------------------------------------------------------

    def _reader_loop(self) -> None:
        """Background thread: read stdout lines into queue."""
        stdout = self._proc.stdout if self._proc else None
        if stdout is None:
            return
        try:
            for line in iter(stdout.readline, ""):
                stripped = line.strip()
                if stripped:
                    self._msg_queue.put(stripped)
        except (ValueError, OSError):
            pass
        self._msg_queue.put(None)  # EOF sentinel

    def _read_msg(self, timeout: float = 5.0) -> Optional[dict]:
        """Read one JSON-RPC message from the queue with timeout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            remaining = max(0.01, deadline - time.time())
            try:
                line = self._msg_queue.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                continue
            if line is None:  # EOF
                return None
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
        return None

    def _read_until_id(
        self, target_id: int, timeout: float
    ) -> Tuple[Optional[dict], List[dict]]:
        """Read messages until response with target_id, collecting notifications."""
        notifications: List[dict] = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            remaining = max(0.1, deadline - time.time())
            msg = self._read_msg(timeout=min(remaining, 2.0))
            if msg is None:
                if self._proc and self._proc.poll() is not None:
                    return None, notifications
                continue
            if msg.get("id") == target_id:
                return msg, notifications
            notifications.append(msg)
        return None, notifications

    def _drain(self, seconds: float = _DRAIN_TIMEOUT) -> List[dict]:
        """Drain pending notifications for a few seconds."""
        drained: List[dict] = []
        deadline = time.time() + seconds
        while time.time() < deadline:
            remaining = max(0.01, deadline - time.time())
            try:
                line = self._msg_queue.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                continue
            if line is None:
                break
            try:
                drained.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return drained

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @staticmethod
    def _effective_model(model: str) -> str:
        """Normalize model for comparison. auto-* uses CLI default (no --model flag)."""
        return "" if model.startswith("auto-") else model

    def _ensure_ready(self, model: str = "") -> bool:
        """Ensure ACP process is running and initialized with the right model."""
        with self._lock:
            # Model changed → restart with new model (clears all sessions)
            eff = self._effective_model(model)
            cur = self._effective_model(self._current_model)
            if eff and cur and eff != cur:
                self._cleanup_locked()
            if self._proc and self._proc.poll() is None and self._initialized:
                return True
            return self._start_locked(model)

    def _start_locked(self, model: str = "") -> bool:
        """Spawn gemini --acp and run initialize handshake. Caller holds _lock."""
        gemini_path = shutil.which("gemini")
        if not gemini_path:
            return False

        self._cleanup_locked()

        cmd = [gemini_path, "--acp"]
        # auto-* models are internal routing (default behavior), not CLI --model values
        if model and not model.startswith("auto-"):
            cmd.extend(["--model", model])

        # Extensions/MCP servers are loaded per-session via session/new mcpServers

        self._msg_queue = queue.Queue()
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
        )
        self._id = 0
        self._initialized = False
        self._sessions.clear()

        # Start background reader
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

        # ACP initialize handshake
        # clientCapabilities: empty — we don't handle fs/terminal callbacks.
        # Gemini CLI uses its own tools for file access and shell execution.
        rid = self._next_id()
        self._send_locked(
            rid,
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {},
                "clientInfo": {"name": "geminimcp", "version": VERSION},
            },
        )

        resp, _ = self._read_until_id(rid, timeout=_INIT_TIMEOUT)
        if resp and "result" in resp:
            result = resp["result"]
            # Validate protocol version matches what we sent
            server_version = result.get("protocolVersion")
            if server_version is not None and server_version != 1:
                return False  # Incompatible protocol version
            self._initialized = True
            self._current_model = model
            self._agent_info = result.get("agentInfo", {})
            self._agent_caps = result.get("agentCapabilities", {})
            return True
        return False

    def supports_image(self) -> bool:
        """Check if agent supports image content blocks."""
        return self._agent_caps.get("promptCapabilities", {}).get("image", False)

    def supports_load_session(self) -> bool:
        """Check if agent supports session/load for session restoration."""
        return bool(self._agent_caps.get("loadSession"))

    def _cleanup_locked(self) -> None:
        """Terminate old process if running. Caller holds _lock."""
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None
        self._initialized = False
        self._sessions.clear()

    def shutdown(self) -> None:
        with self._lock:
            self._cleanup_locked()

    # ------------------------------------------------------------------
    # Low-level JSON-RPC
    # ------------------------------------------------------------------

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _write(self, text: str) -> None:
        """Write text to process stdin. Caller ensures _proc is alive."""
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write(text)
        self._proc.stdin.flush()

    def _send_locked(self, rid: int, method: str, params: dict) -> None:
        """Send a JSON-RPC request. Caller holds _lock or is in single-thread init."""
        msg = json.dumps(
            {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        )
        self._write(msg + "\n")

    def _send(self, rid: int, method: str, params: dict) -> None:
        """Send a JSON-RPC request (acquires lock)."""
        with self._lock:
            self._send_locked(rid, method, params)

    def _send_notification(self, method: str, params: dict) -> None:
        """Send a JSON-RPC notification (no id)."""
        with self._lock:
            msg = json.dumps({"jsonrpc": "2.0", "method": method, "params": params})
            self._write(msg + "\n")

    def _request(
        self, method: str, params: dict, timeout: float = 30
    ) -> Tuple[Optional[dict], List[dict]]:
        """Send request and wait for response."""
        rid = self._next_id()
        self._send(rid, method, params)
        return self._read_until_id(rid, timeout)

    @staticmethod
    def _extract_error(resp: dict) -> str:
        """Extract error message with code/data from JSON-RPC error response."""
        err = resp.get("error", {})
        msg = err.get("message", str(err))
        code = err.get("code")
        data = err.get("data")
        parts = [msg]
        if code:
            parts.append(f"(code={code})")
        if data:
            parts.append(f"data={json.dumps(data)[:200]}")
        return " ".join(parts)

    # ------------------------------------------------------------------
    # Extension discovery
    # ------------------------------------------------------------------

    def _discover_mcp_servers(
        self, cwd: str = "", allowed_names: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """Discover MCP servers from Gemini CLI config and extensions.

        Sources (in order, later overrides earlier):
        1. User ~/.gemini/settings.json → mcpServers (`gemini mcp add --scope user`)
        2. Project .gemini/settings.json → mcpServers (`gemini mcp add`)
        3. Extensions ~/.gemini/extensions/*/gemini-extension.json → mcpServers

        Returns ACP-compatible list.
        ACP server formats (verified via protocol probe):
          stdio: {name, command, args, env:[]}
          http:  {name, type:"http", url, headers:[]}
          sse:   {name, type:"sse", url, headers:[]}
        """
        servers: Dict[str, Dict[str, Any]] = {}

        # Source 1 & 2: user-level then project-level gemini mcp config
        settings_paths = [Path.home() / ".gemini" / "settings.json"]
        if cwd:
            settings_paths.append(Path(cwd) / ".gemini" / "settings.json")

        for settings_file in settings_paths:
            if not settings_file.exists():
                continue
            try:
                settings = json.loads(settings_file.read_text(encoding="utf-8"))
                for name, cfg in settings.get("mcpServers", {}).items():
                    entry: Dict[str, Any] = {"name": name}
                    # HTTP/SSE servers have "url" instead of "command"
                    if "url" in cfg:
                        transport = cfg.get("transport", cfg.get("type", "sse"))
                        entry["type"] = transport  # ACP uses "type", not "transport"
                        entry["url"] = cfg["url"]
                        # headers is required (array), even if empty
                        raw_headers = cfg.get("headers", [])
                        if isinstance(raw_headers, dict):
                            entry["headers"] = [
                                {"name": k, "value": v} for k, v in raw_headers.items()
                            ]
                        else:
                            entry["headers"] = raw_headers if raw_headers else []
                    else:
                        entry["command"] = cfg.get("command", "node")
                        entry["args"] = cfg.get("args", [])
                        # env is required (array) for stdio
                        raw_env = cfg.get("env", {})
                        if isinstance(raw_env, dict) and raw_env:
                            entry["env"] = [
                                {"name": k, "value": v} for k, v in raw_env.items()
                            ]
                        elif isinstance(raw_env, list):
                            entry["env"] = raw_env
                        else:
                            entry["env"] = []
                    servers[name] = entry
            except (json.JSONDecodeError, KeyError):
                pass

        # Source 3: installed extensions
        ext_dir = Path.home() / ".gemini" / "extensions"
        if ext_dir.is_dir():
            for ext_path in ext_dir.iterdir():
                if not ext_path.is_dir():
                    continue
                config_file = ext_path / "gemini-extension.json"
                if not config_file.exists():
                    continue
                try:
                    config = json.loads(config_file.read_text(encoding="utf-8"))
                    for name, cfg in config.get("mcpServers", {}).items():
                        if name in servers:
                            continue  # project config takes precedence
                        args = [
                            a.replace("${extensionPath}", str(ext_path))
                            for a in cfg.get("args", [])
                        ]
                        raw_env = cfg.get("env", {})
                        if isinstance(raw_env, dict) and raw_env:
                            env_list = [
                                {"name": k, "value": v} for k, v in raw_env.items()
                            ]
                        elif isinstance(raw_env, list):
                            env_list = raw_env
                        else:
                            env_list = []
                        entry: Dict[str, Any] = {
                            "name": name,
                            "command": cfg.get("command", "node"),
                            "args": args,
                            "env": env_list,
                        }
                        servers[name] = entry
                except (json.JSONDecodeError, KeyError):
                    continue

        result = list(servers.values())
        if allowed_names:
            result = [s for s in result if s["name"] in allowed_names]
        return result

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def _get_or_create_session(
        self,
        cwd: str,
        approval_mode: str = "yolo",
        allowed_mcp_servers: Optional[List[str]] = None,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Get existing session or create new one. Returns (session_id, error)."""
        cached = self._sessions.get(cwd)
        if cached and cached["turn_count"] < _MAX_TURNS_PER_SESSION:
            return cached["session_id"], None

        mcp_servers = self._discover_mcp_servers(cwd, allowed_names=allowed_mcp_servers)

        # If evicted session exists and agent supports it, try session/load
        if (
            cached
            and cached["turn_count"] >= _MAX_TURNS_PER_SESSION
            and self.supports_load_session()
        ):
            resp, _ = self._request(
                "session/load",
                {
                    "sessionId": cached["session_id"],
                    "cwd": cwd,
                    "mcpServers": mcp_servers if mcp_servers else [],
                },
                timeout=_SESSION_NEW_TIMEOUT,
            )
            if resp and "result" in resp:
                # session/load reuses the same sessionId, resets turn count
                modes = resp["result"].get("modes", {}).get("availableModes", [])
                self._sessions[cwd]["turn_count"] = 0
                self._drain()
                self._set_mode(cached["session_id"], modes, approval_mode)
                return cached["session_id"], None

        # Create new session with discovered MCP servers (project config + extensions)
        resp, _ = self._request(
            "session/new",
            {
                "cwd": cwd,
                "mcpServers": mcp_servers,
            },
            timeout=_SESSION_NEW_TIMEOUT,
        )

        # Fallback: if mcpServers caused error, retry without them
        if (not resp or "result" not in resp) and mcp_servers:
            resp, _ = self._request(
                "session/new",
                {
                    "cwd": cwd,
                    "mcpServers": [],
                },
                timeout=_SESSION_NEW_TIMEOUT,
            )

        if not resp or "result" not in resp:
            err = self._extract_error(resp) if resp else "session/new timeout"
            return None, err

        sid = resp["result"]["sessionId"]
        modes = resp["result"].get("modes", {}).get("availableModes", [])
        # Track actual model reported by agent (may differ from requested)
        actual_model = resp["result"].get("models", {}).get("currentModelId", "")

        self._sessions[cwd] = {
            "session_id": sid,
            "turn_count": 0,
            "actual_model": actual_model,
        }

        # Drain setup notifications (available_commands_update etc.)
        self._drain()

        # Set approval mode
        self._set_mode(sid, modes, approval_mode)

        return sid, None

    def _set_mode(
        self, session_id: str, modes: List[Dict[str, Any]], approval_mode: str = "yolo"
    ) -> None:
        """Set approval mode on the session.

        approval_mode values: default, auto_edit, yolo, plan.
        Maps to ACP mode IDs (camelCase): default, autoEdit, yolo, plan.
        Falls back through yolo → autoEdit if requested mode unavailable.
        """
        mode_ids = [m.get("id") for m in modes]
        # Map caller value to ACP id
        target = _APPROVAL_MODES.get(approval_mode, approval_mode)

        # Build preference order: requested mode first, then fallbacks
        if target == "yolo":
            candidates = ["yolo", "autoEdit"]
        elif target == "autoEdit":
            candidates = ["autoEdit"]
        else:
            candidates = [target]

        for mode_id in candidates:
            if mode_id in mode_ids:
                self._request(
                    "session/set_mode",
                    {"sessionId": session_id, "modeId": mode_id},
                    timeout=10,
                )
                self._drain(1)
                break

    # ------------------------------------------------------------------
    # Prompt execution
    # ------------------------------------------------------------------

    def prompt(
        self,
        cwd: str,
        text: str,
        model: str = "",
        approval_mode: str = "yolo",
        image_path: str = "",
        context: str = "",
        allowed_mcp_servers: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Send a prompt and collect the full response."""
        if not self._ensure_ready(model=model):
            return {
                "success": False,
                "error": "Failed to start gemini --acp process. Is 'gemini' in PATH?",
            }

        session_id, err = self._get_or_create_session(
            cwd, approval_mode=approval_mode, allowed_mcp_servers=allowed_mcp_servers
        )
        if not session_id:
            return {"success": False, "error": err}

        # Build content blocks
        blocks: List[Dict[str, Any]] = []
        # Resource content block for embedded context (verified: type="resource")
        if context:
            blocks.append(
                {
                    "type": "resource",
                    "resource": {
                        "uri": "context://embedded",
                        "mimeType": "text/plain",
                        "text": context,
                    },
                }
            )
        if image_path:
            img = Path(image_path)
            if img.exists() and self.supports_image():
                mime = _MIME_MAP.get(img.suffix.lower(), "image/png")
                data = base64.b64encode(img.read_bytes()).decode()
                blocks.append({"type": "image", "data": data, "mimeType": mime})
        # Text prompt always last
        blocks.append({"type": "text", "text": text})

        # Send prompt
        start_time = time.monotonic()
        rid = self._next_id()
        self._send(
            rid,
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": blocks,
            },
        )

        # Collect streaming response
        agent_text = ""
        thought_text = ""
        tool_calls: List[Dict[str, Any]] = []
        plan_entries: List[Dict[str, Any]] = []
        stop_reason = ""
        deadline = time.time() + _PROMPT_TIMEOUT

        while time.time() < deadline:
            remaining = max(0.1, deadline - time.time())
            msg = self._read_msg(timeout=min(remaining, 5.0))

            if msg is None:
                if self._proc and self._proc.poll() is not None:
                    return {
                        "success": False,
                        "error": "ACP process died during prompt",
                        "SESSION_ID": session_id,
                        "agent_messages": agent_text,
                        "duration_ms": round((time.monotonic() - start_time) * 1000),
                    }
                continue

            # Final response (has matching id)
            if msg.get("id") == rid:
                if "error" in msg:
                    return {
                        "success": False,
                        "error": self._extract_error(msg),
                        "SESSION_ID": session_id,
                        "duration_ms": round((time.monotonic() - start_time) * 1000),
                    }
                stop_reason = msg.get("result", {}).get("stopReason", "unknown")
                break

            # session/update notification
            if msg.get("method") == "session/update":
                update = msg.get("params", {}).get("update", {})
                su_type = update.get("sessionUpdate", "")

                if su_type == "agent_message_chunk":
                    content = update.get("content", {})
                    agent_text += (
                        content.get("text", "")
                        if isinstance(content, dict)
                        else str(content)
                    )

                elif su_type == "agent_thought_chunk":
                    content = update.get("content", {})
                    thought_text += (
                        content.get("text", "")
                        if isinstance(content, dict)
                        else str(content)
                    )

                elif su_type == "tool_call":
                    tool_calls.append(
                        {
                            "id": update.get("toolCallId", ""),
                            "title": update.get("title", ""),
                            "kind": update.get("kind", ""),
                            "status": update.get("status", ""),
                        }
                    )

                elif su_type == "tool_call_update":
                    tcid = update.get("toolCallId")
                    status = update.get("status", "")
                    for tc in tool_calls:
                        if tc["id"] == tcid:
                            tc["status"] = status
                            break

                elif su_type == "plan":
                    plan_entries = update.get("entries", [])

            # Handle permission requests — auto-approve first option
            elif msg.get("method") == "session/request_permission":
                req_id = msg.get("id")
                if req_id is not None:
                    # ACP spec: respond with {"selected": option_id} or {"cancelled": true}
                    options = msg.get("params", {}).get("options", [])
                    if options:
                        selected = options[0].get("id", options[0].get("value", True))
                    else:
                        selected = True
                    with self._lock:
                        approve = json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": req_id,
                                "result": {"selected": selected},
                            }
                        )
                        self._write(approve + "\n")

        else:
            # Timeout — cancel and return partial
            self._send_notification("session/cancel", {"sessionId": session_id})
            return {
                "success": False,
                "error": f"Prompt timed out after {_PROMPT_TIMEOUT}s",
                "SESSION_ID": session_id,
                "agent_messages": agent_text,
                "duration_ms": round((time.monotonic() - start_time) * 1000),
            }

        # Update turn count
        if cwd in self._sessions:
            self._sessions[cwd]["turn_count"] += 1

        duration_ms = round((time.monotonic() - start_time) * 1000)

        # Report actual model from session (e.g. auto-gemini-3 routes to specific model)
        cached = self._sessions.get(cwd, {})
        actual_model = cached.get("actual_model", "") or self._current_model

        result: Dict[str, Any] = {
            "success": True,
            "SESSION_ID": session_id,
            "agent_messages": agent_text,
            "stop_reason": stop_reason,
            "duration_ms": duration_ms,
            "model_used": actual_model,
        }

        if not agent_text.strip():
            if tool_calls:
                result["agent_messages"] = (
                    "[No text output — Gemini performed tool calls. "
                    "Send another prompt to continue the session.]"
                )
            else:
                result["success"] = False
                result["error"] = "Empty response from agent"

        if thought_text:
            result["thought"] = thought_text
        if tool_calls:
            result["tool_calls"] = tool_calls
        if plan_entries:
            result["plan"] = plan_entries

        return result

    def cancel(self, cwd: str) -> bool:
        """Cancel any ongoing operation for a workspace session."""
        cached = self._sessions.get(cwd)
        if not cached:
            return False
        try:
            self._send_notification(
                "session/cancel", {"sessionId": cached["session_id"]}
            )
            return True
        except Exception:
            return False


# Singleton bridge instance
_bridge = AcpBridge()


# ============================================================================
# MCP tool
# ============================================================================


@mcp.tool(
    name="gemini",
    annotations=ToolAnnotations(
        title="Gemini CLI Agent",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
    description="""
    Invokes Gemini via ACP (Agent Client Protocol) for AI-driven tasks.

    **Return structure:**
        - `success`: boolean indicating execution status
        - `SESSION_ID`: ACP session identifier (auto-managed per workspace)
        - `agent_messages`: concatenated assistant response text
        - `thought`: agent reasoning/thinking (when available)
        - `stop_reason`: why the agent stopped (end_turn, max_tokens, etc.)
        - `tool_calls`: list of tool invocations made by the agent (if any)
        - `plan`: agent execution plan entries (if any)
        - `error`: error description when `success=False`

    **Best practices:**
        - Sessions auto-reuse per workspace with turn-count eviction
        - ALWAYS pass `model`. Use `gemini-3.1-pro-preview` for complex tasks, `gemini-3-flash-preview` for simple tasks
        - Use `approval_mode` to control tool approval: yolo (default), auto_edit, default, plan
        - On 429 capacity errors, automatically retries with `gemini-3-flash-preview`
        - Pass `image_path` for vision analysis (requires agent image support)
        - Pass `context` to inject text as embedded resource (ACP resource ContentBlock)
        - Pass `allowed_mcp_servers` to filter which MCP servers Gemini loads
    """,
)
async def gemini(
    PROMPT: Annotated[
        str,
        Field(description="Instruction for the task to send to Gemini."),
    ],
    cd: Annotated[
        Path,
        Field(
            description="Set the workspace root for Gemini before executing the task."
        ),
    ],
    model: Annotated[
        str,
        Field(
            description="REQUIRED. Pass 'gemini-3.1-pro-preview' for complex tasks, "
            "'gemini-3-flash-preview' for simple tasks."
        ),
    ] = "gemini-3.1-pro-preview",
    approval_mode: Annotated[
        str,
        Field(
            description="Tool approval mode. "
            "'yolo': auto-approve all (default). "
            "'auto_edit': auto-approve edits only. "
            "'default': prompt for every action (safest). "
            "'plan': read-only mode."
        ),
    ] = "yolo",
    image_path: Annotated[
        str,
        Field(
            description="Path to an image file for vision analysis. "
            "Sent as image ContentBlock. Empty string means no image."
        ),
    ] = "",
    context: Annotated[
        str,
        Field(
            description="Text context to inject as ACP resource ContentBlock. "
            "Use for passing file contents, docs, or background info that Gemini should reference."
        ),
    ] = "",
    allowed_mcp_servers: Annotated[
        Optional[List[str]],
        Field(
            description="Filter which MCP servers Gemini loads. "
            "Pass a list of server names to include. None means load all discovered servers."
        ),
    ] = None,
) -> Dict[str, Any]:
    """Execute a Gemini session via ACP and return results."""
    if not shutil.which("gemini"):
        return {"success": False, "error": "CLI tool 'gemini' not found in PATH."}

    if not cd.exists():
        return {
            "success": False,
            "error": f"Workspace directory `{cd.absolute().as_posix()}` does not exist.",
        }

    if approval_mode not in _APPROVAL_MODES:
        return {
            "success": False,
            "error": f"Invalid approval_mode '{approval_mode}'. "
            f"Valid values: {', '.join(_APPROVAL_MODES.keys())}",
        }

    cwd = cd.absolute().as_posix()
    result = _bridge.prompt(
        cwd,
        PROMPT,
        model=model,
        approval_mode=approval_mode,
        image_path=image_path,
        context=context,
        allowed_mcp_servers=allowed_mcp_servers,
    )

    # Session error → retry with fresh session
    if not result["success"] and result.get("SESSION_ID"):
        _bridge._sessions.pop(cwd, None)
        result = _bridge.prompt(
            cwd,
            PROMPT,
            model=model,
            approval_mode=approval_mode,
            image_path=image_path,
            context=context,
            allowed_mcp_servers=allowed_mcp_servers,
        )

    # 429 fallback: capacity error → retry with flash model
    # Skip fallback for auto-* models (they handle routing internally)
    _FALLBACK_MODEL = "gemini-3-flash-preview"
    if (
        not result["success"]
        and model != _FALLBACK_MODEL
        and not model.startswith("auto-")
        and any(
            kw in result.get("error", "").lower()
            for kw in ("capacity", "429", "resource_exhausted", "overloaded")
        )
    ):
        _bridge._sessions.pop(cwd, None)
        result = _bridge.prompt(
            cwd, PROMPT, model=_FALLBACK_MODEL, approval_mode=approval_mode
        )
        result["fallback_model"] = _FALLBACK_MODEL

    return result


@mcp.tool(
    name="list_models",
    annotations=ToolAnnotations(
        title="List Available Models",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
    description="List available Gemini models and current bridge state. "
    "Returns known models, current active model, and agent info.",
)
async def list_models() -> Dict[str, Any]:
    """List available models and bridge status."""
    return {
        "models": _KNOWN_MODELS,
        "approval_modes": list(_APPROVAL_MODES.keys()),
        "current_model": _bridge._current_model or "(not started)",
        "agent_info": _bridge._agent_info or None,
        "bridge_version": VERSION,
        "process_running": _bridge._proc is not None and _bridge._proc.poll() is None,
    }


@mcp.tool(
    name="list_sessions",
    annotations=ToolAnnotations(
        title="List Active Sessions",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
    description="List all active ACP sessions managed by the bridge. "
    "Shows workspace path, session ID, turn count, and model for each session.",
)
async def list_sessions() -> Dict[str, Any]:
    """List active sessions."""
    sessions = []
    for workspace, info in _bridge._sessions.items():
        sessions.append(
            {
                "workspace": workspace,
                "session_id": info["session_id"],
                "turn_count": info["turn_count"],
                "max_turns": _MAX_TURNS_PER_SESSION,
                "model": info.get("actual_model", ""),
            }
        )
    return {
        "sessions": sessions,
        "count": len(sessions),
    }


@mcp.tool(
    name="reset_session",
    annotations=ToolAnnotations(
        title="Reset Session",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=False,
    ),
    description="Reset (clear) the ACP session for a workspace. "
    "The next gemini call for this workspace will create a fresh session. "
    "Pass workspace path, or omit to reset all sessions.",
)
async def reset_session(
    workspace: Annotated[
        str,
        Field(description="Workspace path to reset. Empty string resets all sessions."),
    ] = "",
) -> Dict[str, Any]:
    """Reset session for a workspace or all sessions."""
    if workspace:
        removed = _bridge._sessions.pop(workspace, None)
        if not removed:
            # Try matching by suffix (user might pass partial path)
            matched = [k for k in _bridge._sessions if k.endswith(workspace)]
            if matched:
                for k in matched:
                    _bridge._sessions.pop(k)
                return {"reset": matched, "count": len(matched)}
            return {
                "reset": [],
                "count": 0,
                "message": "No session found for workspace",
            }
        return {"reset": [workspace], "count": 1}
    else:
        count = len(_bridge._sessions)
        _bridge._sessions.clear()
        return {"reset": "all", "count": count}


def run() -> None:
    """Start the MCP server over stdio transport."""
    import atexit

    atexit.register(_bridge.shutdown)
    mcp.run(transport="stdio")
