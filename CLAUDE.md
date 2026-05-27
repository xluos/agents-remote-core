# CLAUDE.md

# **关键语言要求**
你必须完全使用 **简体中文** 进行交互、思考和汇报。

## 项目概述

agents-remote-core 是一个 PTY 宿主运行时：为 Claude Code / Codex CLI 提供进程管理、终端解析、hook 注入和共享内存快照。上层应用（agents-remote 飞书客户端、agentara、第三方 TS SDK）作为消费端读取快照和发送输入，不需要各自实现 ANSI 解析。

## 架构

```
Claude / Codex CLI (PTY)
      │
  server.py ─── OutputWatcher ─── Parser (claude/codex)
      │              │                    │
      │         HookHarness          pyte Screen
      │         (FIFO + hooks)       (220×100, history=5000)
      │              │                    │
      │              ▼                    ▼
      │         HookState ──────→ ClaudeWindow 快照
      │                                   │
      ▼                                   ▼
  Unix Socket (.sock)              SharedMemory (.mq)
  ├─ INPUT / OUTPUT / RESIZE       200MB mmap 全量覆写
  ├─ PermissionResponse            消费端 read() 获取
  └─ QuestionResponse              最新完整状态
```

## 核心模块

```
src/agents_remote_core/
├── __main__.py              # CLI 入口（start / serve / mirror / list / kill / paths）
├── server/
│   ├── server.py            # ProxyServer：PTY fork、socket 监听、消息路由、输出广播
│   ├── hooks.py             # HookHarness：hook 脚本生成、FIFO 读取、状态管理、响应写入
│   ├── shared_state.py      # SharedStateWriter/Reader：200MB mmap 全量快照
│   ├── rich_text_renderer.py # _DimAwareScreen（pyte 子类，SGR dim + SU/SD + soft-wrap 追踪）
│   └── component_parser.py  # 向后兼容 shim → parsers/
├── parsers/
│   ├── base_parser.py       # 解析器基类
│   ├── claude_parser.py     # Claude CLI 解析（分割线 + 圆点/星号 + blink）
│   └── codex_parser.py      # Codex CLI 解析（背景色区域 + › 提示符）
└── utils/
    ├── protocol.py          # 消息协议（9 种类型，含 PermissionResponse / QuestionResponse）
    ├── session.py           # socket/mq/pid 路径管理，--data-dir namespace 隔离
    └── components.py        # 数据模型（OutputBlock / UserInput / StatusLine / OptionBlock / ...）
```

## Hook 系统（Claude Code + Codex CLI 双模式）

HookHarness 在启动时创建 FIFO 管道和 shell 脚本，注入到 CLI 进程：

| | Claude Code | Codex CLI |
|---|---|---|
| **注入方式** | `--settings '{"hooks":...}'` 内联 JSON | 合并到 `~/.codex/hooks.json`（标记 `__agents_remote_core__`，cleanup 时移除） |
| **AskUserQuestion** | PreToolUse 双向拦截 → `updatedInput.answers` | 不存在（Plan Mode 内建交互） |
| **PermissionRequest** | 独立事件，双向（allow/deny） | 相同 |
| **状态追踪事件** | SessionStart / Stop / PreToolUse / PostToolUse / Notification | SessionStart / Stop / PreToolUse / PostToolUse |

两类 hook 脚本：
- **relay.sh**（单向）：写 FIFO 后立即 exit 0
- **permission.sh**（双向）：写 FIFO 后等待 `resp_*` 响应文件

HookState 字段：
```python
session_id, transcript_path           # 会话元信息
turn_complete, turn_error             # 轮次状态
active_tool, tool_status              # 工具执行状态
pending_permission, pending_question  # 等待人工交互
waiting_permission                    # 权限提示已出现
```

## 共享内存快照（ClaudeWindow）

```python
ClaudeWindow {
    blocks: list          # 累积型：OutputBlock / UserInput / PlanBlock / SystemBlock
    status_line: object   # 状态型：执行进度 | None
    bottom_bar: object    # 状态型：权限/agent 状态 | None
    agent_panel: object   # 状态型：agent 管理面板 | None
    option_block: object  # 状态型：选项交互 | None
    hook_state: object    # HookState 权威状态 | None（无 hook 时为 None）
    input_area_text: str
    timestamp: float
    layout_mode: str      # normal | option | detail | agent_list | agent_detail
    cli_type: str         # claude | codex
}
```

## 常用命令

```bash
# 安装
uv sync

# 前台启动（在当前进程跑，Ctrl+C 退出）
agents-remote-core start <会话名> [--cli-type codex] [-- CLI参数]

# 前台模式（tmux pane 内用，stdin/stdout 透传）
agents-remote-core serve --foreground --name <名> [-- CLI参数]

# 镜像已有 tmux session（只读旁观）
agents-remote-core mirror <tmux-session-name>

# 管理
agents-remote-core list [--json]
agents-remote-core kill <会话名>
agents-remote-core paths <会话名>
```

### namespace 隔离

```bash
# 默认路径：/tmp/remote-claude/
# 上层应用可通过 --data-dir 隔离
agents-remote-core --data-dir /tmp/agents-remote start mywork
agents-remote-core --data-dir /tmp/agentara start mywork
```

## 测试

```bash
uv run python3 tests/test_codex_split_regions.py   # Codex 区域分割
uv run python3 tests/test_codex_option_block.py     # Codex 选项交互
uv run python3 tests/test_renderer.py               # rich_text_renderer
```

## 开发须知

- 上层应用 **agents-remote** 通过 editable 包依赖引用本项目
- 改本项目代码即生效（editable 模式），agents-remote 的 `uv run` 自动拿到最新
- 终端解析规则的修改在本仓库的 `parsers/` 目录
- hook 注入逻辑在 `server/hooks.py`
- 共享内存格式变更需同步更新 agents-remote 的 `utils/shared_state_reader.py`
