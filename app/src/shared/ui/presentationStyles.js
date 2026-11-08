import { StyleSheet } from "react-native";
export const createUiStyles = colors => StyleSheet.create({
  header: {
    minHeight: 60,
    paddingHorizontal: 20,
    paddingVertical: 8,
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "center",
    gap: 12,
    backgroundColor: colors.surface,
    borderBottomWidth: 1,
    borderBottomColor: colors.border
  },
  home: {
    flex: 1,
    backgroundColor: colors.background
  },
  linkButton: {
    minHeight: 44,
    minWidth: 64,
    alignItems: "center",
    justifyContent: "center",
    paddingHorizontal: 4
  },
  emptySection: {
    minHeight: 64,
    paddingHorizontal: 20,
    paddingVertical: 16,
    justifyContent: "center"
  },
  scrim: {
    flex: 1,
    justifyContent: "flex-end",
    backgroundColor: colors.scrim
  },
  sheetHeading: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    gap: 8
  },
  sheetTitle: {
    flex: 1,
    fontSize: 20,
    lineHeight: 28,
    fontWeight: "700",
    color: colors.text
  },
  iconButton: {
    minHeight: 44,
    minWidth: 44,
    alignItems: "center",
    justifyContent: "center"
  },
  flex: {
    flex: 1,
    minWidth: 0
  },
  tabBar: {
    flexDirection: "row",
    backgroundColor: colors.surface,
    borderTopWidth: 1,
    borderTopColor: colors.border,
    paddingTop: 8
  },
  tab: {
    flex: 1,
    minHeight: 52,
    alignItems: "center",
    justifyContent: "center",
    gap: 3
  },
  tabLabel: {
    color: colors.muted,
    fontSize: 12,
    lineHeight: 18
  },
  selectedTabLabel: {
    color: colors.primary,
    fontWeight: "700"
  },
  chatHeader: {
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
    minHeight: 60,
    paddingHorizontal: 20,
    paddingVertical: 12,
    borderBottomWidth: 1,
    borderBottomColor: colors.border
  },
  chatContext: {
    flex: 1,
    fontSize: 15,
    lineHeight: 22,
    fontWeight: "600",
    color: colors.text
  },
  readiness: {
    fontSize: 12,
    lineHeight: 18,
    color: colors.muted
  },
  composerArea: {
    padding: 16,
    gap: 8,
    backgroundColor: colors.background,
    borderTopWidth: 1,
    borderTopColor: colors.border
  },
  input: {
    flex: 1,
    minHeight: 44,
    paddingVertical: 10,
    paddingHorizontal: 8
  },
  disabledSend: {
    minHeight: 44,
    minWidth: 44,
    borderRadius: 12,
    backgroundColor: colors.primarySoft,
    alignItems: "center",
    justifyContent: "center",
    opacity: .55
  }
});
