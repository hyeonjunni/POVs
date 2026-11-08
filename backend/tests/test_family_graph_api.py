from types import SimpleNamespace

import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import inspect
from sqlalchemy.orm import sessionmaker

from conftest import fk_enforced_engine
from scripts.adopt_alembic import _build_alembic_config
from app.api import deps
from app.core import database
from app.core.database import Base
from app.main import app
from app.models.family import FamilyMember, FamilyPerson, FamilyRelationship, FamilyRoom
from app.models.user import User


ROOM_ID = "fg-room-1"
OTHER_ROOM_ID = "fg-room-2"
OWNER_ID = "fg-owner"
MEMBER_ID = "fg-member"
OUTSIDER_ID = "fg-outsider"


class UserTestClient(TestClient):
    def __init__(self, user_id):
        super().__init__(app)
        self.user_id = user_id

    def request(self, *args, **kwargs):
        app.dependency_overrides[deps.get_current_user] = lambda: SimpleNamespace(
            id=self.user_id
        )
        return super().request(*args, **kwargs)


@pytest.fixture
def graph_api():
    engine = fk_enforced_engine("sqlite://")
    Base.metadata.create_all(engine)
    session_maker = sessionmaker(bind=engine)

    with session_maker() as db:
        db.add_all(
            [
                User(id=OWNER_ID, name="Owner", email="owner@example.test"),
                User(id=MEMBER_ID, name="Member", email="member@example.test"),
                User(id=OUTSIDER_ID, name="Outsider", email="outsider@example.test"),
            ]
        )
        db.flush()
        db.add_all(
            [
                FamilyRoom(
                    id=ROOM_ID, name="Room", invite_code="FG0001",
                    owner_user_id=OWNER_ID,
                ),
                FamilyRoom(
                    id=OTHER_ROOM_ID, name="Other", invite_code="FG0002",
                    owner_user_id=OUTSIDER_ID,
                ),
            ]
        )
        db.flush()
        db.add_all(
            [
                FamilyMember(id="fg-m1", room_id=ROOM_ID, user_id=OWNER_ID, role="member"),
                FamilyMember(id="fg-m2", room_id=ROOM_ID, user_id=MEMBER_ID, role="owner"),
                FamilyMember(id="fg-m3", room_id=OTHER_ROOM_ID, user_id=OUTSIDER_ID, role="owner"),
            ]
        )
        db.commit()

    def get_db():
        db = session_maker()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[database.get_db] = get_db

    def client_as(user_id):
        return UserTestClient(user_id)

    try:
        yield client_as, session_maker
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _create_person(client, name, **extra):
    return client.post(
        f"/api/v1/families/{ROOM_ID}/people",
        json={"display_name": name, **extra},
    )


def _edge(client, source, target, relation="PARENT_OF"):
    return client.post(
        f"/api/v1/families/{ROOM_ID}/relationships",
        json={
            "from_person_id": source,
            "to_person_id": target,
            "relation_type": relation,
        },
    )


