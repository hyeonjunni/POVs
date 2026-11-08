const { test } = require("node:test");
const assert = require("node:assert/strict");
const { createAuthSession } = require("./authSession.cjs");
const { resolveProfileLoginConfig, createProfileLogin } = require("./src/features/auth/profileLogin.cjs");

const fixture = {
  enabled: "true", apiBaseUrl: "https://test.example/api/v1",
  profiles: ["F1", "F2", "F3", "F4"].map((id, index) => ({ id, name: `회원 ${index + 1}`, username: `fixture_member_${index + 1}`, password: `fixture-password-${index + 1}` })),
};
const config = resolveProfileLoginConfig(fixture);
const displayProfiles = config.profiles.map(({ id, name, username }) => ({ id, name, username }));
const credentialsFor = index => ({ username: fixture.profiles[index].username, password: fixture.profiles[index].password });

test("profile shortcuts require explicit opt-in, an exact server, and complete unique profiles", () => {
  for (const enabled of [undefined, false, "false", "1"]) assert.equal(resolveProfileLoginConfig({ ...fixture, enabled }), null);
  for (const apiBaseUrl of [" ", "test.example", "file:///api/v1", "https://user:pass@test.example", "https://test.example?secret=value"]) {
    assert.equal(resolveProfileLoginConfig({ ...fixture, apiBaseUrl }), null);
  }
  for (const profiles of [undefined, {}, [], [null], ["F1"]]) assert.equal(resolveProfileLoginConfig({ ...fixture, profiles }), null);
  for (const field of ["id", "name", "username", "password"]) {
    assert.equal(resolveProfileLoginConfig({ ...fixture, profiles: fixture.profiles.map((profile, index) => index ? profile : { ...profile, [field]: " " }) }), null);
  }
  for (const field of ["id", "username"]) {
    assert.equal(resolveProfileLoginConfig({ ...fixture, profiles: [fixture.profiles[0], { ...fixture.profiles[1], [field]: ` ${fixture.profiles[0][field].toLowerCase()} ` }] }), null);
  }
  assert.deepEqual(config, { apiBaseUrl: fixture.apiBaseUrl, profiles: fixture.profiles });
  assert.ok(Object.isFrozen(config));
  assert.ok(Object.isFrozen(config.profiles));
  assert.ok(config.profiles.every(Object.isFrozen));
});

test("configuration normalizes labels, IDs and endpoint but preserves the exact password", () => {
  const normalized = resolveProfileLoginConfig({ enabled: true, apiBaseUrl: " https://test.example/api/v1/ ", profiles: [{ id: " F1 ", name: " 회원 1 ", username: " fixture_member_1 ", password: " significant spaces " }] });
  assert.deepEqual(normalized, { apiBaseUrl: fixture.apiBaseUrl, profiles: [{ id: "F1", name: "회원 1", username: "fixture_member_1", password: " significant spaces " }] });
});

test("each profile delegates only its exact configured credentials and preserves the server result", async () => {
  const calls = [];
  const serverUser = { id: "server-user", profile_completed: false };
  const login = createProfileLogin({ config, getApiBaseUrl: () => `${fixture.apiBaseUrl}/`, onLogin: async credentials => { calls.push(credentials); return serverUser; } });
  for (const profile of fixture.profiles) assert.equal(await login(profile.id), serverUser);
  assert.deepEqual(calls, fixture.profiles.map(({ username, password }) => ({ username, password })));
});

test("same-profile taps and F1/F2 taps share one in-flight request, then allow another profile", async () => {
  let resolveRequest;
  const calls = [];
  const login = createProfileLogin({ config, getApiBaseUrl: () => fixture.apiBaseUrl, onLogin: credentials => {
    calls.push(credentials);
    return new Promise(resolve => { resolveRequest = resolve; });
  } });
  const first = login("F1");
  assert.equal(login("F1"), first);
  assert.equal(login("F2"), first);
  await Promise.resolve();
  assert.deepEqual(calls, [credentialsFor(0)]);
  resolveRequest({ id: "server-F1" });
  await first;
  const next = login("F2");
  await Promise.resolve();
  assert.deepEqual(calls, [credentialsFor(0), credentialsFor(1)]);
  resolveRequest({ id: "server-F2" });
  assert.deepEqual(await next, { id: "server-F2" });
});

