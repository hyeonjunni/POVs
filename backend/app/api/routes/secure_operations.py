"""Secure B3 HTTP paths. Never imports legacy routes, cleanup or plaintext storage.

Async handlers retain the boundary's owner task and guard. Every SQL context ends
before hashing, key access, crypto, storage and response send.
"""
import base64
import hashlib
import hmac
import json
import secrets
import time
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.deps import _decode_subject
from app.core import database
from app.core.security import create_access_token, hash_password, verify_password
from app.models.auth_identity import authenticated_identity_matches
from app.schemas.auth import ActivationStatus, AuthSessionResponse, SignUpRequest, LoginRequest, UserResponse, UserProfileUpdateRequest
from app.schemas.family import FamilyCreateRequest, FamilyResponse, FamilyJoinRequest, FamilyMemberResponse, FamilyDetailResponse, FamilyJoinPreviewResponse
from app.services import secure_catalogue as cat
from app.storage.contracts import KeyRef, PayloadIdentity, SealedPayload
from app.storage.crypto import seal_payload, open_payload
from app.storage.profile import normalize_lookup, blind_index, seal_profile, open_profile

router = APIRouter()


def tx():
    return database.engine.begin()


def error(status, detail, code=None):
    return JSONResponse({"detail": detail, **({"code": code} if code else {})}, status_code=status)


def principal(request):
    value = request.headers.get("authorization", "")
    if not value.startswith("Bearer "):
        raise HTTPException(401, "Authentication credentials were not provided")
    uid = _decode_subject(value[7:])
    try:
        return str(cat._uuid(uid))
    except cat.SecureStateChanged:
        raise HTTPException(401, "Invalid access token payload") from None


def capability(request):
    value = request.headers.get("idempotency-key", "")
    try:
        raw = base64.b64decode(value + "=", altchars=b"-_", validate=True)
        if len(value) != 43 or len(raw) != 32 or base64.urlsafe_b64encode(raw).decode().rstrip("=") != value:
            raise ValueError()
        return hashlib.sha256(raw).digest()
    except (ValueError, UnicodeError):
        raise HTTPException(400, "Invalid Idempotency-Key") from None


