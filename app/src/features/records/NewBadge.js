import { useThemedStyles } from "../../shared/theme/ThemeProvider";
import React from "react";
import { StyleSheet, Text } from "react-native";

export function NewBadge({ visible, compact = false }) {
  const s = useThemedStyles(createStyles);
  if (!visible) return null;
  return <Text accessibilityLabel="아직 열어보지 않은 기록" style={[s.badge, compact && s.compact]}>New</Text>;
}

const createStyles = colors => StyleSheet.create({
  badge: { color: colors.danger, backgroundColor: colors.dangerSoft, borderRadius: 6, overflow: "hidden", paddingHorizontal: 6, paddingVertical: 2, fontSize: 11, lineHeight: 16, fontWeight: "700", alignSelf: "center", flexShrink: 0 },
  compact: { position: "absolute", top: 4, right: 4, fontSize: 10, lineHeight: 14, paddingHorizontal: 4, paddingVertical: 1 },
});
