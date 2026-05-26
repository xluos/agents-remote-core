"""
Claude Code Hook 注入系统

在 start/serve 模式下注入 Claude Code hooks，通过 FIFO 获取权威状态事件。
Mirror 模式和 codex cli_type 不支持（无法注入 --settings）。

两类 hook 脚本：
  relay（单向）：写 FIFO 后立即 exit 0，用于 SessionStart/Stop 等
  permission（双向）：写 FIFO 后等待响应文件，用于 PermissionRequest 和 AskUserQuestion

事件流：
  SessionStart → session_id, transcript_path
  PreToolUse(普通工具) → active_tool, tool_input, tool_status="executing"
  PreToolUse(AskUserQuestion) → pending_question（等待消费端 respond_question）
  PostToolUse → 清除 active_tool
  PostToolUseFailure → tool_status="failed"
  Stop → turn_complete=True
  StopFailure → turn_error=True, turn_error_type
  Notification(permission_prompt) → waiting_permission=True
  PermissionRequest → pending_permission（等待消费端 respond_permission）
"""

import asyncio
import json
import logging
import os
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from ..utils.session import get_socket_dir, _safe_filename

logger = logging.getLogger('Hooks')


@dataclass
class HookState:
    """从 Claude Code hooks 获取的权威状态"""
    session_id: str = ""
    transcript_path: str = ""

    turn_complete: bool = False
    turn_error: bool = False
    turn_error_type: str = ""
    last_assistant_message: str = ""

    active_tool: str = ""
    active_tool_input: dict = field(default_factory=dict)
    tool_status: str = ""

    waiting_permission: bool = False
    pending_permission: Optional[dict] = None
    pending_question: Optional[dict] = None

    last_event_ts: float = 0.0
    last_event_name: str = ""
    event_count: int = 0


# 单向 relay 事件（写 FIFO 后立即退出）
RELAY_EVENTS = {
    "SessionStart":       "*",
    "Stop":               "*",
    "StopFailure":        "*",
    "PostToolUse":        "*",
    "PostToolUseFailure": "*",
    "Notification":       "permission_prompt",
}

# 双向事件（写 FIFO 后等待响应文件）
BIDIRECTIONAL_EVENTS = {
    "PermissionRequest":  "*",
}

# AskUserQuestion 需要双向等待用户选择答案
ASKUSER_TOOL = "AskUserQuestion"

PERMISSION_WAIT_SECONDS = 300


