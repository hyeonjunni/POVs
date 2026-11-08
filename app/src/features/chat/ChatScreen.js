import { useTheme, useThemedStyles } from "../../shared/theme/ThemeProvider";
import React, { useRef } from "react";
import { ActivityIndicator, KeyboardAvoidingView, Platform, Pressable, ScrollView, StyleSheet, Text, TextInput, View } from "react-native";
import { UiIcon } from "../../shared/ui/UiIcon";
import { ChatResultCards } from "./ChatResultCards";
import { audioCitationLocation } from "./audioCitation.cjs";
const { answerText } = require("./answerText.cjs");
const { serviceMedia } = require("./serviceMedia.cjs");
const gateCopy = {
  NEEDS_DATA: { title: "확인할 근거가 더 필요합니다" },
  REFUSE: { title: "답변할 수 없는 질문" },
  CLARIFY: { title: "질문을 조금 더 구체적으로 알려주세요" },
  CONFLICT: { title: "기록에 서로 다른 내용이 있습니다" },
  NO_RECORD: { title: "관련 기록을 찾지 못했습니다" },
};

function errorMessage(error) {
  return typeof error === "string" ? error : error?.message || "질문을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요.";
}

function formatSeconds(value) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return null;
  const seconds = Math.floor(value);
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

function sourceLocation(citation) {
  const audioLocation = audioCitationLocation(citation);
  if (audioLocation) return audioLocation;
  const locator = citation.source_locator;
  if (!locator || typeof locator !== "object") return "원본에서 근거 위치를 확인하세요";
  if (locator.kind === "stt_segments" && Array.isArray(locator.segments)) {
    const ranges = locator.segments.slice(0, 3).map(segment => {
      const start = formatSeconds(segment?.start_seconds);
      const end = formatSeconds(segment?.end_seconds);
      return start ? `${start}${end ? `–${end}` : ""}` : null;
    }).filter(Boolean);
    if (ranges.length) return `음성 구간 ${ranges.join(", ")}${locator.segments.length > 3 ? " 외" : ""}`;
    return "음성의 추출 구간";
  }
  if (locator.kind === "ocr_regions" && Array.isArray(locator.indexes)) {
    const indexes = locator.indexes.filter(value => Number.isSafeInteger(value) && value >= 0);
    if (indexes.length) return `문자 인식 영역 ${indexes.slice(0, 4).map(value => value + 1).join(", ")}${indexes.length > 4 ? " 외" : ""}`;
    return "문자 인식 구간";
  }
  if (Number.isSafeInteger(locator.page) && locator.page >= 0) return `원본 페이지 ${locator.page}`;
  if (locator.kind === "full_text") return "원문 텍스트";
  return "원본에서 근거 위치를 확인하세요";
}

function CitationCard({ citation, index, onOpenCitation, openingCitation }) {
  const { colors } = useTheme();
  const p = useThemedStyles(createStyles);
  const opening = openingCitation != null && (openingCitation === citation.fact_id || openingCitation === citation.chunk_id);
  const disabled = typeof onOpenCitation !== "function" || openingCitation != null;
  const extractor = citation.extractor === "AUTO_OCR" ? "자동 문자 인식" : citation.extractor === "AUTO_STT" ? "자동 음성 인식" : "기록의 추출 근거";
  const review = citation.review_status === "unreviewed" ? "미검토" : citation.review_status === "confirmed" ? "검토 완료" : citation.review_status === "rejected" ? "검토에서 제외" : "검토 상태 미확인";
  const hasRange = Number.isSafeInteger(citation.chunk_char_start) && Number.isSafeInteger(citation.chunk_char_end) && citation.chunk_char_start >= 0 && citation.chunk_char_end > citation.chunk_char_start;
  return <View style={p.citation}>
    <Text style={p.citationTitle}>근거 기록 {index + 1}</Text>
    <Text style={p.meta}>{sourceLocation(citation)}</Text>
    {hasRange ? <Text style={p.meta}>원문 문자 위치 {citation.chunk_char_start}–{citation.chunk_char_end}</Text> : null}
    <Text style={p.meta}>{extractor} · 추출 당시 {review}</Text>
    <Pressable accessibilityRole="button" accessibilityLabel={`근거 기록 ${index + 1} 원본 보기`} accessibilityState={{ disabled, busy: opening }}
      disabled={disabled} onPress={() => onOpenCitation(citation)} style={({ pressed }) => [p.sourceButton, disabled && p.disabled, pressed && p.pressed]}>
      {opening ? <ActivityIndicator size="small" color={colors.primaryStrong} /> : <UiIcon name="text" size={18} />}
      <Text style={p.sourceButtonText}>{opening ? "원본 확인 중…" : "원본 보기"}</Text>
    </Pressable>
  </View>;
}

