const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { createRequire } = require('node:module');
const babel = require('@babel/core');
const { createAuthSession } = require('./authSession.cjs');

// Execute production ES modules and JSX with the installed Babel plugins. Only
// React hooks/native views are simulated; API, validation and session code run.
function harness(originalFetch = (...args) => globalThis.fetch(...args), overrides = {}) {
  const slots = [], effects = [], modules = new Map();
  let cursor = 0, dirty = false, currentHook, props, value;
  const react = {
    createElement(type, props, ...children) { return { type, props: { ...props, children } }; },
    Fragment: 'Fragment',
    useRef(initial) { const i = cursor++; return slots[i] ||= { current: initial }; },
    useState(initial) {
      const i = cursor++;
      if (!(i in slots)) slots[i] = typeof initial === 'function' ? initial() : initial;
      return [slots[i], next => { const result = typeof next === 'function' ? next(slots[i]) : next; if (!Object.is(result, slots[i])) { slots[i] = result; dirty = true; } }];
    },
    useMemo(factory, deps) {
      const i = cursor++, previous = slots[i];
      if (!previous || !deps || deps.some((item, index) => !Object.is(item, previous.deps[index]))) {
        slots[i] = { deps, value: factory() };
      }
      return slots[i].value;
    },
    useEffect(effect, deps) {
      const i = cursor++, previous = slots[i];
      if (!previous || !deps || deps.some((item, index) => !Object.is(item, previous.deps[index]))) {
        slots[i] = { deps, cleanup: previous?.cleanup };
        effects.push(() => { slots[i].cleanup?.(); slots[i].cleanup = effect(); });
      }
    },
  };
  react.useLayoutEffect = react.useEffect;
  const native = { Platform: { OS: 'android' }, StyleSheet: { create: value => value } };
  for (const name of ['ActivityIndicator', 'KeyboardAvoidingView', 'Pressable', 'ScrollView', 'Text', 'TextInput', 'View', 'Modal', 'Image', 'SafeAreaView', 'StatusBar']) native[name] = name;
  function load(relative) {
    const filename = path.resolve(__dirname, relative);
    if (modules.has(filename)) return modules.get(filename).exports;
    const module = { exports: {} }; modules.set(filename, module);
    const localRequire = createRequire(filename);
    const requireMock = id => {
      if (Object.hasOwn(overrides, id)) return overrides[id];
      if (id === 'react') return react;
      if (id === '@react-native-async-storage/async-storage') return { getItem: async () => null, setItem: async () => {} };
      if (id === 'react-native') return native;
      if (id === 'expo/fetch') return { fetch: originalFetch };
      if (id === 'expo/node_modules/expo-file-system') return { Paths: { cache: 'cache' }, File: class {
        constructor() { this.uri = 'file:///cache/citation.wav'; this.exists = false; }
        create() { this.exists = true; }
        write(bytes) { this.written = bytes; }
        delete() { this.exists = false; }
        bytes() { throw new Error('Unexpected file read in chat test'); }
      } };
      if (id === 'expo/node_modules/expo-file-system/legacy') return {};
      if (id === 'expo-crypto') return { randomUUID: () => 'unique' };
      if (id.endsWith("/ThemeProvider")) return require("./testSupport/theme.cjs");
      if (id.endsWith('/tokens')) return { colors: {}, presentationStyles: {} };
      if (id.endsWith('/presentationStyles')) return { u: {} };
      if (id.endsWith('/UiIcon')) return { UiIcon: 'UiIcon' };
      if (id.endsWith('/RecordMedia')) return { PassiveVideo: 'PassiveVideo' };
      if (id.endsWith('/RecordAudio')) return { PassiveAudio: 'PassiveAudio' };
      if (id.startsWith('.') && !id.endsWith('.cjs')) return load(path.relative(__dirname, localRequire.resolve(id)));
      return localRequire(id);
    };
    const code = babel.transformSync(fs.readFileSync(filename, 'utf8'), {
      filename, babelrc: false, configFile: false,
      plugins: ['@babel/plugin-transform-modules-commonjs', ['@babel/plugin-transform-react-jsx', { runtime: 'classic' }]],
    }).code;
    new Function('require', 'module', 'exports', '__DEV__', code)(requireMock, module, module.exports, false);
    return module.exports;
  }
  function render(nextProps = props) {
    props = nextProps;
    let attempts = 0;
    do {
      dirty = false; cursor = 0; value = currentHook(props);
      while (effects.length) effects.shift()();
      assert.ok(++attempts < 20, 'hook render must settle');
    } while (dirty);
    return value;
  }
  return {
    load, native,
    mount(hook, initialProps) { currentHook = hook; return render(initialProps); },
    render,
    unmount() { for (const slot of slots) slot?.cleanup?.(); },
    expand(node) {
      if (Array.isArray(node)) return node.flatMap(child => this.expand(child));
      if (node == null || typeof node === 'boolean') return [];
      if (typeof node !== 'object') return [node];
      if (typeof node.type === 'function') return this.expand(node.type(node.props));
      return [{ ...node, props: { ...node.props, children: this.expand(node.props.children) } }];
    },
  };
}
const baseUrl = 'https://backend.example/api/v1';
function citation() {
  const hash = 'a'.repeat(64);
  return { fact_id: 'fact-A', chunk_id: 'chunk-A', record_id: 'record-A', file_id: 'file-A',
    chunk_char_start: 2, chunk_char_end: 32, source_locator: { kind: 'ocr_regions', indexes: [0, 2] },
    extractor: 'AUTO_OCR', extractor_id: 'tesseract:v1', review_status: 'confirmed', user_confirmed: true,
    evidence_status: 'extracted', source_object_id: 'object-A', source_object_version: 3, source_ciphertext_sha256: hash,
    original_url: `/api/v1/uploads/record-A/content?file_id=file-A&source_object_id=object-A&source_object_version=3&expected_ciphertext_sha256=${hash}` };
}
const answer = () => ({ gate_route: 'ANSWER', gate_action: 'literal_render', provider_mode: 'selected', answer: 'Private answer A', citations: [citation()] });
function currentCitation() {
  const value = citation();
  return { ...value, review_status: 'unreviewed', user_confirmed: false,
    original_url: `${value.original_url}&processing_job_id=cc9ad45c-45ea-4dfa-b3b1-073d9ffec789&source_attempt=1&source_result_sha256=${'b'.repeat(64)}` };
}
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };
const response = (payload, status = 200) => new Response(JSON.stringify(payload), { status, headers: { 'content-type': 'application/json' } });
const tick = () => new Promise(resolve => setImmediate(resolve));
function nodes(tree) { return tree.flatMap(node => typeof node === 'object' ? [node, ...nodes(node.props.children)] : []); }
function text(tree) { return tree.map(node => typeof node === 'object' ? text(node.props.children) : String(node)).join(''); }
function setup(t, fetchImpl, originalFetch = fetchImpl) {
  const previous = globalThis.fetch; globalThis.fetch = fetchImpl;
  const h = harness(originalFetch);
  const runtime = h.load('src/shared/api/runtime.js');
  runtime.setRuntimeApiBaseUrl(baseUrl); runtime.setRuntimeAccessToken('token-A');
  h.mount(h.load('src/features/chat/useGroundedChat.js').useGroundedChat, { userId: 'user-A', roomId: 'room-A' });
  t.after(() => { h.unmount(); globalThis.fetch = previous; });
  return { h, runtime };
}
async function send(h) { h.render().onChangeDraft('Private question A'); return h.render().onSend(); }

