import { useTheme } from "../../shared/theme/ThemeProvider";
import { useVideoPlayer, VideoView } from "expo-video";
import { fetch as expoFetch } from "expo/fetch";
import { File, Paths } from "expo/node_modules/expo-file-system";
import { randomUUID } from "expo-crypto";
import React, { useState, useEffect } from "react";
import { Platform, Text, ActivityIndicator, Image } from "react-native";
import { createUploadPreviewSource } from "../../../mediaSource.cjs";
import { PassiveAudio } from "./RecordAudio";
export function PassiveVideo({
  source,
  style,
  onError,
  onViewed
}) {
  const player = useVideoPlayer(source);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    const handleStatus = ({status}) => {
      setFailed(status === "error");
      if (status === "error") onError?.();
      if (status === "readyToPlay") onViewed?.();
    };
    const listener = player.addListener("statusChange", handleStatus);
    handleStatus({status: player.status});
    return () => {
      listener.remove();
      try {
        player.pause();
      } catch (_) {}
      if (Platform.OS === "web") {
        try {
          player.replace(null);
          player.release?.();
        } catch (_) {}
      }
    };
  }, [player, onError, onViewed]);
  return failed ? <Text>영상을 표시할 수 없습니다.</Text> : <VideoView player={player} nativeControls style={style} contentFit="contain" />;
}
export function AuthenticatedMedia({
  item,
  token,
  baseUrl,
  large = false,
  imageStyle,
  fallback,
  onViewed
}) {
  const { styles } = useTheme();
  const identity = [baseUrl, token, item.id, item.fileId, item.fileUrl, item.hasFile, item.type].join("\0");
  const [loaded, setLoaded] = useState(null);
  const [failed, setFailed] = useState(null);
  useEffect(() => {
    let active = true;
    let viewed = false;
    let decodeFailed = false;
    setLoaded(null);
    setFailed(null);
    const request = createUploadPreviewSource({
      baseUrl,
      upload: {
        upload_id: item.id,
        file_id: item.fileId,
        file_url: item.fileUrl,
        has_file: item.hasFile
      },
      token,
      platform: Platform.OS,
      kind: item.type,
      fetchImpl: Platform.OS === "web" ? globalThis.fetch : expoFetch,
      nativeFile: Platform.OS === "web" ? undefined : {File, cache: Paths.cache, randomUUID}
    });
    request.ready.then(source => {
      if (active) setLoaded({
        identity,
        source,
        onViewed: () => { if (active && !decodeFailed && !viewed) { viewed = true; onViewed?.(item.id); } },
        onError: () => {
          if (!active) return;
          decodeFailed = true;
          request.report();
          setFailed({ identity });
          request.release();
        }
      });
    }, error => {
      if (active && error.name !== "AbortError") setFailed({ identity, message: error.status === 503 ? error.message : null });
    });
    return () => {
      active = false;
      request.release();
    };
  }, [identity]);
  if (failed?.identity === identity) return fallback ?? <Text>{failed.message || "이 파일은 안전하게 표시할 수 없습니다."}</Text>;
  if (loaded?.identity !== identity) return fallback ?? <ActivityIndicator />;
  if (item.type === "voice" || item.type === "audio") {
    return <PassiveAudio key={identity} source={loaded.source} onError={loaded.onError} onViewed={loaded.onViewed} />;
  }
  const mediaStyle = large ? {
    width: "100%",
    height: 320
  } : styles.mediaPreviewImage;
  return item.type === "video" ? <PassiveVideo source={loaded.source} style={mediaStyle} onError={loaded.onError} onViewed={loaded.onViewed} /> : <Image source={loaded.source} style={imageStyle ?? mediaStyle} resizeMode={large ? "contain" : "cover"} onError={loaded.onError} onLoad={loaded.onViewed} />;
}