test("unknown profile IDs never fall back to a configured profile, including during another login", async () => {
  let calls = 0;
  let release;
  const login = createProfileLogin({ config, getApiBaseUrl: () => fixture.apiBaseUrl, onLogin: () => { calls += 1; return new Promise(resolve => { release = resolve; }); } });
  for (const id of [undefined, null, "", "f1", "F5", { id: "F1" }]) await assert.rejects(login(id), { code: "PROFILE_LOGIN_UNKNOWN_PROFILE" });
  assert.equal(calls, 0);
  const pending = login("F1");
  await assert.rejects(login("F5"), { code: "PROFILE_LOGIN_UNKNOWN_PROFILE" });
  assert.equal(calls, 1);
  release({ id: "server-F1" });
  await pending;
});

test("normal auth failures propagate unchanged and release the shared lock for a different profile", async () => {
  const authFailure = Object.assign(new Error("Rejected credentials"), { status: 401 });
  const calls = [];
  const login = createProfileLogin({ config, getApiBaseUrl: () => fixture.apiBaseUrl, onLogin: credentials => {
    calls.push(credentials);
    if (calls.length === 1) throw authFailure;
    return Promise.resolve({ id: "retry-user" });
  } });
  await assert.rejects(login("F1"), error => error === authFailure);
  assert.deepEqual(await login("F2"), { id: "retry-user" });
  assert.deepEqual(calls, [credentialsFor(0), credentialsFor(1)]);
});

test("missing configuration or a mismatched API target never receives profile credentials", async () => {
  let calls = 0;
  const onLogin = () => { calls += 1; };
  for (const target of ["https://other.example/api/v1", "https://test.example/different/api/v1", "https://test.example/api/v1?redirect=1", "", null]) {
    const login = createProfileLogin({ config, getApiBaseUrl: () => target, onLogin });
    await assert.rejects(login("F1"), { code: "PROFILE_LOGIN_API_MISMATCH" });
  }
  const disabled = createProfileLogin({ config: null, getApiBaseUrl: () => fixture.apiBaseUrl, onLogin });
  await assert.rejects(disabled("F1"), { code: "PROFILE_LOGIN_NOT_CONFIGURED" });
  assert.equal(calls, 0);
});

test("the live API target is checked immediately before credentials are sent", async () => {
  let target = fixture.apiBaseUrl;
  let calls = 0;
  const login = createProfileLogin({ config, getApiBaseUrl: () => target, onLogin: async () => { calls += 1; } });
  const pending = login("F1");
  target = "https://other.example/api/v1";
  await assert.rejects(pending, { code: "PROFILE_LOGIN_API_MISMATCH" });
  assert.equal(calls, 0);
  target = fixture.apiBaseUrl;
  await login("F2");
  assert.equal(calls, 1);
});

test("an older saved API target is selected before the chosen profile is delegated", async () => {
  let target = "https://old.example/api/v1";
  const events = [];
  const login = createProfileLogin({ config, getApiBaseUrl: () => target,
    selectApiBaseUrl: async nextTarget => { events.push(["select", nextTarget]); target = nextTarget; },
    onLogin: async credentials => { events.push(["login", target, credentials]); return { id: "real-user" }; },
  });
  assert.deepEqual(await login("F3"), { id: "real-user" });
  assert.deepEqual(events, [["select", fixture.apiBaseUrl], ["login", fixture.apiBaseUrl, credentialsFor(2)]]);
  await login("F4");
  assert.equal(events.filter(([kind]) => kind === "select").length, 1);
});

test("failed or ineffective target selection cannot transmit credentials and permits retry", async () => {
  let target = "https://old.example/api/v1";
  let selector = async () => { throw new Error("settings failed"); };
  let calls = 0;
  const login = createProfileLogin({ config, getApiBaseUrl: () => target,
    selectApiBaseUrl: nextTarget => selector(nextTarget), onLogin: async () => { calls += 1; },
  });
  await assert.rejects(login("F1"), { code: "PROFILE_LOGIN_API_MISMATCH" });
  selector = async () => fixture.apiBaseUrl;
  await assert.rejects(login("F2"), { code: "PROFILE_LOGIN_API_MISMATCH" });
  assert.equal(calls, 0);
  selector = async nextTarget => { target = nextTarget; };
  await login("F3");
  assert.equal(calls, 1);
});