test('backend-valid HTTP 200 with extraction metadata and URL-only processing identity renders and opens exact original', async t => {
  const source = currentCitation(), requests = [];
  const { h } = setup(t, async (url, options) => {
    requests.push({ url, options });
    return url.endsWith('/ask') ? response({ ...answer(), citations: [source] })
      : new Response('image bytes', { headers: { 'content-type': 'image/png' } });
  });
  assert.equal(await send(h), true);
  const tree = h.expand(h.load('src/features/chat/ChatScreen.js').ChatScreen({ ...h.render(), serviceMode: false }));
  assert.match(text(tree), /Private answer A/);
  assert.doesNotMatch(text(tree), /사용자가 아직 확인하지 않은 근거/);
  await nodes(tree).find(node => node.props.accessibilityLabel === '근거 기록 1 원본 보기').props.onPress();
  assert.equal(requests.length, 2);
  assert.equal(requests[1].url, new URL(source.original_url, baseUrl).toString());
  assert.equal(requests[1].options.headers.Authorization, 'Bearer token-A');
  assert.equal(requests[1].options.redirect, 'error');
  assert.equal(requests[1].options.credentials, 'omit');
  assert.doesNotMatch(requests[1].url, /token-A/);
  assert.equal(h.render().original.media.kind, 'image');
});

