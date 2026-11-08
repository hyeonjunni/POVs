"""게이트 공용 텍스트 규칙 — 조사 제거, 내용어, hard-atom 추출, 한국어 판정, 누수 패턴.

전부 결정론·stdlib. DGX A/B 실험(research_notes/gate_ab_eval_2026-07-12.py)에서 검증된
로직을 그대로 옮겼다. hard atom = 숫자(시각/금액/날짜 성분)와 따옴표 인용구 —
한국어 NER 없이도 오탐이 낮은, 객관 대조 가능한 집합.
"""
from __future__ import annotations

import re
import unicodedata

# SCPC 이식: 안전한 단방향 표기 접기 (메신저 종결 변이·맞춤법 혼동 — 검증된 목록만)
CANONICAL_FOLDS = (
    ("해주세여", "해줘"), ("해주세용", "해줘"), ("해주세요", "해줘"), ("해줘요", "해줘"),
    ("알려주세여", "알려줘"), ("알려주세요", "알려줘"), ("알려줘요", "알려줘"),
    ("됬", "됐"), ("안됨", "안된다"), ("안돼", "안된다"),
    # 흔한 맞춤법 오류 → 표준형 (cue 어휘와 직결되는 것만: 몇일은 며칠의 대표적 오기)
    ("몇 일", "며칠"), ("몇일", "며칠"), ("메칠", "며칠"), ("몇 칠", "며칠"),
    ("어데", "어디"), ("오데", "어디"), ("어듸", "어디"),
    ("얼마에요", "얼마"), ("얼마예요", "얼마"), ("멫", "몇"),
)

# ── SCPC 이식: 두벌식 인접키 자모 오타 1글자 허용 매칭 ─────────────────────────
# 한글 4음절 이상 cue에만 적용 (짧은 cue는 오탐 위험 — SCPC 검증 임계 그대로)
_HANGUL_LEADS = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"
_HANGUL_VOWELS = "ㅏㅐㅑㅒㅓㅔㅕㅖㅗㅘㅙㅚㅛㅜㅝㅞㅟㅠㅡㅢㅣ"
_HANGUL_TAILS = "\0ㄱㄲㄳㄴㄵㄶㄷㄹㄺㄻㄼㄽㄾㄿㅀㅁㅂㅄㅅㅆㅇㅈㅊㅋㅌㅍㅎ"
_DUBEOLSIK_ROWS = ("ㅂㅈㄷㄱㅅ", "ㅁㄴㅇㄹㅎ", "ㅋㅌㅊㅍ", "ㅛㅕㅑㅐㅔ", "ㅗㅓㅏㅣ", "ㅠㅜㅡ")
_DUBEOLSIK_POSITION = {
    jamo: (row_index, column_index)
    for row_index, row in enumerate(_DUBEOLSIK_ROWS)
    for column_index, jamo in enumerate(row)
}


def _hangul_parts(char: str):
    offset = ord(char) - 0xAC00
    if not 0 <= offset < 11172:
        return None
    lead, rest = divmod(offset, 588)
    vowel, tail = divmod(rest, 28)
    return _HANGUL_LEADS[lead], _HANGUL_VOWELS[vowel], _HANGUL_TAILS[tail]


def _adjacent_jamo(left: str, right: str) -> bool:
    lp = _DUBEOLSIK_POSITION.get(left)
    rp = _DUBEOLSIK_POSITION.get(right)
    return lp is not None and rp is not None and lp[0] == rp[0] and abs(lp[1] - rp[1]) == 1


def one_adjacent_jamo_typo(text: str, cue: str) -> bool:
    """cue와 정확히 한 글자만 다르고, 그 차이가 인접키 자모 1개인 창이 text에 있으면 True."""
    hangul_count = sum("가" <= ch <= "힣" for ch in cue)
    if hangul_count < 4:
        return False
    for start in range(len(text) - len(cue) + 1):
        window = text[start:start + len(cue)]
        diffs = [(a, b) for a, b in zip(window, cue) if a != b]
        if len(diffs) != 1:
            continue
        left_parts = _hangul_parts(diffs[0][0])
        right_parts = _hangul_parts(diffs[0][1])
        if left_parts is None or right_parts is None:
            continue
        comp = [(a, b) for a, b in zip(left_parts, right_parts) if a != b]
        if len(comp) == 1 and _adjacent_jamo(*comp[0]):
            return True
    return False


