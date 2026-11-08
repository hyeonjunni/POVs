import { useTheme, useThemedStyles } from "../../shared/theme/ThemeProvider";
import React, { useEffect, useRef, useState } from "react";
import { ActivityIndicator, KeyboardAvoidingView, Modal, Pressable, ScrollView, StyleSheet, Text, TextInput, View } from "react-native";
import { runtimeApiClient, getRuntimeAccessToken, getRuntimeApiBaseUrl } from "../../shared/api/runtime";
import { KEYBOARD_AVOIDING_BEHAVIOR } from "../../shared/ui/layout";

export function PersonalContextEditor({ userId, roomId, familyName, onClose }) {
  const { colors } = useTheme();
  const s = useThemedStyles(createStyles);
  const [tab, setTab] = useState("aliases");
  const [context, setContext] = useState(null);
  const [aliases, setAliases] = useState({});
  const [memory, setMemory] = useState("");
  const [busy, setBusy] = useState(false);
  const [conflict, setConflict] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const lifetime = useRef(null);
  const pending = useRef(false);
  const path = `/families/${encodeURIComponent(roomId)}/personal-context`;
  const isCurrent = scope => scope === lifetime.current && !scope.controller.signal.aborted
    && scope.token === getRuntimeAccessToken() && scope.baseUrl === getRuntimeApiBaseUrl();

  function receive(data) {
    if (data.viewer_user_id !== userId) throw new Error("현재 계정의 설정이 아닙니다. 창을 닫고 다시 열어주세요.");
    setContext(data);
    setMemory(data.memory_note || "");
    setAliases(Object.fromEntries(data.people.map(person => [person.user_id, person.aliases.join(", ")])));
    setConflict(false);
  }

  async function load(scope = lifetime.current) {
    if (!isCurrent(scope) || pending.current) return;
    pending.current = true;
    setBusy(true); setError(""); setNotice("");
    try {
      const data = await runtimeApiClient.get(path, { signal: scope.controller.signal });
      if (isCurrent(scope)) receive(data);
    } catch (failure) {
      if (isCurrent(scope)) setError(failure.message || "설정을 불러오지 못했습니다.");
    } finally {
      pending.current = false;
      if (isCurrent(scope)) setBusy(false);
    }
  }

  useEffect(() => {
    const scope = { controller: new AbortController(), token: getRuntimeAccessToken(), baseUrl: getRuntimeApiBaseUrl() };
    lifetime.current = scope;
    load(scope);
    return () => { scope.controller.abort(); };
  }, []);

  async function save() {
    const scope = lifetime.current;
    if (!isCurrent(scope) || pending.current || !context || conflict) return;
    const values = context.people.map(person => ({
      target_user_id: person.user_id,
      values: [...new Set((aliases[person.user_id] || "").split(/[,，\n]/).map(value => value.trim()).filter(Boolean))]
    }));
    pending.current = true;
    setBusy(true); setError(""); setNotice("");
    try {
      const data = await runtimeApiClient.put(path, {
        signal: scope.controller.signal,
        json: { version: context.version, memory_note: memory, aliases: values }
      });
      if (isCurrent(scope)) { receive(data); setNotice("저장했습니다. 다음 질문부터 적용됩니다."); }
    } catch (failure) {
      if (isCurrent(scope)) {
        if (failure.status === 409) {
          setConflict(true);
          setError("다른 화면에서 설정이 변경됐습니다. 최신 설정을 다시 불러온 뒤 수정해주세요. 현재 입력은 다시 불러올 때 사라집니다.");
        } else setError(failure.message || "설정을 저장하지 못했습니다. 입력을 확인하고 다시 시도해주세요.");
      }
    } finally {
      pending.current = false;
      if (isCurrent(scope)) setBusy(false);
    }
  }

  return <Modal visible transparent animationType="slide" onRequestClose={onClose}>
    <KeyboardAvoidingView behavior={KEYBOARD_AVOIDING_BEHAVIOR} style={s.backdrop}>
      <View style={s.panel} accessibilityViewIsModal>
        <View style={s.heading}>
          <View style={s.grow}><Text style={s.title}>개인 메모리·호칭</Text><Text style={s.hint}>{familyName} · 나만의 설정</Text></View>
          <Pressable accessibilityRole="button" onPress={onClose} style={s.button}><Text style={s.buttonText}>닫기</Text></Pressable>
        </View>
        <View style={s.tabs}>
          {[['aliases', '호칭'], ['memory', '개인 메모리']].map(([key, label]) => <Pressable key={key} accessibilityRole="tab" accessibilityState={{ selected: tab === key }} onPress={() => setTab(key)} style={[s.tab, tab === key && s.selected]}><Text style={s.buttonText}>{label}</Text></Pressable>)}
        </View>
        <ScrollView contentContainerStyle={s.content} keyboardShouldPersistTaps="handled">
          <Text style={s.hint}>이 계정의 대화에 계속 사용하는 설정입니다. 새 대화를 시작해도 유지되며 가족에게 공유되지 않습니다.</Text>
          {busy && !context ? <ActivityIndicator accessibilityLabel="설정 불러오는 중" color={colors.primary} /> : null}
          {context && tab === "aliases" ? <>
            <Text style={s.body}>내가 부르는 호칭을 쉼표로 나눠 적어주세요. 같은 사람을 부르는 여러 표현을 함께 등록할 수 있습니다.</Text>
            {context.people.map(person => <View key={person.user_id} style={s.person}>
              <Text style={s.name}>{person.name}{person.relation ? ` · ${person.relation}` : ""}</Text>
              <TextInput accessibilityLabel={`${person.name} 호칭`} value={aliases[person.user_id] || ""} onChangeText={value => { setAliases(previous => ({ ...previous, [person.user_id]: value })); setNotice(""); }} editable={!busy} style={s.input} placeholder="예: 엄마, 어머니" placeholderTextColor={colors.muted} />
              <Pressable accessibilityRole="button" accessibilityLabel={`${person.name} 기본 호칭으로 되돌리기`} disabled={busy} onPress={() => { setAliases(previous => ({ ...previous, [person.user_id]: person.default_aliases.join(", ") })); setNotice(""); }} style={s.reset}><Text style={s.buttonText}>기본 호칭으로 되돌리기</Text></Pressable>
            </View>)}
            {context.suggestions?.length ? <View style={s.person}><Text style={s.name}>관계별 호칭 참고</Text><Text style={s.hint}>실제로 쓰는 표현만 해당 사람의 호칭에 추가해주세요.</Text>{context.suggestions.map((suggestion, index) => <Text key={`${suggestion.relation}:${index}`} style={s.body}>{suggestion.relation}: {suggestion.aliases.join(", ")}</Text>)}</View> : null}
          </> : null}
          {context && tab === "memory" ? <>
            <Text style={s.body}>대화에서 기억할 선호나 가족 표현을 적어주세요. 이 메모는 질문을 이해하는 참고이며, 사건의 사실이나 기록 근거로 취급하지 않습니다.</Text>
            <TextInput accessibilityLabel="개인 메모리" multiline maxLength={2000} value={memory} onChangeText={value => { setMemory(value); setNotice(""); }} editable={!busy} style={[s.input, s.memory]} placeholder="예: 남편을 자기라고 불러요. 답변은 짧고 편하게 해주세요." placeholderTextColor={colors.muted} />
            <Text style={s.hint}>{memory.length} / 2,000자</Text>
          </> : null}
          {error ? <Text accessibilityRole="alert" style={s.error}>{error}</Text> : null}
          {(!context && error || conflict) ? <Pressable accessibilityRole="button" disabled={busy} onPress={() => load()} style={s.button}><Text style={s.buttonText}>최신 설정 다시 불러오기</Text></Pressable> : null}
          {notice ? <Text accessibilityLiveRegion="polite" style={s.success}>{notice}</Text> : null}
        </ScrollView>
        <View style={s.footer}>
          <Pressable accessibilityRole="button" onPress={onClose} style={s.button}><Text style={s.buttonText}>닫기</Text></Pressable>
          <Pressable accessibilityRole="button" disabled={busy || !context || conflict} onPress={save} style={[s.save, (busy || !context || conflict) && s.disabled]}><Text style={s.saveText}>{busy ? "처리 중…" : "저장"}</Text></Pressable>
        </View>
      </View>
    </KeyboardAvoidingView>
  </Modal>;
}

