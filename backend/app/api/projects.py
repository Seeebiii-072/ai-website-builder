import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from app.core.db import get_session
from app.models.models import Project, ProjectStatus
from app.services import project as project_service
from app.services import build as build_service
from app.services import preview as preview_serviceimport asyncio
import json
import logging
import re

import httpx
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    Response,
)
from pydantic import BaseModel
from sqlmodel import Session

from app.core.db import get_session
from app.core.events import event_bus
from app.models.models import (
    Project,
    ProjectStatus,
    PreviewStatus,
)
from app.services import project as project_service
from app.services import build as build_service
from app.services import preview as preview_service
from app.services.llm import (
    LLMAllProvidersFailedError,
)


logger = logging.getLogger(
    "api.projects"
)

router = APIRouter(
    prefix="/api/projects",
    tags=["projects"],
)


# ============================================================
# REQUEST / RESPONSE MODELS
# ============================================================

class CreateProjectRequest(BaseModel):
    name: str
    prompt: str


class ProjectResponse(BaseModel):
    id: str
    name: str
    prompt: str
    status: str
    error_message: str | None = None
    preview_url: str | None = None
    created_at: str
    updated_at: str


class EditRequest(BaseModel):
    message: str


# ============================================================
# SERIALIZATION
# ============================================================

def _serialize(
    session: Session,
    project: Project,
) -> ProjectResponse:

    preview = preview_service.get_preview(
        session,
        project.id,
    )

    preview_url = None

    if (
        preview
        and preview.status == PreviewStatus.RUNNING
    ):
        preview_url = (
            preview.url
            or preview_service.get_public_preview_url(
                project.id
            )
        )

    return ProjectResponse(
        id=project.id,
        name=project.name,
        prompt=project.prompt,
        status=project.status,
        error_message=project.error_message,
        preview_url=preview_url,
        created_at=project.created_at.isoformat(),
        updated_at=project.updated_at.isoformat(),
    )


# ============================================================
# FULL GENERATION PIPELINE
# ============================================================

async def _run_full_pipeline(
    project_id: str,
):
    """
    Generate -> build -> preview.
    Runs as background task.
    """

    from app.core.db import engine
    from sqlmodel import Session as SQLSession

    with SQLSession(engine) as session:

        project = project_service.get_project(
            session,
            project_id,
        )

        if project is None:
            return

        try:

            try:

                await project_service.generate_website(
                    session,
                    project,
                )

            except (
                LLMAllProvidersFailedError,
                ValueError,
            ):

                return

            session.refresh(project)

            ok = await build_service.build_with_autofix(
                session,
                project,
            )

            if not ok:
                return

            session.refresh(project)

            await preview_service.start_preview(
                session,
                project,
            )

        except Exception as e:

            logger.exception(
                "Unexpected error in generation "
                "pipeline for %s",
                project_id,
            )

            project_service.set_status(
                session,
                project,
                ProjectStatus.FAILED,
                f"Unexpected error: {e}",
            )

            await event_bus.publish(
                project_id,
                "generation_failed",
                {
                    "error": f"Unexpected error: {e}"
                },
            )


# ============================================================
# PREVIEW PROXY HELPERS
# ============================================================

def _preview_prefix(
    project_id: str,
) -> str:

    return (
        f"/api/projects/"
        f"{project_id}/preview"
    )


