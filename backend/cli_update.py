"""Keep the Claude Code CLI current for Ducky-only users.

Users never run `claude update` or the install script. This plugin:
- installs the CLI if missing
- runs `claude update` when the plugin Store-updates
- heals a "CLI too old for this model" launch error and retries
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

_INSTALL_PS = "irm https://claude.ai/install.ps1 | iex"
_VERSION_RE = re.compile(r"(\d+(?:\.\d+)+)")
_REQUIRED_RE = re.compile(
    r"does not support this model;\s*version\s+(\d+(?:\.\d+)+)\s+or newer",
    re.I,
)
_update_lock = threading.Lock()
_busy = False
_last: dict[str, Any] = {}


def plugin_package_version() -> str:
    path = Path(__file__).resolve().parent.parent / "plugin.json"
    try:
        return str(json.loads(path.read_text(encoding="utf-8")).get("version") or "")
    except (OSError, json.JSONDecodeError, TypeError):
        return ""


def parse_version_tuple(text: str) -> tuple[int, ...] | None:
    m = _VERSION_RE.search(text or "")
    if not m:
        return None
    try:
        return tuple(int(p) for p in m.group(1).split("."))
    except ValueError:
        return None


def version_less(current: tuple[int, ...], required: tuple[int, ...]) -> bool:
    n = max(len(current), len(required))
    a = current + (0,) * (n - len(current))
    b = required + (0,) * (n - len(required))
    return a < b


def parse_required_cli_version(error: str) -> tuple[int, ...] | None:
    m = _REQUIRED_RE.search(error or "")
    return parse_version_tuple(m.group(1)) if m else None


def is_cli_too_old_error(*chunks: str) -> bool:
    text = "\n".join(c or "" for c in chunks)
    if parse_required_cli_version(text):
        return True
    low = text.lower()
    return "does not support this model" in low and "claude update" in low


def stamp_path() -> Path:
    from frontend.settings import default_app_data_dir

    return default_app_data_dir() / "coding_agents" / "claude_cli.json"


def read_stamp() -> dict[str, Any]:
    path = stamp_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_stamp(data: dict[str, Any]) -> None:
    path = stamp_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def status_text() -> str:
    if _busy:
        return "Updating Claude Code CLI…"
    last = _last.get("message") or ""
    return str(last)


def _env() -> dict[str, str]:
    try:
        from frontend.ui_web.terminal.path_env import env_with_fresh_path, refresh_process_path

        refresh_process_path()
        return env_with_fresh_path()
    except Exception:
        return dict(os.environ)


def _run(argv: list[str], *, timeout_s: float) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, Any] = {
        "capture_output": True,
        "text": True,
        "timeout": timeout_s,
        "encoding": "utf-8",
        "errors": "replace",
        "env": _env(),
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(argv, **kwargs)


def read_cli_version(binary: str) -> str:
    if not (binary or "").strip():
        return ""
    try:
        proc = _run([binary, "--version"], timeout_s=20)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    text = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    tup = parse_version_tuple(text)
    return ".".join(str(p) for p in tup) if tup else ""


def _install_cli() -> dict[str, Any]:
    if os.name != "nt":
        try:
            proc = _run(["bash", "-lc", "curl -fsSL https://claude.ai/install.sh | bash"], timeout_s=300)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "error": str(exc)}
        if proc.returncode != 0:
            return {"ok": False, "error": (proc.stderr or proc.stdout or "install failed").strip()}
        return {"ok": True}
    ps = (
        "Set-ExecutionPolicy -Scope Process Bypass -Force; "
        f"{_INSTALL_PS}"
    )
    try:
        proc = _run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            timeout_s=300,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": str(exc)}
    if proc.returncode != 0:
        return {"ok": False, "error": (proc.stderr or proc.stdout or "install failed").strip()}
    return {"ok": True}


def update_claude_cli(binary: str = "") -> dict[str, Any]:
    """Install if missing, otherwise `claude update`. Locked; safe to call twice."""
    global _busy, _last
    with _update_lock:
        _busy = True
        try:
            from .claude_auth import resolve_claude_bin

            before_bin = resolve_claude_bin(binary)
            before = read_cli_version(before_bin) if before_bin else ""
            if before_bin:
                try:
                    proc = _run([before_bin, "update"], timeout_s=300)
                    out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
                    if proc.returncode != 0 and "unknown" in out.lower():
                        installed = _install_cli()
                        if not installed.get("ok"):
                            _last = installed
                            return installed
                    elif proc.returncode != 0:
                        installed = _install_cli()
                        if not installed.get("ok"):
                            err = {"ok": False, "error": out or installed.get("error") or "update failed"}
                            _last = err
                            return err
                except (OSError, subprocess.TimeoutExpired):
                    installed = _install_cli()
                    if not installed.get("ok"):
                        _last = installed
                        return installed
            else:
                installed = _install_cli()
                if not installed.get("ok"):
                    _last = installed
                    return installed

            after_bin = resolve_claude_bin(binary) or before_bin
            after = read_cli_version(after_bin) if after_bin else ""
            result = {
                "ok": bool(after_bin),
                "cli_path": after_bin,
                "version_before": before,
                "version_after": after,
                "message": (
                    f"Claude Code CLI {after}" if after else "Claude Code CLI updated"
                ),
                "error": "" if after_bin else "Claude Code CLI still not found after install/update",
            }
            if result["ok"]:
                write_stamp(
                    {
                        "plugin_version": plugin_package_version(),
                        "cli_version": after,
                        "updated_at": time.time(),
                        "ok": True,
                    }
                )
            _last = result
            return result
        finally:
            _busy = False


def needs_plugin_load_update() -> bool:
    """True when CLI is missing or this plugin version has not updated it yet."""
    from .claude_auth import resolve_claude_bin

    if not resolve_claude_bin(""):
        return True
    stamp = read_stamp()
    if not stamp.get("ok"):
        return True
    return str(stamp.get("plugin_version") or "") != plugin_package_version()


def schedule_cli_update_on_plugin_load() -> None:
    """Background install/update after Store install/update or app start."""
    try:
        if not needs_plugin_load_update():
            return
    except Exception:
        return

    def _worker() -> None:
        try:
            update_claude_cli("")
        except Exception:
            pass

    threading.Thread(target=_worker, name="claude-cli-update", daemon=True).start()
