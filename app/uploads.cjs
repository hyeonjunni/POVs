const { requestWithDeadline } = require("./processing.cjs");

const MAX_UPLOAD_BYTES = 20 * 1024 * 1024;
const MEDIA_EXTENSIONS = {
  "image/jpeg": ["jpg", "jpeg"], "image/png": ["png"], "image/gif": ["gif"],
  "image/webp": ["webp"], "video/mp4": ["mp4", "m4v"],
  "audio/mpeg": ["mp3"], "audio/wav": ["wav"],
};

function normalizeUploadAsset(asset) {
  validateUploadAsset(asset);
  if (!/^(file|content|blob):\/\//.test(asset.uri || "") && !/^blob:/.test(asset.uri || "")) {
    throw new Error("이 파일 경로를 읽을 수 없습니다. 파일을 다시 선택해주세요.");
  }
  const uriName = String(asset.uri).split(/[?#]/)[0].split("/").pop();
  let fileName = asset.fileName || (/\.[a-z0-9]+$/i.test(uriName) ? uriName : "upload");
  const extension = fileName.split(".").pop().toLowerCase();
  const mimeType = asset.mimeType?.toLowerCase() || Object.keys(MEDIA_EXTENSIONS).find(type => MEDIA_EXTENSIONS[type].includes(extension));
  if (!MEDIA_EXTENSIONS[mimeType]) {
    throw new Error("지원하지 않거나 확인할 수 없는 파일 형식입니다. PNG, JPG, GIF, WebP 이미지, MP4 영상 또는 MP3, WAV 음성을 선택해주세요.");
  }
  if (!MEDIA_EXTENSIONS[mimeType].includes(extension)) {
    fileName = fileName.replace(/\.[^.]+$/, "") + "." + MEDIA_EXTENSIONS[mimeType][0];
  }
  return { ...asset, fileName, mimeType };
}

function validateUploadAsset(asset) {
  if (!Number.isSafeInteger(asset?.fileSize) || asset.fileSize <= 0) {
    throw new Error("파일 크기를 확인할 수 없습니다.");
  }
  if (asset.fileSize > MAX_UPLOAD_BYTES) {
    throw new Error("파일은 20 MiB 이하여야 합니다.");
  }
  return asset;
}

async function createUploadWithBinary(uploadsApi, payload, asset, { upload: existingUpload = null, assertCurrent = () => {} } = {}) {
  asset = normalizeUploadAsset(asset);
  assertCurrent();
  const upload = existingUpload || await uploadsApi.create(payload);
  try {
    assertCurrent();
    if (existingUpload) {
      // A timed-out POST may have committed. Reconcile before sending again.
      const rows = await uploadsApi.list(payload.room_id);
      assertCurrent();
      const stored = rows.find(row => row.upload_id === upload.upload_id);
      if (!stored) throw new Error("기록을 확인할 수 없습니다. 목록을 새로고침해주세요.");
      if (stored.has_file === true && stored.file_id) return { upload, uploadedFile: stored };
    }
    const uploadedFile = await uploadsApi.uploadBinary(upload.upload_id, asset);
    assertCurrent();
    if (uploadedFile?.has_file !== true || !uploadedFile.file_id || uploadedFile.upload_id !== upload.upload_id) {
      throw Object.assign(new Error("파일 저장 확인 정보가 없습니다. 같은 기록의 파일 상태를 다시 확인해주세요."), { code: "BINARY_NOT_CONFIRMED" });
    }
    return { upload, uploadedFile };
  } catch (error) {
    error.uploadStage = "binary";
    error.uploadId = upload.upload_id;
    error.upload = upload;
    throw error;
  }
}

function createUploadsApi(client, createFormData = () => new FormData(), { timeoutMs = 30000 } = {}) {
  const rawClient = client;
  client = Object.fromEntries(["get", "post"].map(method => [method, (path, options = {}) =>
    requestWithDeadline(signal => rawClient[method](path, { ...options, signal }), { timeoutMs })]));
  return {
    create: (payload) => client.post("/uploads", { json: payload }),
    list: (roomId) => client.get(`/uploads/${encodeURIComponent(roomId)}`),
    remove: (uploadId) => rawClient.delete(`/uploads/${encodeURIComponent(uploadId)}`),
    publishText: uploadId => client.post(`/uploads/${encodeURIComponent(uploadId)}/publish-text`),
    uploadBinary: async (uploadId, asset) => {
      asset = normalizeUploadAsset(asset);
      const formData = createFormData();
      if (asset.file) formData.append("file", asset.file, asset.fileName);
      else formData.append("file", {
        uri: asset.uri,
        name: asset.fileName,
        type: asset.mimeType,
      });
      return client.post(`/uploads/${encodeURIComponent(uploadId)}/binary`, { body: formData });
    },
  };
}

module.exports = { MAX_UPLOAD_BYTES, createUploadWithBinary, createUploadsApi, validateUploadAsset, normalizeUploadAsset };
