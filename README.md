# mcp-gdb

MCP server that exposes GDB debugging as tools. An AI assistant can set
breakpoints, run programs, step through code, inspect variables and memory,
and examine registers — all via structured tool calls.

## Requirements

- Python 3.12+
- GDB on `$PATH`
- [rr](https://rr-project.org/) on `$PATH` (optional — only needed for `rr_record` / `start_replay_session`)

## Installation

```bash
git clone https://github.com/yourname/mcp-gdb
cd mcp-gdb
uv sync
```

## Connecting to Claude

### Claude Code

```bash
claude mcp add gdb -- uv run --directory /path/to/mcp-gdb python server.py
```

Or add manually to `~/.claude.json`:

```json
{
  "mcpServers": {
    "gdb": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/mcp-gdb", "python", "server.py"]
    }
  }
}
```

### Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "gdb": {
      "command": "/path/to/mcp-gdb/.venv/bin/python",
      "args": ["/path/to/mcp-gdb/server.py"]
    }
  }
}
```

## Tools

### Session management

| Tool | Description |
|---|---|
| `start_session` | Spawn a GDB process, optionally loading a binary |
| `stop_session` | Kill a session and free its resources |
| `list_sessions` | Show all active sessions with idle time and kind (`gdb` / `rr-replay`) |

### Time-travel debugging (rr)

[rr](https://rr-project.org/) records a full execution trace and replays it deterministically, enabling reverse-execution (`reverse-continue`, `reverse-step`, etc.).

| Tool | Description |
|---|---|
| `rr_record` | Record a program execution; returns `trace_dir` for later replay |
| `start_replay_session` | Start an rr replay session; accepts `trace_dir` from `rr_record`, or omit to replay the latest recording |

A replay session works with all standard tools. Reverse-execution is available via `exec_command`:

```
reverse-continue   run backwards to the previous breakpoint or watchpoint
reverse-step       step backwards one source line (entering calls)
reverse-next       step backwards one source line (skipping calls)
reverse-finish     run backwards to where the current function was called
```

**Typical workflow:**

```
rr_record("/path/to/binary", args=["--flag"])
  → { "trace_dir": "/home/user/.local/share/rr/binary-0", ... }

start_replay_session(trace_dir="/home/user/.local/share/rr/binary-0")
  → { "session_id": "a1b2c3d4", ... }

# Now use the session_id with any tool: breakpoint, run, reverse-continue, etc.
```

### Execution control

Execution tools block until the inferior stops (breakpoint, signal, exit, or
timeout). While blocked, use `interrupt` to send SIGINT and unblock.

| Tool | GDB command | Description |
|---|---|---|
| `run` | `run` | Start or restart the inferior |
| `continue_exec` | `continue` | Continue after a stop |
| `step` | `step` / `stepi` | Step into next line or instruction |
| `next` | `next` / `nexti` | Step over next line or instruction |
| `finish` | `finish` | Run until current function returns |
| `until` | `until` | Run until a specific location (skip loops) |
| `interrupt` | SIGINT | Interrupt a running inferior |

### Breakpoints and watchpoints

| Tool | GDB command | Description |
|---|---|---|
| `breakpoint` | `break` / `tbreak` | Set a breakpoint (supports conditions) |
| `delete_breakpoints` | `delete` | Delete one or all breakpoints |
| `watch` | `watch` / `rwatch` / `awatch` | Stop when an expression is written, read, or accessed |

### Threads

| Tool | GDB command | Description |
|---|---|---|
| `list_threads` | `info threads` | List all threads with their current location |
| `select_thread` | `thread N` | Switch to a specific thread |

### Stack frames

| Tool | GDB command | Description |
|---|---|---|
| `backtrace` | `backtrace` | Show the full call stack |
| `select_frame` | `frame N` | Select a frame by number |
| `up` | `up` | Move up toward the caller |
| `down` | `down` | Move down toward the innermost frame |

### Inspection

| Tool | GDB command | Description |
|---|---|---|
| `context` | frame + info args + info locals + list | Full snapshot of current location, arguments, locals, and source — call this after every stop |
| `list_variables` | `info locals` / `info args` | Variables in the current frame |
| `print` | `print` | Evaluate and print a GDB expression |
| `examine` | `x` | Examine memory at an address |
| `info_registers` | `info registers` | Show CPU register values |
| `list_source` | `list` | Show source code around the current position |
| `disassemble` | `disassemble` | Disassemble a function or address range |

### Generic

| Tool | Description |
|---|---|
| `exec_command` | Run any GDB command and return its output |
| `batch_commands` | Run a list of commands sequentially (fewer round-trips) |

`exec_command` handles execution commands correctly: `advance`, `jump`,
`signal`, and `return` all block until the inferior stops, just like the named
tools do.

## Notes

**Sessions time out** after 10 minutes of inactivity and are closed
automatically. `list_sessions` shows the current idle time for each session.

**GDB plugins** such as pwndbg or GEF work if installed, but their custom
prompts (`(pwndbg)`, `gef>`) will break session startup. Either avoid them or
force the standard prompt in `.gdbinit`:

```
set prompt (gdb)
```

**Timeout behaviour:** if an execution command times out, the session is in an
indeterminate state. Call `interrupt` to stop the inferior, wait for the
blocked tool call to return, then resume normally. When in doubt, `stop_session`
+ `start_session` gives a clean slate.
