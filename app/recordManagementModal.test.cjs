const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { createRequire } = require("node:module");
const babel = require("@babel/core");

const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const words = values => values.flatMap(value => typeof value === "object" ? words(value.props.children) : [value]).join("").trim();
const nodes = values => values.flatMap(value => typeof value === "object" ? [value, ...nodes(value.props.children)] : []);

// Run production JSX + preparation flow with isolated state per component.
// The only external effects replaced here are API transport and random UUIDs.
function harness(runtime) {
  const modules = new Map(), frames = new Map(), effects = [];
  let current = null, dirty = false, alive = true, props, tree, uuid = 0;
  function hook() { const frame = current, index = frame.cursor++; return [frame, index]; }
  const react = {
    createElement(type, props, ...children) { return { type, props: { ...props, children } }; },
    Fragment: "Fragment",
    useRef(initial) { const [frame, index] = hook(); return frame.slots[index] ||= { current: initial }; },
    useState(initial) {
      const [frame, index] = hook();
      if (!(index in frame.slots)) frame.slots[index] = typeof initial === "function" ? initial() : initial;
      return [frame.slots[index], value => {
        if (!alive || !frame.active) return;
        const next = typeof value === "function" ? value(frame.slots[index]) : value;
        if (!Object.is(next, frame.slots[index])) { frame.slots[index] = next; dirty = true; }
      }];
    },
    useMemo(factory, deps) {
      const [frame, index] = hook(), previous = frame.slots[index];
      if (!previous || !deps || deps.some((item, index) => !Object.is(item, previous.deps[index]))) frame.slots[index] = { deps, value: factory() };
      return frame.slots[index].value;
    },
    useEffect(effect, deps) {
      const [frame, index] = hook(), previous = frame.slots[index];
      if (!previous || !deps || deps.some((item, index) => !Object.is(item, previous.deps[index]))) {
        frame.slots[index] = { deps, cleanup: previous?.cleanup };
        effects.push(() => { frame.slots[index].cleanup?.(); frame.slots[index].cleanup = effect(); });
      }
    },
  };
  const native = { Platform: { OS: "android" }, StyleSheet: { create: value => value } };
  for (const name of ["Modal", "View", "Text", "TextInput", "ScrollView", "Pressable", "ActivityIndicator"]) native[name] = name;
  function load(filename) {
    if (modules.has(filename)) return modules.get(filename).exports;
    const module = { exports: {} }, localRequire = createRequire(filename);
    modules.set(filename, module);
    const mockedRequire = id => {
      if (id === "react") return react;
      if (id === "react-native") return native;
      if (id === "expo-crypto") return { randomUUID: () => `test-action-${++uuid}` };
      if (id.endsWith("/shared/api/runtime")) return runtime;
      if (id.endsWith("/ThemeProvider")) return require("./testSupport/theme.cjs");
      if (id.endsWith("/tokens")) return { colors: {} };
      if (id.startsWith(".") && !id.endsWith(".cjs")) return load(localRequire.resolve(id));
      return localRequire(id);
    };
    const code = babel.transformSync(fs.readFileSync(filename, "utf8"), {
      filename, babelrc: false, configFile: false,
      plugins: ["@babel/plugin-transform-modules-commonjs", ["@babel/plugin-transform-react-jsx", { runtime: "classic" }]],
    }).code;
    new Function("require", "module", "exports", code)(mockedRequire, module, module.exports);
    return module.exports;
  }
  function expand(value, location, visited) {
    if (Array.isArray(value)) return value.flatMap((child, index) => expand(child, `${location}/${index}`, visited));
    if (value == null || typeof value === "boolean") return [];
    if (typeof value !== "object") return [value];
    if (typeof value.type === "function") {
      const key = `${location}/${value.type.name}:${value.props.key ?? ""}`;
      const frame = frames.get(key) || { slots: [], cursor: 0, active: true };
      frames.set(key, frame); visited.add(key); frame.cursor = 0; frame.active = true;
      const previous = current; current = frame;
      const rendered = value.type(value.props); current = previous;
      return expand(rendered, key, visited);
    }
    return [{ ...value, props: { ...value.props, children: expand(value.props.children, `${location}/${value.type}`, visited) } }];
  }
  const { RecordManagementModal } = load(path.join(__dirname, "src/features/records/RecordManagementModal.js"));
  function render(next = props) {
    assert.ok(alive, "cannot render an unmounted tree");
    props = next;
    let attempts = 0;
    do {
      dirty = false;
      const visited = new Set();
      tree = expand(react.createElement(RecordManagementModal, props), "root", visited);
      for (const [key, frame] of frames) if (!visited.has(key) && frame.active) { frame.active = false; for (const slot of frame.slots) slot?.cleanup?.(); }
      while (effects.length) effects.shift()();
      assert.ok(++attempts < 25, "render should settle");
    } while (dirty);
    return tree;
  }
  return {
    render,
    async settle() { for (let i = 0; i < 6; i++) { await new Promise(resolve => setImmediate(resolve)); if (alive && dirty) render(); } return tree; },
    all() { return nodes(render()); },
    text() { return words(render()); },
    button(label) { const result = nodes(render()).find(value => value.type === "Pressable" && words(value.props.children) === label); assert.ok(result, `button exists: ${label}`); return result; },
    field(label) { const result = nodes(render()).find(value => value.type === "TextInput" && value.props.accessibilityLabel === label); assert.ok(result, `field exists: ${label}`); return result; },
    confirm() { const checkbox = nodes(render()).find(value => value.props.accessibilityRole === "checkbox"); assert.ok(checkbox); assert.equal(checkbox.props.disabled, false); checkbox.props.onPress(); render(); },
    async press(label) { const button = this.button(label); assert.equal(button.props.disabled, false, `${label} must be enabled`); await button.props.onPress(); await this.settle(); },
    unmount() { for (const frame of frames.values()) if (frame.active) { frame.active = false; for (const slot of frame.slots) slot?.cleanup?.(); } alive = false; },
  };
}

