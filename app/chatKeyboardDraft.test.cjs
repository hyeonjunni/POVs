const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { createRequire } = require('node:module');
const babel = require('@babel/core');
const React = require('react');

// Use the installed React Native reconciler (no new renderer dependency).
// Only platform views/services and the unrelated app controller are replaced.
// This catches keyboard keys/remounts and draft resets in the real navigator,
// hook, session and screen; it does not emulate Android's IME/native text buffer.
function fixture() {
  const modules = new Map(), listeners = new Map();
  const controller = { storageLoaded: true, user: { id: 'user-A' },
    activeFamily: { id: 'room-A' }, activeTab: 'chat', bottomInset: 28, keyboardInset: 0,
    records: [], groupedRecords: [] };
  let input, mounts = 0, unmounts = 0;
  const container = ({ children }) => children;
  const empty = () => null;
  const native = { Platform: { OS: 'android' },
    StyleSheet: { create: value => value, compose: (a, b) => [a, b] },
    Keyboard: { isVisible: () => false, addListener(name, callback) {
      const callbacks = listeners.get(name) || new Set();
      callbacks.add(callback); listeners.set(name, callbacks);
      return { remove: () => callbacks.delete(callback) };
    } },
    View: container, SafeAreaView: container, ScrollView: container,
    Pressable: container, Text: empty, ActivityIndicator: empty, StatusBar: empty,
    TextInput(props) {
      input = props;
      React.useEffect(() => { mounts++; return () => { unmounts++; }; }, []);
      return null;
    } };
  const privateInterface = {
    ReactNativeViewConfigRegistry: { customBubblingEventTypes: {}, customDirectEventTypes: {} },
    RCTEventEmitter: { register() {} }, ReactFiberErrorDialog: { showErrorDialog: () => false },
    UIManager: { setChildren() {}, manageChildren() {} },
  };
  let refreshTools, root;
  const devtools = { supportsFiber: true,
    inject(value) { refreshTools = value; return 1; },
    onCommitFiberRoot(_id, value) { root = value; },
  };
  const rendererModule = { exports: {} };
  new Function('require', 'module', 'exports', '__DEV__', '__REACT_DEVTOOLS_GLOBAL_HOOK__', fs.readFileSync(require.resolve(
    'react-native/Libraries/Renderer/implementations/ReactNativeRenderer-dev.js'), 'utf8'))(id => {
    if (id.endsWith('ReactNativePrivateInitializeCore')) return {};
    if (id.endsWith('ReactNativePrivateInterface')) return privateInterface;
    return require(id);
  }, rendererModule, rendererModule.exports, true, devtools);
  const renderer = rendererModule.exports;
  const ignored = ['LoadingScreen', 'LoginScreen', 'FeedbackModals', 'AppHeader', 'HomeScreen',
    'CitationOriginalViewer', 'StorageScreen', 'MyPageScreen', 'RecordMediaViewer', 'RecordDetailViewer', 'BottomTabs',
    'ProfileEditor', 'RecordComposer', 'UploadProgress', 'RecordManagementModal', 'UiIcon'];
  function load(filename) {
    filename = path.resolve(__dirname, filename);
    if (modules.has(filename)) return modules.get(filename).exports;
    const module = { exports: {} }; modules.set(filename, module);
    const localRequire = createRequire(filename);
    const code = babel.transformSync(fs.readFileSync(filename, 'utf8'), {
      filename, babelrc: false, configFile: false, presets: ['module:@react-native/babel-preset'],
    }).code;
    new Function('require', 'module', 'exports', code)(id => {
      if (id === 'react') return React;
      if (id === '@react-native-async-storage/async-storage') return { getItem: async () => null, setItem: async () => {} };
      if (id === 'react-native') return native;
      if (id.endsWith('/useAppController')) return { useAppController: () => controller };
      if (id.endsWith('/useRecordReadState')) return { useRecordReadState: () => ({
        scopeKey: controller.user && controller.activeFamily ? `${controller.user.id}:${controller.activeFamily.id}` : null,
        isUnread: () => false, markRead() {},
      }) };
      const name = id.split('/').at(-1);
      if (ignored.includes(name)) return { [name]: empty };
      if (id === 'expo-status-bar') return { StatusBar: empty };
      if (id.endsWith("/ThemeProvider")) return require("./testSupport/theme.cjs");
      if (id.endsWith('/styles')) return { styles: {} };
      if (id.endsWith('/tokens')) return { colors: {}, presentationStyles: {} };
      if (id.endsWith('/presentationStyles')) return { u: {} };
      if (id.endsWith('/runtime')) return {
        getRuntimeAccessToken: () => 'token-A', getRuntimeApiBaseUrl: () => 'https://backend.example/api/v1',
      };
      if (id === 'expo/fetch') return { fetch: () => { throw new Error('Unexpected request'); } };
      if (id === 'expo/node_modules/expo-file-system') return { File: class {}, Paths: { cache: 'cache' } };
      if (id === 'expo-crypto') return { randomUUID: () => 'unique' };
      if (id.endsWith('/Platform')) return native.Platform;
      if (id.endsWith('/StyleSheet')) return native.StyleSheet;
      if (id.endsWith('/Keyboard')) return native.Keyboard;
      if (id.endsWith('/View')) return native.View;
      if (id.endsWith('/AccessibilityInfo') || id.endsWith('/LayoutAnimation')) return {};
      if (id.startsWith('.') && !id.endsWith('.cjs')) return load(localRequire.resolve(id));
      return localRequire(id);
    }, module, module.exports);
    return module.exports;
  }
  native.KeyboardAvoidingView = load(require.resolve(
    'react-native/Libraries/Components/Keyboard/KeyboardAvoidingView')).default;
  let Navigator = load('src/navigation/AppNavigator.js').default;
  const render = () => React.act(() => { renderer.render(React.createElement(Navigator), 1); });
  return {
    controller, render, input: () => input, counts: () => ({ mounts, unmounts }),
    refresh: () => React.act(() => {
      const previous = Navigator;
      modules.delete(path.resolve(__dirname, 'src/navigation/AppNavigator.js'));
      Navigator = load('src/navigation/AppNavigator.js').default;
      const family = { current: Navigator };
      refreshTools.setRefreshHandler(type => type === previous || type === Navigator ? family : undefined);
      refreshTools.scheduleRefresh(root, { updatedFamilies: new Set([family]), staleFamilies: new Set() });
    }),
    type: text => React.act(() => { input.onChangeText(text); }),
    async keyboard(height) {
      controller.keyboardInset = height; controller.bottomInset = height ? height + 28 : 28;
      await React.act(async () => {
        for (const callback of listeners.get(height ? 'keyboardDidShow' : 'keyboardDidHide') || []) {
          callback({ endCoordinates: { height, screenY: height ? 488 : 772 } });
        }
        renderer.render(React.createElement(Navigator), 1);
      });
    },
    close: () => React.act(() => { renderer.unmountComponentAtNode(1); }),
  };
}

