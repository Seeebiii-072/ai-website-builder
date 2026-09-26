import asyncio
import logging
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Optional

import httpx
from sqlmodel import Session

from app.core.config import settings
from app.core.events import event_bus
from app.models.models import Project, Preview, PreviewStatus, ProjectStatus
from app.services import project as project_service


logger = logging.getLogger("preview")


# ============================================================
# LIVE PROCESS REGISTRY
# ============================================================

# Popen objects are kept in memory.
# SQLite stores only pid/port/status/url.
_RUNNING_PROCESSES: dict[str, subprocess.Popen] = {}


# ============================================================
# WINDOWS / NPM HELPERS
# ============================================================

def _npm_executable() -> str:
    """
    Find npm executable.

    On Windows, npm.cmd is normally the executable that should
    be launched through subprocess.
    """
    candidates = []

    if sys.platform == "win32":
        candidates.extend(
            [
                shutil.which("npm.cmd"),
                shutil.which("npm"),
            ]
        )
    else:
        candidates.extend(
            [
                shutil.which("npm"),
                shutil.which("npm.cmd"),
            ]
        )

    for executable in candidates:
        if executable:
            return executable

    raise RuntimeError(
        "npm was not found on PATH. "
        "Install Node.js/npm and restart the backend terminal."
    )


def _build_environment() -> dict[str, str]:
    """
    Build a clean environment for the Next.js dev server.
    """
    env = os.environ.copy()

    # Prevent interactive npm prompts.
    env["CI"] = "1"

    return env


# ============================================================
# PORT MANAGEMENT
# ============================================================

def _port_is_free(port: int) -> bool:
    """
    Check whether localhost port is currently available.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)

        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def find_available_port() -> int:
    """
    Find an available preview port.
    """
    for port in range(
        settings.preview_start_port,
        settings.preview_max_port + 1,
    ):
        if _port_is_free(port):
            return port

    raise RuntimeError(
        f"No available port between "
        f"{settings.preview_start_port} and "
        f"{settings.preview_max_port}"
    )


# ============================================================
# DATABASE HELPERS
# ============================================================

def _get_or_create_preview_row(
    session: Session,
    project_id: str,
) -> Preview:
    """
    Get the Preview row or create it.
    """
    row = session.get(Preview, project_id)

    if row is None:
        row = Preview(
            project_id=project_id,
            status=PreviewStatus.STOPPED,
        )

        session.add(row)
        session.commit()
        session.refresh(row)

    return row


# ============================================================
# HTTP HEALTH CHECK
# ============================================================

async def _wait_until_responsive(
    url: str,
    timeout_seconds: int = 90,
) -> bool:
    """
    Wait until the Next.js preview server responds.

    We intentionally use HTTP polling instead of relying only
    on process state.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds

    async with httpx.AsyncClient(
        timeout=2.0,
        follow_redirects=True,
    ) as client:

        while loop.time() < deadline:

            try:
                response = await client.get(url)

                if response.status_code < 500:
                    return True

            except (
                httpx.ConnectError,
                httpx.ConnectTimeout,
                httpx.ReadTimeout,
                httpx.RemoteProtocolError,
                httpx.HTTPError,
            ):
                pass

            await asyncio.sleep(1)

    return False


# ============================================================
# PROCESS LOG READER
# ============================================================

async def _read_process_output(
    project_id: str,
    proc: subprocess.Popen,
) -> None:
    """
    Continuously consume stdout from the Next.js process.

    This is important because stdout=subprocess.PIPE can
    eventually block a child process if nobody consumes it.
    """

    if proc.stdout is None:
        return

    try:
        while True:
            line = await asyncio.to_thread(proc.stdout.readline)

            if not line:
                break

            line = line.rstrip()

            if line:
                logger.info(
                    "[preview:%s] %s",
                    project_id,
                    line,
                )

    except Exception as exc:
        logger.debug(
            "Preview output reader stopped for %s: %s",
            project_id,
            exc,
        )


# ============================================================
# START PREVIEW
# ============================================================