for (const kind of ['local-contract', 'malformed-url', 'malformed-version', 'malformed-digest', 'STALE_SOURCE', 'GroundedEvidenceChanged', '503', 'network']) {
  test(`retry eligibility survives error rendering: ${kind}`, async t => {
    let calls = 0;
    const retryable = ['503', 'network'].includes(kind);
    const { h } = setup(t, async () => {
      calls++;
      if (kind === 'network') throw new TypeError('Failed to fetch');
      if (kind === 'malformed-version' || kind === 'malformed-digest') return response({ ...answer(), citations: [{ ...currentCitation(),
        [kind === 'malformed-version' ? 'source_object_version' : 'source_ciphertext_sha256']: { toString: null } }] });
      if (kind === 'local-contract' || kind === 'malformed-url') return response({ ...answer(), citations: [{ ...currentCitation(),
        original_url: kind === 'malformed-url' ? 'http://[' : 'https://foreign.example/api/v1/uploads/record-A/content' }] });
      return response({ detail: { code: kind } }, kind === '503' ? 503 : 409);
    });
    assert.equal(await send(h), false);
    const failed = h.render().messages[0];
    const tree = h.expand(h.load('src/features/chat/ChatScreen.js').ChatScreen({ ...h.render(), serviceMode: false }));
    assert.equal(nodes(tree).some(node => node.props.accessibilityLabel === '이 질문 다시 시도'), retryable);
    await h.render().onRetry(failed);
    await h.render().onRetry();
    assert.equal(calls, retryable ? 3 : 1);
  });
}

function composer(h) {
  const tree = h.expand(h.load('src/features/chat/ChatScreen.js').ChatScreen({ ...h.render(), serviceMode: false }));
  return nodes(tree).find(node => node.type === 'TextInput');
}

test('Android composer accepts typing and deletion once per native text change', t => {
  const { h } = setup(t, async () => response(answer()));
  for (const draft of ['가', '가족', '가', '']) {
    const input = composer(h);
    assert.equal(input.props.editable, true);
    let changes = 0;
    const chat = h.render();
    const tree = h.expand(h.load('src/features/chat/ChatScreen.js').ChatScreen({ ...chat,
      onChangeDraft(value) { changes++; chat.onChangeDraft(value); } }));
    nodes(tree).find(node => node.type === 'TextInput').props.onChangeText(draft);
    assert.equal(changes, 1);
    assert.equal(composer(h).props.value, draft);
  }
});

for (const status of [200, 400, 503]) {
  test(`pending Ask keeps composer editable and its late ${status} response preserves new typing`, async t => {
    const pending = deferred();
    const { h } = setup(t, () => pending.promise);
    composer(h).props.onChangeText('첫 질문');
    const sent = h.render().onSend();
    assert.equal(composer(h).props.value, '', 'accepted send clears exactly once');
    assert.equal(composer(h).props.editable, true, 'waiting for Ask must not disable typing/deletion');
    composer(h).props.onChangeText('다음 질문');
    composer(h).props.onChangeText('다음');
    pending.resolve(response(status === 200 ? answer() : { detail: 'Invalid ask request' }, status));
    assert.equal(await sent, status === 200);
    assert.equal(composer(h).props.value, '다음');
    assert.equal(composer(h).props.editable, true);
    assert.equal(h.render().sending, false);
    if (status !== 200) {
      const tree = h.expand(h.load('src/features/chat/ChatScreen.js').ChatScreen({ ...h.render(), serviceMode: false }));
      assert.ok(text(tree).includes(`HTTP ${status}`));
      assert.doesNotMatch(text(tree), /Invalid ask request/);
    }
  });
}

test('retrying a failed question preserves the new unsent draft', async t => {
  const { h } = setup(t, async () => response({ detail: 'Embedding provider failed' }, 503));
  await send(h);
  composer(h).props.onChangeText('새로 작성 중인 질문');
  await h.render().onRetry();
  assert.equal(composer(h).props.value, '새로 작성 중인 질문');
  assert.equal(composer(h).props.editable, true);
});

