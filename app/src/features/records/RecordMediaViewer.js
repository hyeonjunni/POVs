import { useTheme } from "../../shared/theme/ThemeProvider";
import { Modal, View, Pressable, Text } from "react-native";
import { AuthenticatedMedia } from "./RecordMedia";
import { getRuntimeAccessToken, getRuntimeApiBaseUrl } from "../../shared/api/runtime";
import React from "react";
export function RecordMediaViewer({
  setViewingMediaId,
  viewedRecord,
  onViewed
}) {
  const { styles } = useTheme();
  return <Modal transparent visible onRequestClose={() => setViewingMediaId(null)}>
          <View style={styles.centerModalBackdrop}><View style={styles.uploadLoadingSheet}>
            <Text style={styles.recordTitle}>{viewedRecord.title}</Text>
            <AuthenticatedMedia item={viewedRecord} token={getRuntimeAccessToken()} baseUrl={getRuntimeApiBaseUrl()} large onViewed={onViewed} />
            <Pressable accessibilityRole="button" style={styles.uploadResultButton} onPress={() => setViewingMediaId(null)}>
              <Text style={styles.uploadResultButtonText}>닫기</Text>
            </Pressable>
          </View></View>
        </Modal>;
}
