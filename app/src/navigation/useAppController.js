import { useAndroidBottomInset, useKeyboardInset } from "../shared/ui/layout";
import { useState, useRef, useMemo, useEffect } from "react";
import { initialItems, uploadTypes, mapUploadToRecord, buildDefaultTagsForType, parseTagInput } from "../features/records/recordHelpers";
import { getRuntimeApiBaseUrl, runtimeApiClient, setRuntimeAccessToken, setRuntimeUnauthorizedHandler, getRuntimeAccessToken, setRuntimeApiBaseUrl, CONFIGURED_API_BASE_URL, getReadableErrorMessage, fetchUploads, fetchUserFamilies, fetchFamilyMembers, checkBackendHealth, updateUserProfile, familyRoomsApi, fetchFamilyJoinPreview, processingApi, uploadsApi, createUploadEntry, createNativeTextRecord, deleteUploadRecord } from "../shared/api/runtime";
import { buildProfileDraft, isProfileComplete, isProfileFormComplete } from "../features/profile/utils";
const {
  createAuthSession
} = require("../../authSession.cjs");
import AsyncStorage from "@react-native-async-storage/async-storage";
import { runtimeUnauthorizedHandler } from "../shared/api/runtime";
import { mapBackendUser } from "../features/auth/mappers";
import { STORAGE_KEYS } from "../shared/storage/keys";
const {
  loadWorkspacePreservingSession
} = require("../../workspaceBootstrap.cjs");
const {
  persistActiveRoomId,
  selectActiveRoomId
} = require("../../familyRooms.cjs");
import { mapFamilyRoom } from "../features/family/relations";
const {
  normalizeApiBaseUrl,
  createApiClient
} = require("../../apiClient.cjs");
import * as ImagePicker from "expo-image-picker";
import { pickVoiceFile, releaseVoiceFile } from "../features/records/voicePicker";
import { recordsApi, recordUploadsApi } from "../shared/api/runtime";
const { managementError } = require("../features/records/recordManagement.cjs");
const { pickMediaAsset } = require("../../mediaPicker.cjs");
import * as Crypto from "expo-crypto";
const {
  createUploadWithBinary, normalizeUploadAsset
} = require("../../uploads.cjs");
const {
  completeActivation,
  createActivationCoordinator,
  createActivationKey
} = require("../../activation.cjs");
const {
  createOcrReviewFlow
} = require("../../processing.cjs");