function ContextSummary({ context }) {
  const p = useThemedStyles(createStyles);
  if (!context) return null;
  const labels = { ROOM: "전체 접근 가능한 기록에서 검색", FOLLOWUP: "이전 답변의 기록에 이어서", RECORD: "지정한 기록만 사용" };
  return <View style={p.contextSummary}>
    <Text style={p.contextTitle}>질문 범위: {labels[context.scope]}</Text>
    {context.viewer_name ? <Text style={p.meta}>질문자: {context.viewer_name}{context.personal_memory_applied ? " · 개인 메모리 적용" : ""}</Text> : null}
    {context.address_bindings?.length ? <Text style={p.meta}>호칭 해석: {context.address_bindings.map(binding => `${binding.expression} → ${binding.name}`).join(" · ")}</Text> : null}
    {context.previous_query ? <Text style={p.meta} numberOfLines={2}>이어받은 질문: {context.previous_query}</Text> : null}
    <Text style={p.meta}>사용 기록: {context.records.length}개</Text>
    {context.records.map(record => <Text key={record.record_id} style={p.meta}>• {record.record_kind === "EVENT" ? "사건" : ({ text: "텍스트", image: "사진", voice: "음성", video: "영상" }[record.record_type] || "기록")}: {record.title}</Text>)}
  </View>;
}

function ChatResponse({ response, query, serviceMode, onOpenCitation, openingCitation }) {
  const { colors, presentationStyles: s } = useTheme();
  const p = useThemedStyles(createStyles);
  if (serviceMode) {
    const media = serviceMedia(query, response);
    const text = typeof response.answer === "string" && response.answer.trim()
      ? answerText(response.answer).replace("아래 근거에서 원본을 열 수 있습니다.", "").trim()
      : gateCopy[response.gate_route]?.title || "답변을 확인하지 못했습니다. 다시 질문해 주세요.";
    return <View style={p.answerGroup}>
      <View style={[s.chatBubbleLeft, p.answerBubble]}><Text selectable style={s.chatText}>{text}</Text></View>
      {media.map(({ citation, record }) => {
        const opening = openingCitation != null && (openingCitation === citation.fact_id || openingCitation === citation.chunk_id);
        const disabled = typeof onOpenCitation !== "function" || openingCitation != null;
        const label = { image: "사진 보기", voice: "음성 듣기", video: "영상 보기" }[record.record_type];
        return <Pressable key={record.record_id} accessibilityRole="button" accessibilityLabel={`${record.title} ${label}`}
          accessibilityState={{ disabled, busy: opening }} disabled={disabled} onPress={() => onOpenCitation(citation)}
          style={({ pressed }) => [p.sourceButton, disabled && p.disabled, pressed && p.pressed]}>
          {opening ? <ActivityIndicator size="small" color={colors.primaryStrong} /> : <UiIcon name={record.record_type} size={18} />}
          <Text style={[p.sourceButtonText, p.mediaLabel]}>{opening ? "불러오는 중…" : `${record.title} · ${label}`}</Text>
        </Pressable>;
      })}
    </View>;
  }
  if (response.gate_route !== "ANSWER" && !response.citations?.length) {
    const copy = Object.prototype.hasOwnProperty.call(gateCopy, response.gate_route) ? gateCopy[response.gate_route] : { title: "답변 상태를 확인할 수 없습니다" };
    return <View style={p.noticeCard}>
      <Text style={p.noticeTitle}>{copy.title}</Text>
      <ContextSummary context={response.query_context} />
      {typeof response.answer === "string" && response.answer.trim() ? <Text selectable style={p.body}>{answerText(response.answer)}</Text> : null}
      {response.gate_route === "NO_RECORD" ? <Text style={p.body}>검색 가능한 근거를 찾지 못했습니다. 업로드한 기록의 문자 인식·텍스트 처리 상태를 확인한 뒤 다시 질문해 주세요.</Text> : null}
      <ChatResultCards response={response} />
    </View>;
  }
  return <View style={p.answerGroup}>
    <View style={[s.chatBubbleLeft, p.answerBubble]}>
      <Text style={s.chatMeta}>기록을 바탕으로 한 답변</Text>
      <ContextSummary context={response.query_context} />
      {response.gate_route === "CONFLICT" ? <Text style={p.noticeTitle}>{gateCopy.CONFLICT.title}</Text> : null}
      {response.gate_route === "NEEDS_DATA" ? <Text style={p.noticeTitle}>{gateCopy.NEEDS_DATA.title}</Text> : null}
      {typeof response.answer === "string" && response.answer.trim() ? <Text selectable style={s.chatText}>{answerText(response.answer)}</Text> : null}
      {response.provider_mode === "fallback" ? <Text style={p.fallback}>모델 응답 대신 서버가 근거 문장을 정리했습니다.</Text> : null}
    </View>
    <ChatResultCards response={response} />
    {response.citations?.length ? <View style={p.citations}>
      <Text style={p.evidenceTitle}>답변의 근거 · {response.citations.length}개</Text>
      {response.citations.map((citation, index) => <CitationCard key={`${citation.fact_id}:${citation.chunk_id}:${index}`} citation={citation} index={index} onOpenCitation={onOpenCitation} openingCitation={openingCitation} />)}
      <Text style={p.meta}>자동 추출된 내용은 원본과 함께 확인해 주세요.</Text>
    </View> : null}
  </View>;
}

