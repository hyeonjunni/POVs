"""Import every mapped model so `Base.metadata` is fully populated."""

from app.models import family_context  # noqa: F401 -- register additive M3 tables

from app.models.record import (  # noqa: F401
    Record, RecordGrant, RecordPerson, RecordRestriction, RecordRestrictionGrant,
    RecordAudit, RecordMetadataRevision, RecordDeletionRequest, RecordIdempotency,
)

from app.models.auth_identity import AuthIdentity  # noqa: F401
from app.models.deletion_intent import DeletionIntent  # noqa: F401
from app.models.family import FamilyMember, FamilyPerson, FamilyRelationship, FamilyRoom  # noqa: F401
from app.models.processing_job import ProcessingJob  # noqa: F401
from app.models.storage_allocation import LegacyStorageAllocation  # noqa: F401
from app.models.retrieval import RecordChunk, RecordEmbedding  # noqa: F401
from app.models.upload import Upload, UploadFile  # noqa: F401
from app.models.user import User  # noqa: F401
from app.models.secure_catalogue import (  # noqa: F401
    SecurityControl, SecurityScope, KeyEpoch, CredentialGeneration,
    CredentialGenerationKey, EncryptedObject, ProtectedPointer, SecureOperation,
    BlindIndex, SecurityTombstone, SecureDeletionInventory,
)

__all__ = [
    "AuthIdentity",
    "DeletionIntent",
    "FamilyMember",
    "FamilyPerson",
    "FamilyRelationship",
    "FamilyRoom",
    "ProcessingJob",
    "LegacyStorageAllocation",
    "RecordChunk",
    "RecordEmbedding",
    "Upload",
    "UploadFile",
    "User",
    "SecurityControl", "SecurityScope", "KeyEpoch", "CredentialGeneration",
    "CredentialGenerationKey", "EncryptedObject", "ProtectedPointer", "SecureOperation",
    "BlindIndex", "SecurityTombstone", "SecureDeletionInventory",
]
