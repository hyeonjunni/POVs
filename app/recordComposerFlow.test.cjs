const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const { createRequire } = require("node:module");
const babel = require("@babel/core");

const filename = path.join(__dirname, "src/navigation/useAppController.js");
// Label state slots during compilation, as in demoController.test.cjs. Tests
// execute the real handlers and upload helper; no assertions inspect source text.
const code = babel.transformSync(fs.readFileSync(filename, "utf8"), {
  filename, configFile: false, babelrc: false,
  plugins: [function ({ types }) {
    return { visitor: { CallExpression(p) {
      if (p.node.callee.name === "useState" && p.parent.id?.type === "ArrayPattern") {
        p.node.arguments.push(types.stringLiteral(p.parent.id.elements[0].name));
      }
    } } };
  }, "@babel/plugin-transform-modules-commonjs"],
}).code;

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}

function harness() {
  const calls = [], released = [], rows = [], slots = [];
  const state = new Map(Object.entries({
    user: { id: "owner" }, familyRooms: [{ id: "room-1" }, { id: "room-2" }],
    activeFamilyId: "room-1", storageLoaded: true,
  }));
  const assets = {
    voice: { uri: "blob:voice-selected", fileName: "memory.mp3", mimeType: "audio/mpeg", fileSize: 5, file: new Blob(["sound"], { type: "audio/mpeg" }) },
    image: { uri: "file:///photo.jpg", fileName: "photo.jpg", mimeType: "image/jpeg", fileSize: 5 },
    video: { uri: "file:///clip.mp4", fileName: "clip.mp4", mimeType: "video/mp4", fileSize: 5 },
  };
  let cursor = 0, effects = [], dirty = false, accessToken = "owner-token", baseUrl = "https://api.example/api/v1";
  let failBinary = false, createWait = null, pickerWait = null, textFailure = null;
  const changed = (before, after) => !before || !after || before.length !== after.length || after.some((value, index) => !Object.is(value, before[index]));
  const react = {
    useState(initial, name) {
      if (!state.has(name)) state.set(name, typeof initial === "function" ? initial() : initial);
      return [state.get(name), next => {
        const value = typeof next === "function" ? next(state.get(name)) : next;
        if (!Object.is(value, state.get(name))) { state.set(name, value); dirty = true; }
      }];
    },
    useRef(initial) { const index = cursor++; return slots[index] ||= { current: initial }; },
    useMemo(factory, deps) {
      const index = cursor++;
      if (!slots[index] || changed(slots[index].deps, deps)) slots[index] = { value: factory(), deps };
      return slots[index].value;
    },
    useEffect(effect, deps) {
      const index = cursor++;
      if (slots[index] && !changed(slots[index].deps, deps)) return;
      const cleanup = slots[index]?.cleanup;
      slots[index] = { deps };
      effects.push(() => { cleanup?.(); slots[index].cleanup = effect(); });
    },
  };
  const uploads = {
    async create(payload) {
      calls.push(["create", { ...payload }]);
      const row = { ...payload, upload_id: `record-${rows.length + 1}`, has_file: false };
      rows.push(row);
      if (createWait) await createWait.promise;
      return { ...row };
    },
    async list(roomId) { calls.push(["reconcile", roomId]); return rows.filter(row => row.room_id === roomId).map(row => ({ ...row })); },
    async uploadBinary(id, asset) {
      calls.push(["binary", id, asset]);
      if (failBinary) { failBinary = false; throw new Error("connection lost"); }
      const row = rows.find(row => row.upload_id === id);
      Object.assign(row, { has_file: true, file_id: `file-${id}`, file_url: `${baseUrl}/uploads/${id}/content` });
      return { ...row };
    },
  };
  const runtime = {
    getRuntimeApiBaseUrl: () => baseUrl, getRuntimeAccessToken: () => accessToken,
    setRuntimeApiBaseUrl: value => (baseUrl = value), setRuntimeAccessToken: value => { accessToken = value; },
    setRuntimeUnauthorizedHandler() {},
    getReadableErrorMessage: (error, fallback) => error?.message || fallback,
    processingApi: {}, runtimeApiClient: {}, uploadsApi: uploads, recordUploadsApi: uploads, recordsApi: {},
    async fetchUploads(roomId) { return rows.filter(row => row.room_id === roomId).map(row => ({ ...row })); },
    async createNativeTextRecord(payload) {
      calls.push(["text", { ...payload }]);
      if (textFailure) throw textFailure;
    },
  };
  const mocks = {
    react,
    "../shared/ui/layout": { useAndroidBottomInset: () => 0, useKeyboardInset: () => 0 },
    "../features/records/recordHelpers": {
      initialItems: [], uploadTypes: ["image", "video", "voice", "text"].map(key => ({ key, label: key })),
      mapUploadToRecord: row => ({ ...row, id: row.upload_id, hasFile: row.has_file, fileId: row.file_id, fileUrl: row.file_url }),
      buildDefaultTagsForType: type => [type], parseTagInput: value => value.split(",").map(tag => tag.trim()).filter(Boolean),
    },
    "../shared/api/runtime": runtime,
    "../features/profile/utils": { buildProfileDraft: () => ({}), isProfileComplete: () => true, isProfileFormComplete: () => true },
    // Keep persisted-session bootstrap dormant. This fixture already has an
    // established account and room, without reading a real user's local data.
    "@react-native-async-storage/async-storage": { getItem: () => new Promise(() => {}), async removeItem() {}, async setItem() {}, async multiRemove() {} },
    "../features/auth/mappers": { mapBackendUser: value => value },
    "../shared/storage/keys": { STORAGE_KEYS: {} },
    "../features/family/relations": { mapFamilyRoom: value => value },
    "../../authSession.cjs": { createAuthSession: () => ({ clear: async () => { accessToken = null; } }) },
    "../../activation.cjs": { createActivationCoordinator: () => ({ clear: async () => {}, invalidate() {} }), createActivationKey: () => "key" },
    "expo-crypto": { randomUUID: () => "request-key" },
    "expo-image-picker": {
      requestMediaLibraryPermissionsAsync: async () => ({ granted: true }),
      launchImageLibraryAsync: async options => ({ canceled: false, assets: [assets[options.mediaTypes[0] === "images" ? "image" : "video"]] }),
    },
    "../features/records/voicePicker": {
      pickVoiceFile: async () => { if (pickerWait) await pickerWait.promise; return assets.voice; },
      releaseVoiceFile: asset => released.push(asset.uri),
    },
  };
  const module = { exports: {} }, realRequire = createRequire(filename);
  vm.runInNewContext(code, {
    module, exports: module.exports, require: id => mocks[id] || realRequire(id), console,
    setTimeout, clearTimeout, setInterval: () => 1, clearInterval() {},
  });
  function render() {
    let result;
    for (let iteration = 0; iteration < 12; iteration++) {
      dirty = false; cursor = 0; effects = [];
      result = module.exports.useAppController();
      effects.forEach(effect => effect());
      if (!dirty) return result;
    }
    throw new Error("Controller effects did not settle");
  }
  render();
  return {
    render, state, calls, rows, released, assets,
    failNextBinary() { failBinary = true; },
    deferCreate() { createWait = deferred(); return () => createWait.resolve(); },
    deferPicker() { pickerWait = deferred(); return () => pickerWait.resolve(); },
    changeToken() { accessToken = "other-token"; },
    changeServer() { baseUrl = "https://other.example/api/v1"; },
    failTextPublication() { textFailure = Object.assign(new Error("source publication lost"), { savedRecordId: "text-existing" }); },
  };
}

