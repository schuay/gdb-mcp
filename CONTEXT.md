# Development Context for mcp-gdb

This document captures the full design rationale, protocol mechanics, and architectural decisions made during initial development. It is written so that a new AI agent (or human) can continue work without re-deriving any of it.

---

## What this project does

It bridges two protocols:

```
AI client (Claude / other)
        ↕  MCP over stdio
   server.py  (this server)
        ↕  GDB/MI2 over stdio
      gdb  (subprocess)
```

The AI client invokes MCP tools. Each tool translates user intent into one or more GDB commands, formats the output as plain text, and returns it.

---

## Protocol 1: Model Context Protocol (MCP)

MCP is the protocol used between AI assistants and tool servers. The Python library is `mcp[cli]` (PyPI), specifically `mcp.server.fastmcp.FastMCP`.

**How FastMCP works:**
- `FastMCP("name", lifespan=ctx)` creates a server.
- `@mcp.tool()` decorators on async functions register tools. The function's docstring becomes the tool description shown to the AI; parameter names and type annotations become the input schema.
- `mcp.run()` starts the stdio transport. Internally it uses `anyio` and `mcp.server.stdio.stdio_server()`.
- The `lifespan` parameter is an `@asynccontextmanager` async generator. It runs setup before `yield` (starting the cleanup task) and teardown after (closing all sessions). It runs inside the same asyncio event loop as the tool handlers.

**Tool return values:** Tools return strings, dicts, or lists. FastMCP wraps them in the MCP `TextContent` (or structured) response format automatically. Returning a `dict` or `list` from a tool results in JSON in the response.

**Python name conflicts:** Several natural GDB command names conflict with Python builtins or keywords. The `@mcp.tool(name="...")` override is used to expose them under their natural names:

| Python function name | MCP tool name | Conflict avoided |
|---|---|---|
| `next_line` | `next` | `next()` builtin |
| `set_breakpoint` | `breakpoint` | `breakpoint()` builtin |
| `print_expr` | `print` | `print()` builtin |
| `examine_memory` | `examine` | clarity |
| `continue_exec` | `continue_exec` | `continue` keyword |

---

## Protocol 2: GDB Machine Interface (GDB/MI2)

GDB has two interface modes:
- **CLI mode** (`gdb --quiet`): interactive text, same as a human terminal session.
- **MI mode** (`gdb --interpreter=mi` or `mi2`): structured machine-readable output designed for IDEs and tools.

This project uses **MI2 mode** exclusively.

### Why MI2 over plain CLI

The core problem with CLI mode is **output termination detection**: how do you know when a command's output is done? In CLI mode you scan for the `(gdb)` prompt string, but a program whose output contains `(gdb)` literally would falsely terminate the read. In MI2 mode this is impossible because *all* program output is framed with `@"..."` record prefixes — the bare `(gdb)` prompt can only ever be the actual prompt.

MI2 also gives structured error records (`^error,msg="..."`) that are easy to distinguish from normal output, which improves error reporting.

### MI2 output record types

Every line of GDB output in MI2 mode has a typed prefix:

```
~"text\n"        console stream record  — GDB's own messages to the user
@"text\n"        target stream record   — inferior's stdout/stderr
&"text\n"        log stream record      — command echo (GDB repeats what you typed)
^done[,...]      result record          — command completed successfully
^running[,...]   result record          — inferior started running
^error,msg="..."  result record          — command failed
^exit            result record          — GDB is exiting
*stopped,...     async record           — inferior stopped (breakpoint, signal, etc.)
*running,...     async record           — inferior started running
=notify,...      notify record          — background event (thread created, etc.)
(gdb)            prompt                 — end of output for this command
```

The strings inside `~"..."`, `@"..."`, `&"..."` use C-style escaping: `\n`, `\t`, `\r`, `\\`, `\"`.

### Output formatting strategy

Rather than parsing every MI record type fully, `_format_output()` in `gdb.py` applies a simple filter:

