const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const { createRequire } = require("node:module");
const babel = require("@babel/core");
const api = require("./apiClient.cjs");

const filename = path.join(__dirname, "src/navigation/useAppController.js");
const code = babel.transformSync(fs.readFileSync(filename, "utf8"), {
  filename, configFile: false, babelrc: false,
  plugins: [function ({ types }) {
    return { visitor: { CallExpression(p) {
      if (p.node.callee.name === "useState" && p.parent.id?.type === "ArrayPattern") {
        p.node.arguments.push(types.stringLiteral(p.parent.id.elements[0].name));
      }
    } } };
  }, require("@babel/plugin-transform-modules-commonjs")],
}).code;

const operation = { operation_id: "original-room-operation", state: "WAITING_CREDENTIAL", retry_after_seconds: 0 };
const room = { room_id: "created-room", name: "원래 이름", invite_code: "ABCDEF" };
function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}
async function until(predicate) {
  const deadline = Date.now() + 2000;
  while (!predicate() && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 2));
  assert.ok(predicate(), "expected request boundary reached");
}

function harness({ failPoll = false, pollGate = null, memberGate = null, persisted = new Map(), loginUserId = "original-user", restoreFailure = false, uploadGate = null, binaryFails = false } = {}) {
  const requests = [];
  const changes = [];
  let token = "original-token";
  let keys = 0;
  let memberRequests = 0;
  const state = new Map(Object.entries({ user: { id: "original-user" }, familyRooms: [], records: [] }));
  const slots = [];
  let cursor = 0;
  let effects = [];
  const react = {
    useState(initial, name) {
      if (!state.has(name)) state.set(name, typeof initial === "function" ? initial() : initial);
      return [state.get(name), next => {
        const value = typeof next === "function" ? next(state.get(name)) : next;
        state.set(name, value);
        changes.push([name, value]);
      }];
    },
    useRef(initial) { const slot = cursor++; return slots[slot] ||= { current: initial }; },
    useMemo(factory, deps) {
      const slot = cursor++;
      if (!slots[slot] || deps.some((value, index) => value !== slots[slot].deps[index])) slots[slot] = { value: factory(), deps };
      return slots[slot].value;
    },
    useEffect(effect) { effects.push(effect); }, // Tests opt into startup explicitly.
  };
  const storage = {
    async getItem(key) { return persisted.get(key) || null; },
    async setItem(key, value) { persisted.set(key, value); },
    async removeItem(key) { persisted.delete(key); },
    async multiRemove(list) { list.forEach(key => persisted.delete(key)); },
  };
  const fetchImpl = async (url, options) => {
    requests.push({ url, ...options });
    let value;
    if (options.method === "GET") {
      if (failPoll) {
        failPoll = false;
        throw Object.assign(new Error("활성화 상태 확인 시간이 초과되었습니다."), { code: "REQUEST_TIMEOUT" });
      }
      if (pollGate) await pollGate.promise;
      value = { ...operation, state: "READY" };
    } else value = options.headers["X-Activation-Operation"] ? room : operation;
    return { ok: true, status: 200, text: async () => JSON.stringify(value) };
  };
  const runtime = {
    uploadsApi: {
      create: async () => { requests.push({metadata: true}); return {upload_id: "old-upload", has_file: false}; },
      list: async () => [{upload_id: "old-upload", has_file: false}],
      uploadBinary: async id => { requests.push({binary: id}); if (binaryFails) throw new Error("binary rejected"); return {upload_id: "old-upload", file_id: "old-file", has_file: true}; },
    },
    async fetchUploads(roomId) {
      requests.push({uploadListRoom: roomId});
      if (uploadGate) await uploadGate.promise;
      return [{upload_id: "old-upload", type: "video", has_file: !binaryFails}];
    },
    getRuntimeApiBaseUrl: () => "http://test.invalid/api/v1",
    getRuntimeAccessToken: () => token,
    setRuntimeAccessToken: next => { token = next; },
    setRuntimeApiBaseUrl: value => value,
    CONFIGURED_API_BASE_URL: "http://test.invalid/api/v1",
    getReadableErrorMessage: error => error.message,
    processingApi: {}, runtimeApiClient: {
      async post(route) {
        assert.equal(route, "/auth/login");
        restoreFailure = false;
        return { access_token: "reissued-token", user: { id: loginUserId } };
      },
      async get(route) {
        assert.equal(route, "/auth/me");
        if (restoreFailure) throw new Error("restore transport unavailable");
        return { id: loginUserId };
      },
    },
    async fetchUserFamilies() { return []; },
    async fetchFamilyMembers() { memberRequests++; if (memberGate) await memberGate.promise; return []; },
  };
  runtime.recordUploadsApi = runtime.uploadsApi;
  const mocks = {
    react,
    "../shared/ui/layout": { useAndroidBottomInset: () => 0, useKeyboardInset: () => 0 },
    "../features/records/recordHelpers": { initialItems: [], uploadTypes: [], buildDefaultTagsForType: () => [], parseTagInput: () => [], mapUploadToRecord: value => ({id: value.upload_id, hasFile: value.has_file}) },
    "../shared/api/runtime": runtime,
    "../features/profile/utils": { buildProfileDraft: () => ({}), isProfileComplete: () => true, isProfileFormComplete: () => true },
    "@react-native-async-storage/async-storage": storage,
    "../features/auth/mappers": { mapBackendUser: value => value },
    "../shared/storage/keys": { STORAGE_KEYS: Object.fromEntries(
      ["accessToken", "currentUser", "apiBaseUrl", "activeFamilyId", "familyRooms", "legacyUser"].map(key => [key, `ambient.${key}`])
    ) },
    "../features/family/relations": { mapFamilyRoom: value => ({ id: value.room_id, name: value.name, code: value.invite_code }) },
    "../features/records/voicePicker": { pickVoiceFile: async () => null, releaseVoiceFile() {} },
    "expo-image-picker": {
      requestMediaLibraryPermissionsAsync: async () => ({granted: true}),
      launchImageLibraryAsync: async () => ({canceled: false, assets: [{uri: "file:///clip.mp4", fileName: "clip.mp4", mimeType: "video/mp4", fileSize: 10}]}),
    },
    "expo-crypto": { async getRandomBytesAsync() { keys++; return new Uint8Array(32).fill(keys); } },
    // Keep the real API serialization/headers/errors; replace only its network transport.
    "../../apiClient.cjs": { ...api, createApiClient: options => api.createApiClient({ ...options, fetchImpl }) },
  };
  const module = { exports: {} };
  const realRequire = createRequire(filename);
  vm.runInNewContext(code, { module, exports: module.exports, require: id => mocks[id] || realRequire(id), console, setTimeout, clearTimeout, setInterval, clearInterval });
  return {
    state, changes, requests, persisted,
    allowBinary() { binaryFails = false; },
    get keys() { return keys; },
    get memberRequests() { return memberRequests; },
    switchUser() { token = "new-token"; state.set("user", { id: "new-user" }); },
    async restore() {
      effects.find(effect => effect.toString().includes("loadPersistedData"))();
      await until(() => state.get("storageLoaded") === true);
    },
    render() { cursor = 0; effects = []; return module.exports.useAppController(); },
  };
}

