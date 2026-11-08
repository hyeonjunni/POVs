import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.database import get_db
from app.core.security import generate_invite_code
from app.models.family import FamilyMember, FamilyPerson, FamilyRelationship, FamilyRoom
from app.models.user import User
from app.schemas.family import (
    FamilyCreateRequest,
    FamilyDetailResponse,
    FamilyJoinRequest,
    FamilyJoinPreviewResponse,
    FamilyPersonCreateRequest,
    FamilyPersonResponse,
    FamilyPersonUpdateRequest,
    FamilyRelationshipCreateRequest,
    FamilyRelationshipResponse,
    KinshipResponse,
    FamilyMemberResponse,
    FamilyResponse,
)
from app.services.account_cleanup import delete_family_room_with_related_data
from app.services.family_graph import resolve_kinship


router = APIRouter()
VALID_MEMBER_RELATIONS = {"parent", "child", "spouse"}


def ensure_family_member(db: Session, room_id: str, user_id: str) -> FamilyMember:
    membership = db.scalar(
        select(FamilyMember).where(
            FamilyMember.room_id == room_id,
            FamilyMember.user_id == user_id,
            FamilyMember.is_active.is_(True),
        )
    )
    if membership is None:
        raise HTTPException(status_code=403, detail="User is not a member of this family room")
    return membership


def build_family_member_responses(db: Session, room_id: str) -> list[FamilyMemberResponse]:
    member_rows = db.execute(
        select(FamilyMember, User)
        .join(User, User.id == FamilyMember.user_id)
        .where(FamilyMember.room_id == room_id, FamilyMember.is_active.is_(True))
    ).all()

    member_name_map = {user.id: user.name for member, user in member_rows}

    return [
        FamilyMemberResponse(
            room_id=member.room_id,
            user_id=member.user_id,
            role=member.role,
            name=user.name,
            email=user.email,
            profile_image=user.profile_image,
            age=user.age,
            gender=user.gender,
            phone=user.phone,
            profile_chunk=user.profile_chunk,
            related_to_user_id=member.related_to_user_id,
            related_to_user_name=member_name_map.get(member.related_to_user_id),
            relation_to_related_user=member.relation_to_related_user,
        )
        for member, user in member_rows
    ]