test('NO_RECORD explains missing searchable evidence while leaving input usable', async t => {
  const { h } = setup(t, async () => response({ gate_route: 'NO_RECORD', gate_action: 'llm_not_called', provider_mode: 'not_called', answer: null, citations: [] }));
  await send(h);
  const tree = h.expand(h.load('src/features/chat/ChatScreen.js').ChatScreen({ ...h.render(), serviceMode: false }));
  assert.match(text(tree), /검색 가능한/);
  assert.match(text(tree), /문자 인식/);
  assert.equal(h.render().messages[0].error, null);
  assert.equal(composer(h).props.editable, true);
});

test('chat composer does not duplicate root Android avoidance or system navigation padding', () => {
  const h = harness();
  const tree = h.expand(h.load('src/features/chat/ChatScreen.js').ChatScreen({ enabled: true, bottomInset: 340 }));
  const wrapper = nodes(tree).find(node => node.type === 'KeyboardAvoidingView');
  assert.equal(wrapper.props.behavior, undefined);
  const padding = nodes(tree).flatMap(node => Array.isArray(node.props.style) ? node.props.style : [node.props.style])
    .filter(style => style && Object.hasOwn(style, 'paddingBottom')).map(style => style.paddingBottom);
  assert.deepEqual(padding, [12], 'the composer sits above the tabs, not the system navigation inset');
});

