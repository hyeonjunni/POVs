const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const { createRequire } = require("node:module");
const babel = require("@babel/core");

const filename = path.join(__dirname, "src/navigation/useAppController.js");
// Name state slots in the test transform so setup survives unrelated hook additions.
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

function timeout() {
  return Object.assign(new Error("요청 시간이 초과되었습니다. 서버 처리 상태를 확인해주세요."), { code: "REQUEST_TIMEOUT" });
}

function harness({ failingStage = null } = {}) {
  const events = [];
  const changes = [];
  const record = { id: "upload-1", upload_id: "upload-1", type: "image", title: "photo", hasFile: true,
    room_id: "room-1", fileId: "file-1", record_version: 1, visibility: "FAMILY", review_status: "APPROVED" };
  const job = { job_id: "job-original", status: "SUCCEEDED", record_id: record.id, room_id: "room-1",
    upload_file_id: "file-1", processor: "ocr", attempt: 0, result: { text: "recognized text" } };
  let reviewStatus = "unreviewed";
  let pendingGet = null;
  let pendingIndex = null;
  let indexResult = { chunk_count: 2, embedding_count: 2 };
  let userConfirmed = true;
  let effects = [];
  let unauthorizedHandler;
  let accessToken = "token";
  const state = new Map(Object.entries({
    user: { id: "user-1" }, familyRooms: [{ id: "room-1" }], activeFamilyId: "room-1", records: [record],
  }));
  const slots = [];
  let cursor = 0;
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
    useEffect(effect) { effects.push(effect); }, // Tests opt into only the unauthorized lifecycle effect.
  };
  const processingApi = {
    async create(id, payload) {
      assert.equal(id, record.id);
      assert.deepEqual(payload, { upload_file_id: record.fileId, processor: "ocr" });
      events.push(["create", id]); if (failingStage === "create") throw timeout(); return job;
    },
    async get(id) { events.push(["get", id]); if (pendingGet) return pendingGet; if (failingStage === "get") throw timeout(); return job; },
    async run(id) { events.push(["run", id]); return job; },
    async poll(id) { events.push(["poll", id]); return job; },
    async retry(id) { events.push(["retry", id]); return job; },
    async getReview(id) {
      events.push(["getReview", id]);
      if (failingStage === "review" || (failingStage === "postWriteRead" && reviewStatus === "confirmed")) throw timeout();
      return { job_id: id, review_status: reviewStatus, user_confirmed: reviewStatus === "confirmed" && userConfirmed,
        source_attempt: 0, source_result_sha256: "fixture-result-digest" };
    },
    async review(id, action) {
      events.push(["review", id, action]);
      if (failingStage === "decision") { reviewStatus = "confirmed"; throw timeout(); }
      if (failingStage === "decision503") { reviewStatus = "confirmed"; throw Object.assign(new Error("uncertain"), { status: 503 }); }
      if (failingStage === "rejectConflict") { reviewStatus = "confirmed"; throw Object.assign(new Error("already approved"), { status: 409 }); }
      reviewStatus = action === "approve" ? "confirmed" : "rejected";
    },
    async index(uploadId, jobId) {
      events.push(["index", uploadId, jobId]);
      if (pendingIndex) return pendingIndex;
      if (failingStage === "index503") throw Object.assign(new Error("unavailable"), { status: 503 });
      if (failingStage === "indexTimeout") throw timeout();
      if (failingStage === "index") throw new Error("failed");
      return indexResult;
    },
  };
  const runtime = {
    getRuntimeApiBaseUrl: () => "http://localhost:8000", getRuntimeAccessToken: () => accessToken,
    setRuntimeAccessToken: value => { accessToken = value; },
    setRuntimeUnauthorizedHandler: handler => { unauthorizedHandler = handler; },
    getReadableErrorMessage: (error, fallback) => error?.message || fallback,
    processingApi, runtimeApiClient: {}, uploadsApi: {}, recordUploadsApi: {}, recordsApi: {},
    async fetchUploads() { events.push(["fetchUploads"]); return [record]; },
  };
  const mocks = {
    react,
    "../shared/ui/layout": { useAndroidBottomInset: () => 0, useKeyboardInset: () => 0 },
    "../features/records/recordHelpers": { initialItems: [], uploadTypes: [{ key: "image", label: "이미지" }], mapUploadToRecord: value => value, buildDefaultTagsForType: () => [], parseTagInput: () => [] },
    "../shared/api/runtime": runtime,
    "../features/profile/utils": { buildProfileDraft: () => ({}), isProfileComplete: () => true, isProfileFormComplete: () => true },
    "@react-native-async-storage/async-storage": { async removeItem() {}, async multiRemove() {} },
    "../features/auth/mappers": { mapBackendUser: value => value },
    "../shared/storage/keys": { STORAGE_KEYS: {} },
    "../features/family/relations": { mapFamilyRoom: value => value },
    "../features/records/voicePicker": { pickVoiceFile: async () => null, releaseVoiceFile() {} },
    "expo-image-picker": {
      requestMediaLibraryPermissionsAsync: async () => ({ granted: true }),
      launchImageLibraryAsync: async () => {
        if (failingStage === "picker") throw new Error("Native picker exception");
        return { canceled: false, assets: [{ uri: "file:///photo.jpg", fileName: "photo.jpg", fileSize: 10 }] };
      },
    },
    "expo-crypto": { randomUUID: () => "fixture-request-key" },
    "../../uploads.cjs": {
      ...require("./uploads.cjs"),
      async createUploadWithBinary() {
        events.push(["upload"]);
        if (failingStage === "upload") throw timeout();
        return { upload: record, uploadedFile: { file_id: "file-1" } };
      },
    },
  };
  const module = { exports: {} };
  const realRequire = createRequire(filename);
  vm.runInNewContext(code, { module, exports: module.exports, require: id => mocks[id] || realRequire(id), console, setTimeout, clearTimeout, setInterval, clearInterval });
  const render = () => { cursor = 0; effects = []; return module.exports.useAppController(); };
  return {
    events, changes, record, state,
    recover() { failingStage = null; },
    fail(stage) { failingStage = stage; },
    setIndexResult(value) { indexResult = value; },
    setUserConfirmed(value) { userConfirmed = value; },
    installUnauthorizedHandler() { effects.find(effect => effect.toString().includes("setRuntimeUnauthorizedHandler"))(); },
    unauthorized(token) { return unauthorizedHandler(token); },
    newSession() {
      accessToken = "new-token";
      reviewStatus = "unreviewed";
      state.set("user", { id: "new-user" });
      state.set("familyRooms", [{ id: "room-1" }]);
      state.set("activeFamilyId", "room-1");
    },
    deferIndex() {
      let finish;
      pendingIndex = new Promise(resolve => { finish = () => { pendingIndex = null; resolve(indexResult); }; });
      return finish;
    },
    deferGet() {
      let finish;
      pendingGet = new Promise(resolve => { finish = () => { pendingGet = null; resolve(job); }; });
      return finish;
    },
    async openLegacyReview({ withoutJob = false } = {}) {
      // A job already pending before the explicit preparation UI must remain recoverable.
      state.set("pendingOcrJobs", { [record.id]: { roomId: record.room_id,
        payload: { upload_file_id: record.fileId, processor: "ocr" }, job: withoutJob ? null : job } });
      await render().onReopenOcrReview(record);
    },
    async uploadImage() {
      await render().handleUploadPress("image");
      assert.equal(state.get("modalVisible"), true);
      await render().handleAddRecord();
    },
    render,
  };
}

