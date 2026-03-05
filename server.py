"""MCP server that exposes GDB debugging as tools.

Run directly:  python server.py
Or via MCP CLI: mcp run server.py

Each tool takes a session_id (returned by start_session) plus command-specific
parameters.  Use exec_command for anything not covered by the named tools.
"""

import asyncio
import re
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP

from gdb import GdbError, GdbManager

# Matches: rr: Saving execution to trace directory `/path/to/trace'.
_RR_TRACE_RE = re.compile(r"Saving execution to trace directory `([^']+)'")

manager = GdbManager()


@asynccontextmanager
async def _lifespan(app):
    manager.start_cleanup()
    yield
    await manager.close_all()


mcp = FastMCP("mcp-gdb", lifespan=_lifespan)


# ── Session management ────────────────────────────────────────────────────────

@mcp.tool()
async def start_session(
    binary: str | None = None,
    args: list[str] | None = None,
    cwd: str | None = None,
) -> dict:
    """Start a new GDB session. Returns the session_id used by all other tools.

    binary: path to the executable to debug (can also be loaded later with
            exec_command("file /path/to/binary"))
    args:   command-line arguments passed to the inferior on run
    cwd:    working directory for GDB (defaults to current directory)
    """
    s = await manager.create(binary=binary, args=args, cwd=cwd)
    return {"session_id": s.id, "binary": binary}


@mcp.tool()
async def stop_session(session_id: str) -> dict:
    """Stop and clean up a GDB session (kills the GDB process)."""
    removed = await manager.remove(session_id)
    return {"stopped": removed}


@mcp.tool()
async def list_sessions() -> list:
    """List all active GDB sessions with their idle time and alive status."""
    return manager.list_all()


# ── rr record / replay ────────────────────────────────────────────────────────

@mcp.tool()
async def rr_record(
    binary: str,
    args: list[str] | None = None,
    cwd: str | None = None,
    timeout: float = 300.0,
) -> dict:
    """Record a program execution with rr for later time-travel replay debugging.

    Runs the program under rr's recorder and waits for it to finish.  Returns
    the trace directory path (trace_dir) which can be passed directly to
    start_replay_session.  Crashes and non-zero exit codes are normal and do
    not indicate failure — rr records the crash as part of the trace.
    Note: the program's stdin is closed during recording.

    binary:  path to the executable to record
    args:    command-line arguments for the binary
    cwd:     working directory (defaults to current directory)
    timeout: maximum seconds to wait for the recording to finish
    """
    cmd = ["rr", "record", binary]
    if args:
        cmd.extend(args)

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,   # capture rr's "Saving to..." message
            cwd=cwd,
        )
        try:
            stdout_bytes, _ = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise GdbError(f"rr record timed out after {timeout}s")
    except FileNotFoundError:
        raise GdbError("rr not found in PATH")

    output = stdout_bytes.decode(errors="replace")
    m = _RR_TRACE_RE.search(output)
    trace_dir = m.group(1) if m else None

    return {
        "trace_dir": trace_dir,
        "output": output,
    }


@mcp.tool()
async def start_replay_session(
    trace_dir: str | None = None,
    cwd: str | None = None,
) -> dict:
    """Start an rr replay session for time-travel debugging.

    Launches rr replay as a GDB session.  The returned session_id works with
    all standard tools (breakpoint, run, continue_exec, backtrace, print, …).
    In addition, rr enables reverse-execution commands you can send via
    exec_command:
      reverse-continue   — run backwards until a breakpoint or watchpoint
      reverse-step       — step backwards one source line (into calls)
      reverse-next       — step backwards one source line (over calls)
      reverse-finish     — run backwards until the current function was called

    trace_dir: trace directory returned by rr_record; omit to replay the most
               recently recorded trace in ~/.local/share/rr/
    cwd:       working directory for rr (defaults to current directory)
    """
    s = await manager.create_replay(trace_dir=trace_dir, cwd=cwd)
    return {"session_id": s.id, "trace_dir": trace_dir}


# ── Universal fallback ────────────────────────────────────────────────────────

