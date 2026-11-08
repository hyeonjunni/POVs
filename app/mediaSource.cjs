// One authenticated GET; the viewer owns its validated blob/cache-file lifetime.
const {MAX_UPLOAD_BYTES} = require("./uploads.cjs");
const {ApiError} = require("./apiClient.cjs");
const TYPES = {
  image: new Set(["image/png", "image/jpeg", "image/gif", "image/webp"]),
  video: new Set(["video/mp4"]),
  voice: new Set(["audio/mpeg", "audio/wav"]),
  audio: new Set(["audio/mpeg", "audio/wav"]),
};
const SOURCE_QUERY_KEYS = ["expected_ciphertext_sha256", "file_id", "source_object_id", "source_object_version"];
const EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp", "video/mp4": "mp4", "audio/mpeg": "mp3", "audio/wav": "wav"};

function createUploadPreviewSource({baseUrl, upload, token, platform, kind,
  fetchImpl = globalThis.fetch, nativeFile, onDiagnostic = value => console.info("[protected-media]", value)}) {
  const controller = new AbortController();
  let active = true;
  let blobUrl = null;
  let temporary = null;
  const diagnostic = {upload_id: upload?.upload_id, status: null, media_type: null, byte_length: null, source_kind: null};
  function report(event = "decode_error") { onDiagnostic({...diagnostic, event}); }
  function release() {
    active = false;
    controller.abort();
    if (blobUrl) { URL.revokeObjectURL(blobUrl); blobUrl = null; }
    try {
      if (temporary?.exists) temporary.delete();
      temporary = null;
    } catch (_) {
      // Do not throw through React cleanup or expose a plaintext cache path.
      // Keep the handle so another release can retry deletion.
      report("cleanup_failed");
    }
  }
  const ready = (async () => {
    const base = new URL(baseUrl);
    if (!["https:", "http:"].includes(base.protocol) || base.username || base.password ||
        base.search || base.hash || !base.pathname.endsWith("/api/v1") || !token ||
        upload?.has_file !== true || !upload.upload_id || !upload.file_id || !TYPES[kind]) {
      throw new Error("Media source unavailable");
    }
    const uri = new URL(upload.file_url);
    const expectedPath = base.pathname.replace(/\/$/, "") + "/uploads/" + encodeURIComponent(upload.upload_id) + "/content";
    const queryKeys = Array.from(uri.searchParams.keys()).sort();
    // Upload DTOs in the current backend use a plain protected content URL.
    // When exact-source bindings are supplied, keep validating every binding.
    const validQuery = queryKeys.length === 0 || (
      queryKeys.length === SOURCE_QUERY_KEYS.length &&
      queryKeys.every((key, index) => key === SOURCE_QUERY_KEYS[index]) &&
      SOURCE_QUERY_KEYS.every((key) => uri.searchParams.getAll(key).length === 1 && uri.searchParams.get(key)) &&
      uri.searchParams.get("file_id") === upload.file_id
    );
    if (uri.origin !== base.origin || uri.pathname !== expectedPath || uri.hash ||
        uri.username || uri.password || !validQuery ||
        uri.searchParams.has("access_token")) {
      throw new Error("Media source unavailable");
    }
    const sourceUrl = uri.toString();
    const headers = {Authorization: "Bearer " + token};
    const response = await fetchImpl(sourceUrl, {
      headers, signal: controller.signal, redirect: "error", credentials: "omit", cache: "no-store",
    });
    const type = (response.headers.get("content-type") || "").split(";")[0].trim().toLowerCase();
    diagnostic.status = response.status;
    diagnostic.media_type = TYPES[kind].has(type) ? type : null;
    // A temporarily unavailable server does not imply an unsafe media file.
    // Keep other access/validation failures behind the existing generic message.
    if (response.status === 503) throw new ApiError({status: 503});
    const lengthHeader = response.headers.get("content-length");
    const contentLength = lengthHeader === null ? null : Number(lengthHeader);
    if (!response.ok || response.status !== 200 || response.redirected || !TYPES[kind].has(type) ||
        (contentLength !== null && (!/^\d+$/.test(lengthHeader) || !Number.isSafeInteger(contentLength) || contentLength <= 0 || contentLength > MAX_UPLOAD_BYTES)) ||
        /^attachment/i.test(response.headers.get("content-disposition") || "")) {
      throw new Error("This file cannot be displayed safely");
    }
    const bytes = new Uint8Array(await response.arrayBuffer());
    diagnostic.byte_length = bytes.byteLength;
    if (!active) {
      const error = new Error("Media request closed"); error.name = "AbortError"; throw error;
    }
    const encoding = response.headers.get("content-encoding");
    if (!bytes.byteLength || bytes.byteLength > MAX_UPLOAD_BYTES ||
        (contentLength !== null && (!encoding || encoding === "identity") && contentLength !== bytes.byteLength)) {
      throw new Error("This file cannot be displayed safely");
    }
    if (platform === "web") {
      blobUrl = URL.createObjectURL(new Blob([bytes], {type}));
      diagnostic.source_kind = "blob";
      report("ready");
      return {uri: blobUrl};
    }
    // Extension comes only from the allowlisted response type, never filenames.
    const {File, cache, randomUUID} = nativeFile;
    diagnostic.source_kind = "temp file";
    temporary = new File(cache, `protected-media-${randomUUID()}.${EXTENSIONS[type]}`);
    temporary.create();
    temporary.write(bytes);
    report("ready");
    return {uri: temporary.uri};
  })().catch((error) => { report(error.name === "AbortError" ? "cancelled" : "rejected"); release(); throw error; });
  return {ready, release, report};
}
module.exports = {createUploadPreviewSource};