test('Android chat has window-root keyboard avoidance and only one navigation inset', async () => {
  const controller = { storageLoaded: true, user: { id: 'user-A' }, activeFamily: { id: 'room-A' }, activeTab: 'chat', bottomInset: 340, keyboardInset: 312,
    records: [], groupedRecords: [] };
  const overrides = {
    './useAppController': { useAppController: () => controller },
    '../features/chat/useGroundedChat': { useGroundedChat: () => ({}) },
    '../features/records/useRecordReadState': { useRecordReadState: () => ({ scopeKey: 'user-A:room-A', isUnread: () => false, markRead() {} }) },
    '../shared/api/runtime': { getRuntimeAccessToken: () => 'token', getRuntimeApiBaseUrl: () => baseUrl },
    'expo-status-bar': { StatusBar: 'ExpoStatusBar' },
    '../shared/theme/styles': { styles: {} },
  };
  for (const [directory, names] of Object.entries({
    '../shared/ui': ['LoadingScreen', 'FeedbackModals', 'AppHeader', 'BottomTabs'],
    '../features/auth': ['LoginScreen'], '../features/home': ['HomeScreen'],
    '../features/chat': ['ChatScreen', 'CitationOriginalViewer'],
    '../features/records': ['StorageScreen', 'RecordMediaViewer', 'RecordDetailViewer', 'RecordComposer', 'UploadProgress'],
    '../features/profile': ['MyPageScreen', 'ProfileEditor'],
  })) for (const name of names) overrides[`${directory}/${name}`] = { [name]: name };
  const h = harness(undefined, overrides);
  const render = () => nodes(h.expand(h.load('src/navigation/AppNavigator.js').default({})));
  const root = render()[0];
  // Execute the installed RN implementation: mocking it as a string previously
  // missed the nonzero overlap computed from Android's height-zero hide event.
  const listeners = new Map();
  class Component {
    constructor(props) { this.props = props; }
    setState(next) { this.state = { ...this.state, ...next }; }
  }
  const react = { Component, createRef: () => ({ current: null }), createElement:
    ((type, props, ...children) => ({ type, props: { ...props, children } })) };
  const nativeModule = { exports: {} };
  const filename = require.resolve('react-native/Libraries/Components/Keyboard/KeyboardAvoidingView');
  const code = babel.transformSync(fs.readFileSync(filename, 'utf8'), {
    filename, babelrc: false, configFile: false, presets: ['module:@react-native/babel-preset'],
  }).code;
  new Function('require', 'module', 'exports', code)(id => {
    if (id === 'react') return react;
    if (id === 'react/jsx-runtime') return { jsx: (type, props) => ({ type, props }) };
    if (id.endsWith('/Platform')) return h.native.Platform;
    if (id.endsWith('/StyleSheet')) return { compose: (a, b) => [a, b] };
    if (id.endsWith('/View')) return 'View';
    if (id.endsWith('/Keyboard')) return { isVisible: () => false, addListener: (name, fn) => {
      listeners.set(name, fn); return { remove: () => listeners.delete(name) };
    } };
    if (id.includes('@babel/runtime')) return require(id);
    return {};
  }, nativeModule, nativeModule.exports);
  const avoidance = new nativeModule.exports.default(root.props);
  avoidance.componentDidMount();
  const layout = height => avoidance._onLayout({ persist() {}, nativeEvent: { layout: { x: 0, y: 0, width: 400, height } } });
  const style = () => Object.assign({}, ...[avoidance.render().props.style].flat().filter(Boolean));
  controller.keyboardInset = 0; controller.bottomInset = 28;
  avoidance.props = render()[0].props;
  await layout(800);
  const baseline = style();
  for (let cycle = 0; cycle < 3; cycle++) {
    controller.keyboardInset = 312;
    avoidance.props = render()[0].props;
    listeners.get('keyboardDidShow')({ endCoordinates: { screenY: 488, height: 312 } });
    await tick();
    assert.equal(style().paddingBottom, 312);
    await layout(488);
    assert.equal(style().paddingBottom, 0, 'adjustResize already consumed the overlap');
    controller.activeFamily = { id: `room-${cycle}` };
    avoidance.props = render()[0].props;
    assert.equal(style().paddingBottom, 0, 'room switch does not add a second displacement');
    await layout(800);
    assert.equal(style().paddingBottom, 312, 'edge-to-edge overlap is handled when native resize is absent');
    listeners.get('keyboardDidHide')({ endCoordinates: { screenY: 772, height: 0 } });
    await tick();
    assert.equal(avoidance.state.bottom, 28, 'RN hide retains system-bar overlap: regression trigger');
    controller.keyboardInset = 0;
    avoidance.props = render()[0].props;
    assert.deepEqual(style(), baseline, 'hide restores exact closed style despite stale native state');
  }
  controller.keyboardInset = 312; controller.activeTab = 'mypage';
  avoidance.props = render()[0].props;
  assert.deepEqual(style(), baseline, 'blur removes avoidance even while keyboard remains open');
  await layout(400);
  assert.deepEqual(style(), baseline, 'orientation cannot restore cached height');
  avoidance.componentWillUnmount();
  assert.equal(listeners.size, 0, 'unmount removes both native keyboard listeners');
  controller.activeTab = 'chat'; controller.bottomInset = 340;
  assert.equal(root.type, 'KeyboardAvoidingView', 'avoidance measures at window origin, outside header and safe area');
  assert.equal(root.props.behavior, 'padding');
  assert.equal(root.props.enabled, true);
  assert.equal(root.props.keyboardVerticalOffset, 0);
  assert.ok(nodes([root]).some(node => node.type === 'ChatScreen'), 'composer stays inside keyboard-aware layout');
  assert.equal(root.props.children[0].type, 'SafeAreaView');
  assert.equal(render().find(node => node.type === 'BottomTabs').props.bottomInset, 0);
  controller.keyboardInset = 0; controller.bottomInset = 28;
  assert.equal(render().find(node => node.type === 'BottomTabs').props.bottomInset, 28);
  assert.equal(render()[0].props.enabled, false);
  assert.equal(render()[0].props.behavior, undefined, 'closed layout discards keyboard-only styles without remounting input');
  for (let cycle = 0; cycle < 3; cycle++) {
    controller.keyboardInset = 312;
    assert.equal(render()[0].props.behavior, 'padding');
    controller.keyboardInset = 0;
    assert.equal(render()[0].props.behavior, undefined);
    assert.equal(render().find(node => node.type === 'BottomTabs').props.bottomInset, 28);
  }
  h.native.Platform.OS = 'ios';
  assert.equal(render()[0].props.enabled, false, 'iOS keeps its existing ChatScreen padding owner');
  const iosTree = h.expand(h.load('src/features/chat/ChatScreen.js').ChatScreen({ enabled: true }));
  assert.equal(nodes(iosTree).find(node => node.type === 'KeyboardAvoidingView').props.behavior, 'padding');
  h.native.Platform.OS = 'android';
  controller.activeTab = 'mypage'; controller.keyboardInset = 312; controller.bottomInset = 340;
  assert.equal(render()[0].props.enabled, false, 'other tabs retain native resize behavior');
  assert.equal(render()[0].props.behavior, undefined, 'screen blur drops keyboard styles');
  assert.equal(render().find(node => node.type === 'BottomTabs').props.bottomInset, 340, 'other screens retain their existing layout behavior');
});

