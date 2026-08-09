"""StreamReader — concurrent stdout/stderr consumption for a subprocess.

Reads both streams line-by-line in parallel, invokes a callback per line,
and accumulates the full content for TaskResult.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable


class StreamReader:
    """Consume a subprocess's stdout and stderr concurrently.

    Each line, as soon as it is read, is fed to the corresponding callback
    (typically a terminal printer in Phase 1, a websocket sender in Phase 2).
    The full content is also accumulated for return via ``read_all()``.
    """

    def __init__(
        self,
        stdout: asyncio.StreamReader | None,
        stderr: asyncio.StreamReader | None,
        on_stdout: Callable[[str], None] | None = None,
        on_stderr: Callable[[str], None] | None = None,
    ) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self._on_stdout = on_stdout or (lambda line: print(line, end=""))
        self._on_stderr = on_stderr or (lambda line: print(f"[stderr] {line}", end=""))
        self._stdout_buf: list[str] = []
        self._stderr_buf: list[str] = []

    async def read_all(self) -> tuple[str, str]:
        """Drain both streams to EOF. Returns (stdout_text, stderr_text)."""
        tasks = []
        if self._stdout is not None:
            tasks.append(
                asyncio.create_task(self._drain(self._stdout, self._on_stdout, self._stdout_buf))
            )
        if self._stderr is not None:
            tasks.append(
                asyncio.create_task(self._drain(self._stderr, self._on_stderr, self._stderr_buf))
            )
        if tasks:
            await asyncio.gather(*tasks)
        return "".join(self._stdout_buf), "".join(self._stderr_buf)

    async def _drain(
        self,
        stream: asyncio.StreamReader,
        callback: Callable[[str], None],
        collector: list[str],
    ) -> None:
        try:
            while True:
                line = await stream.readline()
                if not line:
                    return
                text = line.decode("utf-8", errors="replace")
                try:
                    callback(text)
                except Exception:
                    # Callback errors must not break the read loop.
                    pass
                collector.append(text)
        except (asyncio.IncompleteReadError, ConnectionError):
            return


__all__ = ["StreamReader"]