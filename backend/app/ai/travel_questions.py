import json
import re
from dataclasses import replace

from app.ai import event_memory
from app.ai.grounded_answering import GroundedAnswerSnapshot, build_answer_plan, build_fact_sheet, build_query_frame
from app.ai.query_normalization import ENDING, is_speech_act
from app.ai.record_counts import is_count_query
from app.ai.travel_events import _date_key, select_travel_events, summarize_travel_events


# '놀러 간 곳' is the same trip/outing set as '여행'; '첫 여행' is the earliest one.
TRAVEL = r"여행|나들이|놀러"
FIRST = r"처음|최초|첫\s*(?:번째|여행|나들이)"
TEMPORAL = FIRST + r"|최근|마지막|최신"


def travel_context(payload):
    current = re.sub(r"처음부터\s*끝까지", "전부", payload.query)  # 'all of them', not the earliest
    if payload.selected_record_id is not None:
        return None
    history = payload.history if payload.context_mode != "ROOM" else []
    # '그 여행 사진' refers back to the travel in the conversation; it does not start a new travel question.
    bare = re.sub(r"그\s*(?:여행|나들이)", "", current)
    explicit = bool(re.search(TRAVEL, bare))
    if not explicit:
        if event_memory.categories(current) and "TRIP" not in event_memory.categories(current):
            return None
        if not re.search(TEMPORAL + r"|그때|그곳|거기|사진|이미지|음성|영상", current):
            return None
    # A turn that only points back ('그 여행 기록 요약해줘') is not a seed: the seed is the turn it points to.
    seed_index = next((i for i in range(len(history) - 1, -1, -1)
                       if re.search(TRAVEL, re.sub(r"그\s*(?:여행|나들이)", "", history[i].query))), None)
    seed = current if explicit else history[seed_index].query if seed_index is not None else None
    if seed is None:
        return None
    if re.search(r"해외|국내|단둘|둘이|\d{4}|작년|올해|지난해|이번\s*달|지난\s*달", seed + " " + current):
        return None
    relevant_history = history[seed_index:] if seed_index is not None else []
    if not explicit and any(event_memory.categories(turn.query)
                            and "TRIP" not in event_memory.categories(turn.query)
                            and not re.search(r"나들이", turn.query) for turn in relevant_history):
        return None
    temporal = current
    if not re.search(TEMPORAL, temporal):
        temporal = next((turn.query for turn in reversed(relevant_history)
                         if re.search(TEMPORAL, turn.query)), "")
    operation = "latest" if re.search(r"최근|마지막|최신", temporal) else "earliest" if re.search(FIRST, temporal) else "count"
    media = "IMAGE" if re.search(r"사진|이미지", current) else "AUDIO" if re.search(r"음성|녹음", current) else "VIDEO" if "영상" in current else None
    if media and (is_count_query(current) or operation == "count"):
        return None
    if media and not re.search(TEMPORAL, current):
        if explicit or event_memory.categories(bare):
            return None
        if not re.fullmatch(r"(?:그때|그곳|거기|그\s*여행)?\s*(?:의\s*)?(?:사진|이미지|음성|녹음|영상).*", current):
            return None
    if not media and not is_count_query(current) and not re.search(TEMPORAL, current):
        if not explicit or not re.search(r"알려|어디|곳|여행지|목록|장소|리스트|꼽아|정리", current):
            return None
        operation = "list"
    if is_count_query(current):
        operation = "count"
    return seed + " " + current, operation, media


