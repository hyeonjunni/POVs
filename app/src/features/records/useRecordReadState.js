import { useCallback, useEffect, useLayoutEffect, useReducer, useRef } from "react";
import AsyncStorage from "@react-native-async-storage/async-storage";
const { createReadScopeKey, createRecordReadState } = require("./readState.cjs");

export function useRecordReadState({ baseUrl, userId, roomId }) {
  const scope = { baseUrl, userId, roomId };
  const scopeKey = createReadScopeKey(scope);
  const currentKey = useRef(scopeKey);
  currentKey.current = scopeKey;
  const [, refresh] = useReducer(value => value + 1, 0);
  const stateRef = useRef(null);
  if (!stateRef.current) {
    stateRef.current = createRecordReadState({
      storage: AsyncStorage, onChange: refresh,
      readCurrentScopeKey: () => currentKey.current,
    });
  }
  const state = stateRef.current;
  useLayoutEffect(() => { state.setScope(scope); }, [state, scopeKey]);
  useEffect(() => {
    state.connect();
    return () => state.disconnect();
  }, [state]);
  const isUnread = useCallback(recordId => state.isUnread(recordId, scopeKey), [state, scopeKey]);
  const markRead = useCallback(recordId => state.markRead(recordId, scopeKey), [state, scopeKey]);

  return {
    ...state.getStateFor(scopeKey),
    isUnread,
    markRead,
  };
}
