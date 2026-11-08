import React, { createContext, useContext, useMemo } from "react";
import { ActivityIndicator, View } from "react-native";
import { StatusBar } from "expo-status-bar";
import { THEMES, getTheme } from "./palettes.cjs";
import { createPresentationStyles } from "./tokens";
import { createAppStyles } from "./styles";
import { createUiStyles } from "../ui/presentationStyles";
import { useThemePreference } from "./useThemePreference";

const appearances = Object.fromEntries(THEMES.map(theme => [theme.id, {
  ...theme,
  styles: createAppStyles(theme.colors),
  presentationStyles: createPresentationStyles(theme.colors),
  uiStyles: createUiStyles(theme.colors),
}]));
const ThemeContext = createContext(null);

export function ThemeProvider({ children }) {
  const preference = useThemePreference();
  const appearance = appearances[getTheme(preference.themeId).id];
  const value = { ...appearance, ...preference };
  return <ThemeContext.Provider value={value}>
    <StatusBar style={appearance.dark ? "light" : "dark"} />
    {preference.ready ? children : <View style={{ flex: 1, backgroundColor: appearance.colors.background, alignItems: "center", justifyContent: "center" }}>
      <ActivityIndicator accessibilityLabel="화면 설정을 불러오는 중" color={appearance.colors.primary} />
    </View>}
  </ThemeContext.Provider>;
}

export function useTheme() {
  const value = useContext(ThemeContext);
  if (!value) throw new Error("useTheme must be used inside ThemeProvider");
  return value;
}

export function useThemedStyles(factory) {
  const { colors } = useTheme();
  return useMemo(() => factory(colors), [factory, colors]);
}