test("asynchronous target selection and all profile buttons share a lock and recheck the server", async () => {
  let target = "https://old.example/api/v1";
  let releaseSelection;
  let selections = 0;
  const calls = [];
  const login = createProfileLogin({ config, getApiBaseUrl: () => target,
    selectApiBaseUrl: nextTarget => { selections += 1; target = nextTarget; return new Promise(resolve => { releaseSelection = resolve; }); },
    onLogin: async credentials => { calls.push(credentials); },
  });
  const first = login("F1");
  await Promise.resolve();
  assert.equal(login("F2"), first);
  assert.equal(selections, 1);
  assert.deepEqual(calls, []);
  target = "https://changed-during-selection.example/api/v1";
  releaseSelection();
  await assert.rejects(first, { code: "PROFILE_LOGIN_API_MISMATCH" });
  assert.deepEqual(calls, []);
  const retry = login("F2");
  await Promise.resolve();
  assert.equal(login("F1"), retry);
  releaseSelection();
  await retry;
  assert.equal(selections, 2);
  assert.deepEqual(calls, [credentialsFor(1)]);
});

test("profiles use server-issued sessions and server profile state through logout/relogin", async () => {
  const stored = new Map();
  const tokens = [];
  let loginCount = 0;
  let activeProfile;
  const auth = createAuthSession({
    client: {
      get: async path => { assert.equal(path, "/auth/me"); return { user_id: `server-${activeProfile.id}`, profile_completed: activeProfile.id === "F2" }; },
      post: async (path, options) => {
        assert.equal(path, "/auth/login");
        assert.equal(options.includeAuth, false);
        activeProfile = fixture.profiles.find(profile => profile.username === options.json.username);
        assert.deepEqual(options.json, { username: activeProfile.username, password: activeProfile.password });
        loginCount += 1;
        return { access_token: `server-token-${loginCount}`, user: { user_id: `server-${activeProfile.id}`, profile_completed: false } };
      },
    },
    storage: { setItem: async (key, value) => stored.set(key, value), multiRemove: async keys => keys.forEach(key => stored.delete(key)) },
    setAccessToken: token => tokens.push(token),
    mapUser: user => ({ id: user.user_id, profileCompleted: user.profile_completed }),
  });
  const login = createProfileLogin({ config, getApiBaseUrl: () => fixture.apiBaseUrl, onLogin: auth.login });
  assert.deepEqual(await login("F1"), { id: "server-F1", profileCompleted: false });
  assert.equal(stored.get("ambient.accessToken"), "server-token-1");
  await auth.clear();
  assert.equal(stored.size, 0);
  assert.deepEqual(await login("F2"), { id: "server-F2", profileCompleted: true });
  assert.equal(stored.get("ambient.accessToken"), "server-token-2");
  assert.deepEqual(tokens, ["server-token-1", "", "server-token-2"]);
});

test("rejected or malformed server responses cannot establish a preview or token", async () => {
  let storedWrites = 0;
  let tokenWrites = 0;
  for (const result of [new Error("offline"), {}, { access_token: "", user: { id: "user" } }]) {
    const auth = createAuthSession({
      client: { post: async () => { if (result instanceof Error) throw result; return result; } },
      storage: { setItem: async () => { storedWrites += 1; } },
      setAccessToken: () => { tokenWrites += 1; }, mapUser: user => user,
    });
    const login = createProfileLogin({ config, getApiBaseUrl: () => fixture.apiBaseUrl, onLogin: auth.login });
    await assert.rejects(login("F1"));
  }
  assert.equal(storedWrites, 0);
  assert.equal(tokenWrites, 0);
});