- **Include** `~"..."` (console) and `@"..."` (target): unescape and emit as plain text
- **Skip** `&"..."` (log): this is the command echo — the user typed `print x`, GDB echoes `&"print x\n"`, we skip it
- **Format** `^error,msg="..."` as `Error: message\n`
- **Format** `*stopped,...` as `[Stopped: reason, in func, at file:line]` by regex-extracting the key fields
- **Skip everything else**: `^done`, `^running`, `^exit`, `=...`, `*running`

The result is that the tool returns exactly what a human would see in a terminal session, with the addition of `[Stopped: ...]` lines that summarize stop events.

### The `^error` message regex

The error message is a quoted MI string, which can contain escaped quotes. A naive `msg="(.*?)"` stops at the first `\"`, truncating the message. The correct regex is:

```python
r'msg="((?:[^"\\]|\\.)*)"'
```

This matches either a non-quote-non-backslash character (`[^"\\]`) or an escaped character (`\\.`), correctly spanning past `\"` sequences.

### Prompt detection

The prompt `(gdb)` or `(gdb) ` is matched with:

```python
_PROMPT_RE = re.compile(r"^\(gdb\)\s*$")
```

The `\s*` handles the trailing space that GDB includes. This is the sole termination signal for `send()`.

### Session startup sequence

1. Spawn `gdb --interpreter=mi2 --quiet [binary]`
2. `stderr=asyncio.subprocess.STDOUT` — merge stderr into stdout so the MI reader sees everything on one stream (GDB startup warnings, etc.)
3. Read and discard all output until the first `(gdb)` prompt (`_drain_to_prompt`)
4. Send three hardening commands:
   - `set pagination off` — prevents GDB from pausing output with `--Type <return> to continue--`; this would block the reader forever waiting for input that never comes
   - `set confirm off` — prevents GDB from asking "Are you sure?" on dangerous operations
   - `set breakpoint pending on` — allows setting breakpoints on symbols that don't exist yet (e.g. before a shared library loads)
5. If a binary and args were given, send `set args ...`
6. Register the session in `GdbManager._sessions`

### Command execution (`GdbSession.send`)

```
acquire asyncio.Lock
write cmd + "\n" to stdin
drain stdin
read stdout lines until (gdb) prompt → collect into list
format list with _format_output()
release lock
return formatted string
```

The `asyncio.Lock` ensures that concurrent MCP tool calls on the same session never interleave their writes and reads. Without it, if two tool calls write simultaneously, their output would be mixed together.

The inner `_collect()` coroutine is wrapped with `asyncio.wait_for(timeout=30.0)`. On timeout, the implementation sends SIGINT to GDB and drains stdout to the next `(gdb)` prompt before raising `GdbError`. If recovery succeeds, the session is left clean and usable. If recovery fails, `_broken` is set and all future `send()` calls raise immediately.

### Interrupt mechanism

`run` and `continue_exec` block inside `send()` (holding the lock) until the inferior stops. There's no way to send a second GDB command while the lock is held. To interrupt a running inferior, `GdbSession.interrupt()`:

1. Does **not** acquire the lock
2. Calls `self.process.send_signal(signal.SIGINT)` directly

SIGINT causes GDB to stop the inferior and emit `*stopped,reason="signal-received",...` followed by `(gdb)`. This unblocks the pending `send()` call, which then returns the stop output and releases the lock.

### Session IDs

Session IDs are `uuid.uuid4().hex[:8]` — the first 8 hex characters of a UUID4. This gives 4 billion possible values, which is adequate for any realistic number of concurrent sessions. Shorter IDs are easier to type in tool calls.

### Session cleanup

`GdbManager._cleanup_loop()` runs as a background asyncio task (started in the FastMCP lifespan). It wakes every 60 seconds and calls `close()` on any session where `time.monotonic() - last_used > IDLE_TIMEOUT` (default 600 seconds / 10 minutes). `last_used` is updated on every `send()` call.

On server shutdown (FastMCP lifespan teardown), all remaining sessions are explicitly closed regardless of idle time.

### GDB shutdown sequence (`GdbSession.close`)

