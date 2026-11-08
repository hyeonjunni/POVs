// Native host substitute for JSX unit tests. Use the real palette and style
// factories, leaving preference lifecycle coverage to theme.test.cjs.
const fs = require("node:fs");
const path = require("node:path");
const { createRequire } = require("node:module");
const babel = require("@babel/core");
const { getTheme } = require("../src/shared/theme/palettes.cjs");
const modules = new Map();
function load(relative) {
  const filename = path.resolve(__dirname, "..", relative);
  if (modules.has(filename)) return modules.get(filename);
  const localRequire = createRequire(filename);
  const module = { exports: {} };
  const code = babel.transformSync(fs.readFileSync(filename, "utf8"), {
    filename, babelrc: false, configFile: false, plugins: ["@babel/plugin-transform-modules-commonjs"],
  }).code;
  new Function("require", "module", "exports", code)(id => {
    if (id === "react-native") return { StatusBar: { currentHeight: 0 }, StyleSheet: { create: value => value } };
    if (id.startsWith(".") && !id.endsWith(".cjs")) return load(localRequire.resolve(id));
    return localRequire(id);
  }, module, module.exports);
  modules.set(filename, module.exports);
  return module.exports;
}
function appearance(id = "default") {
  const theme = getTheme(id);
  return { ...theme, themeId: theme.id, ready: true, error: "", changeTheme() {},
    styles: load("src/shared/theme/styles.js").createAppStyles(theme.colors),
    presentationStyles: load("src/shared/theme/tokens.js").createPresentationStyles(theme.colors),
    uiStyles: load("src/shared/ui/presentationStyles.js").createUiStyles(theme.colors),
  };
}
const theme = appearance();
module.exports = { appearance, useTheme: () => theme, useThemedStyles: factory => factory(theme.colors) };
