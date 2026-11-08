import { useTheme } from "../theme/ThemeProvider";
import React from "react";
import { Pressable, Text, View } from "react-native";
import { UiIcon } from "./UiIcon";
const tabs = [{
  key: "home",
  label: "홈",
  icon: "home"
}, {
  key: "storage",
  label: "기록",
  icon: "records"
}, {
  key: "chat",
  label: "대화",
  icon: "chat"
}, {
  key: "mypage",
  label: "마이",
  icon: "person"
}];
export function BottomTabs({
  activeTab,
  bottomInset = 0,
  onChange
}) {
  const { colors, presentationStyles: s, uiStyles: u } = useTheme();
  return <View style={[u.tabBar, {
    paddingBottom: 10 + bottomInset
  }]}>
    {tabs.map(tab => {
      const selected = activeTab === tab.key;
      return <Pressable key={tab.key} accessibilityRole="tab" accessibilityLabel={tab.label} accessibilityState={{
        selected
      }} onPress={() => onChange(tab.key)} style={u.tab}>
        <View style={[s.tabIconWrap, selected && s.tabIconWrapActive]}><UiIcon name={tab.icon} color={selected ? colors.primary : colors.muted} size={22} /></View>
        <Text style={[u.tabLabel, selected && u.selectedTabLabel]}>{tab.label}</Text>
      </Pressable>;
    })}
  </View>;
}
