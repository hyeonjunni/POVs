import { useTheme, useThemedStyles } from "../../shared/theme/ThemeProvider";
import React, { useMemo, useRef, useState } from "react";
import { SafeAreaView, StatusBar, View, Text, Pressable, StyleSheet } from "react-native";
import { AppHeader } from "../../shared/ui/AppHeader";
import { BottomTabs } from "../../shared/ui/BottomTabs";
import { FeedbackModals } from "../../shared/ui/FeedbackModals";
import { useAndroidBottomInset, useKeyboardInset } from "../../shared/ui/layout";
import { HomeScreen } from "../home/HomeScreen";
import { StorageScreen } from "../records/StorageScreen";
import { RecordComposer } from "../records/RecordComposer";
import { uploadTypes, parseTagInput, formatRecordDate } from "../records/recordHelpers";
import { ChatScreen } from "../chat/ChatScreen";
import { MyPageScreen } from "../profile/MyPageScreen";
import { ProfileEditor } from "../profile/ProfileEditor";
import { buildProfileDraft, isProfileFormComplete } from "../profile/utils";
const { createPreviewIdentity, createPreviewRecord } = require("./previewModel.cjs");

// This workspace owns only temporary UI state. It never creates an authenticated session.
export function PreviewWorkspace({ onExit }) {
  const { styles } = useTheme();
  const p = useThemedStyles(createStyles);
  const [identity] = useState(createPreviewIdentity);
  const [user, setUser] = useState(identity.user);
  const [activeTab, setActiveTab] = useState("home");
  const [storageType, setStorageType] = useState("image");
  const [records, setRecords] = useState([]);
  const [selectedType, setSelectedType] = useState(null);
  const [formTitle, setFormTitle] = useState("");
  const [formDetail, setFormDetail] = useState("");
  const [formTags, setFormTags] = useState("");
  const [profileOpen, setProfileOpen] = useState(false);
  const [profileDraft, setProfileDraft] = useState(() => buildProfileDraft(identity.user));
  const [popupResult, setPopupResult] = useState(null);
  const sequence = useRef(0);
  const bottomInset = useAndroidBottomInset();
  const keyboardInset = useKeyboardInset();
  const groupedRecords = useMemo(() => uploadTypes.map(type => ({ ...type, items: records.filter(record => record.type === type.key) })), [records]);
  const selectedTypeInfo = uploadTypes.find(type => type.key === selectedType);
  const activeFamily = { ...identity.family, members: [{ ...identity.family.members[0], name: user.name, email: user.email, age: user.age, gender: user.gender, phone: user.phone }] };

  function showNotice() {
    setPopupResult({ title: "테스트 화면", message: "가족방 관리와 실제 파일 업로드는 계정으로 로그인한 뒤 사용할 수 있습니다.", hint: "테스트 화면에서는 서버의 계정·가족방·파일을 변경하지 않습니다." });
    return false;
  }
  function closeComposer() {
    setSelectedType(null);
    setFormTitle("");
    setFormDetail("");
    setFormTags("");
  }
  function addRecord() {
    const record = createPreviewRecord({ type: selectedType, title: formTitle, detail: formDetail, tags: parseTagInput(formTags) }, sequence.current + 1, formatRecordDate(new Date()));
    if (!record) return;
    sequence.current += 1;
    setRecords(previous => [record, ...previous]);
    setStorageType(selectedType);
    closeComposer();
    setActiveTab("storage");
    setPopupResult({ title: "테스트 기록 추가", message: "이 테스트 화면에 임시로 기록을 추가했습니다.", hint: "실제 파일은 전송되지 않았으며, 테스트 종료 시 초기화됩니다." });
  }
  function saveProfile() {
    if (!isProfileFormComplete(profileDraft, "edit")) return;
    setUser(previous => ({ ...previous, ...profileDraft, age: Number.parseInt(profileDraft.age, 10) }));
    setProfileOpen(false);
  }
  function openRecords(type) {
    setStorageType(type);
    setActiveTab("storage");
  }

  return <SafeAreaView style={styles.safeArea}>
    <StatusBar barStyle="dark-content" />
    <View style={styles.appFrame}>
      <AppHeader activeFamily={activeFamily} onManageFamily={() => setActiveTab("mypage")} />
      <View style={p.banner}>
        <View style={{ flex: 1 }}><Text style={p.title}>테스트 모드</Text><Text style={p.description}>변경 사항은 테스트 종료 시 초기화됩니다.</Text></View>
        <Pressable accessibilityRole="button" accessibilityLabel="테스트 종료" onPress={onExit} style={p.exit}><Text style={p.exitText}>테스트 종료</Text></Pressable>
      </View>
      <View style={styles.content}>
        {activeTab === "home" && <HomeScreen records={records} bottomInset={bottomInset} onUploadPress={setSelectedType} onOpenRecords={openRecords} />}
        {activeTab === "storage" && <StorageScreen key={storageType} initialType={storageType} groupedRecords={groupedRecords} mediaToken="" mediaBaseUrl="" bottomInset={bottomInset} onViewMedia={showNotice} onDeleteRecord={item => setRecords(previous => previous.filter(record => record.id !== item.id))} />}
        {activeTab === "chat" && <ChatScreen />}
        {activeTab === "mypage" && <MyPageScreen user={user} familyRooms={[activeFamily]} activeFamily={activeFamily} bottomInset={bottomInset}
          onCreateFamily={showNotice} onPrepareJoinFamily={showNotice} onJoinFamily={showNotice}
          onDeleteFamily={showNotice} onDeleteAccount={showNotice} onSelectFamily={() => {}}
          onLogout={onExit} onOpenProfileEditor={() => { setProfileDraft(buildProfileDraft(user)); setProfileOpen(true); }} />}
      </View>
      <BottomTabs activeTab={activeTab} bottomInset={bottomInset} onChange={tab => { setStorageType("image"); setActiveTab(tab); }} />
    </View>
    <RecordComposer modalVisible={Boolean(selectedType)} closeUploadModal={closeComposer} bottomInset={bottomInset} keyboardInset={keyboardInset}
      selectedTypeInfo={selectedTypeInfo} formTitle={formTitle} setFormTitle={setFormTitle} formDetail={formDetail} setFormDetail={setFormDetail}
      formTags={formTags} setFormTags={setFormTags} uploadSaving={false} handleAddRecord={addRecord} />
    <ProfileEditor profileModalVisible={profileOpen} profileEditorMode="edit" closeProfileEditor={() => setProfileOpen(false)} bottomInset={bottomInset} keyboardInset={keyboardInset}
      descriptionOverride="테스트 프로필을 수정합니다. 변경 사항은 이 화면에서만 반영되고 테스트 종료 시 초기화됩니다."
      profileDraft={profileDraft} setProfileDraft={setProfileDraft} profileSaving={false} handleLogout={onExit}
      profileFormComplete={isProfileFormComplete(profileDraft, "edit")} handleSaveProfile={saveProfile} />
    <FeedbackModals popupResult={popupResult} closePopupResult={() => setPopupResult(null)} popupConfirm={null} popupLoading={null} closePopupConfirm={() => {}} handlePopupConfirm={() => {}} />
  </SafeAreaView>;
}

const createStyles = colors => StyleSheet.create({
  banner: { paddingHorizontal: 16, paddingVertical: 10, backgroundColor: colors.primarySoft, flexDirection: "row", gap: 12, alignItems: "center" },
  title: { color: colors.primaryStrong, fontWeight: "700", fontSize: 13 },
  description: { color: colors.muted, fontSize: 12, lineHeight: 18 },
  exit: { minHeight: 44, paddingHorizontal: 12, justifyContent: "center", borderRadius: 12, backgroundColor: colors.surface },
  exitText: { color: colors.primaryStrong, fontWeight: "600", fontSize: 13 },
});