def answer_travel_question(runtime, user_id, room_id, payload, metadata, corpus, personal):
    context = travel_context(payload)
    if context is None or not personal:
        return None
    from app.ai.record_questions import resolve_addresses

    raw, operation, media = context
    query, _ = resolve_addresses(raw, personal)
    current, _ = resolve_addresses(payload.query, personal)
    names = {person["name"] for person in personal["people"]} | {personal["viewer_name"]}
    subjectless = query
    for name in names:
        subjectless = re.sub(re.escape(name) + r"(?:이랑|랑|이하고|하고|와|과|은|는|이|가|의|도|한테)?", " ", subjectless)
    # Bounded vocabulary: any other word may be an unimplemented constraint, so the question defers.
    supported = (
        r"(?:여행지?|나들이|사진|이미지|음성|녹음|영상|곳|장소|데|기록|횟수|목록|리스트)(?:들)?"
        r"(?:은|는|이|가|을|를|의|에|에는|으로|로|도|이랑|랑|하고|와|과)?"
        r"|우리|가족|함께|함께한|같이|갔던|했던|가|본|중|번째(?:로)?|(?:처음|최근|마지막|최신|최초)(?:에|에는|엔|은|는|으로|로)?"
        r"|모든|전체|전부|모두|다|지금까지|여태|그동안|다녀온|제일|가장|첫|번째|그때|그곳|거기|그|좀|몇|총|간|한"
        rf"|몇?(?:번|회|차례)(?:이나|쯤|째|로)?{ENDING}*"
        r"|(?:갔|했|있)(?:어|지|나|니|었어|었지|었던|나요|어요|지요)?"
        r"|다녀(?:온|간|왔|왔어|왔지|왔던|왔었어|왔나)|놀러(?:간|갔던|갔어|갔지|갔었어|다닌|다녀온)?"
        rf"|(?:어디(?:어디)?|언제)(?:로|에)?{ENDING}*"
        r"|알려줘|알려주세요|보여줘|보여주세요|보내줘|보내주세요|들려줘|재생해줘"
    )
    if any(not re.fullmatch(supported, word) and not is_speech_act(word, movement=True)
           for word in re.findall(r"[\w가-힣]+", subjectless)):
        return None
    current_names = {name for name in names if name in current}
    # '우리 가족 여행' is the whole-family scope, never silently the viewer-only set.
    family = "가족" in query
    if family and current_names:
        return None
    actor_query = current if current_names else query
    if not current_names and payload.context_mode != "ROOM":
        for turn in reversed(payload.history):
            previous, _ = resolve_addresses(turn.query, personal)
            if any(name in previous for name in names):
                actor_query = previous
                break
    required = set() if family else {name for name in names if name in actor_query}
    if not family and not required or len(required) == 1 and (
        re.search(r"함께|같이", actor_query)
        or any(re.search(re.escape(name) + r"(?:이랑|랑|이하고|하고|와|과)", actor_query) for name in required)
    ):
        required.add(personal["viewer_name"])
    memories = event_memory.retrieve(runtime, user_id, room_id, "여행", set(metadata), include_all=True)
    if not memories:
        return None
    for memory in memories:
        source = metadata[memory["record_ids"][0]].get("m2_metadata", {})
        if source.get("event_ids"):
            memory["event_id"] = source["event_ids"][0]
        linked = [record for record in metadata.values()
                  if memory["event_id"] in record.get("m2_metadata", {}).get("event_ids", [])]
        memory["tags"] = [tag for record in linked for tags in (
            record.get("tags", []), record.get("m2_metadata", {}).get("tags", []))
            if isinstance(tags, list) for tag in tags if isinstance(tag, str)]
    events = select_travel_events(memories, query, required, family=family)
    # list reuses the count set verbatim so enumeration can never disagree with the count.
    result = summarize_travel_events(events, "count" if operation == "list" else operation, family=family)
    chosen_ids = set(result["event_ids"])
    source_ids = set(result["record_ids"])
    limit = payload.k
    if operation == "list":
        ordered = sorted(events, key=_date_key)
        lines = [f"{index}. {event.get('date') or '날짜 미상'} ‘{event['title']}’"
                 + (f" ({event['location']})" if event.get("location") else "")
                 for index, event in enumerate(ordered, 1)]
        limit = max(limit, len(source_ids))
    if media:
        selected = [chunk for chunk in corpus.chunks if json.loads(chunk.source_locator_json).get("media_type") == media
                    and chosen_ids.intersection(metadata[chunk.record_id].get("m2_metadata", {}).get("event_ids", []))]
        selected = list({chunk.record_id: chunk for chunk in selected}.values())[:min(payload.k, 3)]
        answer = "해당 여행·나들이의 기록입니다.\n" + "\n".join(metadata[c.record_id].get("title", "기록") for c in selected)
    else:
        selected = list({chunk.record_id: chunk for chunk in corpus.chunks if chunk.record_id in source_ids}.values())[:limit]
        if family:
            viewer = personal["viewer_name"]
            own = len(select_travel_events(memories, query, {viewer}))
            answer = (f"가족 전체 기록 기준(질문자 참여 여부와 무관)으로, {result['answer']} "
                      f"이 중 {viewer} 본인이 현장에 참여한 것은 {own}회입니다.")
        else:
            answer = ", ".join(sorted(required)) + "의 현장 참여가 확인된 기록 기준으로, " + result["answer"]
        if operation == "list":
            answer += "\n" + "\n".join(lines)
    sheet = build_fact_sheet(selected)
    plan = build_answer_plan(sheet, k=limit) if sheet.facts else None
    frame = replace(build_query_frame(query), raw_query=payload.query)
    return GroundedAnswerSnapshot(frame, payload.model_id, "ANSWER" if plan else "NO_RECORD",
        answer if plan else "요청한 사람들의 현장 참여와 연결된 자료를 확인하지 못했습니다. 다른 사건의 자료로 대신 보여드리지는 않습니다.",
        build_fact_sheet(corpus.chunks), plan, corpus, record_discovery=True)
