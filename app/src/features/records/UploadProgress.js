import { useTheme } from "../../shared/theme/ThemeProvider";
import { Modal, View, ActivityIndicator, Text } from "react-native";
import React from "react";
export function UploadProgress({
  uploadSaving,
  uploadSavingMessage
}) {
  const { styles, colors } = useTheme();
  return <Modal transparent visible={uploadSaving} onRequestClose={() => {}}>
        <View style={styles.centerModalBackdrop}>
          <View style={styles.uploadLoadingSheet}>
            <ActivityIndicator size="large" color={colors.primary} />
            <Text style={styles.uploadLoadingTitle}>업로드 중</Text>
            <Text style={styles.uploadLoadingDescription}>{uploadSavingMessage}</Text>
            <Text style={styles.uploadLoadingHint}>잠시만 기다려주세요. 업로드가 완료되면 결과를 안내해드립니다.</Text>
          </View>
        </View>
      </Modal>;
}
