import { useTheme } from "../../shared/theme/ThemeProvider";
import React, { useState, useEffect } from "react";
import { View, Text, ScrollView, Pressable } from "react-native";
import { BASE_SCROLL_BOTTOM_PADDING } from "../../shared/ui/layout";
import { AuthenticatedMedia } from "./RecordMedia";
import { NewBadge } from "./NewBadge";
import { RecordStatus } from "./RecordStatus";
const { recordMatchesScope, managementError } = require("./recordManagement.cjs");
export function StorageScreen({
  groupedRecords,
  onManageRecord,
  onRefresh,
  hasPendingMediaUpload = () => false,
  onRetryMediaUpload,
  hasPendingOcrReview = () => false,
  onReopenOcrReview,
  mediaToken,
  mediaBaseUrl,
  bottomInset,
  onViewMedia,
  onOpenRecord,
  onDeleteRecord,
  initialType = "image"
}) {
  const { styles, colors } = useTheme();
  const [activeStorageType, setActiveStorageType] = useState(initialType);
  const [scope, setScope] = useState("COMBINED");
  const [refreshing, setRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState("");
  useEffect(() => setActiveStorageType(initialType), [initialType]);
  groupedRecords = groupedRecords.map(group => ({ ...group, items: group.items.filter(record => recordMatchesScope(record, scope)) }));
  const activeGroup = groupedRecords.find(group => group.key === activeStorageType) || groupedRecords[0];
  return <View style={styles.storageScreen}>
      <View style={styles.storageTopArea}>
        <Text style={styles.sectionTitle}>{"기록"}</Text>
        <Text style={styles.sectionDescription}>
          {"유형별로 가족의 기억을 확인하세요."}
        </Text>
        <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 8, marginVertical: 8 }}>
          {[["COMBINED", "전체"], ["PERSONAL", "나만 보기"], ["FAMILY", "가족 공유"]].map(([value, label]) => <Pressable key={value} accessibilityRole="radio" accessibilityState={{ checked: scope === value }} style={[styles.storageTypeTab, scope === value && { backgroundColor: colors.primarySoft }]} onPress={() => setScope(value)}><Text style={styles.storageTypeTabText}>{label}</Text></Pressable>)}
          {onRefresh ? <Pressable accessibilityRole="button" disabled={refreshing} style={styles.storageTypeTab} onPress={async () => {
            setRefreshing(true); setRefreshError(""); try { await onRefresh(); } catch (error) { setRefreshError(managementError(error)); } finally { setRefreshing(false); }
          }}><Text style={styles.storageTypeTabText}>{refreshing ? "불러오는 중" : "새로고침"}</Text></Pressable> : null}
        </View>
        {refreshError ? <Text accessibilityRole="alert" style={styles.recordDetail}>{refreshError}</Text> : null}

        <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={styles.storageTypeTabs}>
          {groupedRecords.map(group => {
          const active = group.key === activeStorageType;
          return <Pressable key={group.key} style={[styles.storageTypeTab, active && {
            backgroundColor: colors.primarySoft,
            borderColor: colors.primary
          }]} onPress={() => setActiveStorageType(group.key)}>
                <Text style={[styles.storageTypeTabText, active && styles.storageTypeTabTextActive]}>
                  {group.label}
                </Text>
                <Text style={[styles.storageTypeTabCount, active && styles.storageTypeTabTextActive]}>
                  {group.items.length}
                </Text>
              </Pressable>;
        })}
        </ScrollView>
      </View>

      <ScrollView contentContainerStyle={[styles.storageListContent, {
      paddingBottom: BASE_SCROLL_BOTTOM_PADDING + bottomInset
    }]} showsVerticalScrollIndicator={false}>
        <View style={styles.storageSection}>
          <View style={styles.storageSectionHeader}>
            <View style={[styles.storageBadge, {
            backgroundColor: colors.primarySoft
          }]}>
              <Text style={[styles.storageBadgeText, {
              color: colors.primary
            }]}>
                {activeGroup.label}{" 기록"}
              </Text>
            </View>
            <Text style={styles.storageCount}>{activeGroup.items.length}{"개"}</Text>
          </View>

          {activeGroup.items.length === 0 ? <View style={styles.emptyCard}>
              <Text style={styles.emptyCardText}>
                {"아직 업로드된 "}{activeGroup.label}{" 항목이 없습니다."}
              </Text>
            </View> : activeGroup.items.map(item => {
          const canViewMedia = item.hasFile && ["image", "video", "voice"].includes(item.type);
          return <View key={item.id} style={styles.recordCard}>
                  {!item.hasFile && ["image", "video", "voice"].includes(item.type) ? <Text accessibilityRole="alert" style={styles.recordDetail}>파일 업로드 미완료 · 기록 정보만 저장되어 있습니다. 파일 상태를 확인해주세요.</Text> : null}
                  {hasPendingMediaUpload(item) ? <Pressable accessibilityRole="button" style={styles.mediaViewButton} onPress={() => onRetryMediaUpload(item)}><Text style={styles.mediaViewButtonText}>파일 다시 시도</Text></Pressable> : null}
                  {item.type === "image" && item.hasFile ? <AuthenticatedMedia item={item} token={mediaToken} baseUrl={mediaBaseUrl} /> : null}
                  {item.type === "video" && item.hasFile ? <View style={styles.videoPreviewBox}>
                      <Text style={styles.videoPreviewIcon}>{"▶"}</Text>
                      <Text style={styles.videoPreviewText}>{"영상 파일이 저장되었습니다."}</Text>
                    </View> : null}
                  <Pressable accessibilityRole="button" accessibilityLabel={`${item.title}${item.isUnread ? ", 아직 열어보지 않은 기록" : ""}, 기록 보기`} disabled={!onOpenRecord} onPress={() => onOpenRecord?.(item)} style={[styles.recordHeader, { minHeight: 44, gap: 8 }]}>
                    <Text style={styles.recordTitle}>{item.title}</Text>
                    <NewBadge visible={item.isUnread} />
                    <Text style={styles.recordDate}>{item.displayDate || `등록일 ${item.createdAt}`}</Text>
                  </Pressable>
                  <RecordStatus record={item} />
                  <Text style={styles.recordDetail}>{item.detail}</Text>
                  {item.tags?.length ? <View style={styles.tagRow}>
                      {item.tags.map(tag => <View key={`${item.id}-${tag}`} style={styles.tagChip}>
                          <Text style={styles.tagChipText}>{`#${tag}`}</Text>
                        </View>)}
                    </View> : null}
                  {hasPendingOcrReview(item) ? <Pressable accessibilityRole="button" style={styles.mediaViewButton} onPress={() => onReopenOcrReview(item)}>
                    <Text style={styles.mediaViewButtonText}>OCR 결과 확인</Text>
                  </Pressable> : null}
                  <View style={styles.recordActionRow}>
                    {canViewMedia ? <Pressable accessibilityRole="button" style={styles.mediaViewButton} onPress={() => onOpenRecord ? onOpenRecord(item) : onViewMedia(item)}>
                        <Text style={styles.mediaViewButtonText}>{({ image: "사진 보기", video: "영상 보기", voice: "음성 듣기" })[item.type]}</Text>
                      </Pressable> : onOpenRecord ? <Pressable accessibilityRole="button" style={styles.mediaViewButton} onPress={() => onOpenRecord(item)}><Text style={styles.mediaViewButtonText}>기록 보기</Text></Pressable> : null}
                    {item.capabilities?.can_soft_delete === true ? <Pressable accessibilityRole="button" style={[styles.recordDeleteButton, !canViewMedia && !onOpenRecord && styles.recordDeleteButtonFull]} onPress={() => onDeleteRecord(item)}>
                      <Text style={styles.recordDeleteButtonText}>삭제</Text>
                    </Pressable> : null}
                  </View>
                  {onManageRecord ? <Pressable accessibilityRole="button" style={styles.mediaViewButton} onPress={() => onManageRecord(item)}><Text style={styles.mediaViewButtonText}>공개 범위·기록 관리</Text></Pressable> : null}
                </View>;
        })}
        </View>
      </ScrollView>
    </View>;
}