async function compose(h, type = "voice") {
  await h.render().handleUploadPress(type);
  const app = h.render();
  assert.equal(app.modalVisible, true);
  app.setFormTitle("Family memory"); app.setFormDetail("Private notes");
  app.setFormTags("family, memory"); app.setFormVisibility("PRIVATE");
  return h.render();
}

test("new personal text defaults to PRIVATE and reopening resets a prior FAMILY choice", async () => {
  const h = harness();
  await h.render().handleUploadPress("text");
  let app = h.render();
  assert.equal(app.formVisibility, "PRIVATE");
  app.setFormVisibility("FAMILY");
  h.render().closeUploadModal();
  await h.render().handleUploadPress("text");
  app = h.render();
  assert.equal(app.formVisibility, "PRIVATE");
  app.setFormTitle("내 일기"); app.setFormDetail("오늘의 생각");
  await h.render().handleAddRecord();
  assert.equal(h.calls.find(call => call[0] === "text")[1].visibility, "PRIVATE");
});

test("personal text respects an explicit family sharing selection", async () => {
  const h = harness();
  await h.render().handleUploadPress("text");
  const app = h.render();
  app.setFormTitle("함께 볼 메모"); app.setFormDetail("이번 주 가족 일정"); app.setFormVisibility("FAMILY");
  await h.render().handleAddRecord();
  assert.equal(h.calls.find(call => call[0] === "text")[1].visibility, "FAMILY");
});

test("event catalogue has its own group and does not count toward text records", () => {
  const h = harness();
  h.state.set("records", [{id: "event", type: "text", displayType: "event"}, {id: "diary", type: "text"}]);
  const groups = h.render().groupedRecords;
  assert.equal(groups.find(group => group.key === "text").items.length, 1);
  assert.equal(groups.find(group => group.key === "event").items[0].id, "event");
});

for (const type of ["voice", "image", "video"]) {
  test(`${type} picker opens a composer; only save creates PRIVATE metadata and confirmed binary`, async () => {
    const h = harness();
    const app = await compose(h, type);
    assert.equal(h.calls.length, 0);
    assert.equal(app.selectedAsset.uri, h.assets[type].uri);
    await app.handleAddRecord();
    assert.equal(h.calls[0][0], "create");
    assert.equal(h.calls[0][1].type, type);
    assert.equal(h.calls[0][1].visibility, "PRIVATE");
    assert.equal(h.calls[0][1].room_id, "room-1");
    assert.equal(h.calls[0][1].title, "Family memory");
    assert.equal(h.calls[1][0], "binary");
    const saved = h.render();
    assert.equal(saved.modalVisible, false);
    assert.equal(saved.selectedAsset, null);
    assert.equal(saved.records[0].hasFile, true);
    saved.handleOpenMedia(saved.records[0]);
    assert.equal(h.render().viewedRecord.id, "record-1");
  });
}

