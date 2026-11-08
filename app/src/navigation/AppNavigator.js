import { useTheme } from "../shared/theme/ThemeProvider";
import { useAppController } from "./useAppController";
import { LoadingScreen } from "../shared/ui/LoadingScreen";
import { LoginScreen } from "../features/auth/LoginScreen";
import { FeedbackModals } from "../shared/ui/FeedbackModals";
import { KeyboardAvoidingView, SafeAreaView, StatusBar, View, Text, Platform } from "react-native";
import { StatusBar as ExpoStatusBar } from "expo-status-bar";
import { AppHeader } from "../shared/ui/AppHeader";
import { HomeScreen } from "../features/home/HomeScreen";
import { ChatScreen } from "../features/chat/ChatScreen";
import { useGroundedChat } from "../features/chat/useGroundedChat";
import { useChatDisplayMode } from "../features/chat/useChatDisplayMode";
import { CitationOriginalViewer } from "../features/chat/CitationOriginalViewer";
import { StorageScreen } from "../features/records/StorageScreen";
import { getRuntimeAccessToken, getRuntimeApiBaseUrl, fetchRecord, getReadableErrorMessage } from "../shared/api/runtime";
import { MyPageScreen } from "../features/profile/MyPageScreen";
import { RecordMediaViewer } from "../features/records/RecordMediaViewer";
import { RecordDetailViewer } from "../features/records/RecordDetailViewer";
import { RecordManagementModal } from "../features/records/RecordManagementModal";
const { mergeRecordDetails } = require("../features/records/recordManagement.cjs");
import { useRecordReadState } from "../features/records/useRecordReadState";
import { BottomTabs } from "../shared/ui/BottomTabs";
import { ProfileEditor } from "../features/profile/ProfileEditor";
import { RecordComposer } from "../features/records/RecordComposer";
import { UploadProgress } from "../features/records/UploadProgress";
import React, { useEffect, useMemo, useRef, useState } from "react";
const { resolveProfileLoginConfig, createProfileLogin } = require("../features/auth/profileLogin.cjs");
const profileLoginConfig = resolveProfileLoginConfig({
  enabled: process.env.EXPO_PUBLIC_ENABLE_PROFILE_LOGIN,
  apiBaseUrl: Platform.OS === "web"
    ? process.env.EXPO_PUBLIC_WEB_API_BASE_URL || process.env.EXPO_PUBLIC_PROFILE_LOGIN_API_BASE_URL
    : process.env.EXPO_PUBLIC_PROFILE_LOGIN_API_BASE_URL,
  profiles: [
    { id: "F1", name: "구성원 1", username: process.env.EXPO_PUBLIC_PROFILE_F1_USERNAME, password: process.env.EXPO_PUBLIC_PROFILE_F1_PASSWORD },
    { id: "F2", name: "구성원 2", username: process.env.EXPO_PUBLIC_PROFILE_F2_USERNAME, password: process.env.EXPO_PUBLIC_PROFILE_F2_PASSWORD },
    { id: "F3", name: "구성원 3", username: process.env.EXPO_PUBLIC_PROFILE_F3_USERNAME, password: process.env.EXPO_PUBLIC_PROFILE_F3_PASSWORD },
    { id: "F4", name: "구성원 4", username: process.env.EXPO_PUBLIC_PROFILE_F4_USERNAME, password: process.env.EXPO_PUBLIC_PROFILE_F4_PASSWORD },
  ],
});
// Only display fields reach the screen; credentials stay in the login adapter.
const loginProfiles = profileLoginConfig?.profiles.map(({ id, name, username }) => ({ id, name, username })) || [];