function fixture() {
  const calls = [], record = { id: "record-1", title: "원래 제목", detail: "직접 작성한 텍스트", type: "text", room_id: "room-1", visibility: "PRIVATE", lifecycle_state: "ACTIVE", review_status: "PENDING_REVIEW", record_version: 1, hasFile: true, fileId: "file-1", metadata: {}, capabilities: { can_read: true, can_edit_content: true, can_edit_metadata: true, can_change_visibility: true } };
  let detail = { ...record, record_id: record.id, description: record.detail }, jobStatus = "PENDING", reviewStatus = "unreviewed", token = "local-test-token";
  const job = () => ({ job_id: "job-1", record_id: record.id, room_id: record.room_id, upload_file_id: record.fileId, processor: "text", status: jobStatus, attempt: 0, result: jobStatus === "SUCCEEDED" ? { text: "서버가 추출한 실제 검토 내용" } : null });
  const review = () => ({ job_id: "job-1", source_attempt: 0, source_result_sha256: "a".repeat(64), review_status: reviewStatus, user_confirmed: reviewStatus === "confirmed" });
  const runtime = {
    getRuntimeAccessToken: () => token, getRuntimeApiBaseUrl: () => "http://backend.invalid/api/v1",
    recordsApi: {
      async get(id) { calls.push(["get", id]); return { ...detail }; },
      async approveReview(id, value) { calls.push(["approveReview", id, value]); detail = { ...detail, review_status: "APPROVED", record_version: detail.record_version + 1 }; return { ...detail }; },
      async patchMetadata(id, value) { calls.push(["patchMetadata", id, value]); detail = { ...detail, title: value.changes.title || detail.title, metadata: { ...detail.metadata, ...value.changes }, record_version: detail.record_version + 1 }; return { ...detail }; },
      async listMetadataRevisions(id) { calls.push(["listMetadataRevisions", id]); return []; },
      async changeVisibility(id, value) { calls.push(["changeVisibility", id, value]); detail = { ...detail, visibility: value.visibility, record_version: detail.record_version + 1 }; return { ...detail }; },
    },
    uploadsApi: { async publishText(id) { calls.push(["publishText", id]); return { upload_id: id, has_file: true, file_id: "file-1" }; } },
    processingApi: {
      async create(id, value) { calls.push(["createJob", id, value]); return job(); },
      async get(id) { calls.push(["getJob", id]); return job(); },
      async run(id) { calls.push(["runJob", id]); jobStatus = "SUCCEEDED"; return job(); },
      async retry(id) { calls.push(["retryJob", id]); jobStatus = "PENDING"; return job(); },
      async getReview(id) { calls.push(["getReview", id]); return review(); },
      async review(id, action) { calls.push(["reviewJob", id, action]); reviewStatus = action === "approve" ? "confirmed" : "rejected"; return review(); },
      async index(id, jobId) { calls.push(["index", id, jobId]); return { source_attempt: 0, source_result_sha256: "a".repeat(64), chunk_count: 1, embedding_count: 1 }; },
    },
  };
  return { calls, record, runtime, detail: () => ({ ...detail }), setDetail: value => { detail = { ...detail, ...value }; }, setToken: value => { token = value; },
    mount() { const h = harness(runtime); h.render({ record, roomId: "room-1", onClose: () => {}, onChanged: async () => calls.push(["changed"]) }); return h; } };
}
const mutationNames = new Set(["approveReview", "publishText", "patchMetadata", "changeVisibility", "createJob", "runJob", "retryJob", "reviewJob", "index"]);
const mutations = calls => calls.filter(([name]) => mutationNames.has(name));

