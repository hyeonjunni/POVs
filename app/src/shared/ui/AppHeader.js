import { useTheme } from "../theme/ThemeProvider";
import React from "react";
import { Pressable, Text, View } from "react-native";
import { UiIcon } from "./UiIcon";
export function AppHeader({
  activeFamily,
  onManageFamily
}) {
  const { presentationStyles: s, uiStyles: u } = useTheme();
  return <View style={u.header}>
    <Text style={s.brand}>POVs</Text>
    <Pressable accessibilityRole="button" accessibilityLabel="현재 가족방 관리" onPress={onManageFamily} style={s.roomLink}>
      <Text numberOfLines={1} style={s.roomLinkText}>{activeFamily?.name || "가족방 선택"}</Text>
      <UiIcon name="down" size={16} />
    </Pressable>
  </View>;
}
