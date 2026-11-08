import { useTheme } from "../../shared/theme/ThemeProvider";
import React, { useRef, useState } from "react";
import { SafeAreaView, StatusBar, KeyboardAvoidingView, ScrollView, View, Text, TextInput, Pressable, ActivityIndicator } from "react-native";
import { StatusBar as ExpoStatusBar } from "expo-status-bar";
import { KEYBOARD_AVOIDING_BEHAVIOR, COMMON_SINGLE_LINE_INPUT_PROPS } from "../../shared/ui/layout";
export function LoginScreen({
  authLoading,
  authLoadingMessage,
  bottomInset,
  onLogin,
  onSignup,
  onShowPopupResult,
  onProfileLogin,
  loginProfiles = []
}) {
  const { styles, dark, colors } = useTheme();
  const [authMode, setAuthMode] = useState("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [pendingProfileId, setPendingProfileId] = useState(null);
  const authPending = useRef(false);
  const loginBusy = Boolean(authLoading || pendingProfileId);
  async function submitProfileLogin(profileId) {
    if (loginBusy || authPending.current || !onProfileLogin) return;
    authPending.current = true;
    setPendingProfileId(profileId);
    try {
      await onProfileLogin(profileId);
    } catch (_error) {
      // The shared login handler displays the normal login error popup.
    } finally {
      authPending.current = false;
      setPendingProfileId(null);
    }
  }
  async function submitAuth() {
    if (loginBusy || authPending.current) return;
    const cleanUsername = username.trim();
    const cleanPassword = password.trim();
    if (!cleanUsername || !cleanPassword) {
      onShowPopupResult("입력 필요", "아이디와 비밀번호를 입력해주세요.", {
        variant: "error"
      });
      return;
    }
    if (authMode === "signup") {
      const cleanName = name.trim();
      const cleanEmail = email.trim();
      if (!cleanName || !cleanEmail) {
        onShowPopupResult("입력 필요", "회원가입에는 이름과 이메일도 필요합니다.", {
          variant: "error"
        });
        return;
      }
      authPending.current = true;
      try {
        await onSignup({
          username: cleanUsername,
          password: cleanPassword,
          confirmPassword,
          name: cleanName,
          email: cleanEmail
        });
      } catch (_error) {} finally { authPending.current = false; }
      return;
    }
    authPending.current = true;
    try {
      await onLogin({
        username: cleanUsername,
        password: cleanPassword
      });
    } catch (_error) {} finally { authPending.current = false; }
  }
  return <SafeAreaView style={[styles.safeArea, styles.loginSafeArea]}>
      <ExpoStatusBar style={dark ? "light" : "dark"} />
      <StatusBar barStyle={dark ? "light-content" : "dark-content"} />
      <KeyboardAvoidingView style={styles.keyboardAvoidingContainer} behavior={KEYBOARD_AVOIDING_BEHAVIOR}>
        <ScrollView contentContainerStyle={[styles.loginScreen, {
        paddingBottom: 56 + bottomInset
      }]} showsVerticalScrollIndicator={false} keyboardShouldPersistTaps="handled" keyboardDismissMode="on-drag">
        <View style={styles.loginHero}>
          <Text style={styles.loginEyebrow}>POVs</Text>
          <Text style={styles.loginTitle}>{authMode === "login" ? "가족의 기억을 이어가요" : "함께 남길 기억의 시작"}</Text>
          <Text style={styles.loginDescription}>
            {authMode === "login" ? "나와 가족이 남긴 기록을 만나보세요." : "계정을 만들고 가족방에 참여하세요."}
          </Text>
        </View>

        <View style={styles.loginCard}>
          {authMode === "login" && onProfileLogin && loginProfiles.length > 0 ? <View style={styles.profileLoginSection}>
            <Text style={styles.loginCardTitle}>프로필로 바로 로그인</Text>
            <Text style={styles.loginHint}>사용할 프로필을 선택해주세요.</Text>
            <View style={styles.profileLoginGrid}>
              {loginProfiles.map(profile => <Pressable key={profile.id}
                accessibilityRole="button" accessibilityLabel={`${profile.id} ${profile.name} 프로필로 로그인`}
                accessibilityState={{ disabled: loginBusy, busy: pendingProfileId === profile.id }}
                style={[styles.profileLoginButton, pendingProfileId === profile.id && styles.profileLoginButtonSelected, loginBusy && styles.disabledButton]}
                onPress={() => submitProfileLogin(profile.id)} disabled={loginBusy}>
                <View style={styles.profileLoginBadge}><Text style={styles.profileLoginBadgeText}>{profile.id}</Text></View>
                <Text style={styles.profileLoginName}>{profile.name}</Text>
                <Text style={styles.profileLoginUsername}>{profile.username}</Text>
                {pendingProfileId === profile.id ? <View style={styles.profileLoginProgress}>
                  <ActivityIndicator size="small" color={colors.primaryStrong} />
                  <Text style={styles.loginHint}>로그인 중…</Text>
                </View> : <Text style={styles.profileLoginAction}>바로 로그인</Text>}
              </Pressable>)}
            </View>
            <View style={styles.profileLoginDivider} />
          </View> : null}
          <Text style={styles.loginCardTitle}>{authMode === "login" ? "로그인" : "회원가입"}</Text>

          <View style={styles.authFieldStack}>
            <View>
              <Text style={styles.inputLabel}>아이디</Text>
              <TextInput {...COMMON_SINGLE_LINE_INPUT_PROPS} value={username} onChangeText={setUsername} placeholder="예: family01" placeholderTextColor={colors.placeholder} autoCapitalize="none" autoCorrect={false} style={styles.textInput} />
            </View>

            <View>
              <Text style={styles.inputLabel}>비밀번호</Text>
              <TextInput {...COMMON_SINGLE_LINE_INPUT_PROPS} value={password} onChangeText={setPassword} placeholder="비밀번호를 입력해주세요" placeholderTextColor={colors.placeholder} autoCapitalize="none" autoCorrect={false} secureTextEntry style={styles.textInput} />
            </View>

            {authMode === "signup" ? <>
                <View>
                  <Text style={styles.inputLabel}>비밀번호 확인</Text>
                  <TextInput {...COMMON_SINGLE_LINE_INPUT_PROPS} value={confirmPassword} onChangeText={setConfirmPassword} placeholder="비밀번호를 다시 입력해주세요" placeholderTextColor={colors.placeholder} autoCapitalize="none" autoCorrect={false} secureTextEntry style={styles.textInput} />
                </View>

                <View>
                  <Text style={styles.inputLabel}>이름</Text>
                  <TextInput {...COMMON_SINGLE_LINE_INPUT_PROPS} value={name} onChangeText={setName} placeholder="예: 홍길동" placeholderTextColor={colors.placeholder} autoCapitalize="none" autoCorrect={false} style={styles.textInput} />
                </View>

                <View>
                  <Text style={styles.inputLabel}>이메일</Text>
                  <TextInput {...COMMON_SINGLE_LINE_INPUT_PROPS} value={email} onChangeText={setEmail} placeholder="예: name@example.com" placeholderTextColor={colors.placeholder} autoCapitalize="none" autoCorrect={false} keyboardType="email-address" style={styles.textInput} />
                </View>
              </> : null}
          </View>

          <Pressable style={[styles.demoButton, loginBusy && styles.disabledButton]} onPress={submitAuth} disabled={loginBusy}>
            <Text style={styles.demoButtonText}>
              {authLoading ? authLoadingMessage : authMode === "login" ? "로그인" : "회원가입"}
            </Text>
          </Pressable>
          <Pressable style={[styles.googleButton, loginBusy && styles.googleButtonDisabled]} onPress={() => { if (!authPending.current) setAuthMode(prev => prev === "login" ? "signup" : "login"); }} disabled={loginBusy}>
            <Text style={styles.googleButtonText}>
              {authMode === "login" ? "회원가입으로 전환" : "이미 계정이 있어요"}
            </Text>
          </Pressable>
          {authLoading ? <View style={styles.loginLoadingBox}>
              <ActivityIndicator size="small" color={colors.primary} />
              <Text style={styles.loginLoadingText}>{authLoadingMessage}</Text>
            </View> : null}
          <Text style={styles.loginHint}>
            {authMode === "login" ? "가입한 아이디와 비밀번호로 바로 로그인할 수 있습니다." : "계정 생성 결과에 따라 활성화 후 로그인이 필요할 수 있습니다."}
          </Text>
        </View>
        </ScrollView>
      </KeyboardAvoidingView>
    </SafeAreaView>;
}
