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
"""

import asyncio
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ShellResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int
    timed_out: bool = False


def _shell_args(command: str) -> list[str]:
    """Build the argv list to run `command` in the available shell."""
    if sys.platform == "win32":
        # Prefer PowerShell 7 (pwsh), fall back to Windows PowerShell 5
        pwsh = shutil.which("pwsh") or shutil.which("pwsh.exe")
        ps = shutil.which("powershell") or shutil.which("powershell.exe")
        shell = pwsh or ps
        if shell:
            return [shell, "-NoProfile", "-NonInteractive", "-Command", command]
        # Last resort: cmd.exe
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

    The process runs with `working_dir` as its CWD so relative paths in
    commands (e.g. `npm install`) resolve correctly.  The directory is created
    if it does not yet exist.

    stdout and stderr are each capped at `max_output_bytes` before returning.
    On timeout the process is killed; `timed_out=True` is set in the result.
    """
    Path(working_dir).mkdir(parents=True, exist_ok=True)
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
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=float(timeout_seconds)
            )
        except asyncio.TimeoutError:
            proc.kill()
            stdout_bytes, stderr_bytes = await proc.communicate()
            timed_out = True
    except Exception as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        return ShellResult(
            stdout="",
            stderr=f"Failed to launch shell: {exc}",
            exit_code=1,
            duration_ms=duration_ms,
        )

    duration_ms = int((time.monotonic() - start) * 1000)
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
