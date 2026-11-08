import React from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";
import { THEMES } from "../../shared/theme/palettes.cjs";
import { useTheme, useThemedStyles } from "../../shared/theme/ThemeProvider";

export function ThemePicker() {
  const { themeId, changeTheme, error, styles } = useTheme();
  const s = useThemedStyles(createStyles);
  return <View style={styles.profileCard}>
    <Text style={styles.profileName}>화면 테마</Text>
    <Text style={s.description}>내 취향에 맞는 색을 골라보세요.</Text>
    <View style={s.options} accessibilityLabel="화면 테마 선택">
      {THEMES.map(theme => {
        const selected = themeId === theme.id;
        const palette = theme.colors;
        return <Pressable key={theme.id} accessibilityRole="radio"
          accessibilityLabel={`${theme.name} 테마, ${theme.description}`}
          accessibilityState={{ checked: selected }} onPress={() => changeTheme(theme.id)}
          style={({ pressed }) => [s.option, selected && s.selected, pressed && { opacity: 0.8 }]}>
          <View accessible={false} importantForAccessibility="no-hide-descendants"
            style={[s.preview, { backgroundColor: palette.background, borderColor: palette.border }]}>
            <View style={[s.previewHeader, { backgroundColor: palette.surface, borderColor: palette.border }]}>
              <View style={[s.previewDot, { backgroundColor: palette.primary }]} />
              <View style={[s.previewLine, { backgroundColor: palette.border }]} />
            </View>
            <View style={[s.previewButton, { backgroundColor: palette.primaryStrong }]} />
            <View style={s.previewCards}>{[0, 1, 2].map(key => <View key={key} style={[s.previewCard, { backgroundColor: palette.primarySoft }]} />)}</View>
          </View>
          <View style={s.labelRow}><Text style={s.label}>{theme.name}</Text>
            <Text style={selected ? s.check : s.unselected}>{selected ? "✓" : "○"}</Text></View>
          <Text style={s.caption}>{theme.description}</Text>
        </Pressable>;
      })}
    </View>
    <Text style={s.footnote}>선택한 테마는 이 기기에 저장됩니다.</Text>
    {error ? <Text accessibilityRole="alert" style={s.error}>{error}</Text> : null}
  </View>;
}

const createStyles = colors => StyleSheet.create({
  description: { color: colors.muted, fontSize: 14, lineHeight: 21, marginTop: 6 },
  options: { flexDirection: "row", flexWrap: "wrap", gap: 10, marginTop: 18 },
  option: { flexGrow: 1, flexBasis: 88, minWidth: 88, padding: 8, borderWidth: 2, borderColor: colors.border, borderRadius: 16, backgroundColor: colors.surface },
  selected: { borderColor: colors.primary, backgroundColor: colors.primarySoft },
  preview: { height: 90, borderRadius: 10, borderWidth: 1, overflow: "hidden" },
  previewHeader: { height: 20, borderBottomWidth: 1, flexDirection: "row", alignItems: "center", gap: 5, paddingHorizontal: 6 },
  previewDot: { width: 6, height: 6, borderRadius: 3 }, previewLine: { width: "45%", height: 3, borderRadius: 2 },
  previewButton: { height: 13, margin: 7, borderRadius: 4 }, previewCards: { flexDirection: "row", gap: 4, paddingHorizontal: 7 },
  previewCard: { flex: 1, height: 27, borderRadius: 4 },
  labelRow: { flexDirection: "row", justifyContent: "space-between", alignItems: "center", gap: 3, marginTop: 10 },
  label: { color: colors.text, fontSize: 14, fontWeight: "700" }, check: { color: colors.primaryStrong, fontSize: 18, fontWeight: "700" },
  unselected: { color: colors.muted, fontSize: 18 }, caption: { color: colors.muted, fontSize: 11, lineHeight: 17, marginTop: 3 },
  footnote: { color: colors.muted, fontSize: 12, lineHeight: 18, marginTop: 14 }, error: { color: colors.danger, fontSize: 13, lineHeight: 20, marginTop: 8 },
});
