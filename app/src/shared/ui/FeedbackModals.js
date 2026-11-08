import { useTheme } from "../theme/ThemeProvider";
import { Modal, View, Text, Pressable, ActivityIndicator } from "react-native";
import React from "react";
export function FeedbackModals({
  popupResult,
  popupLoading,
  popupConfirm,
  closePopupConfirm,
  handlePopupCancel,
  handlePopupConfirm,
  closePopupResult
}) {
  const { styles, colors } = useTheme();
  const resultVariant = popupResult?.variant || "success";
  const resultAccentColor = resultVariant === "error" ? colors.danger : colors.primary;
  const resultIconBackgroundColor = resultVariant === "error" ? colors.dangerSoft : colors.primarySoft;
  const resultIconText = resultVariant === "error" ? "!" : "✓";
  const loadingAccentColor = popupLoading?.accentColor === "#DC2626" ? colors.danger : colors.primary;
  const confirmIsDanger = popupConfirm?.variant === "danger";
  return <>
        <Modal transparent visible={Boolean(popupConfirm)} onRequestClose={closePopupConfirm}>
          <View style={styles.centerModalBackdrop}>
            <View style={styles.uploadLoadingSheet}>
              <View style={[styles.deleteConfirmIcon, !confirmIsDanger ? {
            backgroundColor: colors.primarySoft
          } : null]}>
                <Text style={[styles.deleteConfirmIconText, !confirmIsDanger ? {
              color: colors.primary
            } : null]}>
                  {confirmIsDanger ? "!" : "?"}
                </Text>
              </View>
              <Text style={styles.uploadLoadingTitle}>{popupConfirm?.title || "확인"}</Text>
              <Text style={styles.uploadLoadingDescription}>{popupConfirm?.message || "이 작업을 진행할까요?"}</Text>
              {popupConfirm?.hint ? <Text style={styles.uploadLoadingHint}>{popupConfirm.hint}</Text> : null}
              <View style={styles.resultButtonRow}>
                <Pressable style={styles.resultSecondaryButton} onPress={popupConfirm?.onCancel ? handlePopupCancel : closePopupConfirm}>
                  <Text style={styles.resultSecondaryButtonText}>{popupConfirm?.cancelLabel || "취소"}</Text>
                </Pressable>
                <Pressable style={confirmIsDanger ? styles.resultDangerButton : styles.uploadResultButton} onPress={handlePopupConfirm}>
                  <Text style={confirmIsDanger ? styles.resultDangerButtonText : styles.uploadResultButtonText}>
                    {popupConfirm?.confirmLabel || "확인"}
                  </Text>
                </Pressable>
              </View>
            </View>
          </View>
        </Modal>

        <Modal transparent visible={Boolean(popupLoading)} onRequestClose={() => {}}>
          <View style={styles.centerModalBackdrop}>
            <View style={styles.uploadLoadingSheet}>
              <ActivityIndicator size="large" color={loadingAccentColor} />
              <Text style={styles.uploadLoadingTitle}>{popupLoading?.title || "처리 중"}</Text>
              <Text style={styles.uploadLoadingDescription}>{popupLoading?.message || "잠시만 기다려주세요."}</Text>
              <Text style={styles.uploadLoadingHint}>
                {popupLoading?.hint || "작업이 완료되면 결과를 안내해드립니다."}
              </Text>
            </View>
          </View>
        </Modal>

        <Modal transparent visible={Boolean(popupResult)} onRequestClose={closePopupResult}>
          <View style={styles.centerModalBackdrop}>
            <View style={styles.uploadLoadingSheet}>
              <View style={[styles.uploadResultIcon, {
            backgroundColor: resultIconBackgroundColor
          }]}>
                <Text style={[styles.uploadResultIconText, {
              color: resultAccentColor
            }]}>
                  {resultIconText}
                </Text>
              </View>
              <Text style={styles.uploadLoadingTitle}>{popupResult?.title || "완료"}</Text>
              <Text style={styles.uploadLoadingDescription}>{popupResult?.message || "작업이 완료되었습니다."}</Text>
              <Text style={styles.uploadLoadingHint}>
                {popupResult?.hint || "계속 진행하려면 확인 버튼을 눌러주세요."}
              </Text>
              <Pressable style={styles.uploadResultButton} onPress={closePopupResult}>
                <Text style={styles.uploadResultButtonText}>{popupResult?.buttonLabel || "확인"}</Text>
              </Pressable>
            </View>
          </View>
        </Modal>
      </>;
}
