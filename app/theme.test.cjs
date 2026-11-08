const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { createRequire } = require("node:module");
const babel = require("@babel/core");
const React = require("react");
const { THEMES, THEME_STORAGE_KEY, getTheme } = require("./src/shared/theme/palettes.cjs");
const { appearance } = require("./testSupport/theme.cjs");

const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
function storage() {
  const values = new Map();
  return { values, getItem: async key => values.get(key) ?? null, setItem: async (key, value) => { values.set(key, value); } };
}

// Real React reconciliation/effects; substitute only native hosts and disk I/O.
function fixture(disk, { preferenceOnly = false } = {}) {
  const modules = new Map(), buttons = new Map();
  let current, setDraft, mounts = 0, unmounts = 0;
  const container = ({ children }) => children;
  const native = { StatusBar: { currentHeight: 0 }, StyleSheet: { create: value => value },
    View: container, Text: () => null, ActivityIndicator: () => null,
    Pressable: props => { buttons.set(props.accessibilityLabel, props); return props.children; },
  };
  const privateInterface = {
    ReactNativeViewConfigRegistry: { customBubblingEventTypes: {}, customDirectEventTypes: {} },
    RCTEventEmitter: { register() {} }, ReactFiberErrorDialog: { showErrorDialog: () => false },
    UIManager: { setChildren() {}, manageChildren() {} },
  };
  const rendererModule = { exports: {} };
  new Function("require", "module", "exports", "__DEV__", fs.readFileSync(require.resolve(
    "react-native/Libraries/Renderer/implementations/ReactNativeRenderer-dev.js"), "utf8"))(id => {
    if (id.endsWith("ReactNativePrivateInitializeCore")) return {};
    if (id.endsWith("ReactNativePrivateInterface")) return privateInterface;
    return require(id);
  }, rendererModule, rendererModule.exports, true);
  const renderer = rendererModule.exports;
  function load(relative) {
    const filename = path.resolve(__dirname, relative);
    if (modules.has(filename)) return modules.get(filename).exports;
    const module = { exports: {} }; modules.set(filename, module);
    const localRequire = createRequire(filename);
    const code = babel.transformSync(fs.readFileSync(filename, "utf8"), {
      filename, babelrc: false, configFile: false, presets: ["module:@react-native/babel-preset"],
    }).code;
    new Function("require", "module", "exports", code)(id => {
      if (id === "react") return React;
      if (id === "react-native") return native;
      if (id === "expo-status-bar") return { StatusBar: () => null };
      if (id === "@react-native-async-storage/async-storage") return disk;
      if (id.startsWith(".") && !id.endsWith(".cjs")) return load(localRequire.resolve(id));
      return localRequire(id);
    }, module, module.exports);
    return module.exports;
  }
  const { ThemeProvider, useTheme } = load("src/shared/theme/ThemeProvider.js");
  const { useThemePreference } = load("src/shared/theme/useThemePreference.js");
  const { ThemePicker } = load("src/features/profile/ThemePicker.js");
  function Probe() {
    const theme = preferenceOnly ? useThemePreference() : useTheme();
    const [draft, updateDraft] = React.useState("");
    setDraft = updateDraft;
    React.useEffect(() => { mounts++; return () => { unmounts++; }; }, []);
    current = { ...theme, draft };
    return preferenceOnly ? null : React.createElement(ThemePicker);
  }
  return {
    async render() { await React.act(async () => renderer.render(preferenceOnly ? React.createElement(Probe) : React.createElement(ThemeProvider, null, React.createElement(Probe)), 1)); },
    async close() { await React.act(async () => renderer.unmountComponentAtNode(1)); },
    async flush(action = () => {}) { await React.act(async () => { await action(); }); },
    async type(value) { await React.act(async () => setDraft(value)); },
    get value() { return current; }, get mounts() { return mounts; }, get unmounts() { return unmounts; },
    radios() { return [...buttons.values()].filter(button => button.accessibilityRole === "radio"); },
  };
}