test("room-create controller retries a timed-out activation using original operation, capability and body", async () => {
  const h = harness({ failPoll: true });
  assert.equal(await h.render().handleCreateFamily(" 원래 이름 "), false);
  assert.equal(h.render().popupLoading, null);
  assert.match(h.render().popupResult.message, /시간이 초과/);
  const saved = JSON.parse(h.persisted.get("ambient.roomCreateActivation"));
  assert.equal(saved.operationId, operation.operation_id);
  assert.equal(await h.render().handleCreateFamily("바뀐 이름"), true);
  const posts = h.requests.filter(request => request.method === "POST");
  assert.equal(posts.length, 2);
  assert.equal(posts.filter(request => !request.headers["X-Activation-Operation"]).length, 1);
  assert.equal(posts[1].headers["X-Activation-Operation"], operation.operation_id);
  assert.equal(posts[0].headers["Idempotency-Key"], saved.idempotencyKey);
  assert.equal(posts[1].headers["Idempotency-Key"], saved.idempotencyKey);
  assert.equal(posts[1].body, posts[0].body);
  assert.deepEqual(JSON.parse(posts[1].body), { name: "원래 이름" });
  assert.ok(h.requests.filter(request => request.method === "GET").every(request => request.url.endsWith(`/secure-operations/${operation.operation_id}`)));
  assert.equal(h.keys, 1);
  assert.equal(h.persisted.has("ambient.roomCreateActivation"), false);
  assert.equal(h.render().familyRooms[0].id, room.room_id);
});

