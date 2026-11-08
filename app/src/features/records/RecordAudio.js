import { useTheme, useThemedStyles } from "../../shared/theme/ThemeProvider";
import React, { useEffect, useRef } from "react";
import { Platform, View, Text, Pressable, StyleSheet, ActivityIndicator } from "react-native";
import { useAudioPlayer, useAudioPlayerStatus, setAudioModeAsync } from "expo-audio";

function clock(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "0:00";
  const whole = Math.floor(seconds);
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}

function WebAudio({ source, onError, onViewed }) {
  const { dark } = useTheme();
  const ref = useRef(null);
  useEffect(() => {
    const audio = ref.current;
    return () => {
      if (!audio) return;
      audio.pause();
      audio.removeAttribute("src");
      audio.load();
    };
  }, [source.uri]);
  return React.createElement("audio", {
    ref, src: source.uri, controls: true, preload: "metadata",
    "aria-label": "음성 기록 재생", style: { width: "100%", maxWidth: "100%", colorScheme: dark ? "dark" : "light" },
    onError, onPlay: onViewed,
  });
}

function NativeAudio({ source, onError, onViewed }) {
  const audioStyles = useThemedStyles(createAudioStyles);
  // The source is already authenticated and copied to the disposable cache by
  // createUploadPreviewSource. Do not pass a server URL or token to the player.
  const player = useAudioPlayer(source, { updateInterval: 500 });
  const status = useAudioPlayerStatus(player);
  const active = useRef(true);
  useEffect(() => {
    active.current = true;
    return () => {
      active.current = false;
      try { player.pause(); } catch (_) {}
      // useAudioPlayer releases the native player on unmount.
    };
  }, [player]);
  useEffect(() => {
    if (status.playing) onViewed?.();
  }, [status.playing, onViewed]);
  useEffect(() => {
    if (status.isLoaded) return;
    const timeout = setTimeout(() => { if (active.current) onError?.(); }, 20000);
    return () => clearTimeout(timeout);
  }, [status.isLoaded, onError]);
  useEffect(() => {
    if (status.playbackState === "error" || status.playbackState === "failed") onError?.();
  }, [status.playbackState, onError]);

  async function toggle() {
    try {
      if (status.playing) { player.pause(); return; }
      await setAudioModeAsync({ playsInSilentMode: true, shouldPlayInBackground: false });
      if (!active.current) return;
      if (status.didJustFinish || (status.duration > 0 && status.currentTime >= status.duration)) await player.seekTo(0);
      if (active.current) player.play();
    } catch (_) { if (active.current) onError?.(); }
  }
  async function seek(delta) {
    try {
      await player.seekTo(Math.max(0, Math.min(status.duration || 0, (status.currentTime || 0) + delta)));
    } catch (_) { if (active.current) onError?.(); }
  }

  return <View style={audioStyles.card}>
    <Text style={audioStyles.title}>음성 기록</Text>
    {!status.isLoaded ? <View style={audioStyles.loading}><ActivityIndicator /><Text style={audioStyles.time}>음성을 준비하고 있습니다.</Text></View> : null}
    <Text style={audioStyles.time}>{clock(status.currentTime)} / {clock(status.duration)}</Text>
    <View style={audioStyles.actions}>
      <Pressable accessibilityRole="button" accessibilityLabel="10초 뒤로" disabled={!status.isLoaded} onPress={() => seek(-10)} style={audioStyles.secondary}><Text style={audioStyles.secondaryText}>−10초</Text></Pressable>
      <Pressable accessibilityRole="button" disabled={!status.isLoaded} onPress={toggle} style={[audioStyles.play, !status.isLoaded && audioStyles.disabled]}><Text style={audioStyles.playText}>{status.playing ? "일시 정지" : "재생"}</Text></Pressable>
      <Pressable accessibilityRole="button" accessibilityLabel="10초 앞으로" disabled={!status.isLoaded} onPress={() => seek(10)} style={audioStyles.secondary}><Text style={audioStyles.secondaryText}>+10초</Text></Pressable>
    </View>
  </View>;
}

export function PassiveAudio(props) {
  return Platform.OS === "web" ? <WebAudio {...props} /> : <NativeAudio {...props} />;
}

const createAudioStyles = colors => StyleSheet.create({
  card: { width: "100%", padding: 16, gap: 14, borderRadius: 16, backgroundColor: colors.primarySoft },
  title: { fontSize: 16, fontWeight: "700", color: colors.text },
  loading: { flexDirection: "row", gap: 8, alignItems: "center" },
  time: { color: colors.muted, textAlign: "center", fontSize: 14 },
  actions: { flexDirection: "row", alignItems: "center", gap: 8 },
  play: { flex: 1, minHeight: 48, alignItems: "center", justifyContent: "center", borderRadius: 12, backgroundColor: colors.primary },
  playText: { color: colors.onPrimary, fontSize: 15, fontWeight: "700" },
  secondary: { minHeight: 48, paddingHorizontal: 10, justifyContent: "center" },
  secondaryText: { color: colors.primaryStrong, fontSize: 14, fontWeight: "600" },
  disabled: { opacity: 0.5 },
});
