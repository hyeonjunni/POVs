import { useThemedStyles } from "../../shared/theme/ThemeProvider";
import React, { useEffect } from "react";
import { Modal, View, Text, ScrollView, Pressable, StyleSheet } from "react-native";
import { RecordStatus } from "./RecordStatus";

export function RecordDetailViewer({ record, onClose, onViewed, onManage, onAsk }) {
  const s = useThemedStyles(createStyles);
  useEffect(() => {
    if (record.type === "text" || record.type === "voice") onViewed?.(record.id);
  }, [record.id, record.type, onViewed]);
  return <Modal visible transparent onRequestClose={onClose}>
    <View style={s.backdrop}><View style={s.sheet} accessibilityViewIsModal>
      <Text accessibilityRole="header" style={s.title}>{record.title}</Text>
      <Text style={s.date}>{record.displayDate || `등록일 ${record.createdAt}`}</Text>
      <RecordStatus record={record} />
      <ScrollView style={s.content} contentContainerStyle={s.copy}>
        <Text selectable style={s.body}>{record.detail || "추가 설명이 없습니다."}</Text>
        {record.tags?.length ? <Text style={s.tags}>{record.tags.map(tag => `#${tag}`).join("  ")}</Text> : null}
        {["image", "video"].includes(record.type) && !record.hasFile ? <Text accessibilityRole="alert" style={s.notice}>파일 업로드가 완료되지 않았습니다. 원본을 확인할 때까지 New 표시를 유지합니다.</Text> : null}
      </ScrollView>
      {onAsk ? <Pressable accessibilityRole="button" onPress={onAsk} style={s.close}><Text style={s.closeText}>이 기록에 질문</Text></Pressable> : null}
      {onManage ? <Pressable accessibilityRole="button" onPress={onManage} style={s.close}><Text style={s.closeText}>공개 범위·기록 관리</Text></Pressable> : null}
      <Pressable accessibilityRole="button" accessibilityLabel="기록 닫기" onPress={onClose} style={s.close}><Text style={s.closeText}>닫기</Text></Pressable>
    </View></View>
  </Modal>;
}

const createStyles = colors => StyleSheet.create({
  backdrop: { flex: 1, justifyContent: "center", backgroundColor: colors.scrim, padding: 20 },
  sheet: { maxHeight: "85%", backgroundColor: colors.surface, borderRadius: 20, padding: 20, gap: 12 },
  title: { fontSize: 20, lineHeight: 28, fontWeight: "700", color: colors.text },
  date: { fontSize: 12, color: colors.muted },
  content: { maxHeight: 420 }, copy: { gap: 14, paddingVertical: 8 },
  body: { fontSize: 16, lineHeight: 26, color: colors.text },
  tags: { fontSize: 13, lineHeight: 21, color: colors.primaryStrong },
  notice: { fontSize: 13, lineHeight: 21, color: colors.muted },
  close: { minHeight: 48, borderRadius: 12, backgroundColor: colors.primarySoft, alignItems: "center", justifyContent: "center" },
  closeText: { color: colors.primaryStrong, fontWeight: "700", fontSize: 14 },
});
