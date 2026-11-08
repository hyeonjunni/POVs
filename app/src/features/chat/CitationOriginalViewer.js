import { useTheme, useThemedStyles } from "../../shared/theme/ThemeProvider";
import React, { useState } from "react";
import { ActivityIndicator, Image, Modal, Pressable, ScrollView, StyleSheet, Text, View } from "react-native";
import { PassiveVideo } from "../records/RecordMedia";
import { PassiveAudio } from "../records/RecordAudio";
import { audioCitationLocation } from "./audioCitation.cjs";

export function CitationOriginalViewer({ original, onClose, serviceMode = false }) {
  const { colors } = useTheme();
  const p = useThemedStyles(createStyles);
  const [mediaFailed, setMediaFailed] = useState(false);
  const [imageWidth, setImageWidth] = useState(1);
  const [imageRatio, setImageRatio] = useState(1);
  const [zoom, setZoom] = useState(1);
  if (!original) return null;
  const { loading, error, media, citation } = original;
  return <Modal transparent visible onRequestClose={onClose}>
    <View style={p.backdrop}><View style={[p.sheet, media?.kind === "image" && p.imageSheet]} accessibilityViewIsModal>
      <Text style={p.title}>{serviceMode ? ({ image: "사진", audio: "음성", video: "영상", text: "기록" }[media?.kind] || "기록 보기") : "답변의 원본 근거"}</Text>
      {!serviceMode ? <>
        <Text style={p.description}>답변이 참조한 파일과 버전만 표시합니다.</Text>
        <Text style={p.description}>근거 위치: {citation.chunk_char_start}–{citation.chunk_char_end}자</Text>
        {audioCitationLocation(citation) ? <Text style={p.description}>{audioCitationLocation(citation)} · 재생기의 ±10초 버튼으로 이동할 수 있습니다.</Text> : null}
      </> : null}
      {loading ? <View style={p.loading}><ActivityIndicator color={colors.primary} /><Text style={p.description}>원본을 확인하고 있습니다.</Text></View> : null}
      {error || mediaFailed ? <Text accessibilityRole="alert" style={p.error}>{error || (serviceMode ? "파일을 표시하지 못했습니다. 다시 열어주세요." : "원본을 표시하지 못했습니다. 이 근거를 최신 파일로 바꾸지 않았습니다.")}</Text> : null}
      {!error && !mediaFailed && media?.kind === "image" ? <>
        <View style={p.imageControls}>
          <Pressable accessibilityRole="button" accessibilityLabel="이미지 축소" disabled={zoom === 1} accessibilityState={{ disabled: zoom === 1 }} onPress={() => setZoom(value => Math.max(1, value - 1))} style={[p.zoomButton, zoom === 1 && p.disabled]}><Text style={p.closeText}>축소</Text></Pressable>
          <Pressable accessibilityRole="button" accessibilityLabel="이미지 너비 맞춤" onPress={() => setZoom(1)} style={p.zoomButton}><Text style={p.closeText}>{Math.round(zoom * 100)}% · 너비 맞춤</Text></Pressable>
          <Pressable accessibilityRole="button" accessibilityLabel="이미지 확대" disabled={zoom === 4} accessibilityState={{ disabled: zoom === 4 }} onPress={() => setZoom(value => Math.min(4, value + 1))} style={[p.zoomButton, zoom === 4 && p.disabled]}><Text style={p.closeText}>확대</Text></Pressable>
        </View>
        <View style={p.imageViewport} onLayout={event => setImageWidth(Math.max(1, event.nativeEvent.layout.width))}>
          <ScrollView horizontal nestedScrollEnabled style={p.imageViewport}>
            <ScrollView nestedScrollEnabled style={{ width: imageWidth * zoom }}>
              <Image accessibilityLabel="기록 원본 이미지" source={media.source} resizeMode="contain"
                style={{ width: imageWidth * zoom, height: imageWidth * zoom / imageRatio }}
                onLoad={event => {
                  const { source, target } = event.nativeEvent;
                  const width = source?.width ?? target?.naturalWidth;
                  const height = source?.height ?? target?.naturalHeight;
                  if (width > 0 && height > 0) setImageRatio(width / height);
                }}
                onError={() => setMediaFailed(true)} />
            </ScrollView>
          </ScrollView>
        </View>
      </> : null}
      {!error && media?.kind === "video" ? <PassiveVideo source={media.source} style={p.media} /> : null}
      {!error && media?.kind === "text" ? <ScrollView style={p.textOriginal}><Text selectable style={p.originalText}>{media.text}</Text></ScrollView> : null}
      {!error && !mediaFailed && media?.kind === "audio" ? <PassiveAudio source={media.source} onError={() => setMediaFailed(true)} /> : null}
      <Pressable accessibilityRole="button" accessibilityLabel="원본 근거 닫기" onPress={onClose} style={p.close}><Text style={p.closeText}>닫기</Text></Pressable>
    </View></View>
  </Modal>;
}

const createStyles = colors => StyleSheet.create({
  backdrop: { flex: 1, backgroundColor: colors.scrim, justifyContent: "center", padding: 20 },
  sheet: { backgroundColor: colors.surface, borderRadius: 20, padding: 20, gap: 12, maxHeight: "90%" },
  imageSheet: { height: "90%" },
  imageViewport: { flex: 1, minHeight: 0 },
  imageControls: { flexDirection: "row", gap: 8, justifyContent: "center" },
  zoomButton: { minHeight: 48, paddingHorizontal: 12, borderRadius: 12, backgroundColor: colors.primarySoft, justifyContent: "center", alignItems: "center" },
  disabled: { opacity: 0.45 },
  title: { color: colors.text, fontSize: 20, fontWeight: "700" },
  description: { color: colors.muted, fontSize: 13, lineHeight: 20 },
  loading: { gap: 12, alignItems: "center", paddingVertical: 36 },
  error: { color: colors.danger, lineHeight: 22, paddingVertical: 16 },
  media: { width: "100%", height: 300 }, textOriginal: { maxHeight: 360 },
  originalText: { color: colors.text, lineHeight: 22, fontSize: 14 },
  close: { minHeight: 48, borderRadius: 12, backgroundColor: colors.primarySoft, justifyContent: "center", alignItems: "center" },
  closeText: { color: colors.primaryStrong, fontWeight: "700" },
});
