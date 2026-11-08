import { useTheme, useThemedStyles } from "../../shared/theme/ThemeProvider";
import React, { useEffect, useRef, useState } from "react";
import { ActivityIndicator, Pressable, StyleSheet, Text, TextInput, View } from "react-native";
import { metadataChanges, metadataDraft, revisionSummary, validCalendarDate } from "./recordMetadata.cjs";
import { managementError } from "./recordManagement.cjs";

export function RecordInfoEditor(props) {
  return <EditorForm key={props.record?.record_id || props.record?.id} {...props} />;
}

function EditorForm({ record, busy = false, onSave, onHistory }) {
  const { colors } = useTheme();
  const s = useThemedStyles(createStyles);
  const baseline = useRef(record);
  const mounted = useRef(true);
  const inFlight = useRef(false);
  const [draft, setDraft] = useState(() => metadataDraft(record));
  const [reason, setReason] = useState("");
  const [saving, setSaving] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [history, setHistory] = useState(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const disabled = busy || saving || historyLoading;
  const canEdit = record?.capabilities?.can_edit_metadata === true;
  const canEditTitle = canEdit && record?.capabilities?.can_edit_content === true;
  const setField = (field, value) => { setDraft(current => ({ ...current, [field]: value })); setNotice(""); };

  async function save() {
    if (disabled || inFlight.current || !onSave) return;
    setError(""); setNotice("");
    let changes;
    try {
      changes = metadataChanges({ ...baseline.current, capabilities: record.capabilities }, draft);
      if (!Object.keys(changes).length) throw new Error("수정할 내용을 입력해주세요.");
      if (!reason.trim() || reason.trim().length > 2000) throw new Error("변경 사유를 1~2000자로 입력해주세요.");
    } catch (failure) { setError(failure.message); return; }
    inFlight.current = true; setSaving(true);
    try {
      const saved = await onSave(changes, reason.trim());
      if (!mounted.current) return;
      baseline.current = saved?.record_id ? saved : {
        ...baseline.current, ...record,
        ...(changes.title !== undefined ? { title: changes.title } : {}),
        metadata: { ...(baseline.current.metadata || {}), ...(record.metadata || {}), ...changes },
      };
      setDraft(metadataDraft(baseline.current)); setReason(""); setHistory(null);
      setNotice(Object.keys(changes).some(field => field !== "title")
        ? "저장했습니다. 변경한 정보는 다시 검토한 뒤 검색에 반영할 수 있습니다."
        : "제목을 저장했습니다.");
    } catch (failure) {
      if (mounted.current) setError(managementError(failure));
    } finally {
      inFlight.current = false;
      if (mounted.current) setSaving(false);
    }
  }

  async function loadHistory() {
    if (disabled || inFlight.current || !onHistory) return;
    inFlight.current = true; setHistoryLoading(true); setError("");
    try {
      const items = await onHistory();
      if (!Array.isArray(items)) throw new Error("수정 이력을 확인할 수 없습니다.");
      if (mounted.current) setHistory(items.slice().reverse());
    } catch (failure) {
      if (mounted.current) setError(managementError(failure));
    } finally {
      inFlight.current = false;
      if (mounted.current) setHistoryLoading(false);
    }
  }

  const fields = [
    { name: "title", label: "제목", placeholder: "기록 제목", editable: canEditTitle },
    { name: "occurred_at", label: "발생일", placeholder: "YYYY-MM-DD", editable: canEdit },
    { name: "location", label: "장소", placeholder: "예: 강릉", editable: canEdit },
    { name: "category", label: "분류", placeholder: "예: 가족 여행", editable: canEdit },
  ];
  return <View style={s.section}>
    <Text accessibilityRole="header" style={s.heading}>기록 정보</Text>
    <Text style={s.hint}>원문을 설명하는 정보를 수정하고 변경 이력을 확인할 수 있습니다.</Text>
    {fields.map(field => <View key={field.name} style={s.field}>
      <Text style={s.label}>{field.label}{field.name === "title" && !canEditTitle ? " · 작성자만 수정" : ""}</Text>
      <TextInput accessibilityLabel={field.label} value={draft[field.name]} onChangeText={value => setField(field.name, value)}
        editable={field.editable && !disabled} placeholder={field.placeholder} placeholderTextColor={colors.placeholder}
        autoCapitalize="none" autoCorrect={false} style={[s.input, !field.editable && s.readonly]} />
    </View>)}
    {draft.occurred_at && draft.occurred_at === metadataDraft(baseline.current).occurred_at && !validCalendarDate(draft.occurred_at)
      ? <Text style={s.hint}>기존 일시를 그대로 유지합니다. 날짜를 바꾸려면 YYYY-MM-DD로 입력해주세요.</Text> : null}
    {canEdit ? <>
      <Text style={s.label}>변경 사유</Text>
      <TextInput accessibilityLabel="기록 정보 변경 사유" value={reason} onChangeText={setReason} editable={!disabled}
        maxLength={2000} multiline textAlignVertical="top" placeholder="어떤 내용을 수정했나요?" placeholderTextColor={colors.placeholder} style={[s.input, s.reason]} />
      <Pressable accessibilityRole="button" accessibilityState={{ disabled }} disabled={disabled} onPress={save} style={[s.primary, disabled && s.disabled]}>
        {saving ? <ActivityIndicator color={colors.primaryStrong} /> : <Text style={s.primaryText}>정보 저장</Text>}
      </Pressable>
    </> : <Text style={s.hint}>이 기록의 정보 수정 권한이 없습니다.</Text>}
    {error ? <Text accessibilityRole="alert" style={s.error}>{error}</Text> : null}
    {notice ? <Text accessibilityLiveRegion="polite" style={s.hint}>{notice}</Text> : null}
    {onHistory && record?.capabilities?.can_read === true ? <Pressable accessibilityRole="button" disabled={disabled} onPress={loadHistory} style={[s.secondary, disabled && s.disabled]}>
      {historyLoading ? <ActivityIndicator color={colors.primaryStrong} /> : <Text style={s.primaryText}>{history ? "수정 이력 새로고침" : "수정 이력 보기"}</Text>}
    </Pressable> : null}
    {history ? <View style={s.history}>
      {!history.length ? <Text style={s.hint}>아직 수정 이력이 없습니다.</Text> : history.map((item, index) => <View key={item.id || index} style={s.historyItem}>
        <Text style={s.label}>{revisionSummary(item)}</Text>
        <Text style={s.hint}>{formatHistoryTime(item.created_at)}</Text>
        {typeof item.detail?.reason === "string" && item.detail.reason.trim() ? <Text style={s.historyReason}>{item.detail.reason}</Text> : null}
      </View>)}
    </View> : null}
  </View>;
}

function formatHistoryTime(value) {
  if (!value) return "수정 시각 정보 없음";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "수정 시각 정보 없음" : date.toLocaleString("ko-KR");
}

const createStyles = colors => StyleSheet.create({
  section: { gap: 12, padding: 16, borderRadius: 16, backgroundColor: colors.background, borderWidth: 1, borderColor: colors.border },
  heading: { fontSize: 17, lineHeight: 24, fontWeight: "700", color: colors.text },
  hint: { fontSize: 12, lineHeight: 19, color: colors.muted },
  field: { gap: 6 }, label: { fontSize: 13, lineHeight: 20, fontWeight: "600", color: colors.text },
  input: { minHeight: 44, paddingHorizontal: 12, paddingVertical: 10, borderWidth: 1, borderColor: colors.border, borderRadius: 10, backgroundColor: colors.surface, color: colors.text, fontSize: 14 },
  readonly: { backgroundColor: colors.background, color: colors.muted }, reason: { minHeight: 74 },
  primary: { minHeight: 44, borderRadius: 10, backgroundColor: colors.primarySoft, alignItems: "center", justifyContent: "center", padding: 10 },
  primaryText: { color: colors.primaryStrong, fontSize: 13, fontWeight: "700" },
  secondary: { minHeight: 44, borderRadius: 10, borderWidth: 1, borderColor: colors.border, alignItems: "center", justifyContent: "center", padding: 10 },
  disabled: { opacity: 0.5 }, error: { fontSize: 12, lineHeight: 19, color: colors.danger },
  history: { gap: 10 }, historyItem: { gap: 4, padding: 12, backgroundColor: colors.surface, borderRadius: 10 },
  historyReason: { fontSize: 13, lineHeight: 20, color: colors.muted },
});