@router.get("", response_model=list[FamilyResponse])
def list_user_families(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    memberships = db.execute(
        select(FamilyMember, FamilyRoom)
        .join(FamilyRoom, FamilyRoom.id == FamilyMember.room_id)
        .where(FamilyMember.user_id == current_user.id, FamilyMember.is_active.is_(True), FamilyRoom.is_active.is_(True))
    ).all()

    return [
        FamilyResponse(
            room_id=room.id,
            name=room.name,
            invite_code=room.invite_code,
            owner_user_id=room.owner_user_id,
        )
        for _member, room in memberships
    ]


@router.post("", response_model=FamilyResponse)
def create_family(payload: FamilyCreateRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    invite_code = generate_invite_code()
    while db.scalar(select(FamilyRoom).where(FamilyRoom.invite_code == invite_code)) is not None:
        invite_code = generate_invite_code()

    room = FamilyRoom(
        id=str(uuid.uuid4()),
        name=payload.name,
        invite_code=invite_code,
        owner_user_id=current_user.id,
    )
    db.add(room)
    db.flush()

    member = FamilyMember(
        id=str(uuid.uuid4()),
        room_id=room.id,
        user_id=current_user.id,
        role="owner",
    )
    db.add(member)
    db.commit()
    db.refresh(room)

    return FamilyResponse(
        room_id=room.id,
        name=room.name,
        invite_code=room.invite_code,
        owner_user_id=room.owner_user_id,
    )


@router.post("/join", response_model=FamilyResponse)
def join_family(payload: FamilyJoinRequest, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    room = db.scalar(select(FamilyRoom).where(FamilyRoom.invite_code == payload.invite_code.upper(), FamilyRoom.is_active.is_(True)))
    if room is None:
        raise HTTPException(status_code=404, detail="Family room not found")

    existing_member = db.scalar(
        select(FamilyMember).where(
            FamilyMember.room_id == room.id,
            FamilyMember.user_id == current_user.id,
        )
    )

    if existing_member is not None and not existing_member.is_active:
        raise HTTPException(status_code=404, detail="Family room not found")
    if existing_member is None:
        existing_room_members = db.scalars(
            select(FamilyMember).where(FamilyMember.room_id == room.id, FamilyMember.is_active.is_(True))
        ).all()

        if existing_room_members:
            if not payload.related_to_user_id or payload.relation_to_related_user not in VALID_MEMBER_RELATIONS:
                raise HTTPException(
                    status_code=400,
                    detail="Joining a family room requires selecting an existing member and your relationship to that member.",
                )

            related_member = db.scalar(
                select(FamilyMember).where(
                    FamilyMember.room_id == room.id,
                    FamilyMember.user_id == payload.related_to_user_id,
                    FamilyMember.is_active.is_(True),
                )
            )
            if related_member is None:
                raise HTTPException(status_code=400, detail="Selected related family member is not in this room")

        db.add(
            FamilyMember(
                id=str(uuid.uuid4()),
                room_id=room.id,
                user_id=current_user.id,
                role="member",
                related_to_user_id=payload.related_to_user_id,
                relation_to_related_user=payload.relation_to_related_user,
            )
        )
        db.commit()

    return FamilyResponse(
        room_id=room.id,
        name=room.name,
        invite_code=room.invite_code,
        owner_user_id=room.owner_user_id,
    )


@router.get("/invite/{invite_code}", response_model=FamilyJoinPreviewResponse)
def preview_family_by_invite_code(invite_code: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    room = db.scalar(select(FamilyRoom).where(FamilyRoom.invite_code == invite_code.upper(), FamilyRoom.is_active.is_(True)))
    if room is None:
        raise HTTPException(status_code=404, detail="Family room not found")

    members = build_family_member_responses(db, room.id)

    return FamilyJoinPreviewResponse(
        room_id=room.id,
        name=room.name,
        invite_code=room.invite_code,
        owner_user_id=room.owner_user_id,
        members=members,
    )


@router.get("/{room_id}", response_model=FamilyDetailResponse)
def get_family(room_id: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    room = db.get(FamilyRoom, room_id)
    if room is None or not room.is_active:
        raise HTTPException(status_code=404, detail="Family room not found")

    ensure_family_member(db, room_id, current_user.id)

    member_count = db.scalar(
        select(func.count(FamilyMember.id)).where(FamilyMember.room_id == room_id, FamilyMember.is_active.is_(True))
    ) or 0

    return FamilyDetailResponse(
        room_id=room.id,
        name=room.name,
        invite_code=room.invite_code,
        owner_user_id=room.owner_user_id,
        member_count=member_count,
    )


@router.get("/{room_id}/members", response_model=list[FamilyMemberResponse])
def list_family_members(room_id: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    room = db.get(FamilyRoom, room_id)
    if room is None or not room.is_active:
        raise HTTPException(status_code=404, detail="Family room not found")

    ensure_family_member(db, room_id, current_user.id)
    return build_family_member_responses(db, room_id)


@router.delete("/{room_id}")
def delete_family(room_id: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    room = db.get(FamilyRoom, room_id)
    if room is None or not room.is_active:
        raise HTTPException(status_code=404, detail="Family room not found")

    if room.owner_user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the room owner can delete this family room")

    delete_family_room_with_related_data(db, room)
    db.commit()
    db.close()

    return {
        "deleted": True,
        "room_id": room_id,
        "media_cleanup_failed_count": 0,
    }


def ensure_graph_room_member(db: Session, room_id: str, user_id: str) -> FamilyRoom:
    room = db.get(FamilyRoom, room_id)
    if room is None or not room.is_active:
        raise HTTPException(status_code=404, detail="Family room not found")
    ensure_family_member(db, room_id, user_id)
    return room


def ensure_graph_owner(db: Session, room_id: str, user_id: str) -> FamilyRoom:
    # Acquire the room write lock before reading graph rows. PostgreSQL locks
    # this row; SQLite serializes its writers on the same no-op UPDATE.
    locked = db.execute(
        update(FamilyRoom)
        .where(FamilyRoom.id == room_id)
        .values(id=FamilyRoom.id)
    )
    if locked.rowcount != 1:
        raise HTTPException(status_code=404, detail="Family room not found")

    room = ensure_graph_room_member(db, room_id, user_id)
    if room.owner_user_id != user_id:
        raise HTTPException(status_code=403, detail="Only the room owner can edit the family graph")
    return room


def graph_commit(db: Session) -> None:
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail="Family graph constraint conflict") from error


def family_person_response(person: FamilyPerson) -> FamilyPersonResponse:
    return FamilyPersonResponse(
        id=person.id,
        room_id=person.room_id,
        user_id=person.user_id,
        display_name=person.display_name,
        gender=person.gender,
        birth_date=person.birth_date,
        death_date=person.death_date,
    )


def family_relationship_response(edge: FamilyRelationship) -> FamilyRelationshipResponse:
    return FamilyRelationshipResponse(
        id=edge.id,
        room_id=edge.room_id,
        from_person_id=edge.from_person_id,
        to_person_id=edge.to_person_id,
        relation_type=edge.relation_type,
    )


def ensure_linkable_room_member(db: Session, room_id: str, user_id: str) -> None:
    if db.scalar(
        select(FamilyMember.id).where(
            FamilyMember.room_id == room_id,
            FamilyMember.user_id == user_id,
            FamilyMember.is_active.is_(True),
        )
    ) is None:
        raise HTTPException(status_code=400, detail="Linked account must be a room member")


def ensure_person_link_available(
    db: Session, room_id: str, user_id: str | None, exclude_person_id: str | None = None
) -> None:
    if user_id is None:
        return
    ensure_linkable_room_member(db, room_id, user_id)
    query = select(FamilyPerson.id).where(
        FamilyPerson.room_id == room_id,
        FamilyPerson.user_id == user_id,
    )
    if exclude_person_id is not None:
        query = query.where(FamilyPerson.id != exclude_person_id)
    if db.scalar(query) is not None:
        raise HTTPException(status_code=409, detail="Account is already linked to a family person")


@router.get("/{room_id}/people", response_model=list[FamilyPersonResponse])
def list_family_people(
    room_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ensure_graph_room_member(db, room_id, current_user.id)
    people = db.scalars(
        select(FamilyPerson).where(FamilyPerson.room_id == room_id).order_by(FamilyPerson.id)
    ).all()
    return [family_person_response(person) for person in people]


@router.post("/{room_id}/people", response_model=FamilyPersonResponse)
def create_family_person(
    room_id: str,
    payload: FamilyPersonCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ensure_graph_owner(db, room_id, current_user.id)
    if not payload.display_name.strip():
        raise HTTPException(status_code=400, detail="Display name cannot be empty")
    ensure_person_link_available(db, room_id, payload.user_id)
    person = FamilyPerson(
        id=str(uuid.uuid4()),
        room_id=room_id,
        **payload.model_dump(),
    )
    db.add(person)
    graph_commit(db)
    return family_person_response(person)


@router.patch("/{room_id}/people/{person_id}", response_model=FamilyPersonResponse)
def update_family_person(
    room_id: str,
    person_id: str,
    payload: FamilyPersonUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ensure_graph_owner(db, room_id, current_user.id)
    person = db.scalar(
        select(FamilyPerson).where(
            FamilyPerson.room_id == room_id,
            FamilyPerson.id == person_id,
        )
    )
    if person is None:
        raise HTTPException(status_code=404, detail="Family person not found")
    changes = payload.model_dump(exclude_unset=True)
    if "display_name" in changes and (
        changes["display_name"] is None or not changes["display_name"].strip()
    ):
        raise HTTPException(status_code=400, detail="Display name cannot be empty")
    if "user_id" in changes:
        ensure_person_link_available(
            db, room_id, changes["user_id"], exclude_person_id=person.id
        )
    for field, value in changes.items():
        setattr(person, field, value)
    graph_commit(db)
    return family_person_response(person)


@router.get("/{room_id}/relationships", response_model=list[FamilyRelationshipResponse])
def list_family_relationships(
    room_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ensure_graph_room_member(db, room_id, current_user.id)
    edges = db.scalars(
        select(FamilyRelationship)
        .where(FamilyRelationship.room_id == room_id)
        .order_by(FamilyRelationship.id)
    ).all()
    return [family_relationship_response(edge) for edge in edges]


def parent_path_exists(db: Session, room_id: str, start_id: str, target_id: str) -> bool:
    edges = db.execute(
        select(FamilyRelationship.from_person_id, FamilyRelationship.to_person_id).where(
            FamilyRelationship.room_id == room_id,
            FamilyRelationship.relation_type == "PARENT_OF",
        )
    ).all()
    children: dict[str, set[str]] = {}
    for parent_id, child_id in edges:
        children.setdefault(parent_id, set()).add(child_id)

    pending = [start_id]
    seen = set()
    while pending:
        person_id = pending.pop()
        if person_id == target_id:
            return True
        if person_id in seen:
            continue
        seen.add(person_id)
        pending.extend(children.get(person_id, ()))
    return False


@router.post("/{room_id}/relationships", response_model=FamilyRelationshipResponse)
def create_family_relationship(
    room_id: str,
    payload: FamilyRelationshipCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ensure_graph_owner(db, room_id, current_user.id)
    source_id, target_id = payload.from_person_id, payload.to_person_id
    if source_id == target_id:
        raise HTTPException(status_code=400, detail="A person cannot have a relationship to themselves")
    if payload.relation_type == "SPOUSE_OF":
        source_id, target_id = sorted((source_id, target_id))

    endpoint_count = db.scalar(
        select(func.count(FamilyPerson.id)).where(
            FamilyPerson.room_id == room_id,
            FamilyPerson.id.in_((source_id, target_id)),
        )
    ) or 0
    if endpoint_count != 2:
        raise HTTPException(status_code=400, detail="Relationship endpoints must belong to this family room")

    duplicate = db.scalar(
        select(FamilyRelationship.id).where(
            FamilyRelationship.room_id == room_id,
            FamilyRelationship.from_person_id == source_id,
            FamilyRelationship.to_person_id == target_id,
            FamilyRelationship.relation_type == payload.relation_type,
        )
    )
    if duplicate is not None:
        raise HTTPException(status_code=409, detail="Relationship already exists")

    if payload.relation_type == "PARENT_OF" and parent_path_exists(
        db, room_id, target_id, source_id
    ):
        raise HTTPException(status_code=409, detail="Parent relationships cannot contain a cycle")

    edge = FamilyRelationship(
        id=str(uuid.uuid4()),
        room_id=room_id,
        from_person_id=source_id,
        to_person_id=target_id,
        relation_type=payload.relation_type,
    )
    db.add(edge)
    graph_commit(db)
    return family_relationship_response(edge)


@router.delete("/{room_id}/relationships/{relationship_id}")
def delete_family_relationship(
    room_id: str,
    relationship_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ensure_graph_owner(db, room_id, current_user.id)
    edge = db.scalar(
        select(FamilyRelationship).where(
            FamilyRelationship.room_id == room_id,
            FamilyRelationship.id == relationship_id,
        )
    )
    if edge is None:
        raise HTTPException(status_code=404, detail="Family relationship not found")
    db.delete(edge)
    graph_commit(db)
    return {"deleted": True, "relationship_id": relationship_id}


@router.get("/{room_id}/kinship/{target_person_id}", response_model=KinshipResponse)
def get_family_kinship(
    room_id: str,
    target_person_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ensure_graph_room_member(db, room_id, current_user.id)
    viewer = db.scalar(
        select(FamilyPerson).where(
            FamilyPerson.room_id == room_id,
            FamilyPerson.user_id == current_user.id,
        )
    )
    if viewer is None:
        raise HTTPException(status_code=409, detail="Current user is not linked to a family person")
    target = db.scalar(
        select(FamilyPerson).where(
            FamilyPerson.room_id == room_id,
            FamilyPerson.id == target_person_id,
        )
    )
    if target is None:
        raise HTTPException(status_code=404, detail="Family person not found")
    people = db.scalars(select(FamilyPerson).where(FamilyPerson.room_id == room_id)).all()
    edges = db.scalars(
        select(FamilyRelationship).where(FamilyRelationship.room_id == room_id)
    ).all()
    result = resolve_kinship(room_id, viewer.id, target.id, people, edges)
    return KinshipResponse(kind=result.kind, lineage=result.lineage)