class HookHarness:
    """管理 hook 临时文件、FIFO 和 --settings 注入"""

    def __init__(self, session_name: str):
        self._session_name = session_name
        safe = _safe_filename(session_name)
        self._hook_dir = get_socket_dir() / f"{safe}_hooks"
        self._hook_dir.mkdir(parents=True, exist_ok=True)

        self._fifo_path = self._hook_dir / "events.fifo"
        self._relay_script = self._hook_dir / "relay.sh"
        self._permission_script = self._hook_dir / "permission.sh"

        self.state = HookState()
        self._on_event: Optional[Callable] = None
        self._fifo_fd: Optional[int] = None
        self._running = False

        self._setup()

    def _setup(self):
        if self._fifo_path.exists():
            self._fifo_path.unlink()
        os.mkfifo(str(self._fifo_path), 0o600)
        self._fifo_fd = os.open(str(self._fifo_path), os.O_RDWR | os.O_NONBLOCK)

        # relay.sh — 单向：写 FIFO 后立即退出
        self._relay_script.write_text(f"""#!/bin/sh
set -eu
event="$1"
fifo="{self._fifo_path}"
payload="$(cat)"
if [ -p "$fifo" ]; then
    printf '%s\\t%s\\n' "$event" "$payload" >> "$fifo" 2>/dev/null || true
fi
exit 0
""")
        self._relay_script.chmod(self._relay_script.stat().st_mode | stat.S_IEXEC)

        # permission.sh — 双向：写 FIFO 后等待响应文件
        perm_script = (
            '#!/bin/sh\n'
            'set -eu\n'
            'event="$1"\n'
            'hook_dir="__HOOK_DIR__"\n'
            'fifo="$hook_dir/events.fifo"\n'
            'payload="$(cat)"\n'
            '\n'
            'req_id="$$_$(date +%s)"\n'
            '\n'
            'case "$payload" in\n'
            '    \\{*) payload_with_id="{\\\"_req_id\\\":\\\"${req_id}\\\",${payload#\\{}" ;;\n'
            '    *) payload_with_id="$payload" ;;\n'
            'esac\n'
            '\n'
            'if [ -p "$fifo" ]; then\n'
            '    printf \'%s\\t%s\\n\' "$event" "$payload_with_id" >> "$fifo" 2>/dev/null || true\n'
            'fi\n'
            '\n'
            'resp_file="$hook_dir/resp_${req_id}"\n'
            'elapsed=0\n'
            'while [ ! -f "$resp_file" ] && [ "$elapsed" -lt __TIMEOUT__ ]; do\n'
            '    sleep 0.1\n'
            '    elapsed=$((elapsed + 1))\n'
            'done\n'
            '\n'
            'if [ -f "$resp_file" ]; then\n'
            '    cat "$resp_file"\n'
            '    rm -f "$resp_file"\n'
            '    exit 0\n'
            'fi\n'
            '\n'
            'exit 1\n'
        )
        perm_script = perm_script.replace('__HOOK_DIR__', str(self._hook_dir))
        perm_script = perm_script.replace('__TIMEOUT__', str(PERMISSION_WAIT_SECONDS * 10))
        self._permission_script.write_text(perm_script)
        self._permission_script.chmod(self._permission_script.stat().st_mode | stat.S_IEXEC)
        logger.info(f"Hook harness 已创建: {self._hook_dir}")

    def get_settings_json(self) -> str:
        hooks = {}
        relay = str(self._relay_script)
        perm = str(self._permission_script)

        for event, matcher in RELAY_EVENTS.items():
            hooks[event] = [{
                "matcher": matcher,
                "hooks": [{"type": "command", "command": f"{relay} {event}"}],
            }]

        for event, matcher in BIDIRECTIONAL_EVENTS.items():
            hooks[event] = [{
                "matcher": matcher,
                "hooks": [{"type": "command", "command": f"{perm} {event}"}],
            }]

        # PreToolUse 拆成两条 matcher：
        #   AskUserQuestion → 双向（等待消费端选择答案）
        #   其他工具       → 单向（纯追踪）
        hooks["PreToolUse"] = [
            {
                "matcher": f"^{ASKUSER_TOOL}$",
                "hooks": [{"type": "command", "command": f"{perm} PreToolUse"}],
            },
            {
                "matcher": f"^(?!{ASKUSER_TOOL}$)",
                "hooks": [{"type": "command", "command": f"{relay} PreToolUse"}],
            },
        ]

        return json.dumps({"hooks": hooks})

    def get_cli_args(self) -> list:
        return ["--settings", self.get_settings_json()]

    # ── 响应方法 ─────────────────────────────────────────────────

    def respond_question(self, request_id: str, answers: dict) -> bool:
        """写入 AskUserQuestion 响应，带预填 answers 的 updatedInput 跳过交互 UI"""
        resp_file = self._hook_dir / f"resp_{request_id}"
        questions = []
        if self.state.pending_question:
            questions = self.state.pending_question.get("questions", [])
        body = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "updatedInput": {
                    "questions": questions,
                    "answers": answers,
                },
            }
        }
        try:
            resp_file.write_text(json.dumps(body))
            self.state.pending_question = None
            logger.info(f"Question response: {list(answers.values())} → {resp_file.name}")
            return True
        except Exception as e:
            logger.error(f"写入 question 响应失败: {e}")
            return False

    def respond_permission(self, request_id: str, decision: str) -> bool:
        """写入响应文件，解除 permission.sh 的等待"""
        resp_file = self._hook_dir / f"resp_{request_id}"
        if decision == "allow":
            body = {
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": {"behavior": "allow"},
                }
            }
        else:
            body = {
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": {"behavior": "deny"},
                }
            }
        try:
            resp_file.write_text(json.dumps(body))
            self.state.pending_permission = None
            self.state.waiting_permission = False
            logger.info(f"Permission response: {decision} → {resp_file.name}")
            return True
        except Exception as e:
            logger.error(f"写入 permission 响应失败: {e}")
            return False

    # ── FIFO reader ─────────────────────────────────────────────

    async def start_reader(self, on_event=None):
        self._on_event = on_event
        self._running = True
        loop = asyncio.get_event_loop()
        buf = bytearray()

        logger.info("Hook FIFO reader 已启动")
        while self._running:
            try:
                data = await loop.run_in_executor(None, self._read_once)
                if data:
                    buf.extend(data)
                    while b'\n' in buf:
                        line_bytes, buf = buf.split(b'\n', 1)
                        self._handle_line(line_bytes.decode('utf-8', errors='replace'))
                elif data is None:
                    await asyncio.sleep(0.05)
                else:
                    await asyncio.sleep(0.1)
            except Exception as e:
                if self._running:
                    logger.error(f"FIFO 读取错误: {e}")
                await asyncio.sleep(0.1)

        logger.info("Hook FIFO reader 退出")

    def _read_once(self) -> Optional[bytes]:
        try:
            return os.read(self._fifo_fd, 4096)
        except BlockingIOError:
            return None
        except OSError:
            return b""

    # ── 事件分发 ────────────────────────────────────────────────

    def _handle_line(self, line: str):
        line = line.strip()
        if '\t' not in line:
            return

        event_name, payload_str = line.split('\t', 1)
        try:
            payload = json.loads(payload_str)
        except json.JSONDecodeError:
            logger.warning(f"Hook payload 解析失败: event={event_name}")
            return

        now = time.time()
        self.state.last_event_ts = now
        self.state.last_event_name = event_name
        self.state.event_count += 1

        # PreToolUse 根据 _req_id 区分：有 _req_id 是 AskUserQuestion（双向），没有是普通工具（单向）
        if event_name == "PreToolUse" and "_req_id" in payload:
            handler = self._on_pre_tool_use_question
        else:
            handler = {
                "SessionStart":       self._on_session_start,
                "Stop":               self._on_stop,
                "StopFailure":        self._on_stop_failure,
                "PreToolUse":         self._on_pre_tool_use,
                "PostToolUse":        self._on_post_tool_use,
                "PostToolUseFailure": self._on_post_tool_use_failure,
                "Notification":       self._on_notification,
                "PermissionRequest":  self._on_permission_request,
            }.get(event_name)

        if handler:
            handler(payload)
            log_parts = [f"Hook: {event_name}"]
            if self.state.active_tool:
                log_parts.append(f"tool={self.state.active_tool}")
            if event_name == "Stop":
                log_parts.append(f"turn_complete={self.state.turn_complete}")
            if self.state.turn_error:
                log_parts.append(f"error={self.state.turn_error_type}")
            if self.state.pending_permission:
                log_parts.append(f"perm_req={self.state.pending_permission.get('request_id', '')}")
            if self.state.pending_question:
                log_parts.append(f"q_req={self.state.pending_question.get('request_id', '')}")
            logger.info(" ".join(log_parts))
        else:
            logger.debug(f"Hook: 未知事件 {event_name}")

        if self._on_event:
            self._on_event(event_name, payload, self.state)

    # ── 事件处理器 ──────────────────────────────────────────────

    def _on_session_start(self, payload: dict):
        self.state.session_id = payload.get("session_id", "")
        self.state.transcript_path = payload.get("transcript_path", "")
        self.state.turn_complete = False
        self.state.turn_error = False

    def _on_stop(self, payload: dict):
        self.state.turn_complete = True
        self.state.last_assistant_message = payload.get("last_assistant_message", "")
        self.state.transcript_path = payload.get("transcript_path", self.state.transcript_path)
        is_error = payload.get("is_error", False)
        subtype = payload.get("subtype", "success")
        self.state.turn_error = bool(is_error) or subtype == "error"
        self.state.active_tool = ""
        self.state.active_tool_input = {}
        self.state.tool_status = ""
        self.state.waiting_permission = False
        self.state.pending_permission = None
        self.state.pending_question = None

    def _on_stop_failure(self, payload: dict):
        self.state.turn_complete = True
        self.state.turn_error = True
        self.state.turn_error_type = payload.get("error_type", "") or payload.get("subtype", "unknown")
        self.state.active_tool = ""
        self.state.active_tool_input = {}
        self.state.tool_status = ""

    def _on_pre_tool_use(self, payload: dict):
        self.state.turn_complete = False
        self.state.turn_error = False
        self.state.turn_error_type = ""
        self.state.active_tool = payload.get("tool_name", "")
        self.state.active_tool_input = payload.get("tool_input", {})
        self.state.tool_status = "executing"
        self.state.waiting_permission = False

    def _on_pre_tool_use_question(self, payload: dict):
        req_id = payload.pop("_req_id", "")
        tool_input = payload.get("tool_input", {})
        self.state.active_tool = ASKUSER_TOOL
        self.state.active_tool_input = tool_input
        self.state.tool_status = "waiting_input"
        self.state.turn_complete = False
        self.state.pending_question = {
            "request_id": req_id,
            "questions": tool_input.get("questions", []),
            "timestamp": time.time(),
        }

    def _on_post_tool_use(self, payload: dict):
        self.state.active_tool = ""
        self.state.active_tool_input = {}
        self.state.tool_status = ""

    def _on_post_tool_use_failure(self, payload: dict):
        self.state.tool_status = "failed"

    def _on_notification(self, payload: dict):
        self.state.waiting_permission = True

    def _on_permission_request(self, payload: dict):
        req_id = payload.pop("_req_id", "")
        self.state.waiting_permission = True
        self.state.pending_permission = {
            "request_id": req_id,
            "tool_name": payload.get("tool_name", ""),
            "tool_input": payload.get("tool_input", {}),
            "timestamp": time.time(),
        }

    # ── 生命周期 ────────────────────────────────────────────────

    def stop(self):
        self._running = False

    def cleanup(self):
        self.stop()
        if self._fifo_fd is not None:
            try:
                os.close(self._fifo_fd)
            except OSError:
                pass
            self._fifo_fd = None
        try:
            import shutil
            shutil.rmtree(self._hook_dir, ignore_errors=True)
        except Exception:
            pass
        logger.info("Hook harness 已清理")