// Exercise actual JSX handlers with host elements; this does not imply a device/server test.
function renderLogin(props) {
  const fs = require("node:fs");
  const path = require("node:path");
  const vm = require("node:vm");
  const React = require("react");
  const filename = path.join(__dirname, "src/features/auth/LoginScreen.js");
  const code = require("@babel/core").transformSync(fs.readFileSync(filename, "utf8"), {
    filename, configFile: false, babelrc: false,
    plugins: [[require("@babel/plugin-transform-react-jsx"), { runtime: "classic" }], require("@babel/plugin-transform-modules-commonjs")],
  }).code;
  const exports = {};
  const slots = [];
  let cursor = 0;
  const useState = initial => {
    const index = cursor++;
    if (!(index in slots)) slots[index] = typeof initial === "function" ? initial() : initial;
    return [slots[index], next => { slots[index] = typeof next === "function" ? next(slots[index]) : next; }];
  };
  const useRef = initial => { const index = cursor++; return slots[index] ||= { current: initial }; };
  vm.runInNewContext(code, { exports, require: id => {
    if (id === "react") return { ...React, useState, useRef };
    if (id === "react-native") return new Proxy({}, { get: (_target, key) => key === "__esModule" ? false : key });
    if (id === "expo-status-bar") return { StatusBar: "StatusBar" };
    if (id.endsWith("/ThemeProvider")) return require("./testSupport/theme.cjs");
    if (id.endsWith("/styles")) return { styles: {} };
    if (id.endsWith("/layout")) return {};
    throw new Error(`Unexpected import ${id}`);
  } }, { filename });
  function render() {
    cursor = 0;
    const found = { buttons: [], text: [], inputs: [], indicators: [], rerender: render };
    function visit(node) {
      if (Array.isArray(node)) return node.forEach(visit);
      if (typeof node === "string") found.text.push(node);
      else if (node?.props) {
        if (node.type === "Pressable") found.buttons.push(node.props);
        if (node.type === "TextInput") found.inputs.push(node.props);
        if (node.type === "ActivityIndicator") found.indicators.push(node.props);
        visit(node.props.children);
      }
    }
    visit(exports.LoginScreen({ bottomInset: 0, ...props }));
    return found;
  }
  return render();
}

function nodeText(node) {
  if (Array.isArray(node)) return node.map(nodeText).join("");
  if (typeof node === "string") return node;
  return node?.props ? nodeText(node.props.children) : "";
}

test("all four profile buttons select exact real-login profiles and display no password", async () => {
  const calls = [];
  const rendered = renderLogin({ loginProfiles: displayProfiles, onProfileLogin: createProfileLogin({ config, getApiBaseUrl: () => fixture.apiBaseUrl, onLogin: async credentials => { calls.push(credentials); } }) });
  for (const profile of fixture.profiles) {
    const shortcut = rendered.buttons.find(button => button.accessibilityLabel === `${profile.id} ${profile.name} 프로필로 로그인`);
    assert.ok(shortcut);
    assert.ok(rendered.text.includes(profile.name));
    assert.ok(rendered.text.includes(profile.username));
    assert.ok(!rendered.text.some(text => text.includes(profile.password)));
    await shortcut.onPress();
  }
  assert.deepEqual(calls, fixture.profiles.map(({ username, password }) => ({ username, password })));
  assert.ok(!rendered.buttons.some(button => button.accessibilityLabel === "테스트 계정 로그인"));
});

test("profile choices are absent without a configured list and handler", () => {
  for (const props of [{}, { loginProfiles: [] }, { loginProfiles: displayProfiles }, { onProfileLogin: async () => {} }]) {
    assert.ok(!renderLogin(props).buttons.some(button => button.accessibilityLabel?.endsWith("프로필로 로그인")));
  }
});

test("normal auth busy state disables every profile plus normal login and signup", async () => {
  let calls = 0;
  const rendered = renderLogin({ authLoading: true, loginProfiles: displayProfiles, onProfileLogin: async () => { calls += 1; }, onLogin: async () => { calls += 1; } });
  assert.ok(rendered.buttons.length >= 6);
  for (const button of rendered.buttons) assert.equal(button.disabled, true);
  for (const button of rendered.buttons.filter(button => button.accessibilityLabel?.endsWith("프로필로 로그인"))) await button.onPress();
  assert.equal(calls, 0);
});

