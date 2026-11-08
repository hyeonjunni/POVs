class StorageService:
    def build_storage_path(self, room_id: str, filename: str) -> str:
        return f"rooms/{room_id}/uploads/{filename}"

