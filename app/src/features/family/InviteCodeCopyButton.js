import React, { useEffect, useRef, useState } from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";
import * as Clipboard from "expo-clipboard";
import { useThemedStyles } from "../../shared/theme/ThemeProvider";

export function InviteCodeCopyButton({ code, familyName }) {
  const styles = useThemedStyles(createStyles);
  const [status, setStatus] = useState("idle");
  const attempt = useRef(0);
  const copying = useRef(false);
  const resetTimer = useRef(null);
  const available = typeof code === "string" && code.trim().length > 0;

  useEffect(() => {
    setStatus("idle");
    copying.current = false;
    return () => {
      attempt.current += 1;
      clearTimeout(resetTimer.current);
    };
  }, [code]);

  async function copyCode() {
    if (!available || copying.current) return;
    copying.current = true;
    clearTimeout(resetTimer.current);
    const currentAttempt = ++attempt.current;
    setStatus("copying");
    try {
      const copied = await Clipboard.setStringAsync(code);
      if (currentAttempt !== attempt.current) return;
      setStatus(copied === true ? "copied" : "error");
      if (copied === true) resetTimer.current = setTimeout(() => setStatus("idle"), 2500);
    } catch (_error) {
      if (currentAttempt === attempt.current) setStatus("error");
    } finally {
      if (currentAttempt === attempt.current) copying.current = false;
    }
  }

  return <View style={styles.container}>
    <Pressable accessibilityRole="button" accessibilityLabel={`${familyName || "가족방"} 초대코드 복사`}
      accessibilityState={{ disabled: !available || status === "copying", busy: status === "copying" }}
      disabled={!available || status === "copying"} onPress={copyCode}
      style={({ pressed }) => [styles.button, (!available || pressed) && styles.dimmed]}>
      <Text accessibilityLiveRegion="polite" style={styles.label}>
        {!available ? "초대코드 없음" : status === "copying" ? "복사 중…" : status === "copied" ? "복사 완료 ✓" : "초대코드 복사"}
      </Text>
    </Pressable>
    {status === "error" ? <Text accessibilityRole="alert" style={styles.error}>복사하지 못했어요. 다시 눌러 주세요.</Text> : null}
  </View>;
}

const createStyles = colors => StyleSheet.create({
  container: { alignSelf: "flex-start", maxWidth: "100%", gap: 5 },
  button: { minHeight: 44, paddingHorizontal: 13, paddingVertical: 10, justifyContent: "center", alignItems: "center",
    borderRadius: 12, borderWidth: 1, borderColor: colors.border, backgroundColor: colors.primarySoft },
  label: { color: colors.primaryStrong, fontSize: 13, fontWeight: "700" },
  dimmed: { opacity: 0.55 },
  error: { color: colors.danger, fontSize: 12, lineHeight: 18, maxWidth: 200 },
});
