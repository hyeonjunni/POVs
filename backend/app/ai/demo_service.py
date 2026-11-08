from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.provider_factory import get_provider_for_model
from app.ai.providers.base import InferenceRequest
from app.ai.persona_loader import load_persona_pack
from app.ai.prompt_builder import RetrievalChunk, build_prompt_package
from app.ai.gates.answer_router import CLARIFY_TMPL, RouteDecision, route_query
from app.ai.gates.entity_index import KINSHIP, RoomEntityIndex, build_room_entity_index
from app.ai.gates.output_gate import apply_output_gate
from app.ai.gates.textrules import fold_family_terms
from app.models.auth_identity import require_persisted_principal
from app.models.family import FamilyMember
from app.models.upload import Upload
from app.models.user import User

if TYPE_CHECKING:
    from app.api.deps import AuthenticatedPrincipal
    from app.schemas.ai import AIDemoChatRequest


PERSONA_ROOT = Path(__file__).resolve().parents[1] / "personas"


PERSONA_PACKS = {
    "father-calm": PERSONA_ROOT / "father-calm",
    "mother-warm": PERSONA_ROOT / "mother-warm",
    "grandfather-mentor": PERSONA_ROOT / "grandfather-mentor",
}

PERSONA_SUMMARIES = {
    "father-calm": {
        "label": "아버지 페르소나",
        "tone": "차분하고 핵심만 짚는 조언형",
    },
    "mother-warm": {
        "label": "어머니 페르소나",
        "tone": "정서적 공감과 생활 맥락을 살리는 대화형",
    },
    "grandfather-mentor": {
        "label": "할아버지 페르소나",
        "tone": "회고와 조언이 섞인 회상형",
    },
}

QUERY_STOP_TERMS = {
    "db",
    "가족방",
    "기록",
    "관련",
    "내용",
    "찾아",
    "요약",
    "요약해줘",
    "알려줘",
    "답해줘",
    "있으면",
    "없으면",
    "정확한",
    "이름",
    "2문장",
    "2문장으로",
}


def ensure_room_member(*args, **kwargs):
    from app.api.routes.uploads import ensure_room_member as legacy
    return legacy(*args, **kwargs)


def unpack_description_and_tags(value):
    from app.api.routes.uploads import unpack_description_and_tags as legacy
    return legacy(value)


def secure_room_entity_sources(room, records):
    """Only authenticated copies in this room; never another member's user profile."""
    names = [str(p[k]) for p in room["members"].values()
             for k in ("name", "relation_to_related_user") if p.get(k)]
    texts = [" ".join([r.get("title") or "", r.get("description") or "", " ".join(r.get("tags", []))]).strip()
             for r in records]
    return names, texts


def normalize_term(term: str) -> str:
    term = term.strip()
    suffixes = ["에서", "으로", "에게", "께서", "과", "와", "은", "는", "이", "가", "을", "를", "도", "만", "의", "에"]
    for suffix in suffixes:
        if term.endswith(suffix) and len(term) > len(suffix) + 1:
            return term[: -len(suffix)]
    return term


def extract_search_terms(query: str) -> set[str]:
    query_terms: set[str] = set()
    # 가족 호칭·경어 변형(어머니/엄마, 생신/생일)은 대표형으로 접어 검색한다 (UX-G01)
    normalized_query = fold_family_terms(query)
    for punctuation in ["?", ",", ".", "!", ":", ";", "/", "\\", "(", ")", "[", "]"]:
        normalized_query = normalized_query.replace(punctuation, " ")

    for raw_term in normalized_query.split():
        term = normalize_term(raw_term)
        if len(term) < 2 or term.casefold() in QUERY_STOP_TERMS:
            continue
        query_terms.add(term)
    return query_terms


def load_selected_persona_markdown(persona_id: str) -> str:
    persona_root = PERSONA_PACKS.get(persona_id)
    if persona_root is None:
        raise KeyError(f"Unknown persona id: {persona_id}")
    return load_persona_pack(persona_root)


