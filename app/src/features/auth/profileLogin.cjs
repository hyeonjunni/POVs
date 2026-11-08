const { normalizeApiBaseUrl } = require("../../../apiClient.cjs");

function resolveProfileLoginConfig({ enabled, apiBaseUrl, profiles } = {}) {
  // Each build opts in explicitly and supplies its own server/profile settings.
  if (enabled !== true && enabled !== "true") return null;
  if (typeof apiBaseUrl !== "string" || !/^https?:\/\//i.test(apiBaseUrl.trim())) return null;
  if (!Array.isArray(profiles) || profiles.length === 0) return null;
  const ids = new Set();
  const usernames = new Set();
  const configuredProfiles = [];
  for (const profile of profiles) {
    if (!profile || ["id", "name", "username", "password"].some(field => typeof profile[field] !== "string" || !profile[field].trim())) return null;
    const id = profile.id.trim();
    const username = profile.username.trim();
    if (ids.has(id.toLowerCase()) || usernames.has(username.toLowerCase())) return null;
    ids.add(id.toLowerCase());
    usernames.add(username.toLowerCase());
    configuredProfiles.push(Object.freeze({ id, name: profile.name.trim(), username, password: profile.password }));
  }
  try {
    return Object.freeze({ apiBaseUrl: normalizeApiBaseUrl(apiBaseUrl), profiles: Object.freeze(configuredProfiles) });
  } catch (_error) {
    return null;
  }
}

function createProfileLogin({ config, onLogin, getApiBaseUrl, selectApiBaseUrl }) {
  let inFlight = null;
  function readCurrentApiBaseUrl() {
    try {
      const currentTarget = getApiBaseUrl();
      return typeof currentTarget === "string" && /^https?:\/\//i.test(currentTarget.trim())
        ? normalizeApiBaseUrl(currentTarget)
        : null;
    } catch (_error) {
      return null;
    }
  }
  function failure(code, message, cause) {
    const error = new Error(message);
    error.code = code;
    if (cause) error.cause = cause;
    return error;
  }
  function targetMismatch(cause) {
    return failure("PROFILE_LOGIN_API_MISMATCH", "프로필 로그인 서버에 연결하지 못했습니다. 서버 주소를 확인해 주세요.", cause);
  }
  return function loginWithProfile(profileId) {
    if (!config) return Promise.reject(failure("PROFILE_LOGIN_NOT_CONFIGURED", "프로필 로그인 설정이 없습니다."));
    const profile = config.profiles.find(item => item.id === profileId);
    if (!profile) return Promise.reject(failure("PROFILE_LOGIN_UNKNOWN_PROFILE", "선택한 프로필을 확인할 수 없습니다."));
    // One lock covers all profiles, including synchronous taps before React rerenders.
    if (inFlight) return inFlight;
    inFlight = Promise.resolve().then(async () => {
      if (readCurrentApiBaseUrl() !== config.apiBaseUrl && typeof selectApiBaseUrl === "function") {
        try {
          await selectApiBaseUrl(config.apiBaseUrl);
        } catch (cause) {
          throw targetMismatch(cause);
        }
      }
      // Recheck the live target before passing credentials into the existing auth flow.
      if (readCurrentApiBaseUrl() !== config.apiBaseUrl) throw targetMismatch();
      return onLogin({ username: profile.username, password: profile.password });
    }).finally(() => { inFlight = null; });
    return inFlight;
  };
}

module.exports = { resolveProfileLoginConfig, createProfileLogin };