test("Android Back dismissal leaves Storage reopen available for the same existing OCR job", async () => {
  const h = harness();
  await h.openLegacyReview();
  let app = h.render();
  assert.ok(app.popupConfirm);
  assert.equal(app.hasPendingOcrReview(h.record), true);
  app.closePopupConfirm();
  app = h.render();
  assert.equal(app.popupConfirm, null);
  assert.equal(app.hasPendingOcrReview(h.record), true);
  const before = h.events.length;
  await app.onReopenOcrReview(h.record);
  assert.ok(h.render().popupConfirm);
  assert.deepEqual(h.events.slice(before).filter(event => ["get", "getReview"].includes(event[0])), [["get", "job-original"], ["getReview", "job-original"]]);
  assert.equal(h.events.filter(event => event[0] === "create").length, 0);
});

for (const stage of ["upload", "create", "review"]) {
  test(`${stage} timeout releases its blocking UI and exposes same-request recovery`, async () => {
    const h = harness({ failingStage: stage });
    if (stage === "upload") await h.uploadImage();
    else await h.openLegacyReview({ withoutJob: stage === "create" });
    const app = h.render();
    if (stage === "upload") assert.ok(h.changes.some(([name, value]) => name === "uploadSaving" && value === true));
    else assert.ok(h.changes.some(([name, value]) => name === "popupLoading" && value?.title === "OCR 결과 확인"));
    assert.equal(app.uploadSaving, false);
    assert.equal(app.popupLoading, null);
    assert.ok(app.popupResult || app.popupConfirm);
    if (stage !== "upload") {
      assert.equal(app.hasPendingOcrReview(h.record), true);
      h.recover();
      await app.onReopenOcrReview(h.record);
      assert.ok(h.render().popupConfirm);
      if (stage === "review") assert.equal(h.events.filter(event => event[0] === "create").length, 0);
      if (stage === "create") assert.deepEqual(h.events.filter(event => event[0] === "create"), [["create", h.record.id], ["create", h.record.id]]);
    }
  });
}