def list_persona_options() -> list[dict]:
    return [
        {
            "id": persona_id,
            "label": PERSONA_SUMMARIES[persona_id]["label"],
            "tone": PERSONA_SUMMARIES[persona_id]["tone"],
        }
        for persona_id in PERSONA_PACKS.keys()
    ]


def retrieve_room_chunks(db: Session, room_id: str, user_id: str, query: str, limit: int = 3) -> list[RetrievalChunk]:
    ensure_room_member(db, room_id, user_id)
    from app.services.record_policy import allowed_record_ids
    authorized = allowed_record_ids(db, user_id, room_id, purpose="rag")

    uploads = db.scalars(
        select(Upload)
        .where(Upload.room_id == room_id, Upload.id.in_(authorized))
        .order_by(Upload.created_at.desc(), Upload.id.desc())
    ).all()

    query_terms = extract_search_terms(query)

    scored: list[tuple[int, Upload]] = []
    for upload in uploads:
        clean_description, upload_tags = unpack_description_and_tags(upload.description)
        haystack = fold_family_terms(" ".join(
            [
                upload.title or "",
                clean_description or "",
                upload.type or "",
                " ".join(upload_tags),
            ]
        ))
        score = sum(1 for term in query_terms if term and term in haystack)
        if score > 0:
            scored.append((score, upload))

    scored.sort(key=lambda item: item[0], reverse=True)
    selected = scored[:limit]

    # confidence = 실제 커버리지(질의 내용어 중 매치 비율). 하드코드 0.75는 허구였다.
    denom = max(len(query_terms), 1)
    return [
        RetrievalChunk(
            source_type=upload.type,
            timestamp=upload.created_at.isoformat() if upload.created_at else "",
            text=f"{upload.title} {(unpack_description_and_tags(upload.description)[0] or '')}".strip(),
            tags=[upload.type, "family-room", *unpack_description_and_tags(upload.description)[1]],
            confidence=round(score / denom, 2),
        )
        for score, upload in selected
    ]


def load_room_entity_sources(db: Session, room_id: str, *, user_id: str) -> tuple[list[str], list[str]]:
    """가족방 엔티티 사전 재료 — 멤버(이름+관계 호칭)는 DB에서, 장소는 방 전체 기록에서."""
    member_rows = db.execute(
        select(User.name, FamilyMember.relation_to_related_user)
        .join(FamilyMember, FamilyMember.user_id == User.id)
        .where(FamilyMember.room_id == room_id)
    ).all()
    member_names: list[str] = []
    for name, relation in member_rows:
        if name:
            member_names.append(str(name))
        if relation:
            member_names.append(str(relation))

    from app.services.record_policy import allowed_record_ids
    authorized = allowed_record_ids(db, user_id, room_id, purpose="rag")
    uploads = db.scalars(select(Upload).where(Upload.room_id == room_id, Upload.id.in_(authorized))).all()
    record_texts = []
    for upload in uploads:
        clean_description, upload_tags = unpack_description_and_tags(upload.description)
        record_texts.append(" ".join(
            [upload.title or "", clean_description or "", " ".join(upload_tags)]).strip())
    return member_names, record_texts


# Phase 0: 프로바이더 실패는 '답변인 척'하지 않는다 — 실패를 밝히고 기록 원문만 보여준다.
PROVIDER_DOWN_TMPL = ("죄송합니다. 지금 모델 응답을 받지 못해 새 답변을 만들 수 없습니다. "
                      "지어내는 대신 남아 있는 기록 원문을 그대로 보여드립니다: {quotes}")

# Phase 6 (UX-G02): 막연한 되묻기 대신 고를 수 있는 후보 기록을 제시한다 — 무상태.
# 후보는 이 가족방의 최근 기록 제목만 사용한다 (다른 방 데이터 누출 없음).
CLARIFY_CANDIDATES_TMPL = ("어떤 기록을 말씀하시는지 조금 더 구체적으로 알려주시면 "
                           "그 기록을 기준으로 답하겠습니다. 최근 기록 중에는 {candidates} "
                           "이야기가 있어요 — 이 중 하나라면 그 이름으로 다시 물어봐 주세요.")