for (const [route, title] of Object.entries({ ANSWER: '기록을 바탕으로 한 답변', REFUSE: '답변할 수 없는 질문', CLARIFY: '질문을 조금 더 구체적으로 알려주세요', CONFLICT: '기록에 서로 다른 내용이 있습니다', NO_RECORD: '관련 기록을 찾지 못했습니다' })) {
  test(`actual ChatScreen renders canonical ${route} from the Ask hook`, async t => {
    const payload = route === 'ANSWER' ? answer() : { gate_route: route, gate_action: 'llm_not_called', provider_mode: 'not_called', answer: null, citations: [] };
    const { h } = setup(t, async () => response(payload));
    assert.equal(await send(h), true);
    const tree = h.expand(h.load('src/features/chat/ChatScreen.js').ChatScreen({ ...h.render(), serviceMode: false }));
    assert.ok(text(tree).includes(title));
    assert.equal(nodes(tree).filter(node => node.props.accessibilityLabel === '근거 기록 1 원본 보기').length, route === 'ANSWER' ? 1 : 0);
    if (route === 'ANSWER') assert.ok(text(tree).includes(payload.answer));
    else assert.equal(nodes(tree).filter(node => node.type === 'Text' && node.props.selectable).length, 1, 'only the submitted question is selectable; absent backend text must not invent a response');
  });
}

test('citation UI preserves the whole citation and displays canonical OCR provenance', () => {
  const h = harness(), source = citation(); let selected;
  const tree = h.expand(h.load('src/features/chat/ChatScreen.js').ChatScreen({ enabled: true, serviceMode: false, messages: [{ id: 'one', query: 'Q', response: { ...answer(), citations: [source] } }], onOpenCitation: value => { selected = value; } }));
  const button = nodes(tree).find(node => node.props.accessibilityLabel === '근거 기록 1 원본 보기');
  button.props.onPress();
  assert.equal(selected, source);
  assert.match(text(tree), /문자 인식 영역 1, 3/);
  assert.match(text(tree), /원문 문자 위치 2–32/);
  assert.match(text(tree), /자동 문자 인식 · 추출 당시 검토 완료/);
});

test('logout during Ask discards a late response before and after a new login', async t => {
  const pending = deferred(); const { h, runtime } = setup(t, () => pending.promise);
  const sent = send(h); await tick();
  runtime.setRuntimeAccessToken('');
  assert.deepEqual(h.render({ userId: null, roomId: null }).messages, []);
  runtime.setRuntimeAccessToken('token-B');
  h.render({ userId: 'user-B', roomId: 'room-B' });
  pending.resolve(response(answer()));
  assert.equal(await sent, false); await tick();
  assert.deepEqual(h.render().messages, []);
});

test('room switch while Ask is pending keeps the late A answer out of B', async t => {
  const pending = deferred(); let request;
  const { h } = setup(t, (url, options) => { request = { url, options }; return pending.promise; });
  const sent = send(h); await tick();
  assert.equal(request.url, `${baseUrl}/families/room-A/ask`);
  assert.deepEqual(h.render({ userId: 'user-A', roomId: 'room-B' }).messages, []);
  assert.equal(request.options.signal.aborted, true);
  pending.resolve(response(answer())); await sent; await tick();
  assert.deepEqual(h.render().messages, []);
});

for (const change of ['room', 'logout']) {
  test(`late Ask failure after ${change} cannot overwrite a new editable draft`, async t => {
    const pending = deferred();
    const { h, runtime } = setup(t, () => pending.promise);
    const sent = send(h); await tick();
    if (change === 'logout') {
      runtime.setRuntimeAccessToken('');
      h.render({ userId: null, roomId: null });
      runtime.setRuntimeAccessToken('token-B');
    }
    h.render({ userId: change === 'logout' ? 'user-B' : 'user-A', roomId: 'room-B' });
    composer(h).props.onChangeText('새 질문');
    pending.resolve(response({ detail: 'Old private error' }, 503));
    assert.equal(await sent, false); await tick();
    assert.deepEqual(h.render().messages, []);
    assert.equal(h.render().sending, false);
    assert.equal(composer(h).props.value, '새 질문');
    assert.equal(composer(h).props.editable, true);
  });
}