def test_non_account_people_and_member_read_owner_update(graph_api):
    client_as, _sessions = graph_api
    owner = client_as(OWNER_ID)
    created = _create_person(owner, "할아버지", gender="남성")
    assert created.status_code == 200, created.text
    person_id = created.json()["id"]
    assert created.json()["user_id"] is None

    member = client_as(MEMBER_ID)
    listed = member.get(f"/api/v1/families/{ROOM_ID}/people")
    assert listed.status_code == 200, listed.text
    assert [person["id"] for person in listed.json()] == [person_id]

    updated = owner.patch(
        f"/api/v1/families/{ROOM_ID}/people/{person_id}",
        json={"display_name": "수정한 할아버지"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["display_name"] == "수정한 할아버지"


def test_linked_people_must_be_room_members_and_unique(graph_api):
    client_as, _sessions = graph_api
    owner = client_as(OWNER_ID)

    linked = _create_person(owner, "Member", user_id=MEMBER_ID)
    assert linked.status_code == 200, linked.text
    duplicate = _create_person(owner, "Duplicate link", user_id=MEMBER_ID)
    assert duplicate.status_code == 409

    cross_room = _create_person(owner, "Outsider", user_id=OUTSIDER_ID)
    assert cross_room.status_code == 400


def test_only_owner_writes_and_nonmembers_cannot_read(graph_api):
    client_as, _sessions = graph_api
    owner = client_as(OWNER_ID)
    person = _create_person(owner, "사람").json()
    another = _create_person(owner, "다른 사람").json()
    edge = _edge(owner, person["id"], another["id"]).json()

    member = client_as(MEMBER_ID)
    assert member.get(f"/api/v1/families/{ROOM_ID}/people").status_code == 200
    assert member.get(f"/api/v1/families/{ROOM_ID}/relationships").status_code == 200
    assert member.patch(
        f"/api/v1/families/{ROOM_ID}/people/{person['id']}",
        json={"display_name": "member edit"},
    ).status_code == 403
    assert _create_person(member, "member create").status_code == 403
    assert _edge(member, person["id"], another["id"]).status_code == 403
    assert member.delete(
        f"/api/v1/families/{ROOM_ID}/relationships/{edge['id']}"
    ).status_code == 403

    outsider = client_as(OUTSIDER_ID)
    assert outsider.get(f"/api/v1/families/{ROOM_ID}/people").status_code == 403


def test_edges_reject_invalid_duplicates_reverse_spouse_and_cycles(graph_api):
    client_as, _sessions = graph_api
    owner = client_as(OWNER_ID)
    first = _create_person(owner, "First").json()["id"]
    second = _create_person(owner, "Second").json()["id"]
    third = _create_person(owner, "Third").json()["id"]
    outsider = client_as(OUTSIDER_ID)
    foreign = outsider.post(
        f"/api/v1/families/{OTHER_ROOM_ID}/people",
        json={"display_name": "Foreign"},
    )
    assert foreign.status_code == 200, foreign.text

    assert _edge(owner, first, first).status_code == 400
    assert _edge(owner, first, "missing").status_code == 400
    assert _edge(owner, first, foreign.json()["id"]).status_code == 400

    parent_edge = _edge(owner, first, second)
    assert parent_edge.status_code == 200, parent_edge.text
    assert _edge(owner, first, second).status_code == 409
    assert _edge(owner, second, third).status_code == 200
    assert _edge(owner, third, first).status_code == 409

    spouse = _edge(owner, third, first, "SPOUSE_OF")
    assert spouse.status_code == 200, spouse.text
    assert spouse.json()["from_person_id"] == min(first, third)
    assert spouse.json()["to_person_id"] == max(first, third)
    assert _edge(owner, first, third, "SPOUSE_OF").status_code == 409
    assert _edge(owner, first, first, "SPOUSE_OF").status_code == 400


def test_viewer_relative_kinship_requires_link_and_uses_graph(graph_api):
    client_as, _sessions = graph_api
    owner = client_as(OWNER_ID)
    self_person = _create_person(owner, "나", user_id=OWNER_ID).json()
    parent = _create_person(owner, "아버지", gender="남성").json()
    grandparent = _create_person(owner, "할아버지", gender="남성").json()
    assert _edge(owner, parent["id"], self_person["id"]).status_code == 200
    assert _edge(owner, grandparent["id"], parent["id"]).status_code == 200

    kinship = owner.get(
        f"/api/v1/families/{ROOM_ID}/kinship/{grandparent['id']}"
    )
    assert kinship.status_code == 200, kinship.text
    assert kinship.json()["kind"] == "GRANDPARENT"
    assert kinship.json()["lineage"] == "PATERNAL"

    owner.patch(
        f"/api/v1/families/{ROOM_ID}/people/{self_person['id']}",
        json={"user_id": None},
    )
    unlinked = owner.get(
        f"/api/v1/families/{ROOM_ID}/kinship/{grandparent['id']}"
    )
    assert unlinked.status_code == 409


def test_legacy_member_response_keeps_relation_fields(graph_api):
    client_as, sessions = graph_api
    with sessions() as db:
        member = db.get(FamilyMember, "fg-m2")
        member.related_to_user_id = OWNER_ID
        member.relation_to_related_user = "child"
        db.commit()

    response = client_as(OWNER_ID).get(f"/api/v1/families/{ROOM_ID}/members")
    assert response.status_code == 200, response.text
    member = next(item for item in response.json() if item["user_id"] == MEMBER_ID)
    assert member["related_to_user_id"] == OWNER_ID
    assert member["relation_to_related_user"] == "child"


def test_nonmember_link_and_kinship_target_are_room_scoped(graph_api):
    client_as, _sessions = graph_api
    outsider = client_as(OUTSIDER_ID)
    assert outsider.get(f"/api/v1/families/{ROOM_ID}/kinship/not-a-person").status_code == 403


def test_family_graph_migration_upgrade_and_downgrade(tmp_path):
    url = f"sqlite:///{tmp_path / 'family-graph.db'}"
    config = _build_alembic_config(url)

    command.upgrade(config, "0006_family_graph")
    engine = fk_enforced_engine(url)
    try:
        inspector = inspect(engine)
        assert {"family_people", "family_relationships"} <= set(inspector.get_table_names())
    finally:
        engine.dispose()

    command.downgrade(config, "0005_secure_catalogue")
    engine = fk_enforced_engine(url)
    try:
        inspector = inspect(engine)
        assert "family_people" not in inspector.get_table_names()
        assert "family_relationships" not in inspector.get_table_names()
    finally:
        engine.dispose()



def test_room_cleanup_removes_graph_rows(graph_api):
    _client_as, sessions = graph_api
    with sessions() as db:
        db.add_all(
            [
                FamilyPerson(
                    id="fg-delete-person-1", room_id=ROOM_ID,
                    display_name="A",
                ),
                FamilyPerson(
                    id="fg-delete-person-2", room_id=ROOM_ID,
                    display_name="B",
                ),
            ]
        )
        db.flush()
        db.add(
            FamilyRelationship(
                id="fg-delete-edge", room_id=ROOM_ID,
                from_person_id="fg-delete-person-1",
                to_person_id="fg-delete-person-2",
                relation_type="PARENT_OF",
            )
        )
        db.commit()

        from app.services.account_cleanup import delete_family_room_with_related_data

        delete_family_room_with_related_data(db, db.get(FamilyRoom, ROOM_ID))
        db.commit()
        assert not db.get(FamilyRoom, ROOM_ID).is_active
        assert db.get(FamilyPerson, "fg-delete-person-1") is not None
        assert db.get(FamilyRelationship, "fg-delete-edge") is not None


def test_account_cleanup_retains_surviving_family_person_provenance(graph_api):
    _client_as, sessions = graph_api
    with sessions() as db:
        db.add(
            FamilyPerson(
                id="fg-account-person", room_id=ROOM_ID, user_id=MEMBER_ID,
                display_name="Member",
            )
        )
        db.commit()

        from app.services.account_cleanup import delete_user_account_data

        delete_user_account_data(db, db.get(User, MEMBER_ID))
        db.commit()

        person = db.get(FamilyPerson, "fg-account-person")
        assert person is not None
        assert person.user_id == MEMBER_ID
        assert db.get(User, MEMBER_ID) is not None
        assert not db.get(FamilyMember, "fg-m2").is_active
