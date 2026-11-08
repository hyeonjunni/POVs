import re
from dataclasses import replace

from app.ai import event_memory
from app.ai.query_normalization import is_speech_act
from app.ai.grounded_answering import (
    GroundedAnswerSnapshot,
    build_answer_plan,
    build_fact_sheet,
    build_query_frame,
)


def resolve_listing_subjects(query, personal):
    if not personal:
        return None
    from app.ai.record_questions import resolve_addresses

    resolved, _ = resolve_addresses(query, personal)
    names = {person["name"] for person in personal["people"]} | {personal["viewer_name"]}
    required = {name for name in names if name in resolved}
    if not required:
        return None
    remainder = resolved
    for name in sorted(names, key=len, reverse=True):
        remainder = re.sub(re.escape(name) + r"(?:이랑|랑|와|과|은|는|이|가|의|하고)?", " ", remainder)
    broad = re.search(r"전체|모든|전부|모두|빠짐없이|처음부터\s*끝까지|지금까지|여지껏|여태|무슨\s*일|어떤\s*일|뭐|목록|(?<![가-힣])[다싹쭉](?![가-힣])"
                      r"|(?:했던|있었던|있던|함께한|함께였던|같이\s*한|겪은|겪었던)\s*(?:일|것|순간)", remainder)
    # A request to show "records" of two people together is a list request; one person alone stays deferred.
    pair = len(required) > 1 or re.search(r"같이|함께", resolved) or any(
        re.search(re.escape(name) + r"(?:이랑|랑|와|과|하고)", resolved) for name in required)
    if not broad and not (pair and re.search(r"기록\S*\s*(?:보여|알려|찾아)", remainder)):
        return None
    supported = (
        r"전체|모든|전부|지금까지|여태|그동안|이제까지|처음부터|끝까지|우리|같이|함께|하고"
        r"|무슨|어떤|뭐(?:가|야)?|뭘|뭐뭐|(?:무슨|어떤|했던|있었던|있던|함께한|겪은)?(?:일|것|순간)(?:이|은|을|들|들이|들을)?"
        r"|사건(?:이|은|을|들|들이|들을)?"
        r"|기록(?:이|은|을|들|들이|들을)?|목록(?:이|은|을)?|다|좀|쭉"
        r"|있(?:어|지|나|니|었어|었지|었나|었니|었는지|는지|나요|어요|었나요|었어요)?"
        r"|했(?:어|지|나|니|었어|었지|는지|나요|어요|었나요|었어요)?|한|했던|있던|있었던|함께한|관련된"
        r"|알려줘|알려주세요|보여줘|보여주세요|찾아줘|정리해줘|있지"
    )
    if any(not re.fullmatch(supported, word) and not is_speech_act(word)
           for word in re.findall(r"[\w가-힣]+", remainder)):
        return None
    if len(required) == 1 and (
        re.search(r"같이|함께|둘이", resolved)
        or any(re.search(re.escape(name) + r"(?:이랑|랑|와|과|하고)", resolved) for name in required)
    ):
        required.add(personal["viewer_name"])
    return required


def answer_event_listing(runtime, user_id, room_id, payload, metadata, corpus, personal):
    if payload.selected_record_id is not None:
        return None
    required = resolve_listing_subjects(payload.query, personal)
    if not required:
        return None
    memories = event_memory.retrieve(runtime, user_id, room_id, payload.query, set(metadata), include_all=True)
    if not memories:
        return None
    grouped = {}
    for memory in memories:
        participants = {person["name"] for person in memory.get("participants", [])
                        if {"ONSITE", "REMOTE", "SUPPORT"}.intersection(person["modes"])}
        if not required <= participants or not memory.get("record_ids"):
            continue
        source = metadata.get(memory["record_ids"][0], {}).get("m2_metadata", {})
        event_ids = source.get("event_ids") or []
        event_id = event_ids[0] if len(event_ids) == 1 else memory["event_id"]
        if event_id not in grouped:
            grouped[event_id] = dict(memory, event_id=event_id, record_ids=[])
        grouped[event_id]["record_ids"] = list(dict.fromkeys(
            grouped[event_id]["record_ids"] + memory["record_ids"]))
    if not grouped:
        return GroundedAnswerSnapshot(
            build_query_frame(payload.query), payload.model_id, "NO_RECORD",
            ", ".join(sorted(required)) + "의 참여가 함께 확인된 사건을 현재 볼 수 있는 기록에서 찾지 못했습니다.",
            build_fact_sheet(corpus.chunks), None, corpus, record_discovery=True,
        )
    events = sorted(grouped.values(), key=lambda event: (
        re.sub(r"\D", "", event.get("date") or "")[:8].ljust(8, "0")
        if event.get("date") else "99999999", event["event_id"]))
    source_ids = {rid for event in events for rid in event["record_ids"]}
    selected = list({chunk.record_id: chunk for chunk in corpus.chunks if chunk.record_id in source_ids}.values())
    if {chunk.record_id for chunk in selected} != source_ids:
        return None
    lines = []
    for event in events:
        date = event.get("date") or "날짜 미상"
        if event.get("end") and event["end"] != date:
            date += " ~ " + event["end"]
        if event.get("precision") == "APPROXIMATE":
            date += " (대략)"
        remote = sorted({person["name"] for person in event["participants"]
                         if person["name"] in required and "REMOTE" in person["modes"]
                         and "ONSITE" not in person["modes"]})
        participation = " · " + ", ".join(remote) + " 원격 참여" if remote else ""
        support = sorted({person["name"] for person in event["participants"]
                          if person["name"] in required and "SUPPORT" in person["modes"]
                          and not {"ONSITE", "REMOTE"}.intersection(person["modes"])})
        if support:
            participation += " · " + ", ".join(support) + " 지원 참여"
        lines.append(f"- {date}: {event['title']}{participation}")
    answer = (", ".join(sorted(required)) + "의 참여가 확인된 사건은 현재 볼 수 있는 기록 기준으로 총 "
              + str(len(events)) + "건입니다. 현장·원격·지원 참여를 포함하고 같은 사건의 여러 기록은 하나로 묶었습니다.\n"
              + "\n".join(lines))
    sheet = build_fact_sheet(selected)
    return GroundedAnswerSnapshot(
        replace(build_query_frame(payload.query), raw_query=payload.query), payload.model_id, "ANSWER", answer,
        build_fact_sheet(corpus.chunks), build_answer_plan(sheet, k=len(sheet.facts)), corpus,
        record_discovery=True, event_memory=tuple(events),
    )