test("controller reload resumes durable activation without a second initial POST or fresh capability", async () => {
  const original = harness({ failPoll: true });
  await original.render().handleCreateFamily("원래 이름");
  const saved = JSON.parse(original.persisted.get("ambient.roomCreateActivation"));
  const reloaded = harness({ persisted: original.persisted });
  assert.equal(await reloaded.render().handleCreateFamily("다른 입력"), true);
  assert.equal(reloaded.requests[0].method, "GET");
  const posts = reloaded.requests.filter(request => request.method === "POST");
  assert.equal(posts.length, 1);
  assert.equal(posts[0].headers["X-Activation-Operation"], saved.operationId);
  assert.equal(posts[0].headers["Idempotency-Key"], saved.idempotencyKey);
  assert.equal(posts[0].body, saved.serializedPayload);
  assert.equal(reloaded.keys, 0);
});

for (const account of ["original-user", "different-user"]) {
  test(`failed restore followed by ${account} credential login ${account === "original-user" ? "preserves" : "clears"} durable room continuation`, async () => {
    const original = harness({ failPoll: true });
    await original.render().handleCreateFamily("원래 이름");
    const raw = original.persisted.get("ambient.roomCreateActivation");
    const saved = JSON.parse(raw);
    original.persisted.set("ambient.accessToken", "saved-token");
    const reloaded = harness({ persisted: original.persisted, loginUserId: account, restoreFailure: true });
    reloaded.render();
    await reloaded.restore();
    assert.equal(reloaded.state.get("user"), null);
    assert.equal(reloaded.persisted.get("ambient.roomCreateActivation"), raw);
    await reloaded.render().handleCredentialLogin({ username: "account", password: "test-password" });
    assert.equal(reloaded.state.get("user").id, account);
    if (account === "original-user") {
      assert.equal(reloaded.persisted.get("ambient.roomCreateActivation"), raw);
      assert.equal(await reloaded.render().handleCreateFamily("다른 입력"), true);
      const posts = reloaded.requests.filter(request => request.method === "POST");
      assert.equal(posts.length, 1);
      assert.equal(posts[0].headers["X-Activation-Operation"], saved.operationId);
      assert.equal(posts[0].headers["Idempotency-Key"], saved.idempotencyKey);
      assert.equal(posts[0].headers.Authorization, "Bearer reissued-token");
      assert.equal(posts[0].body, saved.serializedPayload);
      assert.equal(reloaded.keys, 0);
    } else {
      assert.equal(reloaded.persisted.has("ambient.roomCreateActivation"), false);
      assert.equal(reloaded.requests.length, 0);
    }
  });
}

test("repeated room-create taps produce one initial activation and one continuation", async () => {
  const gate = deferred();
  const h = harness({ pollGate: gate });
  const app = h.render();
  const first = app.handleCreateFamily("원래 이름");
  const second = app.handleCreateFamily("다른 이름");
  await until(() => h.requests.some(request => request.method === "GET"));
  assert.equal(await second, false);
  gate.resolve();
  assert.equal(await first, true);
  assert.equal(h.keys, 1);
  assert.equal(h.requests.filter(request => request.method === "POST").length, 2);
});

