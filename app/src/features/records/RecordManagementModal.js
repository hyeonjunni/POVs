import { useTheme, useThemedStyles } from "../../shared/theme/ThemeProvider";
import React, { useEffect, useMemo, useRef, useState } from "react";
import { Modal, View, Text, ScrollView, Pressable, ActivityIndicator, StyleSheet } from "react-native";
import { randomUUID } from "expo-crypto";
import { recordsApi, uploadsApi, processingApi, getRuntimeAccessToken, getRuntimeApiBaseUrl } from "../../shared/api/runtime";
import { RecordStatus, VisibilitySelector } from "./RecordStatus";
import { RecordInfoEditor } from "./RecordInfoEditor";
const { PROCESSORS, canPrepare, mergeRecordDetails, createPreparationFlow, managementError } = require("./recordManagement.cjs");

export function RecordManagementModal({ record: initialRecord, roomId, onClose, onChanged }) {
  const { colors } = useTheme();
  const s = useThemedStyles(createStyles);
  const [record, setRecord] = useState(initialRecord);
  const [state, setState] = useState(null);
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [blocked, setBlocked] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [visibility, setVisibility] = useState(initialRecord.visibility);
  const [confirmed, setConfirmed] = useState(false);
  const lifetime = useRef({ alive: true, token: getRuntimeAccessToken(), baseUrl: getRuntimeApiBaseUrl() });
  const pending = useRef(false);
  const assertCurrent = () => {
    const context = lifetime.current;
    if (!context.alive || context.token !== getRuntimeAccessToken() || context.baseUrl !== getRuntimeApiBaseUrl()) throw Object.assign(new Error("이전 세션의 요청입니다."), { code: "RECORD_ABANDONED" });
  };
  const flow = useMemo(() => createPreparationFlow({ records: recordsApi, uploads: uploadsApi, processing: processingApi,
    roomId, createKey: randomUUID, assertCurrent, onState: next => { setState(next); if (next.record) setRecord(next.record); } }), [roomId]);
  useEffect(() => {
    lifetime.current.alive = true;
    perform(reload);
    return () => { lifetime.current.alive = false; };
  }, []);
  async function reload() {
    assertCurrent();
    const detail = await recordsApi.get(initialRecord.id);
    assertCurrent();
    if (detail.room_id !== roomId || detail.capabilities?.can_read !== true) {
      setBlocked(true); setState(null);
      throw new Error("이 기록을 읽을 수 없습니다.");
    }
    const next = mergeRecordDetails(initialRecord, detail);
    setRecord(next); setVisibility(next.visibility); setLoaded(true); setBlocked(false); setConfirmed(false);
    return next;
  }
  async function perform(work, { rethrow = false } = {}) {
    if (pending.current) { if (rethrow) throw new Error("이전 요청이 처리 중입니다."); return; }
    pending.current = true; setBusy(true); setError(""); setNotice("");
    try {
      assertCurrent();
      const result = await work();
      assertCurrent();
      return result;
    } catch (failure) {
      if (lifetime.current.alive && failure?.code !== "RECORD_ABANDONED") {
        setError(managementError(failure));
        if ([403, 404].includes(failure?.status)) { setBlocked(true); setState(null); }
      }
      if (rethrow) throw failure;
    } finally {
      pending.current = false;
      if (lifetime.current.alive) setBusy(false);
    }
  }
  async function changed() {
    assertCurrent();
    try { await onChanged?.(); assertCurrent(); }
    catch (failure) { assertCurrent(); setNotice("변경은 저장됐지만 목록 갱신에 실패했습니다. 기록 목록을 새로고침해주세요."); }
  }
  async function prepare(retry = false) {
    if (!confirmed) throw new Error("원문과 공개 범위를 확인해주세요.");
    await flow.start(record, { retry });
    await changed();
  }
  async function setAudience() {
    const next = await recordsApi.changeVisibility(record.id, { visibility, recordVersion: record.record_version,
      reason: "기록 공개 범위를 변경함", idempotencyKey: flow.actionKey(`visibility:${visibility}`, record) });
    assertCurrent(); setRecord(mergeRecordDetails(record, next)); setState(null); setConfirmed(false);
    setNotice("공개 범위를 저장했습니다. 검색에 사용할 내용은 다시 확인해주세요."); await changed();
  }
  async function saveMetadata(changes, reason) {
    return perform(async () => {
      const next = await recordsApi.patchMetadata(record.id, { changes, reason, recordVersion: record.record_version });
      assertCurrent(); setRecord(mergeRecordDetails(record, next)); setState(null); setConfirmed(false);
      setNotice(next.review_status === "APPROVED" ? "기록 정보를 저장했습니다." : "정보를 저장했습니다. 변경한 내용을 다시 검토해주세요.");
      await changed(); return next;
    }, { rethrow: true });
  }
  async function history() {
    assertCurrent(); const items = await recordsApi.listMetadataRevisions(record.id); assertCurrent(); return items;
  }
  const review = state?.review;
  const canReview = loaded && !blocked && canPrepare(record) && Boolean(PROCESSORS[record.type]);
  const failed = state?.job?.status === "FAILED";
  const button = (label, action, disabled = false) => <Pressable accessibilityRole="button" disabled={busy || disabled}
    onPress={() => perform(action)} style={[s.button, (busy || disabled) && s.disabled]}><Text style={s.buttonText}>{label}</Text></Pressable>;
  return <Modal visible transparent animationType="slide" onRequestClose={onClose}>
    <View style={s.backdrop}><View style={s.sheet} accessibilityViewIsModal>
      <View style={s.heading}><Text style={s.title}>기록 관리</Text><Pressable accessibilityRole="button" onPress={onClose} style={s.close}><Text style={s.link}>닫기</Text></Pressable></View>
      <ScrollView contentContainerStyle={s.content} keyboardShouldPersistTaps="handled">
        {error ? <Text accessibilityRole="alert" style={s.error}>{error}</Text> : null}
        {notice ? <Text accessibilityLiveRegion="polite" style={s.body}>{notice}</Text> : null}
        {busy ? <ActivityIndicator color={colors.primary} /> : null}
        {button("최신 상태 불러오기", async () => { await reload(); setState(null); })}
        {!blocked && loaded ? <>
          <Text style={s.subtitle}>{record.title}</Text><RecordStatus record={record} />
          <Text selectable style={s.body}>{record.detail || "추가 설명 없음"}</Text>
          {record.capabilities?.can_change_visibility ? <View style={s.section}>
            <Text style={s.subtitle}>공개 범위</Text>
            <Text style={s.caption}>나만 보기는 작성자만, 가족 전체는 이 가족방의 모든 구성원이 볼 수 있습니다.</Text>
            <VisibilitySelector value={visibility} onChange={setVisibility} disabled={busy} />
            {record.visibility === "CUSTOM" ? <Text style={s.caption}>현재 선택한 가족에게 공개 중입니다. 가족 전체로 변경하면 공개 대상이 늘어납니다.</Text> : null}
            {button("공개 범위 저장", setAudience, !["PRIVATE", "FAMILY"].includes(visibility) || visibility === record.visibility)}
          </View> : null}
          <Text style={s.caption}>기록 승인과 검색 준비는 별개입니다. 분석 결과를 확인한 뒤 검색·대화에 사용할 수 있습니다.</Text>
          {canReview ? <View style={s.section}>
            <Text style={s.subtitle}>검토·검색 준비</Text>
            <Pressable accessibilityRole="checkbox" accessibilityState={{ checked: confirmed, disabled: busy }} disabled={busy}
              onPress={() => setConfirmed(value => !value)} style={s.check}><Text style={s.body}>{confirmed ? "☑" : "☐"} 원문과 공개 범위를 확인했습니다.</Text></Pressable>
            {button(state?.job ? "처리 상태 확인" : "내용 승인·분석 시작", () => prepare(false), !confirmed)}
            {state?.message ? <Text accessibilityLiveRegion="polite" style={s.body}>{state.message}</Text> : null}
            {state?.job && !review ? <Text style={s.caption}>{({ PENDING: "분석 대기", RUNNING: "분석 중", FAILED: "분석 실패" })[state.job.status] || "결과 확인 중"}</Text> : null}
            {failed ? <><Text style={s.error}>{managementError({ code: state.job.failure_code, message: state.job.failure_message })}</Text>{button("같은 분석 다시 시도", () => prepare(true), !confirmed)}</> : null}
            {review ? <>
              <Text style={s.subtitle}>분석된 내용</Text>
              <Text selectable style={s.result}>{state.job.result?.text || "추출된 내용이 없습니다."}</Text>
              <Text style={s.caption}>승인·거절은 최종 결정입니다. 내용을 확인하고 선택해주세요.</Text>
              {review.review_status === "unreviewed" ? <>
                {button("분석 결과 승인·검색 준비", () => flow.decide("approve"), !state.job.result?.text?.trim())}
                {button("분석 결과 거절", () => flow.decide("reject"))}
              </> : review.review_status === "confirmed" ? <>
                <Text style={s.caption}>분석 결과 승인됨</Text>
                {!state.indexed ? button("검색 준비 다시 확인", () => flow.decide("index")) : null}
              </> : <Text style={s.caption}>거절된 분석 결과입니다. 검색 근거로 사용하지 않습니다.</Text>}
            </> : null}
          </View> : null}
          <RecordInfoEditor record={record} busy={busy} onSave={saveMetadata} onHistory={history} />
        </> : null}
      </ScrollView>
    </View></View>
  </Modal>;
}
const createStyles = colors => StyleSheet.create({
  backdrop: { flex: 1, justifyContent: "center", padding: 16, backgroundColor: colors.scrim },
  sheet: { maxHeight: "92%", width: "100%", maxWidth: 680, alignSelf: "center", backgroundColor: colors.surface, padding: 18, borderRadius: 22 },
  content: { gap: 12, paddingBottom: 24 }, heading: { flexDirection: "row", justifyContent: "space-between", alignItems: "center" },
  title: { fontSize: 21, fontWeight: "700", color: colors.text }, subtitle: { fontSize: 16, fontWeight: "700", color: colors.text },
  close: { minHeight: 48, paddingHorizontal: 16, justifyContent: "center" }, link: { color: colors.primaryStrong, fontWeight: "700" },
  body: { fontSize: 15, lineHeight: 24, color: colors.text }, caption: { fontSize: 13, lineHeight: 21, color: colors.muted },
  error: { color: colors.danger, lineHeight: 22 }, section: { borderTopWidth: 1, borderColor: colors.border, paddingTop: 18, gap: 10 },
  button: { minHeight: 46, padding: 12, borderRadius: 12, backgroundColor: colors.primarySoft, alignItems: "center", justifyContent: "center" },
  buttonText: { color: colors.primaryStrong, fontWeight: "700" }, disabled: { opacity: .45 },
  check: { minHeight: 48, justifyContent: "center" }, result: { padding: 14, borderRadius: 12, backgroundColor: colors.background, lineHeight: 24, color: colors.text },
});
