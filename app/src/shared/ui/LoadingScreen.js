import { useTheme } from "../theme/ThemeProvider";
import { SafeAreaView, StatusBar, View, Text } from "react-native";
import { StatusBar as ExpoStatusBar } from "expo-status-bar";
import React from "react";
export function LoadingScreen({
  message = "업로드 기록과 가족방 정보를 확인하고 있습니다."
}) {
  const { styles, dark } = useTheme();
  return <SafeAreaView style={[styles.safeArea, styles.loginSafeArea]}>
      <ExpoStatusBar style={dark ? "light" : "dark"} />
      <StatusBar barStyle={dark ? "light-content" : "dark-content"} />
      <View style={styles.loadingScreen}>
        <Text style={styles.loginEyebrow}>POVs</Text>
        <Text style={styles.loadingTitle}>{"\uc800\uc7a5\ub41c \uae30\ub85d\uc744 \ubd88\ub7ec\uc624\ub294 \uc911"}</Text>
        <Text style={styles.loginDescription}>{message}</Text>
      </View>
    </SafeAreaView>;
}
