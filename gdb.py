"""GDB session management for gdb-mcp.

Each GdbSession owns one GDB subprocess launched with --interpreter=mi2.
Commands are sent as plain GDB CLI text; responses are read until the
(gdb) prompt and converted from MI2 framing into clean human-readable text
before being returned to callers.

MI2 output record types handled:
  ~"..."    console stream  → included in output (GDB's own messages)
  @"..."    target stream   → included in output (inferior stdout/stderr)
  &"..."    log stream      → skipped (command echo)
  ^done     result record   → terminates read loop (no output)
  ^running  result record   → sets waiting_for_stop; suppresses intermediate prompt
  ^exit     result record   → terminates read loop (no output)
  ^error,msg="..."          → formatted as "Error: ..."
  *stopped,...              → formatted as "[Stopped: reason, func, file:line]"
                              clears waiting_for_stop; next (gdb) terminates loop
  =...      notify record   → skipped
  *running  async record    → skipped
"""

import asyncio
import re
import shlex
import signal
import uuid
from contextlib import suppress
from dataclasses import dataclass, field


# Matches the GDB/MI prompt line: "(gdb)" or "(gdb) "
_PROMPT_RE = re.compile(r"^\(gdb\)\s*$")

# Matches MI stream records:  ~"content"  @"content"  &"content"
_STREAM_RE = re.compile(r'^([~@&])"(.*)"$')


class GdbError(Exception):
    pass


def _unescape(s: str) -> str:
    """Unescape GDB MI C-style string escapes (content between outer quotes).

    Handles: simple escapes (\\n \\t \\r \\\\ \\" \\a \\b \\f \\v),
             octal sequences (\\NNN), and hex sequences (\\xNN).
    Unknown escapes are passed through with the backslash preserved.
    """
    _SIMPLE = {
        "n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"',
        "a": "\a", "b": "\b", "f": "\f", "v": "\v",
    }
    result: list[str] = []
    i = 0
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s):
            c = s[i + 1]
            if c in _SIMPLE:
                result.append(_SIMPLE[c])
                i += 2
            elif c == "x" and i + 3 < len(s) and all(h in "0123456789abcdefABCDEF" for h in s[i+2:i+4]):
                result.append(chr(int(s[i+2:i+4], 16)))
                i += 4
            elif c in "01234567":
                j = i + 1
                while j < min(i + 4, len(s)) and s[j] in "01234567":
                    j += 1
                result.append(chr(int(s[i+1:j], 8)))
                i = j
            else:
                result.append("\\" + c)  # preserve unknown escapes intact
                i += 2
        else:
            result.append(s[i])
            i += 1
    return "".join(result)


def _format_output(lines: list[str]) -> str:
    """Convert raw MI2 output lines into clean human-readable text."""
    parts: list[str] = []
    for line in lines:
        m = _STREAM_RE.match(line)
        if m:
            kind, content = m.group(1), m.group(2)
            if kind in ("~", "@"):           # console + target output → keep
                parts.append(_unescape(content))
            # & (log / command echo) → skip
        elif line.startswith("^error"):
            msg_m = re.search(r'msg="((?:[^"\\]|\\.)*)"', line)
            msg = _unescape(msg_m.group(1)) if msg_m else line
            parts.append(f"Error: {msg}\n")
        elif line.startswith("*stopped"):
            reason_m = re.search(r'reason="([^"]+)"', line)
            func_m   = re.search(r'func="([^"]+)"', line)
            file_m   = re.search(r'file="([^"]+)"', line)
            lineno_m = re.search(r'\bline="([^"]+)"', line)
            info: list[str] = []
            if reason_m: info.append(reason_m.group(1))
            if func_m:   info.append(f"in {func_m.group(1)}")
            if file_m and lineno_m:
                info.append(f"at {file_m.group(1)}:{lineno_m.group(1)}")
            parts.append(f"[Stopped: {', '.join(info)}]\n" if info else f"[{line}]\n")
        elif line and line[0] not in "^*=":
            # Raw text not matching any MI record prefix — pass through.
            # Catches warnings from stderr or other non-MI-framed output
            # (e.g. libthread_db messages, dynamic linker warnings).
            parts.append(line + "\n")
        # ^done, ^running, ^exit, =..., *running → no content, skip
    return "".join(parts)


