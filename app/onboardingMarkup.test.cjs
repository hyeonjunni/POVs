const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const babel = require("@babel/core");
const React = require("react");

// Render the actual JSX with native host elements and deterministic hook state.
function render(file, name, props, state = {}) {
  const filename = path.join(__dirname, "src/features", file);
  const code = babel.transformSync(fs.readFileSync(filename, "utf8"), {
    filename, configFile: false, babelrc: false,
    plugins: [function ({ types }) {
      return { visitor: { CallExpression(p) {
        if (p.node.callee.name === "useState" && p.parent.id?.type === "ArrayPattern") {
          p.node.arguments.push(types.stringLiteral(p.parent.id.elements[0].name));
        }
      } } };
    }, [require("@babel/plugin-transform-react-jsx"), { runtime: "classic" }],
    require("@babel/plugin-transform-modules-commonjs")],
  }).code;
  const react = { ...React,
    useState: (initial, key) => [Object.hasOwn(state, key) ? state[key] : initial, () => {}],
    useRef: initial => ({ current: initial }), useEffect: () => {},
  };
  const native = new Proxy({
    Platform: { OS: "android" },
    Animated: {
      Value: class { setValue() {} },
      multiply: () => ({ interpolate: () => 1 }), event: () => () => {},
    },
  }, { get: (target, key) => key === "__esModule" ? false : target[key] || key });
  const exports = {};
  vm.runInNewContext(code, { exports, require: id => {
    if (id === "react") return react;
    if (id === "react-native") return native;
    if (id === "expo-status-bar") return { StatusBar: "StatusBar" };
    if (id === "react-native-gesture-handler") return { State: {}, GestureHandlerRootView: "View", PinchGestureHandler: "View" };
    if (["./PersonalContextEditor", "./ThemePicker"].includes(id)) return { [id.slice(2)]: id.slice(2) };
    if (id === "../family/InviteCodeCopyButton") return { InviteCodeCopyButton: "InviteCodeCopyButton" };
    if (id.endsWith("/ThemeProvider")) return require("./testSupport/theme.cjs");
    if (id.endsWith("/styles")) return { styles: {} };
    if (id.endsWith("/layout")) return { COMMON_SINGLE_LINE_INPUT_PROPS: {}, BASE_SCROLL_BOTTOM_PADDING: 112 };
    if (id.endsWith("/relations")) return { buildFamilyTreeLevels: () => [], getMemberDisplayName: member => member.name, getFamilyRelationLabel: () => "", getRoleLabel: value => value };
    throw new Error(`Unexpected import: ${id}`);
  } }, { filename });
  return exports[name](props);
}

function visible(node, result = { text: [], inputs: [] }) {
  if (Array.isArray(node)) node.forEach(child => visible(child, result));
  else if (typeof node === "string" || typeof node === "number") result.text.push(String(node));
  else if (node?.props && !(node.type === "Modal" && node.props.visible === false)) {
    if (node.type === "TextInput") result.inputs.push(node.props);
    visible(node.props.children, result);
  }
  return result;
}

function assertCleanCopy(output) {
  // Only fixture-controlled labels/placeholders, never sanitize arbitrary user data.
  for (const copy of [...output.text, ...output.inputs.map(input => input.placeholder || "")]) {
    assert.doesNotMatch(copy, /\\u[0-9a-f]{4}|<\/?(?:Text|View|Pressable|TextInput)\b|&lt;|\[object Object\]/i);
  }
}

const profileProps = {
  profileModalVisible: true, profileEditorMode: "edit", bottomInset: 0, keyboardInset: 0,
  profileDraft: { name: "", age: "", gender: "", phone: "", email: "" },
};
const familyProps = { user: { name: "테스트" }, familyRooms: [], bottomInset: 0 };

test("signup and name setup render Korean labels and placeholders without implementation markup", () => {
  const output = visible(render("auth/LoginScreen.js", "LoginScreen", { bottomInset: 0 }, { authMode: "signup" }));
  assertCleanCopy(output);
  assert.ok(output.text.includes("이름"));
  assert.ok(output.inputs.some(input => input.placeholder === "예: 홍길동"));
  assert.ok(output.inputs.some(input => input.placeholder === "예: name@example.com"));
});

test("profile name, age, gender, phone and email setup render intended copy", () => {
  for (const mode of ["edit", "onboarding"]) {
    const output = visible(render("profile/ProfileEditor.js", "ProfileEditor", { ...profileProps, profileEditorMode: mode }));
    assertCleanCopy(output);
    assert.ok(output.inputs.some(input => input.placeholder === "예: 24"));
    assert.ok(output.inputs.some(input => input.placeholder === "예: 010-1234-5678"));
    for (const label of ["나이", "성별", "남성", "여성", "기타", "휴대폰번호"]) assert.ok(output.text.includes(label));
    if (mode === "edit") assert.ok(output.inputs.some(input => input.placeholder === "예: 홍길동"));
  }
});

test("family room name setup renders intended Korean placeholder", () => {
  const output = visible(render("profile/MyPageScreen.js", "MyPageScreen", familyProps));
  assertCleanCopy(output);
  assert.ok(output.text.includes("가족방 이름"));
  assert.equal(output.inputs[0].placeholder, "예: 우리 가족방");
});

test("invite and join setup render intended copy", () => {
  const output = visible(render("profile/MyPageScreen.js", "MyPageScreen", familyProps, {
    familyMenu: "join", joinPreview: { name: "테스트 가족", members: [{ user_id: "member", name: "가족" }] },
  }));
  assertCleanCopy(output);
  assert.equal(output.inputs[0].placeholder, "예: A1B2C3");
  assert.ok(output.text.includes("가족 관계 선택"));
});

test("legitimate angle brackets in user names and room input remain intact", () => {
  const value = "<우리 가족>";
  const profile = visible(render("profile/ProfileEditor.js", "ProfileEditor", {
    ...profileProps, profileDraft: { ...profileProps.profileDraft, name: value },
  }));
  assert.equal(profile.inputs[0].value, value);
  const family = visible(render("profile/MyPageScreen.js", "MyPageScreen", {
    ...familyProps, user: { name: value },
  }, { familyName: value }));
  assert.equal(family.inputs[0].value, value);
  assert.ok(family.text.includes(value));
});
