import { useTheme } from "../../shared/theme/ThemeProvider";
import { Modal, View, ScrollView, Platform, Text, TextInput, Pressable } from "react-native";
import { COMMON_SINGLE_LINE_INPUT_PROPS } from "../../shared/ui/layout";
import React from "react";
import { VisibilitySelector } from "./RecordStatus";
export function RecordComposer({
  formVisibility,
  setFormVisibility,
  selectedAsset,
  modalVisible,
  closeUploadModal,
  bottomInset,
  keyboardInset,
  selectedTypeInfo,
  formTitle,
  setFormTitle,
  formDetail,
  setFormDetail,
  formTags,
  setFormTags,
  uploadSaving,
  handleAddRecord
}) {
  const { styles, colors } = useTheme();
  return <Modal animationType="slide" transparent visible={modalVisible} onRequestClose={closeUploadModal}>
        <View style={styles.modalBackdrop}>
          <ScrollView contentContainerStyle={[styles.modalScrollContent, {
        paddingBottom: bottomInset
      }]} showsVerticalScrollIndicator={false} keyboardShouldPersistTaps="handled" keyboardDismissMode="on-drag">
          <View style={[styles.modalSheet, Platform.OS === "android" && keyboardInset > 0 ? {
          marginBottom: Math.max(12, keyboardInset - bottomInset)
        } : null]}>
            <View style={styles.modalHandle} />
            <Text style={styles.modalTitle}>{selectedTypeInfo ? `${selectedTypeInfo.label} 업로드` : "업로드"}</Text>
            <Text style={styles.modalDescription}>
              제목과 설명, 태그를 입력해 기록을 남겨주세요.
            </Text>

            {selectedAsset ? <Text style={styles.modalDescription}>선택한 파일: {selectedAsset.fileName}</Text> : null}
            <View style={styles.inputGroup}>
              <Text style={styles.inputLabel}>공개 범위</Text>
              <VisibilitySelector value={formVisibility} onChange={setFormVisibility} disabled={uploadSaving} />
              {selectedTypeInfo?.key === "text" ? <Text style={styles.modalDescription}>직접 쓰는 글은 나만 보기로 시작합니다. 함께 볼 기록은 가족 전체를 선택하세요. 저장 후에도 공개 범위를 바꿀 수 있습니다.</Text> : null}
            </View>

            <View style={styles.inputGroup}>
              <Text style={styles.inputLabel}>제목</Text>
              <TextInput {...COMMON_SINGLE_LINE_INPUT_PROPS} value={formTitle} onChangeText={setFormTitle} placeholder="예: 할머니의 창업 조언" placeholderTextColor={colors.placeholder} style={styles.textInput} />
            </View>

            <View style={styles.inputGroup}>
              <Text style={styles.inputLabel}>{selectedTypeInfo?.key === "text" ? "내용" : "설명"}</Text>
              <TextInput value={formDetail} onChangeText={setFormDetail} placeholder="예: 2019년 인터뷰에서 남긴 내용" placeholderTextColor={colors.placeholder} style={[styles.textInput, styles.textArea]} multiline textAlignVertical="top" />
            </View>

            <View style={styles.inputGroup}>
              <Text style={styles.inputLabel}>태그</Text>
              <TextInput {...COMMON_SINGLE_LINE_INPUT_PROPS} value={formTags} onChangeText={setFormTags} placeholder="예: 송년회, 가족행사" placeholderTextColor={colors.placeholder} style={styles.textInput} />
            </View>

            <View style={styles.modalButtons}>
              <Pressable style={[styles.modalButton, styles.cancelButton, uploadSaving && styles.disabledButton]} onPress={closeUploadModal} disabled={uploadSaving}>
                <Text style={styles.cancelButtonText}>취소</Text>
              </Pressable>
              <Pressable style={[styles.modalButton, styles.submitButton, (uploadSaving || !formTitle.trim() || (selectedTypeInfo?.key === "text" && !formDetail.trim())) && styles.disabledButton]} onPress={handleAddRecord} disabled={uploadSaving || !formTitle.trim() || (selectedTypeInfo?.key === "text" && !formDetail.trim())}>
                <Text style={styles.submitButtonText}>저장하기</Text>
              </Pressable>
            </View>
          </View>
          </ScrollView>
        </View>
      </Modal>;
}