Three escalating attempts, each wrapped in `contextlib.suppress(Exception)` so failures at one level don't prevent trying the next:

1. Write `quit\n` to stdin, wait up to 3 seconds for GDB to exit normally
2. `process.terminate()` (SIGTERM), wait up to 2 seconds
3. `process.kill()` (SIGKILL)

---

## Architecture walkthrough: what happens when a tool is called

### `breakpoint(session_id, "main")`

1. FastMCP calls `set_breakpoint("...", "main")` in the asyncio event loop.
2. `manager.get(session_id)` looks up the `GdbSession`.
3. `s.send("break main")` acquires the lock, writes `break main\n`, reads until `(gdb)`.
4. GDB responds with `~"Breakpoint 1 at 0x401234: file foo.c, line 10.\n"\n^done\n(gdb)`.
5. `_format_output` strips MI framing → `"Breakpoint 1 at 0x401234: file foo.c, line 10.\n"`.
6. Since no `condition` was given, return immediately.

### `run(session_id)` hitting a breakpoint

1. `s.send("run", timeout=30.0)` acquires the lock, writes `run\n`.
2. GDB emits:
   ```
   &"run\n"
   ~"Starting program: /tmp/a.out\n"
   =thread-group-started,id="i1",pid="1234"
   =thread-created,id="1",group-id="i1"
   ~"\n"
   ~"Breakpoint 1, main () at foo.c:10\n"
   ~"10\t    int x = 42;\n"
   *stopped,reason="breakpoint-hit",disp="keep",bkptno="1",frame={...,func="main",...,file="foo.c",...,line="10"}
   (gdb)
   ```
3. `_collect()` accumulates lines until `(gdb)`, then returns.
4. `_format_output` produces:
   ```
   Starting program: /tmp/a.out

   Breakpoint 1, main () at foo.c:10
   10	    int x = 42;
   [Stopped: breakpoint-hit, in main, at foo.c:10]
   ```

### `batch_commands(session_id, ["info locals", "backtrace"])`

1. `manager.get(session_id)` → session.
2. Loop: `s.send("info locals")` → output dict, `s.send("backtrace")` → output dict.
3. Return `[{"command": "info locals", "output": "...", "error": false}, ...]`.
4. Commands are sequential (each `send` awaits completion before the next), so the lock is acquired and released once per command.

---

## Key design decisions

**Python + asyncio, not TypeScript/Go.** The most popular reference (signal-slot/mcp-gdb) is TypeScript. Python with asyncio is equally capable: both protocols are line-oriented stdio I/O, and `asyncio.create_subprocess_exec` with `StreamReader` handles it cleanly. Python is shorter, has no compile step, and the resulting code is easy to read and modify.

**No GDB/MI parsing library.** The full MI grammar is complex (nested tuples, lists, key=value trees). We don't need it: all the human-readable output is already in the `~"..."` console stream records, and the `*stopped` record only needs a handful of fields regex-extracted. Pulling in a full MI parser would add a dependency and a lot of code for no benefit in the LLM use case.

**CLI commands in MI2 mode, not MI commands.** GDB/MI has native MI commands like `-break-insert`, `-exec-run`, `-data-evaluate-expression`. These produce structured key=value output, which would need to be parsed and re-formatted. Instead, we send plain CLI commands (`break main`, `run`, `print x`) while still running in MI2 mode. GDB executes them identically, and their output appears in `~"..."` console stream records in the same human-readable form a terminal user would see. The LLM gets familiar-looking output, and the codebase avoids a translation layer.

**Blocking execution model (MCP/MI2 impedance mismatch).** MCP is a request-response protocol: a tool call must return a single response. GDB/MI2's execution model is asynchronous: execution commands (`run`, `continue`, `step`, etc.) emit `^running` immediately when the inferior starts, then emit `*stopped` later when it halts, each followed by a `(gdb)` prompt. This means the prompt appears *twice* for execution commands. A naive read-until-prompt returns too early, leaving `*stopped\n(gdb)\n` in the pipe to corrupt the next command's response.

