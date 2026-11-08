import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Platform } from "react-native";
import { fetch as fetchOriginal } from "expo/fetch";
import { File, Paths } from "expo/node_modules/expo-file-system";
import { randomUUID } from "expo-crypto";
import { runtimeApiClient, runtimeUnauthorizedHandler, getRuntimeAccessToken, getRuntimeApiBaseUrl } from "../../shared/api/runtime";
const { createChatSession, createGroundedChatApi, sameScope, chatErrorMessage } = require("./groundedChat.cjs");
const { createCitationOriginal } = require("./citationOriginal.cjs");

export function useGroundedChat({ userId, roomId }) {
  const identity = useRef({ userId, roomId });
  identity.current = { userId, roomId };
  const [, setRevision] = useState(null);
  const [original, setOriginal] = useState(null);
  const originalRequest = useRef(null);
  const sessionRef = useRef(null);
  function readScope() { return { ...identity.current, token: getRuntimeAccessToken(), baseUrl: getRuntimeApiBaseUrl() }; }
  if (!sessionRef.current) {
    sessionRef.current = createChatSession({
      readScope, onChange: setRevision,
      ask: ({ roomId: currentRoom, query, history, selectedRecordId, contextMode, baseUrl, signal }) => createGroundedChatApi({
        post: (...args) => runtimeApiClient.post(...args),
      }).ask({ roomId: currentRoom, query, history, selectedRecordId, contextMode, baseUrl, signal }),
    });
  }
  const session = sessionRef.current;
  const scope = readScope();
  function closeOriginal() { originalRequest.current?.release(); originalRequest.current = null; setOriginal(null); }
  useLayoutEffect(() => {
    session.updateScope(readScope());
    closeOriginal();
  }, [userId, roomId, scope.token, scope.baseUrl]);
  useEffect(() => {
    // Fast Refresh replays effects while retaining sessionRef. Reconnect that
    // session instead of leaving it permanently disposed after effect cleanup.
    session.connect();
    return () => { session.disconnect(); originalRequest.current?.release(); };
  }, [session]);

  async function openCitation(citation) {
    const captured = readScope();
    if (!captured.userId || !captured.roomId || !captured.token) return;
    const belongsToCurrentAnswer = session.getStateFor(captured).messages.some(message =>
      message.response?.citations.some(currentCitation => currentCitation === citation));
    if (!belongsToCurrentAnswer) return;
    closeOriginal();
    const request = createCitationOriginal({
      citation, baseUrl: captured.baseUrl, token: captured.token, platform: Platform.OS,
      fetchImpl: fetchOriginal,
      nativeFile: Platform.OS === "web" ? undefined : { File, cache: Paths.cache, randomUUID },
      onUnauthorized: requestToken => runtimeUnauthorizedHandler(requestToken),
    });
    originalRequest.current = request;
    setOriginal({ scope: captured, citation, loading: true, media: null, error: null });
    try {
      const media = await request.ready;
      if (originalRequest.current === request && sameScope(readScope(), captured)) {
        setOriginal({ scope: captured, citation, loading: false, media, error: null });
      }
    } catch (error) {
      if (originalRequest.current === request && sameScope(readScope(), captured) && error.name !== "AbortError") {
        setOriginal({ scope: captured, citation, loading: false, media: null, error: chatErrorMessage(error) });
      }
    }
  }
  const visibleOriginal = original && sameScope(original.scope, scope) ? original : null;
  return {
    ...session.getStateFor(scope), enabled: Boolean(userId && roomId && scope.token),
    statusLabel: userId && scope.token ? "가족방 선택" : "로그인 필요",
    disabledReason: userId && scope.token ? "가족방을 선택하면 가족 기록에 질문할 수 있습니다." : "실제 로그인 후 가족방을 선택하면 질문할 수 있습니다.",
    onChangeDraft: value => session.setDraft(value), onSend: () => session.send(),
    onAskRecord: record => session.send(`${record.title} 기록에는 어떤 내용이 있어?`, record.id),
    onNewConversation: () => { closeOriginal(); session.reset(); },
    onChangeContextMode: value => session.setContextMode(value),
    onRetry: message => session.retry(message), onDismissError: () => session.dismissError(),
    onOpenCitation: openCitation, openingCitation: visibleOriginal?.loading ? visibleOriginal.citation.fact_id : null,
    original: visibleOriginal, closeOriginal,
  };
}
