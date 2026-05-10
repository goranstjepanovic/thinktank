"""
Shell command runner — available to Phase 3 agents via the `run_shell` tool.

Unlike the Python script runner, this executes arbitrary shell commands so agents
can do things like `npm install`, `pip install -r requirements.txt`, `dotnet build`,
`pytest`, etc.  There is no sandboxing — the command runs as the server process user
in the project output directory.  This is intentional: implementation agents need
real tool access.  The tool is only exposed when an `allowed_file_dir` is provided
(Phase 3 only), so it is never offered to analysis pipeline models.

Shells tried in order on Windows: pwsh.exe (PowerShell 7), powershell.exe (WPS 5).
On other platforms: bash.

Background processes
--------------------
Long-running commands (dev servers, watchers) are handled via a separate set of tools:
  run_shell_background  — start without blocking, returns a handle
  get_shell_output      — tail the captured output buffer
  stop_shell_process    — kill the process by handle

The module-level `background_process_manager` singleton owns all background processes.
"""

import asyncio
import platform
import re
import shutil
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

_MAX_BG_OUTPUT_LINES = 500


@dataclass
class ShellResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int
    timed_out: bool = False


# ---------------------------------------------------------------------------
# Background process support
# ---------------------------------------------------------------------------

@dataclass
class BackgroundProcess:
    handle: str
    command: str
    working_dir: str
    proc: asyncio.subprocess.Process
    started_at: float = field(default_factory=time.monotonic)
    _lines: list[str] = field(default_factory=list)
    _done: bool = False
    _exit_code: int | None = None
    _reader_task: asyncio.Task | None = None

    def _append(self, line: str) -> None:
        self._lines.append(line)
        if len(self._lines) > _MAX_BG_OUTPUT_LINES:
            self._lines = self._lines[-_MAX_BG_OUTPUT_LINES:]

    def tail(self, n: int = 50) -> list[str]:
        return self._lines[-n:]

    @property
    def is_running(self) -> bool:
        return not self._done

    def mark_done(self, exit_code: int | None) -> None:
        self._done = True
        self._exit_code = exit_code


