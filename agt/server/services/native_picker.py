"""Local operating-system directory picker used by the browser console."""

from __future__ import annotations

import asyncio
import ipaddress
import shutil
import sys
from pathlib import Path

_PICKER_TIMEOUT_SECONDS = 300


class NativePickerError(RuntimeError):
    """A native picker failure carrying an HTTP status code."""

    def __init__(self, code: str, message: str, status_code: int = 500) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(message)


def native_picker_allowed_for_host(host: str) -> bool:
    """Native dialogs are only safe when the server itself is loopback-only."""
    value = host.strip("[]")
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return value == "localhost"


def _macos_command() -> list[str]:
    script = (
        'POSIX path of (choose folder with prompt '
        '"Select an AGT project directory")'
    )
    return ["osascript", "-e", script]


def _linux_command() -> list[str]:
    zenity = shutil.which("zenity")
    if zenity:
        return [
            zenity,
            "--file-selection",
            "--directory",
            "--title=Select an AGT project directory",
        ]
    kdialog = shutil.which("kdialog")
    if kdialog:
        return [
            kdialog,
            "--getexistingdirectory",
            str(Path.home()),
            "--title",
            "Select an AGT project directory",
        ]
    raise NativePickerError(
        "native_picker_unavailable",
        "no supported native directory dialog is installed",
        503,
    )


def _windows_command() -> list[str]:
    script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$dialog = New-Object System.Windows.Forms.FolderBrowserDialog; "
        "$dialog.Description = 'Select an AGT project directory'; "
        "if ($dialog.ShowDialog() -eq 'OK') { "
        "Write-Output $dialog.SelectedPath }"
    )
    return [
        "powershell",
        "-NoProfile",
        "-STA",
        "-Command",
        script,
    ]


def _platform_command() -> list[str]:
    if sys.platform == "darwin":
        return _macos_command()
    if sys.platform.startswith("linux"):
        return _linux_command()
    if sys.platform == "win32":
        return _windows_command()
    raise NativePickerError(
        "native_picker_unavailable",
        f"native directory picker is not supported on {sys.platform}",
        503,
    )


def _is_cancel(returncode: int, stderr: str) -> bool:
    return returncode == 1 and (
        not stderr.strip()
        or "cancel" in stderr.lower()
        or "(-128)" in stderr
    )


async def pick_native_directory() -> str | None:
    """Open the local OS directory chooser and return its absolute path."""
    command = _platform_command()
    if shutil.which(command[0]) is None:
        raise NativePickerError(
            "native_picker_unavailable",
            f"directory picker command is unavailable: {command[0]}",
            503,
        )
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=_PICKER_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        process.kill()
        await process.communicate()
        raise NativePickerError(
            "native_picker_timeout",
            "native directory picker timed out",
            504,
        ) from None

    stderr_text = stderr.decode("utf-8", errors="replace").strip()
    if _is_cancel(process.returncode, stderr_text):
        return None
    if process.returncode != 0:
        raise NativePickerError(
            "native_picker_failed",
            stderr_text or "native directory picker failed",
        )
    value = stdout.decode("utf-8", errors="replace").strip()
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise NativePickerError(
            "native_picker_failed",
            "native directory picker returned a relative path",
        )
    return str(path)


__all__ = [
    "NativePickerError",
    "native_picker_allowed_for_host",
    "pick_native_directory",
]