The fix is `waiting_for_stop` in `_collect()`: when `^running` is seen, the flag is set and the intermediate `(gdb)` is suppressed; `*stopped` clears the flag; the subsequent `(gdb)` terminates the read normally. The mechanism is **data-driven** — it triggers on `^running` in the output, not on the command name. This means it applies correctly to all execution commands, including those sent via `exec_command` (`until`, `advance`, `jump`, `signal`, `return`).

An alternative non-blocking design was considered: `run`/`continue` return at `^running` with `{"status": "running"}`, and a separate `wait_for_stop` tool blocks until `*stopped`. This was rejected because: (1) `interrupt()` already unblocks a running session via SIGINT, so long-running programs can always be interrupted; (2) in GDB all-stop mode the LLM cannot usefully inspect state while the inferior runs; (3) a mandatory `wait_for_stop` call after every `run` adds a round-trip for no practical benefit.

**Named tools plus `exec_command` escape hatch.** Named tools for the most common operations improve LLM discoverability — the model doesn't need to know the exact GDB syntax for `backtrace` or `examine`. But GDB has hundreds of commands. `exec_command` takes any raw command string and returns its output, covering everything not explicitly wrapped. `batch_commands` further reduces round-trips for common multi-step setups.

**`asyncio.Lock` per session, not global.** Locking is at the session level, not the manager level. Multiple sessions (e.g. debugging two different processes) can run commands truly concurrently. Only commands within the same session are serialized. This is safe because each session has its own GDB process with its own stdin/stdout pipes.

**`interrupt()` intentionally bypasses the lock.** This is the only operation that writes to GDB's stdin without holding the lock. It's safe because:
1. We only send `SIGINT` (a signal), not data to stdin.
2. SIGINT is a well-defined operation that causes GDB to emit a `(gdb)` prompt, which unblocks any pending `send()`.
3. If the session is idle (lock is free), SIGINT to GDB while it's at the prompt does nothing meaningful.

**Merged stderr.** `stderr=asyncio.subprocess.STDOUT` puts GDB's stderr into the same stream as stdout. GDB startup warnings (about missing debug info, etc.) appear inline in the initial banner, which we discard during `_drain_to_prompt`. Subsequent stderr from GDB (rare in MI2 mode) would appear inline in command output. The alternative (separate `stderr=PIPE`) would require a second reader task and adds complexity for little gain.

**Short 8-char session IDs.** Full UUIDs (`uuid4().hex` = 32 chars) are awkward in tool call arguments. 8 hex chars (32 bits) are unambiguous for any practical number of concurrent sessions.

**No `--nx` flag.** We don't pass `--nx` (which skips `.gdbinit`), so users can use GDB plugins like pwndbg or GEF. The tradeoff is that plugin prompts (`(pwndbg)`, `gef>`) would break prompt detection. If this becomes an issue, add `--nx` as an option or force the prompt with `-ex "set prompt (gdb) "`.

---

## Reference implementations studied

Three open-source GDB MCP servers were surveyed before writing this one:

**signal-slot/mcp-gdb** (TypeScript, most popular): 18 named tools, MI mode but output treated as raw text using `line.includes('^done')` as terminator (fragile — fails if program output contains `^done`). No per-session locking (race condition risk). No session timeout. Includes VS Code URI links in source listing output. Inspired the named-tools approach.

**hnmr293/gdb-mcp** (Python): 4 tools (`open`, `call`, `close`, `list_sessions`). MI2 mode, `asyncio.Lock` per session, idle timeout, background sweep — the cleanest async architecture of the three. Also exposes GDB documentation as MCP Resources. Minimal tool surface means the LLM must know GDB syntax. Inspired the `asyncio.Lock`-per-session pattern, idle timeout, and session hardening.

**datobena/gdb-mcp** (Python, FastMCP): 7 tools, plain CLI mode (no MI), prompt scanning on raw bytes, supports pwndbg/GEF prompts out of the box. `batch_commands` tool. Graceful shutdown sequence. Inspired `batch_commands` and the three-stage shutdown (quit → SIGTERM → SIGKILL).