test("profile UI prevents a second profile or ordinary login during a synchronous pending tap", async () => {
  const calls = [];
  let release;
  let ordinaryCalls = 0;
  let popups = 0;
  const rendered = renderLogin({ loginProfiles: displayProfiles, onProfileLogin: id => { calls.push(id); return new Promise(resolve => { release = resolve; }); }, onLogin: () => { ordinaryCalls += 1; }, onShowPopupResult: () => { popups += 1; } });
  const shortcuts = rendered.buttons.filter(button => button.accessibilityLabel?.endsWith("프로필로 로그인"));
  const first = shortcuts[0].onPress();
  await shortcuts[0].onPress();
  await shortcuts[1].onPress();
  await rendered.buttons.find(button => nodeText(button.children) === "로그인").onPress();
  assert.deepEqual(calls, ["F1"]);
  assert.equal(ordinaryCalls, 0);
  assert.equal(popups, 0);
  release();
  await first;
  const retry = shortcuts[1].onPress();
  assert.deepEqual(calls, ["F1", "F2"]);
  release();
  await retry;
});

test("pending profile state disables every choice, marks only that profile busy, and recovers after failure", async () => {
  let rejectRequest;
  let rendered = renderLogin({ loginProfiles: displayProfiles, onProfileLogin: () => new Promise((_resolve, reject) => { rejectRequest = reject; }) });
  const pending = rendered.buttons.find(button => button.accessibilityLabel === "F3 회원 3 프로필로 로그인").onPress();
  rendered = rendered.rerender();
  assert.ok(rendered.buttons.every(button => button.disabled === true));
  for (const button of rendered.buttons.filter(button => button.accessibilityLabel?.endsWith("프로필로 로그인"))) {
    assert.equal(button.accessibilityState.busy, button.accessibilityLabel === "F3 회원 3 프로필로 로그인");
  }
  assert.equal(rendered.indicators.length, 1);
  assert.equal(rendered.text.filter(text => text === "로그인 중…").length, 1);
  rejectRequest(Object.assign(new Error("Rejected credentials"), { status: 401 }));
  await pending;
  rendered = rendered.rerender();
  assert.ok(rendered.buttons.every(button => button.disabled === false));
  assert.equal(rendered.indicators.length, 0);
});

test("ordinary login blocks profile selection and signup navigation until its request settles", async () => {
  let release;
  let profileCalls = 0;
  let normalCalls = 0;
  let rendered = renderLogin({ loginProfiles: displayProfiles, onProfileLogin: async () => { profileCalls += 1; }, onLogin: async credentials => {
    normalCalls += 1;
    assert.deepEqual({ ...credentials }, { username: "manual-user", password: "manual-password" });
    return new Promise(resolve => { release = resolve; });
  } });
  rendered.inputs.find(input => input.placeholder === "예: family01").onChangeText("manual-user");
  rendered.inputs.find(input => input.secureTextEntry).onChangeText("manual-password");
  rendered = rendered.rerender();
  const pending = rendered.buttons.find(button => nodeText(button.children) === "로그인").onPress();
  await rendered.buttons.find(button => button.accessibilityLabel === "F1 회원 1 프로필로 로그인").onPress();
  await rendered.buttons.find(button => nodeText(button.children) === "로그인").onPress();
  rendered.buttons.find(button => nodeText(button.children) === "회원가입으로 전환").onPress();
  rendered = rendered.rerender();
  assert.equal(profileCalls, 0);
  assert.equal(normalCalls, 1);
  assert.ok(rendered.buttons.some(button => nodeText(button.children) === "회원가입으로 전환"));
  release();
  await pending;
  rendered.buttons.find(button => nodeText(button.children) === "회원가입으로 전환").onPress();
  rendered = rendered.rerender();
  assert.ok(rendered.buttons.some(button => nodeText(button.children) === "이미 계정이 있어요"));
  assert.ok(!rendered.buttons.some(button => button.accessibilityLabel?.endsWith("프로필로 로그인")));
});
