"""Viewer-specific names and notes stored only inside the encrypted user profile."""
from dataclasses import replace
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import text

from app.api.routes import secure_operations as ops
from app.api.routes.secure_media import abandon_staged
from app.schemas.personal_context import PersonalContextUpdate
from app.services import secure_catalogue as cat


SUGGESTIONS = [
    {"relation": relation, "aliases": aliases}
    for relation, aliases in (
        ("어머니", ["엄마", "어머니", "어머님"]),
        ("아버지", ["아빠", "아버지", "아버님"]),
        ("배우자", ["여보", "자기", "당신", "남편", "아내", "신랑", "각시"]),
        ("손위 형제자매", ["오빠", "오라버니", "형", "형님", "언니", "누나", "누님"]),
        ("손아래 형제자매", ["남동생", "여동생", "동생"]),
        ("자녀", ["아들", "딸", "아이", "애기", "아기"]),
        ("조부모", ["할아버지", "할머니", "친할아버지", "친할머니", "외할아버지", "외할머니"]),
        ("손주", ["손자", "손녀", "손주"]),
        ("아버지 쪽 친척", ["큰아버지", "작은아버지", "삼촌", "고모", "고모부", "큰어머니", "작은어머니"]),
        ("어머니 쪽 친척", ["외삼촌", "외숙모", "이모", "이모부"]),
        ("사촌", ["사촌형", "사촌오빠", "사촌누나", "사촌언니", "사촌동생"]),
        ("배우자의 부모", ["시아버지", "시어머니", "장인어른", "장모님"]),
        ("형제자매의 배우자", ["형수님", "새언니", "올케", "형부", "매형", "매부", "제부", "제수씨"]),
        ("배우자의 형제자매", ["아주버님", "형님", "시동생", "처남", "처형", "처제", "동서"]),
        ("자녀의 배우자와 가족", ["사위", "며느리", "사돈"]),
        ("형제자매의 자녀", ["조카", "조카딸", "조카아들"]),
    )
]


def raw_profile(runtime, uid):
    with ops.tx() as db:
        saved = cat.snapshot(db, principal_id=UUID(uid), targets=(("user", UUID(uid)),))
        values = ops.detached(db, saved)
    profile = ops.decrypt(runtime, values)[0]
    with ops.tx() as db:
        cat.revalidate(db, saved)
    return profile, saved


def _members(db, room_id, uid):
    members = set(db.execute(text("SELECT user_id FROM family_members WHERE room_id=:r AND is_active=true"),
                             {"r": room_id}).scalars())
    if uid not in members:
        raise HTTPException(404, "Family room not found")
    return members


def _gender(person):
    value = (person.get("gender") or "").lower()
    return "male" if value in ("male", "m", "남", "남성", "남자") else "female" if value in ("female", "f", "여", "여성", "여자") else None