@dataclass
class GdbSession:
    id: str
    process: asyncio.subprocess.Process
    kind: str = "gdb"  # "gdb" or "rr-replay"
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _broken: bool = field(default=False, repr=False)

    async def send(self, cmd: str, timeout: float = 30.0) -> str:
        """Execute a GDB command and return formatted output.

        Commands on the same session are serialized via an asyncio.Lock so
        concurrent callers never interleave writes or reads.
        """
        if self._broken:
            raise GdbError(
                "Session is tainted by a previous timeout and cannot be used; "
                "call stop_session and start a new one"
            )
        if "\n" in cmd:
            raise GdbError(
                "Command contains embedded newlines; "
                "use batch_commands to send multiple commands"
            )
        async with self._lock:
            self.process.stdin.write((cmd + "\n").encode())
            await self.process.stdin.drain()

            lines: list[str] = []

            async def _collect() -> None:
                # In MI2 all-stop mode, execution commands (run, continue,
                # step, …) emit (gdb) *twice*: once right after ^running to
                # acknowledge the command, and again after *stopped when the
                # inferior halts.  We must not return on the first (gdb) or
                # the *stopped record and second (gdb) will pollute the next
                # command's response.
                waiting_for_stop = False
                while True:
                    raw = await self.process.stdout.readline()
                    if not raw:
                        raise GdbError("GDB process exited unexpectedly")
                    line = raw.decode(errors="replace").rstrip("\n")
                    if _PROMPT_RE.match(line):
                        if not waiting_for_stop:
                            return
                        # (gdb) between ^running and *stopped — keep reading
                    else:
                        lines.append(line)
                        if line.startswith("^running"):
                            waiting_for_stop = True
                        elif line.startswith("*stopped"):
                            waiting_for_stop = False

            try:
                await asyncio.wait_for(_collect(), timeout=timeout)
            except asyncio.TimeoutError:
                # Attempt recovery: send SIGINT to stop the inferior, then
                # drain stdout to the next (gdb) prompt.  This leaves the
                # pipe clean so the session can be reused.  Only taint the
                # session if recovery itself fails (GDB unresponsive/exited).
                self.interrupt()
                try:
                    await asyncio.wait_for(
                        _drain_to_prompt(self.process.stdout), timeout=5.0
                    )
                except (asyncio.TimeoutError, GdbError):
                    self._broken = True
                raise GdbError(f"Command timed out after {timeout}s: {cmd!r}")

            return _format_output(lines)

    def interrupt(self) -> None:
        """Send SIGINT to interrupt a running inferior.

        This does NOT acquire the session lock, so it can be called while
        a run/continue command is blocking.  The SIGINT causes GDB to stop
        the inferior and emit a *stopped record, which unblocks the pending
        send() call.
        """
        if self.process.returncode is None:
            self.process.send_signal(signal.SIGINT)

    async def close(self) -> None:
        """Gracefully shut down GDB: quit → SIGTERM → SIGKILL."""
        if self.process.returncode is not None:
            return
        with suppress(Exception):
            self.process.stdin.write(b"quit\n")
            await self.process.stdin.drain()
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.process.wait(), timeout=3.0)
        if self.process.returncode is None:
            with suppress(Exception):
                self.process.terminate()
            with suppress(asyncio.TimeoutError, Exception):
                await asyncio.wait_for(self.process.wait(), timeout=2.0)
        if self.process.returncode is None:
            with suppress(Exception):
                self.process.kill()


class GdbManager:
    def __init__(self) -> None:
        self._sessions: dict[str, GdbSession] = {}

    async def create(
        self,
        binary: str | None = None,
        args: list[str] | None = None,
        cwd: str | None = None,
    ) -> GdbSession:
        """Spawn a new GDB process and return a ready, hardened GdbSession."""
        cmd = ["gdb", "--interpreter=mi2", "--quiet"]
        if binary:
            cmd.append(binary)

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,   # merge stderr so MI reader sees all output
            cwd=cwd,
        )

        # Drain the GDB startup banner until the first (gdb) prompt
        try:
            await asyncio.wait_for(_drain_to_prompt(process.stdout), timeout=15.0)
        except asyncio.TimeoutError:
            process.kill()
            raise GdbError("GDB failed to start (no prompt within 15 s)")

        session = GdbSession(id=uuid.uuid4().hex[:8], process=process, kind="gdb")

        # Harden the session: disable interactive prompts that would block the reader
        for setup_cmd in (
            "set pagination off",
            "set confirm off",
            "set breakpoint pending on",
        ):
            await session.send(setup_cmd)

        if binary and args:
            await session.send("set args " + " ".join(shlex.quote(a) for a in args))

        self._sessions[session.id] = session
        return session

    async def create_replay(
        self,
        trace_dir: str | None = None,
        cwd: str | None = None,
    ) -> GdbSession:
        """Start an rr replay session and return a ready GdbSession.

        trace_dir: rr trace directory to replay; omit to replay the latest recording.
        rr execs gdb directly, so the MI2 protocol works identically to a normal session.
        """
        cmd = ["rr", "replay"]
        if trace_dir:
            cmd.append(trace_dir)
        cmd += ["--", "--interpreter=mi2", "--quiet"]

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=cwd,
        )

        try:
            await asyncio.wait_for(_drain_to_prompt(process.stdout), timeout=15.0)
        except asyncio.TimeoutError:
            process.kill()
            raise GdbError("rr replay failed to start (no prompt within 15 s)")

        session = GdbSession(id=uuid.uuid4().hex[:8], process=process, kind="rr-replay")

        for setup_cmd in (
            "set pagination off",
            "set confirm off",
            "set breakpoint pending on",
        ):
            await session.send(setup_cmd)

        self._sessions[session.id] = session
        return session

    def get(self, session_id: str) -> GdbSession:
        s = self._sessions.get(session_id)
        if s is None:
            raise GdbError(f"No session with id {session_id!r}")
        return s

    async def remove(self, session_id: str) -> bool:
        s = self._sessions.pop(session_id, None)
        if s is None:
            return False
        await s.close()
        return True

    async def close_all(self) -> None:
        """Close all active sessions (used on server shutdown)."""
        for sid in list(self._sessions):
            await self.remove(sid)

    def list_all(self) -> list[dict]:
        return [
            {
                "id": s.id,
                "kind": s.kind,
                "alive": s.process.returncode is None,
            }
            for s in self._sessions.values()
        ]


async def _drain_to_prompt(stdout: asyncio.StreamReader) -> None:
    """Read and discard GDB output lines until the first (gdb) prompt."""
    while True:
        raw = await stdout.readline()
        if not raw:
            raise GdbError("GDB exited before showing the initial prompt")
        line = raw.decode(errors="replace").rstrip("\n")
        if _PROMPT_RE.match(line):
            return