def normalize_query(text: str) -> str:
    out = unicodedata.normalize("NFC", text or "")
    for variant, canonical in CANONICAL_FOLDS:
        out = out.replace(variant, canonical)
    return out


def has_cue(text: str, cues) -> bool:
    """공백·오타 강건 cue 매칭 — 표기 접기 + 무공백 + 인접키 자모 오타 1글자(4음절+ cue만)."""
    normalized = normalize_query(text)
    squashed = normalized.replace(" ", "")
    for cue in cues:
        squashed_cue = cue.replace(" ", "")
        if cue in normalized or squashed_cue in squashed:
            return True
        if one_adjacent_jamo_typo(squashed, squashed_cue):
            return True
    return False

JOSA = ["에서", "으로", "에게", "께서", "이었는지", "였는지", "인지", "과", "와", "은", "는",
        "이", "가", "을", "를", "도", "만", "의", "에", "로", "야"]

STOP = {"기록", "기록에", "알려줘", "말해줘", "써줘", "정리해줘", "요약해줘", "있으면", "없으면",
        "정확한", "정확히", "가족", "가족방", "문장", "문장으로", "문단으로", "바탕으로", "몇",
        "언제", "어디", "누구", "누가", "무엇", "뭐였는지", "이름", "우리", "그날", "기준"}

LEAK_PATTERNS = [r"</?think>", r"^#+\s", r"Retrieved Evidence", r"retrieved_evidence",
                 r"tags\s*=", r"confidence\s*=", r"persona", r"instructions?\s*:",
                 r"system prompt", r"\bsaved_at\b"]

REFUSAL_CUES = ("확인되지 않", "기록에 없", "기록에는 없", "남아 있지 않", "알 수 없",
                "확인할 수 없", "나와 있지 않", "적혀 있지 않")


def strip_josa(token: str) -> str:
    for josa in sorted(JOSA, key=len, reverse=True):
        if token.endswith(josa) and len(token) > len(josa) + 1:
            return token[: -len(josa)]
    return token


def content_tokens(text: str) -> set[str]:
    text = re.sub(r"[^\w가-힣\s]", " ", text or "")
    out: set[str] = set()
    for raw in text.split():
        token = strip_josa(raw)
        if len(token) >= 2 and token not in STOP:
            out.add(token)
    return out


def num_atoms(text: str) -> set[str]:
    from app.ai.gates.counts import count_atoms
    atoms: set[str] = {str(value) for _, value, _ in count_atoms(text or "")}
    for m in re.finditer(r"\d+(?::\d+)?", text or ""):
        atoms.add(m.group().split(":")[0].lstrip("0") or "0")
        if ":" in m.group():
            atoms.add(m.group())
    return atoms


def quote_atoms(text: str) -> set[str]:
    return {m.group(1).strip()
            for m in re.finditer(r"[‘'\"“]([^’'\"”]{2,40})[’'\"”]", text or "")}


# ── 타입 값 원자 (Phase 2: UX-G05/06/08/09/10) ──────────────────────────────
# 시각: 오전/오후·12/24시제를 '가능한 하루-분 후보 집합'으로 비교한다.
# 접두 없는 "2시"는 {02:00, 14:00} 두 후보 — 후보 교집합이 있으면 같은 시각일 수 있다.
_TIME_RE = re.compile(
    r"(?:(오전|오후)\s*)?(?<![\d:])(\d{1,2})\s*시(?:\s*(\d{1,2})\s*분)?|(?<![\d:])(\d{1,2}):(\d{2})(?!\d)")


def time_atoms(text: str) -> list[tuple[str, frozenset[int]]]:
    """(표면형, 하루-분 후보 집합) 목록. '10시'={600,1320}, '오후 2시'={840}, '14:00'={840}."""
    out: list[tuple[str, frozenset[int]]] = []
    for m in re.finditer(_TIME_RE, text or ""):
        if m.group(2) is not None:
            hour, minute = int(m.group(2)), int(m.group(3) or 0)
            if hour > 24 or minute > 59:
                continue
            meridiem = m.group(1)
            if meridiem == "오전":
                cands = {(hour % 12) * 60 + minute}
            elif meridiem == "오후":
                cands = {((hour % 12) + 12) * 60 + minute}
            elif hour <= 12:
                cands = {(hour % 12) * 60 + minute, ((hour % 12) + 12) * 60 + minute}
            else:
                cands = {(hour % 24) * 60 + minute}
        else:
            hour, minute = int(m.group(4)), int(m.group(5))
            if hour > 24 or minute > 59:
                continue
            cands = {(hour % 24) * 60 + minute}
        out.append((m.group(0).strip(), frozenset(cands)))
    return out


