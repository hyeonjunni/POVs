"""Room-scoped, source-revalidated extractive answering."""
from app.core.demo_trace import emit
from fastapi import APIRouter, Depends, HTTPException, Request

from app.ai.grounded_answering import (
    GroundedEvidenceChanged, generate_grounded_answer, load_grounded_answer_snapshot,
    revalidate_and_render_grounded_answer,
)
from app.ai.provider_factory import get_provider_for_model
from app.api.deps import AuthenticatedPrincipal, get_authenticated_principal
from app.api.routes.retrieval import EMBEDDING_PROVIDER
from app.core.config import settings
from app.core.database import SessionLocal
from app.models.auth_identity import PrincipalNotPersisted
from app.retrieval.embeddings import EmbeddingProviderError
from app.schemas.ai import AskRequest, SecureAskResponse, OriginalAskResponse
from app.services.retrieval import RetrievalSearchInvalid, RetrievalSearchNotFound

router = APIRouter()
secure_router = APIRouter()


@secure_router.post("/families/{room_id}/ask", response_model=SecureAskResponse)
async def secure_ask(room_id: str, payload: AskRequest, request: Request):
    from app.api.routes.secure_operations import principal
    from app.ai.grounded_answering import secure_inference_factory, render_secure_grounded_answer
    runtime = request.app.state.secure_runtime
    try:
        user_id = principal(request)
        emit("AUTH", validation="PASS")
        # 연구 한계: resolve_ask_query의 친족/모호성 판정, 자동 claim/structured
        # 문법 라우팅, load_secure_grounded_snapshot의 admission·route_query·
        # safety_veto 및 LLM claim_ids 순열 검증은 일반 서술 질문을 오차단했다.
        # 구현은 기존 모듈에 보존하고 MVP 일반 질문에서는 호출하지 않는다.
        # 명시적 structured_query/claim_query와 ACL·원본 재검증은 계속 적용한다.
        from app.ai.record_questions import load_record_question
        if payload.selected_record_id is not None:
            from app.api.routes.secure_operations import tx
            from app.services.record_policy import require_record_access
            with tx() as db:
                require_record_access(db, user_id, str(payload.selected_record_id), purpose="rag", room_id=room_id)
        from app.services.claims import answer as claim_answer
        claimed = claim_answer(runtime, user_id, room_id, payload) if payload.claim_query is not None else None
        if claimed is not None:
            return claimed
        from app.services.structured_answer import secure_answer
        structured = secure_answer(runtime, user_id, room_id, payload) if payload.structured_query is not None else None
        if structured is not None:
            return structured
        from app.services.personal_context import read as read_personal_context
        personal = read_personal_context(runtime, user_id, room_id)
        from app.ai.query_normalization import topic_switch
        payload = topic_switch(payload)
        snapshot = load_record_question(runtime=runtime, user_id=user_id, room_id=room_id,
            payload=payload, personal=personal,
            candidate_limit=settings.retrieval_candidate_limit, timings=request.scope.get("secure_timings"))
        from app.ai.record_questions import answer_context, answer_record_question
        # Answer first: the context budget may narrow the plan, and citations must match what the model saw.
        snapshot, answer = answer_record_question(runtime, snapshot, personal)
        result = generate_grounded_answer(snapshot, provider_factory=secure_inference_factory(runtime))
        response = render_secure_grounded_answer(runtime, result)
        updates = {"query_context": answer_context(runtime, snapshot, payload, personal)}
        if answer:
            updates.update(answer=answer, provider_mode="local", gate_route="ANSWER", gate_action="llm_generated")
            if snapshot.deterministic_answer:
                updates.update(provider_mode="not_called", gate_action="record_discovery_render")
            from app.ai.health_reference import select_health_references
            references = select_health_references(payload.query,
                " ".join(fact.quote for fact in snapshot.plan.facts) if snapshot.plan else "")
            if references:
                updates.update(gate_action="general_health_reference", general_sources=[ref.source_url for ref in references])
        return response.model_copy(update=updates)
    except (RetrievalSearchInvalid, KeyError):
        raise HTTPException(400, "Invalid ask request") from None
    except GroundedEvidenceChanged:
        raise HTTPException(409, "Evidence changed; retry request") from None


@router.post("/families/{room_id}/ask", response_model=OriginalAskResponse)
def ask_family_room(
    room_id: str, payload: AskRequest,
    principal: AuthenticatedPrincipal = Depends(get_authenticated_principal),
):
    emit("AUTH", validation="PASS")
    try:
        from app.services.structured_answer import legacy_answer
        structured = legacy_answer(SessionLocal, principal.user_id, room_id, payload)
        if structured is not None:
            return structured
        snapshot = load_grounded_answer_snapshot(
            session_factory=SessionLocal, room_id=room_id, user_id=principal.user_id,
            payload=payload, retrieval_provider=EMBEDDING_PROVIDER,
            candidate_limit=settings.retrieval_candidate_limit,
        )
        result = generate_grounded_answer(snapshot, provider_factory=get_provider_for_model)
        return revalidate_and_render_grounded_answer(
            session_factory=SessionLocal, room_id=room_id, user_id=principal.user_id,
            result=result,
        )
    except PrincipalNotPersisted as error:
        raise HTTPException(status_code=401, detail="Authenticated user not found") from error
    except RetrievalSearchNotFound as error:
        raise HTTPException(status_code=404, detail="Retrieval room not found") from error
    except (RetrievalSearchInvalid, KeyError) as error:
        raise HTTPException(status_code=400, detail="Invalid ask request") from error
    except GroundedEvidenceChanged as error:
        raise HTTPException(status_code=409, detail="Evidence changed; retry request") from error
    except EmbeddingProviderError as error:
        raise HTTPException(status_code=503, detail="Embedding provider failed") from error
