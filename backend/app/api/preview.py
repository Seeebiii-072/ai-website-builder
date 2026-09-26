import asyncio
import json
import logging
import re

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sse_starlette.sse import EventSourceResponse
from sqlmodel import Session

from app.core.db import get_session
from app.core.events import event_bus
from app.services import project as project_service
from app.services import preview as preview_service


logger = logging.getLogger("api.preview")

router = APIRouter(
    prefix="/api/projects",
    tags=["preview"],
)


# ============================================================
# PREVIEW LIFECYCLE
# ============================================================

@router.post("/{project_id}/preview/start")
async def start(
    project_id: str,
    session: Session = Depends(get_session),
):
    project = project_service.get_project(
        session,
        project_id,
    )

    if project is None:
        raise HTTPException(
            status_code=404,
            detail="Project not found",
        )

    row = await preview_service.start_preview(
        session,
        project,
    )

    return row


@router.post("/{project_id}/preview/stop")
async def stop(
    project_id: str,
    session: Session = Depends(get_session),
):
    project = project_service.get_project(
        session,
        project_id,
    )

    if project is None:
        raise HTTPException(
            status_code=404,
            detail="Project not found",
        )

    await preview_service.stop_preview(
        session,
        project_id,
    )

    return {
        "status": "stopped",
    }


@router.post("/{project_id}/preview/restart")
async def restart(
    project_id: str,
    session: Session = Depends(get_session),
):
    project = project_service.get_project(
        session,
        project_id,
    )

    if project is None:
        raise HTTPException(
            status_code=404,
            detail="Project not found",
        )

    row = await preview_service.restart_preview(
        session,
        project,
    )

    return row


# ============================================================
# PREVIEW STATUS
# ============================================================

@router.get("/{project_id}/preview")
def get_preview(
    project_id: str,
    session: Session = Depends(get_session),
):
    row = preview_service.get_preview(
        session,
        project_id,
    )

    if row is None:
        raise HTTPException(
            status_code=404,
            detail="No preview for this project",
        )

    return row


# ============================================================
# PREVIEW PROXY HELPERS
# ============================================================

def _preview_prefix(project_id: str) -> str:
    return (
        f"/api/projects/"
        f"{project_id}/preview/view"
    )


def _rewrite_preview_urls(
    text: str,
    prefix: str,
) -> str:
    """
    Rewrite root-relative URLs so that generated
    Next.js assets remain inside the preview proxy.
    """

    # HTML attributes:
    # src="/..."
    # href="/..."
    # action="/..."
    # poster="/..."
    # formaction="/..."
    text = re.sub(
        r'((?:src|href|action|poster|formaction)'
        r'\s*=\s*["\'])/(?!/)',
        lambda m: (
            f"{m.group(1)}{prefix}/"
        ),
        text,
        flags=re.IGNORECASE,
    )

    # CSS:
    # url(/...)
    text = re.sub(
        r'(url\(\s*["\']?)/(?!/)',
        lambda m: (
            f"{m.group(1)}{prefix}/"
        ),
        text,
        flags=re.IGNORECASE,
    )

    # Next.js static assets:
    # "/_next/..."
    text = re.sub(
        r'(["\'`])/_next/',
        lambda m: (
            f"{m.group(1)}"
            f"{prefix}/_next/"
        ),
        text,
    )

    # Root API references:
    # "/api/..."
    text = re.sub(
        r'(["\'`])/api/',
        lambda m: (
            f"{m.group(1)}"
            f"{prefix}/api/"
        ),
        text,
    )

    # Browser-side fetch/XHR/history rewriting.
    bootstrap = f"""
<script>
(() => {{
    const PREFIX = {json.dumps(prefix)};

    function rewriteUrl(value) {{
        if (typeof value !== "string" || !value) {{
            return value;
        }}

        try {{
            const url = new URL(
                value,
                window.location.href
            );

            if (
                url.origin === window.location.origin &&
                url.pathname.startsWith("/") &&
                !url.pathname.startsWith(PREFIX)
            ) {{
                url.pathname =
                    PREFIX + url.pathname;
            }}

            return url.toString();

        }} catch (_) {{
            return value;
        }}
    }}

    const originalFetch =
        window.fetch.bind(window);

    window.fetch = function(input, init) {{

        if (typeof input === "string") {{

            input = rewriteUrl(input);

        }} else if (
            typeof Request !== "undefined" &&
            input instanceof Request
        ) {{

            const rewritten =
                rewriteUrl(input.url);

            if (rewritten !== input.url) {{

                input = new Request(
                    rewritten,
                    input
                );
            }
        }}

        return originalFetch(
            input,
            init
        );
    }};


    const originalOpen =
        XMLHttpRequest.prototype.open;

    XMLHttpRequest.prototype.open =
        function(method, url, ...rest) {{

            return originalOpen.call(
                this,
                method,
                rewriteUrl(url),
                ...rest
            );
        }};


    const originalPushState =
        history.pushState;

    history.pushState =
        function(state, title, url) {{

            if (typeof url === "string") {{
                url = rewriteUrl(url);
            }}

            return originalPushState.call(
                this,
                state,
                title,
                url
            );
        }};


    const originalReplaceState =
        history.replaceState;

    history.replaceState =
        function(state, title, url) {{

            if (typeof url === "string") {{
                url = rewriteUrl(url);
            }}

            return originalReplaceState.call(
                this,
                state,
                title,
                url
            );
        }};
}})();
</script>
"""

    if "</head>" in text.lower():

        index = text.lower().find("</head>")

        text = (
            text[:index]
            + bootstrap
            + text[index:]
        )

    else:

        text = (
            bootstrap
            + text
        )

    return text


