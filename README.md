# agents-remote-core

Short-lived PTY-host runtime that wraps a CLI agent (Claude Code / Codex) and exposes its terminal state as a structured shared-memory snapshot. Multiple consumers (TUIs, chat-bot bridges, web dashboards) can attach simultaneously without each running their own ANSI parser.

Extracted from [remote_claude](https://github.com/xluos/remote_claude); the runtime is reusable on its own through the [`@agents-remote/sdk`](https://www.npmjs.com/package/@agents-remote/sdk) (TypeScript) or by talking directly to the protocol.

## What it does

```
  Claude / Codex CLI            ← runs inside a PTY managed by this daemon
        ↑
   pty.fork() / tmux pipe-pane  ← two ways to feed bytes in
        ↓
   pyte HistoryScreen (220×100, history=5000)
        ↓
   Claude / Codex Parser        ← reverse-parses ANSI → structured ClaudeWindow
        ↓
  ┌─ Unix Socket (/tmp/remote-claude/<name>.sock)   ← input + protocol
  └─ Shared Memory (/tmp/remote-claude/<name>.mq)   ← full snapshot, mmap
```

Two modes:

- **`start`** — daemon owns a fresh CLI subprocess via `pty.fork()`. Classic mode.
- **`mirror`** — daemon **observes** an existing tmux session via `tmux pipe-pane -o`, parses the byte stream identically. Zero-modification observability of any tmux-hosted CLI (e.g. sessions created by [claude-squad](https://github.com/smtg-ai/claude-squad)).

Mirror mode is end-to-end byte-faithful — `\x1b[5m` (blink) survives, so streaming detection works the same as in `start` mode.

## Install

```bash
pip install agents-remote-core
# or
uv tool install agents-remote-core
```

Requires Python ≥ 3.9 and (for mirror mode) `tmux`.

## Quick start

```bash
# Mode 1: start a fresh session under the daemon
agents-remote-core start mywork -- --model claude-opus-4-7

# Mode 2: mirror an existing tmux session (e.g. one claude-squad created)
agents-remote-core mirror claudesquad_a1b2c3

# List active sessions
agents-remote-core list

# Stop one
agents-remote-core kill mywork
```

Each session exposes:
- `/tmp/remote-claude/<name>.sock` — Unix socket for input + control messages
- `/tmp/remote-claude/<name>.mq`   — 200 MB mmap, full ClaudeWindow snapshot

## Consuming from your code

The protocol is plain JSON + mmap, so any language can read it. The official client SDK is in TypeScript: [`@agents-remote/sdk`](https://www.npmjs.com/package/@agents-remote/sdk).

```ts
import { SessionReader, SessionWriter } from "@agents-remote/sdk";

const reader = new SessionReader("mywork");
for await (const snap of reader.subscribe()) {
  for (const block of snap.blocks) {
    if (block._type === "OutputBlock") console.log(block.content);
  }
}

const writer = new SessionWriter("mywork");
await writer.sendText("refactor this function");
await writer.sendEnter();
```

See the SDK's README for the full surface (options, control-key helpers, snapshot schema).

## Architecture

- **Full-snapshot model**: every `ClaudeWindow` write is a complete overwrite, not a delta. Consumers can join late, miss frames, or crash and restart — they'll catch up on the next read, no incremental-state machine to repair.
- **Two parsers**: Claude CLI (Ink-based, divider-line layout) and Codex CLI (background-color regions, `›` prompt). Pick via `--cli-type`.
- **Time-window smoothing** (~1s): blink flicker, intermittent missing frames, and PTY noise are smoothed before being published.

For the full design rationale see the upstream [`remote_claude/CLAUDE.md`](https://github.com/xluos/remote_claude/blob/main/CLAUDE.md).

## License

MIT