class BackgroundProcessManager:
    def __init__(self) -> None:
        self._procs: dict[str, BackgroundProcess] = {}

    async def _read_streams(self, bp: BackgroundProcess) -> None:
        async def drain(stream: asyncio.StreamReader | None) -> None:
            if stream is None:
                return
            while True:
                try:
                    line = await stream.readline()
                except Exception:
                    break
                if not line:
                    break
                bp._append(line.decode("utf-8", errors="replace").rstrip("\r\n"))

        await asyncio.gather(drain(bp.proc.stdout), drain(bp.proc.stderr), return_exceptions=True)
        exit_code = await bp.proc.wait()
        bp.mark_done(exit_code)

    async def start(self, command: str, working_dir: str) -> tuple[str, int, str]:
        """Start command in the background. Returns (handle, pid, error). error is '' on success."""
        blocked = _blocked_command_error(command, working_dir)
        if blocked:
            return ("", 0, blocked)

        Path(working_dir).mkdir(parents=True, exist_ok=True)
        argv = _shell_args(command)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=working_dir,
            )
        except Exception as exc:
            return ("", 0, f"Failed to launch: {exc}")

        handle = f"proc_{uuid.uuid4().hex[:8]}"
        bp = BackgroundProcess(handle=handle, command=command, working_dir=working_dir, proc=proc)
        bp._reader_task = asyncio.create_task(self._read_streams(bp))
        self._procs[handle] = bp
        return (handle, proc.pid, "")

    async def get_output(self, handle: str, tail: int = 50) -> dict:
        bp = self._procs.get(handle)
        if bp is None:
            return {"error": f"Unknown handle '{handle}'. Use run_shell_background to start a process first."}
        await asyncio.sleep(0)  # yield so reader task can run
        return {
            "handle": handle,
            "pid": bp.proc.pid,
            "command": bp.command,
            "is_running": bp.is_running,
            "exit_code": bp._exit_code,
            "lines": bp.tail(tail),
            "total_lines_captured": len(bp._lines),
        }

    def _find_by_pid(self, pid: int) -> "BackgroundProcess | None":
        for bp in self._procs.values():
            if bp.proc.pid == pid:
                return bp
        return None

    async def stop(self, handle: str | None = None, pid: int | None = None) -> dict:
        if pid is not None:
            bp = self._find_by_pid(pid)
            if bp is None:
                # Fall back to OS-level kill if we don't track it
                import os, signal
                try:
                    os.kill(pid, signal.SIGTERM)
                    return {"pid": pid, "stopped": True, "message": "Sent SIGTERM (process not tracked by handle)"}
                except Exception as exc:
                    return {"error": f"Kill by PID failed: {exc}"}
        else:
            bp = self._procs.get(handle or "")
            if bp is None:
                return {"error": f"Unknown handle '{handle}'"}

        if bp._done:
            return {"handle": bp.handle, "pid": bp.proc.pid, "stopped": True, "exit_code": bp._exit_code, "message": "Already stopped"}
        try:
            bp.proc.kill()
        except Exception as exc:
            return {"error": f"Kill failed: {exc}"}
        try:
            await asyncio.wait_for(bp.proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass
        bp.mark_done(bp.proc.returncode)
        return {"handle": bp.handle, "pid": bp.proc.pid, "stopped": True, "exit_code": bp._exit_code}

    def cleanup_dir(self, working_dir: str) -> None:
        """Kill all background processes for a given project directory."""
        for bp in list(self._procs.values()):
            if bp.working_dir == working_dir and not bp._done:
                try:
                    bp.proc.kill()
                except Exception:
                    pass


background_process_manager = BackgroundProcessManager()


# ---------------------------------------------------------------------------
# Port-kill helper (cross-platform)
# ---------------------------------------------------------------------------

def kill_port_process(port: int) -> dict:
    """
    Find and kill the process listening on `port`.

    Returns {"killed": bool, "pids": [...], "port": int, "error": str|None}.
    Safe to call even when nothing is listening — returns killed=False with an
    informative message rather than raising.
    """
    import os
    import subprocess
    import signal as _signal

    killed_pids: list[int] = []

    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True, text=True, timeout=10,
            )
        except Exception as exc:
            return {"killed": False, "pids": [], "port": port, "error": f"netstat failed: {exc}"}

        pids: set[str] = set()
        for line in result.stdout.splitlines():
            # Match lines like: TCP  0.0.0.0:3000  ... LISTENING  1234
            if f":{port}" in line and "LISTENING" in line:
                parts = line.split()
                if parts:
                    pids.add(parts[-1])

        if not pids:
            return {"killed": False, "pids": [], "port": port, "error": f"No process listening on port {port}"}

        for pid_str in pids:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/PID", pid_str],
                    capture_output=True, timeout=5,
                )
                killed_pids.append(int(pid_str))
            except Exception:
                pass
    else:
        try:
            result = subprocess.run(
                ["lsof", "-ti", f":{port}"],
                capture_output=True, text=True, timeout=10,
            )
        except Exception as exc:
            return {"killed": False, "pids": [], "port": port, "error": f"lsof failed: {exc}"}

        raw_pids = [p.strip() for p in result.stdout.strip().splitlines() if p.strip().isdigit()]
        if not raw_pids:
            return {"killed": False, "pids": [], "port": port, "error": f"No process listening on port {port}"}

        for pid_str in raw_pids:
            try:
                os.kill(int(pid_str), _signal.SIGTERM)
                killed_pids.append(int(pid_str))
            except Exception:
                pass

    if not killed_pids:
        return {"killed": False, "pids": [], "port": port, "error": "Found PIDs but kill failed — may need elevated privileges"}

    return {"killed": True, "pids": killed_pids, "port": port, "error": None}


# ---------------------------------------------------------------------------
# Shell environment helpers
# ---------------------------------------------------------------------------

