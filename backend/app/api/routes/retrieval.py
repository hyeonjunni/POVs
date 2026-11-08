import json

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.deps import AuthenticatedPrincipal, get_authenticated_principal
from app.core.config import settings
from app.core.database import SessionLocal
from app.models.auth_identity import PrincipalNotPersisted
from app.models.retrieval import M2_EMBEDDING_DIMENSIONS
from app.retrieval.chunking import RetrievalChunk
from app.retrieval.embeddings import EmbeddingProviderError, HTTPEmbeddingProvider
from app.schemas.retrieval import RetrievalIndexRequest, RetrievalIndexResponse, RetrievalSearchRequest, RetrievalSearchResponse
from app.services.retrieval import RetrievalSearchInvalid, RetrievalSearchNotFound, search_room
from app.services.retrieval_indexing import (
    RetrievalIndexNotFound,
    RetrievalIndexNotReady,
    RetrievalIndexStale,
    index_processing_result,
)

router = APIRouter()
secure_router = APIRouter()

EMBEDDING_PROVIDER = HTTPEmbeddingProvider(
    endpoint_url=settings.retrieval_embedding_endpoint_url,
    model_key=settings.retrieval_embedding_model_key,
    dimensions=M2_EMBEDDING_DIMENSIONS,
    batch_size=settings.retrieval_embedding_batch_size,
    timeout_seconds=settings.retrieval_embedding_timeout_seconds,
) if settings.security_mode == "LEGACY" else None


@router.post("/families/{room_id}/retrieval/search", response_model=RetrievalSearchResponse)
def search_family_room(
    room_id: str,
    payload: RetrievalSearchRequest,
    principal: AuthenticatedPrincipal = Depends(get_authenticated_principal),
):
    try:
        hits = search_room(
            room_id=room_id, user_id=principal.user_id, query=payload.query, arm=payload.arm,
            k=payload.k, provider=EMBEDDING_PROVIDER, session_factory=SessionLocal,
            candidate_limit=settings.retrieval_candidate_limit,
        )
        from app.services.record_policy import allowed_record_ids
        from app.services.retrieval import chunk_is_current
        with SessionLocal() as db:
            authorized = set(allowed_record_ids(db, principal.user_id, room_id, purpose="rag"))
            if any(not isinstance(hit.chunk, RetrievalChunk) or hit.chunk.record_id not in authorized or not chunk_is_current(db, hit.chunk, room_id) for hit in hits):
                raise HTTPException(409, "Retrieval evidence changed")
        return RetrievalSearchResponse(
            arm=payload.arm,
            model_key=EMBEDDING_PROVIDER.model_key if payload.arm in {"vector", "hybrid_rrf"} else None,
            hits=[
                {
                    "chunk_id": hit.chunk.id, "record_id": hit.chunk.record_id, "file_id": hit.chunk.file_id,
                    "text": hit.chunk.text, "source_locator": json.loads(hit.chunk.source_locator_json),
                    "rank": rank, "scores": {payload.arm: hit.score},
                }
                for rank, hit in enumerate(hits, start=1)
            ],
        )
    except PrincipalNotPersisted as error:
        raise HTTPException(status_code=401, detail="Authenticated user not found") from error
    except RetrievalSearchNotFound as error:
        raise HTTPException(status_code=404, detail="Retrieval room not found") from error
    except RetrievalSearchInvalid as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except EmbeddingProviderError as error:
        raise HTTPException(status_code=503, detail="Embedding provider failed") from error


@secure_router.post("/families/{room_id}/retrieval/search", response_model=RetrievalSearchResponse)
async def secure_search(room_id: str, payload: RetrievalSearchRequest, request: Request):
    from app.api.routes.secure_operations import principal
    from app.services.retrieval import search_secure_room
    runtime = request.app.state.secure_runtime
    try:
        hits, _ = search_secure_room(runtime=runtime, user_id=principal(request), room_id=room_id,
            query=payload.query, arm=payload.arm, k=payload.k, candidate_limit=settings.retrieval_candidate_limit,
            timings=request.scope.get("secure_timings"))
        return RetrievalSearchResponse(arm=payload.arm,
            model_key=runtime.embedding_provider.model_key if payload.arm in {"vector", "hybrid_rrf"} else None,
            hits=[dict(chunk_id=h.chunk.id, record_id=h.chunk.record_id, file_id=h.chunk.file_id,
                text=h.chunk.text, source_locator=json.loads(h.chunk.source_locator_json), rank=i, scores={payload.arm: h.score})
                for i, h in enumerate(hits, 1)])
    except RetrievalSearchInvalid:
        raise HTTPException(400, "Invalid search request") from None


@secure_router.post("/records/{record_id}/retrieval-index", response_model=RetrievalIndexResponse)
async def secure_index(record_id: str, payload: RetrievalIndexRequest, request: Request):
    from app.api.routes.secure_operations import principal
    from app.services.retrieval_indexing import index_secure_processing_result
    try:
        return index_secure_processing_result(runtime=request.app.state.secure_runtime, user_id=principal(request),
            record_id=record_id, processing_job_id=payload.processing_job_id)
    except RetrievalIndexNotReady:
        raise HTTPException(409, "Retrieval source or model changed") from None


@router.post("/records/{record_id}/retrieval-index", response_model=RetrievalIndexResponse)
def index_record(
    record_id: str,
    payload: RetrievalIndexRequest,
    principal: AuthenticatedPrincipal = Depends(get_authenticated_principal),
):
    try:
        return index_processing_result(
            record_id=record_id,
            processing_job_id=payload.processing_job_id,
            user_id=principal.user_id,
            provider=EMBEDDING_PROVIDER,
            session_factory=SessionLocal,
        )
    except PrincipalNotPersisted as error:
        raise HTTPException(status_code=401, detail="Authenticated user not found") from error
    except RetrievalIndexNotFound as error:
        raise HTTPException(status_code=404, detail="Retrieval source not found") from error
    except RetrievalIndexNotReady as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except RetrievalIndexStale as error:
        raise HTTPException(
            status_code=409, detail="Retrieval source changed during indexing"
        ) from error
    except EmbeddingProviderError as error:
        raise HTTPException(status_code=503, detail="Embedding provider failed") from error