def load_clarify_options(db: Session, room_id: str, query: str,
                         limit: int = 3, *, user_id: str) -> list[dict]:
    """CLARIFY 후보 — 단순 최신순이 아니라 질의 관련도 우선 (감사 D / UX-A08).

    관련도: 접기 후 내용어의 양방향 부분일치('바다여행' ⊃ '여행')로 점수화.
    관련 신호가 전혀 없으면 최신순으로 자연 강등된다. 후보는 안정적인 record id와
    label로 구성한다 (공개 API에는 무파괴 추가 필드 clarify_options로 노출).
    """
    from app.ai.gates.textrules import content_tokens
    from app.services.record_policy import allowed_record_ids
    authorized = allowed_record_ids(db, user_id, room_id, purpose="rag")

    uploads = db.scalars(
        select(Upload)
        .where(Upload.room_id == room_id, Upload.id.in_(authorized))
        .order_by(Upload.created_at.desc(), Upload.id.desc())
    ).all()
    query_tokens = content_tokens(fold_family_terms(query))
    scored: list[tuple[int, int, str, str, str, object]] = []
    for position, upload in enumerate(uploads):
        title = (upload.title or "").strip()
        if not title:
            continue
        clean_description, _tags = unpack_description_and_tags(upload.description)
        record_tokens = content_tokens(fold_family_terms(f"{title} {clean_description or ''}"))
        score = sum(1 for qt in query_tokens for rt in record_tokens
                    if qt == rt or qt in rt or rt in qt)
        scored.append((score, -position, upload.id, title,
                       clean_description or "", upload.created_at))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    picked = scored[:limit]

    # 같은 제목 기록도 각각 선택 가능해야 한다 (감사 10-4) — 기록을 버리는 대신
    # label에 사용자가 구분할 최소 정보를 붙인다: 기록 속 인물 → 저장일 → 번호 순.
    title_counts: dict[str, int] = {}
    for _s, _p, _id, title, _d, _c in picked:
        title_counts[title] = title_counts.get(title, 0) + 1
    options: list[dict] = []
    seen_labels: set[str] = set()
    for ordinal, (_score, _pos, upload_id, title, description, created_at) in enumerate(picked, 1):
        label = title
        if title_counts[title] > 1:
            persons = sorted(k for k in KINSHIP
                             if k in fold_family_terms(f"{title} {description}"))
            if persons:
                label = f"{title} — {persons[0]}"
            elif created_at:
                label = f"{title} — {created_at.month}월 {created_at.day}일 저장"
        while label in seen_labels:
            label = f"{label} ({ordinal})"
        seen_labels.add(label)
        options.append({"record_id": upload_id, "label": label})
    return options


def _quote_snippets(evidence_texts: list[str], limit: int = 2) -> str:
    return " / ".join(f"「{t[:42]}…」" if len(t) > 42 else f"「{t}」"
                      for t in evidence_texts[:limit]) or "「저장된 기록 없음」"


@dataclass(frozen=True)
class DemoChatSnapshot:
    """Everything `generate_demo_chat_response` needs, with no DB session attached.

    Produced by `load_demo_chat_snapshot`/`_gather_demo_chat_snapshot` inside a
    request-scoped session that closes before this snapshot is used (D5).
    """

    room_id: str
    model_id: str
    query: str
    effective_query: str
    decision: RouteDecision
    prompt_package: dict
    evidence_texts: tuple[str, ...]
    entity_index: RoomEntityIndex
    clarify_candidates: tuple[str, ...] = ()
    clarify_options: tuple[dict, ...] = ()