test("decision timeout releases popup loading and retry reads completion without repeating the write", async () => {
  // Simulate a server commit whose response is lost at the client deadline.
  const decision = harness({ failingStage: "decision" });
  await decision.openLegacyReview();
  await decision.render().handlePopupConfirm();
  assert.equal(decision.render().popupLoading, null);
  const before = decision.events.length;
  decision.recover();
  await decision.render().handlePopupConfirm();
  assert.ok(decision.events.slice(before).some(event => event[0] === "getReview"));
  assert.equal(decision.events.filter(event => event[0] === "review").length, 1);
  assert.equal(decision.render().popupLoading, null);
});

test("upload timeout blocks another write until authoritative Storage reload", async () => {
  const h = harness({ failingStage: "upload" });
  await h.uploadImage();
  h.render().closePopupConfirm();
  h.recover();
  await h.render().handleUploadPress("image");
  assert.equal(h.events.filter(event => event[0] === "upload").length, 1);
  await h.render().handlePopupConfirm();
  assert.equal(h.events.at(-1)[0], "fetchUploads");
  assert.equal(h.render().popupLoading, null);
  await h.uploadImage();
  assert.equal(h.events.filter(event => event[0] === "upload").length, 2);
});

test("reopen timeout releases blocking popup and retains the same job for recovery", async () => {
  const h = harness();
  await h.openLegacyReview();
  h.render().closePopupConfirm();
  h.fail("get");
  await h.render().onReopenOcrReview(h.record);
  assert.equal(h.render().popupLoading, null);
  assert.equal(h.render().popupResult.variant, "error");
  assert.equal(h.render().hasPendingOcrReview(h.record), true);
  h.recover();
  await h.render().onReopenOcrReview(h.record);
  assert.ok(h.render().popupConfirm);
  assert.equal(h.events.filter(event => event[0] === "create").length, 0);
  assert.ok(h.events.filter(event => event[0] === "get").every(event => event[1] === "job-original"));
});

test("a deferred reopen response after logout cannot revive OCR review or pending state", async () => {
  const h = harness();
  await h.openLegacyReview();
  h.render().closePopupConfirm();
  const finish = h.deferGet();
  const reopen = h.render().onReopenOcrReview(h.record);
  assert.ok(h.render().popupLoading);
  await h.render().handleLogout();
  finish();
  await reopen;
  const app = h.render();
  assert.equal(app.popupConfirm, null);
  assert.equal(app.popupLoading, null);
  assert.equal(app.hasPendingOcrReview(h.record), false);
});

test("native picker exception reaches the controller error popup and remains retryable", async () => {
  const h = harness({ failingStage: "picker" });
  await h.render().handleUploadPress("image");
  assert.equal(h.render().popupResult.title, "파일 선택 실패");
  assert.equal(h.render().popupResult.variant, "error");
  assert.equal(h.render().uploadSaving, false);
  assert.equal(h.events.some(event => event[0] === "upload"), false);
  h.recover();
  await h.render().handleUploadPress("image");
  assert.equal(h.render().modalVisible, true);
  assert.equal(h.events.some(event => event[0] === "upload"), false, "selection alone must not upload or approve OCR");
  await h.render().handleAddRecord();
  assert.equal(h.events.filter(event => event[0] === "upload").length, 1);
  assert.equal(h.events.some(event => event[0] === "create"), false, "new uploads await explicit management approval");
});