# ── 가족 어휘 접기 (Phase 4: UX-G01) ────────────────────────────────────────
# 존칭/평칭 표기 차이로 검색·인물 판정이 갈리면 안 된다. 대표형(격식형)으로 접는다.
# "규칙은 코드, 사전은 데이터" — 특정 가족의 고유 표현이 아니라 한국어 일반 호칭만 둔다.
FAMILY_TERM_FOLDS: tuple[tuple[str, str], ...] = (
    ("어머님", "어머니"), ("엄마", "어머니"), ("모친", "어머니"),
    ("아버님", "아버지"), ("아빠", "아버지"), ("부친", "아버지"),
    ("할머님", "할머니"), ("할아버님", "할아버지"),
    ("생신", "생일"),
)
_FAMILY_FOLDS_ORDERED = sorted(FAMILY_TERM_FOLDS, key=lambda p: len(p[0]), reverse=True)


def fold_family_terms(text: str) -> str:
    """호칭·경어 변형을 대표형으로 — 검색 haystack/질의, 인물 판정에 공통 적용."""
    folded = text or ""
    for variant, canonical in _FAMILY_FOLDS_ORDERED:
        folded = folded.replace(variant, canonical)
    return folded


# ── 관련도 발췌 (Phase 4: UX-G14) ──────────────────────────────────────────
# 부분답변의 핵심(연도·시각·금액 등 값)이 고정 42자 절단 뒤에 있으면 인용이 무의미하다.
_EXCERPT_VALUE_RE = re.compile(
    r"\d{4}\s*년(?:\s*[가-힣]{1,3})?|\d{1,2}\s*월\s*\d{1,2}\s*일|"
    r"(?:오전|오후)?\s*\d{1,2}\s*시(?:\s*\d{1,2}\s*분)?|\d{1,2}:\d{2}|"
    r"\d[\d,]*\s*(?:만\s*)?원|\d+\s*(?:명|개|번|살)")


