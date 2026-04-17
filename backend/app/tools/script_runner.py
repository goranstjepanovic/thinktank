"""
Sandboxed Python script executor (F16 — Python Script Runner).

Models invoke `run_python` as a tool call during pipeline analysis.
This module executes the script in a restricted subprocess and returns
stdout/stderr/exit_code to the caller for inclusion in the audit trail.

Security constraints enforced here:
  - AST pre-check blocks imports of dangerous modules
  - Subprocess runs with cwd=tmpdir (isolated working directory)
  - asyncio.wait_for enforces the timeout; process is killed on expiry
  - stdout + stderr capped at max_output_bytes before returning
"""

import ast
import os
import sys
import tempfile
import time
from dataclasses import dataclass

# Modules that must not be importable inside scripts.
# The intent is to block network, shell-escape, and broad filesystem access.
_BLOCKED_MODULES = frozenset({
    "subprocess",
    "socket",
    "requests",
    "urllib",
    "urllib3",
    "httpx",
    "aiohttp",
    "ftplib",
    "smtplib",
    "imaplib",
    "telnetlib",
    "xmlrpc",
    "ctypes",
    "importlib",
    "multiprocessing",
    "concurrent",
    "threading",
    "signal",
    "gc",
    "resource",
})

# os is special: we allow it but block the dangerous sub-attributes at AST level
_BLOCKED_OS_ATTRS = frozenset({"system", "popen", "execv", "execve", "execl", "execle", "execlp",
                                "execvp", "execvpe", "spawn", "spawnl", "spawnle", "spawnlp",
                                "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe",
                                "fork", "forkpty"})


@dataclass
class ScriptResult:
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int
    timed_out: bool = False


def _ast_check(script: str) -> str | None:
    """
    Return an error string if the script uses a blocked import or dangerous pattern.
    Returns None if the script passes inspection.
    """
    try:
        tree = ast.parse(script)
    except SyntaxError as e:
        return f"SyntaxError: {e}"

    for node in ast.walk(tree):
        # Block top-level and from-imports of restricted modules
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in _BLOCKED_MODULES:
                    return f"Import of '{alias.name}' is not allowed in script runner"
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                top = node.module.split(".")[0]
                if top in _BLOCKED_MODULES:
                    return f"Import of '{node.module}' is not allowed in script runner"

        # Block os.<dangerous_method>() calls
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id == "os":
                if node.attr in _BLOCKED_OS_ATTRS:
                    return f"Call to 'os.{node.attr}' is not allowed in script runner"

    return None


async def run_script(
    script: str,
    timeout_seconds: int = 30,
    max_output_bytes: int = 65536,
) -> ScriptResult:
    """
    Execute `script` in a sandboxed subprocess and return stdout/stderr/exit_code.

    The script runs in a temporary directory that is cleaned up after execution.
    No network access: socket/requests/httpx/etc. are blocked by AST pre-check.
    stdout and stderr are each capped at `max_output_bytes`.
    """
    import asyncio

    # Pre-flight AST safety check
    err = _ast_check(script)
    if err:
        return ScriptResult(stdout="", stderr=err, exit_code=1, duration_ms=0)

    with tempfile.TemporaryDirectory() as tmpdir:
        script_path = os.path.join(tmpdir, "script.py")
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(script)

        start = time.monotonic()
        timed_out = False
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                script_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=tmpdir,
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
            return ScriptResult(
                stdout="",
                stderr=f"Execution error: {exc}",
                exit_code=1,
                duration_ms=duration_ms,
            )

        duration_ms = int((time.monotonic() - start) * 1000)
        stdout = stdout_bytes[:max_output_bytes].decode("utf-8", errors="replace")
        stderr = stderr_bytes[:max_output_bytes].decode("utf-8", errors="replace")
        if timed_out:
            stderr = f"[TIMEOUT after {timeout_seconds}s]\n" + stderr

        return ScriptResult(
            stdout=stdout,
            stderr=stderr,
            exit_code=proc.returncode if proc.returncode is not None else -1,
            duration_ms=duration_ms,
            timed_out=timed_out,
        )
