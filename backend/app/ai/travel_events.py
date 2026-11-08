"""Deterministic travel answers from authorized, source-linked event memories."""

import re
from collections.abc import Iterable
from typing import Literal, NotRequired, TypedDict, assert_never

from app.ai.event_memory import matches_topic


class Participant(TypedDict):
    name: str
    modes: list[str]
    member_id: NotRequired[str]


class TravelEvent(TypedDict):
    event_id: str
    title: str
    category: str
    date: str | None
    end: str | None
    precision: str
    location: str | None
    participants: list[Participant]
    record_ids: list[str]
    tags: NotRequired[list[str]]
    travel_evidence: NotRequired[str]


class TravelSummary(TypedDict):
    answer: str
    count: int
    event_ids: list[str]
    record_ids: list[str]


def _date_key(event: TravelEvent) -> tuple[int, str]:
    date = event.get("date") or ""
    digits = re.sub(r"\D", "", date)[:8]
    return (int(digits.ljust(8, "0")) if digits else 99999999, event["event_id"])


def select_travel_events(
    memories: Iterable[TravelEvent], query: str, required_names: set[str], *, family: bool = False,
) -> list[TravelEvent]:
    """Select distinct trips/outings with every named person confirmed onsite.

    family=True is the explicit whole-family scope: any family member confirmed onsite.
    The caller supplies only authorized memories. Linked media may add source IDs
    but never establish attendance from an image caption or a person's name alone.
    """
    grouped: dict[str, list[TravelEvent]] = {}
    for memory in memories:
        if memory.get("event_id") and memory.get("record_ids"):
            grouped.setdefault(memory["event_id"], []).append(memory)
    selected: list[TravelEvent] = []
    if not required_names and not family:
        return selected
    for entries in grouped.values():
        for entry in entries:
            onsite = {person["name"] for person in entry.get("participants", [])
                      if "ONSITE" in person["modes"]}
            category_match = matches_topic(
                entry.get("category", ""), entry.get("title", ""),
                entry.get("location") or "", "여행",
            )
            travel_tags = sorted(set(entry.get("tags", [])) & {"여행", "나들이", "TRIP"})
            attended = bool(onsite) if family else required_names <= onsite
            if not attended or not (category_match or travel_tags):
                continue
            chosen = entry.copy()
            chosen["travel_evidence"] = ("category:" + entry.get("category", "") if category_match
                                         else "tags:" + ",".join(travel_tags))
            chosen["record_ids"] = list(dict.fromkeys(
                rid for linked in entries for rid in linked["record_ids"]
            ))
            selected.append(chosen)
            break
    return sorted(selected, key=_date_key)


def summarize_travel_events(
    events: list[TravelEvent], operation: Literal["count", "earliest", "latest"] = "count", *, family: bool = False,
) -> TravelSummary:
    """Return a source-linked answer without asking an LLM to count or order."""
    selected = events
    match operation:
        case "count":
            trips = sum(event.get("category") == "TRIP" or bool(
                set(event.get("tags", [])) & {"TRIP", "여행"}
            ) for event in events)
            outings = len(events) - trips
            who = "가족 구성원이 현장에 참여한" if family else "요청한 사람들이 함께 현장에 참여한"
            answer = (f"확인 가능한 기록에서 {who} 여행·나들이는 {len(events)}회입니다. "
                      f"(여행 {trips}회, 나들이 {outings}회) "
                      "같은 사건의 사진과 일기는 한 번으로 계산했습니다.")
        case "earliest" | "latest":
            dated = sorted((event for event in events if re.search(r"\d{4}", event.get("date") or "")),
                           key=_date_key)
            if dated:
                selected = [dated[0] if operation == "earliest" else dated[-1]]
                event = selected[0]
                label = "가장 이른" if operation == "earliest" else "가장 최근의"
                date = event["date"]
                if event.get("end") and event["end"] != date:
                    date = f"{date} ~ {event['end']}"
                location = f" 장소는 {event['location']}입니다." if event.get("location") else ""
                answer = f"함께 현장에 참여한 기록 중 {label} 여행·나들이는 {date}의 ‘{event['title']}’입니다.{location}"
                if event.get("precision") == "APPROXIMATE":
                    answer += " 날짜는 대략적으로 기록되어 있습니다."
                if len(dated) < len(events):
                    answer += " 날짜가 확인된 사건을 기준으로 비교했습니다."
            else:
                selected = []
                answer = "함께 현장에 참여한 여행·나들이 중 날짜까지 확인되는 기록을 찾지 못했습니다."
        case unreachable:
            assert_never(unreachable)
    return {"answer": answer, "count": len(events),
            "event_ids": [event["event_id"] for event in selected],
            "record_ids": list(dict.fromkeys(rid for event in selected for rid in event["record_ids"]))}
