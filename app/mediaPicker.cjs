async function pickMediaAsset({ imagePicker, typeKey, onPermissionDenied, onError }) {
  try {
    const permission = await imagePicker.requestMediaLibraryPermissionsAsync();
    if (!permission.granted) {
      onPermissionDenied();
      return null;
    }
    const result = await imagePicker.launchImageLibraryAsync({
      mediaTypes: typeKey === "image" ? ["images"] : ["videos"],
      allowsEditing: false,
      quality: 1,
    });
    return result.canceled ? null : result.assets?.[0] || null;
  } catch (error) {
    onError(error);
    return null;
  }
}

const { normalizeUploadAsset } = require("./uploads.cjs");

function normalizeVoiceAsset(asset) {
  const fileName = asset?.name || asset?.fileName || "";
  const extension = fileName.split(".").pop().toLowerCase();
  const expected = { mp3: "audio/mpeg", wav: "audio/wav" }[extension];
  const mime = String(asset?.mimeType || "").toLowerCase().split(";")[0].trim();
  const canonical = { "audio/mp3": "audio/mpeg", "audio/x-mp3": "audio/mpeg", "audio/x-wav": "audio/wav", "audio/wave": "audio/wav", "audio/vnd.wave": "audio/wav" }[mime] || mime;
  if (!expected || (canonical && canonical !== "application/octet-stream" && canonical !== expected)) {
    throw new Error("음성은 MP3 또는 WAV 파일만 지원합니다. M4A, AAC, OGG 파일은 변환 후 선택해주세요.");
  }
  return normalizeUploadAsset({
    ...asset,
    fileName,
    fileSize: asset.size ?? asset.fileSize,
    mimeType: expected,
  });
}

function releaseVoiceAsset(asset, revokeObjectURL = url => URL.revokeObjectURL(url)) {
  // DocumentPicker copies native documents to its managed cache. Only its web
  // object URL belongs to this selection; never delete the user's source file.
  if (asset?.file && String(asset.uri || "").startsWith("blob:")) revokeObjectURL(asset.uri);
}

async function pickVoiceAsset({ documentPicker, onError = () => {} }) {
  let selected;
  try {
    const result = await documentPicker.getDocumentAsync({
      type: ["audio/mpeg", "audio/mp3", "audio/wav", "audio/x-wav"],
      multiple: false,
      copyToCacheDirectory: true,
      base64: false,
    });
    if (result.canceled || !result.assets?.[0]) return null;
    selected = result.assets[0];
    return normalizeVoiceAsset(selected);
  } catch (error) {
    releaseVoiceAsset(selected);
    onError(error);
    return null;
  }
}

module.exports = { pickMediaAsset, pickVoiceAsset, normalizeVoiceAsset, releaseVoiceAsset };
