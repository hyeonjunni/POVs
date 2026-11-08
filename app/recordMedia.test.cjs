const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const babel = require('@babel/core');
function harness(platform = "android", onViewed) {
  const source = fs.readFileSync(__dirname + '/src/features/records/RecordMedia.js', 'utf8');
  const code = babel.transformSync(source, {babelrc: false, configFile: false, plugins: ['@babel/plugin-transform-modules-commonjs', '@babel/plugin-transform-react-jsx']}).code;
  const state = [], effects = [];
  let cursor = 0, finish, fail, released = 0, reported = 0, passedOptions;
  const request = {ready: new Promise((resolve, reject) => {finish = resolve; fail = reject;}), release: () => released++, report: () => reported++};
  const module = {exports: {}};
  const react = {createElement: (type, props, ...children) => ({type, props: {...props, children}}),
    useState: initial => {const index = cursor++; if (!(index in state)) state[index] = initial; return [state[index], value => {state[index] = value;}];},
    useEffect: fn => {effects.push(fn);}};
  new Function('require', 'module', 'exports', code)(id => {
    if (id === 'react') return react;
    if (id === 'react-native') return {Platform: {OS: platform}, Text: 'Text', Image: 'Image', ActivityIndicator: 'ActivityIndicator'};
    if (id === 'expo-video') return {};
    if (id === './RecordAudio') return { PassiveAudio: 'PassiveAudio' };
    if (id === 'expo/fetch') return {fetch: () => {}};
    if (id === 'expo/node_modules/expo-file-system') return {File: class {}, Paths: {get cache() {if (platform === 'web') throw new Error('native cache unavailable'); return 'file:///cache/';}} };
    if (id === 'expo-crypto') return {randomUUID: () => 'random'};
    if (id.endsWith("/ThemeProvider")) return require("./testSupport/theme.cjs");
    if (id.endsWith('/styles')) return {styles: {}};
    if (id.endsWith('mediaSource.cjs')) return {createUploadPreviewSource: options => {passedOptions = options; return request;}};
    throw new Error(id);
  }, module, module.exports);
  return {finish, fail, effects, render: (type, props = {}) => {cursor = 0; return module.exports.AuthenticatedMedia({baseUrl: 'http://test/api/v1', token: 'test-token', onViewed, item: {id: 'upload', fileId: 'file', fileUrl: 'http://test/api/v1/uploads/upload/content', hasFile: true, type}, ...props});},
    get released() {return released;}, get reported() {return reported;}, get options() {return passedOptions;}};
}
for (const kind of ['image', 'video']) {
  test(kind + ' renderer receives local protected source and cleans up on decoder failure and close', async () => {
    const view = harness();
    assert.equal(view.render(kind).type, 'ActivityIndicator');
    const close = view.effects[0]();
    assert.equal(typeof view.options.fetchImpl, 'function');
    assert.ok(view.options.nativeFile);
    view.finish({uri: 'file:///cache/protected-media.mp4'});
    await Promise.resolve();
    const rendered = view.render(kind);
    assert.equal(typeof rendered.type === 'function' ? rendered.type.name : rendered.type, kind === 'image' ? 'Image' : 'PassiveVideo');
    assert.deepEqual(rendered.props.source, {uri: 'file:///cache/protected-media.mp4'});
    rendered.props.onError();
    assert.equal(view.reported, 1);
    assert.equal(view.released, 1);
    assert.equal(view.render(kind).type, 'Text');
    close();
    assert.equal(view.released, 2);
  });
}

test('web renderer does not evaluate native filesystem cache', () => {
  const view = harness('web');
  view.render('image');
  assert.doesNotThrow(() => view.effects[0]());
  assert.equal(view.options.nativeFile, undefined);
  assert.equal(view.options.fetchImpl, globalThis.fetch);
});

test('compact photo preview keeps its fallback during loading and decode failure, and releases the source', async () => {
  const view = harness('web');
  const fallback = {type: 'PhotoPlaceholder'}, imageStyle = {width: '100%', height: '100%'};
  const props = {fallback, imageStyle};
  assert.equal(view.render('image', props), fallback);
  const close = view.effects[0]();
  view.finish({uri: 'blob:protected-photo'});
  await Promise.resolve();
  const image = view.render('image', props);
  assert.equal(image.type, 'Image');
  assert.equal(image.props.style, imageStyle);
  assert.equal(image.props.resizeMode, 'cover');
  assert.doesNotThrow(() => image.props.onLoad());
  image.props.onError();
  assert.equal(view.render('image', props), fallback);
  assert.equal(view.released, 1);
  close();
});

test('unavailable photo preview uses the compact fallback without rendering server details', async () => {
  const view = harness('web');
  const fallback = {type: 'PhotoPlaceholder'};
  view.render('image', {fallback});
  const close = view.effects[0]();
  view.fail(new Error('private backend error detail'));
  await Promise.resolve();
  assert.equal(view.render('image', {fallback}), fallback);
  close();
});

for (const status of [503, 401, 404]) test(`media renderer distinguishes server ${status} without marking the record read`, async () => {
  const {ApiError} = require('./apiClient.cjs');
  const viewed = [];
  const view = harness('web', id => viewed.push(id));
  view.render('image');
  const close = view.effects[0]();
  const error = new ApiError({status, detail: 'private backend error detail'});
  view.fail(error);
  await Promise.resolve();
  const rendered = view.render('image');
  assert.equal(rendered.type, 'Text');
  assert.equal(rendered.props.children.join(''), status === 503 ? error.message : '이 파일은 안전하게 표시할 수 없습니다.');
  assert.deepEqual(viewed, []);
  close();
});

for (const kind of ['image', 'video']) {
  test(kind + ' is read only after successful display, once per open', async () => {
    const read = [];
    const view = harness('android', id => read.push(id));
    view.render(kind);
    const close = view.effects[0]();
    view.finish({uri: 'file:///cache/protected-media'});
    await Promise.resolve();
    const rendered = view.render(kind);
    assert.deepEqual(read, [], 'fetching bytes does not mark a record read');
    const displayed = kind === 'image' ? rendered.props.onLoad : rendered.props.onViewed;
    displayed();
    displayed();
    assert.deepEqual(read, ['upload']);
    close();
    displayed();
    assert.deepEqual(read, ['upload']);
  });

  for (const end of ['close', 'error']) {
    test(kind + ' late display after ' + end + ' cannot mark a record read', async () => {
      const read = [];
      const view = harness('android', id => read.push(id));
      view.render(kind);
      const close = view.effects[0]();
      view.finish({uri: 'file:///cache/protected-media'});
      await Promise.resolve();
      const rendered = view.render(kind);
      if (end === 'close') close(); else rendered.props.onError();
      (kind === 'image' ? rendered.props.onLoad : rendered.props.onViewed)();
      assert.deepEqual(read, []);
      if (end !== 'close') close();
    });
  }
}
