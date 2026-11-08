from fastapi import APIRouter

from app.api.routes.family_context import router as family_context_router

from app.api.routes import ai, auth, families, grounded_answering, multimodal, processing, retrieval, uploads
from app.api.routes import system
from app.api.routes.records import router as records_router


api_router = APIRouter()
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(families.router, prefix="/families", tags=["families"])
api_router.include_router(uploads.router, prefix="/uploads", tags=["uploads"])
api_router.include_router(ai.router, prefix="/ai", tags=["ai"])
api_router.include_router(system.router, prefix="/system", tags=["system"])
api_router.include_router(processing.router, tags=["processing"])
api_router.include_router(retrieval.router, tags=["retrieval"])
api_router.include_router(grounded_answering.router, tags=["grounded-answering"])
api_router.include_router(records_router, tags=["records"])


api_router.include_router(family_context_router, tags=["family-context"])

api_router.include_router(multimodal.router, tags=["multimodal"])
