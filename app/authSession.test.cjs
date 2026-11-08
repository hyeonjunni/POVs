const { test } = require("node:test");
const assert = require("node:assert/strict");
const { createAuthSession } = require("./authSession.cjs");

function memoryStorage(initial = {}) {
  const values = new Map(Object.entries(initial));
  return {
    getItem: async (key) => values.get(key) ?? null,
    setItem: async (key, value) => values.set(key, value),
    multiRemove: async (keys) => keys.forEach((key) => values.delete(key)),
    snapshot: () => Object.fromEntries(values),
  };
}

test("login persists the returned JWT and maps the backend user", async () => {
  const storage = memoryStorage();
  const tokens = [];
  const auth = createAuthSession({
    client: {
      get: async path => { assert.equal(path, "/auth/me"); return { user_id: "user-1", name: "Member" }; },
      post: async (path, options) => {
        assert.equal(path, "/auth/login");
        assert.deepEqual(options, {
          includeAuth: false,
          json: { username: "member1", password: "password1" },
        });
        return { access_token: "jwt-value", token_type: "bearer", user: { user_id: "user-1", name: "Member" } };
      },
    },
    storage,
    setAccessToken: (token) => tokens.push(token),
    mapUser: (user) => ({ id: user.user_id, name: user.name }),
  });

  assert.deepEqual(await auth.login({ username: "member1", password: "password1" }), { id: "user-1", name: "Member" });
  assert.deepEqual(storage.snapshot(), { "ambient.accessToken": "jwt-value" });
  assert.deepEqual(tokens, ["jwt-value"]);
});

test("restore validates a persisted JWT through auth me", async () => {
  const storage = memoryStorage({ "ambient.accessToken": "saved-jwt" });
  const tokens = [];
  const auth = createAuthSession({
    client: { get: async (path) => {
      assert.equal(path, "/auth/me");
      return { user_id: "user-2", name: "Restored" };
    } },
    storage,
    setAccessToken: (token) => tokens.push(token),
    mapUser: (user) => ({ id: user.user_id, name: user.name }),
  });

  assert.deepEqual(await auth.restore(), { id: "user-2", name: "Restored" });
  assert.deepEqual(tokens, ["saved-jwt"]);
});

test("non-401 restore failure preserves the cached session for a later retry", async () => {
  const storage = memoryStorage({
    "ambient.accessToken": "expired",
    "ambient.currentUser": "cached",
    "ambient.user": "legacy",
  });
  const tokens = [];
  const auth = createAuthSession({
    client: { get: async () => { throw new Error("expired"); } },
    storage,
    setAccessToken: (token) => tokens.push(token),
    mapUser: (user) => user,
  });

  await assert.rejects(auth.restore(), /expired/);
  assert.deepEqual(storage.snapshot(), {
    "ambient.accessToken": "expired",
    "ambient.currentUser": "cached",
    "ambient.user": "legacy",
  });
  assert.deepEqual(tokens, ["expired"]);
});

test("missing persisted JWT skips auth me without creating a session", async () => {
  let calls = 0;
  const auth = createAuthSession({
    client: { get: async () => { calls += 1; } },
    storage: memoryStorage(),
    setAccessToken: () => {},
    mapUser: (user) => user,
  });

  assert.equal(await auth.restore(), null);
  assert.equal(calls, 0);
});

test("signup returns its contract result without assuming it created an authenticated session", async () => {
  const tokenWrites = [];
  const auth = createAuthSession({
    client: { post: async (path, options) => {
      assert.equal(path, "/auth/signup");
      assert.equal(options.includeAuth, false);
      assert.deepEqual(options.headers, { Prefer: "respond-async" });
      return { activation_id: "activation-1", state: "WAITING_CREDENTIAL" };
    } },
    storage: memoryStorage(),
    setAccessToken: (token) => tokenWrites.push(token),
    mapUser: (user) => user,
  });

  assert.deepEqual(await auth.signup({ username: "member", password: "secret" }, { headers: { Prefer: "respond-async" } }), {
    activation_id: "activation-1",
    state: "WAITING_CREDENTIAL",
  });
  assert.deepEqual(tokenWrites, []);
});
