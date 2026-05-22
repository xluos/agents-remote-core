# ADR-0001 — Defer the TypeScript rewrite; prefer single-binary distribution

- **Status**: Accepted
- **Date**: 2026-05-21
- **Affects**: `agents-remote-daemon`, indirectly `@agents-remote/sdk` and `claude-squad-ts-remote`

## Context

The daemon is currently Python. Its dependencies are tiny (`pyte` only) but it
still requires the consumer's machine to have Python on PATH, typically
installed via `uv tool install` or `pip install`. The rest of the stack
(`@agents-remote/sdk`, `claude-squad-ts-remote`) is TypeScript/Bun.

The natural question: should the daemon also be TypeScript so the whole stack
is one language?

## Decision

**Do not migrate the daemon to TypeScript at this point.** Instead, when the
"users have to install Python" friction becomes real, solve it by **packaging
the daemon as a single binary** (PyInstaller / shiv / pex) so that the user-
facing install story is just "download a binary" — independent of what
language wrote it.

## Why

1. **The pyte parser is a real asset.** The Claude and Codex parsers in
   `src/agents_remote_daemon/parsers/` (≈2000 LOC combined) have been
   production-hardened for several months:
   - inter-frame `dot_row_cache` persistence (the blinking-dot retry trick),
   - 1-second timing-window smoothing for status lines and streaming flags,
   - box-drawing merge logic for PlanBlocks,
   - two completely different layout heuristics for Claude vs Codex.
   Rewriting these on top of `@xterm/headless` means re-discovering every one
   of those edge cases. The cost is enormous and the upside is mostly
   aesthetic.

2. **The real friction is install distribution, not language.** "User has to
   install Python" is a packaging problem, not an implementation-language
   problem. A self-contained binary solves it without touching parser code.

3. **The SDK already hides Python from TS consumers.** From a
   `@agents-remote/sdk` user's perspective there is no Python anywhere — they
   call TS APIs, read mmap files, send bytes over a Unix socket. Python only
   lives in the daemon process itself, which is a system component, not a
   library the consumer composes with.

4. **TypeScript is not obviously a better choice for terminal-parsing work.**
   `pyte` and `xterm.js` have different design philosophies (emulator-first
   vs. renderer-first). `xterm.js` headless mode works but its buffer API is
   callback-style (`line.getCell(x).isBlink()`), whereas pyte's
   `buffer[row][col].blink` is the natural fit for how the parser reads the
   screen. Rewriting on the latter is a downgrade in ergonomics for the
   hottest path in the codebase.

## Consequences

- The daemon remains Python; releases need a Python build pipeline.
- Consumers of `@agents-remote/sdk` continue to require the daemon binary on
  PATH but never write Python themselves.
- `claude-squad-ts-remote` continues to invoke the daemon by spawning a child
  process (cannot embed it in-process).
- Cross-process state remains mmap + Unix socket (`shared_state.py` v2 format
  documented in the SDK's `reader.ts`).

## Alternatives considered

| Option | Why not now |
|---|---|
| Full TypeScript rewrite of daemon (`node-pty` + `@xterm/headless`) | ~2000 LOC parser rewrite; loses months of production-tested edge-case coverage; no clear user-visible win once distribution is solved |
| Hybrid: keep Python parser, port only the PTY shell to TS | Worse than either pure option — you still need a Python sidecar AND have a new TS-Python boundary to debug |
| Status quo (no packaging work) | Acceptable today; users who run the daemon are usually capable of `pip install`. Revisit when distribution friction becomes noticeable. |

## Revisit triggers

Reopen this decision when **any** of these become true:

1. **User-reported friction** about installing Python crosses some threshold
   (e.g. multiple unrelated users hit it in onboarding).
2. **In-process embedding** of the daemon becomes desirable — e.g. wanting to
   import the daemon directly into `claude-squad-ts-remote` or `agentara` to
   eliminate the spawn-and-IPC overhead. The current architecture can't do
   this across languages.
3. **A drop-in TS terminal-emulator library** matures with a pyte-shaped API
   (direct cell access, all SGR attributes, history buffer). Today's
   `@xterm/headless` is render-shaped.
4. **The Python parser's coverage starts decaying** (Claude/Codex change
   their UI in ways the parser can't keep up with). At that point the rewrite
   cost is no longer "throwing away working code" — it's "rewriting code that
   has to change anyway."

## Action items (when we do execute the distribution work)

These are scoped for the **packaging** path, not the rewrite path:

- [ ] Pick a packager: PyInstaller (most mature, biggest binaries) vs shiv
      (Python-aware, requires Python on target — no go) vs pex (similar
      tradeoff). **Default: PyInstaller**.
- [ ] Write a `release.spec` for PyInstaller covering `pyte`, `dataclasses`,
      and the `parsers` submodule (it uses `from .. import ...`).
- [ ] GitHub Actions release workflow producing:
  - `agents-remote-daemon-{version}-macos-arm64`
  - `agents-remote-daemon-{version}-macos-x86_64`
  - `agents-remote-daemon-{version}-linux-x86_64`
- [ ] (Optional) `homebrew-tap` repo with a Formula so `brew install
      xluos/tap/agents-remote-daemon` works.
- [ ] Update SDK README and `claude-squad-ts-remote` README to document the
      binary install path as the recommended option.

Expected effort: a few hours, zero source-code changes.

## Notes

- This decision was made in the same session that initially split out
  `agents-remote-daemon`, `@agents-remote/sdk`, and `claude-squad-ts-remote`
  from the upstream `remote_claude` project, with the end-to-end mirror flow
  verified working (Claude/Codex byte stream → tmux pipe-pane → pyte →
  parser → mmap → SDK reader, with `is_streaming: true` preserved).
- The mirror-mode feature itself is new in this repo — it did not exist in
  the upstream `remote_claude` (which only supported PTY-fork mode). All the
  parser code is, however, lifted from upstream and unchanged in behavior.
