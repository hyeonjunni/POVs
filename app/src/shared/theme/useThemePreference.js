import { useEffect, useRef, useState } from "react";
import AsyncStorage from "@react-native-async-storage/async-storage";
import { DEFAULT_THEME_ID, THEME_STORAGE_KEY, getTheme } from "./palettes.cjs";

export function useThemePreference() {
  const [themeId, setThemeId] = useState(DEFAULT_THEME_ID);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState("");
  const revision = useRef(0);
  const writes = useRef(Promise.resolve());
  const mounted = useRef(false);

  useEffect(() => {
    mounted.current = true;
    let cancelled = false;
    const startedAt = revision.current;
    (async () => {
      try {
        const saved = await AsyncStorage.getItem(THEME_STORAGE_KEY);
        if (!cancelled && revision.current === startedAt) setThemeId(getTheme(saved).id);
      } catch (_) {
        if (!cancelled && revision.current === startedAt) setError("저장된 테마를 불러오지 못했습니다. 다시 선택해주세요.");
      } finally {
        if (!cancelled) setReady(true);
      }
    })();
    return () => { cancelled = true; mounted.current = false; };
  }, []);

  function changeTheme(id) {
    const next = getTheme(id).id;
    const currentRevision = ++revision.current;
    setThemeId(next);
    setError("");
    // Serialize rapid selections: an older write must never win on relaunch.
    writes.current = writes.current.catch(() => {}).then(() => AsyncStorage.setItem(THEME_STORAGE_KEY, next));
    return writes.current.catch(() => {
      if (mounted.current && revision.current === currentRevision) {
        setError("테마를 저장하지 못했습니다. 현재 화면에는 적용되었습니다. 다시 선택해 저장해주세요.");
      }
    });
  }

  return { themeId, ready, error, changeTheme };
}