@mcp.tool()
async def exec_command(
    session_id: str,
    command: str,
    timeout: float = 30.0,
) -> str:
    """Execute any GDB command and return its output.

    Use this for commands not covered by the other tools, for example:
      info breakpoints
      watch expr, rwatch expr
      set var x = 5
      thread apply all bt
      catch syscall, catch throw
      source /path/to/script.gdb
      attach PID
      core-file /path/to/core

    IMPORTANT — execution commands: any command that resumes the inferior
    (run, continue, step, next, finish, until, advance, jump, signal, return)
    will BLOCK until the inferior stops again, exactly like the named tools do.
    The return value will include the stop reason, e.g.:
      [Stopped: breakpoint-hit, in main, at foo.c:10]
    While blocked, the session can be interrupted with the interrupt tool.
    """
    return await manager.get(session_id).send(command, timeout=timeout)


@mcp.tool()
async def batch_commands(
    session_id: str,
    commands: list[str],
    timeout: float = 15.0,
    stop_on_error: bool = False,
) -> list[dict]:
    """Execute a list of GDB commands sequentially and return each output.

    Useful to avoid multiple round-trips, e.g.:
      ["file /bin/ls", "break main", "run -la"]
    stop_on_error: halt the batch on the first GdbError (default: continue).
    """
    s = manager.get(session_id)
    results: list[dict] = []
    for cmd in commands:
        try:
            out = await s.send(cmd, timeout=timeout)
            # send() only raises GdbError for infrastructure failures (timeout,
            # process exit).  GDB-level errors (^error records) are returned as
            # "Error: ..." strings, so we detect them here for stop_on_error.
            is_error = out.startswith("Error: ")
            results.append({"command": cmd, "output": out, "error": is_error})
            if is_error and stop_on_error:
                break
        except GdbError as e:
            results.append({"command": cmd, "output": str(e), "error": True})
            if stop_on_error:
                break
    return results


# ── Execution control ─────────────────────────────────────────────────────────

@mcp.tool()
async def run(
    session_id: str,
    args: str | None = None,
    timeout: float = 30.0,
) -> str:
    """Run (or re-run) the inferior program (GDB 'run' / 'r').

    Waits until the program stops (breakpoint, signal, or exit) or the
    timeout expires.  Pass args to override the arguments set at start_session.
    """
    cmd = f"run {args}" if args else "run"
    return await manager.get(session_id).send(cmd, timeout=timeout)


@mcp.tool()
async def continue_exec(
    session_id: str,
    timeout: float = 30.0,
) -> str:
    """Continue execution after a breakpoint or interrupt (GDB 'continue' / 'c').

    Waits until the program stops again or the timeout expires.
    """
    return await manager.get(session_id).send("continue", timeout=timeout)


@mcp.tool()
async def step(
    session_id: str,
    count: int = 1,
    instruction: bool = False,
) -> str:
    """Step into the next source line or machine instruction (GDB 's' / 'stepi').

    count:       number of steps to take
    instruction: if True, use stepi — step one machine instruction instead of
                 one source line (useful when debugging without source)
    """
    cmd = f"{'stepi' if instruction else 'step'} {count}"
    return await manager.get(session_id).send(cmd)


@mcp.tool(name="next")
async def next_line(
    session_id: str,
    count: int = 1,
    instruction: bool = False,
) -> str:
    """Step over the next source line or machine instruction (GDB 'next' / 'nexti' / 'n').

    Unlike step, next does not enter called functions.
    count:       number of steps to take
    instruction: if True, use nexti — step over one machine instruction
    """
    cmd = f"{'nexti' if instruction else 'next'} {count}"
    return await manager.get(session_id).send(cmd)


@mcp.tool()
async def finish(
    session_id: str,
    timeout: float = 30.0,
) -> str:
    """Run until the current function returns, then print the return value (GDB 'finish')."""
    return await manager.get(session_id).send("finish", timeout=timeout)


@mcp.tool()
async def until(
    session_id: str,
    location: str,
    timeout: float = 30.0,
) -> str:
    """Run until a source location is reached (GDB 'until').

    Useful for skipping over loops or blocks of code without setting and
    deleting a temporary breakpoint.  Blocks until the inferior stops.

    location: file:line, function name, or *address
              e.g. "foo.c:42", "cleanup", "*0x401234"
    """
    return await manager.get(session_id).send(f"until {location}", timeout=timeout)


@mcp.tool()
async def interrupt(session_id: str) -> dict:
    """Send SIGINT to interrupt a running inferior.

    Use this when run or continue_exec is blocking because the program has not
    stopped yet.  The blocked call will then return with the stop output.
    """
    manager.get(session_id).interrupt()
    return {"status": "SIGINT sent"}


