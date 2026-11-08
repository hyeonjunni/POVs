const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const babel = require('@babel/core');
function loadScreen(home) {
  const react = {createElement: (type, props, ...children) => ({type, props: props || {}, children}), useState: initial => [initial, () => {}], useEffect() {}};
  const native = {...Object.fromEntries(['View','Text','ScrollView','Pressable','Modal'].map(name => [name,name])), StyleSheet: {create: value => value}};
  function load(relative) {
  const source = fs.readFileSync(__dirname + relative, 'utf8');
  const code = babel.transformSync(source, {babelrc: false, configFile: false, plugins: ['@babel/plugin-transform-modules-commonjs', '@babel/plugin-transform-react-jsx']}).code;
  const module = {exports: {}};
  new Function('require', 'module', 'exports', code)(id => {
    if (id === 'react') return react;
    if (id === 'react-native') return native;
    if (id.endsWith("/ThemeProvider")) return require("./testSupport/theme.cjs");
    if (id.endsWith('/styles')) return {styles: {}};
    if (id.endsWith('/tokens')) return {colors: {}, presentationStyles: {}};
    if (id.endsWith('/presentationStyles')) return {u: {}};
    if (id.endsWith('/UiIcon')) return {UiIcon: 'UiIcon'};
    if (id.endsWith('/layout')) return {BASE_SCROLL_BOTTOM_PADDING: 0};
    if (id.endsWith('/RecordMedia')) return {AuthenticatedMedia: 'AuthenticatedMedia'};
    if (id.endsWith('/NewBadge')) return load('/src/features/records/NewBadge.js');
    if (id.endsWith('/RecordStatus')) return load('/src/features/records/RecordStatus.js');
    if (id === './recordManagement.cjs') return require('./src/features/records/recordManagement.cjs');
    throw new Error(id);
  }, module, module.exports);
  return module.exports;
  }
  return home ? load('/src/features/home/HomeScreen.js').HomeScreen : load('/src/features/records/StorageScreen.js').StorageScreen;
}
function expand(node) {
  if (Array.isArray(node)) return node.flatMap(expand);
  if (node == null || typeof node === 'boolean') return [];
  if (typeof node !== 'object') return [node];
  if (typeof node.type === 'function') return expand(node.type(node.props));
  return [{...node, children: expand(node.children)}];
}
function nodes(tree) { return tree.flatMap(node => typeof node === 'object' ? [node, ...nodes(node.children)] : []); }
function renderRecord(record, home, callbacks = {}) {
  const screen = loadScreen(home);
  return expand(home ? screen({records: [record], ...callbacks}) : screen({groupedRecords: [{key: record.type, label: record.type, items: [record]}], initialType: record.type, bottomInset: 0, ...callbacks}));
}
function renderCard(hasFile, type = 'image', home = false) {
  const record = {id: 'record', type, title: 'name', hasFile};
  return JSON.stringify(renderRecord(record, home));
}
test('storage distinguishes record kind from audience and labels the historical date', () => {
  const card = JSON.stringify(renderRecord({id: 'record', type: 'text', title: '건강 기록', hasFile: true,
    visibility: 'PRIVATE', metadata: {record_kind: 'SHARED'}, displayDate: '발생일 2009.05.17'}, false));
  assert.match(card, /공동 기록/);
  assert.match(card, /나만 보기/);
  assert.match(card, /발생일 2009.05.17/);
});
for (const type of ['image', 'video']) {
  test(`${type} metadata-only card explicitly says file is not confirmed`, () => {
    const card = renderCard(false, type);
    assert.match(card, /파일 업로드 미완료/);
    assert.doesNotMatch(card, /파일이 저장되었습니다|사진 보기|영상 보기|AuthenticatedMedia/);
  });
  test(`${type} home slot marks missing binary rather than implying success`, () => {
    assert.match(renderCard(false, type, true), /파일 업로드 미완료/);
    assert.doesNotMatch(renderCard(true, type, true), /파일 업로드 미완료/);
  });
}
test('successful image and video cards expose protected in-app viewing', () => {
  assert.match(renderCard(true), /AuthenticatedMedia/);
  assert.match(renderCard(true, 'video'), /영상 보기/);
});

test('home photo previews use the current media session without reading records or fetching missing files', () => {
  const record = {id: 'photo', type: 'image', title: '가족 여행', hasFile: true, isUnread: true};
  const session = {mediaToken: 'current-session', mediaBaseUrl: 'https://family.example/api/v1'};
  const preview = nodes(renderRecord(record, true, session)).find(node => node.type === 'AuthenticatedMedia');
  assert.equal(preview.props.item, record);
  assert.equal(preview.props.token, session.mediaToken);
  assert.equal(preview.props.baseUrl, session.mediaBaseUrl);
  assert.equal(preview.props.onViewed, undefined);
  assert.equal(preview.props.fallback.type, 'UiIcon');
  assert.equal(nodes(renderRecord({...record, hasFile: false}, true, session)).some(node => node.type === 'AuthenticatedMedia'), false);
});

for (const home of [true, false]) for (const type of ['image', 'video', 'text', 'voice']) {
  test(`${home ? 'Home' : 'Storage'} ${type} shows New until opened and passes the exact record on press`, () => {
    const record = {id: `record-${type}`, type, title: '아직 안 읽은 기록', hasFile: true, isUnread: true};
    const opened = [], lists = [];
    const callbacks = {onOpenRecord: item => opened.push(item), onOpenRecords: category => lists.push(category)};
    const tree = renderRecord(record, home, callbacks);
    const all = nodes(tree);
    const badge = all.find(node => node.type === 'Text' && node.children.includes('New'));
    assert.ok(badge, 'the real NewBadge renders for an unread record');
    assert.equal(badge.props.accessibilityLabel, '아직 열어보지 않은 기록');
    assert.deepEqual(opened, [], 'rendering the list must not open or mark any record');
    if (home) {
      all.find(node => node.props.accessibilityLabel === `${{image: '사진', video: '영상', text: '텍스트', voice: '음성'}[type]} 기록 모두 보기`).props.onPress();
      assert.deepEqual(lists, [type]);
      assert.deepEqual(opened, [], 'opening a category is not reading a record');
    }
    const thumbnail = all.find(node => node.type === 'AuthenticatedMedia');
    if (thumbnail) assert.equal(thumbnail.props.onViewed, undefined, 'passive thumbnails must not mark records read');
    const target = all.find(node => node.type === 'Pressable' && node.props.accessibilityLabel === `${record.title}, 아직 열어보지 않은 기록, 기록 보기`);
    assert.ok(target, 'individual records have an accessible open action');
    target.props.onPress();
    assert.equal(opened.length, 1);
    assert.equal(opened[0], record, 'open action passes the selected record without substituting its category');
    assert.equal(record.isUnread, true, 'the list delegates successful viewing instead of marking immediately');
    const readTree = renderRecord({...record, isUnread: false}, home, callbacks);
    assert.equal(nodes(readTree).some(node => node.type === 'Text' && node.children.includes('New')), false);
  });
}