def evidence_excerpt(text: str, query: str = "", limit: int = 42) -> str:
    """인용 발췌 — 값(연도·시각·금액…) 또는 질의어 주변을 창으로 잘라 핵심을 보존한다."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    match = _EXCERPT_VALUE_RE.search(text)
    if match is None:
        for token in sorted(content_tokens(query), key=len, reverse=True):
            pos = text.find(token)
            if pos >= 0:
                class _M:  # noqa: N801 - 간단한 위치 홀더
                    start_pos = pos
                    end_pos = pos + len(token)
                match = _M
                break
    if match is None:
        return text[:limit] + "…"
    start_pos = match.start() if hasattr(match, "start") else match.start_pos
    end_pos = match.end() if hasattr(match, "end") else match.end_pos
    center = (start_pos + end_pos) // 2
    begin = max(0, min(center - limit // 2, len(text) - limit))
    end = begin + limit
    prefix = "…" if begin > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{text[begin:end].strip()}{suffix}"


# 시간 범위: "10시에서 11시 사이" — 두 점값이 아니라 구간 하나다 (UX-G15).
_TIME_RANGE_RE = re.compile(
    r"(?:(오전|오후)\s*)?(\d{1,2})\s*시\s*(?:에서|부터|~|∼|-)\s*(?:(오전|오후)\s*)?(\d{1,2})\s*시\s*(?:사이|까지)")


def _hour_cands(meridiem: str | None, hour: int) -> set[int]:
    if meridiem == "오전":
        return {(hour % 12) * 60}
    if meridiem == "오후":
        return {((hour % 12) + 12) * 60}
    if hour <= 12:
        return {(hour % 12) * 60, ((hour % 12) + 12) * 60}
    return {(hour % 24) * 60}


def time_intervals(text: str) -> list[tuple[str, list[tuple[int, int]]]]:
    """(표면형, 후보 (시작,끝) 구간 목록). 점값이 구간 안에 들면 모순이 아니다."""
    out: list[tuple[str, list[tuple[int, int]]]] = []
    for m in re.finditer(_TIME_RANGE_RE, text or ""):
        starts = _hour_cands(m.group(1), int(m.group(2)))
        ends = _hour_cands(m.group(3) or m.group(1), int(m.group(4)))
        pairs = [(a, b) for a in starts for b in ends if a <= b]
        if pairs:
            out.append((m.group(0).strip(), pairs))
    return out


def time_display(surface: str, cands: frozenset[int]) -> str:
    """모순 표시용 한국어 표면형 — '11:00' 같은 표기는 '11시'로 통일한다."""
    if "시" in surface:
        return surface
    minute_of_day = sorted(cands)[0]
    hour, minute = divmod(minute_of_day, 60)
    return f"{hour}시" if minute == 0 else f"{hour}시 {minute}분"


# 금액: 5만원 == 50,000원 == 50000원 — 원화 표기를 정수 원으로 정규화해 대조한다.
_AMOUNT_RE = re.compile(
    r"(?<![\d,.])(?:(\d+)\s*만\s*(\d+)\s*천\s*원|(\d+)\s*만\s*원|(\d+)\s*천\s*원|(\d{1,3}(?:,\d{3})+)\s*원|(\d+)\s*원)")


def amount_atoms(text: str) -> list[tuple[str, int]]:
    """(표면형, 정규화 금액[원]) 목록."""
    out: list[tuple[str, int]] = []
    for m in re.finditer(_AMOUNT_RE, text or ""):
        if m.group(1) is not None:
            value = int(m.group(1)) * 10000 + int(m.group(2)) * 1000
        elif m.group(3) is not None:
            value = int(m.group(3)) * 10000
        elif m.group(4) is not None:
            value = int(m.group(4)) * 1000
        elif m.group(5) is not None:
            value = int(m.group(5).replace(",", ""))
        else:
            value = int(m.group(6))
        out.append((m.group(0).strip(), value))
    return out


_LIST_MARKER_RE = re.compile(r"(?<![\d.,])([1-9]\d?)\.(?=\s)")


def list_format_markers(text: str) -> list[str]:
    """출력 '형식' 번호 판정 — 1.부터 1씩 오르는 목록 마커(2개 이상)일 때만 형식으로 인정.

    형식 번호는 사실 주장이 아니므로 근거 대조에서 제외한다. 그 외 숫자는 전부
    사실 값으로 취급한다 (UX-G08 vs UX-G09: '3. '은 형식, '3번 방문'은 사실).
    """
    numbers = [int(m.group(1)) for m in re.finditer(_LIST_MARKER_RE, text or "")]
    if len(numbers) >= 2 and numbers == list(range(1, len(numbers) + 1)):
        return [f"{n}." for n in numbers]
    return []


def unsupported_atoms(text: str, evidence_text: str) -> set[str]:
    """타입 값(시각·금액)은 동치 비교, 잔여 숫자는 원자 일치, 인용구는 substring 대조.

    Phase 0: 검색된 증거만 신뢰한다. 질의에 등장한 값은 검증할 '주장'이지
    근거가 아니다 — 질문에 날짜를 심고 복창시키는 우회를 막는다.
    Phase 2: 1~3 전역 허용 제거 — 형식 번호는 호출측이 마스킹해 넘긴다(list_format_markers).
    """
    unsupported: set[str] = set()
    masked = text or ""

    ev_times = time_atoms(evidence_text)
    for surface, cands in time_atoms(masked):
        if any(cands & ev_cands for _, ev_cands in ev_times):
            masked = masked.replace(surface, " ")
        else:
            unsupported.add(surface)

    ev_amounts = {value for _, value in amount_atoms(evidence_text)}
    for surface, value in amount_atoms(masked):
        if value in ev_amounts:
            masked = masked.replace(surface, " ")
        else:
            unsupported.add(surface)

    allowed_nums = num_atoms(evidence_text)
    unsupported |= {n for n in num_atoms(masked) if n not in allowed_nums}
    unsupported |= {q for q in quote_atoms(text) if q not in evidence_text}
    return unsupported


def strip_leaks(text: str) -> tuple[str, bool]:
    leaked = False
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    kept_lines = []
    for line in text.splitlines():
        if any(re.search(p, line, flags=re.I) for p in LEAK_PATTERNS):
            leaked = True
            continue
        kept_lines.append(line)
    return "\n".join(kept_lines).strip(), leaked


def korean_ok(text: str) -> bool:
    letters = re.findall(r"[A-Za-z가-힣]", text or "")
    if not letters:
        return False
    hangul = re.findall(r"[가-힣]", text)
    return len(hangul) / len(letters) >= 0.5


def looks_refusal(text: str) -> bool:
    return any(cue in (text or "") for cue in REFUSAL_CUES)
