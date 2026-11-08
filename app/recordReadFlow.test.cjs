const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { createRequire } = require('node:module');
const babel = require('@babel/core');
const React = require('react');
const { createReadScopeKey } = require('./src/features/records/readState.cjs');

const scope = { baseUrl: 'https://backend.example/api/v1', userId: 'user-A', roomId: 'room-A' };
const record = { id: 'shared-record', type: 'text', title: '가족의 기억', detail: '전체 본문', tags: ['가족'] };
function memoryStorage() {
  const data = new Map(), writes = [];
  return { data, writes, async getItem(key) { return data.get(key) ?? null; },
    async setItem(key, value) { writes.push({ key, value }); data.set(key, value); } };
}

// Run the real hook and viewer through the installed React Native reconciler.
// Only platform views and AsyncStorage are replaced; effects and hook identity
// are React's own, so an unstable onViewed callback would cause a real loop.
function fixture(storage) {
  const modules = new Map();
  const container = ({ children }) => children;
  const native = { StyleSheet: { create: value => value },
    Modal: container, View: container, ScrollView: container, Pressable: container, Text: () => null };
  const privateInterface = {
    ReactNativeViewConfigRegistry: { customBubblingEventTypes: {}, customDirectEventTypes: {} },
    RCTEventEmitter: { register() {} }, ReactFiberErrorDialog: { showErrorDialog: () => false },
    UIManager: { setChildren() {}, manageChildren() {} },
  };
  const rendererModule = { exports: {} };
  new Function('require', 'module', 'exports', '__DEV__', fs.readFileSync(require.resolve(
    'react-native/Libraries/Renderer/implementations/ReactNativeRenderer-dev.js'), 'utf8'))(id => {
    if (id.endsWith('ReactNativePrivateInitializeCore')) return {};
    if (id.endsWith('ReactNativePrivateInterface')) return privateInterface;
    return require(id);
  }, rendererModule, rendererModule.exports, true);
  const renderer = rendererModule.exports;
  function load(relative) {
    const filename = path.resolve(__dirname, relative);
    if (modules.has(filename)) return modules.get(filename).exports;
    const module = { exports: {} }; modules.set(filename, module);
    const localRequire = createRequire(filename);
    const code = babel.transformSync(fs.readFileSync(filename, 'utf8'), {
      filename, babelrc: false, configFile: false, presets: ['module:@react-native/babel-preset'],
    }).code;
    new Function('require', 'module', 'exports', code)(id => {
      if (id === 'react') return React;
      if (id === 'react-native') return native;
      if (id === '@react-native-async-storage/async-storage') return storage;
      if (id.endsWith("/ThemeProvider")) return require("./testSupport/theme.cjs");
      if (id.endsWith('/tokens')) return { colors: {} };
      if (id.startsWith('.') && !id.endsWith('.cjs')) return load(localRequire.resolve(id));
      return localRequire(id);
    }, module, module.exports);
    return module.exports;
  }
  const { useRecordReadState } = load('src/features/records/useRecordReadState.js');
  const { RecordDetailViewer } = load('src/features/records/RecordDetailViewer.js');
  const { NewBadge } = load('src/features/records/NewBadge.js');
  let current, renders = 0;
  let props = { scope, record, opened: false };
  function Screen({ scope, record, opened }) {
    assert.ok(++renders < 100, 'viewer and read-state updates must settle without an effect loop');
    current = useRecordReadState(scope);
    return React.createElement(React.Fragment, null,
      React.createElement(NewBadge, { visible: current.isUnread(record.id) }),
      opened ? React.createElement(RecordDetailViewer, { record, onViewed: current.markRead, onClose() {} }) : null);
  }
  return {
    state: () => current,
    async render(next = {}) {
      props = { ...props, ...next };
      await React.act(async () => { renderer.render(React.createElement(Screen, props), 1); });
    },
    async close() { await React.act(async () => { renderer.unmountComponentAtNode(1); }); },
  };
}

test('explicit text detail opening persists once, stable rerenders settle, and a new hook restores it', async () => {
  const storage = memoryStorage();
  const first = fixture(storage);
  try {
    await first.render();
    assert.equal(first.state().ready, true);
    assert.equal(first.state().isUnread(record.id), true);
    assert.equal(storage.writes.length, 0, 'merely rendering a record list does not mark it read');
    const mark = first.state().markRead;
    await first.render({ opened: true });
    assert.equal(first.state().isUnread(record.id), false);
    assert.deepEqual(JSON.parse(storage.data.get(createReadScopeKey(scope))), { version: 1, recordIds: [record.id] });
    assert.equal(storage.writes.length, 1, 'one detail open causes one persisted view');
    for (let i = 0; i < 3; i++) await first.render();
    assert.equal(first.state().markRead, mark, 'stable scope retains onViewed identity');
    assert.equal(storage.writes.length, 1, 'read notifications and normal rerenders do not reopen the record');
  } finally { await first.close(); }
  const remounted = fixture(storage);
  try {
    await remounted.render();
    assert.equal(remounted.state().isUnread(record.id), false, 'fresh hook loads the previous explicit view');
    assert.equal(remounted.state().isUnread('another-record'), true);
    assert.equal(storage.writes.length, 1, 'restoring history does not create another view');
  } finally { await remounted.close(); }
});

test('room and account switches isolate the same record ID and reject captured old view callbacks', async () => {
  const storage = memoryStorage(), h = fixture(storage);
  try {
    await h.render({ opened: true });
    const oldMark = h.state().markRead;
    assert.equal(h.state().isUnread(record.id), false);
    for (const otherScope of [{ ...scope, roomId: 'room-B' }, { ...scope, userId: 'user-B' }]) {
      await h.render({ scope: otherScope, opened: false });
      assert.equal(h.state().ready, true);
      assert.equal(h.state().isUnread(record.id), true);
      assert.equal(await oldMark(record.id), false, 'late old-view callback cannot mark in a different scope');
      assert.equal(h.state().isUnread(record.id), true);
      assert.equal(storage.data.has(createReadScopeKey(otherScope)), false);
    }
    await h.render({ scope });
    assert.equal(h.state().isUnread(record.id), false, 'returning to the first scope retains its view');
    assert.equal(storage.writes.length, 1);
  } finally { await h.close(); }
});

test('opening image metadata without the original file keeps New and does not persist a view', async () => {
  const storage = memoryStorage(), h = fixture(storage);
  try {
    await h.render({ record: { ...record, type: 'image', hasFile: false }, opened: true });
    assert.equal(h.state().ready, true);
    assert.equal(h.state().isUnread(record.id), true);
    await h.render();
    assert.equal(storage.writes.length, 0, 'metadata-only detail is not a successful original view');
  } finally { await h.close(); }
});
