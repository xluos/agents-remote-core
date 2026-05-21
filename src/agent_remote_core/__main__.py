"""agent-remote-core CLI

A short-lived PTY-host runtime. Spawned per-session by upper-layer apps
(agent-remote, agentara, third-party TS apps via @agent-remote/sdk).
Each invocation hosts one CLI inside a PTY, parses ANSI into a
ClaudeWindow snapshot, exposes input over a Unix socket.

Subcommands:
  start   起一个新会话（fork PTY 包 claude/codex），后台 daemon 模式
  serve   serve --foreground：在当前 pane 跑 PTY 并 stdout 透传（前台模式）
  mirror  镜像一个已存在的 tmux session（不 fork PTY，旁观字节流）
  list    列出活跃会话
  kill    停止一个会话
  paths   打印一个会话的 socket / mq 路径（供其他语言 SDK 使用）
"""

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from .utils.session import (
    USER_DATA_DIR,
    get_socket_path, get_pid_file, get_env_snapshot_path, get_socket_dir,
    list_active_sessions, cleanup_session,
    ensure_socket_dir, ensure_user_data_dir, set_data_dir,
    _safe_filename,
)


def _setup_logging(foreground: bool = False):
    """配置 logging。前台模式下 logger 不能写 stdout（会污染 PTY 透传）。"""
    if foreground:
        # 写到 stderr，stderr 在 cla -f 启动时会被 cla 包装重定向到日志文件
        handler = logging.StreamHandler(sys.stderr)
    else:
        handler = logging.StreamHandler(sys.stdout)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d [%(name)s] %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[handler],
    )


def _run_server_blocking(session_name: str, claude_args, cli_type: str,
                         debug_screen: bool, debug_verbose: bool,
                         tmux_mirror_target=None, foreground: bool = False):
    """在当前进程内直接 run server（前台阻塞）"""
    from .server.server import ProxyServer

    claude_cmd = os.environ.get("CLAUDE_COMMAND", "claude")
    codex_cmd = os.environ.get("CODEX_COMMAND", "codex")

    server = ProxyServer(
        session_name=session_name,
        claude_args=claude_args or [],
        claude_cmd=claude_cmd,
        codex_cmd=codex_cmd,
        cli_type=cli_type,
        debug_screen=debug_screen,
        debug_verbose=debug_verbose,
        tmux_mirror_target=tmux_mirror_target,
        foreground=foreground,
    )

    asyncio.run(server.start())


def _spawn_server_background(args_list, session_name: str) -> int:
    """把 server 后台化（detached subprocess）。返回 PID。"""
    ensure_user_data_dir()
    log_path = USER_DATA_DIR / f"daemon-{_safe_filename(session_name)}.log"

    # 保存调用方完整环境（PTY 子进程 exec 后会用这个还原 shell env）
    ensure_socket_dir()
    env_path = get_env_snapshot_path(session_name)
    env_path.write_text(json.dumps(dict(os.environ)), encoding="utf-8")
    os.chmod(env_path, 0o600)

    # 后台启动
    with open(log_path, "ab", buffering=0) as log_fd:
        proc = subprocess.Popen(
            args_list,
            stdout=log_fd, stderr=log_fd,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    return proc.pid


def cmd_serve(args):
    """前台模式：当前进程作为 PTY 宿主，stdout/stdin 透传

    给 cla -f / claude-squad program 替换 / 任何在 tmux pane 里直接调用的
    场景用。本进程结束 = 会话结束（跟 pane 同生共死）。
    """
    _setup_logging(foreground=True)
    ensure_socket_dir()

    # 名字策略：参数 > $TMUX 自动探测 > 错误
    name = args.name
    if not name:
        tmux_session = _detect_tmux_session()
        if tmux_session:
            name = tmux_session
        else:
            print("错误: serve --foreground 必须指定 --name 或在 tmux 内运行（自动用 tmux session name）",
                  file=sys.stderr)
            return 1

    # 保存环境快照
    env_path = get_env_snapshot_path(name)
    env_path.write_text(json.dumps(dict(os.environ)), encoding="utf-8")
    os.chmod(env_path, 0o600)

    _run_server_blocking(
        session_name=name,
        claude_args=args.cli_args or [],
        cli_type=args.cli_type,
        debug_screen=args.debug_screen,
        debug_verbose=False,
        foreground=True,
    )
    return 0


def _detect_tmux_session() -> Optional[str]:
    """探测当前是否在 tmux 内，若在则返回 session name"""
    if not os.environ.get("TMUX"):
        return None
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "#{session_name}"],
            capture_output=True, text=True, check=False,
        )
        name = result.stdout.strip()
        return name or None
    except Exception:
        return None