async def start_preview(
    session: Session,
    project: Project,
) -> Preview:

    workspace = Path(project.workspace_path)

    row = _get_or_create_preview_row(
        session,
        project.id,
    )

    # --------------------------------------------------------
    # Validate workspace
    # --------------------------------------------------------

    if not workspace.exists():
        row.status = PreviewStatus.FAILED
        row.error_message = (
            f"Project workspace does not exist: {workspace}"
        )

        session.add(row)
        session.commit()

        await event_bus.publish(
            project.id,
            "preview_failed",
            {
                "error": row.error_message,
            },
        )

        return row

    # --------------------------------------------------------
    # Stop previous preview
    # --------------------------------------------------------

    await stop_preview(
        session,
        project.id,
    )

    # --------------------------------------------------------
    # Find available port
    # --------------------------------------------------------

    try:
        port = find_available_port()

    except Exception as exc:
        row.status = PreviewStatus.FAILED
        row.error_message = (
            f"Unable to find preview port: "
            f"{type(exc).__name__}: {exc}"
        )

        session.add(row)
        session.commit()

        await event_bus.publish(
            project.id,
            "preview_failed",
            {
                "error": row.error_message,
            },
        )

        return row

    # --------------------------------------------------------
    # Update DB
    # --------------------------------------------------------

    row.status = PreviewStatus.STARTING
    row.port = port
    row.pid = None
    row.url = None
    row.error_message = None

    session.add(row)
    session.commit()

    await event_bus.publish(
        project.id,
        "preview_starting",
        {
            "port": port,
        },
    )

    # --------------------------------------------------------
    # Find npm
    # --------------------------------------------------------

    try:
        npm = _npm_executable()

    except Exception as exc:
        row.status = PreviewStatus.FAILED
        row.error_message = str(exc)

        session.add(row)
        session.commit()

        await event_bus.publish(
            project.id,
            "preview_failed",
            {
                "error": row.error_message,
            },
        )

        return row

    # --------------------------------------------------------
    # Next.js command
    # --------------------------------------------------------

    cmd = [
        npm,
        "run",
        "dev",
        "--",
        "--port",
        str(port),
        "--hostname",
        "127.0.0.1",
    ]

    logger.info(
        "Starting preview for project=%s",
        project.id,
    )

    logger.info(
        "Preview workspace=%s",
        workspace,
    )

    logger.info(
        "Preview command=%s",
        cmd,
    )

    # --------------------------------------------------------
    # Windows-safe subprocess startup
    # --------------------------------------------------------

    try:

        def start_process() -> subprocess.Popen:
            kwargs = {
                "args": cmd,
                "cwd": str(workspace),
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "shell": False,
                "env": _build_environment(),
            }

            # Windows:
            # CREATE_NEW_PROCESS_GROUP allows us to manage the
            # child process more safely.
            if sys.platform == "win32":
                kwargs["creationflags"] = (
                    subprocess.CREATE_NEW_PROCESS_GROUP
                )

            return subprocess.Popen(**kwargs)

        # IMPORTANT:
        #
        # Do NOT use:
        #
        # asyncio.create_subprocess_exec()
        #
        # because your current Windows event-loop configuration
        # caused NotImplementedError.
        proc = await asyncio.to_thread(
            start_process
        )

    except Exception as exc:

        row.status = PreviewStatus.FAILED

        row.error_message = (
            f"Failed to start dev server: "
            f"{type(exc).__name__}: {exc}"
        )

        logger.exception(
            "Failed to start preview for project=%s",
            project.id,
        )

        session.add(row)
        session.commit()

        await event_bus.publish(
            project.id,
            "preview_failed",
            {
                "error": row.error_message,
            },
        )

        return row

    # --------------------------------------------------------
    # Register process
    # --------------------------------------------------------

    _RUNNING_PROCESSES[project.id] = proc

    row.pid = proc.pid

    session.add(row)
    session.commit()

    logger.info(
        "Preview process started: project=%s pid=%s port=%s",
        project.id,
        proc.pid,
        port,
    )

    # --------------------------------------------------------
    # Start background output reader
    # --------------------------------------------------------

    asyncio.create_task(
        _read_process_output(
            project.id,
            proc,
        )
    )

    # --------------------------------------------------------
    # Health check
    # --------------------------------------------------------

    url = f"http://127.0.0.1:{port}"

    responsive = await _wait_until_responsive(
        url,
        timeout_seconds=90,
    )

    # --------------------------------------------------------
    # Preview did not start
    # --------------------------------------------------------

    if not responsive:

        returncode = proc.poll()

        if returncode is None:
            error_message = (
                "Preview server did not become "
                "responsive within 90 seconds."
            )
        else:
            error_message = (
                "Preview server exited unexpectedly "
                f"with code {returncode}."
            )

        row.status = PreviewStatus.FAILED
        row.error_message = error_message

        logger.error(
            "Preview failed: project=%s pid=%s "
            "returncode=%s",
            project.id,
            proc.pid,
            returncode,
        )

        # Clean process
        try:
            if proc.poll() is None:
                proc.terminate()

                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(proc.wait),
                        timeout=5,
                    )
                except asyncio.TimeoutError:
                    proc.kill()
                    await asyncio.to_thread(proc.wait)

        except Exception:
            logger.exception(
                "Error cleaning failed preview process"
            )

        _RUNNING_PROCESSES.pop(
            project.id,
            None,
        )

        row.pid = None

        session.add(row)
        session.commit()

        await event_bus.publish(
            project.id,
            "preview_failed",
            {
                "error": row.error_message,
            },
        )

        return row

    # --------------------------------------------------------
    # Check process after successful HTTP response
    # --------------------------------------------------------

    returncode = proc.poll()

    if returncode is not None:

        row.status = PreviewStatus.FAILED
        row.error_message = (
            "Preview server exited unexpectedly "
            f"with code {returncode}"
        )

        _RUNNING_PROCESSES.pop(
            project.id,
            None,
        )

        row.pid = None

        session.add(row)
        session.commit()

        await event_bus.publish(
            project.id,
            "preview_failed",
            {
                "error": row.error_message,
            },
        )

        return row

    # --------------------------------------------------------
    # Preview is running
    # --------------------------------------------------------

    row.status = PreviewStatus.RUNNING
    row.url = url
    row.error_message = None

    session.add(row)
    session.commit()

    logger.info(
        "Preview is READY: project=%s url=%s",
        project.id,
        url,
    )

    await event_bus.publish(
        project.id,
        "preview_ready",
        {
            "url": url,
        },
    )

    # --------------------------------------------------------
    # Project becomes READY
    # --------------------------------------------------------

    project_service.set_status(
        session,
        project,
        ProjectStatus.READY,
    )

    return row