export function ChatScreen({
  serviceMode = true,
  enabled = false,
  disabledReason = "화면 확인용 테스트 모드입니다. 대화를 시작하려면 실제 계정으로 로그인해 주세요.",
  statusLabel = "로그인 필요",
  draft = "",
  onChangeDraft,
  onSend,
  sending = false,
  messages = [],
  error = null,
  onDismissError,
  onRetry,
  onOpenCitation,
  openingCitation = null,
  contextMode = "ROOM",
  onChangeContextMode,
  onNewConversation,
} = {}) {
  const { colors, presentationStyles: s, uiStyles: u } = useTheme();
  const p = useThemedStyles(createStyles);
  const scroll = useRef(null);
  const canSend = enabled && typeof onSend === "function" && !sending && draft.trim().length > 0 && draft.length <= 4096;
  const canRetry = enabled && !sending && typeof onRetry === "function";
  const lastAnswer = [...messages].reverse().find(message => message.response?.gate_route === "ANSWER");
  const contextTitles = lastAnswer?.response?.query_context?.records.map(record => record.title) || [];
  // AppNavigator owns Android avoidance at the window root. Only iOS adjusts
  // here, so the composer never adds a second keyboard-height inset.
  return <KeyboardAvoidingView style={s.chatEmptyScreen} behavior={Platform.OS === "ios" ? "padding" : undefined}>
    <View style={u.chatHeader}>
      <UiIcon name="chat" size={20} />
      <Text style={u.chatContext}>가족 기록 질문</Text>
      {!serviceMode ? <Text style={u.readiness}>{enabled ? "기록 기반 대화" : statusLabel}</Text> : null}
      {onNewConversation ? <Pressable accessibilityRole="button" accessibilityLabel="새 대화 시작" onPress={onNewConversation} style={p.scopeButton}><Text style={p.scopeText}>{serviceMode ? "새 주제" : "새 대화"}</Text></Pressable> : null}
    </View>
    <ScrollView ref={scroll} style={p.messageList} contentContainerStyle={messages.length ? p.messages : s.chatEmptyContent} showsVerticalScrollIndicator={false}
      keyboardShouldPersistTaps="handled" keyboardDismissMode="on-drag" onContentSizeChange={() => { if (messages.length || sending) scroll.current?.scrollToEnd({ animated: true }); }}>
      {messages.length === 0 ? <>
        <View style={s.chatEmptyIcon}><UiIcon name="chat" size={38} /></View>
        <Text style={s.chatEmptyTitle}>가족의 기록에 질문해 보세요</Text>
        <Text style={s.chatEmptyDescription}>{enabled ? serviceMode ? "궁금한 기억을 물어보세요." : "선택한 가족방의 기록을 바탕으로 답변합니다. 답변 아래의 근거에서 원본을 확인할 수 있습니다." : disabledReason}</Text>
      </> : messages.map(message => <View key={message.id} style={p.exchange}>
        <View style={s.chatBubbleRight}><Text selectable style={s.chatText}>{message.query}</Text></View>
        {message.response ? <ChatResponse response={message.response} query={message.query} serviceMode={serviceMode} onOpenCitation={onOpenCitation} openingCitation={openingCitation} /> : null}
        {message.error ? <View style={p.errorCard} accessibilityLiveRegion="polite">
          <Text style={p.errorText}>{errorMessage(message.error)}</Text>
          {canRetry && message.retryable === true ? <Pressable accessibilityRole="button" accessibilityLabel="이 질문 다시 시도" onPress={() => onRetry(message)} style={p.retryButton}><Text style={p.retryText}>다시 시도</Text></Pressable> : null}
        </View> : null}
      </View>)}
      {sending ? <View style={p.loading} accessibilityLiveRegion="polite">
        <ActivityIndicator size="small" color={colors.primaryStrong} />
        <Text style={p.loadingText}>{serviceMode ? "기억을 찾고 있어요…" : "가족 기록에서 근거를 확인하고 있습니다…"}</Text>
      </View> : null}
    </ScrollView>
    {error ? <View style={p.errorBanner} accessibilityLiveRegion="polite">
      <Text style={[p.errorText, p.errorCopy]}>{errorMessage(error)}</Text>
      {canRetry && messages.some(message => message.error && message.retryable === true) ? <Pressable accessibilityRole="button" accessibilityLabel="질문 다시 시도" onPress={() => onRetry()} style={p.retryButton}><Text style={p.retryText}>재시도</Text></Pressable> : null}
      {onDismissError ? <Pressable accessibilityRole="button" accessibilityLabel="대화 오류 안내 닫기" onPress={onDismissError} style={u.iconButton}><UiIcon name="close" size={18} color={colors.danger} /></Pressable> : null}
    </View> : null}
    <View style={[u.composerArea, { paddingBottom: 12 }]}>
      {!serviceMode && enabled && onChangeContextMode ? <>
        <View style={p.contextControls}>
          {[{ mode: "ROOM", label: "전체 기록에서 새 질문" }, { mode: "FOLLOWUP", label: "이전 답변에 이어서" }].map(option => {
            const disabled = sending || (option.mode === "FOLLOWUP" && !lastAnswer);
            return <Pressable key={option.mode} accessibilityRole="radio" accessibilityLabel={option.label}
              accessibilityState={{ checked: contextMode === option.mode, disabled }} disabled={disabled}
              onPress={() => onChangeContextMode(option.mode)} style={[p.scopeButton, contextMode === option.mode && p.scopeSelected, disabled && p.disabled]}>
              <Text style={p.scopeText}>{option.label}</Text>
            </Pressable>;
          })}
        </View>
        <Text style={p.meta} numberOfLines={2}>{contextMode === "FOLLOWUP" && lastAnswer
          ? `다음 질문은 이전 답변의 기록에 한정합니다: ${contextTitles.join(" · ") || lastAnswer.query}`
          : "다음 질문은 내 비공개 기록과 접근 가능한 가족 기록 전체에서 새로 검색합니다."}</Text>
      </> : null}
      <View style={s.chatComposerPlaceholder}>
        <TextInput editable={enabled} value={draft} onChangeText={onChangeDraft} multiline maxLength={4096}
          placeholder={enabled ? "가족 기록에 대해 질문해 보세요" : "실제 로그인 후 질문할 수 있어요"} placeholderTextColor={colors.muted}
          accessibilityLabel="가족 기록 질문 입력" accessibilityHint="최대 4096자까지 입력할 수 있습니다" accessibilityState={{ disabled: !enabled }}
          style={[s.chatComposerText, u.input, p.input]} />
        <Pressable disabled={!canSend} accessibilityRole="button" accessibilityLabel="질문 보내기" accessibilityState={{ disabled: !canSend, busy: sending }}
          onPress={() => { if (canSend) onSend(); }} style={({ pressed }) => [p.sendButton, !canSend && p.disabled, pressed && p.pressed]}>
          {sending ? <ActivityIndicator size="small" color={colors.onPrimary} /> : <UiIcon name="send" color={colors.onPrimary} size={20} />}
        </Pressable>
      </View>
      {!serviceMode || !enabled || draft.length >= 3800 ? <Text style={s.chatComposerHint}>{!enabled ? disabledReason : draft.length >= 3800 ? `${draft.length} / 4096자` : "선택한 가족방의 기록만 사용합니다. 근거 원본을 함께 확인하세요."}</Text> : null}
    </View>
  </KeyboardAvoidingView>;
}

