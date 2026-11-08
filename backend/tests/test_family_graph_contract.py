from dataclasses import dataclass
from importlib import import_module

import pytest


@dataclass(frozen=True)
class Person:
    id: str
    room_id: str
    gender: str | None = None


@dataclass(frozen=True)
class Relationship:
    room_id: str
    from_person_id: str
    to_person_id: str
    relation_type: str


ROOM = "room-a"
PEOPLE = [
    Person("me", ROOM),
    Person("father", ROOM, "남성"),
    Person("mother", ROOM, "여성"),
    Person("paternal-grandfather", ROOM, "남성"),
    Person("paternal-grandmother", ROOM, "여성"),
    Person("maternal-grandfather", ROOM, "남성"),
    Person("maternal-grandmother", ROOM, "여성"),
    Person("sibling", ROOM),
    Person("spouse", ROOM),
    Person("child", ROOM),
    Person("other-room-person", "room-b"),
]
RELATIONSHIPS = [
    Relationship(ROOM, "father", "me", "PARENT_OF"),
    Relationship(ROOM, "mother", "me", "PARENT_OF"),
    Relationship(ROOM, "paternal-grandfather", "father", "PARENT_OF"),
    Relationship(ROOM, "paternal-grandmother", "father", "PARENT_OF"),
    Relationship(ROOM, "maternal-grandfather", "mother", "PARENT_OF"),
    Relationship(ROOM, "maternal-grandmother", "mother", "PARENT_OF"),
    Relationship(ROOM, "father", "sibling", "PARENT_OF"),
    Relationship(ROOM, "mother", "sibling", "PARENT_OF"),
    Relationship(ROOM, "me", "child", "PARENT_OF"),
    Relationship(ROOM, "me", "spouse", "SPOUSE_OF"),
]


def resolve(
    room_id: str,
    viewer: str,
    target: str,
    people=PEOPLE,
    relationships=RELATIONSHIPS,
):
    module_name = "app.services.family_graph"
    try:
        module = import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name != module_name:
            raise
        pytest.fail(
            "contract RED: app.services.family_graph is not implemented",
            pytrace=False,
        )

    try:
        resolver = module.resolve_kinship
    except AttributeError:
        pytest.fail(
            "contract RED: app.services.family_graph.resolve_kinship is not implemented",
            pytrace=False,
        )
    return resolver(room_id, viewer, target, people, relationships)


def assert_unknown(result):
    assert result.kind == "UNKNOWN"
    assert result.lineage == "UNKNOWN"


@pytest.mark.parametrize(
    ("viewer", "target", "kind", "lineage"),
    [
        ("me", "me", "SELF", "NONE"),
        ("me", "father", "PARENT", "NONE"),
        ("me", "mother", "PARENT", "NONE"),
        ("father", "me", "CHILD", "NONE"),
        ("me", "spouse", "SPOUSE", "NONE"),
        ("spouse", "me", "SPOUSE", "NONE"),
        ("me", "paternal-grandfather", "GRANDPARENT", "PATERNAL"),
        ("me", "paternal-grandmother", "GRANDPARENT", "PATERNAL"),
        ("me", "maternal-grandfather", "GRANDPARENT", "MATERNAL"),
        ("me", "maternal-grandmother", "GRANDPARENT", "MATERNAL"),
        ("paternal-grandfather", "me", "GRANDCHILD", None),
        ("me", "sibling", "SIBLING", "NONE"),
        ("me", "other-room-person", "UNKNOWN", "UNKNOWN"),
    ],
)
def test_resolves_documented_kinship_kinds_and_lineage(viewer, target, kind, lineage):
    result = resolve(ROOM, viewer, target)

    assert result.kind == kind
    if lineage is not None:
        assert result.lineage == lineage


def test_unknown_person_and_viewer_resolve_unknown():
    assert_unknown(resolve(ROOM, "me", "missing-person"))
    assert_unknown(resolve(ROOM, "missing-person", "me"))


def test_cross_room_edge_does_not_contribute():
    people = PEOPLE + [Person("edge-person", ROOM)]
    relationships = RELATIONSHIPS + [
        Relationship("room-b", "edge-person", "me", "PARENT_OF")
    ]

    assert_unknown(resolve(ROOM, "me", "edge-person", people, relationships))


def test_unknown_or_conflicting_parent_gender_gives_unknown_lineage():
    unknown_gender = [
        Person(person.id, person.room_id, None if person.id == "father" else person.gender)
        for person in PEOPLE
    ]
    result = resolve(ROOM, "me", "paternal-grandfather", unknown_gender)

    assert result.kind == "GRANDPARENT"
    assert result.lineage == "UNKNOWN"

    conflicting_paths = RELATIONSHIPS + [
        Relationship(ROOM, "paternal-grandfather", "mother", "PARENT_OF")
    ]
    result = resolve(ROOM, "me", "paternal-grandfather", PEOPLE, conflicting_paths)

    assert result.kind == "GRANDPARENT"
    assert result.lineage == "UNKNOWN"


def test_ambiguous_kinship_kind_resolves_unknown():
    relationships = RELATIONSHIPS + [
        Relationship(ROOM, "me", "father", "SPOUSE_OF")
    ]

    assert_unknown(resolve(ROOM, "me", "father", PEOPLE, relationships))