test("theme picker switches all roles, preserves mounted draft and restores choice on relaunch", async () => {
  const disk = storage(), first = fixture(disk);
  await first.render();
  assert.equal(first.value.themeId, "default");
  assert.equal(first.radios().length, 3);
  await first.type("가족 기록에 대한 작성 중 질문");
  for (const id of ["dark", "sage", "default", "dark"]) {
    const radio = first.radios().find(item => item.accessibilityLabel.startsWith(getTheme(id).name));
    await first.flush(() => radio.onPress());
    assert.equal(first.value.themeId, id);
    assert.equal(first.radios().filter(item => item.accessibilityState.checked).length, 1);
    assert.equal(first.value.styles.appFrame.backgroundColor, getTheme(id).colors.background);
    assert.equal(first.value.uiStyles.tabBar.backgroundColor, getTheme(id).colors.surface);
    assert.equal(first.value.draft, "가족 기록에 대한 작성 중 질문");
    assert.equal(first.mounts, 1);
    assert.equal(first.unmounts, 0);
  }
  assert.equal(disk.values.get(THEME_STORAGE_KEY), "dark");
  await first.close();
  const relaunched = fixture(disk);
  await relaunched.render();
  assert.equal(relaunched.value.themeId, "dark");
  assert.equal(relaunched.radios().find(item => item.accessibilityLabel.startsWith("다크")).accessibilityState.checked, true);
  await relaunched.close();
});

test("late preference read cannot override a new choice and rapid writes finish in selection order", async () => {
  const read = deferred(), write = deferred(), calls = [];
  const disk = { getItem: () => read.promise, setItem: async (key, value) => { calls.push(value); if (calls.length === 1) await write.promise; } };
  const h = fixture(disk, { preferenceOnly: true });
  await h.render();
  let first, second;
  await h.flush(() => { first = h.value.changeTheme("dark"); second = h.value.changeTheme("sage"); });
  assert.deepEqual(calls, ["dark"]);
  assert.equal(h.value.themeId, "sage");
  await h.flush(() => read.resolve("default"));
  assert.equal(h.value.themeId, "sage");
  await h.flush(() => write.resolve());
  await Promise.all([first, second]);
  assert.deepEqual(calls, ["dark", "sage"]);
  await h.close();
});

test("invalid or failed restoration falls back and failed saving stays usable with retry", async () => {
  const corrupt = fixture({ getItem: async () => "not-a-theme", setItem: async () => {} });
  await corrupt.render();
  assert.equal(corrupt.value.themeId, "default");
  await corrupt.close();
  let failSave = true;
  const disk = { getItem: async () => { throw new Error("read failed"); }, setItem: async () => { if (failSave) throw new Error("write failed"); } };
  const h = fixture(disk);
  await h.render();
  assert.equal(h.value.ready, true);
  assert.match(h.value.error, /불러오지 못했습니다/);
  await h.flush(() => h.value.changeTheme("dark"));
  assert.equal(h.value.themeId, "dark");
  assert.match(h.value.error, /저장하지 못했습니다/);
  failSave = false;
  await h.flush(() => h.value.changeTheme("dark"));
  assert.equal(h.value.error, "");
  await h.close();
});

function contrast(a, b) {
  const luminance = hex => {
    const rgb = [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16) / 255).map(x => x <= 0.04045 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4);
    return rgb[0] * 0.2126 + rgb[1] * 0.7152 + rgb[2] * 0.0722;
  };
  const values = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (values[0] + 0.05) / (values[1] + 0.05);
}
test("default colors stay familiar and new themes keep text, buttons, modal surfaces readable", () => {
  assert.deepEqual([getTheme("default").colors.background, getTheme("default").colors.surface, getTheme("default").colors.primary], ["#F8FAFD", "#FFFFFF", "#527AAE"]);
  for (const theme of THEMES) {
    const c = theme.colors, { styles } = appearance(theme.id);
    for (const [foreground, background] of [[c.text, c.background], [c.text, c.surface], [c.muted, c.surface], [c.primaryStrong, c.primarySoft], [c.onPrimary, c.primaryStrong], [c.onDanger, c.danger]]) {
      assert.ok(contrast(foreground, background) >= 4.5, `${theme.id}: ${foreground} on ${background}`);
    }
    for (const key of ["profileModalSheet", "uploadLoadingSheet", "memberDetailSheet", "joinFamilyRelationSheet", "homeChooserSheet"]) {
      assert.equal(styles[key].backgroundColor, c.surface, `${theme.id}: ${key}`);
    }
    assert.ok(contrast(styles.submitButtonText.color, styles.submitButton.backgroundColor) >= 4.5);
  }
});