def cmd_start(args):
    """前台启动会话（PTY 模式）。后台启动建议用 tmux 包一层，详见 README。"""
    _setup_logging()
    ensure_socket_dir()

    # 检查会话已存在
    if get_socket_path(args.name).exists():
        print(f"错误: 会话 '{args.name}' 已存在")
        return 1

    # 保存环境快照
    env_path = get_env_snapshot_path(args.name)
    env_path.write_text(json.dumps(dict(os.environ)), encoding="utf-8")
    os.chmod(env_path, 0o600)

    _run_server_blocking(
        session_name=args.name,
        claude_args=args.cli_args or [],
        cli_type=args.cli_type,
        debug_screen=args.debug_screen,
        debug_verbose=args.debug_verbose,
    )
    return 0


def cmd_mirror(args):
    """镜像一个已有 tmux session（前台运行；后台请用 tmux/nohup 包）"""
    _setup_logging()
    ensure_socket_dir()

    # 默认 session name = tmux session name
    session_name = args.name or args.tmux_target

    if get_socket_path(session_name).exists():
        print(f"错误: 会话 '{session_name}' 已存在；先 kill 或换一个 --name")
        return 1

    # 校验 tmux 存在
    if subprocess.run(["which", "tmux"], capture_output=True).returncode != 0:
        print("错误: 未找到 tmux 命令")
        return 1

    check = subprocess.run(
        ["tmux", "has-session", "-t", args.tmux_target],
        capture_output=True,
    )
    if check.returncode != 0:
        print(f"错误: tmux session 不存在: {args.tmux_target}")
        return 1

    print(f"镜像 tmux session: {args.tmux_target}")
    print(f"  daemon session : {session_name}")
    print(f"  socket         : {get_socket_path(session_name)}")
    print(f"  mq snapshot    : {get_socket_dir() / (_safe_filename(session_name) + '.mq')}")
    print()

    _run_server_blocking(
        session_name=session_name,
        claude_args=[],
        cli_type=args.cli_type,
        debug_screen=args.debug_screen,
        debug_verbose=False,
        tmux_mirror_target=args.tmux_target,
    )
    return 0


def cmd_list(args):
    """列出活跃会话"""
    sessions = list_active_sessions()
    if not sessions:
        print("没有活跃的会话")
        return 0

    if args.json:
        print(json.dumps(sessions, indent=2, ensure_ascii=False, default=str))
        return 0

    print(f"{'CLI':<8} {'PID':<10} {'名称':<40}")
    print("-" * 60)
    for s in sessions:
        cli_type = s.get('cli_type', 'claude')
        print(f"{cli_type:<8} {s['pid']:<10} {s['name']}")
    print(f"\n共 {len(sessions)} 个会话")
    return 0


def cmd_kill(args):
    """终止会话"""
    pid_file = get_pid_file(args.name)
    if not pid_file.exists():
        print(f"会话 '{args.name}' 不存在")
        return 1

    try:
        pid = int(pid_file.read_text())
        os.kill(pid, 15)  # SIGTERM
        print(f"已发送 SIGTERM 到 PID {pid}")
        # 等待最多 3 秒
        for _ in range(30):
            time.sleep(0.1)
            try:
                os.kill(pid, 0)  # 探测
            except ProcessLookupError:
                break
        else:
            os.kill(pid, 9)  # SIGKILL
            print(f"强制 SIGKILL")
    except (ValueError, ProcessLookupError) as e:
        print(f"读取 PID 失败: {e}")

    cleanup_session(args.name)
    print(f"已清理 {args.name}")
    return 0


