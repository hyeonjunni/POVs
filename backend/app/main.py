from pathlib import Path

from fastapi import FastAPI

from app.core.config import settings
from app.core.database import close_connector, engine


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="Ambient Digital Legacy backend scaffold",
)

from app.services.record_audit import denied_record_response
from app.services.record_policy import RecordAccessDenied

app.add_exception_handler(RecordAccessDenied, denied_record_response)

MEDIA_ROOT = Path(__file__).resolve().parent / 'storage'
UPLOADS_ROOT = MEDIA_ROOT / 'uploads'
if settings.security_mode == "LEGACY":
    UPLOADS_ROOT.mkdir(parents=True, exist_ok=True)


# Startup performs no DDL and no data mutation: schema changes are an explicit
# deploy-time step (`python backend/scripts/adopt_alembic.py`), never a side
# effect of the app booting (FOUNDATION_IDENTITY_MIGRATION_DECISION.md D3).
@app.on_event("shutdown")
def on_shutdown():
    runtime = getattr(app.state, "secure_runtime", None)
    if runtime is not None and runtime.worker_lock is not None:
        runtime.worker_lock.release()
    close_connector()


@app.on_event("startup")
def on_startup():
    from app.services.secure_runtime import validate_catalogue_mode, startup_runtime
    from app.core.config import validate_secure_config
    validate_secure_config(settings)
    validate_catalogue_mode(engine, settings.security_mode)
    if settings.security_mode == "SECURE":
        app.state.secure_runtime = startup_runtime(engine, settings)


if settings.security_mode == "LEGACY":
    from app.api.router import api_router
    app.include_router(api_router, prefix=settings.api_prefix)
else:
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse

    @app.exception_handler(RequestValidationError)
    async def secure_validation_error(request, exc):
        return JSONResponse({"detail": "Invalid request"}, status_code=422)

    # B2 is an intermediate boundary: B3/B4/B5 explicitly register secure routes.
    # Importing the legacy router also creates a plaintext upload directory.
    class SecureMiddleware:
        def __init__(self, app):
            self.inner = app

        async def __call__(self, scope, receive, send):
            from app.services.secure_runtime import SecureBoundary, SecureRuntime, SecureUnavailable
            if scope["type"] == "lifespan":
                return await self.inner(scope, receive, send)
            runtime = getattr(app.state, "secure_runtime", None)
            if runtime is None:
                raise SecureUnavailable()
            return await SecureBoundary(self.inner, runtime)(scope, receive, send)

    app.add_middleware(SecureMiddleware)

    from app.api.routes.secure_operations import router as secure_router
    app.include_router(secure_router, prefix=settings.api_prefix)
    from app.api.routes.secure_media import router as secure_media_router
    app.include_router(secure_media_router, prefix=settings.api_prefix)
    from app.api.routes.multimodal import router as multimodal_router
    app.include_router(multimodal_router, prefix=settings.api_prefix)
    from app.api.routes.retrieval import secure_router as secure_retrieval_router
    from app.api.routes.grounded_answering import secure_router as secure_answer_router
    app.include_router(secure_retrieval_router, prefix=settings.api_prefix)
    app.include_router(secure_answer_router, prefix=settings.api_prefix)
    from app.api.routes.records import router as records_router
    app.include_router(records_router, prefix=settings.api_prefix)
    from app.api.routes.secure_family_context import router as secure_context_router
    app.include_router(secure_context_router, prefix=settings.api_prefix)

    @app.api_route('/{path:path}', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'])
    def secure_not_implemented(path: str):
        from fastapi import HTTPException
        raise HTTPException(503, "secure_request_unavailable", headers={"Retry-After": "2"})


@app.get("/")
def read_root():
    return {
        "service": settings.app_name,
        "status": "ok",
    }


# Opt-in, outermost metadata observer.
from app.core.demo_trace import DemoTraceMiddleware
app.add_middleware(DemoTraceMiddleware)