# ============================================================
# PREVIEW VIEW / PROXY
# ============================================================

@router.api_route(
    "/{project_id}/preview/view",
    methods=[
        "GET",
        "HEAD",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
        "OPTIONS",
    ],
)
async def preview_view(
    project_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    """
    Proxy browser requests to the project's local
    Next.js preview server.
    """

    row = preview_service.get_preview(
        session,
        project_id,
    )

    if row is None:
        raise HTTPException(
            status_code=404,
            detail="No preview for this project",
        )

    if row.port is None:
        raise HTTPException(
            status_code=503,
            detail="Preview has no assigned port",
        )

    if not preview_service.is_preview_running(
        project_id
    ):
        raise HTTPException(
            status_code=503,
            detail="Preview server is not running",
        )

    # Preserve path after /preview/view
    proxy_path = request.path_params.get(
        "path",
        "",
    )

    # For the exact /preview/view URL.
    if not proxy_path:
        proxy_path = ""

    target_url = (
        f"http://127.0.0.1:{row.port}"
        f"/{proxy_path}"
    )

    if request.url.query:
        target_url += (
            f"?{request.url.query}"
        )

    body = await request.body()

    headers = {}

    hop_by_hop = {
        "host",
        "content-length",
        "connection",
        "keep-alive",
        "transfer-encoding",
    }

    for key, value in request.headers.items():

        if key.lower() not in hop_by_hop:
            headers[key] = value

    try:

        async with httpx.AsyncClient(
            timeout=60.0,
            follow_redirects=False,
        ) as client:

            upstream = await client.request(
                request.method,
                target_url,
                headers=headers,
                content=body,
            )

    except httpx.ConnectError as exc:

        logger.exception(
            "Preview proxy connection failed: "
            "project=%s port=%s",
            project_id,
            row.port,
        )

        raise HTTPException(
            status_code=502,
            detail=(
                "Preview server is unavailable"
            ),
        ) from exc

    except httpx.HTTPError as exc:

        logger.exception(
            "Preview proxy HTTP error: project=%s",
            project_id,
        )

        raise HTTPException(
            status_code=502,
            detail=(
                "Unable to communicate with "
                "preview server"
            ),
        ) from exc

    response_headers = {}

    response_hop_by_hop = {
        "content-length",
        "transfer-encoding",
        "connection",
        "keep-alive",
    }

    for key, value in upstream.headers.items():

        if key.lower() not in response_hop_by_hop:

            response_headers[key] = value

    content_type = (
        upstream.headers.get(
            "content-type",
            "",
        )
    )

    content = upstream.content

    # Rewrite HTML so its root-relative resources
    # continue going through this proxy.
    if (
        "text/html" in content_type.lower()
        and content
    ):

        try:

            text = content.decode(
                upstream.encoding or "utf-8",
                errors="replace",
            )

            prefix = _preview_prefix(
                project_id
            )

            text = _rewrite_preview_urls(
                text,
                prefix,
            )

            content = text.encode("utf-8")

            response_headers.pop(
                "content-encoding",
                None,
            )

        except Exception:

            logger.exception(
                "Failed to rewrite preview HTML: "
                "project=%s",
                project_id,
            )

    return Response(
        content=content,
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=None,
    )


# ============================================================
# SSE EVENTS
# ============================================================

@router.get("/{project_id}/events")
async def events(
    project_id: str,
):
    """Server-Sent Events stream of status updates."""

    queue = event_bus.subscribe(
        project_id
    )

    async def event_generator():

        try:

            while True:

                payload = await queue.get()

                data = json.loads(
                    payload
                )

                yield {
                    "event": data["type"],
                    "data": json.dumps(data),
                }

        except asyncio.CancelledError:
            pass

        finally:

            event_bus.unsubscribe(
                project_id,
                queue,
            )

    return EventSourceResponse(
        event_generator()
    )