test('navigator keyboard/layout rerenders preserve the complete editable Korean draft and input identity', async () => {
  const h = fixture();
  try {
    await h.render();
    await h.keyboard(312);
    await h.type('아빠');
    await h.render();
    assert.equal(h.input().value, '아빠');
    await h.type('아빠 건강검진');
    assert.equal(h.input().value, '아빠 건강검진');
    for (let cycle = 0; cycle < 3; cycle++) {
      await h.keyboard(0);
      assert.equal(h.input().value, '아빠 건강검진');
      await h.keyboard(312);
      assert.equal(h.input().value, '아빠 건강검진');
    }
    await h.type('아빠 건강검');
    assert.equal(h.input().value, '아빠 건강검');
    assert.deepEqual(h.counts(), { mounts: 1, unmounts: 0 });
    h.controller.activeFamily = { id: 'room-B' };
    await h.render();
    assert.equal(h.input().value, '', 'a real room switch still discards the old draft');
    await h.type('새 질문');
    h.controller.user = null;
    await h.render();
    h.controller.user = { id: 'user-A' };
    await h.render();
    assert.equal(h.input().value, '', 'logout/relogin does not restore a stale draft');
  } finally { await h.close(); }
});

test('Metro Fast Refresh of navigator does not leave the retained draft session disposed', async () => {
  const h = fixture();
  try {
    await h.render();
    await h.refresh();
    await h.keyboard(312);
    await h.type('아빠');
    assert.equal(h.input().value, '아빠', 'the first text change after refresh must reach controlled value');
    await h.render();
    await h.type('아빠 건강검진');
    for (const height of [0, 312, 0, 312]) {
      await h.keyboard(height);
      assert.equal(h.input().value, '아빠 건강검진');
    }
    await h.refresh();
    assert.equal(h.input().value, '아빠 건강검진', 'refresh also preserves an already composed draft');
    await h.type('아빠 건강검');
    assert.equal(h.input().value, '아빠 건강검');
    assert.deepEqual(h.counts(), { mounts: 1, unmounts: 0 }, 'refresh and keyboard transitions preserve input identity');
  } finally { await h.close(); }
});

test('effect disconnect aborts old Ask and reconnect cannot let its response erase the newer draft', async () => {
  const { createChatSession } = require('./src/features/chat/groundedChat.cjs');
  const scope = { userId: 'user-A', roomId: 'room-A', token: 'token-A', baseUrl: 'https://backend.example/api/v1' };
  const pending = [];
  let rendered;
  const session = createChatSession({ readScope: () => scope, onChange: value => { rendered = value; },
    ask: ({ signal }) => new Promise(resolve => pending.push({ resolve, signal })) });
  try {
    session.updateScope(scope);
    session.connect();
    session.setDraft('첫 질문');
    const oldAsk = session.send();
    assert.equal(rendered.draft, '');
    session.setDraft('다음 질문');
    session.disconnect();
    assert.equal(pending[0].signal.aborted, true);
    session.connect();
    assert.equal(rendered.draft, '다음 질문');
    assert.equal(rendered.sending, false);
    const nextAsk = session.send();
    session.setDraft('작성 중');
    pending[0].resolve({ answer: 'stale' });
    assert.equal(await oldAsk, false);
    assert.equal(rendered.draft, '작성 중');
    assert.equal(rendered.sending, true, 'old finalizer cannot stop the new request');
    pending[1].resolve({ answer: 'current' });
    assert.equal(await nextAsk, true);
    assert.equal(rendered.draft, '작성 중');
    assert.equal(rendered.messages[0].response, null);
    assert.equal(rendered.messages[1].response.answer, 'current');
    session.disconnect();
    const lastRendered = rendered;
    session.setDraft('detached callback');
    assert.equal(rendered, lastRendered, 'unmounted effect cannot publish updates');
  } finally { session.dispose(); }
});