def _gather_demo_chat_snapshot(
    db: Session,
    *,
    room_id: str,
    user_id: str,
    model_id: str,
    persona_id: str,
    query: str,
    selected_record_id: str | None = None,
) -> DemoChatSnapshot:
    require_persisted_principal(db, user_id)   # A6: covers load_demo_chat_snapshot and the compat wrapper
    persona_markdown = load_selected_persona_markdown(persona_id)
    if selected_record_id:
        # 감사 9-4: 후보 선택 — 사용자가 고른 record_id를 근거로 고정한다 (무상태 연속).
        # 멤버십(내가 이 방 구성원인가)과 소속(이 기록이 이 방 것인가)을 모두 검증 —
        # 타 방 record_id로는 존재 여부조차 새어나가면 안 된다 (LookupError → 404).
        ensure_room_member(db, room_id, user_id)
        from app.services.record_policy import require_record_access
        require_record_access(db, user_id, selected_record_id, purpose="rag", room_id=room_id)
        selected_upload = db.get(Upload, selected_record_id)
        if selected_upload is None or selected_upload.room_id != room_id:
            raise LookupError("selected record is not in this room")
        clean_description, upload_tags = unpack_description_and_tags(selected_upload.description)
        chunks = [RetrievalChunk(
            source_type=selected_upload.type,
            timestamp=selected_upload.created_at.isoformat() if selected_upload.created_at else "",
            text=f"{selected_upload.title} {clean_description or ''}".strip(),
            tags=[selected_upload.type, "family-room", *upload_tags],
            confidence=1.0,
        )]
    else:
        chunks = retrieve_room_chunks(db=db, room_id=room_id, user_id=user_id, query=query)
    prompt_package = build_prompt_package(
        model_id=model_id,
        persona_markdown=persona_markdown,
        user_query=query,
        retrieved_chunks=chunks,
    )
    evidence_texts = [chunk.text for chunk in chunks]
    member_names, record_texts = load_room_entity_sources(db, room_id, user_id=user_id)
    entity_index = build_room_entity_index(member_names, record_texts)

    # ── 입력 래더 (Phase 2): 기록으로 답 못 하는 질문은 LLM을 부르지 않는다 ──
    effective_query = query
    decision = route_query(effective_query, evidence_texts, entity_index)

    # 방에 기록은 있는데 '검색'만 0건이면 NO_RECORD가 아니라 CLARIFY가 맞는 메시지다.
    if decision.route == "NO_RECORD" and record_texts:
        decision = RouteDecision(route="CLARIFY", answer=CLARIFY_TMPL, detail="retrieval_miss")

    # 감사 9-2: CLARIFY 경로의 LLM 재작성을 제거했다 — 오타·표기는 라우터·검색의 결정론
    # 정규화(CANONICAL_FOLDS·가족 어휘 접기·무공백·자모오타)가 이미 처리하고, 엄격한
    # 의도 보존 가드를 통과하는 재작성은 결국 표기 복원뿐이라 LLM 왕복은 비용(호출 1회)과
    # 하이재킹 표면만 남는다. 의미 보존을 증명할 수 없는 재작성은 채택하지 않는 것이
    # 계약이며(rewrite_preserves_intent, 재도입 시 필수 가드), CLARIFY는 결정론 후보
    # 제시(clarify_options)로 해소한다 — provider 호출 0회.
    clarify_options: list[dict] = []
    clarify_candidates: list[str] = []
    # 막연한 CLARIFY는 실행형으로: 고를 수 있는 기록 후보를 붙인다 (UX-G02).
    # 후보는 질의 관련도 우선 정렬 (UX-A08). 인물/사건 모호는 이미 후보를 제시함.
    if decision.route == "CLARIFY" and decision.detail not in {"ambiguous_person", "ambiguous_event"}:
        clarify_options = load_clarify_options(db, room_id, effective_query, user_id=user_id)
        clarify_candidates = [option["label"] for option in clarify_options]

    return DemoChatSnapshot(
        room_id=room_id,
        model_id=model_id,
        query=query,
        effective_query=effective_query,
        decision=decision,
        prompt_package=prompt_package,
        evidence_texts=tuple(evidence_texts),
        entity_index=entity_index,
        clarify_candidates=tuple(clarify_candidates),
        clarify_options=tuple(clarify_options),
    )