# ── Breakpoints ───────────────────────────────────────────────────────────────

@mcp.tool(name="breakpoint")
async def set_breakpoint(
    session_id: str,
    location: str,
    condition: str | None = None,
    temporary: bool = False,
) -> str:
    """Set a breakpoint (GDB 'break' / 'tbreak' / 'br').

    location:  function name, file:line, or *address
               e.g. "main", "foo.c:42", "*0x401234", "MyClass::method"
    condition: optional GDB expression that must be true to trigger
               e.g. "x > 5", "strcmp(name, \"alice\") == 0"
    temporary: if True, the breakpoint auto-deletes after its first hit
    """
    s = manager.get(session_id)
    cmd = f"{'tbreak' if temporary else 'break'} {location}"
    output = await s.send(cmd)
    if condition:
        # Case-insensitive: GDB prints "Breakpoint N" for break and
        # "Temporary breakpoint N" (lowercase b) for tbreak.
        bp_m = re.search(r"(?i)breakpoint (\d+)", output)
        if bp_m:
            cond_out = await s.send(f"condition {bp_m.group(1)} {condition}")
            if cond_out.strip():
                output += cond_out
    return output


@mcp.tool()
async def delete_breakpoints(
    session_id: str,
    number: int | None = None,
) -> str:
    """Delete one breakpoint or all breakpoints (GDB 'delete').

    number: breakpoint number to delete; omit to delete all breakpoints.
    """
    cmd = f"delete {number}" if number is not None else "delete"
    return await manager.get(session_id).send(cmd)


@mcp.tool()
async def watch(
    session_id: str,
    expression: str,
    mode: str = "write",
) -> str:
    """Set a watchpoint that stops execution when an expression changes (GDB 'watch').

    Watchpoints detect *when* data changes, not *where* execution reaches.
    Useful for tracking memory corruption, unexpected variable mutations, etc.

    expression: any GDB expression — variable, memory location, dereferenced pointer
                e.g. "x", "buf[4]", "*0x601020", "obj->field"
    mode:       "write"  — stop when expression is written (default, GDB 'watch')
                "read"   — stop when expression is read (GDB 'rwatch')
                "access" — stop on any read or write (GDB 'awatch')
    """
    cmds = {"write": "watch", "read": "rwatch", "access": "awatch"}
    cmd = cmds.get(mode, "watch")
    return await manager.get(session_id).send(f"{cmd} {expression}")


# ── Threads ───────────────────────────────────────────────────────────────────

@mcp.tool()
async def list_threads(session_id: str) -> str:
    """List all threads in the inferior with their current location (GDB 'info threads')."""
    return await manager.get(session_id).send("info threads")


@mcp.tool()
async def select_thread(session_id: str, thread_id: int) -> str:
    """Switch to a specific thread (GDB 'thread N').

    Use list_threads to see thread IDs.  After switching, stack and local
    variable commands operate on the selected thread.
    """
    return await manager.get(session_id).send(f"thread {thread_id}")


# ── Stack frame navigation ────────────────────────────────────────────────────

@mcp.tool()
async def backtrace(
    session_id: str,
    limit: int | None = None,
) -> str:
    """Show the call stack (GDB 'backtrace' / 'bt').

    limit: maximum number of frames to show (omit for full stack).
    """
    cmd = f"backtrace {limit}" if limit is not None else "backtrace"
    return await manager.get(session_id).send(cmd)


@mcp.tool()
async def select_frame(session_id: str, frame: int) -> str:
    """Select a stack frame by number (GDB 'frame N').

    Frame 0 is the innermost (current) frame; use backtrace to see frame numbers.
    After selecting a frame, inspection commands (print, info locals, list_source)
    operate in that frame's context.
    """
    return await manager.get(session_id).send(f"frame {frame}")


@mcp.tool()
async def up(session_id: str, count: int = 1) -> str:
    """Move up the call stack toward the caller (GDB 'up').

    count: number of frames to move up (default 1).
    """
    return await manager.get(session_id).send(f"up {count}")


@mcp.tool()
async def down(session_id: str, count: int = 1) -> str:
    """Move down the call stack toward the innermost frame (GDB 'down').

    count: number of frames to move down (default 1).
    """
    return await manager.get(session_id).send(f"down {count}")


# ── Inspection ────────────────────────────────────────────────────────────────