This implementation takes: MI2 for reliable termination (vs. signal-slot), named tools for discoverability (vs. hnmr293), `batch_commands` and graceful shutdown (from datobena), and `asyncio.Lock` + idle timeout (from hnmr293).

---

## Known limitations and edge cases

**Timeout triggers auto-recovery via SIGINT.** If `send()` times out (program ran past the timeout), the implementation automatically sends SIGINT to stop the inferior and drains stdout to the next `(gdb)` prompt. If recovery succeeds the session is left clean and fully usable. If recovery itself fails (GDB is unresponsive or has exited), the session is tainted (`_broken = True`) and all subsequent `send()` calls immediately raise `GdbError`. In the taint case, `stop_session` + `start_session` gives a clean slate.

**Programs that read from stdin will block.** The inferior's stdin is inherited from GDB's own stdin pipe (not a PTY). Programs that call `scanf`, `fgets`, `std::cin`, or any other blocking stdin read will hang indefinitely — the `run` tool will eventually time out. To test such programs, either redirect stdin from a file (`run < input.txt` via `exec_command`) or pre-feed input via `exec_command("set args ...")` before running.

**`set args` with shell metacharacters.** `GdbManager.create` builds the args string with `" ".join(args)` and sends `set args <string>` to GDB. GDB's `set args` uses its own shell-like parsing. Arguments with spaces or quotes need to be pre-escaped by the caller. There is no sanitization.

**No `--nx` means gdbinit runs.** If a `.gdbinit` changes the prompt string (e.g. pwndbg sets `(pwndbg) `), `_PROMPT_RE` will not match and `_drain_to_prompt` will hang until the 15-second timeout. Fix by adding the alternative prompt, or by having `start_session` accept a `gdbinit=False` option to pass `--nx`.

**`_STREAM_RE` assumes content ends at the last `"` on the line.** The regex `^([~@&])"(.*)"$` uses greedy `.*`, so for `~"foo"bar"` it would match content `foo"bar`. This is correct MI2 behavior (content is everything between the outermost quotes), but relies on GDB not emitting malformed records with unescaped interior quotes.

**Binary path passed to GDB via argv, not `file` command.** `GdbManager.create` appends the binary path directly to the GDB command line. Paths with spaces work because asyncio subprocess passes them as a separate argv element (no shell involved). However, if the binary is not found, GDB starts normally but shows a warning in the startup banner, which is discarded. The session appears healthy. The error only surfaces when `run` is called. To detect this earlier, `start_session` could call `exec_command("info target")` after creation.

**No async notification handling.** GDB can send `*stopped` records asynchronously (without a corresponding command), for example when the inferior hits a breakpoint while another command is in flight. In the current architecture, these notifications are only seen if they arrive before the `(gdb)` prompt that terminates a `send()` call. GDB/MI2 guarantees that `*stopped` is sent before the next prompt, so in practice this is not a problem — the notification is collected along with the result of whatever command was running.

**`interrupt()` on an idle session.** If `interrupt()` is called when no inferior is running, SIGINT goes to the GDB process itself, which in MI2 mode is typically a no-op (GDB re-displays the prompt). This is harmless but does nothing useful.

---

## How to add a new tool

1. Identify the GDB command(s) needed. Test them manually in a GDB session first.

2. Add an `@mcp.tool()` async function in `server.py`. Follow the pattern:
   - Call `manager.get(session_id)` to get the session (raises `GdbError` on bad ID).
   - Call `session.send("gdb command")` and return the string.
   - For multi-step tools, call `send()` multiple times (the lock is re-acquired each time).
   - Use `@mcp.tool(name="...")` if the natural name conflicts with a Python builtin.

3. Write a clear docstring — FastMCP uses it as the tool description. Include parameter explanations and GDB command equivalents.