test("room names over 200 code points do not persist invalid activation and corrected input succeeds", async () => {
  const h = harness();
  assert.equal(await h.render().handleCreateFamily("가".repeat(201)), false);
  assert.equal(h.requests.length, 0);
  assert.equal(h.persisted.has("ambient.roomCreateActivation"), false);
  assert.equal(h.keys, 0);
  // Astral characters count as one code point, matching the backend length rule.
  const corrected = "😀".repeat(200);
  assert.equal(await h.render().handleCreateFamily(corrected), true);
  const post = h.requests.find(request => request.method === "POST");
  assert.equal(JSON.parse(post.body).name, corrected);
});

for (const boundary of ["logout", "user switch"]) {
  for (const stage of ["poll", "members"]) {
    test(`${boundary} discards late ${stage} response without changing newer room UI`, async () => {
      const gate = deferred();
      const h = harness(stage === "poll" ? { pollGate: gate } : { memberGate: gate });
      const pending = h.render().handleCreateFamily("원래 이름");
      await until(() => stage === "poll" ? h.requests.some(request => request.method === "GET") : h.memberRequests > 0);
      if (boundary === "logout") await h.render().handleLogout();
      else h.switchUser();
      h.state.set("popupLoading", { title: "new session work" });
      const before = h.changes.length;
      gate.resolve();
      assert.equal(await pending, false);
      assert.equal(h.changes.length, before);
      assert.equal(h.render().familyRooms.length, 0);
      assert.equal(h.render().popupLoading.title, "new session work");
      if (stage === "poll") assert.equal(h.requests.filter(request => request.method === "POST").length, 1);
      if (boundary === "logout") assert.equal(h.persisted.has("ambient.roomCreateActivation"), false);
    });
  }
}

for (const binaryFails of [false, true]) {
  for (const boundary of ['room switch', 'logout']) {
    test(`media ${binaryFails ? 'failed' : 'successful'} binary refresh discards late list after ${boundary}`, async () => {
      const gate = deferred();
      const h = harness({uploadGate: gate, binaryFails});
      h.state.set('familyRooms', [{id: 'room-a'}, {id: 'room-b'}]);
      h.state.set('activeFamilyId', 'room-a');
      await h.render().handleUploadPress('video');
      const pending = h.render().handleAddRecord();
      await until(() => h.requests.some(request => request.uploadListRoom === 'room-a'));
      if (boundary === 'room switch') h.render().handleSelectFamily('room-b');
      else await h.render().handleLogout();
      h.render();
      const boundaryChange = h.changes.length;
      gate.resolve();
      await pending;
      assert.equal(h.state.get('records').length, 0);
      assert.equal(h.changes.slice(boundaryChange).some(([name, value]) => name === 'records' && value.some(item => item.id === 'old-upload')), false);
    });
  }
}

test('failed binary exposes same-record retry and settles upload loading', async () => {
  const h = harness({binaryFails: true});
  h.state.set('familyRooms', [{id: 'room-a'}]);
  h.state.set('activeFamilyId', 'room-a');
  await h.render().handleUploadPress('video');
  await h.render().handleAddRecord();
  assert.equal(h.state.get('uploadSaving'), false);
  assert.equal(h.render().hasPendingMediaUpload({id: 'old-upload'}), true);
  h.allowBinary();
  await h.render().onRetryMediaUpload({id: 'old-upload'});
  assert.equal(h.requests.filter(r => r.metadata).length, 1);
  assert.deepEqual(h.requests.filter(r => r.binary).map(r => r.binary), ['old-upload', 'old-upload']);
  assert.equal(h.render().hasPendingMediaUpload({id: 'old-upload'}), false);
  assert.equal(h.state.get('uploadSaving'), false);
});
