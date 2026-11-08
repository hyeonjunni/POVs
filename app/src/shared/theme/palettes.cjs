const DEFAULT_THEME_ID = "default";
const THEME_STORAGE_KEY = "ambient.appearance.theme.v1";

// Keep the existing palette as the default. Foreground-on-accent is separate
// from the surface color so filled buttons stay readable in dark mode.
const defaultColors = {
  background: "#F8FAFD", surface: "#FFFFFF", text: "#1E2C43", muted: "#637287",
  primary: "#527AAE", primaryStrong: "#4B6F9F", primarySoft: "#EAF2FC", border: "#E2E9F2",
  danger: "#B4464D", dangerSoft: "#FCF0F1", dangerBorder: "#E8CBCD", success: "#327663",
  onPrimary: "#FFFFFF", onDanger: "#FFFFFF", placeholder: "#94A3B8",
  scrim: "rgba(30,44,67,0.4)", coverOverlay: "rgba(255,255,255,0.94)",
  secondarySoft: "#FCE7F3",
};

const THEMES = [
  { id: "default", name: "기본", description: "익숙한 파스텔 블루", dark: false, colors: defaultColors },
  { id: "dark", name: "다크", description: "은은한 블루와 차콜", dark: true, colors: {
    background: "#121A26", surface: "#1B2636", text: "#EDF2F9", muted: "#AFBED1",
    primary: "#9EBEE7", primaryStrong: "#B3CDF0", primarySoft: "#26374C", border: "#35465B",
    danger: "#F0A1A8", dangerSoft: "#402A34", dangerBorder: "#754552", success: "#8CCBB4",
    onPrimary: "#172333", onDanger: "#28151A", placeholder: "#A0B0C4",
    scrim: "rgba(0,0,0,0.65)", coverOverlay: "rgba(27,38,54,0.94)", secondarySoft: "#463246",
  } },
  { id: "sage", name: "세이지", description: "차분한 그린과 아이보리", dark: false, colors: {
    background: "#F4F7F2", surface: "#FFFEFA", text: "#283B31", muted: "#617367",
    primary: "#527762", primaryStrong: "#426650", primarySoft: "#E5EEE2", border: "#DCE5D8",
    danger: "#A8414D", dangerSoft: "#FAEFF0", dangerBorder: "#E7C8CE", success: "#35694C",
    onPrimary: "#FFFFFF", onDanger: "#FFFFFF", placeholder: "#78877B",
    scrim: "rgba(28,43,33,0.4)", coverOverlay: "rgba(255,254,250,0.94)", secondarySoft: "#EDE4DB",
  } },
];
for (const theme of THEMES) { Object.freeze(theme.colors); Object.freeze(theme); }
Object.freeze(THEMES);

function getTheme(id) {
  return THEMES.find(theme => theme.id === id) || THEMES[0];
}

module.exports = { DEFAULT_THEME_ID, THEME_STORAGE_KEY, THEMES, getTheme };