def load_demo_chat_snapshot(
    session_factory: Callable[[], Session],
    principal: "AuthenticatedPrincipal",
    payload: "AIDemoChatRequest",
) -> DemoChatSnapshot:
    """Transaction A: open a session, do every auth/ACL/evidence/locator read, close it.

    No storage/OCR/STT/model call may run while this session is open (D5) — this
    function's only job is to produce an immutable snapshot and close its session,
    even when it raises (e.g. `LookupError` for a cross-room `selected_record_id`).
    """
    db = session_factory()
    try:
        return _gather_demo_chat_snapshot(
            db,
            room_id=payload.room_id,
            user_id=principal.user_id,
            model_id=payload.model_id,
            persona_id=payload.persona_id,
            query=payload.query,
            selected_record_id=payload.selected_record_id,
        )
    finally:
        db.close()


def generate_demo_chat_response(snapshot: DemoChatSnapshot) -> dict:
    """Transaction A is already closed by the time this runs — no `Session` parameter."""
    decision = snapshot.decision
    prompt_package = snapshot.prompt_package
    evidence_texts = list(snapshot.evidence_texts)

    if decision.route != "ANSWER":
        answer = decision.answer
        clarify_candidates = list(snapshot.clarify_candidates)
        if decision.route == "CLARIFY" and clarify_candidates:
            answer = CLARIFY_CANDIDATES_TMPL.format(
                candidates=" / ".join(f"「{t}」" for t in clarify_candidates))
        return {
            "answer": answer,
            "answer_source": "rule_gate",
            "gate_route": decision.route,
            "gate_detail": decision.detail,
            "gate_action": "llm_not_called",
            "inference_source": "rule_gate",
            "provider_name": "rule_gate",
            "provider_mode": "rule_gate",
            "selected_model": prompt_package["model_profile"],
            # 공개 evidence는 가족 가독형 기록 원문만 — 내부 메타(source/saved_at/
            # tags/confidence)는 프롬프트 전용이다 (UX-G11/M02)
            "retrieved_evidence": evidence_texts,
            "clarify_candidates": clarify_candidates,
            "clarify_options": list(snapshot.clarify_options),
        }

    provider = get_provider_for_model(snapshot.model_id)
    request = InferenceRequest(
        model_id=snapshot.model_id,
        user_query=snapshot.effective_query,
        prompt_package=prompt_package,
    )
    provider_response = provider.generate(request)
    # 소형 모델 빈응답 완화: 정상 모드인데 출력이 비면 1회 재생성 (v3 실측: e4b 빈응답 0.37→0)
    if provider_response.mode not in {"mock", "unconfigured", "error"} and \
            len((provider_response.output_text or "").strip()) < 5:
        provider_response = provider.generate(request)
    if provider_response.mode in {"mock", "unconfigured", "error"}:
        # Phase 0: 실패를 밝힌다 — 데모 설명문을 답변인 척 내보내지 않는다
        answer = PROVIDER_DOWN_TMPL.format(quotes=_quote_snippets(evidence_texts))
        answer_source = "fallback"
        gate_action = "provider_unavailable"
    else:
        # ── 출력 게이트 (Phase 1): 누수 제거 + 문장 단위 근거·엔티티 대조 ──
        gate = apply_output_gate(provider_response.output_text or "", snapshot.effective_query,
                                 evidence_texts, snapshot.entity_index)
        answer = gate.answer
        answer_source = "provider+gate"
        gate_action = gate.action
    return {
        "answer": answer,
        "answer_source": answer_source,
        "gate_route": decision.route,
        "gate_detail": decision.detail,
        "gate_action": gate_action,
        "inference_source": prompt_package["model_profile"]["placement"],
        "provider_name": provider_response.provider,
        "provider_mode": provider_response.mode,
        "selected_model": prompt_package["model_profile"],
        "retrieved_evidence": evidence_texts,
    }


def build_demo_chat_response(
    *,
    session_factory: Callable[[], Session],
    room_id: str,
    user_id: str,
    model_id: str,
    persona_id: str,
    query: str,
    selected_record_id: str | None = None,
) -> dict:
    """Single-call script form that closes Transaction A before generation."""
    db = session_factory()
    try:
        snapshot = _gather_demo_chat_snapshot(
            db,
            room_id=room_id,
            user_id=user_id,
            model_id=model_id,
            persona_id=persona_id,
            query=query,
            selected_record_id=selected_record_id,
        )
    finally:
        db.close()
    return generate_demo_chat_response(snapshot)