# ============================================================
# STOP PREVIEW
# ============================================================

async def stop_preview(
    session: Session,
    project_id: str,
) -> None:

    proc = _RUNNING_PROCESSES.pop(
        project_id,
        None,
    )

    if proc is not None:

        try:

            if proc.poll() is None:

                logger.info(
                    "Stopping preview: project=%s pid=%s",
                    project_id,
                    proc.pid,
                )

                proc.terminate()

                try:
                    await asyncio.wait_for(
                        asyncio.to_thread(proc.wait),
                        timeout=10,
                    )

                except asyncio.TimeoutError:

                    logger.warning(
                        "Preview did not terminate gracefully. "
                        "Killing process: project=%s pid=%s",
                        project_id,
                        proc.pid,
                    )

                    proc.kill()

                    await asyncio.to_thread(
                        proc.wait
                    )

        except Exception:
            logger.exception(
                "Error stopping preview process: "
                "project=%s",
                project_id,
            )

    # --------------------------------------------------------
    # Update DB
    # --------------------------------------------------------

    row = session.get(
        Preview,
        project_id,
    )

    if row is not None:

        row.status = PreviewStatus.STOPPED
        row.pid = None

        session.add(row)
        session.commit()

        await event_bus.publish(
            project_id,
            "preview_stopped",
            {},
        )


# ============================================================
# RESTART
# ============================================================

async def restart_preview(
    session: Session,
    project: Project,
) -> Preview:

    return await start_preview(
        session,
        project,
    )


# ============================================================
# GET PREVIEW
# ============================================================

def get_preview(
    session: Session,
    project_id: str,
) -> Optional[Preview]:

    return session.get(
        Preview,
        project_id,
    )


# ============================================================
# RUNNING CHECK
# ============================================================

def is_preview_running(
    project_id: str,
) -> bool:

    proc = _RUNNING_PROCESSES.get(
        project_id
    )

    return (
        proc is not None
        and proc.poll() is None
    )