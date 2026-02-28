"""GDB session management for mcp-gdb.

Each GdbSession owns one GDB subprocess launched with --interpreter=mi2.
Commands are sent as plain GDB CLI text; responses are read until the
(gdb) prompt and converted from MI2 framing into clean human-readable text
before being returned to callers.

MI2 output record types handled:
  ~"..."    console stream  → included in output (GDB's own messages)
  @"..."    target stream   → included in output (inferior stdout/stderr)
  &"..."    log stream      → skipped (command echo)
  ^done     result record   → terminates read loop (no output)
  ^running  result record   → terminates read loop (no output)
  ^exit     result record   → terminates read loop (no output)
  ^error,msg="..."          → formatted as "Error: ..."
  *stopped,...              → formatted as "[Stopped: reason, func, file:line]"
  =...      notify record   → skipped
  *running  async record    → skipped
"""

import asyncio
import re
import signal
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Optional


# Matches the GDB/MI prompt line: "(gdb)" or "(gdb) "
_PROMPT_RE = re.compile(r"^\(gdb\)\s*$")

# Matches MI stream records:  ~"content"  @"content"  &"content"
_STREAM_RE = re.compile(r'^([~@&])"(.*)"$')

# Default idle timeout before a session is reaped by the cleanup task
IDLE_TIMEOUT = 600.0  # seconds


class GdbError(Exception):
    pass


def _unescape(s: str) -> str:
    """Unescape GDB MI C-style string escapes (content between outer quotes)."""
    result: list[str] = []
    i = 0
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s):
            c = s[i + 1]
            result.append({"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"'}.get(c, c))
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
            lineno_m = re.search(r',line="([^"]+)"', line)
            info: list[str] = []
            if reason_m: info.append(reason_m.group(1))
            if func_m:   info.append(f"in {func_m.group(1)}")
            if file_m and lineno_m:
                info.append(f"at {file_m.group(1)}:{lineno_m.group(1)}")
            parts.append(f"[Stopped: {', '.join(info)}]\n" if info else f"[{line}]\n")
        # ^done, ^running, ^exit, =..., *running → no content, skip
    return "".join(parts)


@dataclass
class GdbSession:
    id: str
    process: asyncio.subprocess.Process
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    last_used: float = field(default_factory=time.monotonic, repr=False)

    async def send(self, cmd: str, timeout: float = 30.0) -> str:
        """Execute a GDB command and return formatted output.

        Commands on the same session are serialized via an asyncio.Lock so
        concurrent callers never interleave writes or reads.
        """
        async with self._lock:
            self.last_used = time.monotonic()
            self.process.stdin.write((cmd + "\n").encode())
            await self.process.stdin.drain()

            lines: list[str] = []

            async def _collect() -> None:
                while True:
                    raw = await self.process.stdout.readline()
                    if not raw:
                        raise GdbError("GDB process exited unexpectedly")
                    line = raw.decode(errors="replace").rstrip("\n")
                    if _PROMPT_RE.match(line):
                        return
                    lines.append(line)

            try:
                await asyncio.wait_for(_collect(), timeout=timeout)
            except asyncio.TimeoutError:
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

    def start_cleanup(self) -> None:
        """Start a background task that reaps sessions idle longer than IDLE_TIMEOUT."""
        asyncio.create_task(self._cleanup_loop())

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            now = time.monotonic()
            stale = [
                sid for sid, s in list(self._sessions.items())
                if now - s.last_used > IDLE_TIMEOUT
            ]
            for sid in stale:
                await self.remove(sid)

    async def create(
        self,
        binary: Optional[str] = None,
        args: Optional[list[str]] = None,
        cwd: Optional[str] = None,
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

        session = GdbSession(id=uuid.uuid4().hex[:8], process=process)

        # Harden the session: disable interactive prompts that would block the reader
        for setup_cmd in (
            "set pagination off",
            "set confirm off",
            "set breakpoint pending on",
        ):
            await session.send(setup_cmd)

        if binary and args:
            await session.send("set args " + " ".join(args))

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

    def list_all(self) -> list[dict]:
        now = time.monotonic()
        return [
            {
                "id": s.id,
                "alive": s.process.returncode is None,
                "idle_seconds": int(now - s.last_used),
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
