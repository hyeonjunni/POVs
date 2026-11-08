const { ApiError } = require("../../../apiClient.cjs");
const { validateCitation } = require("./groundedChat.cjs");
const { fromByteArray } = require("base64-js");
const IMAGE_TYPES = new Set(["image/png", "image/jpeg", "image/gif", "image/webp"]);
const MAX_BYTES = 20 * 1024 * 1024;

function createCitationOriginal({ citation, baseUrl, token, platform, nativeFile, onUnauthorized = () => {}, fetchImpl = globalThis.fetch, timeoutMs = 20000 }) {
  const controller = new AbortController();
  let active = true, blobUrl = null, timer, temporary = null;
  function release() {
    active = false; controller.abort(); clearTimeout(timer);
    if (blobUrl) { URL.revokeObjectURL(blobUrl); blobUrl = null; }
    try { if (temporary?.exists) temporary.delete(); temporary = null; } catch (_) {}
  }
  const ready = (async () => {
    const uri = validateCitation(citation, baseUrl);
    if (typeof token !== "string" || !token.trim()) throw new Error("원본을 보려면 로그인해주세요.");
    const headers = { Authorization: `Bearer ${token}` };
    const load = async () => {
      const response = await fetchImpl(uri, { headers, signal: controller.signal, redirect: "error", credentials: "omit", cache: "no-store" });
      if (!response.ok) {
        let payload;
        try { payload = JSON.parse(await response.text()); } catch (_) { payload = null; }
        if (response.status === 401) await onUnauthorized(token);
        const detail = payload?.detail;
        throw new ApiError({ status: response.status, code: response.status === 409 ? "STALE_SOURCE" : payload?.code || detail?.code, detail });
      }
      const mime = (response.headers.get("content-type") || "").split(";")[0].trim().toLowerCase();
      const audio = citation.source_locator.kind === "multimodal" && citation.source_locator.media_type === "AUDIO";
      const kind = audio ? (["audio/wav", "audio/mpeg"].includes(mime) ? "audio" : null)
        : IMAGE_TYPES.has(mime) ? "image" : mime === "video/mp4" ? "video" : mime === "text/plain" ? "text" : null;
      const declaredSize = Number(response.headers.get("content-length") || 0);
      // Secure text originals intentionally use attachment + sandbox headers.
      // Read their bytes as plain text for React <Text>; never navigate to them.
      const blockedAttachment = kind !== "text" && /^attachment/i.test(response.headers.get("content-disposition") || "");
      if (!kind || declaredSize > MAX_BYTES || blockedAttachment || (audio && (response.status !== 200 || response.redirected))) {
        throw new Error("이 원본 형식은 대화 화면에서 안전하게 표시할 수 없습니다.");
      }
      if (kind === "text") {
        const text = await response.text();
        if (text.length > 250000) throw new Error("원본 텍스트가 표시 한도를 초과했습니다.");
        return { kind, text };
      }
      if (platform !== "web") {
        // Render the bytes already checked above. A second native HTTP request
        // could miss our 401/409 handling or follow a redirect with credentials.
        const bytes = new Uint8Array(await response.arrayBuffer());
        if (bytes.byteLength > MAX_BYTES) throw new Error("원본이 표시 한도를 초과했습니다.");
        if (!active) { const error = new Error("Original closed"); error.name = "AbortError"; throw error; }
        if (kind === "audio") {
          if (!bytes.byteLength || (declaredSize && declaredSize !== bytes.byteLength)) throw new Error("음성 원본이 완전하지 않습니다.");
          const { File, cache, randomUUID } = nativeFile;
          temporary = new File(cache, `protected-citation-${randomUUID()}.${mime === "audio/wav" ? "wav" : "mp3"}`);
          temporary.create(); temporary.write(bytes);
          return { kind, source: { uri: temporary.uri } };
        }
        return { kind, source: { uri: `data:${mime};base64,${fromByteArray(bytes)}` } };
      }
      const blob = await response.blob();
      if (blob.size > MAX_BYTES) throw new Error("원본이 표시 한도를 초과했습니다.");
      if (kind === "audio" && (!blob.size || (declaredSize && declaredSize !== blob.size))) throw new Error("음성 원본이 완전하지 않습니다.");
      if (!active) { const error = new Error("Original closed"); error.name = "AbortError"; throw error; }
      blobUrl = URL.createObjectURL(blob);
      return { kind, source: { uri: blobUrl } };
    };
    const deadline = new Promise((_, reject) => {
      timer = setTimeout(() => { const error = new Error("Original timed out"); error.name = "TimeoutError"; reject(error); controller.abort(); }, timeoutMs);
      controller.signal.addEventListener("abort", () => { const error = new Error("Original cancelled"); error.name = "AbortError"; reject(error); }, { once: true });
    });
    return await Promise.race([load(), deadline]);
  })().finally(() => clearTimeout(timer)).catch(error => { release(); throw error; });
  return { ready, release };
}

module.exports = { createCitationOriginal };
