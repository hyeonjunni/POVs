import * as DocumentPicker from "expo-document-picker";
import { pickVoiceAsset, releaseVoiceAsset } from "../../../mediaPicker.cjs";

// Match the image picker's asset fields so the existing confirmed-binary upload
// flow, room/session guards, and retry reconciliation also apply to voice files.
export function pickVoiceFile({ onError } = {}) {
  return pickVoiceAsset({ documentPicker: DocumentPicker, onError });
}

export const releaseVoiceFile = releaseVoiceAsset;