@mcp.tool()
async def context(session_id: str) -> str:
    """Return a full snapshot of the current debugging context.

    Combines the most commonly needed post-stop information into one call:
      - Current frame: function, file, and line number
      - Function arguments
      - Local variables
      - Source listing around the current line

    Call this immediately after any stop event (breakpoint hit, step, interrupt)
    to orient yourself before deciding on the next action.

    Note: this resets GDB's internal source-listing position to the current PC,
    which cancels any forward scrolling done by previous list_source calls.
    """
    s = manager.get(session_id)
    parts: list[str] = []
    # NOTE: "frame" must come before "list". GDB's "list" without arguments
    # scrolls forward on each call, but "frame" resets the internal list
    # pointer to the current PC.  Reordering these would cause "list" to
    # show the wrong lines on repeated context() calls.
    for cmd, label in [
        ("frame",        "Frame"),
        ("info args",    "Arguments"),
        ("info locals",  "Locals"),
        ("list",         "Source"),
    ]:
        out = await s.send(cmd)
        if out.strip():
            parts.append(f"=== {label} ===\n{out}")
    return "\n".join(parts)


@mcp.tool()
async def list_variables(
    session_id: str,
    scope: str = "locals",
) -> str:
    """Show variables in the current stack frame (GDB 'info locals' / 'info args').

    scope: "locals" — local variables only (default)
           "args"   — function arguments only
           "all"    — both locals and arguments
    """
    s = manager.get(session_id)
    if scope == "args":
        return await s.send("info args")
    if scope == "all":
        args_out = await s.send("info args")
        locals_out = await s.send("info locals")
        return f"=== Arguments ===\n{args_out}\n=== Locals ===\n{locals_out}"
    return await s.send("info locals")


@mcp.tool(name="print")
async def print_expr(
    session_id: str,
    expression: str,
    fmt: str | None = None,
) -> str:
    """Print/evaluate a GDB expression (GDB 'print' / 'p').

    expression: any GDB expression — variable, cast, function call, etc.
    fmt:        optional output format: x=hex, d=decimal, o=octal,
                t=binary, f=float, s=string, c=char, a=address
                e.g. fmt="x" → "print /x expression"
    """
    cmd = f"print {'/' + fmt + ' ' if fmt else ''}{expression}"
    return await manager.get(session_id).send(cmd)


@mcp.tool(name="examine")
async def examine_memory(
    session_id: str,
    address: str,
    count: int = 1,
    fmt: str = "x",
    unit: str = "w",
) -> str:
    """Examine memory at an address (GDB 'x').

    address: GDB expression for the target address: "&var", "0x601020", "$rsp"
    count:   number of units to display
    fmt:     x=hex, d=decimal, i=instruction, s=string, c=char, o=octal, t=binary
    unit:    b=byte(1B), h=halfword(2B), w=word(4B), g=giant(8B)

    Example: examine("$rsp", count=8, fmt="x", unit="g")
             → x/8xg $rsp  (8 giant-word hex dump of the stack)
    """
    return await manager.get(session_id).send(f"x/{count}{fmt}{unit} {address}")


@mcp.tool()
async def info_registers(
    session_id: str,
    register: str | None = None,
) -> str:
    """Show CPU register values (GDB 'info registers').

    register: specific register name (e.g. "rax", "eip"), or omit for all
              general-purpose registers.
    """
    cmd = f"info registers {register}" if register else "info registers"
    return await manager.get(session_id).send(cmd)


@mcp.tool()
async def list_source(
    session_id: str,
    location: str | None = None,
) -> str:
    """List source code (GDB 'list' / 'l').

    location: function name, file:line, or omit to list around the current
              position.  Call repeatedly with no location to scroll forward.
    """
    cmd = f"list {location}" if location else "list"
    return await manager.get(session_id).send(cmd)


@mcp.tool()
async def disassemble(
    session_id: str,
    location: str | None = None,
    with_source: bool = False,
) -> str:
    """Disassemble code (GDB 'disassemble').

    location:    function name, *address, or "start,end" address range.
                 Omit to disassemble the current function.
    with_source: if True, interleave C source lines with assembly (/s flag).
    """
    parts = ["disassemble"]
    if with_source:
        parts.append("/s")
    if location:
        parts.append(location)
    return await manager.get(session_id).send(" ".join(parts))


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