def _rewrite_preview_urls(
    text: str,
    prefix: str,
) -> str:
    """
    Rewrite root-relative URLs in generated
    HTML/CSS/JS so they remain inside the
    project preview proxy.
    """

    # --------------------------------------------------------
    # HTML attributes
    # --------------------------------------------------------

    text = re.sub(
        r'((?:src|href|action|poster|formaction)'
        r'\s*=\s*["\'])/(?!/)',
        lambda match: (
            f"{match.group(1)}{prefix}/"
        ),
        text,
        flags=re.IGNORECASE,
    )

    # --------------------------------------------------------
    # CSS url(/...)
    # --------------------------------------------------------

    text = re.sub(
        r'(url\(\s*["\']?)/(?!/)',
        lambda match: (
            f"{match.group(1)}{prefix}/"
        ),
        text,
        flags=re.IGNORECASE,
    )

    # --------------------------------------------------------
    # Next.js static URLs inside scripts
    # --------------------------------------------------------

    text = re.sub(
        r'(["\'`])/_next/',
        lambda match: (
            f"{match.group(1)}"
            f"{prefix}/_next/"
        ),
        text,
    )

    # --------------------------------------------------------
    # API paths inside generated scripts
    # --------------------------------------------------------

    text = re.sub(
        r'(["\'`])/api/',
        lambda match: (
            f"{match.group(1)}"
            f"{prefix}/api/"
        ),
        text,
    )

    # --------------------------------------------------------
    # Browser-side URL rewriting
    # --------------------------------------------------------

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

    // fetch()
    const originalFetch =
        window.fetch.bind(window);

    window.fetch = function(input, init) {{

        if (typeof input === "string") {{

            input = rewriteUrl(input);

        }} else if (
            input instanceof Request
        ) {{

            const rewritten =
                rewriteUrl(input.url);

            if (
                rewritten !== input.url
            ) {{
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

    // XMLHttpRequest
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

    // history.pushState
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

    // history.replaceState
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

    # Inject script before </head>
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

        text = (
            bootstrap
            + text
        )

    return text


# ============================================================
# PREVIEW PROXY
# ============================================================

@router.api_route(
    "/{project_id}/preview",
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
    "/{project_id}/preview/{path:path}",
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
    session: Session = Depends(
        get_session
    ),
):
    """
    Securely proxy the project's private
    localhost Next.js preview.

    Browser:
        /api/projects/{id}/preview/...

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
            status_code=404,
            detail="Project not found",
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
            status_code=409,
            detail="Preview is not running",
        )

    # --------------------------------------------------------
    # IMPORTANT:
    # Port comes ONLY from our database.
    # The client cannot choose the upstream host/port.
    # --------------------------------------------------------

    port = int(
        preview.port
    )

    clean_path = path.lstrip(
        "/"
    )

    upstream_url = (
        f"http://127.0.0.1:"
        f"{port}/"
        f"{clean_path}"
    )

    # --------------------------------------------------------
    # Forward safe request headers
    # --------------------------------------------------------

    hop_by_hop_headers = {
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

        if key.lower() in hop_by_hop_headers:
            continue

        request_headers[key] = value

    # Force identity encoding because
    # responses may need rewriting.
    request_headers["accept-encoding"] = (
        "identity"
    )

    body = await request.body()

    # --------------------------------------------------------
    # Upstream request
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
            status_code=502,
            detail="Preview server is unavailable",
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

        if (
            key.lower()
            not in blocked_response_headers
        ):
            response_headers[key] = value

    # --------------------------------------------------------
    # Redirect rewriting
    # --------------------------------------------------------

    location = upstream.headers.get(
        "location"
    )

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

        if location.startswith(
            "/"
        ):

            location = (
                _preview_prefix(
                    project_id
                )
                + location
            )

        response_headers["location"] = location

    # --------------------------------------------------------
    # Response body
    # --------------------------------------------------------

    content = upstream.content

    content_type = upstream.headers.get(
        "content-type",
        "",
    ).lower()

    prefix = _preview_prefix(
        project_id
    )

    # --------------------------------------------------------
    # HTML
    # --------------------------------------------------------

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

            text_body = (
                _rewrite_preview_urls(
                    text_body,
                    prefix,
                )
            )

            content = text_body.encode(
                "utf-8"
            )

        except Exception:

            logger.exception(
                "Failed to rewrite preview "
                "HTML for %s",
                project_id,
            )

    # --------------------------------------------------------
    # CSS / JS
    # --------------------------------------------------------

    elif (
        "text/css" in content_type
        or "javascript" in content_type
        or "ecmascript"
        in content_type
    ):

        try:

            text_body = content.decode(
                "utf-8",
                errors="replace",
            )

            # Next.js static files
            text_body = re.sub(
                r'(["\'`])/_next/',
                lambda match: (
                    f"{match.group(1)}"
                    f"{prefix}/_next/"
                ),
                text_body,
            )

            # Root API references
            text_body = re.sub(
                r'(["\'`])/api/',
                lambda match: (
                    f"{match.group(1)}"
                    f"{prefix}/api/"
                ),
                text_body,
            )

            # CSS url(/...)
            text_body = re.sub(
                r'(url\(\s*["\']?)/(?!/)',
                lambda match: (
                    f"{match.group(1)}"
                    f"{prefix}/"
                ),
                text_body,
            )

            content = text_body.encode(
                "utf-8"
            )

        except Exception:

            logger.exception(
                "Failed to rewrite preview "
                "asset for %s",
                project_id,
            )

    # --------------------------------------------------------
    # Return response
    # --------------------------------------------------------

    return Response(
        content=content,
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=None,
    )


# ============================================================
# CREATE PROJECT
# ============================================================

@router.post(
    "",
    response_model=ProjectResponse,
)
def create_project(
    req: CreateProjectRequest,
    session: Session = Depends(
        get_session
    ),
):

    if (
        not req.name.strip()
        or not req.prompt.strip()
    ):

        raise HTTPException(
            400,
            "name and prompt are required",
        )

    project = (
        project_service.create_project(
            session,
            req.name.strip(),
            req.prompt.strip(),
        )
    )

    return _serialize(
        session,
        project,
    )


# ============================================================
# GET PROJECT
# ============================================================

@router.get(
    "/{project_id}",
    response_model=ProjectResponse,
)
def get_project(
    project_id: str,
    session: Session = Depends(
        get_session
    ),
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

    return _serialize(
        session,
        project,
    )


# ============================================================
# GENERATE
# ============================================================

@router.post(
    "/{project_id}/generate",
    response_model=ProjectResponse,
)
async def generate(
    project_id: str,
    session: Session = Depends(
        get_session
    ),
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

    asyncio.create_task(
        _run_full_pipeline(
            project_id
        )
    )

    project_service.set_status(
        session,
        project,
        ProjectStatus.GENERATING,
    )

    return _serialize(
        session,
        project,
    )


# ============================================================
# EDIT
# ============================================================

@router.post(
    "/{project_id}/edit",
    response_model=ProjectResponse,
)
async def edit(
    project_id: str,
    req: EditRequest,
    session: Session = Depends(
        get_session
    ),
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

    if not req.message.strip():

        raise HTTPException(
            400,
            "message is required",
        )

    async def _edit_pipeline():

        from app.core.db import engine

        from sqlmodel import (
            Session as SQLSession
        )

        with SQLSession(
            engine
        ) as edit_session:

            current_project = (
                project_service.get_project(
                    edit_session,
                    project_id,
                )
            )

            if current_project is None:
                return

            try:

                try:

                    await project_service.edit_website(
                        edit_session,
                        current_project,
                        req.message.strip(),
                    )

                except Exception:

                    return

                edit_session.refresh(
                    current_project
                )

                ok = (
                    await build_service
                    .build_with_autofix(
                        edit_session,
                        current_project,
                    )
                )

                if not ok:
                    return

                edit_session.refresh(
                    current_project
                )

                await preview_service.start_preview(
                    edit_session,
                    current_project,
                )

            except Exception as e:

                logger.exception(
                    "Unexpected error in edit "
                    "pipeline for %s",
                    project_id,
                )

                await event_bus.publish(
                    project_id,
                    "ai_edit_failed",
                    {
                        "error":
                        f"Unexpected error: {e}"
                    },
                )

    asyncio.create_task(
        _edit_pipeline()
    )

    return _serialize(
        session,
        project,
    )


# ============================================================
# LIST PROJECTS
# ============================================================

@router.get(
    "",
    response_model=list[ProjectResponse],
)
def list_projects(
    session: Session = Depends(
        get_session
    ),
):

    from sqlmodel import select

    projects = session.exec(
        select(Project)
        .order_by(
            Project.created_at.desc()
        )
    ).all()

    return [
        _serialize(
            session,
            project,
        )
        for project in projects
    ]
from app.services.llm import LLMAllProvidersFailedError

logger = logging.getLogger("api.projects")
router = APIRouter(prefix="/api/projects", tags=["projects"])


class CreateProjectRequest(BaseModel):
    name: str
    prompt: str


class ProjectResponse(BaseModel):
    id: str
    name: str
    prompt: str
    status: str
    error_message: str | None = None
    preview_url: str | None = None
    created_at: str
    updated_at: str


class EditRequest(BaseModel):
    message: str


def _serialize(session: Session, project: Project) -> ProjectResponse:
    preview = preview_service.get_preview(session, project.id)
    return ProjectResponse(
        id=project.id,
        name=project.name,
        prompt=project.prompt,
        status=project.status,
        error_message=project.error_message,
        preview_url=preview.url if preview else None,
        created_at=project.created_at.isoformat(),
        updated_at=project.updated_at.isoformat(),
    )


async def _run_full_pipeline(project_id: str):
    """Generate -> build (with autofix) -> start preview. Runs as a background task."""
    from app.core.db import engine
    from app.core.events import event_bus
    from sqlmodel import Session as SQLSession

    with SQLSession(engine) as session:
        project = project_service.get_project(session, project_id)
        if project is None:
            return
        try:
            try:
                await project_service.generate_website(session, project)
            except (LLMAllProvidersFailedError, ValueError):
                # already logged, status set, and an event published inside
                # generate_website itself - nothing more to do here.
                return

            session.refresh(project)
            ok = await build_service.build_with_autofix(session, project)
            if not ok:
                return

            session.refresh(project)
            await preview_service.start_preview(session, project)
        except Exception as e:
            # Catch-all so an unexpected bug (e.g. in the build/preview
            # services) can never leave a project stuck in GENERATING /
            # BUILDING forever with no terminal event fired.
            logger.exception("Unexpected error in generation pipeline for %s", project_id)
            project_service.set_status(
                session, project, ProjectStatus.FAILED, f"Unexpected error: {e}"
            )
            await event_bus.publish(project_id, "generation_failed", {"error": f"Unexpected error: {e}"})


@router.post("", response_model=ProjectResponse)
def create_project(req: CreateProjectRequest, session: Session = Depends(get_session)):
    if not req.name.strip() or not req.prompt.strip():
        raise HTTPException(400, "name and prompt are required")
    project = project_service.create_project(session, req.name.strip(), req.prompt.strip())
    return _serialize(session, project)


@router.get("/{project_id}", response_model=ProjectResponse)
def get_project(project_id: str, session: Session = Depends(get_session)):
    project = project_service.get_project(session, project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    return _serialize(session, project)


@router.post("/{project_id}/generate", response_model=ProjectResponse)
async def generate(project_id: str, session: Session = Depends(get_session)):
    project = project_service.get_project(session, project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    asyncio.create_task(_run_full_pipeline(project_id))
    project_service.set_status(session, project, ProjectStatus.GENERATING)
    return _serialize(session, project)


@router.post("/{project_id}/edit", response_model=ProjectResponse)
async def edit(project_id: str, req: EditRequest, session: Session = Depends(get_session)):
    project = project_service.get_project(session, project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    if not req.message.strip():
        raise HTTPException(400, "message is required")

    async def _edit_pipeline():
        from app.core.db import engine
        from app.core.events import event_bus
        from sqlmodel import Session as SQLSession
        with SQLSession(engine) as s:
            p = project_service.get_project(s, project_id)
            if p is None:
                return
            try:
                try:
                    await project_service.edit_website(s, p, req.message.strip())
                except Exception:
                    # ai_edit_failed already published inside edit_website
                    return
                s.refresh(p)
                ok = await build_service.build_with_autofix(s, p)
                if not ok:
                    return
                s.refresh(p)
                await preview_service.start_preview(s, p)
            except Exception as e:
                logger.exception("Unexpected error in edit pipeline for %s", project_id)
                await event_bus.publish(project_id, "ai_edit_failed", {"error": f"Unexpected error: {e}"})

    asyncio.create_task(_edit_pipeline())
    return _serialize(session, project)


@router.get("", response_model=list[ProjectResponse])
def list_projects(session: Session = Depends(get_session)):
    from sqlmodel import select
    projects = session.exec(select(Project).order_by(Project.created_at.desc())).all()
    return [_serialize(session, p) for p in projects]
