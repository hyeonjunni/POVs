import { useTheme } from "../../shared/theme/ThemeProvider";
import React, { useState } from "react";
import { Modal, Pressable, ScrollView, Text, View } from "react-native";
import { UiIcon } from "../../shared/ui/UiIcon";
import { NewBadge } from "../records/NewBadge";
import { AuthenticatedMedia } from "../records/RecordMedia";
const recordTypes = [{
  key: "text",
  label: "텍스트",
  icon: "text",
  detail: "글로 남기는 가족의 기억"
}, {
  key: "image",
  label: "사진",
  icon: "image",
  detail: "사진과 문서 이미지"
}, {
  key: "voice",
  label: "음성",
  icon: "voice",
  detail: "가족의 목소리와 이야기"
}, {
  key: "video",
  label: "영상",
  icon: "video",
  detail: "함께한 순간의 영상"
}];
export function HomeScreen({
  records = [],
  onUploadPress,
  onOpenRecords,
  onOpenRecord,
  mediaToken,
  mediaBaseUrl,
  bottomInset = 0
}) {
  const { colors, presentationStyles: s, uiStyles: u } = useTheme();
  const [chooserOpen, setChooserOpen] = useState(false);
  const openType = key => {
    setChooserOpen(false);
    onUploadPress(key);
  };
  const heading = (label, type) => <View style={s.homeSectionHeader}>
    <Text style={s.homeSectionTitle}>{label}</Text>
    <Pressable accessibilityRole="button" accessibilityLabel={`${label} 기록 모두 보기`} onPress={() => onOpenRecords(type)} style={u.linkButton}>
      <Text style={s.homeLink}>모두 보기</Text>
    </Pressable>
  </View>;
  return <>
    <ScrollView style={u.home} contentContainerStyle={[s.homeContent, {
      paddingBottom: bottomInset + 24
    }]} showsVerticalScrollIndicator={false}>
      <View style={s.homeHero}>
        <Text style={s.homeTitle}>우리 가족의 기록</Text>
        <Pressable accessibilityRole="button" accessibilityLabel="기록 추가" onPress={() => setChooserOpen(true)} style={s.homeAddButton}>
          <UiIcon name="add" color={colors.onPrimary} size={24} /><Text style={s.homeAddText}>기록 추가</Text>
        </Pressable>
      </View>
      {[{
        type: "image",
        label: "사진",
        icon: "image"
      }, {
        type: "video",
        label: "영상",
        icon: "video"
      }].map(section => {
        const items = records.filter(record => record.type === section.type).slice(0, 8);
        return <View key={section.type} style={s.homeSection}>
          {heading(section.label, section.type)}
          {items.length ? <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={s.homeCovers}>
            {items.map(record => {
              const hasPhoto = section.type === "image" && record.hasFile;
              return <Pressable key={record.id} accessibilityRole="button" accessibilityLabel={`${record.title}${record.isUnread ? ", 아직 열어보지 않은 기록" : ""}, 기록 보기`} onPress={() => onOpenRecord ? onOpenRecord(record) : onOpenRecords(section.type)} style={s.homeCover}>
                {hasPhoto ? <View pointerEvents="none" style={s.homeCoverMedia}>
                  {/* Passive previews must not mark the record as read. */}
                  <AuthenticatedMedia item={record} token={mediaToken} baseUrl={mediaBaseUrl} imageStyle={s.homeCoverImage} fallback={<UiIcon name="image" size={28} />} />
                </View> : <UiIcon name={section.icon} size={28} />}
                {hasPhoto ? <View pointerEvents="none" style={s.homeCoverTitleOverlay}>
                  <Text numberOfLines={2} style={s.homeCoverPhotoTitle}>{record.title}</Text>
                </View> : <Text numberOfLines={2} style={s.homeCoverTitle}>{record.title}</Text>}
                {!record.hasFile ? <Text accessibilityRole="alert" style={s.homeEmpty}>파일 업로드 미완료</Text> : null}
                <NewBadge visible={record.isUnread} compact />
              </Pressable>;
            })}
          </ScrollView> : <View style={u.emptySection}><Text style={s.homeEmpty}>아직 {section.label} 기록이 없어요.</Text></View>}
        </View>;
      })}
      {[{
        type: "text",
        label: "텍스트"
      }, {
        type: "voice",
        label: "음성"
      }, {
        type: "event",
        label: "사건"
      }].map(section => {
        const items = records.filter(record => (record.displayType || record.type) === section.type).slice(0, 3);
        return <View key={section.type} style={s.homeSection}>
          {heading(section.label, section.type)}
          {items.length ? items.map(record => <Pressable key={record.id} accessibilityRole="button" accessibilityLabel={`${record.title}${record.isUnread ? ", 아직 열어보지 않은 기록" : ""}, 기록 보기`} onPress={() => onOpenRecord ? onOpenRecord(record) : onOpenRecords(section.type)} style={s.homeRecordRow}>
            {section.type === "voice" ? <UiIcon name="voice" size={20} /> : null}
            <Text numberOfLines={2} style={s.homeRecordTitle}>{record.title}</Text><NewBadge visible={record.isUnread} /><UiIcon name="chevron" size={16} color={colors.muted} />
          </Pressable>) : <View style={u.emptySection}><Text style={s.homeEmpty}>아직 {section.label} 기록이 없어요.</Text></View>}
        </View>;
      })}
    </ScrollView>
    <Modal visible={chooserOpen} transparent animationType="slide" onRequestClose={() => setChooserOpen(false)}>
      <View style={u.scrim}>
        <View style={[s.homeChooserSheet, {
          paddingBottom: bottomInset + 24
        }]} accessibilityViewIsModal>
          <View style={u.sheetHeading}><Text style={u.sheetTitle}>어떤 기록을 남길까요?</Text>
            <Pressable accessibilityRole="button" accessibilityLabel="기록 종류 선택 닫기" onPress={() => setChooserOpen(false)} style={u.iconButton}><UiIcon name="close" color={colors.muted} /></Pressable>
          </View>
          <ScrollView showsVerticalScrollIndicator={false} keyboardShouldPersistTaps="handled">
            {recordTypes.map(type => <Pressable key={type.key} accessibilityRole="button" accessibilityLabel={`${type.label} 기록 추가`} onPress={() => openType(type.key)} style={s.homeChooserRow}>
              <View style={s.homeTypeIcon}><UiIcon name={type.icon} /></View>
              <View style={u.flex}><Text style={s.homeChooserText}>{type.label}</Text><Text style={s.homeChooserDetail}>{type.detail}</Text></View>
              <UiIcon name="chevron" color={colors.muted} size={18} />
            </Pressable>)}
          </ScrollView>
        </View>
      </View>
    </Modal>
  </>;
}