test("post-write review read timeout recovers committed state without another decision write", async () => {
  const h = harness({ failingStage: "postWriteRead" });
  await h.openLegacyReview();
  await h.render().handlePopupConfirm();
  assert.equal(h.render().popupLoading, null);
  assert.equal(h.render().popupConfirm.confirmLabel, "다시 불러오기");
  const before = h.events.length;
  h.recover();
  await h.render().handlePopupConfirm();
  assert.ok(h.events.slice(before).some(event => event[0] === "getReview"));
  assert.equal(h.events.filter(event => event[0] === "review").length, 1);
  assert.equal(h.render().popupLoading, null);
  assert.equal(h.render().hasPendingOcrReview(h.record), false);
});

const readyMessage = '업로드 및 검색 준비가 완료되었습니다.';
const indexEvents = h => h.events.filter(event => event[0] === 'index');

test('confirmed approval indexes once with upload identity and original job then shows searchable ready', async () => {
  const h = harness();
  await h.openLegacyReview();
  assert.equal(indexEvents(h).length, 0);
  await h.render().handlePopupConfirm();
  assert.deepEqual(indexEvents(h), [['index', 'upload-1', 'job-original']]);
  assert.ok(h.events.findIndex(e => e[0] === 'index') > h.events.findLastIndex(e => e[0] === 'getReview'));
  assert.equal(h.render().popupResult.message, readyMessage);
  assert.ok(h.changes.some(([name, value]) => name === 'popupLoading' && value?.message === '검색할 수 있도록 기록을 준비하고 있습니다.'));
  assert.equal(h.render().hasPendingOcrReview(h.record), false);
});

test('reject reloads authoritative review and never indexes', async () => {
  const h = harness();
  await h.openLegacyReview();
  await h.render().handlePopupCancel();
  assert.equal(indexEvents(h).length, 0);
  assert.equal(h.events.at(-1)[0], 'getReview');
  assert.equal(h.render().popupResult.title, 'OCR 거절 완료');
});

test('confirmed status without user_confirmed true cannot index or claim searchable ready', async () => {
  const h = harness();
  h.setUserConfirmed(false);
  await h.openLegacyReview();
  await h.render().handlePopupConfirm();
  assert.equal(indexEvents(h).length, 0);
  assert.notEqual(h.render().popupResult?.message, readyMessage);
  assert.equal(h.render().hasPendingOcrReview(h.record), true);
});

for (const result of [{ chunk_count: 0, embedding_count: 1 }, { chunk_count: 1, embedding_count: 0 }, {}]) {
  test(`empty publication is not searchable ready: ${JSON.stringify(result)}`, async () => {
    const h = harness();
    h.setIndexResult(result);
    await h.openLegacyReview();
    await h.render().handlePopupConfirm();
    assert.notEqual(h.render().popupResult?.message, readyMessage);
    assert.equal(h.render().popupConfirm?.confirmLabel, '검색 준비 다시 시도');
  });
}

for (const failure of ['index', 'index503', 'indexTimeout']) {
  test(`${failure} preserves approval and retry reloads authoritative state with same identity`, async () => {
    const h = harness({ failingStage: failure });
    await h.openLegacyReview();
    await h.render().handlePopupConfirm();
    assert.notEqual(h.render().popupResult?.message, readyMessage);
    assert.equal(h.render().popupConfirm?.message, 'OCR 승인은 완료되었습니다.\n검색 준비 중 오류가 발생했습니다.');
    assert.equal(h.render().popupConfirm?.confirmLabel, '검색 준비 다시 시도');
    assert.equal(h.render().hasPendingOcrReview(h.record), true);
    const before = h.events.length;
    h.recover();
    await h.render().handlePopupConfirm();
    assert.equal(h.events[before][0], 'getReview');
    assert.deepEqual(indexEvents(h), [['index', 'upload-1', 'job-original'], ['index', 'upload-1', 'job-original']]);
    assert.equal(h.events.filter(e => e[0] === 'review').length, 1);
    assert.equal(h.events.filter(e => e[0] === 'create').length, 0);
    assert.equal(h.render().popupResult.message, readyMessage);
  });
}

test('Storage reopen after publication failure reuses approved job and prepares search', async () => {
  const h = harness({ failingStage: 'index' });
  await h.openLegacyReview();
  await h.render().handlePopupConfirm();
  h.render().closePopupConfirm();
  h.recover();
  await h.render().onReopenOcrReview(h.record);
  assert.equal(h.events.filter(e => e[0] === 'create').length, 0);
  assert.equal(h.events.filter(e => e[0] === 'review').length, 1);
  assert.deepEqual(indexEvents(h).at(-1), ['index', 'upload-1', 'job-original']);
  assert.equal(h.render().popupResult.message, readyMessage);
});