def raw_json(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def profile_chunk(value):
    return " | ".join(f"{k}: {value[k]}" for k in ("name", "age", "gender", "phone", "email")
                      if value.get(k) is not None and value[k] != "")


def lookup_keys(db):
    c = cat.control(db)
    rows = tuple(tuple(r) for r in db.execute(text("SELECT k.domain,k.owner_id,k.key_id,k.epoch,k.state,k.revision,s.revision FROM key_epochs k JOIN security_scopes s ON s.domain=k.domain AND s.owner_id=k.owner_id JOIN credential_generation_keys m ON m.key_id=k.key_id AND m.key_epoch=k.epoch AND m.domain=k.domain AND m.owner_id=k.owner_id WHERE k.domain='lookup' AND k.owner_id=:c AND k.state IN ('ACTIVE','DECRYPT_ONLY') AND k.provisioning_state='PERSISTED' AND s.state='ACTIVE' AND m.generation_id=:g ORDER BY k.key_id,k.epoch"), dict(c=c["catalogue_id"], g=c["runtime_generation"])))
    accepted = db.scalar(text("SELECT count(*) FROM key_epochs WHERE domain='lookup' AND owner_id=:c AND state IN ('ACTIVE','DECRYPT_ONLY')"), dict(c=c["catalogue_id"]))
    if not rows or len(rows) != accepted or sum(r[4] == "ACTIVE" for r in rows) != 1:
        raise cat.SecureStateChanged()
    return rows


def tokens(runtime, keys, kind, identifier):
    normalized = normalize_lookup(kind, identifier)
    return tuple((r, blind_index(kind, normalized, runtime.vault.load(KeyRef(r[0], UUID(r[1]), UUID(r[2]), r[3])), r[3])) for r in keys)


def candidates(db, kind, values):
    found = set()
    for k, token in values:
        found.update(tuple(r) for r in db.execute(text("SELECT user_id,identity_id FROM blind_indexes WHERE kind=:kind AND provider=:provider AND normalization_version=1 AND key_id=:key AND key_epoch=:epoch AND key_domain='lookup' AND key_owner_id=:owner AND token=:token"),
                dict(kind=kind, provider={"local_username": "local", "email": "email", "google_subject": "google"}[kind], key=k[2], epoch=k[3], owner=k[1], token=token)))
    return found


def unique(db, keys, values, owner=None):
    if lookup_keys(db) != keys:
        raise cat.SecureStateChanged()
    for kind, entries in values.items():
        if any(uid != owner for uid, _ in candidates(db, kind, entries)):
            raise HTTPException(409, "identity_conflict")


def insert_tokens(db, kind, values, user_id, identity_id=None):
    for k, token in values:
        if k[4] != "ACTIVE":
            continue
        cat._cas(db, "INSERT INTO blind_indexes (kind,provider,normalization_version,key_id,key_epoch,key_domain,key_owner_id,token,user_id,identity_id) VALUES (:kind,:provider,1,:key,:epoch,'lookup',:owner,:token,:uid,:aid)",
                 kind=kind, provider={"local_username": "local", "email": "email"}[kind], key=k[2], epoch=k[3], owner=k[1], token=token, uid=user_id, aid=identity_id)


def detached(db, saved):
    return tuple(cat.envelope_values(cat._row(db, "SELECT * FROM encrypted_objects WHERE object_id=:o AND object_version=:v", o=str(p[4]), v=p[5])) for p in saved.pointers)


def decrypt(runtime, values):
    result = []
    for identity, ref, wrapped, receipt in values:
        blob = runtime.store.read(receipt)
        key = runtime.vault.load(ref)
        if identity.domain == "user":
            payload = open_profile(identity, SealedPayload(blob, wrapped), key)
            if payload.get("user_id") != str(identity.owner_id):
                raise cat.SecureStateChanged()
        else:
            payload = json.loads(open_payload(identity, blob, wrapped, key))
            if payload.get("room_id") != str(identity.owner_id):
                raise cat.SecureStateChanged()
        result.append(payload)
    return result


def read_user(runtime, uid):
    with tx() as db:
        saved = cat.snapshot(db, principal_id=UUID(uid), targets=(("user", UUID(uid)),))
        values = detached(db, saved)
    payload = decrypt(runtime, values)[0]
    dto = UserResponse.model_validate(payload)
    with tx() as db:
        cat.revalidate(db, saved)
    return dto


def status_response(request, op, state, pending=False):
    status = ActivationStatus(operation_id=op["operation_id"], kind=op["kind"], state=state,
                              expires_at=op["expires_at"], retry_after_seconds=2 if state in ("WAITING_CREDENTIAL", "READY") else None)
    code = 410 if state == "EXPIRED" else 409 if state == "FAILED" else 202 if pending else 200
    if code == 202:
        request.scope["secure_activation_location"] = UUID(op["operation_id"])
    return JSONResponse(status.model_dump(), status_code=code)


def activation_request(request, kind, uid=None):
    if request.headers.get("prefer", "").strip().lower() != "respond-async":
        return None, None, error(503, "Secure activation requires a compatible client", "activation_client_required")
    digest = capability(request)
    explicit = request.headers.get("x-activation-operation")
    with tx() as db:
        c = cat.control(db)
        if uid:
            cat.snapshot(db, principal_id=UUID(uid), targets=(("user", UUID(uid)),))
        found = db.execute(text("SELECT operation_id FROM secure_operations WHERE continuation_digest=:d"), dict(d=digest)).scalar_one_or_none()
        if explicit or found:
            try:
                op = cat.activation_parent(db, operation_id=explicit or found, digest=digest, kind=kind, principal_id=uid)
            except cat.SecureStateChanged:
                raise HTTPException(404, "Operation not found") from None
            state = cat.activation_effective(db, op, int(time.time()))
            if state != op["state"] and op["state"] in ("WAITING_CREDENTIAL", "READY"):
                cat._cas(db, "UPDATE secure_operations SET state=:s,runtime_generation=:g,failure_code=:f,revision=revision+1 WHERE operation_id=:o AND state=:old AND revision=:r",
                         s=state, g=None if state == "WAITING_CREDENTIAL" else op["runtime_generation"], f="activation_expired" if state == "EXPIRED" else None, o=op["operation_id"], old=op["state"], r=op["revision"])
                op = cat.activation_parent(db, operation_id=op["operation_id"], digest=digest, kind=kind, principal_id=uid)
            if state == "COMMITTED":
                return op, digest, error(409, "Signup completed; sign in", "signup_completed") if kind == "SIGNUP" else None
            if state in ("EXPIRED", "FAILED"):
                return op, digest, status_response(request, op, state)
            if state == "WAITING_CREDENTIAL":
                delivered = db.scalar(text("SELECT count(*) FROM key_epochs k JOIN credential_generation_keys m ON m.key_id=k.key_id AND m.key_epoch=k.epoch AND m.domain=k.domain AND m.owner_id=k.owner_id WHERE k.key_id=:k AND k.epoch=:e AND k.state='PREPARED' AND k.provisioning_state='PERSISTED' AND m.generation_id=:g"), dict(k=op["activation_key_id"], e=op["activation_key_epoch"], g=c["runtime_generation"]))
                if delivered:
                    cat._cas(db, "UPDATE secure_operations SET state='READY',runtime_generation=:g,revision=revision+1 WHERE operation_id=:o AND state='WAITING_CREDENTIAL' AND revision=:r", g=c["runtime_generation"], o=op["operation_id"], r=op["revision"])
                    op = cat.activation_parent(db, operation_id=op["operation_id"], digest=digest, kind=kind, principal_id=uid)
            if op["state"] != "READY" or not explicit:
                return op, digest, status_response(request, op, op["state"], pending=True)
            return op, digest, None
        owner, kid, oid = (str(uuid4()) for _ in range(3))
        domain = "user" if kind == "SIGNUP" else "room"
        cat._cas(db, "INSERT INTO security_scopes VALUES (:d,:o,'ACTIVE',0,0,1)", d=domain, o=owner)
        cat._cas(db, "INSERT INTO key_epochs (key_id,epoch,domain,owner_id,state,provisioning_state,revision,recovery_inventory_state) VALUES (:k,1,:d,:o,'PREPARED','REQUESTED',1,'UNKNOWN')", k=kid, d=domain, o=owner)
        now = int(time.time())
        cat._cas(db, "INSERT INTO secure_operations (operation_id,kind,principal_id,owner_type,owner_id,domain,scope_id,control_revision,pointer_revision,source_revision,deletion_epoch,authorization_epoch,activation_key_id,activation_key_epoch,continuation_digest,created_at,expires_at,revision,state) VALUES (:id,:kind,:uid,:domain,:owner,:domain,:owner,:cr,1,1,0,0,:key,1,:digest,:now,:expires,1,'WAITING_CREDENTIAL')", id=oid, kind=kind, uid=uid, domain=domain, owner=owner, cr=c["revision"], key=kid, digest=digest, now=now, expires=now+86400)
        op = cat.activation_parent(db, operation_id=oid, digest=digest, kind=kind, principal_id=uid)
    return op, digest, status_response(request, op, "WAITING_CREDENTIAL", pending=True)


@router.get("/secure-operations/{operation_id}", response_model=ActivationStatus)
async def poll(operation_id: str, request: Request):
    digest = capability(request)
    uid = principal(request) if request.headers.get("authorization") else None
    with tx() as db:
        try:
            op = cat.activation_parent(db, operation_id=operation_id, digest=digest, principal_id=uid)
            if op["kind"] == "ROOM_CREATE":
                cat.snapshot(db, principal_id=UUID(uid), targets=(("user", UUID(uid)),))
        except cat.SecureStateChanged:
            raise HTTPException(404, "Operation not found") from None
        state = cat.activation_effective(db, op, int(time.time()))
    return status_response(request, op, state)


def create_ciphertext(runtime, saved, identity, sealed, *, activation=None):
    """Authorize and reserve under SQL before entering the ciphertext store."""
    from app.storage.contracts import StorageCreateUnconfirmed
    creating = authorized = False
    try:
        with tx() as db:
            if activation is None:
                cat._operation(db, saved, saved.operation_id, "CLAIMED")
            else:
                op, digest, uid = activation
                cat.activation_child(db, op, saved, digest=digest, principal_id=uid, state="CLAIMED")
            authorized = True
            cat.reserve_storage(db, operation_id=saved.operation_id, identity=identity,
                                ciphertext_size=len(sealed.blob))
            cat.bind_create(db, operation_id=saved.operation_id, identity=identity,
                            sealed=sealed, store=runtime.store)
        creating = True
        return runtime.store.create(identity.object_id, sealed.blob)
    except BaseException as error:
        with tx() as db:
            operation = cat._row(db, "SELECT * FROM secure_operations WHERE operation_id=:id",
                                 id=str(saved.operation_id))
            if (authorized and operation["state"] == "CLAIMED"
                    and (creating or operation.get("reserved_ciphertext_bytes") is None)):
                cat.abandon(db, operation_id=saved.operation_id, expected_revision=operation["revision"],
                            receipt_unknown=creating,
                            known_receipt=(error.receipt if isinstance(error, StorageCreateUnconfirmed)
                                           and identity.domain == "room" else None))
        if isinstance(error, cat.RoomStorageQuotaExceeded):
            raise HTTPException(413, "Room storage quota exceeded") from None
        raise


def stage_initial(runtime, op, digest, uid, payloads):
    with tx() as db:
        children = db.execute(text("SELECT * FROM secure_operations WHERE parent_operation_id=:p"), dict(p=op["operation_id"])).mappings().all()
        if any(c["state"] in ("CLAIMED", "ORPHAN_UNRESOLVED") for c in children):
            raise cat.SecureStateChanged()
        for c in children:
            if c["state"] in ("RECEIPTED", "VERIFIED"):
                cat.abandon(db, operation_id=UUID(c["operation_id"]), expected_revision=c["revision"])
    staged = []
    for target, payload in payloads:
        child, obj = uuid4(), uuid4()
        with tx() as db:
            saved = cat.claim_activation_child(db, op, digest=digest, principal_id=uid, target=target, child_id=child, candidate_id=obj)
        raw = raw_json(payload)
        identity = PayloadIdentity(op["domain"], UUID(op["scope_id"]), None, obj, 1, cat.KINDS[target[0]], None, None, len(raw))
        ref = saved.key_refs[0]
        key = runtime.vault.load(ref)
        sealed = seal_profile(identity, payload, key) if identity.domain == "user" else seal_payload(identity, raw, key)
        receipt = create_ciphertext(runtime, saved, identity, sealed, activation=(op, digest, uid))
        with tx() as db:
            cat.receipt_activation_child(db, op, saved, digest=digest, principal_id=uid, identity=identity, sealed=sealed, receipt=receipt)
        proof = cat.authenticate_readback(identity=identity, sealed=sealed, receipt=receipt, key_ref=ref, vault=runtime.vault, store=runtime.store)
        staged.append((saved, proof))
    return staged


def publish_initial(db, op, digest, uid, staged, create_owner):
    children = [cat.verify_activation_child(db, op, saved, digest=digest, principal_id=uid, proof=proof) for saved, proof in staged]
    create_owner(db)
    cat._cas(db, "UPDATE key_epochs SET state='ACTIVE',revision=revision+1 WHERE key_id=:k AND epoch=:e AND state='PREPARED' AND provisioning_state='PERSISTED'", k=op["activation_key_id"], e=op["activation_key_epoch"])
    for child in children:
        cat._cas(db, "UPDATE encrypted_objects SET state='ACTIVE',envelope_revision=envelope_revision+1 WHERE object_id=:o AND object_version=1 AND state='STAGED' AND envelope_revision=1", o=child["candidate_object_id"])
        cat._cas(db, "INSERT INTO protected_pointers (owner_type,owner_id,domain,scope_id,object_id,object_version,state,source_revision,deletion_epoch,revision,authorization_epoch,operation_id) VALUES (:t,:o,:d,:s,:obj,1,'ENCRYPTED_ACTIVE',1,:de,2,:ae,:op)", t=child["owner_type"], o=child["owner_id"], d=child["domain"], s=child["scope_id"], obj=child["candidate_object_id"], de=child["deletion_epoch"], ae=child["authorization_epoch"], op=child["operation_id"])
        cat._cas(db, "UPDATE secure_operations SET state='COMMITTED',revision=revision+1 WHERE operation_id=:o AND state='VERIFIED' AND revision=:r", o=child["operation_id"], r=child["revision"]+1)
    cat._cas(db, "UPDATE security_scopes SET revision=revision+1 WHERE domain=:d AND owner_id=:o", d=op["domain"], o=op["scope_id"])
    cat._cas(db, "UPDATE secure_operations SET state='COMMITTED',revision=revision+1 WHERE operation_id=:o AND state='READY' AND revision=:r", o=op["operation_id"], r=op["revision"])


@router.post("/auth/signup", response_model=AuthSessionResponse)
async def signup(payload: SignUpRequest, request: Request):
    try:
        username = normalize_lookup("local_username", payload.username).decode()
    except ValueError:
        raise HTTPException(400, "Invalid username") from None
    op, digest, response = activation_request(request, "SIGNUP")
    if response is not None:
        return response
    runtime = request.app.state.secure_runtime
    with tx() as db:
        keys = lookup_keys(db)
    email = normalize_lookup("email", str(payload.email)).decode()
    values = {"local_username": tokens(runtime, keys, "local_username", username), "email": tokens(runtime, keys, "email", email)}
    uid, aid = op["owner_id"], str(uuid4())
    user = UserResponse(user_id=uid, username=username, email=email, name=payload.name.strip()).model_dump()
    user["profile_chunk"] = profile_chunk(user)
    identity = dict(user_id=uid, identity_id=aid, provider="local", normalization_version=1,
                    normalized_identifier=username, credential_hash=hash_password(payload.password))
    try:
        with tx() as db:
            unique(db, keys, values)
        staged = stage_initial(runtime, op, digest, None, ((('user', UUID(uid)), user), (('auth_identity', UUID(aid)), identity)))
        def owner(db):
            unique(db, keys, values)
            cat._cas(db, "INSERT INTO users (id) VALUES (:u)", u=uid)
            cat._cas(db, "INSERT INTO auth_identities (id,user_id,provider,payload_state) VALUES (:a,:u,'local','ENCRYPTED')", a=aid, u=uid)
            insert_tokens(db, "local_username", values["local_username"], uid, aid)
            insert_tokens(db, "email", values["email"], uid)
        with tx() as db:
            publish_initial(db, op, digest, None, staged, owner)
    except HTTPException as exc:
        if exc.status_code != 409:
            raise
        with tx() as db:
            cat._cas(db, "UPDATE secure_operations SET state='FAILED',failure_code='identity_conflict',revision=revision+1 WHERE operation_id=:o AND state='READY' AND revision=:r", o=op["operation_id"], r=op["revision"])
        return error(409, "Identity already exists", "identity_conflict")
    token = create_access_token(uid)
    dto = read_user(runtime, uid)
    return AuthSessionResponse(access_token=token, user=dto)


@router.post("/auth/login", response_model=AuthSessionResponse)
async def login(payload: LoginRequest, request: Request):
    runtime = request.app.state.secure_runtime
    try:
        with tx() as db:
            keys = lookup_keys(db)
        normalized = normalize_lookup("local_username", payload.username).decode()
        values = tokens(runtime, keys, "local_username", normalized)
        with tx() as db:
            if lookup_keys(db) != keys:
                raise cat.SecureStateChanged()
            found = candidates(db, "local_username", values)
            if len(found) != 1:
                raise cat.SecureStateChanged()
            uid, aid = next(iter(found))
            row = dict(cat._row(db, "SELECT * FROM auth_identities WHERE id=:a AND user_id=:u AND provider='local' AND payload_state='ENCRYPTED'", a=aid, u=uid))
            saved = cat.snapshot(db, principal_id=UUID(uid), targets=(("auth_identity", UUID(aid)), ("user", UUID(uid))))
            detached_values = detached(db, saved)
        identity, user = decrypt(runtime, detached_values)
        if not authenticated_identity_matches(identity, user_id=uid, identity_id=aid, provider="local", normalized_identifier=normalized) or not verify_password(payload.password, identity.get("credential_hash")):
            raise cat.SecureStateChanged()
        dto = UserResponse.model_validate(user)
        token = create_access_token(uid)
        with tx() as db:
            cat.revalidate(db, saved)
            if lookup_keys(db) != keys or candidates(db, "local_username", values) != found:
                raise cat.SecureStateChanged()
            if dict(cat._row(db, "SELECT * FROM auth_identities WHERE id=:a", a=aid)) != row:
                raise cat.SecureStateChanged()
        return AuthSessionResponse(access_token=token, user=dto)
    except (cat.SecureStateChanged, ValueError, TypeError):
        raise HTTPException(401, "Invalid username or password") from None


@router.get("/auth/me", response_model=UserResponse)
async def me(request: Request):
    return read_user(request.app.state.secure_runtime, principal(request))


def read_room(runtime, uid, rid, *, invite=None, guard=None, proofs=None):
    try:
        with tx() as db:
            saved = (cat.invitation_candidate(db, principal_id=UUID(uid), room_id=UUID(rid)) if invite else
                     cat.snapshot(db, principal_id=UUID(uid), targets=(("room", UUID(rid)),)))
            values = detached(db, saved)
            room = dict(cat._row(db, "SELECT id,owner_user_id FROM family_rooms WHERE id=:r", r=rid))
            membership = tuple(tuple(r) for r in db.execute(text("SELECT user_id,role,related_to_user_id FROM family_members WHERE room_id=:r ORDER BY user_id"), dict(r=rid)))
        if invite:
            payload, proof = cat.authenticate_invitation(saved, values=values[0], supplied_capability=invite,
                vault=runtime.vault, store=runtime.store, guard=guard)
            if proofs is not None:
                proofs.append(proof)
        else:
            payload = decrypt(runtime, values)[0]
        if payload.get("owner_user_id") != room["owner_user_id"] or set(payload.get("members", {})) != {r[0] for r in membership}:
            raise cat.SecureStateChanged()
        if invite and not hmac.compare_digest(payload.get("invite_code", ""), invite):
            raise cat.SecureStateChanged()
        with tx() as db:
            if invite:
                if cat.invitation_candidate(db, principal_id=UUID(uid), room_id=UUID(rid)) != saved:
                    raise cat.SecureStateChanged()
            else:
                cat.revalidate(db, saved)
        return payload, saved, membership
    except (cat.SecureStateChanged, ValueError, TypeError):
        raise HTTPException(404, "Family room not found") from None


def member_dtos(payload, membership):
    members = payload["members"]
    results = []
    for uid, role, related in membership:
        profile = members[uid]
        if profile.get("user_id") != uid:
            raise cat.SecureStateChanged()
        results.append(FamilyMemberResponse(**{k: v for k, v in profile.items() if k != "username"},
            room_id=payload["room_id"], role=role, related_to_user_id=related,
            related_to_user_name=members[related]["name"] if related in members else None))
    return results


@router.post("/families", response_model=FamilyResponse)
async def create_room(payload: FamilyCreateRequest, request: Request):
    if not payload.name.strip() or len(payload.name) > 200:
        raise HTTPException(400, "Invalid room name")
    uid = principal(request)
    op, digest, response = activation_request(request, "ROOM_CREATE", uid)
    if response is not None:
        return response
    runtime = request.app.state.secure_runtime
    if op["state"] == "COMMITTED":
        room, _, _ = read_room(runtime, uid, op["owner_id"])
        if room["owner_user_id"] != uid:
            raise HTTPException(404, "Operation not found")
        return FamilyResponse.model_validate(room)
    user = read_user(runtime, uid).model_dump()
    rid = op["owner_id"]
    room = dict(room_id=rid, name=payload.name, owner_user_id=uid,
                invite_code=UUID(rid).hex.upper() + secrets.token_hex(32).upper(), members={uid: user})
    staged = stage_initial(runtime, op, digest, uid, ((('room', UUID(rid)), room),))
    def owner(db):
        cat._cas(db, "INSERT INTO family_rooms (id,owner_user_id) VALUES (:r,:u)", r=rid, u=uid)
        cat._cas(db, "INSERT INTO family_members (id,room_id,user_id,role) VALUES (:id,:r,:u,'owner')", id=str(uuid4()), r=rid, u=uid)
    with tx() as db:
        publish_initial(db, op, digest, uid, staged, owner)
    return FamilyResponse.model_validate(read_room(runtime, uid, rid)[0])


@router.get("/families", response_model=list[FamilyResponse])
async def list_rooms(request: Request):
    uid = principal(request)
    runtime = request.app.state.secure_runtime
    read_user(runtime, uid)
    with tx() as db:
        rooms = tuple(db.execute(text("SELECT room_id FROM family_members WHERE user_id=:u ORDER BY room_id"), dict(u=uid)).scalars())
    return [FamilyResponse.model_validate(read_room(runtime, uid, rid)[0]) for rid in rooms]


def parse_invite(code):
    if len(code) != 96 or any(c not in "0123456789ABCDEF" for c in code):
        raise HTTPException(404, "Family room not found")
    return str(UUID(hex=code[:32]))


@router.get("/families/invite/{invite_code}", response_model=FamilyJoinPreviewResponse)
async def preview(invite_code: str, request: Request):
    uid = principal(request)
    payload, _, membership = read_room(request.app.state.secure_runtime, uid, parse_invite(invite_code), invite=invite_code, guard=request.scope["secure_guard"])
    return FamilyJoinPreviewResponse(**FamilyResponse.model_validate(payload).model_dump(), members=member_dtos(payload, membership))


def stage_update(runtime, uid, target, payload):
    with tx() as db:
        saved = cat.snapshot(db, principal_id=UUID(uid), targets=(target,))
        saved = cat.claim(db, saved, operation_id=uuid4(), target=target, candidate_object_id=uuid4())
        operation = cat._row(db, "SELECT candidate_object_id FROM secure_operations WHERE operation_id=:o", o=str(saved.operation_id))
        domain, owner, record = cat.ownership(db, *target)
        key = cat._row(db, "SELECT key_id,epoch FROM key_epochs WHERE domain=:d AND owner_id=:o AND state='ACTIVE' AND provisioning_state='PERSISTED'", d=domain, o=owner)
    ref = KeyRef(domain, UUID(owner), UUID(key["key_id"]), key["epoch"])
    raw = raw_json(payload)
    identity = PayloadIdentity(domain, UUID(owner), UUID(record) if record else None, UUID(operation["candidate_object_id"]), 1, cat.KINDS[target[0]], None, None, len(raw))
    sealed = seal_profile(identity, payload, runtime.vault.load(ref)) if domain == "user" else seal_payload(identity, raw, runtime.vault.load(ref))
    receipt = create_ciphertext(runtime, saved, identity, sealed)
    with tx() as db:
        cat.record_receipt(db, saved, operation_id=saved.operation_id, identity=identity, sealed=sealed, receipt=receipt)
    proof = cat.authenticate_readback(identity=identity, sealed=sealed, receipt=receipt, key_ref=ref, vault=runtime.vault, store=runtime.store)
    return saved, identity, sealed, receipt, proof


def publish_update(db, staged):
    saved, identity, sealed, receipt, proof = staged
    cat.verify_readback(db, saved, operation_id=saved.operation_id, proof=proof)
    cat.publish(db, saved, operation_id=saved.operation_id, identity=identity, sealed=sealed, receipt=receipt)
    cat._cas(db, "UPDATE protected_pointers SET source_revision=source_revision+1 WHERE owner_type=:t AND owner_id=:o", t=saved.pointers[0][0], o=str(saved.pointers[0][1]))


@router.post("/families/join", response_model=FamilyResponse)
async def join(payload: FamilyJoinRequest, request: Request):
    uid, runtime = principal(request), request.app.state.secure_runtime
    rid = parse_invite(payload.invite_code)
    proofs = []
    room, saved, membership = read_room(runtime, uid, rid, invite=payload.invite_code, guard=request.scope["secure_guard"], proofs=proofs)
    if uid in room["members"]:
        return FamilyResponse.model_validate(room)
    if payload.related_to_user_id not in room["members"] or payload.relation_to_related_user not in ("parent", "child", "spouse"):
        raise HTTPException(400, "Select a current family member and relationship")
    user = read_user(runtime, uid).model_dump()
    updated = dict(room, members=room["members"] | {uid: user | {"relation_to_related_user": payload.relation_to_related_user}})
    with cat.invitation_access(saved, guard=request.scope["secure_guard"], authenticated_payload=proofs[0], supplied_capability=payload.invite_code):
        staged = stage_update(runtime, uid, ("room", UUID(rid)), updated)
        with tx() as db:
            # Publication still revalidates the pre-membership capability snapshot.
            publish_update(db, staged)
            cat._cas(db, "INSERT INTO family_members (id,room_id,user_id,role,related_to_user_id) VALUES (:id,:r,:u,'member',:related)", id=str(uuid4()), r=rid, u=uid, related=payload.related_to_user_id)
            cat._cas(db, "UPDATE security_scopes SET authorization_epoch=authorization_epoch+1,revision=revision+1 WHERE domain='room' AND owner_id=:r", r=rid)
            cat._cas(db, "UPDATE protected_pointers SET authorization_epoch=authorization_epoch+1 WHERE owner_type='room' AND owner_id=:r", r=rid)
    return FamilyResponse.model_validate(read_room(runtime, uid, rid)[0])


@router.get("/families/{room_id}", response_model=FamilyDetailResponse)
async def room_detail(room_id: str, request: Request):
    room, _, membership = read_room(request.app.state.secure_runtime, principal(request), room_id)
    return FamilyDetailResponse(**FamilyResponse.model_validate(room).model_dump(), member_count=len(membership))


@router.get("/families/{room_id}/members", response_model=list[FamilyMemberResponse])
async def members(room_id: str, request: Request):
    room, _, membership = read_room(request.app.state.secure_runtime, principal(request), room_id)
    return member_dtos(room, membership)


@router.put("/auth/profile", response_model=UserResponse)
async def update_profile(payload: UserProfileUpdateRequest, request: Request):
    from app.services.personal_context import raw_profile
    from app.api.routes.secure_media import abandon_staged
    from dataclasses import replace
    uid, runtime = principal(request), request.app.state.secure_runtime
    original, before = raw_profile(runtime, uid)
    user = UserResponse.model_validate(original).model_dump() | payload.model_dump(mode="json")
    user["email"] = normalize_lookup("email", user["email"]).decode()
    user["profile_chunk"] = profile_chunk(user)
    with tx() as db:
        keys = lookup_keys(db)
        rooms = tuple(db.execute(text("SELECT room_id FROM family_members WHERE user_id=:u ORDER BY room_id"), dict(u=uid)).scalars())
    values = {"email": tokens(runtime, keys, "email", user["email"])}
    try:
        with tx() as db:
            unique(db, keys, values, owner=uid)
        changes = []
        for rid in rooms:
            room = read_room(runtime, uid, rid)[0]
            room["members"][uid] = room["members"][uid] | user
            changes.append(stage_update(runtime, uid, ("room", UUID(rid)), room))
        changes.append(stage_update(runtime, uid, ("user", UUID(uid)), original | user))
        if replace(changes[-1][0], operation_id=None) != replace(before, operation_id=None):
            abandon_staged(changes)
            raise cat.SecureStateChanged()
        with tx() as db:
            unique(db, keys, values, owner=uid)
            for change in changes:
                publish_update(db, change)
            db.execute(text("DELETE FROM blind_indexes WHERE kind='email' AND user_id=:u"), dict(u=uid))
            insert_tokens(db, "email", values["email"], uid)
    except HTTPException as exc:
        if exc.status_code != 409:
            raise
        return error(409, "Identity already exists", "identity_conflict")
    return read_user(runtime, uid)
