import { fetch as expoFetch } from "expo/fetch";
import { Platform } from "react-native";
// Reuse Expo's already-installed, locked filesystem dependency (no package change).
import { File, Paths } from "expo/node_modules/expo-file-system";
import { copyAsync } from "expo/node_modules/expo-file-system/legacy";
import { randomUUID } from "expo-crypto";
const { createMultipartTransport, readUploadBytes } = require("../../../multipartTransport.cjs");
const uploadFetch = createMultipartTransport({
  multipartFetch: expoFetch,
  readUriBytes: uri => readUploadBytes(uri, { File, cache: Paths.cache, copyAsync, randomUUID })
});
const {
  resolveApiBaseUrl,
  probeSession,
  createApiClient,
  normalizeApiBaseUrl
} = require("../../../apiClient.cjs");
const {
  createFamilyRoomsApi
} = require("../../../familyRooms.cjs");
const { createRecordsApi, createTextRecord } = require("../../../records.cjs");
const {
  createUploadsApi
} = require("../../../uploads.cjs");
const {
  createProcessingApi
} = require("../../../processing.cjs");

// Existing cleanbase runtime extracted unchanged; one shared client/token instance.
export const CONFIGURED_API_BASE_URL = resolveApiBaseUrl(Platform.OS === "web"
  ? process.env.EXPO_PUBLIC_WEB_API_BASE_URL || process.env.EXPO_PUBLIC_API_BASE_URL
  : process.env.EXPO_PUBLIC_API_BASE_URL);
