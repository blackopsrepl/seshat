from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


COMMAND_NAME = "seshat"
COMMAND_ENV = "SESHAT_COMMAND"


DEFAULT_TIMEOUT = 5.0
TEXT_LIMIT = 10_000


class ToolError(Exception):
    """Expected tool failure reported as MCP tool content."""


@dataclass
class CommandResult:
    stdout: bytes
    stderr: bytes
    returncode: int

    @property
    def text(self) -> str:
        return self.stdout.decode("utf-8", errors="replace")


def eprint(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def run_command(
    args: list[str],
    timeout: float = DEFAULT_TIMEOUT,
    input_text: str | None = None,
    input_bytes: bytes | None = None,
    binary: bool = False,
    capture: bool = True,
) -> CommandResult:
    try:
        completed = subprocess.run(
            args,
            input=input_bytes if input_bytes is not None else input_text,
            text=(input_bytes is None and not binary),
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.PIPE if capture else subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ToolError(f"required command not found: {args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"command timed out after {timeout:.1f}s: {args[0]}") from exc

    stdout = completed.stdout if completed.stdout is not None else b""
    stderr = completed.stderr if completed.stderr is not None else b""
    if isinstance(stdout, str):
        stdout = stdout.encode("utf-8")
    if isinstance(stderr, str):
        stderr = stderr.encode("utf-8")

    if completed.returncode != 0:
        tail = stderr.decode("utf-8", errors="replace")[-500:].strip()
        detail = f": {tail}" if tail else ""
        raise ToolError(f"command failed ({completed.returncode}): {args[0]}{detail}")

    return CommandResult(stdout=stdout, stderr=stderr, returncode=completed.returncode)


def command_available(name: str) -> bool:
    return shutil.which(name) is not None


def require_binaries(names: list[str]) -> None:
    missing = [name for name in names if not command_available(name)]
    if missing:
        raise ToolError(f"missing required command(s): {', '.join(missing)}")


def command_argv() -> list[str]:
    configured = os.environ.get(COMMAND_ENV)
    if configured:
        parts = shlex.split(configured)
        if not parts:
            raise ToolError(f"{COMMAND_ENV} is set but empty")
        return parts

    invoked = Path(sys.argv[0])
    if invoked.name == COMMAND_NAME:
        if invoked.exists():
            return [str(invoked.resolve())]
        found = shutil.which(sys.argv[0])
        if found:
            return [found]

    installed = shutil.which(COMMAND_NAME)
    if installed:
        return [installed]

    return [sys.executable, "-m", "seshat"]


def read_json_command(args: list[str], timeout: float = DEFAULT_TIMEOUT) -> Any:
    return json.loads(run_command(args, timeout=timeout).text)


def strict_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolError(f"{name} must be an integer")
    return value


def strict_number(
    value: Any,
    name: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolError(f"{name} must be a number")
    number = float(value)
    if minimum is not None and number < minimum:
        raise ToolError(f"{name} must be at least {minimum:g}")
    if maximum is not None and number > maximum:
        raise ToolError(f"{name} must be at most {maximum:g}")
    return number


def json_text(value: Any) -> list[dict[str, str]]:
    return [{"type": "text", "text": json.dumps(value, indent=2, sort_keys=True)}]
