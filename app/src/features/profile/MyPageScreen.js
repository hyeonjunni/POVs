import { useTheme } from "../../shared/theme/ThemeProvider";
import React, { useState, useRef, useEffect } from "react";
import { Animated, KeyboardAvoidingView, ScrollView, View, Image, Text, Pressable, TextInput, Modal } from "react-native";
import { State, GestureHandlerRootView, PinchGestureHandler } from "react-native-gesture-handler";
import { KEYBOARD_AVOIDING_BEHAVIOR, BASE_SCROLL_BOTTOM_PADDING, COMMON_SINGLE_LINE_INPUT_PROPS } from "../../shared/ui/layout";
import { getMemberDisplayName, getFamilyRelationLabel, getRoleLabel, buildFamilyTreeLevels } from "../family/relations";
import { PersonalContextEditor } from "./PersonalContextEditor";
import { ThemePicker } from "./ThemePicker";
import { InviteCodeCopyButton } from "../family/InviteCodeCopyButton";
export function MyPageScreen({
  chatDisplay,
  user,
  familyRooms,
  activeFamily,
  bottomInset,
  onCreateFamily,
  onPrepareJoinFamily,
  onJoinFamily,
  onLogout,
  onDeleteAccount,
  onOpenProfileEditor,
  onDeleteFamily,
  onSelectFamily
}) {
  const { styles, colors } = useTheme();
  const [familyMenu, setFamilyMenu] = useState("create");
  const [familyName, setFamilyName] = useState("");
  const [inviteCode, setInviteCode] = useState("");
  const [selectedMember, setSelectedMember] = useState(null);
  const [joinPreview, setJoinPreview] = useState(null);
  const [joinRelationTargetUserId, setJoinRelationTargetUserId] = useState("");
  const [joinRelationType, setJoinRelationType] = useState("child");
  const [familyTreeVisible, setFamilyTreeVisible] = useState(false);
  const [personalContextScope, setPersonalContextScope] = useState(null);
  const currentPersonalScope = user?.id && activeFamily?.id ? `${user.id}:${activeFamily.id}` : null;
  useEffect(() => { setPersonalContextScope(null); }, [currentPersonalScope]);
  const familyTreeScale = useRef(new Animated.Value(1)).current;
  const familyTreeBaseScale = useRef(1);
  const familyTreePinchScale = useRef(new Animated.Value(1)).current;
  const familyTreeCombinedScale = Animated.multiply(familyTreeScale, familyTreePinchScale).interpolate({
    inputRange: [0.65, 1, 2.4],
    outputRange: [0.65, 1, 2.4],
    extrapolate: "clamp"
  });
  const handleFamilyTreePinchGesture = Animated.event([{
    nativeEvent: {
      scale: familyTreePinchScale
    }
  }], {
    useNativeDriver: true
  });
  function handleFamilyTreePinchStateChange(event) {
    const nativeEvent = event.nativeEvent;
    if (nativeEvent.oldState === State.ACTIVE) {
      const nextScale = Math.min(2.4, Math.max(0.65, familyTreeBaseScale.current * nativeEvent.scale));
      familyTreeBaseScale.current = nextScale;
      familyTreeScale.setValue(nextScale);
      familyTreePinchScale.setValue(1);
    }
  }
  useEffect(() => {
    if (familyTreeVisible) {
      familyTreeBaseScale.current = 1;
      familyTreeScale.setValue(1);
      familyTreePinchScale.setValue(1);
    }
  }, [familyTreeVisible, familyTreeScale, familyTreePinchScale]);
  async function submitCreate() {
    const success = await onCreateFamily(familyName);
    if (success) {
      setFamilyName("");
    }
  }
  async function submitJoin() {
    const preview = await onPrepareJoinFamily(inviteCode);
    if (!preview) {
      return;
    }
    const previewMembers = Array.isArray(preview.members) ? preview.members : [];
    if (previewMembers.length === 0) {
      const success = await onJoinFamily(inviteCode);
      if (success) {
        setInviteCode("");
      }
      return;
    }
    setJoinPreview(preview);
    setJoinRelationTargetUserId(previewMembers[0]?.user_id || "");
    setJoinRelationType("child");
  }
  async function submitJoinWithRelation() {
    if (!joinPreview) {
      return;
    }
    if (!joinRelationTargetUserId) {
      return;
    }
    const success = await onJoinFamily(inviteCode, {
      relatedToUserId: joinRelationTargetUserId,
      relationToRelatedUser: joinRelationType
    });
    if (success) {
      setInviteCode("");
      setJoinPreview(null);
      setJoinRelationTargetUserId("");
      setJoinRelationType("child");
    }
  }
  function closeJoinPreview() {
    setJoinPreview(null);
    setJoinRelationTargetUserId("");
    setJoinRelationType("child");
  }
  return <>
      <KeyboardAvoidingView style={styles.keyboardAvoidingContainer} behavior={KEYBOARD_AVOIDING_BEHAVIOR}>
      <ScrollView contentContainerStyle={[styles.scrollContent, {
        paddingBottom: BASE_SCROLL_BOTTOM_PADDING + bottomInset
      }]} showsVerticalScrollIndicator={false} keyboardShouldPersistTaps="handled" keyboardDismissMode="on-drag">
        <ThemePicker />
        {chatDisplay ? <View style={styles.profileCard}>
          <Text style={styles.profileName}>대화 화면</Text>
          <View style={styles.joinRelationToggleRow}>
            {[{ value: "service", label: "서비스용" }, { value: "developer", label: "개발자용" }].map(option => <Pressable key={option.value}
              accessibilityRole="radio" accessibilityLabel={`${option.label} 대화 화면`} accessibilityState={{ checked: chatDisplay.mode === option.value }}
              onPress={() => chatDisplay.changeMode(option.value)}
              style={[styles.joinRelationToggle, chatDisplay.mode === option.value && styles.joinRelationToggleActive]}>
              <Text style={[styles.joinRelationToggleText, chatDisplay.mode === option.value && styles.joinRelationToggleTextActive]}>{option.label}</Text>
            </Pressable>)}
          </View>
          <Text style={styles.profileMetaText}>{chatDisplay.mode === "service" ? "답변과 요청한 사진·음성·영상을 간결하게 보여줍니다." : "검색 범위와 근거 기록의 상세 정보를 함께 보여줍니다."}</Text>
          {chatDisplay.error ? <Text accessibilityRole="alert" style={styles.profileMetaText}>{chatDisplay.error}</Text> : null}
        </View> : null}
        <View style={styles.profileCard}>
          <View style={styles.profileTopRow}>
            {user?.picture ? <Image source={{
              uri: user.picture
            }} style={styles.profileAvatar} /> : <View style={styles.profileAvatarFallback}>
                <Text style={styles.profileAvatarText}>{(user?.name || "U").slice(0, 1)}</Text>
              </View>}
            <View style={styles.profileInfo}>
              <Text style={styles.profileLabel}>{"\ub0b4 \uc815\ubcf4"}</Text>
              <Text style={styles.profileName}>{user?.name || "\uc0ac\uc6a9\uc790"}</Text>
            </View>
          </View>

          <View style={styles.profileMetaList}>
            <Text style={styles.profileMetaText}>{"\ub098\uc774 \u00b7 "}{user?.age ? `${user.age}\uc138` : "\ubbf8\uc785\ub825"}</Text>
            <Text style={styles.profileMetaText}>{"\uc131\ubcc4 \u00b7 "}{user?.gender || "\ubbf8\uc785\ub825"}</Text>
            <Text style={styles.profileMetaText}>{"\ud734\ub300\ud3f0\ubc88\ud638 \u00b7 "}{user?.phone || "\ubbf8\uc785\ub825"}</Text>
          </View>

          <Pressable style={styles.inlineProfileButton} onPress={onOpenProfileEditor}>
            <Text style={styles.inlineProfileButtonText}>{"\ub0b4 \uc815\ubcf4 \uc218\uc815"}</Text>
          </Pressable>
          {currentPersonalScope ? <Pressable accessibilityRole="button" style={[styles.familyTreeOpenButton, { minHeight: 44 }]} onPress={() => setPersonalContextScope(currentPersonalScope)}>
            <View style={{ flex: 1 }}>
              <Text style={styles.familyTreeOpenTitle}>개인 메모리·호칭</Text>
              <Text style={styles.familyTreeOpenDescription}>{activeFamily.name}에서 내가 쓰는 호칭과 대화 메모를 관리합니다.</Text>
            </View>
            <Text style={styles.familyTreeOpenArrow}>›</Text>
          </Pressable> : <Text style={styles.sectionDescription}>가족방을 선택하면 개인 메모리와 호칭을 관리할 수 있습니다.</Text>}
        </View>

        <View style={styles.familySummaryCard}>
          <Text style={styles.sectionTitle}>{"\ub0b4 \uac00\uc871"}</Text>
          {activeFamily ? <View style={styles.activeFamilyBox}>
              <Text style={styles.activeFamilyName}>{activeFamily.name}</Text>
              <Text style={styles.activeFamilyMeta}>{activeFamily.role}{" \u00b7 "}{activeFamily.members.length}{"\uba85"}</Text>
              <InviteCodeCopyButton key={activeFamily.id} code={activeFamily.code} familyName={activeFamily.name} />
              {onDeleteFamily && activeFamily.role === "\ubc29\uc7a5" ? <View style={styles.familyDangerZone}>
    <Text style={styles.familyDangerHint}>{"\ubc29\uc7a5\ub9cc \uac00\uc871\ubc29\uc744 \uc0ad\uc81c\ud560 \uc218 \uc788\uc2b5\ub2c8\ub2e4."}</Text>
    <Pressable style={styles.familyDeleteButton} onPress={() => onDeleteFamily(activeFamily)}>
      <Text style={styles.familyDeleteButtonText}>{"\uac00\uc871\ubc29 \uc0ad\uc81c"}</Text>
    </Pressable>
  </View> : null}

              <Pressable style={styles.familyTreeOpenButton} onPress={() => setFamilyTreeVisible(true)}>
                <View style={{
                flex: 1,
                minWidth: 0
              }}>
                  <Text style={styles.familyTreeOpenTitle}>가족 관계도 보기</Text>
                  <Text style={styles.familyTreeOpenDescription}>입장 시 선택한 부모/자녀 관계를 트리로 확인합니다.</Text>
                </View>
                <Text style={styles.familyTreeOpenArrow}>›</Text>
              </Pressable>

              <View style={styles.familyMemberList}>
                {activeFamily.members.map(member => {
                const memberLabel = getMemberDisplayName(member);
                const relationLabel = getFamilyRelationLabel(member, user?.id, activeFamily.members);
                return <Pressable key={member.user_id} accessibilityRole="button" accessibilityLabel={`${memberLabel} 상세보기`} style={styles.familyMemberItem} onPress={() => setSelectedMember(member)}>
                      <View style={styles.familyMemberAvatar}>
                        <Text style={styles.familyMemberAvatarText}>{memberLabel.slice(0, 1)}</Text>
                      </View>
                      <View style={styles.familyMemberInfo}>
                        <Text style={styles.familyMemberName}>{memberLabel}</Text>
                        <Text style={styles.familyMemberMeta}>
                          {getRoleLabel(member.role)}
                          {relationLabel ? ` · ${relationLabel}` : ""}
                        </Text>
                      </View>
                      <Text style={styles.familyMemberLink}>{"\uc0c1\uc138\ubcf4\uae30"}</Text>
                    </Pressable>;
              })}
              </View>
            </View> : <Text style={styles.emptyCardText}>{"\uc544\uc9c1 \uc785\uc7a5\ud55c \uac00\uc871\ubc29\uc774 \uc5c6\uc2b5\ub2c8\ub2e4."}</Text>}
        </View>
        <View style={styles.familyBuilderCard}>
          <View style={styles.familyBuilderHeader}>
            <Text style={styles.sectionTitle}>{"\uac00\uc871 \ub9cc\ub4e4\uae30"}</Text>
            <Text style={styles.sectionDescription}>{"\uc0c8 \uac00\uc871\ubc29\uc744 \uc0dd\uc131\ud558\uac70\ub098 \ucd08\ub300 \ucf54\ub4dc\ub85c \uc785\uc7a5\ud560 \uc218 \uc788\uc2b5\ub2c8\ub2e4."}</Text>
          </View>

          <View style={styles.familyModeTabs}>
            <Pressable style={[styles.familyModeTab, familyMenu === "create" && styles.familyModeTabActive]} onPress={() => setFamilyMenu("create")}>
              <Text style={[styles.familyModeText, familyMenu === "create" && styles.familyModeTextActive]}>{"\uc0dd\uc131"}</Text>
            </Pressable>
            <Pressable style={[styles.familyModeTab, familyMenu === "join" && styles.familyModeTabActive]} onPress={() => setFamilyMenu("join")}>
              <Text style={[styles.familyModeText, familyMenu === "join" && styles.familyModeTextActive]}>{"\uc785\uc7a5"}</Text>
            </Pressable>
          </View>

          {familyMenu === "create" ? <View style={styles.familyForm}>
              <Text style={styles.inputLabel}>{"\uac00\uc871\ubc29 \uc774\ub984"}</Text>
              <TextInput {...COMMON_SINGLE_LINE_INPUT_PROPS} value={familyName} onChangeText={setFamilyName} placeholder="예: 우리 가족방" placeholderTextColor={colors.placeholder} style={styles.textInput} />
              <Pressable style={styles.familyPrimaryButton} onPress={submitCreate}>
                <Text style={styles.familyPrimaryButtonText}>{"\ucf54\ub4dc \uc0dd\uc131\ud558\uae30"}</Text>
              </Pressable>
            </View> : <View style={styles.familyForm}>
              <Text style={styles.inputLabel}>{"\ucd08\ub300 \ucf54\ub4dc"}</Text>
              <TextInput {...COMMON_SINGLE_LINE_INPUT_PROPS} value={inviteCode} onChangeText={setInviteCode} placeholder="예: A1B2C3" placeholderTextColor={colors.placeholder} autoCapitalize="characters" style={styles.textInput} />
              <Pressable style={styles.familyPrimaryButton} onPress={submitJoin}>
                <Text style={styles.familyPrimaryButtonText}>{"\uac00\uc871\ubc29 \uc785\uc7a5\ud558\uae30"}</Text>
              </Pressable>
            </View>}
        </View>

        <View style={styles.storageSection}>
          <Text style={styles.sectionTitle}>{"\uac00\uc871\ubc29 \ubaa9\ub85d"}</Text>
          {familyRooms.length === 0 ? <View style={styles.emptyCard}>
              <Text style={styles.emptyCardText}>{"\uc0dd\uc131\ud558\uac70\ub098 \uc785\uc7a5\ud55c \uac00\uc871\ubc29\uc774 \uc5c6\uc2b5\ub2c8\ub2e4."}</Text>
            </View> : familyRooms.map(room => <View key={room.id} style={[styles.familyRoomCard, room.id === activeFamily?.id && styles.familyRoomCardActive]}>
                <Pressable accessibilityRole="button" accessibilityLabel={`${room.name} 가족방 선택`} accessibilityState={{ selected: room.id === activeFamily?.id }}
                  onPress={() => onSelectFamily(room.id)} style={styles.familyRoomInfoBlock}>
                  <Text style={styles.familyRoomName} numberOfLines={2}>{room.name}</Text>
                  <Text style={styles.familyRoomMeta}>{room.role}{" \u00b7 "}{room.createdAt}</Text>
                </Pressable>
                <View style={styles.familyRoomActions}>
                  <InviteCodeCopyButton code={room.code} familyName={room.name} />
                  {onDeleteFamily && room.role === "\ubc29\uc7a5" ? <Pressable accessibilityRole="button" accessibilityLabel={`${room.name} 가족방 삭제`} style={styles.familyDeleteChip} onPress={() => onDeleteFamily(room)}>
                      <Text style={styles.familyDeleteChipText}>삭제</Text>
                    </Pressable> : null}
                </View>
              </View>)}
        </View>

        <View style={styles.logoutSection}>
          <Pressable style={styles.myPageLogoutButton} onPress={onLogout}>
            <Text style={styles.myPageLogoutText}>{"\ub85c\uadf8\uc544\uc6c3"}</Text>
          </Pressable>
          {onDeleteAccount ? <Pressable style={styles.withdrawButton} onPress={onDeleteAccount}>
            <Text style={styles.withdrawButtonText}>회원탈퇴</Text>
          </Pressable> : null}
          <Text style={styles.logoutHint}>{"\ub85c\uadf8\uc544\uc6c3\ud558\uba74 \ucc98\uc74c \ub85c\uadf8\uc778 \ud654\uba74\uc73c\ub85c \ub3cc\uc544\uac11\ub2c8\ub2e4."}</Text>
        </View>
      </ScrollView>
      </KeyboardAvoidingView>

      <Modal transparent visible={Boolean(selectedMember)} onRequestClose={() => setSelectedMember(null)}>
        <View style={styles.centerModalBackdrop}>
          <View style={styles.memberDetailSheet}>
            <Text style={styles.modalTitle}>{"\uac00\uc871 \uba64\ubc84 \uc0c1\uc138\uc815\ubcf4"}</Text>
            {selectedMember ? <>
                <View style={styles.memberDetailHeader}>
                  <View style={styles.memberDetailAvatar}>
                    <Text style={styles.memberDetailAvatarText}>{getMemberDisplayName(selectedMember).slice(0, 1)}</Text>
                  </View>
                  <View style={styles.memberDetailHeaderInfo}>
                    <Text style={styles.memberDetailName}>{selectedMember.name || "\uc774\ub984 \ubbf8\uc785\ub825"}</Text>
                    <Text style={styles.memberDetailRole}>{getRoleLabel(selectedMember.role)}</Text>
                  </View>
                </View>

                <View style={styles.memberDetailList}>
                  <View style={styles.memberDetailRow}><Text style={styles.memberDetailLabel}>{"\ub098\uc774"}</Text><Text style={styles.memberDetailValue}>{selectedMember.age ? `${selectedMember.age}\uc138` : "\ubbf8\uc785\ub825"}</Text></View>
                  <View style={styles.memberDetailRow}><Text style={styles.memberDetailLabel}>{"\uc131\ubcc4"}</Text><Text style={styles.memberDetailValue}>{selectedMember.gender || "\ubbf8\uc785\ub825"}</Text></View>
                  <View style={styles.memberDetailRow}><Text style={styles.memberDetailLabel}>{"\ud734\ub300\ud3f0\ubc88\ud638"}</Text><Text style={styles.memberDetailValue}>{selectedMember.phone || "\ubbf8\uc785\ub825"}</Text></View>
                </View>

              </> : null}

            <Pressable style={[styles.modalButton, styles.submitButton]} onPress={() => setSelectedMember(null)}>
              <Text style={styles.submitButtonText}>{"\ub4a4\ub85c\uac00\uae30"}</Text>
            </Pressable>
          </View>
        </View>
      </Modal>
      <Modal transparent visible={familyTreeVisible} onRequestClose={() => setFamilyTreeVisible(false)}>
        <GestureHandlerRootView style={styles.gestureModalRoot}>
        <View style={styles.centerModalBackdrop}>
          <View style={styles.familyTreeSheet}>
            <View style={styles.familyTreeHeader}>
              <View style={styles.familyTreeHeading}>
                <Text style={styles.familyTreeEyebrow}>Family Tree</Text>
                <Text style={styles.familyTreeTitle}>{activeFamily?.name || "가족방"} 관계도</Text>
              </View>
              <Pressable accessibilityRole="button" accessibilityLabel="가족 관계도 닫기" style={styles.familyTreeCloseButton} onPress={() => setFamilyTreeVisible(false)}>
                <Text style={styles.familyTreeCloseText}>닫기</Text>
              </Pressable>
            </View>
            <Text style={styles.familyTreeDescription}>
              가족방 입장 시 선택한 부모/자녀 태그를 기준으로 구성원을 세대별로 배치했습니다.
            </Text>

            <ScrollView style={styles.familyTreeVerticalScroll} contentContainerStyle={styles.familyTreeVerticalContent} showsVerticalScrollIndicator nestedScrollEnabled>
              <ScrollView horizontal showsHorizontalScrollIndicator nestedScrollEnabled contentContainerStyle={styles.familyTreeHorizontalContent}>
                <PinchGestureHandler onGestureEvent={handleFamilyTreePinchGesture} onHandlerStateChange={handleFamilyTreePinchStateChange}>
                  <Animated.View style={[styles.familyTreeZoomLayer, {
                    transform: [{
                      scale: familyTreeCombinedScale
                    }]
                  }]}>
                    <View style={styles.familyTreeCanvas}>
                      {buildFamilyTreeLevels(activeFamily?.members || []).map((level, levelIndex, levels) => <View key={`level-${levelIndex}`} style={styles.familyTreeLevelBlock}>
                          <View style={styles.familyTreeLevelBadge}>
                            <Text style={styles.familyTreeLevelBadgeText}>{levelIndex + 1}세대</Text>
                          </View>
                          <View style={styles.familyTreeLevelRow}>
                            {level.map(member => {
                            const isCurrentUser = member.user_id === user?.id;
                            return <View key={member.user_id} style={[styles.familyTreeMemberCard, isCurrentUser && styles.familyTreeMemberCardActive]}>
                                  <View style={styles.familyTreeMemberTop}>
                                    <View style={[styles.familyTreeAvatar, member.gender === "여성" && styles.familyTreeAvatarFemale]}>
                                      <Text style={styles.familyTreeAvatarText}>
                                        {getMemberDisplayName(member).slice(0, 1)}
                                      </Text>
                                    </View>
                                    <View style={styles.familyTreeMemberInfo}>
                                      <Text style={[styles.familyTreeMemberName, isCurrentUser && styles.familyTreeMemberNameActive]} numberOfLines={1}>
                                        {getMemberDisplayName(member)}
                                      </Text>
                                      <Text style={[styles.familyTreeMemberRole, isCurrentUser && styles.familyTreeMemberRoleActive]}>
                                        {isCurrentUser ? "나 · " : ""}
                                        {getRoleLabel(member.role)}
                                      </Text>
                                    </View>
                                  </View>
                                  <View style={styles.familyTreeMetaList}>
                                    <Text style={styles.familyTreeMetaText}>나이: {member.age ? `${member.age}세` : "미입력"}</Text>
                                    <Text style={styles.familyTreeMetaText}>성별: {member.gender || "미입력"}</Text>
                                    <Text style={styles.familyTreeMetaText} numberOfLines={1}>연락처: {member.phone || "미입력"}</Text>
                                <Text style={styles.familyTreeRelationText}>
                                  {getFamilyRelationLabel(member, user?.id, activeFamily?.members || []) || "기준 구성원"}
                                </Text>
                                  </View>
                                </View>;
                          })}
                          </View>
                          {levelIndex < levels.length - 1 ? <View style={styles.familyTreeConnector} /> : null}
                        </View>)}
                    </View>
                  </Animated.View>
                </PinchGestureHandler>
              </ScrollView>
            </ScrollView>

            <Text style={styles.familyTreeHint}>
              두 손가락으로 확대/축소하고, 위아래와 좌우로 밀어 전체 관계도를 확인할 수 있습니다.
            </Text>
          </View>
        </View>
        </GestureHandlerRootView>
      </Modal>
      <Modal transparent visible={Boolean(joinPreview)} onRequestClose={closeJoinPreview}>
        <View style={styles.centerModalBackdrop}>
          <View style={styles.joinFamilyRelationSheet}>
            <View style={styles.uploadResultIcon}>
              <Text style={styles.uploadResultIconText}>◎</Text>
            </View>
            <Text style={styles.uploadLoadingTitle}>가족 관계 선택</Text>
            <Text style={styles.uploadLoadingDescription}>
              {joinPreview ? `${joinPreview.name}에 입장하기 전에 이미 들어와 있는 가족을 기준으로 관계를 선택해주세요.` : ""}
            </Text>
            <Text style={styles.uploadLoadingHint}>이 정보는 이후 가족 트리와 호칭 구조를 만드는 기준으로 사용됩니다.</Text>

            <View style={styles.joinRelationSection}>
              <Text style={styles.inputLabel}>기준 가족 구성원</Text>
              <View style={styles.joinRelationOptionList}>
                {joinPreview?.members?.map(member => {
                const isActive = joinRelationTargetUserId === member.user_id;
                return <Pressable key={member.user_id} style={[styles.joinRelationMemberOption, isActive && styles.joinRelationMemberOptionActive]} onPress={() => setJoinRelationTargetUserId(member.user_id)}>
                      <Text style={[styles.joinRelationMemberOptionText, isActive && styles.joinRelationMemberOptionTextActive]}>
                        {getMemberDisplayName(member)}
                      </Text>
                      <Text style={[styles.joinRelationMemberOptionMeta, isActive && styles.joinRelationMemberOptionTextActive]}>
                        {getRoleLabel(member.role)}
                      </Text>
                    </Pressable>;
              })}
              </View>
            </View>

            <View style={styles.joinRelationSection}>
              <Text style={styles.inputLabel}>관계 선택</Text>
              <View style={styles.joinRelationToggleRow}>
                <Pressable style={[styles.joinRelationToggle, joinRelationType === "child" && styles.joinRelationToggleActive]} onPress={() => setJoinRelationType("child")}>
                  <Text style={[styles.joinRelationToggleText, joinRelationType === "child" && styles.joinRelationToggleTextActive]}>
                    이 사람의 자녀
                  </Text>
                </Pressable>
                <Pressable style={[styles.joinRelationToggle, joinRelationType === "parent" && styles.joinRelationToggleActive]} onPress={() => setJoinRelationType("parent")}>
                  <Text style={[styles.joinRelationToggleText, joinRelationType === "parent" && styles.joinRelationToggleTextActive]}>
                    이 사람의 부모
                  </Text>
                </Pressable>
                <Pressable style={[styles.joinRelationToggle, joinRelationType === "spouse" && styles.joinRelationToggleActive]} onPress={() => setJoinRelationType("spouse")}>
                  <Text style={[styles.joinRelationToggleText, joinRelationType === "spouse" && styles.joinRelationToggleTextActive]}>
                    이 사람의 배우자
                  </Text>
                </Pressable>
              </View>
            </View>

            <View style={styles.resultButtonRow}>
              <Pressable style={styles.resultSecondaryButton} onPress={closeJoinPreview}>
                <Text style={styles.resultSecondaryButtonText}>취소</Text>
              </Pressable>
              <Pressable style={styles.uploadResultButton} onPress={submitJoinWithRelation} disabled={!joinRelationTargetUserId}>
                <Text style={styles.uploadResultButtonText}>입장하기</Text>
              </Pressable>
            </View>
          </View>
        </View>
      </Modal>
      {personalContextScope && personalContextScope === currentPersonalScope ? <PersonalContextEditor key={personalContextScope} userId={user.id} roomId={activeFamily.id} familyName={activeFamily.name} onClose={() => setPersonalContextScope(null)} /> : null}
    </>;
}