// State, effects and handlers are from the current cleanbase, not the legacy app core.
export function useAppController() {
  const bottomInset = useAndroidBottomInset();
  const keyboardInset = useKeyboardInset();
  const [activeTab, setActiveTab] = useState("home");
  const [storagePreviewType, setStoragePreviewType] = useState("image");
  const [records, setRecords] = useState(initialItems);
  const [pendingOcrJobs, setPendingOcrJobs] = useState({});
  const [pendingMediaUploads, updatePendingMediaUploads] = useState({});
  const pendingMediaUploadsRef = useRef({});
  function setPendingMediaUploads(update) {
    const previous = pendingMediaUploadsRef.current;
    const next = typeof update === "function" ? update(previous) : update;
    for (const [id, entry] of Object.entries(previous)) {
      if (entry.typeKey === "voice" && next[id]?.asset?.uri !== entry.asset?.uri) releaseVoiceFile(entry.asset);
    }
    pendingMediaUploadsRef.current = next;
    updatePendingMediaUploads(next);
  }
  useEffect(() => () => {
    for (const entry of Object.values(pendingMediaUploadsRef.current)) if (entry.typeKey === "voice") releaseVoiceFile(entry.asset);
  }, []);
  const mediaUploadPending = useRef(false);
  const textSavePending = useRef(false);
  const uncertainUploadRooms = useRef(new Set());
  const recordSessionGeneration = useRef(0);
  const popupActionPending = useRef(false);
  const recordSession = recordSessionGeneration.current;
  const recordSessionToken = getRuntimeAccessToken();
  const recordSessionBaseUrl = getRuntimeApiBaseUrl();
  const [viewingMediaId, setViewingMediaId] = useState(null);
  const [selectedType, setSelectedType] = useState(null);
  const [modalVisible, setModalVisible] = useState(false);
  const [formTitle, setFormTitle] = useState("");
  const [formDetail, setFormDetail] = useState("");
  const [formTags, setFormTags] = useState("");
  const [formVisibility, setFormVisibility] = useState("PRIVATE");
  const [selectedAsset, setSelectedAsset] = useState(null);
  const composerScope = useRef(null);
  const recordMutationKeys = useRef(new Map());
  const [familyRooms, setFamilyRooms] = useState([]);
  const [activeFamilyId, setActiveFamilyId] = useState(null);
  const [user, setUser] = useState(null);
  const [apiBaseUrl, setApiBaseUrl] = useState(getRuntimeApiBaseUrl());
  const [storageLoaded, setStorageLoaded] = useState(false);
  const [profileModalVisible, setProfileModalVisible] = useState(false);
  const [profileDraft, setProfileDraft] = useState(buildProfileDraft(null));
  const [profileEditorMode, setProfileEditorMode] = useState("complete");
  const [profileSaving, setProfileSaving] = useState(false);
  const [uploadSaving, setUploadSaving] = useState(false);
  const [uploadSavingMessage, setUploadSavingMessage] = useState("업로드 중입니다.");
  const [popupResult, setPopupResult] = useState(null);
  const [popupConfirm, setPopupConfirm] = useState(null);
  const [popupLoading, setPopupLoading] = useState(null);
  const [authLoading, setAuthLoading] = useState(false);
  const [authLoadingMessage, setAuthLoadingMessage] = useState("로그인 정보를 확인하고 있습니다.");
  const [sessionRestoreLoading, setSessionRestoreLoading] = useState(false);
  const [workspaceBootstrapLoading, setWorkspaceBootstrapLoading] = useState(false);
  const [workspaceError, setWorkspaceError] = useState(null);
  const latestUserRef = useRef(null);
  const profileModalVisibleRef = useRef(false);
  const profileSavingRef = useRef(false);
  const roomActivationRef = useRef(false);
  const activationCoordinator = useMemo(() => createActivationCoordinator({
    storage: AsyncStorage,
    createKey: () => createActivationKey(() => Crypto.getRandomBytesAsync(32))
  }), []);
  const ocrReviewFlow = useMemo(() => createOcrReviewFlow(processingApi), []);
  const authSession = useMemo(() => createAuthSession({
    client: {
      get: (...args) => runtimeApiClient.get(...args),
      post: (...args) => runtimeApiClient.post(...args)
    },
    storage: AsyncStorage,
    setAccessToken: setRuntimeAccessToken,
    mapUser: mapBackendUser
  }), []);
  useEffect(() => {
    setRuntimeUnauthorizedHandler(async requestToken => {
      if (!requestToken || requestToken !== getRuntimeAccessToken()) return;
      const activationCleared = activationCoordinator.clear();
      ocrReviewFlow.clear();
      recordSessionGeneration.current += 1;
      setPendingMediaUploads({});
      setUploadSaving(false);
      popupActionPending.current = false;
      setPendingOcrJobs({});
      uncertainUploadRooms.current.clear();
      setPopupConfirm(null);
      setPopupLoading(null);
      setPopupResult(null);
      setUser(null);
      setFamilyRooms([]);
      setActiveFamilyId(null);
      setRecords([]);
      try {
        await Promise.all([authSession.clear(), activationCleared]);
      } catch (_error) {}
    });
    return () => {
      activationCoordinator.invalidate();
      setRuntimeUnauthorizedHandler(null);
    };
  }, [activationCoordinator, authSession, ocrReviewFlow]);
  useEffect(() => {
    setViewingMediaId(null);
  }, [activeTab, activeFamilyId, user?.id]);
  const viewedRecord = records.find(record => record.id === viewingMediaId);
  const groupedRecords = useMemo(() => {
    return [...uploadTypes, { key: "event", label: "사건" }].map(type => ({
      ...type,
      items: records.filter(record => (record.displayType || record.type) === type.key)
    }));
  }, [records]);
  const activeFamily = useMemo(() => {
    return familyRooms.find(room => room.id === activeFamilyId) || null;
  }, [familyRooms, activeFamilyId]);
  useEffect(() => {
    // Drafts must not cross account, server or room boundaries.
    if (selectedType === "voice" && selectedAsset) releaseVoiceFile(selectedAsset);
    composerScope.current = null;
    recordMutationKeys.current.clear();
    setModalVisible(false); setSelectedType(null); setSelectedAsset(null);
    setFormTitle(""); setFormDetail(""); setFormTags(""); setFormVisibility("PRIVATE");
  }, [user?.id, activeFamilyId, apiBaseUrl]);
  useEffect(() => {
    async function loadPersistedData() {
      try {
        const [savedAccessToken, savedCurrentUser, savedApiBaseUrl, savedActiveFamilyId] = await Promise.all([AsyncStorage.getItem(STORAGE_KEYS.accessToken), AsyncStorage.getItem(STORAGE_KEYS.currentUser), AsyncStorage.getItem(STORAGE_KEYS.apiBaseUrl), AsyncStorage.getItem(STORAGE_KEYS.activeFamilyId)]);
        AsyncStorage.removeItem(STORAGE_KEYS.legacyUser);
        let cachedUser = null;
        if (savedCurrentUser) {
          cachedUser = JSON.parse(savedCurrentUser);
        }
        if (savedApiBaseUrl) {
          const normalizedApiBaseUrl = setRuntimeApiBaseUrl(savedApiBaseUrl);
          setApiBaseUrl(normalizedApiBaseUrl);
        } else {
          setApiBaseUrl(setRuntimeApiBaseUrl(CONFIGURED_API_BASE_URL));
        }
        if (savedAccessToken) {
          setSessionRestoreLoading(true);
          setAuthLoadingMessage("저장된 계정 정보를 확인하고 있습니다.");
          try {
            const mappedUser = await authSession.restore();
            setUser(mappedUser);
            await loadWorkspacePreservingSession(() => bootstrapWorkspaceForUser(mappedUser, savedActiveFamilyId), workspaceError => setWorkspaceError(getReadableErrorMessage(workspaceError, "가족 공간을 불러오지 못했습니다.")));
          } catch (_error) {
            setUser(null);
          } finally {
            setSessionRestoreLoading(false);
            setAuthLoadingMessage("로그인 정보를 확인하고 있습니다.");
          }
        } else {
          await activationCoordinator.clear();
          setUser(null);
        }
      } catch (_error) {
        showPopupResult("저장된 데이터 로드 실패", "로컬에 저장된 데이터를 불러오지 못했습니다.", {
          variant: "error"
        });
      } finally {
        setStorageLoaded(true);
      }
    }
    loadPersistedData();
  }, []);
  useEffect(() => {
    if (!storageLoaded) {
      return;
    }
    if (user) {
      AsyncStorage.setItem(STORAGE_KEYS.currentUser, JSON.stringify(user));
    } else {
      AsyncStorage.removeItem(STORAGE_KEYS.currentUser);
    }
  }, [user, storageLoaded]);
  useEffect(() => {
    if (!storageLoaded) {
      return;
    }
    const normalizedApiBaseUrl = setRuntimeApiBaseUrl(apiBaseUrl);
    if (normalizedApiBaseUrl !== apiBaseUrl) {
      setApiBaseUrl(normalizedApiBaseUrl);
      return;
    }
    AsyncStorage.setItem(STORAGE_KEYS.apiBaseUrl, normalizedApiBaseUrl);
  }, [apiBaseUrl, storageLoaded]);
  useEffect(() => {
    if (!storageLoaded || !user?.id) return;
    persistActiveRoomId(AsyncStorage, STORAGE_KEYS.activeFamilyId, activeFamilyId);
  }, [activeFamilyId, storageLoaded, user?.id]);
  useEffect(() => {
    latestUserRef.current = user;
  }, [user]);
  useEffect(() => {
    profileModalVisibleRef.current = profileModalVisible;
  }, [profileModalVisible]);
  useEffect(() => {
    profileSavingRef.current = profileSaving;
  }, [profileSaving]);
  useEffect(() => {
    if (!storageLoaded || sessionRestoreLoading) {
      return;
    }
    if (user && !isProfileComplete(user)) {
      setProfileEditorMode("complete");
      if (!profileModalVisibleRef.current || profileSavingRef.current) {
        setProfileDraft(buildProfileDraft(user));
      }
      setProfileModalVisible(true);
      return;
    }
    if (!profileModalVisibleRef.current || profileSavingRef.current) {
      setProfileEditorMode("complete");
      setProfileModalVisible(false);
    }
  }, [user, storageLoaded, sessionRestoreLoading]);
  useEffect(() => {
    if (activeTab !== "mypage" || !user || familyRooms.length === 0 || profileModalVisible) {
      return;
    }
    const syncRooms = async () => {
      try {
        await refreshFamilyRoomsFromBackend();
      } catch (_error) {}
    };
    syncRooms();
    const intervalId = setInterval(syncRooms, 5000);
    return () => {
      clearInterval(intervalId);
    };
  }, [activeTab, user, familyRooms.length, profileModalVisible]);
  useEffect(() => {
    if (!storageLoaded) {
      return;
    }
    if (!user?.id || !activeFamilyId) {
      setRecords([]);
      return;
    }
    let cancelled = false;
    const syncUploads = async () => {
      try {
        const uploads = await fetchUploads(activeFamilyId);
        if (cancelled) {
          return;
        }
        setRecords(uploads.map(mapUploadToRecord));
        setWorkspaceError(null);
      } catch (error) {
        if (!cancelled) {
          setWorkspaceError(getReadableErrorMessage(error, "가족 기록을 불러오지 못했습니다."));
        }
      }
    };
    syncUploads();
    const intervalId = activeTab === "home" || activeTab === "storage" ? setInterval(syncUploads, 5000) : null;
    return () => {
      cancelled = true;
      if (intervalId) clearInterval(intervalId);
    };
  }, [activeTab, user?.id, activeFamilyId, storageLoaded]);
  function upsertFamilyRoom(nextRoom) {
    setFamilyRooms(prev => [nextRoom, ...prev.filter(room => room.id !== nextRoom.id)]);
  }
  function handleSelectFamily(roomId) {
    if (roomId === activeFamilyId) return;
    recordSessionGeneration.current += 1;
    setPendingMediaUploads({});
    setUploadSaving(false);
    ocrReviewFlow.clear();
    popupActionPending.current = false;
    setPopupConfirm(null);
    setPopupLoading(null);
    setPopupResult(null);
    setRecords([]);
    setActiveFamilyId(roomId);
  }
  async function fetchHydratedFamilyRoomsForUser(nextUser, preferredRoomId = null) {
    const rooms = await fetchUserFamilies();
    const hydratedRooms = await Promise.all(rooms.map(async room => {
      const members = await fetchFamilyMembers(room.room_id);
      return mapFamilyRoom(room, members, nextUser.id);
    }));
    const nextActiveFamilyId = selectActiveRoomId(hydratedRooms, preferredRoomId);
    return {
      hydratedRooms,
      nextActiveFamilyId
    };
  }
  async function bootstrapWorkspaceForUser(nextUser, preferredRoomId = activeFamilyId) {
    setWorkspaceBootstrapLoading(true);
    setAuthLoadingMessage("가족방과 저장소를 불러오고 있습니다.");
    try {
      const {
        hydratedRooms,
        nextActiveFamilyId
      } = await fetchHydratedFamilyRoomsForUser(nextUser, preferredRoomId);
      setFamilyRooms(hydratedRooms);
      setActiveFamilyId(nextActiveFamilyId);
      setWorkspaceError(null);
    } finally {
      setWorkspaceBootstrapLoading(false);
      setAuthLoadingMessage("로그인 정보를 확인하고 있습니다.");
    }
  }
  async function refreshFamilyRoomsFromBackend() {
    if (!user) {
      return;
    }
    const {
      hydratedRooms: refreshedRooms
    } = await fetchHydratedFamilyRoomsForUser(user, activeFamilyId);
    setFamilyRooms(refreshedRooms);
    setActiveFamilyId(prev => {
      if (prev && refreshedRooms.some(room => room.id === prev)) {
        return prev;
      }
      return refreshedRooms[0]?.id || null;
    });
  }
  async function refreshUploadsFromBackend(roomId = null, ensuredUserOverride = null) {
    const resolvedRoomId = roomId || activeFamilyId;
    const requestGeneration = recordSession;
    const requestToken = getRuntimeAccessToken();
    const requestBaseUrl = getRuntimeApiBaseUrl();
    const isCurrent = () => requestGeneration === recordSessionGeneration.current
      && requestToken === getRuntimeAccessToken() && requestBaseUrl === getRuntimeApiBaseUrl();
    if (!isCurrent() || resolvedRoomId !== activeFamilyId) return [];
    if (!user || !resolvedRoomId) {
      setRecords([]);
      return [];
    }
    const uploads = await fetchUploads(resolvedRoomId);
    if (!isCurrent()) return [];
    const mappedRecords = uploads.map(mapUploadToRecord);
    setRecords(mappedRecords);
    return mappedRecords;
  }
  async function handleCredentialLogin({
    username,
    password
  }) {
    let sessionEstablished = false;
    try {
      setAuthLoading(true);
      setAuthLoadingMessage("로그인 정보를 확인하고 있습니다.");
      const nextUser = await authSession.login({
        username,
        password
      });
      recordSessionGeneration.current += 1;
      setPendingMediaUploads({});
      setUploadSaving(false);
      await activationCoordinator.changeSession(JSON.stringify([getRuntimeApiBaseUrl(), nextUser.id]));
      sessionEstablished = true;
      setFamilyRooms([]);
      setActiveFamilyId(null);
      setRecords([]);
      setActiveTab("home");
      setUser(nextUser);
      await loadWorkspacePreservingSession(() => bootstrapWorkspaceForUser(nextUser), workspaceError => setWorkspaceError(getReadableErrorMessage(workspaceError, "가족 공간을 불러오지 못했습니다.")));
    } catch (error) {
      if (!sessionEstablished) {
        await authSession.clear();
        setUser(null);
      }
      showPopupResult(sessionEstablished ? "가족 공간을 불러오지 못했습니다" : "로그인 실패", getReadableErrorMessage(error, "아이디 또는 비밀번호를 확인해주세요."), {
        variant: "error"
      });
      throw error;
    } finally {
      setAuthLoading(false);
      setAuthLoadingMessage("로그인 정보를 확인하고 있습니다.");
    }
  }
  async function handleCredentialSignup({
    username,
    password,
    confirmPassword,
    name,
    email
  }) {
    if (password !== confirmPassword) {
      showPopupResult("회원가입 실패", "비밀번호 확인이 일치하지 않습니다.", {
        variant: "error"
      });
      return;
    }
    try {
      setAuthLoading(true);
      setAuthLoadingMessage("새 계정을 생성하고 있습니다.");
      const details = {
        username,
        password,
        name,
        email
      };
      const activation = await activationCoordinator.run({
        kind: "SIGNUP",
        payload: details,
        execute: idempotencyKey => completeActivation({
          idempotencyKey,
          submit: (headers, options) => authSession.signup(details, { ...options, headers }),
          getStatus: (operationId, headers, options) => runtimeApiClient.get(`/secure-operations/${encodeURIComponent(operationId)}`, { ...options, includeAuth: false, headers }),
          onState: status => setAuthLoadingMessage(status.state === "READY" ? "계정 활성화를 완료하고 있습니다." : "계정 보안 자격 증명을 준비하고 있습니다.")
        })
      });
      if (activation.state !== "COMMITTED" || !activation.value) {
        throw new Error(activation.state === "EXPIRED" ? "계정 활성화 요청이 만료되었습니다. 다시 시도해주세요." : "계정 활성화에 실패했습니다.");
      }
      await authSession.establish(activation.value);
      await activationCoordinator.clear();
      const nextUser = mapBackendUser(activation.value.user);
      setUser(nextUser);
      setFamilyRooms([]);
      setActiveFamilyId(null);
      setRecords([]);
      setActiveTab("home");
      await loadWorkspacePreservingSession(() => bootstrapWorkspaceForUser(nextUser), workspaceError => setWorkspaceError(getReadableErrorMessage(workspaceError, "가족 공간을 불러오지 못했습니다.")));
    } catch (error) {
      if (error?.code === "ACTIVATION_ABANDONED") return;
      await authSession.clear();
      setUser(null);
      showPopupResult("회원가입 실패", getReadableErrorMessage(error, "계정을 생성하지 못했습니다."), {
        variant: "error"
      });
      throw error;
    } finally {
      setAuthLoading(false);
      setAuthLoadingMessage("로그인 정보를 확인하고 있습니다.");
    }
  }
  async function handleSaveApiBaseUrl(nextValue) {
    const nextBaseUrl = normalizeApiBaseUrl(nextValue);
    if (nextBaseUrl !== getRuntimeApiBaseUrl()) {
      recordSessionGeneration.current += 1;
      ocrReviewFlow.clear();
      setPendingOcrJobs({}); setPendingMediaUploads({});
      setPopupConfirm(null); setPopupLoading(null); setUploadSaving(false);
      uncertainUploadRooms.current.clear();
    }
    const normalizedApiBaseUrl = setRuntimeApiBaseUrl(nextBaseUrl);
    setApiBaseUrl(normalizedApiBaseUrl);
    showPopupResult("테스트 서버 저장 완료", normalizedApiBaseUrl, {
      variant: "success",
      hint: "로그인과 가족방 동작은 이 서버 주소를 기준으로 진행됩니다."
    });
    return normalizedApiBaseUrl;
  }
  async function handleCheckBackendConnection(nextValue = null) {
    const candidateBaseUrl = normalizeApiBaseUrl(nextValue || apiBaseUrl);
    try {
      const result = await checkBackendHealth(candidateBaseUrl);
      if (result?.reachable === true) {
        showPopupResult("서버 연결 성공", `현재 테스트 서버\n${candidateBaseUrl}`, {
          variant: "success",
          hint: result.authenticated ? "로그인 세션이 유효합니다." : "서버에 연결됐습니다. 로그인 후 가족방과 업로드를 이용해주세요."
        });
        return true;
      }
      showPopupResult("서버 연결 실패", `응답은 왔지만 예상한 형식이 아닙니다.\n${candidateBaseUrl}`, {
        variant: "error"
      });
      return false;
    } catch (error) {
      showPopupResult("서버 연결 실패", getReadableErrorMessage(error, `백엔드 연결을 확인하지 못했습니다.\n${candidateBaseUrl}`), {
        variant: "error"
      });
      return false;
    }
  }
  function showPopupResult(title, message, options = {}) {
    setPopupResult({
      title,
      message,
      variant: options.variant || "success",
      hint: options.hint || (options.variant === "error" ? "입력 내용과 연결 상태를 다시 확인한 뒤 한 번 더 시도해주세요." : "이 작업 결과는 현재 화면과 연결된 데이터에 바로 반영됩니다."),
      buttonLabel: options.buttonLabel || "확인"
    });
  }
  function closePopupResult() {
    setPopupResult(null);
  }
  function openPopupConfirm(config) {
    setPopupConfirm({
      confirmLabel: "확인",
      cancelLabel: "취소",
      variant: "danger",
      ...config
    });
  }
  function closePopupConfirm() {
    if (popupLoading) {
      return;
    }
    setPopupConfirm(null);
  }
  async function handlePopupCancel() {
    if (popupActionPending.current) return;
    if (!popupConfirm?.onCancel || popupLoading) return closePopupConfirm();
    popupActionPending.current = true;
    const currentConfirm = popupConfirm;
    setPopupConfirm(null);
    setPopupLoading({
      title: currentConfirm.cancelLoadingTitle || "처리 중",
      message: currentConfirm.cancelLoadingMessage || "서버의 최신 상태를 반영하고 있습니다.",
      accentColor: "#DC2626"
    });
    try {
      await currentConfirm.onCancel();
    } catch (error) {
      if (recordSession !== recordSessionGeneration.current) return;
      showPopupResult(currentConfirm.errorTitle || "처리 실패", getReadableErrorMessage(error, currentConfirm.errorMessage || "요청을 처리하지 못했습니다."), { variant: "error" });
    } finally {
      if (recordSession === recordSessionGeneration.current) {
        popupActionPending.current = false;
        setPopupLoading(null);
      }
    }
  }
  async function handlePopupConfirm() {
    if (!popupConfirm?.onConfirm || popupActionPending.current) {
      return;
    }
    popupActionPending.current = true;
    const currentConfirm = popupConfirm;
    setPopupConfirm(null);
    if (currentConfirm.loadingTitle || currentConfirm.loadingMessage) {
      setPopupLoading({
        title: currentConfirm.loadingTitle || "처리 중",
        message: currentConfirm.loadingMessage || "잠시만 기다려주세요.",
        accentColor: currentConfirm.variant === "danger" ? "#DC2626" : "#2563EB"
      });
    }
    try {
      await currentConfirm.onConfirm();
    } catch (error) {
      if (recordSession !== recordSessionGeneration.current) return;
      showPopupResult(currentConfirm.errorTitle || "처리 실패", getReadableErrorMessage(error, currentConfirm.errorMessage || "요청을 처리하지 못했습니다."), {
        variant: "error"
      });
    } finally {
      if (recordSession === recordSessionGeneration.current) {
        popupActionPending.current = false;
        setPopupLoading(null);
      }
    }
  }
  async function handleLogout() {
    popupActionPending.current = false;
    setViewingMediaId(null);
    latestUserRef.current = null;
    const activationCleared = activationCoordinator.clear();
    ocrReviewFlow.clear();
    recordSessionGeneration.current += 1;
    setPendingMediaUploads({});
    setUploadSaving(false);
    setPendingOcrJobs({});
    uncertainUploadRooms.current.clear();
    setPopupConfirm(null);
    setPopupLoading(null);
    setPopupResult(null);
    try {
      await Promise.all([authSession.clear(), activationCleared]);
      await AsyncStorage.multiRemove([STORAGE_KEYS.familyRooms, STORAGE_KEYS.activeFamilyId]);
    } catch (_error) {}
    setUser(null);
    setFamilyRooms([]);
    setActiveFamilyId(null);
    setRecords([]);
    setProfileModalVisible(false);
    setProfileEditorMode("complete");
    setProfileDraft(buildProfileDraft(null));
    setActiveTab("home");
  }
  function openProfileEditor() {
    setProfileEditorMode("edit");
    setProfileDraft(buildProfileDraft(user));
    setProfileModalVisible(true);
  }
  function closeProfileEditor() {
    if (profileSaving) {
      return;
    }
    setProfileDraft(buildProfileDraft(user));
    setProfileEditorMode("complete");
    setProfileModalVisible(false);
  }
  async function handleSaveProfile() {
    const cleanName = profileDraft.name.trim() || user?.name || "";
    const cleanEmail = profileDraft.email.trim() || user?.email || "";
    const cleanPhone = profileDraft.phone.trim();
    const cleanGender = profileDraft.gender.trim();
    const normalizedAge = profileDraft.age.trim() ? Number(profileDraft.age.trim()) : null;
    if (!user?.id) {
      showPopupResult("저장 실패", "로그인 정보가 확인되지 않아 개인정보를 저장할 수 없습니다.", {
        variant: "error"
      });
      return;
    }
    if (profileEditorMode === "edit" && !cleanName) {
      showPopupResult("이름 입력 필요", "가족 멤버 상세정보에 표시할 이름을 입력해주세요.", {
        variant: "error"
      });
      return;
    }
    if (profileEditorMode === "edit" && !cleanEmail) {
      showPopupResult("이메일 입력 필요", "연락 가능한 이메일을 입력해주세요.", {
        variant: "error"
      });
      return;
    }
    if (!Number.isSafeInteger(normalizedAge) || normalizedAge <= 0) {
      showPopupResult("나이 입력 오류", "나이는 1 이상의 숫자로 입력해주세요.", {
        variant: "error"
      });
      return;
    }
    setProfileSaving(true);
    try {
      const updatedProfile = await updateUserProfile({
        name: cleanName,
        age: normalizedAge,
        gender: cleanGender,
        phone: cleanPhone,
        email: cleanEmail || user.email || ""
      });
      const nextUser = {
        ...user,
        name: updatedProfile.name || cleanName,
        username: updatedProfile.username || user.username || "",
        email: updatedProfile.email || cleanEmail || user.email || "",
        age: updatedProfile.age ?? normalizedAge,
        gender: updatedProfile.gender || cleanGender || null,
        phone: updatedProfile.phone || cleanPhone || null,
        profileChunk: updatedProfile.profile_chunk || null
      };
      setUser(nextUser);
      setProfileDraft(buildProfileDraft(nextUser));
      setProfileEditorMode("complete");
      setProfileModalVisible(false);
      showPopupResult(profileEditorMode === "edit" ? "내 정보 수정 완료" : "개인정보 입력 완료", profileEditorMode === "edit" ? "내 정보가 수정되었습니다." : "개인정보가 입력되었습니다.");
    } catch (error) {
      showPopupResult("개인정보 저장 실패", getReadableErrorMessage(error, "개인정보를 저장하지 못했습니다."), {
        variant: "error"
      });
    } finally {
      setProfileSaving(false);
    }
  }
  async function handleDeleteAccount() {
    showPopupResult("회원탈퇴 미지원", "현재 서버는 회원탈퇴를 지원하지 않습니다. 로그아웃은 이용할 수 있습니다.", { variant: "error" });
  }
  async function handleCreateFamily(familyName) {
    if (!user?.id || !getRuntimeAccessToken()) return false;
    const cleanName = familyName.trim();
    if (!cleanName) {
      showPopupResult("가족방 이름 필요", "생성할 가족방 이름을 입력해주세요.", {
        variant: "error"
      });
      return false;
    }
    if ([...cleanName].length > 200) {
      showPopupResult("가족방 이름 확인", "가족방 이름은 200자 이하로 입력해주세요.", { variant: "error" });
      return false;
    }
    if (roomActivationRef.current) return false;
    roomActivationRef.current = true;
    const requestToken = getRuntimeAccessToken();
    const requestBaseUrl = getRuntimeApiBaseUrl();
    const requestGeneration = recordSessionGeneration.current;
    const isCurrent = () => requestGeneration === recordSessionGeneration.current
      && requestToken === getRuntimeAccessToken() && requestBaseUrl === getRuntimeApiBaseUrl();
    const roomClient = createApiClient({
      baseUrl: requestBaseUrl,
      getAccessToken: () => requestToken,
      onUnauthorized: token => runtimeUnauthorizedHandler(token)
    });
    try {
      const payload = {
        name: cleanName
      };
      const activation = await activationCoordinator.run({
        kind: "ROOM_CREATE",
        payload,
        scope: JSON.stringify([requestBaseUrl, user.id]),
        isCurrent,
        execute: (idempotencyKey, context) => completeActivation({
          idempotencyKey,
          ...context,
          submit: (headers, options) => roomClient.post("/families", { ...options, headers, json: context.payload }),
          getStatus: (operationId, headers, options) => roomClient.get(`/secure-operations/${encodeURIComponent(operationId)}`, { ...options, headers }),
          onState: async status => {
            await context.onState(status);
            setPopupLoading({
              title: "가족방 활성화 중",
              message: status.state === "READY" ? "가족방 생성을 완료하고 있습니다." : "가족방 보안 자격 증명을 준비하고 있습니다."
            });
          }
        })
      });
      if (!isCurrent()) return false;
      if (activation.state !== "COMMITTED" || !activation.value) {
        throw new Error(activation.state === "EXPIRED" ? "가족방 활성화 요청이 만료되었습니다. 다시 시도해주세요." : "가족방 활성화에 실패했습니다.");
      }
      const createdRoom = activation.value;
      const members = await fetchFamilyMembers(createdRoom.room_id);
      if (!isCurrent()) return false;
      const normalizedRoom = mapFamilyRoom(createdRoom, members, user.id);
      upsertFamilyRoom(normalizedRoom);
      handleSelectFamily(normalizedRoom.id);
      showPopupResult("가족방 생성 완료", "마이페이지에서 초대 코드를 복사해 가족에게 공유해 주세요.", {
        hint: `${normalizedRoom.name} 가족방이 준비되었습니다.`
      });
      return true;
    } catch (error) {
      if (!isCurrent() || error?.code === "ACTIVATION_ABANDONED") return false;
      showPopupResult("가족방 생성 실패", getReadableErrorMessage(error, "가족방을 생성하지 못했습니다."), {
        variant: "error"
      });
      return false;
    } finally {
      roomActivationRef.current = false;
      if (isCurrent()) setPopupLoading(null);
    }
  }
  async function prepareJoinFamily(inviteCode) {
    const cleanCode = inviteCode.trim().toUpperCase();
    if (!cleanCode) {
      showPopupResult("코드 입력 필요", "입장할 가족방 코드를 입력해주세요.", {
        variant: "error"
      });
      return null;
    }
    try {
      return await fetchFamilyJoinPreview(cleanCode);
    } catch (error) {
      showPopupResult("가족방 정보 확인 실패", getReadableErrorMessage(error, "가족방 정보를 불러오지 못했습니다."), {
        variant: "error"
      });
      return null;
    }
  }
  async function handleJoinFamily(inviteCode, relationSelection = null) {
    const cleanCode = inviteCode.trim().toUpperCase();
    if (!cleanCode) {
      showPopupResult("코드 입력 필요", "입장할 가족방 코드를 입력해주세요.", {
        variant: "error"
      });
      return false;
    }
    try {
      const joinedRoom = await familyRoomsApi.join({
        invite_code: cleanCode,
        related_to_user_id: relationSelection?.relatedToUserId || null,
        relation_to_related_user: relationSelection?.relationToRelatedUser || null
      });
      const members = await fetchFamilyMembers(joinedRoom.room_id);
      const normalizedRoom = mapFamilyRoom(joinedRoom, members, user.id);
      upsertFamilyRoom(normalizedRoom);
      handleSelectFamily(normalizedRoom.id);
      showPopupResult("입장 완료", `${normalizedRoom.name}에 입장했습니다.`);
      return true;
    } catch (error) {
      showPopupResult("가족방 입장 실패", getReadableErrorMessage(error, "가족방에 입장하지 못했습니다."), {
        variant: "error"
      });
      return false;
    }
  }
  function handleDeleteFamily() {
    showPopupResult("가족방 삭제 미지원", "현재 서버는 가족방 삭제를 지원하지 않습니다.", { variant: "error" });
  }
  async function handleUploadPress(typeKey) {
    if (!activeFamily?.id) {
      showPopupResult("가족방 선택 필요", "업로드를 시작하려면 먼저 가족방을 생성하거나 입장해주세요.", {
        variant: "error"
      });
      setActiveTab("mypage");
      return;
    }
    if (uncertainUploadRooms.current.has(activeFamily.id)) {
      showUncertainUpload(new Error("이전 업로드 결과를 먼저 확인해주세요."));
      return;
    }
    if (["image", "video", "voice"].includes(typeKey)) {
      await pickMediaFile(typeKey);
      return;
    }
    openUploadModal(typeKey);
  }
  function openUploadModal(typeKey, asset = null) {
    composerScope.current = { session: recordSession, token: getRuntimeAccessToken(), baseUrl: getRuntimeApiBaseUrl(), roomId: activeFamily.id };
    setSelectedType(typeKey);
    setSelectedAsset(asset);
    setFormVisibility(typeKey === "text" ? "PRIVATE" : "FAMILY");
    setFormTitle(asset?.fileName || "");
    setFormDetail("");
    setFormTags("");
    setModalVisible(true);
  }
  function showUploadResult(title, message) {
    showPopupResult(title, message, {
      variant: title.includes("실패") ? "error" : "success",
      hint: title === "삭제 완료" ? "복원 가능 기간은 삭제 후 30일입니다." : "기록의 ‘공개 범위·기록 관리’에서 공개 범위를 변경하고 내용 검토를 이어갈 수 있습니다."
    });
  }
  function openDeleteRecordConfirm(item) {
    if (item.capabilities?.can_soft_delete !== true) return;
    openPopupConfirm({
      title: "기록 삭제",
      message: "정말 삭제하시겠습니까?",
      hint: "기록이 목록에서 제외됩니다. 복원 가능 기간은 삭제 후 30일입니다.",
      confirmLabel: "삭제",
      variant: "danger",
      onConfirm: () => handleDeleteRecord(item)
    });
  }
  function assertCurrentRecordSession() {
    if (recordSession !== recordSessionGeneration.current || recordSessionToken !== getRuntimeAccessToken() || recordSessionBaseUrl !== getRuntimeApiBaseUrl()) {
      throw Object.assign(new Error("이전 세션의 요청이 취소되었습니다."), { code: "OCR_REVIEW_ABANDONED" });
    }
  }
  async function runOcrJob(jobId, initialStatus = "PENDING") {
    const timeoutMs = 120000;
    const startedAt = Date.now();
    if (initialStatus === "PENDING") {
      await processingApi.run(jobId, {
        timeoutMs
      });
    }
    assertCurrentRecordSession();
    const completedJob = await processingApi.poll(jobId, {
      timeoutMs: Math.max(1, timeoutMs - (Date.now() - startedAt))
    });
    assertCurrentRecordSession();
    if (completedJob.status === "FAILED") {
      throw new Error(completedJob.failure_message || "OCR 처리에 실패했습니다.");
    }
    return completedJob;
  }
  async function finishOcrJob(jobId) {
    const current = await processingApi.get(jobId);
    assertCurrentRecordSession();
    const completedJob = current.status === "SUCCEEDED" ? current : await runOcrJob(jobId, current.status);
    try {
      await refreshUploadsFromBackend(activeFamily.id);
    } catch (_error) {}
    await presentOcrReview(completedJob);
  }
  async function showResolvedOcrReview({ review, job, action }) {
    assertCurrentRecordSession();
    if (review.review_status === "confirmed") {
      if (action === "reject") {
        showUploadResult("OCR 승인 상태 확인", "이미 승인된 기록입니다. 저장소에서 OCR 결과를 다시 확인해주세요.");
        return;
      }
      if (review.user_confirmed !== true) {
        ocrReviewFlow.setContext(job);
        openOcrReviewReload(new Error("사용자 승인 상태를 확인하지 못했습니다."));
        return;
      }
      setPopupLoading({ title: "검색 준비 중", message: "검색할 수 있도록 기록을 준비하고 있습니다." });
      try {
        const result = await processingApi.index(job.upload_id || job.record_id, job.job_id);
        assertCurrentRecordSession();
        if (!(Number.isInteger(result?.chunk_count) && result.chunk_count > 0 &&
              Number.isInteger(result?.embedding_count) && result.embedding_count > 0)) {
          throw new Error("검색할 수 있는 기록이 준비되지 않았습니다.");
        }
      } catch (error) {
        assertCurrentRecordSession();
        // Every retry reloads the same authoritative review before publishing again.
        // A failed publication never reverses the completed approval.
        ocrReviewFlow.setContext(job);
        openPopupConfirm({
          title: "검색 준비 확인 필요",
          message: "OCR 승인은 완료되었습니다.\n검색 준비 중 오류가 발생했습니다.",
          hint: "승인된 같은 기록의 검색 준비를 다시 시도할 수 있습니다.",
          confirmLabel: "검색 준비 다시 시도", cancelLabel: "닫기", variant: "default",
          loadingTitle: "검색 준비 상태 확인 중", onConfirm: loadCurrentOcrReview
        });
        return;
      }
    } else if (review.review_status !== "rejected") {
      ocrReviewFlow.setContext(job);
      openOcrReviewReload(new Error("OCR 검토 상태를 확인하지 못했습니다."));
      return;
    }
    setPendingOcrJobs(previous => Object.fromEntries(Object.entries(previous).filter(([, entry]) => entry.job?.job_id !== job.job_id)));
    showUploadResult(
      review.review_status === "confirmed" ? "검색 준비 완료" : "OCR 거절 완료",
      review.review_status === "confirmed" ? "업로드 및 검색 준비가 완료되었습니다." : "추출된 텍스트를 가족 기록에서 제외했습니다."
    );
  }
  function openOcrReviewPrompt(job, errorHint = null) {
    openPopupConfirm({
      title: "OCR 결과 검토",
      message: job.result?.text || "추출된 텍스트를 확인하고 승인 또는 거절해주세요.",
      hint: errorHint || "승인과 거절은 최종 결정이며 반대 결정으로 다시 변경할 수 없습니다.",
      confirmLabel: "승인",
      cancelLabel: "거절",
      variant: "default",
      loadingTitle: "OCR 결과 승인 중",
      loadingMessage: "서버의 최신 검토 상태를 반영하고 있습니다.",
      cancelLoadingTitle: "OCR 결과 거절 중",
      onConfirm: () => submitOcrReview("approve"),
      onCancel: () => submitOcrReview("reject")
    });
  }
  function openOcrReviewReload(error) {
    const job = ocrReviewFlow.getContext()?.job;
    if (!job) return;
    openPopupConfirm({
      title: "OCR 검토 불러오기 실패",
      message: getReadableErrorMessage(error, "OCR 검토 상태를 불러오지 못했습니다."),
      hint: "같은 OCR 작업을 새로 만들지 않고 다시 불러옵니다.",
      confirmLabel: "다시 불러오기",
      cancelLabel: "닫기",
      variant: "default",
      loadingTitle: "OCR 검토 다시 불러오는 중",
      onConfirm: loadCurrentOcrReview
    });
  }
  async function loadCurrentOcrReview() {
    try {
      const current = await ocrReviewFlow.reload();
      if (current.review.review_status === "unreviewed") openOcrReviewPrompt(current.job);
      else await showResolvedOcrReview(current);
    } catch (error) {
      if (error?.code === "OCR_REVIEW_ABANDONED") return;
      openOcrReviewReload(error);
    }
  }
  async function submitOcrReview(action) {
    try {
      const resolved = await ocrReviewFlow.decide(action);
      if (resolved.review.review_status === "unreviewed") openOcrReviewPrompt(resolved.job);
      else await showResolvedOcrReview(resolved);
    } catch (error) {
      if (error?.code === "OCR_REVIEW_ABANDONED") return;
      const job = ocrReviewFlow.getContext()?.job;
      if (job) openOcrReviewReload(error);
    }
  }
  async function presentOcrReview(completedJob) {
    assertCurrentRecordSession();
    setPendingOcrJobs(previous => ({ ...previous, [completedJob.record_id]: { ...previous[completedJob.record_id], job: completedJob, roomId: completedJob.room_id || activeFamily?.id } }));
    try {
      const current = await ocrReviewFlow.start(completedJob);
      if (current.review.review_status === "unreviewed") openOcrReviewPrompt(current.job);
      else await showResolvedOcrReview(current);
    } catch (error) {
      if (error?.code === "OCR_REVIEW_ABANDONED") return;
      openOcrReviewReload(error);
    }
  }
  async function retryOcrJob(jobId) {
    const current = await processingApi.get(jobId);
    assertCurrentRecordSession();
    if (current.status === "SUCCEEDED") return presentOcrReview(current);
    if (current.status !== "FAILED") return finishOcrJob(jobId, current.status);
    await processingApi.retry(jobId, {
      timeoutMs: 10000
    });
    await finishOcrJob(jobId, "PENDING");
  }
  function hasPendingOcrReview(item) {
    return Boolean(activeFamily?.id && pendingOcrJobs[item.id]?.roomId === activeFamily.id);
  }
  async function onReopenOcrReview(item) {
    const entry = pendingOcrJobs[item.id];
    if (!entry || entry.roomId !== activeFamily?.id) return;
    setPopupLoading({ title: "OCR 결과 확인", message: "서버의 최신 상태를 불러오고 있습니다." });
    try {
      // The backend reuses this exact record/file/processor tuple after a lost create response.
      const job = entry.job || await processingApi.create(item.id, entry.payload);
      assertCurrentRecordSession();
      setPendingOcrJobs(previous => ({ ...previous, [item.id]: { ...entry, job } }));
      const current = await processingApi.get(job.job_id);
      assertCurrentRecordSession();
      if (current.status === "SUCCEEDED") await presentOcrReview({ ...current, upload_id: item.id, file_id: entry.payload?.upload_file_id });
      else if (current.status === "FAILED") openPopupConfirm({
        title: "OCR 처리 실패", message: current.failure_message || "같은 OCR 작업을 다시 시도할 수 있습니다.",
        confirmLabel: "OCR 다시 시도", variant: "default", loadingTitle: "OCR 재처리 중",
        onConfirm: () => retryOcrJob(job.job_id),
      });
      else await finishOcrJob(job.job_id, current.status);
    } catch (error) {
      if (error?.code === "OCR_REVIEW_ABANDONED") return;
      showPopupResult("OCR 상태 확인 필요", getReadableErrorMessage(error, "OCR 상태를 확인하지 못했습니다."), {
        variant: "error", hint: "저장소의 OCR 결과 확인에서 같은 작업을 다시 확인해주세요.",
      });
    } finally {
      if (recordSession === recordSessionGeneration.current) setPopupLoading(null);
    }
  }
  async function recoverUncertainUpload() {
    await refreshUploadsFromBackend(activeFamily.id);
    assertCurrentRecordSession();
    uncertainUploadRooms.current.delete(activeFamily.id);
    closeUploadModal();
    setActiveTab("storage");
    showPopupResult("업로드 상태 확인", "서버의 기록 목록을 새로 불러왔습니다. 업로드가 반영되었는지 확인해주세요.", {
      hint: "응답이 늦은 요청은 아직 처리 중일 수 있습니다. 같은 파일을 바로 다시 올리지 마세요.",
    });
  }
  function showUncertainUpload(error) {
    uncertainUploadRooms.current.add(activeFamily.id);
    openPopupConfirm({ title: "업로드 상태 확인 필요", message: getReadableErrorMessage(error, "업로드 결과를 확인하지 못했습니다."),
      hint: "서버에 저장되었을 수 있습니다. 다시 업로드하기 전에 기록 목록을 확인해주세요.",
      confirmLabel: "저장소 확인", cancelLabel: "닫기", variant: "default", loadingTitle: "업로드 상태 확인 중",
      onConfirm: recoverUncertainUpload,
    });
  }
  async function pickMediaFile(typeKey) {
    const asset = typeKey === "voice" ? await pickVoiceFile({
      onError: error => showPopupResult("음성 파일 선택 실패", managementError(error), { variant: "error" }),
    }) : await pickMediaAsset({
      imagePicker: ImagePicker, typeKey,
      onPermissionDenied: () => showPopupResult("권한이 필요합니다", "이미지와 영상을 업로드하려면 사진 보관함 접근 권한을 허용해야 합니다.", { variant: "error" }),
      onError: () => showPopupResult("파일 선택 실패", "파일을 선택하지 못했습니다. 잠시 후 다시 시도해주세요.", { variant: "error" }),
    });
    if (!asset || recordSession !== recordSessionGeneration.current) { if (typeKey === "voice" && asset) releaseVoiceFile(asset); return; }
    return openUploadModal(typeKey, asset);
  }
  function hasPendingMediaUpload(item) {
    return pendingMediaUploads[item.id]?.session === recordSession;
  }
  function onRetryMediaUpload(item) {
    const pending = pendingMediaUploads[item.id];
    if (!pending || pending.session !== recordSession || recordSession !== recordSessionGeneration.current) return;
    return uploadMediaAsset(pending.typeKey, pending.asset, pending.upload);
  }
  async function uploadMediaAsset(typeKey, asset, existingUpload = null) {
    if (mediaUploadPending.current || recordSession !== recordSessionGeneration.current) return;
    mediaUploadPending.current = true;
    const label = ({ image: "이미지", video: "영상", voice: "음성" })[typeKey];
    try {
      asset = normalizeUploadAsset(asset);
      const fileName = asset.fileName;
      const meta = [asset.width && asset.height ? `${asset.width} x ${asset.height}` : null, asset.duration ? `${Math.round(asset.duration / 1000)}초` : null, asset.mimeType].filter(Boolean).join(" / ");
      if (!activeFamily?.id) {
        throw new Error("활성 가족방이 없습니다.");
      }
      setUploadSaving(true);
      setUploadSavingMessage(`${label}를 업로드하고 있습니다.`);
      const {
        upload: uploadEntry,
        uploadedFile
      } = await createUploadWithBinary(recordUploadsApi, {
        room_id: activeFamily.id,
        type: typeKey,
        title: formTitle.trim() || fileName,
        description: formDetail.trim() || meta || `${label} 파일 업로드`,
        tags: parseTagInput(formTags).length ? parseTagInput(formTags) : buildDefaultTagsForType(typeKey),
        visibility: formVisibility
      }, asset, { upload: existingUpload, assertCurrent: assertCurrentRecordSession });
      assertCurrentRecordSession();
      setPendingMediaUploads(previous => Object.fromEntries(Object.entries(previous).filter(([id]) => id !== uploadEntry.upload_id)));
      let listRefreshFailed = false;
      try {
        await refreshUploadsFromBackend(activeFamily.id);
      } catch (_error) {
        listRefreshFailed = true;
      }
      assertCurrentRecordSession();
      setActiveTab("storage");
      setStoragePreviewType(typeKey);
      if (typeKey === "voice") releaseVoiceFile(asset);
      closeUploadModal();
      if (listRefreshFailed) {
        showPopupResult("업로드 완료 · 목록 갱신 필요", `${label} 파일은 저장됐지만 목록을 새로 불러오지 못했습니다. 연결 후 저장소를 다시 열어주세요.`, {
          variant: "error"
        });
      } else {
        showUploadResult("업로드 완료", `${label} 업로드 정보와 파일이 가족방에 저장되었습니다.`);
      }
    } catch (error) {
      if (error?.code === "OCR_REVIEW_ABANDONED" || recordSession !== recordSessionGeneration.current) return;
      if (error?.uploadStage === "binary") {
        setPendingMediaUploads(previous => ({ ...previous, [error.uploadId]: { session: recordSession, typeKey, asset, upload: error.upload } }));
        closeUploadModal({ retainAsset: true });
        try { await refreshUploadsFromBackend(activeFamily.id); } catch (_) {}
        if (recordSession !== recordSessionGeneration.current) return;
        setActiveTab("storage");
        showPopupResult("파일 업로드 미확인", `${label} 기록 정보는 저장됐지만 파일 저장을 확인하지 못했습니다. 저장소의 파일 다시 시도로 같은 기록을 확인하고 재전송할 수 있습니다.`, {
          variant: "error", hint: `파일 전송 단계${Number.isInteger(error.status) ? ` · HTTP ${error.status}` : ""} · ${error.uploadId}\n${getReadableErrorMessage(error, "파일 전송 실패")}`
        });
        return;
      }
      if (error?.code === "REQUEST_TIMEOUT") { showUncertainUpload(error); return; }
      showPopupResult("업로드 실패", getReadableErrorMessage(error, `${label} 업로드 정보를 저장하지 못했습니다.`), {
        variant: "error"
      });
    } finally {
      mediaUploadPending.current = false;
      if (recordSession === recordSessionGeneration.current) {
        setUploadSaving(false);
        setUploadSavingMessage("업로드 중입니다.");
      }
    }
  }
  function closeUploadModal(options = {}) {
    if (!options.retainAsset && selectedType === "voice" && selectedAsset && !Object.values(pendingMediaUploads).some(entry => entry.asset?.uri === selectedAsset.uri)) releaseVoiceFile(selectedAsset);
    composerScope.current = null;
    setModalVisible(false);
    setSelectedAsset(null);
    setSelectedType(null);
    setFormTitle("");
    setFormDetail("");
    setFormTags("");
  }
  function handleOpenMedia(item) {
    if (!item?.hasFile || !["image", "video", "voice"].includes(item.type)) {
      showPopupResult("파일 보기 불가", "표시할 수 있는 미디어 파일이 없습니다.", {
        variant: "error"
      });
      return;
    }
    setViewingMediaId(item.id);
  }
  async function handleAddRecord() {
    const scope = composerScope.current;
    if (!scope || scope.session !== recordSessionGeneration.current || scope.roomId !== activeFamily?.id || scope.token !== getRuntimeAccessToken() || scope.baseUrl !== getRuntimeApiBaseUrl()) { closeUploadModal(); return; }
    if (!selectedType || !formTitle.trim()) {
      return;
    }
    if (!activeFamily?.id) {
      showPopupResult("가족방 선택 필요", "업로드를 시작하려면 먼저 가족방을 생성하거나 입장해주세요.", {
        variant: "error"
      });
      setActiveTab("mypage");
      return;
    }
    if (uncertainUploadRooms.current.has(activeFamily.id)) {
      showUncertainUpload(new Error("이전 업로드 결과를 먼저 확인해주세요."));
      return;
    }
    if (["image", "video", "voice"].includes(selectedType)) {
      if (!selectedAsset) return;
      return uploadMediaAsset(selectedType, selectedAsset);
    }
    if (textSavePending.current) return;
    textSavePending.current = true;
    try {
      setUploadSaving(true);
      setUploadSavingMessage(`${selectedTypeInfo?.label || "기록"} 업로드 정보를 저장하고 있습니다.`);
      const createEntry = selectedType === "text" ? createNativeTextRecord : createUploadEntry;
      await createEntry({
        room_id: activeFamily.id,
        type: selectedType,
        title: formTitle.trim(),
        description: selectedType === "text" ? formDetail.trim() : formDetail.trim() || "추가 설명 없음",
        tags: parseTagInput(formTags).length > 0 ? parseTagInput(formTags) : buildDefaultTagsForType(selectedType),
        visibility: formVisibility
      }, assertCurrentRecordSession);
      await refreshUploadsFromBackend(activeFamily.id);
      assertCurrentRecordSession();
      closeUploadModal();
      setActiveTab("storage");
      setStoragePreviewType(selectedType);
      showUploadResult("업로드 완료", "업로드 정보가 가족방 기준으로 저장되었습니다.");
    } catch (error) {
      if (recordSession !== recordSessionGeneration.current) return;
      if (error?.savedRecordId) {
        closeUploadModal(); setActiveTab("storage"); setStoragePreviewType("text");
        await refreshUploadsFromBackend(activeFamily.id).catch(() => {});
        if (recordSession !== recordSessionGeneration.current) return;
        showPopupResult("기록 저장됨 · 원문 확인 필요", "기록 정보는 저장되었습니다. 같은 기록의 ‘공개 범위·기록 관리’에서 원문 저장을 이어갈 수 있습니다.", { variant: "error" });
        return;
      }
      if (error?.code === "REQUEST_TIMEOUT") { closeUploadModal(); showUncertainUpload(error); return; }
      showPopupResult("업로드 저장 실패", getReadableErrorMessage(error, "업로드 정보를 저장하지 못했습니다."), {
        variant: "error"
      });
    } finally {
      textSavePending.current = false;
      if (recordSession === recordSessionGeneration.current) { setUploadSaving(false); setUploadSavingMessage("업로드 중입니다."); }
    }
  }
  async function handleDeleteRecord(item) {
    if (!item?.id || !activeFamily?.id) {
      return;
    }
    try {
      setPopupLoading({
        title: "삭제 중",
        message: `${item.title || "기록"}을 삭제하고 있습니다.`,
        accentColor: "#DC2626"
      });
      assertCurrentRecordSession();
      if (item.capabilities?.can_soft_delete !== true) throw new Error("이 기록을 삭제할 권한이 없습니다.");
      const mutationKey = `${item.id}:delete:${item.record_version}`;
      if (!recordMutationKeys.current.has(mutationKey)) recordMutationKeys.current.set(mutationKey, Crypto.randomUUID());
      await recordsApi.transitionLifecycle(item.id, { action: "SOFT_DELETE", recordVersion: item.record_version,
        reason: "사용자가 기록 삭제를 요청함", idempotencyKey: recordMutationKeys.current.get(mutationKey) });
      assertCurrentRecordSession();
      await refreshUploadsFromBackend(activeFamily.id);
      assertCurrentRecordSession();
      showUploadResult("삭제 완료", "선택한 기록이 목록에서 제외되었습니다.");
    } catch (error) {
      if (recordSession !== recordSessionGeneration.current) return;
      if (error?.status === 412) await refreshUploadsFromBackend(activeFamily.id).catch(() => {});
      showPopupResult("기록 삭제 확인 필요", managementError(error), {
        variant: "error"
      });
    } finally {
      if (recordSession === recordSessionGeneration.current) setPopupLoading(null);
    }
  }
  const selectedTypeInfo = uploadTypes.find(item => item.key === selectedType);
  const profileFormComplete = isProfileFormComplete(profileDraft, profileEditorMode);
  return {
    refreshRecords: () => refreshUploadsFromBackend(activeFamily?.id),
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
  };
}