def shell_environment_context() -> str:
    """Describe the OS and shell used by run_shell_command for agent prompts."""
    os_name = f"{platform.system()} {platform.release()}".strip()
    if sys.platform == "win32":
        if shutil.which("pwsh") or shutil.which("pwsh.exe"):
            cli = "PowerShell 7 (pwsh)"
        elif shutil.which("powershell") or shutil.which("powershell.exe"):
            cli = "Windows PowerShell 5"
        else:
            cli = "cmd.exe"
        note = (
            "IMPORTANT RULES FOR THIS ENVIRONMENT:\n"
            "- ONE command per run_shell call — chaining with &&, &, ||, or ; is BLOCKED and will error.\n"
            "- Do NOT use activate.bat or Activate.ps1 to activate a virtualenv — it has no effect in a "
            "subprocess. Instead call the venv executables directly: "
            "`venv\\Scripts\\python.exe` and `venv\\Scripts\\pip.exe`.\n"
            "- mkdir -p and touch do not exist; use `New-Item -ItemType Directory -Force -Path <dir>` "
            "and `New-Item -ItemType File -Force -Path <file>` or just write the file with file_edit.\n"
            "- Bare npm commands may search parent directories; use `npm --prefix <dir>` when package.json "
            "is in a subdirectory.\n"
            "- For long-running commands (dev servers, watchers): use run_shell_background instead of run_shell."
        )
    else:
        cli = "bash"
        note = (
            "IMPORTANT RULES FOR THIS ENVIRONMENT:\n"
            "- ONE command per run_shell call — chaining with &&, &, ||, or ; is BLOCKED and will error.\n"
            "- Bare npm commands may search parent directories; use `npm --prefix <dir>` when package.json "
            "is in a subdirectory.\n"
            "- For long-running commands (dev servers, watchers): use run_shell_background instead of run_shell."
        )
    return f"OS: {os_name}; CLI: {cli}.\n{note}"


def _find_parent_package_json(start: Path) -> Path | None:
    """Walk up from start looking for a package.json in an ancestor directory."""
    current = start.parent
    while current != current.parent:
        candidate = current / "package.json"
        if candidate.is_file():
            return candidate
        current = current.parent
    return None


def _is_chained_command(command: str) -> bool:
    """Return True if the command chains multiple sub-commands."""
    cleaned = re.sub(r'"[^"]*"', '""', command)
    cleaned = re.sub(r"'[^']*'", "''", cleaned)
    return bool(re.search(r"(&&|\|\||\s&\s|\s;\s|;\s*$)", cleaned))


# Commands that start persistent servers and never exit — block them from run_shell
# so agents don't hang. They should use run_shell_background instead.
_LONG_RUNNING_PATTERNS = [
    r"^npm(?:\.cmd)?\s+(start|run\s+dev|run\s+start|run\s+serve|run\s+preview)\b",
    r"^yarn\s+(start|dev|serve|preview)\b",
    r"^pnpm\s+(start|dev|serve|preview)\b",
    r"^npx\s+(vite|next|nuxt|gatsby|astro|svelte-kit|remix|expo)\b",
    r"^vite\b",
    r"^next\s+dev\b",
    r"^nuxt\s+dev\b",
    r"^uvicorn\b",
    r"^gunicorn\b",
    r"^flask\s+run\b",
    r"^python\s+.*\bapp\.py\b",
    r"^python\s+.*\bserver\.py\b",
    r"^python\s+.*\bmain\.py\b",
    r"^node\s+.*\bserver\.[cm]?js\b",
    r"^cargo\s+run\b",
    r"^go\s+run\b",
    r"^rails\s+server\b",
    r"^ruby\s+.*\bserver\.rb\b",
]

def _is_long_running(command: str) -> bool:
    stripped = command.strip()
    return any(re.search(pat, stripped, re.IGNORECASE) for pat in _LONG_RUNNING_PATTERNS)


def _blocked_command_error(command: str, working_dir: str) -> str | None:
    """
    Block commands whose tool behavior would escape the generated project root,
    and enforce the one-command-per-call rule.
    """
    cwd = Path(working_dir)
    stripped = command.strip()

    if _is_long_running(stripped):
        return (
            f"Blocked long-running command: '{stripped}'. "
            "run_shell is for commands that exit on their own (installs, builds, tests). "
            "Do NOT run dev servers, watchers, or start commands — they never exit and will block the agent. "
            "If you need to verify the project starts, check the build output instead. "
            "Use run_shell_background if you genuinely need a background process."
        )

    if _is_chained_command(stripped):
        return (
            f"Blocked chained command: '{stripped}'. "
            "You MUST run exactly ONE command per run_shell call — do not use &&, &, ||, or ;. "
            "Split this into separate run_shell calls."
        )

    if sys.platform == "win32" and re.match(r"^make\b", stripped, re.IGNORECASE):
        return (
            "Blocked: `make` is not available on Windows. "
            "Do not use Makefiles or make targets on this platform. "
            "Run the underlying commands directly (e.g. `venv\\Scripts\\pip.exe install -r requirements.txt`) "
            "or use `pyproject.toml` scripts / `package.json` scripts instead."
        )

    if re.match(r"^npm(?:\.cmd)?\b", stripped, re.IGNORECASE):
        has_explicit_prefix = re.search(r"(^|\s)(--prefix|-C)\s+\S+", stripped)
        if not has_explicit_prefix and not (cwd / "package.json").is_file():
            parent_pkg = _find_parent_package_json(cwd)
            if parent_pkg:
                try:
                    import json as _json
                    pkg_data = _json.loads(parent_pkg.read_text(encoding="utf-8", errors="replace"))
                    parent_name = pkg_data.get("name", "(unknown)")
                    parent_note = (
                        f" A package.json was found at {parent_pkg} belonging to a DIFFERENT project "
                        f"('{parent_name}'). Do NOT treat that as the generated project's Node setup — "
                        "it is the host application and is unrelated to the code you generated."
                    )
                except Exception:
                    parent_note = f" A package.json was found at {parent_pkg} — it belongs to the host application, not to the generated project."
            else:
                parent_note = ""
            return (
                f"Blocked bare npm command in {working_dir}: no package.json exists in this directory.{parent_note} "
                "Inspect the project layout with list_files and run npm with an explicit package directory, for example "
                "`npm --prefix frontend run dev`, or create a package.json in the project root if that is intended."
            )
    return None


