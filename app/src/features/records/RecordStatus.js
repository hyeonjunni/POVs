import { useThemedStyles } from "../../shared/theme/ThemeProvider";
import React from "react";
import { View, Text, Pressable, StyleSheet } from "react-native";
const { VISIBILITY_LABELS, recordStatus } = require("./recordManagement.cjs");

export function RecordStatus({ record }) {
  const s = useThemedStyles(createStyles);
  return <View style={s.row}>
    {record.metadata?.record_kind === "PERSONAL" ? <Text style={s.badge}>개인 작성 기록</Text> : record.metadata?.record_kind === "SHARED" ? <Text style={s.badge}>공동 기록</Text> : null}
    {record.visibility ? <Text style={s.badge}>{VISIBILITY_LABELS[record.visibility] || "공개 범위 확인 필요"}</Text> : null}
    <Text style={s.badge}>{recordStatus(record)}</Text>
  </View>;
}

export function VisibilitySelector({ value, onChange, disabled = false }) {
  const s = useThemedStyles(createStyles);
  return <View style={s.row}>{["PRIVATE", "FAMILY"].map(key => <Pressable key={key}
    accessibilityRole="radio" accessibilityState={{ checked: value === key, disabled }}
    disabled={disabled} onPress={() => onChange(key)} style={[s.option, value === key && s.selected, disabled && { opacity: 0.5 }]}>
    <Text style={s.label}>{VISIBILITY_LABELS[key]}</Text>
  </Pressable>)}</View>;
}
const createStyles = colors => StyleSheet.create({
  row: { flexDirection: "row", flexWrap: "wrap", gap: 8, marginVertical: 8 },
  badge: { color: colors.primaryStrong, backgroundColor: colors.primarySoft, paddingHorizontal: 10, paddingVertical: 5, borderRadius: 10, fontSize: 12 },
  option: { minHeight: 44, paddingHorizontal: 16, justifyContent: "center", borderWidth: 1, borderColor: colors.border, borderRadius: 12 },
  selected: { backgroundColor: colors.primarySoft, borderColor: colors.primary },
  label: { color: colors.text, fontSize: 14 },
});
