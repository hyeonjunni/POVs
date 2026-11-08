export function mapFamilyRoom(room, members, currentUserId) {
  return {
    id: room.room_id,
    name: room.name,
    code: room.invite_code,
    role: room.owner_user_id === currentUserId ? "\ubc29\uc7a5" : "\uac00\uc871 \uad6c\uc131\uc6d0",
    members: Array.isArray(members) ? members : [],
    createdAt: new Date().toLocaleDateString("ko-KR")
  };
}
export function getGenderedRelationName(member, relationToCurrentUser) {
  const gender = member?.gender || "";
  if (relationToCurrentUser === "spouse") {
    if (gender === "여성") {
      return "아내";
    }
    if (gender === "남성") {
      return "남편";
    }
    return "배우자";
  }
  if (relationToCurrentUser === "grandparent") {
    if (gender === "여성") {
      return "할머니";
    }
    if (gender === "남성") {
      return "할아버지";
    }
    return "조부모";
  }
  if (relationToCurrentUser === "parent") {
    if (gender === "여성") {
      return "엄마";
    }
    if (gender === "남성") {
      return "아빠";
    }
    return "부모";
  }
  if (relationToCurrentUser === "child") {
    if (gender === "여성") {
      return "딸";
    }
    if (gender === "남성") {
      return "아들";
    }
    return "자녀";
  }
  return null;
}
export function getParentIdFromMemberRelation(member) {
  if (!member?.user_id || !member?.related_to_user_id || !member?.relation_to_related_user) {
    return null;
  }
  if (!["parent", "child"].includes(member.relation_to_related_user)) {
    return null;
  }
  return member.relation_to_related_user === "parent" ? member.user_id : member.related_to_user_id;
}
export function getChildIdFromMemberRelation(member) {
  if (!member?.user_id || !member?.related_to_user_id || !member?.relation_to_related_user) {
    return null;
  }
  if (!["parent", "child"].includes(member.relation_to_related_user)) {
    return null;
  }
  return member.relation_to_related_user === "parent" ? member.related_to_user_id : member.user_id;
}
export function getParentIdsForUser(userId, members = []) {
  return members.filter(member => getChildIdFromMemberRelation(member) === userId).map(member => getParentIdFromMemberRelation(member)).filter(Boolean);
}
export function getSharedParentIds(firstUserId, secondUserId, members = []) {
  const firstParentIds = getParentIdsForUser(firstUserId, members);
  const secondParentIds = getParentIdsForUser(secondUserId, members);
  return firstParentIds.filter(parentId => secondParentIds.includes(parentId));
}
export function getSiblingRelationName(member, currentUser) {
  const memberAge = Number.parseInt(String(member?.age || ""), 10);
  const currentUserAge = Number.parseInt(String(currentUser?.age || ""), 10);
  const memberGender = member?.gender || "";
  if (!Number.isFinite(memberAge) || !Number.isFinite(currentUserAge)) {
    if (memberGender === "여성") {
      return "자매";
    }
    if (memberGender === "남성") {
      return "형제";
    }
    return "형제자매";
  }
  if (memberAge < currentUserAge) {
    if (memberGender === "여성") {
      return "여동생";
    }
    if (memberGender === "남성") {
      return "남동생";
    }
    return "동생";
  }
  if (memberAge === currentUserAge) {
    if (memberGender === "여성") {
      return "자매";
    }
    if (memberGender === "남성") {
      return "형제";
    }
    return "동갑 형제자매";
  }
  const currentUserGender = currentUser?.gender || "";
  if (memberGender === "여성") {
    return currentUserGender === "여성" ? "언니" : "누나";
  }
  if (memberGender === "남성") {
    return currentUserGender === "여성" ? "오빠" : "형";
  }
  return "손위 형제";
}
export function getFamilyRelationLabel(member, currentUserId = null, members = []) {
  if (!member?.related_to_user_name || !member?.relation_to_related_user) {
    return null;
  }
  if (currentUserId && member.user_id === currentUserId) {
    return "나";
  }
  if (currentUserId && Array.isArray(members)) {
    const currentUser = members.find(item => item?.user_id === currentUserId);
    if (member.related_to_user_id === currentUserId) {
      const relationName = getGenderedRelationName(member, member.relation_to_related_user);
      if (relationName) {
        return relationName;
      }
    }
    const currentUserLinkToMember = members.find(item => item?.user_id === currentUserId && item.related_to_user_id === member.user_id);
    if (currentUserLinkToMember) {
      const reverseRelation = currentUserLinkToMember.relation_to_related_user === "parent" ? "child" : currentUserLinkToMember.relation_to_related_user === "child" ? "parent" : currentUserLinkToMember.relation_to_related_user;
      const relationName = getGenderedRelationName(member, reverseRelation);
      if (relationName) {
        return relationName;
      }
    }
    const parentIds = getParentIdsForUser(currentUserId, members);
    for (const parentId of parentIds) {
      const parent = members.find(item => item?.user_id === parentId);
      const grandParentIds = getParentIdsForUser(parentId, members);
      if (grandParentIds.includes(member.user_id)) {
        const grandParentName = getGenderedRelationName(member, "grandparent");
        if (parent?.gender === "남성") {
          return `친${grandParentName}`;
        }
        if (parent?.gender === "여성") {
          return `외${grandParentName}`;
        }
        return grandParentName;
      }
    }
    if (currentUser && member.user_id !== currentUserId && getSharedParentIds(member.user_id, currentUserId, members).length > 0) {
      return getSiblingRelationName(member, currentUser);
    }
  }
  const relationLabel = member.relation_to_related_user === "parent" ? `${member.related_to_user_name}의 부모` : member.relation_to_related_user === "child" ? `${member.related_to_user_name}의 자녀` : `${member.related_to_user_name}의 배우자`;
  return relationLabel;
}
export function getRoleLabel(role) {
  return role === "owner" ? "\ubc29\uc7a5" : "\uad6c\uc131\uc6d0";
}
export function getMemberDisplayName(member) {
  return member?.name?.trim() || "이름 미입력";
}
export function buildFamilyTreeLevels(members = []) {
  const memberMap = new Map();
  const childrenByParent = new Map();
  const childIds = new Set();
  members.forEach(member => {
    if (member?.user_id) {
      memberMap.set(member.user_id, member);
      childrenByParent.set(member.user_id, []);
    }
  });
  members.forEach(member => {
    if (!member?.user_id || !member?.related_to_user_id || !memberMap.has(member.related_to_user_id) || !["parent", "child"].includes(member.relation_to_related_user)) {
      return;
    }
    const parentId = member.relation_to_related_user === "parent" ? member.user_id : member.related_to_user_id;
    const childId = member.relation_to_related_user === "parent" ? member.related_to_user_id : member.user_id;
    if (memberMap.has(parentId) && memberMap.has(childId) && parentId !== childId) {
      childrenByParent.get(parentId)?.push(memberMap.get(childId));
      childIds.add(childId);
    }
  });
  const roots = members.filter(member => member?.user_id && !childIds.has(member.user_id));
  const firstLevel = roots.length > 0 ? roots : members.filter(member => member?.user_id);
  const levels = [];
  const visited = new Set();
  let currentLevel = firstLevel;
  while (currentLevel.length > 0) {
    const uniqueLevel = currentLevel.filter(member => {
      if (!member?.user_id || visited.has(member.user_id)) {
        return false;
      }
      visited.add(member.user_id);
      return true;
    });
    if (uniqueLevel.length > 0) {
      levels.push(uniqueLevel);
    }
    currentLevel = uniqueLevel.flatMap(member => childrenByParent.get(member.user_id) || []);
  }
  const unplacedMembers = members.filter(member => member?.user_id && !visited.has(member.user_id));
  if (unplacedMembers.length > 0) {
    levels.push(unplacedMembers);
  }
  return levels;
}