def cmd_paths(args):
    """打印一个会话的 socket / mq / pid 路径（机器可读）"""
    out = {
        "session_name": args.name,
        "socket": str(get_socket_path(args.name)),
        "mq":     str(get_socket_dir() / f"{_safe_filename(args.name)}.mq"),
        "pid_file": str(get_pid_file(args.name)),
        "active": get_socket_path(args.name).exists(),
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def main():
    parser = argparse.ArgumentParser(
        prog="agent-remote-core",
        description="Short-lived PTY-host runtime — start / serve --foreground / mirror / list / kill / paths",
    )
    # Namespace 全局参数：所有 socket / mq / pid / FIFO 落在这个目录下
    # 优先级：flag > AGENT_REMOTE_CORE_DATA_DIR env > 默认 /tmp/remote-claude
    parser.add_argument(
        "--data-dir",
        help="Runtime directory for sockets / mq / pid files. "
             "Default: $AGENT_REMOTE_CORE_DATA_DIR or /tmp/remote-claude. "
             "Use this to namespace-isolate sessions from other apps using "
             "the same core binary (e.g. agentara → /tmp/agentara/).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_serve = sub.add_parser(
        "serve",
        help="前台模式：在当前进程跑 PTY + stdout/stdin 透传（给 cla -f / claude-squad program 用）",
    )
    p_serve.add_argument("--foreground", action="store_true", required=True,
                         help="必须指定，标记前台模式")
    p_serve.add_argument("--name",
                         help="daemon session 名；不传时自动用当前 tmux session name")
    p_serve.add_argument("--cli-type", default="claude", choices=["claude", "codex"])
    p_serve.add_argument("--debug-screen", action="store_true")
    p_serve.add_argument("cli_args", nargs="*", help="传给 CLI 的参数")
    p_serve.set_defaults(func=cmd_serve)

    p_start = sub.add_parser("start", help="启动新会话（PTY 包 claude/codex）")
    p_start.add_argument("name")
    p_start.add_argument("--cli-type", default="claude", choices=["claude", "codex"])
    p_start.add_argument("--debug-screen", action="store_true")
    p_start.add_argument("--debug-verbose", action="store_true")
    p_start.add_argument("cli_args", nargs="*", help="传给 CLI 的参数")
    p_start.set_defaults(func=cmd_start)

    p_mirror = sub.add_parser("mirror", help="镜像已有的 tmux session")
    p_mirror.add_argument("tmux_target", help="要镜像的 tmux session 名（如 claudesquad_xxx）")
    p_mirror.add_argument("--name", help="daemon session 名（默认 = tmux_target）")
    p_mirror.add_argument("--cli-type", default="claude", choices=["claude", "codex"])
    p_mirror.add_argument("--debug-screen", action="store_true")
    p_mirror.set_defaults(func=cmd_mirror)

    p_list = sub.add_parser("list", help="列出活跃会话")
    p_list.add_argument("--json", action="store_true")
    p_list.set_defaults(func=cmd_list)

    p_kill = sub.add_parser("kill", help="停止会话")
    p_kill.add_argument("name")
    p_kill.set_defaults(func=cmd_kill)

    p_paths = sub.add_parser("paths", help="打印 socket / mq 路径")
    p_paths.add_argument("name")
    p_paths.set_defaults(func=cmd_paths)

    args = parser.parse_args()

    # 应用 namespace（必须在调用任何 cmd_* 之前，以便 set_data_dir 改变
    # utils.session.SOCKET_DIR，让 get_socket_dir() 拿到新值）
    if args.data_dir:
        set_data_dir(args.data_dir)

    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
