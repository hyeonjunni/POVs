const DEFAULT_KEYS = {
  accessToken: "ambient.accessToken",
  currentUser: "ambient.currentUser",
  legacyUser: "ambient.user",
};

function createAuthSession({ client, storage, setAccessToken, mapUser, keys = DEFAULT_KEYS }) {
  async function clear() {
    setAccessToken("");
    await storage.multiRemove([keys.accessToken, keys.currentUser, keys.legacyUser]);
  }

  async function establish(payload) {
    const token = String(payload?.access_token || "").trim();
    const user = mapUser(payload?.user);
    if (!token || !user?.id) throw new Error("로그인 세션 정보를 확인하지 못했습니다.");
    setAccessToken(token);
    await storage.setItem(keys.accessToken, token);
    return user;
  }

  async function login(credentials) {
    const payload = await client.post("/auth/login", { includeAuth: false, json: credentials });
    await establish(payload);
    try {
      const user = mapUser(await client.get("/auth/me"));
      if (!user?.id) throw new Error("로그인 세션을 확인할 수 없습니다.");
      return user;
    } catch (error) {
      await clear();
      throw error;
    }
  }

  async function signup(details, options = {}) {
    return client.post("/auth/signup", { ...options, includeAuth: false, json: details });
  }

  async function restore() {
    const token = await storage.getItem(keys.accessToken);
    if (!token) return null;
    setAccessToken(token);
    return mapUser(await client.get("/auth/me"));
  }

  return { clear, establish, login, restore, signup };
}

module.exports = { createAuthSession };
