"""Pure, read-only kinship resolution over room-scoped person and edge objects."""

from collections import defaultdict
from dataclasses import dataclass


@dataclass(frozen=True)
class KinshipResolution:
    kind: str
    lineage: str


def resolve_kinship(room_id, viewer_person_id, target_person_id, people, relationships):
    room_people = {
        person.id: person for person in people if person.room_id == room_id
    }
    if viewer_person_id not in room_people or target_person_id not in room_people:
        return KinshipResolution("UNKNOWN", "UNKNOWN")
    if viewer_person_id == target_person_id:
        return KinshipResolution("SELF", "NONE")

    parents_by_child = defaultdict(set)
    children_by_parent = defaultdict(set)
    spouses = set()

    for edge in relationships:
        if edge.room_id != room_id:
            continue
        if edge.from_person_id not in room_people or edge.to_person_id not in room_people:
            continue
        if edge.relation_type == "PARENT_OF":
            children_by_parent[edge.from_person_id].add(edge.to_person_id)
            parents_by_child[edge.to_person_id].add(edge.from_person_id)
        elif edge.relation_type == "SPOUSE_OF":
            spouses.add((edge.from_person_id, edge.to_person_id))
            spouses.add((edge.to_person_id, edge.from_person_id))

    viewer = viewer_person_id
    target = target_person_id
    candidates = {}

    if target in parents_by_child[viewer]:
        candidates["PARENT"] = {"NONE"}
    if target in children_by_parent[viewer]:
        candidates["CHILD"] = {"NONE"}
    if (viewer, target) in spouses:
        candidates["SPOUSE"] = {"NONE"}
    if parents_by_child[viewer] & parents_by_child[target]:
        candidates["SIBLING"] = {"NONE"}

    grandparent_lineages = set()
    for parent_id in parents_by_child[viewer]:
        if target not in parents_by_child[parent_id]:
            continue
        gender = room_people[parent_id].gender
        if gender == "남성":
            grandparent_lineages.add("PATERNAL")
        elif gender == "여성":
            grandparent_lineages.add("MATERNAL")
        else:
            grandparent_lineages.add("UNKNOWN")
    if grandparent_lineages:
        candidates["GRANDPARENT"] = grandparent_lineages

    if any(
        target in children_by_parent[child_id]
        for child_id in children_by_parent[viewer]
    ):
        candidates["GRANDCHILD"] = {"NONE"}

    if len(candidates) != 1:
        return KinshipResolution("UNKNOWN", "UNKNOWN")

    kind, lineages = next(iter(candidates.items()))
    lineage = next(iter(lineages)) if len(lineages) == 1 else "UNKNOWN"
    return KinshipResolution(kind, lineage)