def _shell_args(command: str) -> list[str]:
    """Build the argv list to run `command` in the available shell."""
    if sys.platform == "win32":
        pwsh = shutil.which("pwsh") or shutil.which("pwsh.exe")
        ps = shutil.which("powershell") or shutil.which("powershell.exe")
        shell = pwsh or ps
        if shell:
            return [shell, "-NoProfile", "-NonInteractive", "-Command", command]
        return ["cmd.exe", "/c", command]
    else:
        bash = shutil.which("bash") or "/bin/bash"
        return [bash, "-c", command]


async def run_shell_command(
    command: str,
    working_dir: str,
    timeout_seconds: int = 120,
    max_output_bytes: int = 65536,
) -> ShellResult:
    """
    Execute `command` in `working_dir` and return stdout/stderr/exit_code.
    Use for short-lived commands only. For servers/watchers use background_process_manager.
    """
    Path(working_dir).mkdir(parents=True, exist_ok=True)
    blocked_error = _blocked_command_error(command, working_dir)
    if blocked_error:
        return ShellResult(stdout="", stderr=blocked_error, exit_code=1, duration_ms=0)

    argv = _shell_args(command)
    start = time.monotonic()
    timed_out = False

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_dir,
        )

        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []

        async def _read_stream(stream: asyncio.StreamReader, chunks: list[bytes]) -> None:
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    break
                chunks.append(chunk)

        stdout_reader = asyncio.create_task(_read_stream(proc.stdout, stdout_chunks))
        stderr_reader = asyncio.create_task(_read_stream(proc.stderr, stderr_chunks))

        try:
            # Wait for the main process to exit (not for pipe EOF).
            # On Windows, npm/node child processes keep pipes open after npm exits,
            # so using proc.communicate() would hang until the full timeout.
            await asyncio.wait_for(proc.wait(), timeout=float(timeout_seconds))
            # Process exited — give a short grace period to drain buffered pipe output.
            try:
                await asyncio.wait_for(
                    asyncio.gather(stdout_reader, stderr_reader, return_exceptions=True),
                    timeout=5.0,
                )
            except asyncio.TimeoutError:
                stdout_reader.cancel()
                stderr_reader.cancel()
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            stdout_reader.cancel()
            stderr_reader.cancel()
            try:
                await asyncio.wait_for(proc.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                pass
            timed_out = True
    except Exception as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        return ShellResult(stdout="", stderr=f"Failed to launch shell: {exc}", exit_code=1, duration_ms=duration_ms)

    duration_ms = int((time.monotonic() - start) * 1000)
    stdout_bytes = b"".join(stdout_chunks)
    stderr_bytes = b"".join(stderr_chunks)
    stdout = stdout_bytes[:max_output_bytes].decode("utf-8", errors="replace")
    stderr = stderr_bytes[:max_output_bytes].decode("utf-8", errors="replace")
    if timed_out:
        stderr = f"[TIMED OUT after {timeout_seconds}s]\n" + stderr

    return ShellResult(
        stdout=stdout,
        stderr=stderr,
        exit_code=proc.returncode if proc.returncode is not None else -1,
        duration_ms=duration_ms,
        timed_out=timed_out,
    )