def defaults(room, membership, uid, active):
    people = {key: value for key, value in room["members"].items() if key in active}
    parents = {key: set() for key in people}
    spouses = {key: set() for key in people}
    for member, _, related in membership:
        if member not in people or related not in people:
            continue
        relation = people[member].get("relation_to_related_user")
        if relation == "child":
            parents[member].add(related)
        elif relation == "parent":
            parents[related].add(member)
        elif relation == "spouse":
            spouses[member].add(related)
            spouses[related].add(member)
    # Room onboarding records one parent; its recorded spouse supplies the second.
    for child, direct in parents.items():
        direct.update({spouse for parent in tuple(direct) for spouse in spouses[parent] if spouse != child})
    result = []
    viewer = people[uid]
    for target, person in people.items():
        name = person.get("name") or "가족"
        aliases = [name]
        if len(name) == 3 and all("가" <= ch <= "힣" for ch in name):
            aliases.append(name[1:])
        gender = _gender(person)
        relation = "가족"
        if target == uid:
            relation = "본인"
            aliases += ["나", "저", "본인"]
        elif target in parents[uid]:
            relation = "아버지" if gender == "male" else "어머니" if gender == "female" else "부모"
            aliases += ["아빠", "아버지", "아버님"] if gender == "male" else ["엄마", "어머니", "어머님"] if gender == "female" else []
        elif target in spouses[uid]:
            relation = "배우자"
            aliases += ["여보", "자기", "당신", "남편" if gender == "male" else "아내" if gender == "female" else "배우자"]
            if (_gender(viewer) == "female" and gender == "male"
                    and isinstance(person.get("age"), int) and isinstance(viewer.get("age"), int)
                    and person["age"] > viewer["age"]):
                aliases.append("오빠")
            for child in people:
                if uid in parents[child] and target in parents[child] and gender:
                    child_name = people[child].get("name", "")
                    if len(child_name) == 3:
                        child_name = child_name[1:]
                    parent_title = "아빠" if gender == "male" else "엄마"
                    aliases.extend([child_name + " " + parent_title, child_name + parent_title])
        elif uid in parents[target]:
            relation = "아들" if gender == "male" else "딸" if gender == "female" else "자녀"
            aliases += [relation]
        elif parents[uid] & parents[target]:
            relation = "형제자매"
            age, own_age = person.get("age"), viewer.get("age")
            if isinstance(age, int) and isinstance(own_age, int) and age > own_age and _gender(viewer) and gender:
                relation = ("형" if _gender(viewer) == "male" else "오빠") if gender == "male" else ("누나" if _gender(viewer) == "male" else "언니")
            elif isinstance(age, int) and isinstance(own_age, int) and age < own_age:
                relation = "남동생" if gender == "male" else "여동생" if gender == "female" else "동생"
            aliases += [relation]
            aliases += {"형": ["형님"], "오빠": ["오라버니"], "누나": ["누님"]}.get(relation, [])
            if relation in ("남동생", "여동생"):
                aliases.append("동생")
        elif any(target in parents[parent] for parent in parents[uid]):
            relation = "할아버지" if gender == "male" else "할머니" if gender == "female" else "조부모"
            aliases += [relation]
            if any(_gender(people[parent]) == "female" and target in parents[parent] for parent in parents[uid]) and gender:
                aliases.append("외" + relation)
            if any(_gender(people[parent]) == "male" and target in parents[parent] for parent in parents[uid]) and gender:
                aliases.append("친" + relation)
        if len(name) == 3 and all("가" <= ch <= "힣" for ch in name) and (
                target in spouses[uid] or uid in parents[target] or relation in ("남동생", "여동생", "동생")):
            aliases.append(name[1:] + ("아" if (ord(name[-1]) - 0xAC00) % 28 else "야"))
        result.append({"user_id": target, "name": name, "relation": relation,
                       "default_aliases": list(dict.fromkeys(aliases))[:20]})
    return result


def _load(runtime, uid, room_id):
    room, room_snapshot, membership = ops.read_room(runtime, uid, room_id)
    profile, user_snapshot = raw_profile(runtime, uid)
    with ops.tx() as db:
        active = _members(db, room_id, uid)
        cat.revalidate(db, room_snapshot)
        cat.revalidate(db, user_snapshot)
    return room, room_snapshot, membership, profile, user_snapshot, active


def _response(room, membership, profile, uid, room_id, active):
    saved = profile.get("personal_context", {}).get(room_id, {})
    people = defaults(room, membership, uid, active)
    for person in people:
        person["aliases"] = saved.get("aliases", {}).get(person["user_id"], person["default_aliases"])
    return {"version": saved.get("version", 0), "viewer_user_id": uid,
            "viewer_name": profile["name"], "memory_note": saved.get("memory_note", ""),
            "people": people, "suggestions": SUGGESTIONS,
            "family_address_context": [
                {"speaker_user_id": speaker, "speaker_name": room["members"][speaker]["name"],
                 "people": [{"user_id": person["user_id"], "name": person["name"],
                             "relation": person["relation"], "aliases": person["default_aliases"]}
                            for person in defaults(room, membership, speaker, active)]}
                for speaker in sorted(active)
            ]}


def read(runtime, uid, room_id):
    room, _, membership, profile, _, active = _load(runtime, uid, room_id)
    return _response(room, membership, profile, uid, room_id, active)


def save(runtime, uid, room_id, payload: PersonalContextUpdate):
    room, before_room, membership, profile, before_user, active = _load(runtime, uid, room_id)
    settings = dict(profile.get("personal_context", {}))
    if payload.version != settings.get(room_id, {}).get("version", 0):
        raise HTTPException(409, "Personal context changed; reload before saving")
    if any(str(entry.target_user_id) not in active for entry in payload.aliases):
        raise HTTPException(400, "Alias target must be an active family member")
    settings[room_id] = {"version": payload.version + 1, "memory_note": payload.memory_note.strip(),
                         "aliases": {str(entry.target_user_id): list(dict.fromkeys(entry.values)) for entry in payload.aliases}}
    updated = profile | {"personal_context": settings}
    staged = ops.stage_update(runtime, uid, ("user", UUID(uid)), updated)
    try:
        if replace(staged[0], operation_id=None) != replace(before_user, operation_id=None):
            raise HTTPException(409, "Personal context changed; reload before saving")
        with ops.tx() as db:
            cat.lock_rooms(db, (room_id,))
            cat.revalidate(db, before_room)
            if _members(db, room_id, uid) != active:
                raise cat.SecureStateChanged()
            ops.publish_update(db, staged)
    except BaseException:
        abandon_staged([staged])
        raise
    return _response(room, membership, updated, uid, room_id, active)