export let runtimeApiBaseUrl = CONFIGURED_API_BASE_URL;
export let runtimeAccessToken = "";
export let runtimeUnauthorizedHandler = async () => {};
export let runtimeApiClient = createApiClient({
  fetchImpl: uploadFetch,
  baseUrl: runtimeApiBaseUrl,
  getAccessToken: () => runtimeAccessToken,
  onUnauthorized: requestToken => runtimeUnauthorizedHandler(requestToken)
});
export const familyRoomsApi = createFamilyRoomsApi({
  get: (...args) => runtimeApiClient.get(...args),
  post: (...args) => runtimeApiClient.post(...args),
  delete: (...args) => runtimeApiClient.delete(...args)
});
export const uploadsApi = createUploadsApi({
  get: (...args) => runtimeApiClient.get(...args),
  post: (...args) => runtimeApiClient.post(...args),
  delete: (...args) => runtimeApiClient.delete(...args)
});
export const processingApi = createProcessingApi({
  get: (...args) => runtimeApiClient.get(...args),
  post: (...args) => runtimeApiClient.post(...args)
});
export function setRuntimeApiBaseUrl(nextValue) {
  runtimeApiBaseUrl = normalizeApiBaseUrl(nextValue);
  runtimeApiClient = createApiClient({
    fetchImpl: uploadFetch,
    baseUrl: runtimeApiBaseUrl,
    getAccessToken: () => runtimeAccessToken,
    onUnauthorized: requestToken => runtimeUnauthorizedHandler(requestToken)
  });
  return runtimeApiBaseUrl;
}
export function getRuntimeApiBaseUrl() {
  return runtimeApiBaseUrl;
}
export function setRuntimeAccessToken(nextToken) {
  runtimeAccessToken = String(nextToken || "").trim();
  return runtimeAccessToken;
}
export function getRuntimeAccessToken() {
  return runtimeAccessToken;
}
export function setRuntimeUnauthorizedHandler(handler) {
  runtimeUnauthorizedHandler = handler || (async () => {});
}
export function getNetworkGuidanceMessage() {
  return `휴대폰의 네트워크 연결과 서버 접근 권한을 확인하고 앱의 서버 주소를 확인해주세요. 현재 설정: ${getRuntimeApiBaseUrl()}`;
}
export function getReadableErrorMessage(error, fallbackMessage) {
  if (error?.message?.includes("Network request failed")) {
    return `${fallbackMessage}\n\n${getNetworkGuidanceMessage()}`;
  }
  if (Array.isArray(error)) {
    return error.map(item => getReadableErrorMessage(item, fallbackMessage)).join("\n");
  }
  if (error && typeof error === "object" && !error.message) {
    try {
      return JSON.stringify(error, null, 2);
    } catch (_error) {
      return fallbackMessage;
    }
  }
  return error?.message || fallbackMessage;
}
export function formatApiErrorDetail(detail, statusCode) {
  if (!detail) {
    return `요청에 실패했습니다. (${statusCode})`;
  }
  if (typeof detail === "string") {
    return detail;
  }
  if (Array.isArray(detail)) {
    const normalized = detail.map(item => {
      if (typeof item === "string") {
        return item;
      }
      if (item && typeof item === "object") {
        const location = Array.isArray(item.loc) ? item.loc.join(" > ") : "";
        const message = item.msg || item.message || JSON.stringify(item);
        return location ? `${location}: ${message}` : message;
      }
      return String(item);
    }).filter(Boolean);
    return normalized.join("\n");
  }
  if (typeof detail === "object") {
    return detail.message || detail.detail || JSON.stringify(detail, null, 2);
  }
  return String(detail);
}
export async function apiRequest(path, options = {}) {
  if (options.baseUrl) {
    return createApiClient({
      baseUrl: options.baseUrl
    }).request(path, {
      method: options.method,
      includeAuth: options.includeAuth
    });
  }
  return runtimeApiClient.request(path, {
    method: options.method,
    includeAuth: options.includeAuth,
    headers: options.headers,
    json: options.body ? JSON.parse(options.body) : undefined
  });
}
export async function fetchFamilyMembers(roomId) {
  return familyRoomsApi.members(roomId);
}
export async function fetchFamilyJoinPreview(inviteCode) {
  return familyRoomsApi.previewInvite(inviteCode);
}
export async function fetchUserFamilies() {
  return familyRoomsApi.list();
}
export async function deleteUploadRecord(uploadId) {
  return uploadsApi.remove(uploadId);
}
export async function updateUserProfile(payload) {
  return apiRequest("/auth/profile", {
    method: "PUT",
    body: JSON.stringify(payload)
  });
}
export async function checkBackendHealth(baseUrlOverride = null) {
  return probeSession({ baseUrl: baseUrlOverride || runtimeApiBaseUrl,
    currentBaseUrl: runtimeApiBaseUrl, getAccessToken: getRuntimeAccessToken,
    fetchImpl: uploadFetch });
}
export async function fetchUploads(roomId) {
  const [uploads, records] = await Promise.all([uploadsApi.list(roomId), recordsApi.list(roomId)]);
  const readable = new Map(records.filter(record => record.capabilities?.can_read === true).map(record => [record.record_id, record]));
  return uploads.filter(upload => readable.has(upload.upload_id)).map(upload => {
    const record = readable.get(upload.upload_id);
    return { ...upload, ...record, title: record.title, description: record.description, tags: record.tags || upload.tags };
  });
}
export async function createUploadEntry(payload) {
  return uploadsApi.create(payload);
}

export const recordsApi = createRecordsApi({
  get: (...args) => runtimeApiClient.get(...args),
  post: (...args) => runtimeApiClient.post(...args),
  patch: (...args) => runtimeApiClient.patch(...args)
});
export const fetchRecord = recordId => recordsApi.get(recordId);
export async function createNativeTextRecord(payload, assertCurrent) {
  return createTextRecord({ records: recordsApi, uploads: uploadsApi,
    payload: { ...payload, visibility: payload.visibility || "PRIVATE" }, idempotencyKey: randomUUID(), assertCurrent });
}
// Create with the chosen audience before sending any media bytes.
export const recordUploadsApi = {
  ...uploadsApi,
  async create(payload) {
    const record = await recordsApi.create(payload, randomUUID());
    if (!record?.record_id) throw new Error("기록 저장 응답을 확인할 수 없습니다.");
    return { ...record, upload_id: record.record_id };
  }
};
