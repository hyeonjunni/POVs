"""Speech-act layer of a family question: how the user asks, never what they ask about.

Request verbs (알려줄래요, 정리해 주세요), generic narration (있었던, 겪은, 함께한) and polite or
casual endings carry no record constraint. Routing vocabularies and the media constraint guard
ignore them. Everything else stays a possible constraint: nouns (places, years, activities),
negation (없, 안, 못), counts (셋이, 둘이, 하나만) and months are never matched here.
Movement verbs (갔던, 다녀온, 놀러, 여행했던) are speech acts only where travel is already the
topic (travel tool, media guard); elsewhere they name the topic and must stay a constraint.
"""
import re

# Verb stems whose inflections only request, ask or narrate being/doing. A stem must be followed by
# ending syllables only, so nouns that share a first syllable (주말, 가게, 한강, 간식) are never
# swallowed. Single-syllable stems are past/irregular forms that never start a noun (했, 있, 겪, 찍).
_STEMS = ("알려|보여|말해|정리해|꼽아|꺼내|세어|찾아|들려|틀어|재생해|보내|모아|나열해|읊어|얘기해|설명해|확인해"
          "|했|햇|있|겪|찍|나왔|나오|남아|남은|남긴|남겨|맞|함께하|함께했|함께한|함께였|같이하|같이했|같이한"
          "|어떻게|어땠|어디|언제|누구|봐|싶|마치|마쳤|마친|궁금|열어|띄워|기억나|읽어")
_MOVEMENT = "갔|갓|다녀|다녔|다닌|떠나|떠난|떠났|놀러|가봤|가본|여행하|여행했|여행한|여행간|여행갔|돌아오|돌아온|돌아왔"
ENDING = "[혔하해에엇혀이인힌왔봤어아여요죠지니냐나야다더라던든는은을래까게고서며음써봐줘줄주세시실수었았였겠습네군구온간갈본볼한할셈쥬으면]"
_SPEECH_ACT = re.compile(f"(?:{_STEMS}){ENDING}*")
_MOVEMENT_ACT = re.compile(f"(?:{_MOVEMENT}){ENDING}*")
# The separated auxiliary of '정리해 주세요', '보여 줄래' and the copula of '몇 번이나 돼?'. Spelled out
# so that nouns such as 주인, 주간, 돼지 are never swallowed.
_AUXILIARY = re.compile(r"주(?:세요|세여|실래요?|시겠어요|라|어|어요)|줘요?|줄래요?|돼(?:요)?|됐(?:어|지|나)|되(?:나요?|지|는지)|나와요|나온|나왔나요|세(?:줘|줄래|주세요)|거(?:야|예요|에요|지|니|냐|였지|였어|였더라)")
FILLERS = frozenset({"좀", "혹시", "한번", "다", "싹", "쭉", "전부", "모두", "빠짐없이", "그냥", "수", "거", "것",
                     "게", "건", "걸", "기록상", "지금껏", "여지껏", "여태껏", "쯤", "정도", "대충",
                     "젤", "제일", "가장", "해서", "합쳐서", "합해서", "통틀어", "하나하나", "일일이", "순서대로", "차례대로"})


def is_speech_act(word: str, movement: bool = False) -> bool:
    return (word in FILLERS or bool(_SPEECH_ACT.fullmatch(word) or _AUXILIARY.fullmatch(word))
            or movement and bool(_MOVEMENT_ACT.fullmatch(word)))


# An explicit topic switch closes the previous topic ('그건 그렇고', '여행 얘긴 됐고', '다른 질문인데').
# 'X 말고' is usually an exclusion ('여행 말고 엄마랑 있었던 일'); it closes topic X only when the rest
# names another domain ('여행 말고 내 건강검진 몇 번 했어?').
_TOPIC_SWITCH = re.compile(r"^\s*(?:그건\s*그렇고|[가-힣]+\s*(?:얘기[는은]?|얘긴|이야기[는은]?)\s*(?:됐고|그만하고)"
                           r"|다른\s*(?:질문|얘기|이야기)인데)\s*[,.]?\s*")
_DOMAINS = {"여행": r"여행|나들이", "일기": r"일기", "검진": r"검진|검사", "생일": r"생일"}
_EXCEPT = re.compile(r"^\s*(여행|일기|건강검진|검진|생일)\s*말고\s*[,.]?\s*")


def topic_switch(payload):
    """Drop the closing phrase and the history, so the rest is answered as a new topic."""
    rest = _TOPIC_SWITCH.sub("", payload.query, count=1)
    closed = _EXCEPT.match(payload.query)
    if rest == payload.query and closed:
        closed_domain = "검진" if closed.group(1) == "건강검진" else closed.group(1)
        other = payload.query[closed.end():]
        if any(re.search(pattern, other) for domain, pattern in _DOMAINS.items() if domain != closed_domain):
            rest = other
    if rest == payload.query or not rest.strip() or payload.selected_record_id is not None:
        return payload
    return payload.model_copy(update={"query": rest, "context_mode": "ROOM", "history": []})


if __name__ == "__main__":
    for word in ("알려줄래요", "정리해", "주세요", "주실래요", "보여주세여", "찾아줄래요", "겪은", "있으면", "있나요",
                 "나와요", "함께였던", "어떻게", "돼요", "돼", "찍은", "꼽아줄래", "있엇던", "언제였지", "어디였더라",
                 "싶어", "봐줘", "찍혀", "마치고", "궁금해", "세줘", "순서대로", "거야", "햇어", "열어줄래", "기억나게", "찍혔나요", "나온"):
        assert is_speech_act(word), word
    for word in ("다녀왔지", "떠난", "갔었는지", "여행했던", "돌아온", "놀러", "여행간", "갓어"):
        assert is_speech_act(word, movement=True) and not is_speech_act(word), word
    for word in ("주말", "가게", "한강", "간식", "없었지", "셋이", "둘이", "5월에", "서울숲", "2019", "스키", "전역",
                 "돼지", "주인", "주간", "나와", "하나만", "하나", "보고", "안", "못", "세상", "세시", "거북", "갓김치"):
        assert not is_speech_act(word, movement=True), word
    from types import SimpleNamespace as P
    class Q(P):
        def model_copy(self, update): return Q(**{**vars(self), **update})
    def switched(text):
        return topic_switch(Q(query=text, context_mode="FOLLOWUP", history=[1], selected_record_id=None))
    for text, rest in (("그건 그렇고 내가 쓴 일기 보여줘", "내가 쓴 일기 보여줘"), ("여행 얘긴 됐고 제가 쓴 일기 보여줘요", "제가 쓴 일기 보여줘요"),
                       ("다른 질문인데 내 일기 보여줘", "내 일기 보여줘"), ("여행 말고 내 건강검진 몇 번 했어?", "내 건강검진 몇 번 했어?")):
        out = switched(text)
        assert (out.query, out.context_mode, out.history) == (rest, "ROOM", []), text
    for text in ("사진 말고 음성 보여줘", "엄마랑 여행 몇 번 갔어?", "그때 사진 보여줘", "여행 말고 김영희랑 있었던 일 다 알려줘",
                 "검진 말고 엄마 기록 몇 개야?", "생일 말고 다른 날 찍은 사진"):
        assert switched(text).query == text, text
    print("ok")