const createStyles = colors => StyleSheet.create({
  messageList: { flex: 1 },
  messages: { padding: 16, paddingBottom: 22, gap: 22 },
  exchange: { gap: 12 },
  answerGroup: { gap: 12, alignSelf: "stretch" },
  answerBubble: { maxWidth: "100%" },
  contextSummary: { gap: 4, marginVertical: 8 },
  contextTitle: { color: colors.primaryStrong, fontSize: 12, fontWeight: "700", lineHeight: 19 },
  contextControls: { flexDirection: "row", flexWrap: "wrap", gap: 8 },
  scopeButton: { minHeight: 44, paddingHorizontal: 12, justifyContent: "center", borderRadius: 10, backgroundColor: colors.primarySoft },
  scopeSelected: { borderWidth: 1, borderColor: colors.primaryStrong },
  scopeText: { color: colors.primaryStrong, fontSize: 12, fontWeight: "700" },
  body: { color: colors.text, fontSize: 14, lineHeight: 22 },
  noticeCard: { backgroundColor: colors.primarySoft, borderWidth: 1, borderColor: colors.border, borderRadius: 16, padding: 16, gap: 8 },
  noticeTitle: { color: colors.primaryStrong, fontSize: 15, fontWeight: "700", lineHeight: 22 },
  citations: { gap: 10 },
  evidenceTitle: { color: colors.text, fontWeight: "700", fontSize: 13, lineHeight: 20 },
  citation: { borderWidth: 1, borderColor: colors.border, borderRadius: 14, padding: 14, gap: 6, backgroundColor: colors.surface },
  citationTitle: { color: colors.text, fontWeight: "700", fontSize: 14, lineHeight: 21 },
  meta: { color: colors.muted, fontSize: 12, lineHeight: 19 },
  sourceButton: { alignSelf: "flex-start", minHeight: 44, paddingHorizontal: 14, marginTop: 4, borderRadius: 12, backgroundColor: colors.primarySoft, flexDirection: "row", gap: 8, alignItems: "center", justifyContent: "center" },
  sourceButtonText: { color: colors.primaryStrong, fontSize: 13, fontWeight: "700" },
  mediaLabel: { flexShrink: 1 },
  fallback: { color: colors.muted, fontSize: 12, lineHeight: 19, marginTop: 10 },
  loading: { alignSelf: "flex-start", flexDirection: "row", alignItems: "center", gap: 10, padding: 14, backgroundColor: colors.primarySoft, borderRadius: 16 },
  loadingText: { flexShrink: 1, color: colors.primaryStrong, fontSize: 13, lineHeight: 20 },
  errorCard: { padding: 14, borderRadius: 14, backgroundColor: colors.dangerSoft, gap: 8 },
  errorBanner: { paddingHorizontal: 16, paddingVertical: 6, backgroundColor: colors.dangerSoft, flexDirection: "row", alignItems: "center", gap: 8 },
  errorText: { color: colors.danger, fontSize: 13, lineHeight: 20 },
  errorCopy: { flex: 1 },
  retryButton: { alignSelf: "flex-start", minHeight: 44, minWidth: 64, paddingHorizontal: 12, alignItems: "center", justifyContent: "center" },
  retryText: { color: colors.danger, fontWeight: "700", fontSize: 13 },
  input: { color: colors.text, maxHeight: 120, textAlignVertical: "top" },
  sendButton: { minHeight: 44, minWidth: 44, borderRadius: 12, backgroundColor: colors.primaryStrong, alignItems: "center", justifyContent: "center", alignSelf: "flex-end", marginBottom: 4 },
  disabled: { opacity: 0.5 },
  pressed: { opacity: 0.8 },
});