test('late original response cannot enter a new session, and old citation callbacks cannot refetch', async t => {
  const pending = deferred(); const requests = [];
  const { h, runtime } = setup(t, (url, options) => { requests.push({ url, options }); return url.endsWith('/ask') ? Promise.resolve(response(answer())) : pending.promise; });
  await send(h);
  const source = h.render().messages[0].response.citations[0];
  const oldOpen = h.render().onOpenCitation;
  const opening = oldOpen(source); await tick();
  assert.equal(h.render().original.loading, true);
  assert.equal(requests[1].url, new URL(source.original_url, baseUrl).toString());
  assert.equal(requests[1].options.headers.Authorization, 'Bearer token-A');
  assert.ok(!requests[1].url.includes('token-A'));
  runtime.setRuntimeAccessToken('token-B'); h.render({ userId: 'user-B', roomId: 'room-B' });
  assert.equal(requests[1].options.signal.aborted, true);
  pending.resolve(new Response('Old private original', { headers: { 'content-type': 'text/plain' } }));
  await opening; await tick();
  assert.equal(h.render().original, null);
  await oldOpen(source);
  assert.equal(requests.length, 2);
});

test('409 STALE_SOURCE is rendered as a dedicated original error with no fallback fetch', async t => {
  let count = 0;
  const { h } = setup(t, async url => { count++; return url.endsWith('/ask') ? response(answer()) : response({ detail: 'STALE_SOURCE' }, 409); });
  await send(h); await h.render().onOpenCitation(h.render().messages[0].response.citations[0]);
  const original = h.render().original;
  assert.equal(original.media, null);
  const tree = h.expand(h.load('src/features/chat/CitationOriginalViewer.js').CitationOriginalViewer({ original, onClose() {} }));
  assert.match(text(tree), /원본이 변경되었습니다/);
  assert.match(text(tree), /최신 원본으로 바꾸지 않고/);
  assert.equal(count, 2);
});

test('401 Ask invokes the shared runtime handler and real auth session clearing', async t => {
  const { h, runtime } = setup(t, async () => response({ detail: 'expired' }, 401));
  const removed = [], tokens = [];
  const auth = createAuthSession({ storage: { async multiRemove(keys) { removed.push(...keys); } }, setAccessToken: runtime.setRuntimeAccessToken });
  runtime.setRuntimeUnauthorizedHandler(async token => { tokens.push(token); if (token === runtime.getRuntimeAccessToken()) await auth.clear(); });
  assert.equal(await send(h), false);
  assert.deepEqual(tokens, ['token-A']);
  assert.deepEqual(removed, ['ambient.accessToken', 'ambient.currentUser', 'ambient.user']);
  assert.equal(runtime.getRuntimeAccessToken(), '');
  assert.equal(h.render().enabled, false);
  assert.deepEqual(h.render().messages, []);
});


test('protected originals use expo fetch with redirect rejection, separate from the runtime Ask transport', async t => {
  const askRequests = [], originalRequests = [];
  const { h } = setup(t,
    async (url, options) => { askRequests.push({ url, options }); return response(answer()); },
    async (url, options) => { originalRequests.push({ url, options }); return new Response('Bound original', { headers: { 'content-type': 'text/plain' } }); });
  await send(h);
  const source = h.render().messages[0].response.citations[0];
  await h.render().onOpenCitation(source);
  assert.equal(originalRequests.length, 1, 'protected original must use expo/fetch');
  assert.equal(askRequests.length, 1, 'RN runtime transport must only perform Ask');
  assert.equal(askRequests[0].url, baseUrl + '/families/room-A/ask');
  assert.equal(originalRequests[0].url, new URL(source.original_url, baseUrl).toString());
  assert.equal(originalRequests[0].options.redirect, 'error');
  assert.equal(originalRequests[0].options.headers.Authorization, 'Bearer token-A');
  assert.equal(h.render().original.media.text, 'Bound original');
});

