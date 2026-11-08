import { useEffect, useRef, useState } from "react";
import AsyncStorage from "@react-native-async-storage/async-storage";

const STORAGE_KEY = "ambient.chatDisplayMode";

export function useChatDisplayMode(allowDeveloper = true) {
  const [mode, setMode] = useState("service");
  const [error, setError] = useState(null);
  const changed = useRef(false);
  useEffect(() => {
    if (!allowDeveloper) return;
    let active = true;
    AsyncStorage.getItem(STORAGE_KEY).then(value => {
      if (active && !changed.current && value === "developer") setMode(value);
    }).catch(() => { if (active) setError("화면 설정을 불러오지 못했습니다."); });
    return () => { active = false; };
  }, []);
  async function changeMode(value) {
    if (!allowDeveloper) return;
    changed.current = true;
    setMode(value);
    setError(null);
    try { await AsyncStorage.setItem(STORAGE_KEY, value); }
    catch { setError("화면 설정을 저장하지 못했습니다. 다시 선택해 주세요."); }
  }
  return { mode, changeMode, error };
}
