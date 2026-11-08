const DEFAULT_API_BASE_URL = "http://127.0.0.1:8002/api/v1";

class ApiError extends Error {
  constructor({ status, code = null, detail = null }) {
    super(status === 503
      ? "서버가 일시적으로 요청을 처리하지 못했습니다. 최신 상태를 확인한 뒤 다시 시도해주세요."
      : typeof detail === "string" ? detail : `API request failed (${status})`);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

function normalizeApiBaseUrl(value, fallback = DEFAULT_API_BASE_URL) {
  const raw = String(value || fallback).trim();
  const withProtocol = /^https?:\/\//i.test(raw) ? raw : `http://${raw}`;
  const url = new URL(withProtocol);

  if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.search || url.hash) {
    throw new Error("Invalid API base URL");
  }

  const pathname = url.pathname.replace(/\/+$/, "");
  url.pathname = pathname.endsWith("/api/v1") ? pathname : `${pathname}/api/v1`;
  return url.toString().replace(/\/$/, "");
}

function resolveApiBaseUrl(environmentValue = process.env.EXPO_PUBLIC_API_BASE_URL) {
  return normalizeApiBaseUrl(environmentValue || DEFAULT_API_BASE_URL);
}

function parseResponseBody(text) {
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch (_error) {
    return text;
  }
}

function createApiClient({
  baseUrl = resolveApiBaseUrl(),
  getAccessToken = () => "",
  onUnauthorized = () => {},
  fetchImpl = globalThis.fetch,
} = {}) {
  const normalizedBaseUrl = normalizeApiBaseUrl(baseUrl);

  async function request(path, options = {}) {
    const token = options.includeAuth === false ? "" : String(await getAccessToken() || "").trim();
    const headers = {
      Accept: "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(options.json !== undefined ? { "Content-Type": "application/json" } : {}),
      ...(options.headers || {}),
    };
    const response = await fetchImpl(`${normalizedBaseUrl}/${String(path).replace(/^\/+/, "")}`, {
      method: options.method || "GET",
      headers,
      body: options.json !== undefined ? JSON.stringify(options.json) : options.body,
      signal: options.signal,
    });
    const payload = parseResponseBody(await response.text());

    if (!response.ok) {
      if (response.status === 401) await onUnauthorized(token);
      const detail = payload && typeof payload === "object" ? payload.detail ?? payload.message ?? payload : payload;
      const code = payload && typeof payload === "object"
        ? payload.code ?? (payload.detail && typeof payload.detail === "object" ? payload.detail.code : null)
        : null;
      throw new ApiError({ status: response.status, code, detail });
    }

    if (options.expectedStatus !== undefined && response.status !== options.expectedStatus) {
      throw new ApiError({ status: response.status, code: "UNEXPECTED_STATUS" });
    }

    return payload;
  }

  return {
    baseUrl: normalizedBaseUrl,
    request,
    get: (path, options) => request(path, { ...options, method: "GET" }),
    post: (path, options) => request(path, { ...options, method: "POST" }),
    put: (path, options) => request(path, { ...options, method: "PUT" }),
    patch: (path, options) => request(path, { ...options, method: "PATCH" }),
    delete: (path, options) => request(path, { ...options, method: "DELETE" }),
  };
}

async function probeSession({ baseUrl, currentBaseUrl, getAccessToken = () => "", fetchImpl = globalThis.fetch }) {
  const target = normalizeApiBaseUrl(baseUrl);
  const sameServer = target === normalizeApiBaseUrl(currentBaseUrl);
  const client = createApiClient({ baseUrl: target, fetchImpl,
    getAccessToken: sameServer ? getAccessToken : () => "" });
  try {
    const user = await client.get("/auth/me");
    if (!user || typeof user.user_id !== "string" || !user.user_id) {
      throw new Error("서버의 세션 응답을 확인할 수 없습니다.");
    }
    return { reachable: true, authenticated: true };
  } catch (error) {
    if (error.status === 401) return { reachable: true, authenticated: false };
    throw error;
  }
}

module.exports = {
  probeSession,
  ApiError,
  DEFAULT_API_BASE_URL,
  createApiClient,
  normalizeApiBaseUrl,
  resolveApiBaseUrl,
};