test('secure text attachment opens through the real citation flow and renders only selectable text', async t => {
  const body = '검증 원문 <script>window.example = true</script>';
  const {h} = setup(t, async () => response(answer()), async () => new Response(body, {headers: {
    'content-type': 'text/plain; charset=utf-8', 'content-disposition': 'attachment',
    'content-security-policy': "sandbox; default-src 'none'",
  }}));
  await send(h);
  await h.render().onOpenCitation(h.render().messages[0].response.citations[0]);
  const original = h.render().original;
  assert.equal(original.error, null);
  assert.deepEqual(original.media, {kind: 'text', text: body});
  const tree = h.expand(h.load('src/features/chat/CitationOriginalViewer.js').CitationOriginalViewer({original, onClose() {}}));
  const plainText = nodes(tree).find(node => node.type === 'Text' && node.props.selectable);
  assert.ok(plainText);
  assert.equal(text(plainText.props.children), body);
  assert.equal(nodes(tree).some(node => ['WebView', 'iframe', 'script', 'Image', 'PassiveVideo'].includes(node.type)), false);
});

test('portrait original keeps its aspect ratio, scrolls, and zooms within bounds', () => {
  const h = harness();
  const Viewer = h.load('src/features/chat/CitationOriginalViewer.js').CitationOriginalViewer;
  const original = { citation: currentCitation(), media: { kind: 'image', source: { uri: 'blob:protected-image' } } };
  const render = () => nodes(h.expand(h.render()));
  h.mount(Viewer, { original, onClose() {}, serviceMode: true });
  let tree = render();
  tree.find(node => node.props.onLayout).props.onLayout({ nativeEvent: { layout: { width: 320 } } });
  tree.find(node => node.type === 'Image').props.onLoad({ nativeEvent: { source: { width: 1600, height: 2400 } } });
  tree = render();
  assert.deepEqual(tree.find(node => node.type === 'Image').props.style, { width: 320, height: 480 });
  assert.equal(tree.filter(node => node.type === 'ScrollView').length, 2);
  assert.ok(tree.some(node => node.type === 'ScrollView' && node.props.horizontal));
  const button = label => render().find(node => node.props.accessibilityLabel === label);
  assert.equal(button('이미지 축소').props.disabled, true);
  button('이미지 확대').props.onPress();
  assert.deepEqual(render().find(node => node.type === 'Image').props.style, { width: 640, height: 960 });
  for (let i = 0; i < 4; i++) button('이미지 확대').props.onPress();
  assert.equal(button('이미지 확대').props.disabled, true);
  assert.equal(render().find(node => node.type === 'Image').props.style.width, 1280);
  button('이미지 너비 맞춤').props.onPress();
  assert.equal(render().find(node => node.type === 'Image').props.style.width, 320);
  assert.equal(render().find(node => node.type === 'Image').props.source.uri, 'blob:protected-image');
  render().find(node => node.type === 'Image').props.onLoad({ nativeEvent: { target: { naturalWidth: 1600, naturalHeight: 2000 } } });
  assert.deepEqual(render().find(node => node.type === 'Image').props.style, { width: 320, height: 400 });
});

test('multimodal AUDIO original renders protected audio and real segment offsets, never image', async t => {
  const source = currentCitation();
  source.source_locator = { kind: 'multimodal', media_type: 'AUDIO', review_version: 1,
    anchors: [{ kind: 'AUDIO', segment_id: 'segment-1', char_start: 0, char_end: 24, start_ms: 12000, end_ms: 19500 }] };
  const { h } = setup(t, async () => response({ ...answer(), citations: [source] }),
    async () => new Response('protected audio', { headers: { 'content-type': 'audio/wav' } }));
  await send(h);
  await h.render().onOpenCitation(h.render().messages[0].response.citations[0]);
  const original = h.render().original;
  assert.equal(original.error, null);
  const tree = h.expand(h.load('src/features/chat/CitationOriginalViewer.js').CitationOriginalViewer({ original, onClose() {} }));
  const player = nodes(tree).find(node => node.type === 'PassiveAudio');
  assert.deepEqual(player.props.source, { uri: 'file:///cache/citation.wav' });
  assert.match(text(tree), /0:12–0:19/);
  assert.equal(nodes(tree).some(node => ['Image', 'PassiveVideo'].includes(node.type)), false);
});
