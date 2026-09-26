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
from app.models.models import PreviewStatus
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
            404,
            "Project not found",
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
            404,
            "Project not found",
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
            404,
            "Project not found",
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
            404,
            "No preview for this project",
        )

    return row


# ============================================================
# PREVIEW URL REWRITE
# ============================================================

def _preview_prefix(
    project_id: str,
) -> str:
    return (
        f"/api/projects/"
        f"{project_id}/preview/view"
    )


def _rewrite_preview_urls(
    text: str,
    prefix: str,
) -> str:
    """
    Rewrite root-relative URLs in generated
    HTML so assets remain inside the proxy.
    """

    # HTML:
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

    # CSS url(/...)
    text = re.sub(
        r'(url\(\s*["\']?)/(?!/)',
        lambda m: (
            f"{m.group(1)}{prefix}/"
        ),
        text,
        flags=re.IGNORECASE,
    )

    # Next.js static assets in inline JS
    text = re.sub(
        r'(["\'`])/_next/',
        lambda m: (
            f"{m.group(1)}"
            f"{prefix}/_next/"
        ),
        text,
    )

    # Root API references
    text = re.sub(
        r'(["\'`])/api/',
        lambda m: (
            f"{m.group(1)}"
            f"{prefix}/api/"
        ),
        text,
    )

    # Browser-side fetch/XHR/history rewriting
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
        }} else if (input instanceof Request) {{
            const rewritten =
                rewriteUrl(input.url);

            if (rewritten !== input.url) {{
                input = new Request(
                    rewritten,
                    input
                );
            }}
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

    history.pushState = function(
        state,
        title,
        url
    ) {{
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

    history.replaceState = function(
        state,
        title,
        url
    ) {{
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

    if re.search(
        r"</head>",
        text,
        flags=re.IGNORECASE,
    ):
        text = re.sub(
            r"</head>",
            bootstrap + "</head>",
            text,
            count=1,
            flags=re.IGNORECASE,
        )
    else:
        text = bootstrap + text

    return text


# ============================================================
# SECURE PREVIEW PROXY
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
@router.api_route(
    "/{project_id}/preview/view/{path:path}",
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
async def proxy_preview(
    project_id: str,
    request: Request,
    path: str = "",
    session: Session = Depends(get_session),
):
    """
    Securely proxy the project's private
    localhost Next.js preview.

    Browser:
        /api/projects/{id}/preview/view/...

    VM:
        127.0.0.1:<stored-port>/...
    """

    # --------------------------------------------------------
    # Validate project
    # --------------------------------------------------------

    project = project_service.get_project(
        session,
        project_id,
    )

    if project is None:
        raise HTTPException(
            404,
            "Project not found",
        )

    # --------------------------------------------------------
    # Validate preview
    # --------------------------------------------------------

    preview = preview_service.get_preview(
        session,
        project_id,
    )

    if (
        preview is None
        or preview.port is None
        or preview.status != PreviewStatus.RUNNING
    ):
        raise HTTPException(
            409,
            "Preview is not running",
        )

    # --------------------------------------------------------
    # Upstream
    # --------------------------------------------------------

    port = int(preview.port)

    clean_path = path.lstrip("/")

    upstream_url = (
        f"http://127.0.0.1:"
        f"{port}/"
        f"{clean_path}"
    )

    # --------------------------------------------------------
    # Request headers
    # --------------------------------------------------------

    hop_by_hop = {
        "host",
        "content-length",
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }

    request_headers = {}

    for key, value in request.headers.items():
        if key.lower() in hop_by_hop:
            continue

        request_headers[key] = value

    # We need uncompressed content because HTML/CSS/JS
    # may be rewritten below.
    request_headers["accept-encoding"] = "identity"

    body = await request.body()

    # --------------------------------------------------------
    # Forward request
    # --------------------------------------------------------

    try:
        async with httpx.AsyncClient(
            timeout=60.0,
            follow_redirects=False,
        ) as client:

            upstream = await client.request(
                method=request.method,
                url=upstream_url,
                params=request.query_params,
                headers=request_headers,
                content=body,
            )

    except (
        httpx.ConnectError,
        httpx.ConnectTimeout,
        httpx.ReadTimeout,
        httpx.RemoteProtocolError,
        httpx.HTTPError,
    ) as exc:

        logger.warning(
            "Preview proxy failed: "
            "project=%s port=%s error=%s",
            project_id,
            port,
            exc,
        )

        raise HTTPException(
            502,
            "Preview server is unavailable",
        )

    # --------------------------------------------------------
    # Response headers
    # --------------------------------------------------------

    blocked_response_headers = {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "content-length",
        "content-encoding",
    }

    response_headers = {}

    for key, value in upstream.headers.items():
        if key.lower() not in blocked_response_headers:
            response_headers[key] = value

    # --------------------------------------------------------
    # Rewrite redirects
    # --------------------------------------------------------

    location = upstream.headers.get("location")

    if location:

        internal_origin = (
            f"http://127.0.0.1:{port}"
        )

        if location.startswith(
            internal_origin
        ):
            location = (
                location[
                    len(internal_origin):
                ]
                or "/"
            )

        if location.startswith("/"):
            location = (
                _preview_prefix(project_id)
                + location
            )

        response_headers["location"] = location

    # --------------------------------------------------------
    # Rewrite response body
    # --------------------------------------------------------

    content = upstream.content

    content_type = upstream.headers.get(
        "content-type",
        "",
    ).lower()

    prefix = _preview_prefix(
        project_id
    )

    # HTML
    if (
        "text/html" in content_type
        or "application/xhtml+xml"
        in content_type
    ):

        try:
            text_body = content.decode(
                "utf-8",
                errors="replace",
            )

            text_body = _rewrite_preview_urls(
                text_body,
                prefix,
            )

            content = text_body.encode(
                "utf-8"
            )

        except Exception:

            logger.exception(
                "Failed to rewrite preview HTML "
                "for project=%s",
                project_id,
            )

    # CSS / JavaScript
    elif (
        "text/css" in content_type
        or "javascript" in content_type
        or "ecmascript" in content_type
    ):

        try:
            text_body = content.decode(
                "utf-8",
                errors="replace",
            )

            text_body = re.sub(
                r'(["\'`])/_next/',
                lambda m: (
                    f"{m.group(1)}"
                    f"{prefix}/_next/"
                ),
                text_body,
            )

            text_body = re.sub(
                r'(["\'`])/api/',
                lambda m: (
                    f"{m.group(1)}"
                    f"{prefix}/api/"
                ),
                text_body,
            )

            text_body = re.sub(
                r'(url\(\s*["\']?)/(?!/)',
                lambda m: (
                    f"{m.group(1)}"
                    f"{prefix}/"
                ),
                text_body,
            )

            content = text_body.encode(
                "utf-8"
            )

        except Exception:

            logger.exception(
                "Failed to rewrite preview asset "
                "for project=%s",
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
    """
    Server-Sent Events stream of status updates.
    """

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