test("binary failure closes composer and retries the same record without duplicate metadata", async () => {
  const h = harness(); h.failNextBinary();
  await (await compose(h)).handleAddRecord();
  let app = h.render();
  assert.equal(app.modalVisible, false);
  assert.equal(app.hasPendingMediaUpload(app.records[0]), true);
  assert.equal(h.released.length, 0, "retry must retain the selected web file");
  await app.onRetryMediaUpload(app.records[0]);
  app = h.render();
  assert.equal(h.calls.filter(call => call[0] === "create").length, 1);
  assert.deepEqual(h.calls.filter(call => call[0] === "binary").map(call => call[1]), ["record-1", "record-1"]);
  assert.deepEqual(h.calls.filter(call => call[0] === "reconcile").map(call => call[1]), ["room-1"]);
  assert.equal(app.hasPendingMediaUpload(app.records[0]), false);
  assert.equal(app.records[0].hasFile, true);
  assert.ok(h.released.includes(h.assets.voice.uri));
});

test("binary timeout reconciliation does not retransmit an already stored file", async () => {
  const h = harness(); h.failNextBinary();
  await (await compose(h)).handleAddRecord();
  Object.assign(h.rows[0], { has_file: true, file_id: "committed-after-timeout" });
  await h.render().onRetryMediaUpload(h.render().records[0]);
  assert.equal(h.calls.filter(call => call[0] === "create").length, 1);
  assert.equal(h.calls.filter(call => call[0] === "binary").length, 1);
  assert.equal(h.render().records[0].fileId, "committed-after-timeout");
});

test("room change discards an open voice draft and stale save cannot upload into another room", async () => {
  const h = harness(), stale = await compose(h);
  stale.handleSelectFamily("room-2");
  const current = h.render();
  assert.equal(current.modalVisible, false);
  assert.equal(current.selectedAsset, null);
  assert.equal(current.formTitle, "");
  await stale.handleAddRecord();
  assert.equal(h.calls.filter(call => call[0] === "create").length, 0);
  assert.ok(h.released.includes(h.assets.voice.uri));
});

for (const change of ["changeToken", "changeServer"]) {
  test(`${change} prevents saving a draft prepared for the earlier credentials`, async () => {
    const h = harness(), app = await compose(h);
    h[change]();
    await app.handleAddRecord();
    assert.equal(h.calls.filter(call => call[0] === "create").length, 0);
    assert.equal(h.render().modalVisible, false);
  });
  test(`${change} while metadata is pending prevents the binary request`, async () => {
    const h = harness(), finish = h.deferCreate(), app = await compose(h);
    const saving = app.handleAddRecord();
    await Promise.resolve();
    h[change](); finish();
    await saving;
    assert.equal(h.calls.filter(call => call[0] === "binary").length, 0);
  });
}

test("logging out during the picker discards its late file without opening a composer", async () => {
  const h = harness(), finish = h.deferPicker();
  const picking = h.render().handleUploadPress("voice");
  await h.render().handleLogout(); h.render();
  finish(); await picking;
  assert.equal(h.render().modalVisible, false);
  assert.equal(h.render().selectedAsset, null);
  assert.ok(h.released.includes(h.assets.voice.uri));
  assert.equal(h.calls.length, 0);
});

test("logging out discards a pending voice retry and releases its selected blob", async () => {
  const h = harness(); h.failNextBinary();
  await (await compose(h)).handleAddRecord();
  const stale = h.render(), record = stale.records[0];
  assert.equal(stale.hasPendingMediaUpload(record), true);
  await stale.handleLogout(); h.render();
  await stale.onRetryMediaUpload(record);
  assert.equal(h.calls.filter(call => call[0] === "binary").length, 1);
  assert.ok(h.released.includes(h.assets.voice.uri));
});

test("a saved text record with failed source publication closes the composer and offers recovery", async () => {
  const h = harness(); h.failTextPublication();
  const app = await compose(h, "text");
  await app.handleAddRecord();
  const current = h.render();
  assert.equal(current.modalVisible, false);
  assert.equal(current.activeTab, "storage");
  assert.equal(current.storagePreviewType, "text");
  assert.match(current.popupResult.message, /같은 기록/);
  assert.equal(h.calls.filter(call => call[0] === "text").length, 1);
});

test("rapid text save taps create only one record", async () => {
  const h = harness(), app = await compose(h, "text");
  await Promise.all([app.handleAddRecord(), app.handleAddRecord()]);
  assert.equal(h.calls.filter(call => call[0] === "text").length, 1);
});
