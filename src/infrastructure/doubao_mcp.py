"""Thread-safe bridge to the pinned Doubao Search MCP stdio server."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


DOUBAO_MCP_REVISION = "03d951aed6855a6badf92219c23570302cbd263d"
DOUBAO_MCP_SOURCE = (
    "git+https://github.com/volcengine/mcp-server"
    f"@{DOUBAO_MCP_REVISION}"
    "#subdirectory=server/mcp_server_askecho_search_infinity"
)
_PASSTHROUGH_ENV = (
    "PATH",
    "HOME",
    "USER",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)


def fit_doubao_query(query: str, limit: int = 100) -> tuple[str, bool]:
    """Normalize a query and retain both ends when the API limit is exceeded."""
    normalized = " ".join(query.split())
    if not normalized:
        raise ValueError("Doubao query must not be empty")
    if len(normalized) <= limit:
        return normalized, False
    prefix_limit = (limit - 1) // 2
    prefix = normalized[:prefix_limit].rsplit(" ", 1)[0] or normalized[:prefix_limit]
    suffix_limit = limit - len(prefix) - 1
    suffix = normalized[-suffix_limit:].lstrip()
    return f"{prefix} {suffix}", True


def parse_doubao_web_results(text: str) -> list[dict[str, Any]]:
    """Validate a ``web_search`` response and return its web result records."""
    try:
        body = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Doubao MCP returned non-JSON content") from exc
    result = body.get("Result")
    if not isinstance(result, dict):
        raise RuntimeError("Doubao MCP returned no Result")
    rows = result.get("WebResults")
    if not isinstance(rows, list):
        raise RuntimeError("Doubao MCP returned no WebResults")
    return [row for row in rows if isinstance(row, dict)]


def resolve_uvx_path(configured_path: str = "") -> str:
    """Resolve uvx from explicit config, PATH, or the project's virtualenvs."""
    if configured_path:
        candidate = Path(configured_path).expanduser()
        if candidate.is_file():
            return str(candidate.resolve())
        resolved = shutil.which(configured_path)
        if resolved:
            return str(Path(resolved).resolve())
        raise FileNotFoundError(f"uvx executable not found: {configured_path}")

    resolved = shutil.which("uvx")
    if resolved:
        return str(Path(resolved).resolve())
    project_root = Path(__file__).resolve().parents[2]
    for candidate in (
        project_root / ".venv311" / "bin" / "uvx",
        project_root / ".venv" / "bin" / "uvx",
    ):
        if candidate.is_file():
            return str(candidate.resolve())
    raise FileNotFoundError(
        "uvx executable not found; install the uv package or set DOUBAO_UVX_PATH"
    )


def _child_environment(api_key: str) -> dict[str, str]:
    child = {
        name: value
        for name in _PASSTHROUGH_ENV
        if (value := os.environ.get(name))
    }
    child["ASK_ECHO_SEARCH_INFINITY_API_KEY"] = api_key
    return child


class DoubaoMcpClient:
    """Own one lazy persistent MCP session and expose synchronous searches."""

    def __init__(self, *, api_key: str, uvx_path: str = "") -> None:
        if not api_key:
            raise ValueError("ASK_ECHO_SEARCH_INFINITY_API_KEY is required")
        self._api_key = api_key
        self._uvx_path = resolve_uvx_path(uvx_path)
        self._state_lock = threading.Lock()
        self._ready = threading.Event()
        self._close_requested = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._shutdown: asyncio.Event | None = None
        self._session: ClientSession | None = None
        self._startup_error: BaseException | None = None
        self._closed = False

    def _start_if_needed(self) -> None:
        with self._state_lock:
            if self._closed:
                raise RuntimeError("Doubao MCP client is closed")
            if self._thread is not None:
                return
            self._ready.clear()
            self._startup_error = None
            self._thread = threading.Thread(
                target=self._run_session,
                name="doubao-mcp",
                daemon=True,
            )
            self._thread.start()

    def _wait_until_ready(self, timeout: float) -> None:
        self._start_if_needed()
        if not self._ready.wait(timeout):
            raise TimeoutError("Timed out starting Doubao MCP server")
        if self._startup_error is not None:
            raise RuntimeError("Failed to start Doubao MCP server") from (
                self._startup_error
            )
        if self._session is None or self._loop is None:
            raise RuntimeError("Doubao MCP session is not ready")

    def _run_session(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._own_session())
        except BaseException as exc:
            self._startup_error = exc
            self._ready.set()
        finally:
            self._session = None
            loop.close()
            with self._state_lock:
                self._loop = None
                self._shutdown = None
                self._thread = None

    async def _own_session(self) -> None:
        server = StdioServerParameters(
            command=self._uvx_path,
            args=[
                "--python",
                "3.12",
                "--with",
                "mcp<2",
                "--from",
                DOUBAO_MCP_SOURCE,
                "mcp-server-askecho-search-infinity",
            ],
            env=_child_environment(self._api_key),
        )
        self._shutdown = asyncio.Event()
        async with stdio_client(server) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                if "web_search" not in {tool.name for tool in tools.tools}:
                    raise RuntimeError("Doubao MCP did not expose web_search")
                self._session = session
                self._ready.set()
                if self._close_requested.is_set():
                    return
                await self._shutdown.wait()

    async def _search(
        self,
        *,
        query: str,
        count: int,
        time_range: str | None,
        timeout: float,
    ) -> list[dict[str, Any]]:
        if self._session is None:
            raise RuntimeError("Doubao MCP session is not ready")
        arguments: dict[str, Any] = {
            "Query": query,
            "Count": count,
            "SearchType": "web",
        }
        if time_range:
            arguments["TimeRange"] = time_range
        result = await self._session.call_tool(
            "web_search",
            arguments=arguments,
            read_timeout_seconds=timedelta(seconds=timeout),
        )
        if result.isError or not result.content:
            raise RuntimeError("Doubao MCP web_search failed")
        texts = [
            str(text)
            for item in result.content
            if (text := getattr(item, "text", ""))
        ]
        if not texts:
            raise RuntimeError("Doubao MCP web_search returned no text")
        return parse_doubao_web_results("\n".join(texts))

    def search(
        self,
        query: str,
        *,
        count: int,
        time_range: str | None = None,
        timeout: float,
    ) -> list[dict[str, Any]]:
        if not 1 <= count <= 50:
            raise ValueError("Doubao result count must be between 1 and 50")
        if timeout <= 0:
            raise ValueError("Doubao timeout must be positive")
        started = time.monotonic()
        self._wait_until_ready(timeout)
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError("Doubao MCP search timed out")
        loop = self._loop
        if loop is None:
            raise RuntimeError("Doubao MCP event loop is unavailable")
        future = asyncio.run_coroutine_threadsafe(
            self._search(
                query=query,
                count=count,
                time_range=time_range,
                timeout=remaining,
            ),
            loop,
        )
        try:
            return future.result(timeout=remaining)
        except TimeoutError:
            future.cancel()
            raise

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            thread = self._thread
            loop = self._loop
            shutdown = self._shutdown
            self._close_requested.set()
        if (
            thread is not None
            and thread.is_alive()
            and loop is not None
            and shutdown is not None
            and loop.is_running()
        ):
            loop.call_soon_threadsafe(shutdown.set)
        if thread is not None and thread.is_alive():
            thread.join(timeout=15)

    def __enter__(self) -> "DoubaoMcpClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
