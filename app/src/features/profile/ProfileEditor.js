import { useTheme } from "../../shared/theme/ThemeProvider";
import { Modal, View, ScrollView, Platform, Text, TextInput, Pressable } from "react-native";
import { COMMON_SINGLE_LINE_INPUT_PROPS } from "../../shared/ui/layout";
import React from "react";
export function ProfileEditor({
  profileModalVisible,
  profileEditorMode,
  closeProfileEditor,
  bottomInset,
  keyboardInset,
  profileDraft,
  setProfileDraft,
  profileSaving,
  handleLogout,
  profileFormComplete,
  handleSaveProfile,
  descriptionOverride
}) {
  const { styles, colors } = useTheme();
  return <Modal transparent visible={profileModalVisible} onRequestClose={profileEditorMode === "edit" ? closeProfileEditor : () => {}}>
        <View style={styles.centerModalBackdrop}>
          <ScrollView contentContainerStyle={[styles.centerModalScrollContent, {
        paddingBottom: 28 + bottomInset
      }]} showsVerticalScrollIndicator={false} keyboardShouldPersistTaps="handled" keyboardDismissMode="on-drag">
          <View style={[styles.profileModalSheet, Platform.OS === "android" && keyboardInset > 0 ? {
          marginBottom: Math.max(12, keyboardInset - bottomInset)
        } : null]}>
            <Text style={styles.modalTitle}>{profileEditorMode === "edit" ? "내 정보 수정" : "개인정보 입력"}</Text>
            <Text style={styles.modalDescription}>
              {descriptionOverride ?? (profileEditorMode === "edit" ? "현재 계정의 개인정보를 수정합니다. 변경한 내용은 가족 멤버 상세정보와 프로필 청크에 함께 반영됩니다." : "회원가입 시 저장한 이름과 이메일을 제외한 기본 정보를 입력해주세요. 저장 시 프로필 청크로 함께 보관됩니다.")}
            </Text>
            <Text style={styles.modalHint}>
              {profileEditorMode === "edit" ? "이름, 이메일, 나이, 성별, 휴대폰번호를 모두 확인한 뒤 저장해주세요." : "나이, 성별, 휴대폰번호를 입력해야 확인 버튼이 활성화됩니다."}
            </Text>

            {profileEditorMode === "edit" ? <View style={styles.inputGroup}>
                <Text style={styles.inputLabel}>이름</Text>
                <TextInput {...COMMON_SINGLE_LINE_INPUT_PROPS} value={profileDraft.name} onChangeText={value => setProfileDraft(prev => ({
              ...prev,
              name: value
            }))} placeholder="예: 홍길동" placeholderTextColor={colors.placeholder} style={styles.textInput} />
              </View> : null}

            <View style={styles.inputGroup}>
              <Text style={styles.inputLabel}>{"\ub098\uc774"}</Text>
              <TextInput {...COMMON_SINGLE_LINE_INPUT_PROPS} value={profileDraft.age} onChangeText={value => setProfileDraft(prev => ({
              ...prev,
              age: value
            }))} placeholder="예: 24" placeholderTextColor={colors.placeholder} keyboardType="number-pad" style={styles.textInput} />
            </View>

            <View style={styles.inputGroup}>
              <Text style={styles.inputLabel}>{"\uc131\ubcc4"}</Text>
              <View style={styles.genderOptionRow}>
                {["\ub0a8\uc131", "\uc5ec\uc131", "\uae30\ud0c0"].map(option => <Pressable key={option} style={[styles.genderOptionButton, profileDraft.gender === option && styles.genderOptionButtonActive]} onPress={() => setProfileDraft(prev => ({
                ...prev,
                gender: option
              }))}>
                    <Text style={[styles.genderOptionText, profileDraft.gender === option && styles.genderOptionTextActive]}>{option}</Text>
                  </Pressable>)}
              </View>
            </View>

            <View style={styles.inputGroup}>
              <Text style={styles.inputLabel}>{"\ud734\ub300\ud3f0\ubc88\ud638"}</Text>
              <TextInput {...COMMON_SINGLE_LINE_INPUT_PROPS} value={profileDraft.phone} onChangeText={value => setProfileDraft(prev => ({
              ...prev,
              phone: value
            }))} placeholder="예: 010-1234-5678" placeholderTextColor={colors.placeholder} keyboardType="phone-pad" style={styles.textInput} />
            </View>

            {profileEditorMode === "edit" ? <View style={styles.inputGroup}>
                <Text style={styles.inputLabel}>이메일</Text>
                <TextInput {...COMMON_SINGLE_LINE_INPUT_PROPS} value={profileDraft.email} onChangeText={value => setProfileDraft(prev => ({
              ...prev,
              email: value
            }))} placeholder="예: name@example.com" placeholderTextColor={colors.placeholder} keyboardType="email-address" autoCapitalize="none" style={styles.textInput} />
              </View> : null}

            <View style={styles.modalButtons}>
              <Pressable style={[styles.modalButton, styles.cancelButton, profileSaving && styles.disabledButton]} onPress={profileEditorMode === "edit" ? closeProfileEditor : handleLogout} disabled={profileSaving}>
                <Text style={styles.cancelButtonText}>{profileEditorMode === "edit" ? "취소" : "로그아웃"}</Text>
              </Pressable>
              <Pressable style={[styles.modalButton, styles.submitButton, (!profileFormComplete || profileSaving) && styles.disabledButton]} onPress={handleSaveProfile} disabled={!profileFormComplete || profileSaving}>
                <Text style={styles.submitButtonText}>{profileSaving ? "\uc800\uc7a5 \uc911..." : profileEditorMode === "edit" ? "저장" : "확인"}</Text>
              </Pressable>
            </View>
          </View>
          </ScrollView>
        </View>
      </Modal>;
}
