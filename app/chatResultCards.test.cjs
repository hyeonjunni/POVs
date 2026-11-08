const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { createRequire } = require("node:module");
const babel = require("@babel/core");

// Run the real presentation components; only native views and React state are
// substituted so these checks need no backend or device.
function harness() {
  const cache = new Map(), slots = [];
  let cursor = 0;
  const react = {
    createElement: (type, props, ...children) => ({ type, props: { ...props, children } }),
    Fragment: "Fragment",
    useState(initial) {
      const index = cursor++;
      if (!(index in slots)) slots[index] = initial;
      return [slots[index], value => { slots[index] = typeof value === "function" ? value(slots[index]) : value; }];
    },
    useRef(initial) { const index = cursor++; return slots[index] ||= { current: initial }; },
  };
  const native = { Platform: { OS: "android" }, StyleSheet: { create: value => value } };
  for (const name of ["ActivityIndicator", "KeyboardAvoidingView", "Pressable", "ScrollView", "Text", "TextInput", "View"]) native[name] = name;
  function load(filename) {
    if (cache.has(filename)) return cache.get(filename).exports;
    const module = { exports: {} }, localRequire = createRequire(filename);
    cache.set(filename, module);
    const requireMock = id => {
      if (id === "react") return react;
      if (id === "react-native") return native;
      if (id.endsWith("/ThemeProvider")) return require("./testSupport/theme.cjs");
      if (id.endsWith("/tokens")) return { colors: {}, presentationStyles: {} };
      if (id.endsWith("/presentationStyles")) return { u: {} };
      if (id.endsWith("/UiIcon")) return { UiIcon: "UiIcon" };
      if (id.startsWith(".") && !id.endsWith(".cjs")) return load(localRequire.resolve(id));
      return localRequire(id);
    };
    const code = babel.transformSync(fs.readFileSync(filename, "utf8"), {
      filename, babelrc: false, configFile: false,
      plugins: ["@babel/plugin-transform-modules-commonjs", ["@babel/plugin-transform-react-jsx", { runtime: "classic" }]],
    }).code;
    new Function("require", "module", "exports", code)(requireMock, module, module.exports);
    return module.exports;
  }
  function expand(value) {
    if (Array.isArray(value)) return value.flatMap(expand);
    if (value == null || typeof value === "boolean") return [];
    if (typeof value !== "object") return [value];
    if (typeof value.type === "function") return expand(value.type(value.props));
    return [{ ...value, props: { ...value.props, children: expand(value.props.children) } }];
  }
  const { ChatScreen } = load(path.join(__dirname, "src/features/chat/ChatScreen.js"));
  return { render(response, onOpenCitation = () => {}, options = {}) {
    cursor = 0;
    return expand(ChatScreen({ enabled: true, serviceMode: false, messages: [{ id: "q-1", query: options.query || "언제였지?", response }], onOpenCitation, ...options }));
  } };
}
const nodes = values => values.flatMap(value => typeof value === "object" ? [value, ...nodes(value.props.children)] : []);
const text = values => values.flatMap(value => typeof value === "object" ? text(value.props.children) : [value]).join(" ");
const citation = { fact_id: "fact-1", chunk_id: "chunk-1", record_id: "record-1", extractor: "AUTO_OCR", review_status: "confirmed", source_locator: { kind: "full_text" } };
const response = () => ({ gate_route: "ANSWER", answer: "서버가 보낸 답변을 그대로 표시합니다.", citations: [citation], provider_mode: "not_called", gate_action: "structured_render", structured_result: {
  route: "ANSWER", plan: { operation: "TIMELINE" }, entries: Array.from({ length: 6 }, (_, index) => ({ event_id: `event-${index}`, title: `여행 기록 ${index + 1}`, event_type: "TRIP", event_status: "COMPLETED", occurred_at: { precision: "MONTH", granularity: "MONTH", value: `202${index}-03` }, sources: [{ record_id: "record-1", source_revision: 2, record_version: 3 }], member_modes: {}, location: {} })),
} });

test("real ChatScreen keeps original answer/source callback alongside expandable timeline cards", () => {
  const h = harness(), payload = response();
  let opened;
  let tree = h.render(payload, value => { opened = value; });
  assert.match(text(tree), /서버가 보낸 답변을 그대로 표시합니다/);
  assert.match(text(tree), /기록 속 타임라인/);
  assert.match(text(tree), /2020년 3월/);
  assert.doesNotMatch(text(tree), /여행 기록 5/);
  const original = nodes(tree).find(value => value.props.accessibilityLabel === "근거 기록 1 원본 보기");
  original.props.onPress();
  assert.equal(opened, citation);
  const more = nodes(tree).find(value => value.type === "Pressable" && value.props.accessibilityState?.expanded === false);
  assert.match(text(more.props.children), /2개 더 보기/);
  more.props.onPress();
  tree = h.render(payload);
  assert.match(text(tree), /여행 기록 6/);
  assert.equal(nodes(tree).filter(value => value.props.accessibilityLabel === "근거 기록 1 원본 보기").length, 1);
});

test("real non-answer screen shows server reason without exposing stale entries or inventing facts", () => {
  const h = harness(), payload = response();
  payload.gate_route = "CLARIFY";
  payload.citations = [];
  payload.structured_result.route = "CLARIFY";
  payload.structured_result.reason = "OVERLAPPING_TIMES";
  const tree = h.render(payload);
  assert.match(text(tree), /날짜 범위가 겹쳐 순서를 확정할 수 없습니다/);
  assert.doesNotMatch(text(tree), /여행 기록 1|OVERLAPPING_TIMES/);
  assert.equal(nodes(tree).filter(value => value.props.accessibilityLabel?.includes("원본 보기")).length, 0);
});

test("service screen shows only answer and requested unique media through the protected callback", () => {
  const h = harness(), payload = response();
  payload.query_context = { scope: "ROOM", viewer_name: "테스트 사용자", records: [{ record_id: "record-1", title: "여행 사진", record_type: "image" }] };
  payload.citations.push({ ...citation, chunk_id: "chunk-2" });
  let opened;
  const tree = h.render(payload, value => { opened = value; }, { serviceMode: true, query: "여행 사진 보여줘" });
  assert.match(text(tree), /서버가 보낸 답변을 그대로 표시합니다/);
  assert.doesNotMatch(text(tree), /근거 기록|자동 추출|원문 문자|질문 범위|기록 속 타임라인/);
  const actions = nodes(tree).filter(value => value.props.accessibilityLabel === "여행 사진 사진 보기");
  assert.equal(actions.length, 1);
  actions[0].props.onPress();
  assert.equal(opened, citation);
  const plain = h.render(payload, undefined, { serviceMode: true, query: "언제 여행 갔어?" });
  assert.doesNotMatch(text(plain), /사진 보기|여행 사진/);
  payload.gate_route = "NO_RECORD";
  const missing = h.render(payload, undefined, { serviceMode: true, query: "사진 보여줘" });
  assert.doesNotMatch(text(missing), /사진 보기|처리 상태|근거 기록/);
});

test("service mode keeps new topic reset available without developer context controls", () => {
  let resets = 0;
  const tree = harness().render(response(), undefined, { serviceMode: true, onNewConversation: () => { resets++; }, onChangeContextMode() {} });
  assert.doesNotMatch(text(tree), /전체 기록에서 새 질문|이전 답변에 이어서/);
  const reset = nodes(tree).find(value => value.props.accessibilityLabel === "새 대화 시작");
  assert.match(text(reset.props.children), /새 주제/);
  reset.props.onPress();
  assert.equal(resets, 1);
});