test("owner can switch PRIVATE to FAMILY and back with current record versions", async () => {
  const f = fixture(), h = f.mount();
  await h.settle();
  assert.equal(h.button("공개 범위 저장").props.disabled, true);
  await h.press("가족 전체");
  await h.press("공개 범위 저장");
  assert.equal(f.detail().visibility, "FAMILY");
  await h.press("나만 보기");
  await h.press("공개 범위 저장");
  assert.equal(f.detail().visibility, "PRIVATE");
  const calls = f.calls.filter(([name]) => name === "changeVisibility");
  assert.deepEqual(calls.map(([, , value]) => [value.visibility, value.recordVersion]), [["FAMILY", 1], ["PRIVATE", 2]]);
  assert.notEqual(calls[0][2].idempotencyKey, calls[1][2].idempotencyKey);
  assert.equal(f.calls.filter(([name]) => name === "changed").length, 2);
  h.unmount();
});

test("non-owner cannot access visibility mutation controls", async () => {
  const f = fixture();
  f.setDetail({ capabilities: { can_read: true, can_change_visibility: false } });
  const h = f.mount(); await h.settle();
  assert.equal(h.all().some(value => value.props.accessibilityRole === "radio"), false);
  assert.deepEqual(mutations(f.calls), []);
  h.unmount();
});

test("management modal mount reads only and requires explicit content/audience confirmation before approval", async () => {
  const f = fixture(), h = f.mount();
  await h.settle();
  assert.deepEqual(f.calls, [["get", "record-1"]]);
  assert.match(h.text(), /원래 제목/);
  assert.match(h.text(), /직접 작성한 텍스트/);
  assert.match(h.text(), /나만 보기/);
  assert.equal(h.button("내용 승인·분석 시작").props.disabled, true);
  h.confirm();
  assert.equal(h.button("내용 승인·분석 시작").props.disabled, false);
  assert.deepEqual(mutations(f.calls), []);
  h.unmount();
});

test("load permission errors are visible and no mutation controls or editor are exposed", async () => {
  const f = fixture();
  f.runtime.recordsApi.get = async id => { f.calls.push(["get", id]); throw Object.assign(new Error("server rejected"), { status: 403 }); };
  const h = f.mount(); await h.settle();
  assert.match(h.text(), /권한이 없거나 기록에 더 이상 접근할 수 없습니다/);
  assert.equal(h.all().some(value => value.type === "TextInput"), false);
  assert.equal(h.all().some(value => value.props.accessibilityRole === "checkbox"), false);
  assert.deepEqual(mutations(f.calls), []);
  h.unmount();
});