const createStyles = colors => StyleSheet.create({
  backdrop: { flex: 1, backgroundColor: colors.scrim, justifyContent: "center", alignItems: "center", padding: 16 },
  panel: { width: "100%", maxWidth: 620, maxHeight: "92%", backgroundColor: colors.surface, borderRadius: 20, overflow: "hidden", flexShrink: 1 },
  heading: { flexDirection: "row", alignItems: "center", padding: 18, gap: 12 },
  grow: { flex: 1 },
  title: { color: colors.text, fontWeight: "700", fontSize: 20 },
  hint: { color: colors.muted, fontSize: 13, lineHeight: 20 },
  tabs: { flexDirection: "row", gap: 8, paddingHorizontal: 18 },
  tab: { flex: 1, minHeight: 44, alignItems: "center", justifyContent: "center", borderRadius: 12, borderWidth: 1, borderColor: colors.border },
  selected: { backgroundColor: colors.primarySoft, borderColor: colors.primary },
  content: { padding: 18, gap: 16 },
  body: { color: colors.text, fontSize: 14, lineHeight: 22 },
  person: { gap: 8, paddingBottom: 12, borderBottomWidth: 1, borderBottomColor: colors.border },
  name: { color: colors.text, fontWeight: "700", fontSize: 16 },
  input: { minHeight: 48, borderWidth: 1, borderColor: colors.border, borderRadius: 12, padding: 12, color: colors.text, backgroundColor: colors.background, fontSize: 15 },
  memory: { minHeight: 180, textAlignVertical: "top", lineHeight: 23 },
  reset: { minHeight: 44, justifyContent: "center", alignSelf: "flex-start", paddingHorizontal: 8 },
  button: { minHeight: 44, paddingHorizontal: 14, justifyContent: "center", alignItems: "center", borderRadius: 12, backgroundColor: colors.primarySoft },
  buttonText: { color: colors.primaryStrong, fontWeight: "600", fontSize: 14 },
  footer: { padding: 16, flexDirection: "row", gap: 12, borderTopWidth: 1, borderTopColor: colors.border },
  save: { flex: 1, minHeight: 48, borderRadius: 12, backgroundColor: colors.primary, alignItems: "center", justifyContent: "center" },
  saveText: { color: colors.onPrimary, fontSize: 15, fontWeight: "700" },
  disabled: { opacity: 0.5 },
  error: { color: colors.danger, fontSize: 14, lineHeight: 22 },
  success: { color: colors.success, fontSize: 14, lineHeight: 22 }
});