for (const boundary of ['logout', 'room']) {
  test(`${boundary} during indexing discards late completion without altering newer UI`, async () => {
    const h = harness();
    await h.openLegacyReview();
    const finish = h.deferIndex();
    const pending = h.render().handlePopupConfirm();
    for (let i = 0; i < 10 && !indexEvents(h).length; i++) await Promise.resolve();
    assert.equal(indexEvents(h).length, 1);
    if (boundary === 'logout') await h.render().handleLogout();
    else h.render().handleSelectFamily('room-2');
    h.state.set('popupLoading', { title: 'new session request' });
    const before = h.changes.length;
    finish();
    await pending;
    assert.equal(h.render().popupResult, null);
    assert.equal(h.render().popupConfirm, null);
    assert.equal(h.render().popupLoading.title, 'new session request');
    assert.equal(h.changes.length, before);
  });
}

test('rapid approval taps publish exactly once', async () => {
  const h = harness();
  await h.openLegacyReview();
  const app = h.render();
  await Promise.all([app.handlePopupConfirm(), app.handlePopupConfirm()]);
  assert.equal(h.events.filter(e => e[0] === 'review').length, 1);
  assert.equal(indexEvents(h).length, 1);
});

test('review 503 does not claim failure or publish before authoritative reload', async () => {
  const h = harness({ failingStage: 'decision503' });
  await h.openLegacyReview();
  await h.render().handlePopupConfirm();
  assert.equal(indexEvents(h).length, 0);
  assert.notEqual(h.render().popupResult?.message, readyMessage);
  const before = h.events.length;
  h.recover();
  await h.render().handlePopupConfirm();
  assert.equal(h.events[before][0], 'getReview');
  assert.equal(h.events.filter(e => e[0] === 'review').length, 1);
  assert.deepEqual(indexEvents(h), [['index', 'upload-1', 'job-original']]);
  assert.equal(h.render().popupResult.message, readyMessage);
});

test('reject conflicting with confirmed server review still makes zero index calls', async () => {
  const h = harness({ failingStage: 'rejectConflict' });
  await h.openLegacyReview();
  await h.render().handlePopupCancel();
  assert.equal(indexEvents(h).length, 0);
  assert.equal(h.events.at(-1)[0], 'getReview');
  assert.notEqual(h.render().popupResult?.message, readyMessage);
});

test('current-token 401 during index releases popup lock for the next session', async () => {
  const h = harness();
  h.render();
  h.installUnauthorizedHandler();
  await h.openLegacyReview();
  const finish = h.deferIndex();
  const pending = h.render().handlePopupConfirm();
  for (let i = 0; i < 10 && !indexEvents(h).length; i++) await Promise.resolve();
  assert.equal(indexEvents(h).length, 1);
  await h.unauthorized('token');
  finish();
  await pending;
  assert.equal(h.render().popupResult, null);
  h.newSession();
  await h.openLegacyReview();
  await h.render().handlePopupConfirm();
  assert.equal(indexEvents(h).length, 2);
  assert.equal(h.render().popupResult.message, readyMessage);
});

test('stale 401 cannot clear a newer session while index is pending', async () => {
  const h = harness();
  h.render();
  h.installUnauthorizedHandler();
  h.newSession();
  await h.openLegacyReview();
  const finish = h.deferIndex();
  const pending = h.render().handlePopupConfirm();
  for (let i = 0; i < 10 && !indexEvents(h).length; i++) await Promise.resolve();
  assert.equal(indexEvents(h).length, 1);
  await h.unauthorized('token');
  assert.equal(h.state.get('user').id, 'new-user');
  finish();
  await pending;
  assert.equal(h.render().popupResult.message, readyMessage);
});

test('ambiguous reject recovery reloads confirmed state without indexing', async () => {
  const h = harness({ failingStage: 'decision503' });
  await h.openLegacyReview();
  await h.render().handlePopupCancel();
  assert.equal(indexEvents(h).length, 0);
  h.recover();
  await h.render().handlePopupConfirm();
  assert.equal(indexEvents(h).length, 0);
  assert.equal(h.events.filter(e => e[0] === 'review').length, 1);
  assert.notEqual(h.render().popupResult?.message, readyMessage);
});
