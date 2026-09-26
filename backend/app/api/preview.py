import asyncio
import json
import re

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sse_starlette.sse import EventSourceResponse
from sqlmodel import Session

from app.core.db import get_session
from app.core.events import event_bus
from app.services import preview as preview_service
from app.services import project as project_service

router = APIRouter(prefix="/api/projects", tags=["preview"])


# ============================================================================
# Preview lifecycle
# ============================================================================

@router.post("/{project_id}/preview/start")
async def start_preview(
    project_id: str,
    session: Session = Depends(get_session),
):
    project = project_service.get_project(session, project_id)

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
async def stop_preview(
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
        "project_id": project_id,
    }


@router.post("/{project_id}/preview/restart")
async def restart_preview(
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


# ============================================================================
# Preview proxy helpers
# ============================================================================

def _preview_prefix(project_id: str) -> str:
    """
    Browser-visible prefix for all generated preview resources.

    Example:

        /api/projects/abc123/preview/view
    """
    return f"/api/projects/{project_id}/preview/view"


def _is_already_proxied(
    url: str,
    prefix: str,
) -> bool:
    return (
        url == prefix
        or url.startswith(prefix + "/")
        or url.startswith(prefix + "?")
    )


def _rewrite_root_relative_url(
    url: str,
    prefix: str,
) -> str:
    """
    Convert root-relative URLs:

        /_next/static/...
        /images/logo.png

    into:

        /api/projects/{id}/preview/view/_next/static/...
        /api/projects/{id}/preview/view/images/logo.png
    """

    url = url.strip()

    # Not root-relative.
    if not url.startswith("/"):
        return url

    # Protocol-relative URL:
    # //cdn.example.com/file.js
    if url.startswith("//"):
        return url

    # Already proxied.
    if _is_already_proxied(
        url,
        prefix,
    ):
        return url

    # Keep application API routes untouched.
    if url.startswith("/api/"):
        return url

    return f"{prefix}{url}"


# ============================================================================
# CSS URL rewriting
# ============================================================================

def _rewrite_css_urls(
    text: str,
    prefix: str,
) -> str:
    """
    Rewrite root-relative URLs inside CSS.

    Example:

        url(/_next/static/media/font.woff2)

    becomes:

        url(/api/projects/{id}/preview/view/_next/static/media/font.woff2)
    """

    def replace_css_url(
        match: re.Match,
    ) -> str:
        original = match.group(0)

        value = match.group(1).strip()

        quote = ""

        # url("...")
        # url('...')
        if (
            len(value) >= 2
            and value[0] in {"'", '"'}
            and value[-1] == value[0]
        ):
            quote = value[0]
            value = value[1:-1]

        rewritten = _rewrite_root_relative_url(
            value,
            prefix,
        )

        if rewritten == value:
            return original

        return f"url({quote}{rewritten}{quote})"

    text = re.sub(
        r"url\(\s*([^)]+?)\s*\)",
        replace_css_url,
        text,
        flags=re.IGNORECASE,
    )

    # Catch quoted /_next/... references that may not be
    # inside url(...).
    text = re.sub(
        r'(["\'])/_next/',
        rf'\1{prefix}/_next/',
        text,
    )

    # Prevent accidental double prefixing.
    while f"{prefix}{prefix}" in text:
        text = text.replace(
            f"{prefix}{prefix}",
            prefix,
        )

    return text


# ============================================================================
# HTML URL rewriting
# ============================================================================

def _rewrite_preview_urls(
    text: str,
    project_id: str,
) -> str:
    """
    Rewrite root-relative URLs generated by Next.js.

    Handles:

        src="/..."
        href="/..."
        action="/..."
        poster="/..."
        formaction="/..."
        srcset="/..."

    Also rewrites inline CSS URLs and injects a small browser-side
    helper for fetch/XHR/history calls.
    """

    prefix = _preview_prefix(project_id)

    # ------------------------------------------------------------------------
    # HTML attributes
    # ------------------------------------------------------------------------

    attribute_pattern = re.compile(
        r'(?P<attr>\b(?:src|href|action|poster|formaction)\s*=\s*)'
        r'(?P<quote>["\'])'
        r'(?P<url>/[^"\']*)'
        r'(?P=quote)',
        re.IGNORECASE,
    )

    def replace_attribute(
        match: re.Match,
    ) -> str:
        attr = match.group("attr")
        quote = match.group("quote")
        url = match.group("url")

        rewritten = _rewrite_root_relative_url(
            url,
            prefix,
        )

        return (
            f"{attr}"
            f"{quote}"
            f"{rewritten}"
            f"{quote}"
        )

    text = attribute_pattern.sub(
        replace_attribute,
        text,
    )

    # ------------------------------------------------------------------------
    # srcset
    # ------------------------------------------------------------------------

    def replace_srcset(
        match: re.Match,
    ) -> str:
        attr = match.group("attr")
        quote = match.group("quote")
        value = match.group("value")

        parts = []

        for item in value.split(","):
            item = item.strip()

            if not item:
                continue

            pieces = item.split()

            if pieces:
                url = pieces[0]

                if url.startswith("/"):
                    url = _rewrite_root_relative_url(
                        url,
                        prefix,
                    )

                pieces[0] = url

            parts.append(
                " ".join(pieces)
            )

        return (
            f"{attr}"
            f"{quote}"
            f"{', '.join(parts)}"
            f"{quote}"
        )

    text = re.sub(
        r'(?P<attr>\bsrcset\s*=\s*)'
        r'(?P<quote>["\'])'
        r'(?P<value>[^"\']*)'
        r'(?P=quote)',
        replace_srcset,
        text,
        flags=re.IGNORECASE,
    )

    # ------------------------------------------------------------------------
    # Inline CSS URLs inside HTML
    # ------------------------------------------------------------------------

    text = _rewrite_css_urls(
        text,
        prefix,
    )

    # ------------------------------------------------------------------------
    # Remaining Next.js asset references
    #
    # Matches:
    #
    # "/_next/..."
    # '/_next/...'
    # (/_next/...)
    # ------------------------------------------------------------------------

    text = re.sub(
        r'(["\'(])/_next/',
        rf'\1{prefix}/_next/',
        text,
    )

    # ------------------------------------------------------------------------
    # Prevent double prefixing
    # ------------------------------------------------------------------------

    while f"{prefix}{prefix}" in text:
        text = text.replace(
            f"{prefix}{prefix}",
            prefix,
        )

    # =========================================================================
    # Browser-side URL rewriting
    # =========================================================================

    bootstrap = f"""
<script>
(function() {{
    "use strict";

    const PREVIEW_PREFIX = {json.dumps(prefix)};

    function rewriteUrl(input) {{
        if (typeof input !== "string") {{
            return input;
        }}

        if (
            input.startsWith(PREVIEW_PREFIX) ||
            input.startsWith("http://") ||
            input.startsWith("https://") ||
            input.startsWith("//") ||
            input.startsWith("data:") ||
            input.startsWith("blob:") ||
            input.startsWith("javascript:") ||
            input.startsWith("#")
        ) {{
            return input;
        }}

        /*
         * Keep API URLs untouched.
         *
         * Example:
         * /api/something
         */
        if (input.startsWith("/api/")) {{
            return input;
        }}

        /*
         * Rewrite root-relative URLs.
         */
        if (input.startsWith("/")) {{
            return PREVIEW_PREFIX + input;
        }}

        return input;
    }}

    // ========================================================================
    // fetch()
    // ========================================================================

    const originalFetch = window.fetch;

    window.fetch = function(input, init) {{
        try {{
            if (typeof input === "string") {{
                input = rewriteUrl(input);
            }} else if (
                typeof Request !== "undefined" &&
                input instanceof Request
            ) {{
                const rewritten = rewriteUrl(input.url);

                if (rewritten !== input.url) {{
                    input = new Request(
                        rewritten,
                        input
                    );
                }}
            }}
        }} catch (error) {{
            console.debug(
                "Preview fetch rewrite failed",
                error
            );
        }}

        return originalFetch.call(
            this,
            input,
            init
        );
    }};

    // ========================================================================
    // XMLHttpRequest
    // ========================================================================

    const OriginalXHR = window.XMLHttpRequest;

    window.XMLHttpRequest = function() {{
        const xhr = new OriginalXHR();

        const originalOpen = xhr.open;

        xhr.open = function(
            method,
            url,
            ...args
        ) {{
            try {{
                url = rewriteUrl(url);
            }} catch (error) {{
                console.debug(
                    "Preview XHR rewrite failed",
                    error
                );
            }}

            return originalOpen.call(
                this,
                method,
                url,
                ...args
            );
        }};

        return xhr;
    }};

    // Preserve XMLHttpRequest prototype.
    window.XMLHttpRequest.prototype =
        OriginalXHR.prototype;

    // ========================================================================
    // history.pushState()
    // ========================================================================

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

    // ========================================================================
    // history.replaceState()
    // ========================================================================

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

    # ------------------------------------------------------------------------
    # Inject helper before </head>
    # ------------------------------------------------------------------------

    if re.search(
        r"</head>",
        text,
        re.IGNORECASE,
    ):
        text = re.sub(
            r"</head>",
            bootstrap + "\n</head>",
            text,
            count=1,
            flags=re.IGNORECASE,
        )
    else:
        text = (
            bootstrap
            + text
        )

    return text


# ============================================================================
# Preview proxy
# ============================================================================

async def _proxy_preview(
    project_id: str,
    path: str,
    request: Request,
    session: Session,
) -> Response:

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

    # ------------------------------------------------------------------------
    # Verify preview process is alive
    # ------------------------------------------------------------------------

    if not preview_service.is_preview_running(
        project_id
    ):
        raise HTTPException(
            status_code=503,
            detail="Preview server is not running",
        )

    # ------------------------------------------------------------------------
    # Build upstream URL
    # ------------------------------------------------------------------------

    path = path.lstrip("/")

    target_url = (
        f"http://127.0.0.1:"
        f"{row.port}/"
        f"{path}"
    )

    query_string = request.url.query

    if query_string:
        target_url = (
            f"{target_url}"
            f"?{query_string}"
        )

    # ------------------------------------------------------------------------
    # Forward request
    # ------------------------------------------------------------------------

    try:
        async with httpx.AsyncClient(
            timeout=30.0,
            follow_redirects=False,
        ) as client:

            upstream = await client.request(
                method=request.method,
                url=target_url,
                headers={
                    key: value
                    for key, value
                    in request.headers.items()
                    if key.lower()
                    not in {
                        "host",
                        "content-length",
                        "connection",
                    }
                },
                content=await request.body(),
            )

    except httpx.ConnectError:
        raise HTTPException(
            status_code=502,
            detail="Unable to connect to preview server",
        )

    except httpx.TimeoutException:
        raise HTTPException(
            status_code=504,
            detail="Preview server request timed out",
        )

    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=(
                "Preview proxy error: "
                f"{type(exc).__name__}"
            ),
        )

    # ------------------------------------------------------------------------
    # Response content
    # ------------------------------------------------------------------------

    content = upstream.content

    content_type = (
        upstream.headers.get(
            "content-type",
            "",
        )
        .lower()
    )

    # ------------------------------------------------------------------------
    # Response headers
    # ------------------------------------------------------------------------

    response_headers = {
        key: value
        for key, value in upstream.headers.items()
        if key.lower()
        not in {
            "content-length",
            "content-encoding",
            "transfer-encoding",
            "connection",
        }
    }

    # =========================================================================
    # HTML
    # =========================================================================

    if "text/html" in content_type:

        try:
            encoding = (
                upstream.encoding
                or "utf-8"
            )

            text = content.decode(
                encoding,
                errors="replace",
            )

            text = _rewrite_preview_urls(
                text,
                project_id,
            )

            content = text.encode(
                "utf-8"
            )

            response_headers[
                "content-type"
            ] = "text/html; charset=utf-8"

        except Exception as exc:
            # Do not break the preview if rewriting fails.
            print(
                "Preview HTML rewrite failed:",
                repr(exc),
            )

    # =========================================================================
    # CSS
    # =========================================================================

    elif "text/css" in content_type:

        try:
            encoding = (
                upstream.encoding
                or "utf-8"
            )

            text = content.decode(
                encoding,
                errors="replace",
            )

            text = _rewrite_css_urls(
                text,
                _preview_prefix(
                    project_id
                ),
            )

            content = text.encode(
                "utf-8"
            )

            response_headers[
                "content-type"
            ] = "text/css; charset=utf-8"

        except Exception as exc:
            print(
                "Preview CSS rewrite failed:",
                repr(exc),
            )

    # =========================================================================
    # Return response
    # =========================================================================

    return Response(
        content=content,
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=None,
    )


# ============================================================================
# Preview root
# ============================================================================

@router.get("/{project_id}/preview/view")
async def preview_view_root(
    project_id: str,
    request: Request,
    session: Session = Depends(get_session),
):
    return await _proxy_preview(
        project_id=project_id,
        path="",
        request=request,
        session=session,
    )


# ============================================================================
# Preview catch-all
#
# Examples:
#
# /preview/view/_next/static/...
# /preview/view/assets/logo.png
# /preview/view/about
# /preview/view/api/...
# ============================================================================

@router.api_route(
    "/{project_id}/preview/view/{path:path}",
    methods=[
        "GET",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
        "OPTIONS",
        "HEAD",
    ],
)
async def preview_view(
    project_id: str,
    path: str,
    request: Request,
    session: Session = Depends(get_session),
):
    return await _proxy_preview(
        project_id=project_id,
        path=path,
        request=request,
        session=session,
    )


# ============================================================================
# Server-Sent Events
# ============================================================================

@router.get("/{project_id}/events")
async def events(
    project_id: str,
):
    queue = event_bus.subscribe(
        project_id
    )

    async def event_generator():
        try:
            while True:
                payload = await queue.get()

                try:
                    data = json.loads(
                        payload
                    )

                except json.JSONDecodeError:
                    data = {
                        "type": "message",
                        "data": payload,
                    }

                yield {
                    "event": data.get(
                        "type",
                        "message",
                    ),
                    "data": json.dumps(
                        data
                    ),
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