test("confirmed preparation exposes actual extracted text and separates review approval from indexing", async () => {
  const f = fixture(), h = f.mount(); await h.settle(); h.confirm();
  await h.press("내용 승인·분석 시작");
  assert.deepEqual(mutations(f.calls).map(([name]) => name), ["approveReview", "createJob", "runJob"]);
  assert.match(h.text(), /서버가 추출한 실제 검토 내용/);
  assert.equal(h.button("분석 결과 승인·검색 준비").props.disabled, false);
  assert.equal(f.calls.some(([name]) => name === "index"), false);
  await h.press("분석 결과 승인·검색 준비");
  assert.deepEqual(mutations(f.calls).map(([name]) => name), ["approveReview", "createJob", "runJob", "reviewJob", "index"]);
  assert.match(h.text(), /검색·대화에 사용할 준비가 완료되었습니다/);
  assert.doesNotMatch(h.text(), /검색 준비 다시 확인/);
  h.unmount();
});

test("metadata 412 displays conflict and preserves title/reason draft through refresh and retry", async () => {
  const f = fixture(), originalSave = f.runtime.recordsApi.patchMetadata;
  let conflict = true;
  f.runtime.recordsApi.patchMetadata = async (id, value) => {
    if (conflict) { f.calls.push(["patchMetadata", id, value]); throw Object.assign(new Error("revision changed"), { status: 412 }); }
    return originalSave(id, value);
  };
  const h = f.mount(); await h.settle();
  h.field("제목").props.onChangeText("내가 편집한 제목");
  h.field("기록 정보 변경 사유").props.onChangeText("제목 오탈자 수정");
  await h.press("정보 저장");
  assert.match(h.text(), /다른 곳에서 기록이 변경되었습니다/);
  assert.equal(h.field("제목").props.value, "내가 편집한 제목");
  assert.equal(h.field("기록 정보 변경 사유").props.value, "제목 오탈자 수정");
  f.setDetail({ title: "다른 기기의 제목", record_version: 4 });
  await h.press("최신 상태 불러오기");
  assert.equal(h.field("제목").props.value, "내가 편집한 제목");
  assert.equal(h.field("기록 정보 변경 사유").props.value, "제목 오탈자 수정");
  conflict = false;
  await h.press("정보 저장");
  const saves = f.calls.filter(([name]) => name === "patchMetadata");
  assert.equal(saves.length, 2);
  assert.equal(saves[0][2].recordVersion, 1);
  assert.equal(saves[1][2].recordVersion, 4);
  assert.deepEqual(saves[1][2].changes, { title: "내가 편집한 제목" });
  assert.equal(h.field("기록 정보 변경 사유").props.value, "");
  h.unmount();
});

test("closing while fresh record GET is pending prevents subsequent approval and processing POSTs", async () => {
  const f = fixture(), h = f.mount(); await h.settle(); h.confirm();
  const fresh = deferred();
  f.runtime.recordsApi.get = id => { f.calls.push(["get", id]); return fresh.promise; };
  const action = h.button("내용 승인·분석 시작").props.onPress();
  h.unmount();
  fresh.resolve(f.detail());
  await action;
  assert.deepEqual(mutations(f.calls), []);
  assert.equal(f.calls.some(([name]) => name === "changed"), false);
});

test("session change while approval is pending prevents the next processing POST", async () => {
  const f = fixture(), h = f.mount(); await h.settle(); h.confirm();
  const approval = deferred();
  f.runtime.recordsApi.approveReview = (id, value) => { f.calls.push(["approveReview", id, value]); return approval.promise; };
  const action = h.button("내용 승인·분석 시작").props.onPress();
  await h.settle();
  assert.equal(f.calls.some(([name]) => name === "approveReview"), true);
  f.setToken("different-user-token");
  approval.resolve({ ...f.detail(), review_status: "APPROVED", record_version: 2 });
  await action; await h.settle();
  assert.deepEqual(mutations(f.calls).map(([name]) => name), ["approveReview"]);
  assert.equal(f.calls.some(([name]) => name === "changed"), false);
  h.unmount();
});