export default function AppNavigator() {
  const { styles, dark } = useTheme();
  const {
    refreshRecords,
    formVisibility,
    setFormVisibility,
    selectedAsset,
    storageLoaded,
    sessionRestoreLoading,
    workspaceBootstrapLoading,
    authLoadingMessage,
    user,
    authLoading,
    bottomInset,
    handleCredentialLogin,
    handleCredentialSignup,
    handleSaveApiBaseUrl,
    showPopupResult,
    popupResult,
    popupLoading,
    popupConfirm,
    closePopupConfirm,
    handlePopupCancel,
    handlePopupConfirm,
    closePopupResult,
    activeFamily,
    setActiveTab,
    workspaceError,
    activeTab,
    records,
    handleUploadPress,
    setStoragePreviewType,
    storagePreviewType,
    groupedRecords,
    hasPendingOcrReview,
    hasPendingMediaUpload,
    onRetryMediaUpload,
    onReopenOcrReview,
    handleOpenMedia,
    openDeleteRecordConfirm,
    familyRooms,
    handleCreateFamily,
    prepareJoinFamily,
    handleJoinFamily,
    handleLogout,
    handleDeleteAccount,
    openProfileEditor,
    handleDeleteFamily,
    handleSelectFamily,
    viewedRecord,
    setViewingMediaId,
    profileModalVisible,
    profileEditorMode,
    closeProfileEditor,
    keyboardInset,
    profileDraft,
    setProfileDraft,
    profileSaving,
    profileFormComplete,
    handleSaveProfile,
    modalVisible,
    closeUploadModal,
    selectedTypeInfo,
    formTitle,
    setFormTitle,
    formDetail,
    setFormDetail,
    formTags,
    setFormTags,
    uploadSaving,
    handleAddRecord,
    uploadSavingMessage
  } = useAppController();
  const chat = useGroundedChat({ userId: user?.id, roomId: activeFamily?.id });
  const chatDisplay = useChatDisplayMode(__DEV__);
  const readState = useRecordReadState({ baseUrl: getRuntimeApiBaseUrl(), userId: user?.id, roomId: activeFamily?.id });
  const [openedRecord, setOpenedRecord] = useState(null);
  const [managedRecord, setManagedRecord] = useState(null);
  useEffect(() => { setOpenedRecord(null); setManagedRecord(null); }, [readState.scopeKey]);
  const loginHandlers = useRef(null);
  loginHandlers.current = { handleCredentialLogin, handleSaveApiBaseUrl, closePopupResult, showPopupResult };
  const profileLogin = useMemo(() => {
    const login = createProfileLogin({ config: profileLoginConfig, getApiBaseUrl: getRuntimeApiBaseUrl,
      selectApiBaseUrl: async baseUrl => {
        await loginHandlers.current.handleSaveApiBaseUrl(baseUrl);
        loginHandlers.current.closePopupResult();
      },
      onLogin: credentials => loginHandlers.current.handleCredentialLogin(credentials) });
    return async profileId => {
      try { return await login(profileId); }
      catch (error) {
        if (["PROFILE_LOGIN_NOT_CONFIGURED", "PROFILE_LOGIN_API_MISMATCH", "PROFILE_LOGIN_UNKNOWN_PROFILE"].includes(error?.code)) {
          loginHandlers.current.showPopupResult("프로필 로그인 설정 확인", error.message, { variant: "error" });
        }
        throw error;
      }
    };
  }, []);
  const displayRecords = records.map(record => ({ ...record, isUnread: readState.isUnread(record.id) }));
  const displayGroups = groupedRecords.map(group => ({ ...group, items: group.items.map(record => ({ ...record, isUnread: readState.isUnread(record.id) })) }));
  const recordReadContext = useRef(null);
  recordReadContext.current = readState.scopeKey;
  const recordReadSequence = useRef(0);
  useEffect(() => () => { recordReadSequence.current += 1; }, []);
  const selectedRecord = openedRecord?.scopeKey === readState.scopeKey && records.some(record => record.id === openedRecord.id)
    ? openedRecord.record : null;
  const openRecord = async record => {
    if (!readState.scopeKey || !records.some(current => current.id === record.id)) return;
    const sequence = ++recordReadSequence.current, scope = readState.scopeKey, token = getRuntimeAccessToken();
    const current = () => sequence === recordReadSequence.current && scope === recordReadContext.current && token === getRuntimeAccessToken();
    try {
      const detail = await fetchRecord(record.id);
      if (!current()) return;
      if (detail.record_id !== record.id || detail.capabilities?.can_read !== true) throw new Error("기록을 읽을 수 없습니다.");
      const resolved = mergeRecordDetails(record, detail);
      setOpenedRecord({ id: record.id, scopeKey: scope, record: resolved });
      if (record.hasFile && ["image", "video", "voice"].includes(record.type)) handleOpenMedia(record);
    } catch (error) {
      if (current()) showPopupResult("기록 열람 실패", getReadableErrorMessage(error, "기록 접근 권한과 서버 연결을 확인해주세요."), { variant: "error" });
    }
  };
  if (!storageLoaded || sessionRestoreLoading || workspaceBootstrapLoading) {
    return <LoadingScreen message={authLoadingMessage} />;
  }
  if (!user) {
    return <>
        <LoginScreen authLoading={authLoading} authLoadingMessage={authLoadingMessage} bottomInset={bottomInset} onLogin={handleCredentialLogin} onSignup={handleCredentialSignup} onShowPopupResult={showPopupResult} loginProfiles={loginProfiles} onProfileLogin={profileLoginConfig ? profileLogin : undefined} />
        <FeedbackModals popupResult={popupResult} popupLoading={popupLoading} popupConfirm={popupConfirm} closePopupConfirm={closePopupConfirm} handlePopupCancel={handlePopupCancel} handlePopupConfirm={handlePopupConfirm} closePopupResult={closePopupResult} />
      </>;
  }
  const avoidAndroidKeyboard = Platform.OS === "android" && activeTab === "chat" && keyboardInset > 0;
  // RN Android hide coordinates can retain system-bar overlap. Drop avoidance
  // entirely when closed/blurred; padding also avoids height's cached initial frame.
  return <KeyboardAvoidingView style={styles.appFrame} behavior={avoidAndroidKeyboard ? "padding" : undefined}
    enabled={avoidAndroidKeyboard} keyboardVerticalOffset={0}>
    <SafeAreaView style={styles.safeArea}>
      <ExpoStatusBar style={dark ? "light" : "dark"} />
      <StatusBar barStyle={dark ? "light-content" : "dark-content"} />

      <View style={styles.appFrame}>
        <AppHeader activeFamily={activeFamily} onManageFamily={() => setActiveTab("mypage")} />

        {workspaceError ? <View style={styles.workspaceNotice}>
            <Text style={styles.workspaceNoticeText}>
              {workspaceError}
            </Text>
          </View> : null}
        {readState.error ? <View style={styles.workspaceNotice}><Text accessibilityRole="alert" style={styles.workspaceNoticeText}>{readState.error}</Text></View> : null}

        <View style={styles.content}>
          {activeTab === "home" && <HomeScreen records={displayRecords} mediaToken={getRuntimeAccessToken()} mediaBaseUrl={getRuntimeApiBaseUrl()} onOpenRecord={openRecord} onUploadPress={handleUploadPress} bottomInset={bottomInset} onOpenRecords={type => {
          setStoragePreviewType(type);
          setActiveTab("storage");
        }} />}
          {activeTab === "chat" && <ChatScreen {...chat} serviceMode={chatDisplay.mode === "service"} />}
          {activeTab === "storage" && <StorageScreen key={readState.scopeKey} initialType={storagePreviewType} groupedRecords={displayGroups} onManageRecord={record => setManagedRecord({ record, scopeKey: readState.scopeKey })} onRefresh={refreshRecords} hasPendingMediaUpload={hasPendingMediaUpload} onRetryMediaUpload={onRetryMediaUpload} hasPendingOcrReview={hasPendingOcrReview} onReopenOcrReview={onReopenOcrReview} mediaToken={getRuntimeAccessToken()} mediaBaseUrl={getRuntimeApiBaseUrl()} bottomInset={bottomInset} onViewMedia={handleOpenMedia} onOpenRecord={openRecord} onDeleteRecord={openDeleteRecordConfirm} />}
          {activeTab === "mypage" && <MyPageScreen chatDisplay={__DEV__ ? chatDisplay : null} user={user} familyRooms={familyRooms} activeFamily={activeFamily} bottomInset={bottomInset} onCreateFamily={handleCreateFamily} onPrepareJoinFamily={prepareJoinFamily} onJoinFamily={handleJoinFamily} onLogout={handleLogout} onDeleteAccount={null} onOpenProfileEditor={openProfileEditor} onDeleteFamily={null} onSelectFamily={handleSelectFamily} />}
        </View>

        {viewedRecord && selectedRecord?.id === viewedRecord.id ? <RecordMediaViewer key={`${readState.scopeKey}:${viewedRecord.id}`} setViewingMediaId={value => { setViewingMediaId(value); if (!value) setOpenedRecord(null); }} viewedRecord={viewedRecord} onViewed={readState.markRead} /> : null}
        {selectedRecord && !(selectedRecord.hasFile && ["image", "video", "voice"].includes(selectedRecord.type)) ? <RecordDetailViewer key={`${readState.scopeKey}:${selectedRecord.id}`} record={selectedRecord} onClose={() => setOpenedRecord(null)} onViewed={readState.markRead} onAsk={() => { setOpenedRecord(null); setActiveTab("chat"); chat.onAskRecord(selectedRecord); }} onManage={() => { setOpenedRecord(null); setManagedRecord({ record: selectedRecord, scopeKey: readState.scopeKey }); }} /> : null}
        {managedRecord?.scopeKey === readState.scopeKey && activeFamily ? <RecordManagementModal key={`${readState.scopeKey}:${managedRecord.record.id}`} record={managedRecord.record} roomId={activeFamily.id} onClose={() => setManagedRecord(null)} onChanged={refreshRecords} /> : null}
        {chat.original ? <CitationOriginalViewer key={chat.original.citation.fact_id} original={chat.original} onClose={chat.closeOriginal} serviceMode={chatDisplay.mode === "service"} /> : null}
        <BottomTabs activeTab={activeTab} bottomInset={Platform.OS === "android" && activeTab === "chat" && keyboardInset > 0 ? 0 : bottomInset} onChange={tab => {
        setStoragePreviewType("image");
        setActiveTab(tab);
      }} />
      </View>

      <ProfileEditor profileModalVisible={profileModalVisible} profileEditorMode={profileEditorMode} closeProfileEditor={closeProfileEditor} bottomInset={bottomInset} keyboardInset={keyboardInset} profileDraft={profileDraft} setProfileDraft={setProfileDraft} profileSaving={profileSaving} handleLogout={handleLogout} profileFormComplete={profileFormComplete} handleSaveProfile={handleSaveProfile} />

      <RecordComposer formVisibility={formVisibility} setFormVisibility={setFormVisibility} selectedAsset={selectedAsset} modalVisible={modalVisible} closeUploadModal={closeUploadModal} bottomInset={bottomInset} keyboardInset={keyboardInset} selectedTypeInfo={selectedTypeInfo} formTitle={formTitle} setFormTitle={setFormTitle} formDetail={formDetail} setFormDetail={setFormDetail} formTags={formTags} setFormTags={setFormTags} uploadSaving={uploadSaving} handleAddRecord={handleAddRecord} />

      <UploadProgress uploadSaving={uploadSaving} uploadSavingMessage={uploadSavingMessage} />
      <FeedbackModals popupResult={popupResult} popupLoading={popupLoading} popupConfirm={popupConfirm} closePopupConfirm={closePopupConfirm} handlePopupCancel={handlePopupCancel} handlePopupConfirm={handlePopupConfirm} closePopupResult={closePopupResult} />
    </SafeAreaView>
  </KeyboardAvoidingView>;
}