**GDB commands worth adding:**
- `info locals` / `info args` → show local variables and parameters (currently via `exec_command`)
- `watch expr` → set a watchpoint (data breakpoint)
- `set var name = expr` → modify a variable at runtime
- `thread apply all bt` → backtrace all threads
- `frame N` / `up` / `down` → navigate the call stack
- `catch syscall` / `catch throw` → catchpoints
- `core-file /path` → load a core dump (signal-slot has a dedicated tool for this)
- `attach PID` → attach to a running process
- `source script.gdb` → execute a GDB script file

---

## rr record/replay support

### Overview

[rr](https://rr-project.org/) is a record-and-replay debugger: it records all non-determinism during a program run (syscalls, signals, etc.) and can replay that execution identically, including backwards. Two tools expose this:

- `rr_record` — runs `rr record binary [args]` as a plain subprocess and waits for it to finish.
- `start_replay_session` — spawns `rr replay [trace_dir] -- --interpreter=mi2 --quiet` and returns a normal `GdbSession`.

### Why rr replay works as a GdbSession

`rr replay` sets up an rr-managed GDB remote target and then **exec-replaces itself with gdb**, passing the `--interpreter=mi2` flag via the `--` argument separator:

```
rr replay /path/to/trace -- --interpreter=mi2 --quiet
```

From the subprocess's point of view, once rr has handed off to gdb, the stdin/stdout pipe is talking to a real GDB process in MI2 mode. The `_drain_to_prompt`, `send`, and all session machinery work identically. The `GdbSession.kind` field is set to `"rr-replay"` (vs. `"gdb"`) and is surfaced in `list_sessions` output.

### rr_record implementation

`rr record` is not interactive — it runs the program to completion and exits. It is implemented as a one-shot `asyncio.create_subprocess_exec` call with `communicate()`, not as a `GdbSession`. Key points:

- `stderr=asyncio.subprocess.STDOUT` — merges rr's own messages (including the trace path) and the program's stdout/stderr into one stream.
- `stdin=asyncio.subprocess.DEVNULL` — programs under recording cannot read from stdin (they would block forever since MCP has no way to inject input).
- The trace directory is extracted from rr's output with:
  ```python
  re.compile(r"Saving execution to trace directory `([^`]+)`")
  ```
  rr writes this to stderr: `rr: Saving execution to trace directory `/home/user/.local/share/rr/binary-0`.`
- Returns `{exit_code, trace_dir, output}`. `trace_dir` is `None` if the regex didn't match (rr not found, or unexpected output format).
- `FileNotFoundError` from `create_subprocess_exec` is caught and re-raised as `GdbError("rr not found in PATH")`.

### Reverse-execution in replay sessions

rr replay sessions support GDB's reverse-execution commands. These are sent via `exec_command` since they're not common enough to warrant dedicated tools:

| Command | Description |
|---|---|
| `reverse-continue` | Run backwards until a breakpoint or watchpoint |
| `reverse-step` | Step backwards one source line (enters calls) |
| `reverse-next` | Step backwards one source line (skips calls) |
| `reverse-finish` | Run backwards to where the current function was called |

These commands produce `^running` / `*stopped` output, so the `waiting_for_stop` mechanism in `_collect()` handles them correctly without any changes.

---

## Dependencies

| Package | Purpose |
|---|---|
| `mcp[cli]>=1.0` | FastMCP server, MCP stdio transport, `mcp run` CLI |
| `anyio` | Async runtime adapter (transitive dep of mcp) |
| `pydantic` | Schema generation for tool parameters (transitive dep of mcp) |

Everything else in the lockfile is transitive. `mcp` pulls in `httpx`, `starlette`, `uvicorn`, etc. for its HTTP/SSE transport — these are unused in stdio mode but installed regardless.

---

## Running and configuration

```bash
# Development
uv run python server.py

# Via MCP CLI
uv run mcp run server.py

# MCP client config (Claude Desktop, Claude Code, etc.)
# command: /path/to/mcp-gdb/.venv/bin/python
# args: ["/path/to/mcp-gdb/server.py"]
```

The server communicates over stdio and has no command-line arguments. GDB must be on `$PATH`. The server inherits the environment from its parent process.
