import mimetypes
import re
import shutil
import time
import uuid
from pathlib import Path
from urllib.parse import unquote, urlparse

from fastapi import UploadFile


def safe_name(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", Path(name).name) or "upload.bin"


class AssetService:
    def __init__(self, data_dir: Path, max_upload_mb: int) -> None:
        self.data_dir = data_dir.resolve()
        self.assets_dir = (data_dir / "assets").resolve()
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_upload_mb * 1024 * 1024

    async def save_upload(
        self, upload: UploadFile, destination: Path
    ) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        with destination.open("wb") as out:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > self.max_bytes:
                    out.close()
                    destination.unlink(missing_ok=True)
                    raise ValueError("上传文件超过大小限制")
                out.write(chunk)
        return destination

    async def upload_to_library(self, upload: UploadFile) -> dict:
        filename = safe_name(upload.filename or "asset.bin")
        destination = self._unique_asset_path(filename)
        path = await self.save_upload(upload, destination)
        return self.describe(path)

    def save_existing_url(self, url: str, preferred_name: str = "") -> dict:
        source = self.resolve_data_url(url)
        if not source.is_file():
            raise FileNotFoundError("待保存文件不存在")

        filename = safe_name(preferred_name.strip() or source.name)
        if not Path(filename).suffix and source.suffix:
            filename += source.suffix.lower()

        # 已经在素材库中时直接返回，避免重复复制。
        try:
            source.relative_to(self.assets_dir)
            return self.describe(source)
        except ValueError:
            pass

        destination = self._unique_asset_path(filename)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        return self.describe(destination)

    def resolve_asset_url(self, url: str) -> Path:
        path = self.resolve_data_url(url)
        try:
            path.relative_to(self.assets_dir)
        except ValueError as exc:
            raise ValueError("只能选择素材库中的文件") from exc
        if not path.is_file():
            raise FileNotFoundError("素材库文件不存在")
        return path

    def delete_asset_url(self, url: str) -> dict:
        path = self.resolve_asset_url(url)
        description = self.describe(path)
        path.unlink()

        # 清理素材库中已经变空的子目录，但绝不删除素材库根目录。
        parent = path.parent
        while parent != self.assets_dir:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

        return {
            "deleted": True,
            "name": description["name"],
            "url": description["url"],
        }

    def resolve_data_url(self, url: str) -> Path:
        parsed = urlparse(url)
        raw_path = unquote(parsed.path or url)
        prefix = "/files/"
        if not raw_path.startswith(prefix):
            raise ValueError("素材地址格式不正确")
        relative = Path(raw_path[len(prefix):])
        candidate = (self.data_dir / relative).resolve()
        try:
            candidate.relative_to(self.data_dir)
        except ValueError as exc:
            raise ValueError("素材地址越界") from exc
        return candidate

    def url(self, path: Path) -> str:
        relative = path.resolve().relative_to(self.data_dir)
        return "/files/" + "/".join(relative.parts)

    def describe(self, path: Path) -> dict:
        mime, _ = mimetypes.guess_type(path.name)
        stat = path.stat()
        return {
            "id": path.stem,
            "name": path.name,
            "url": self.url(path),
            "size": stat.st_size,
            "mime": mime or "application/octet-stream",
            "modified": stat.st_mtime,
            "in_library": True,
        }

    def list_assets(self, limit: int = 200) -> list[dict]:
        files = [path for path in self.assets_dir.rglob("*") if path.is_file()]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return [self.describe(path) for path in files[:limit]]

    def _unique_asset_path(self, filename: str) -> Path:
        cleaned = safe_name(filename)
        stem = Path(cleaned).stem[:80] or "asset"
        suffix = Path(cleaned).suffix.lower()
        stamp = time.strftime("%Y%m%d_%H%M%S")
        token = uuid.uuid4().hex[:8]
        return self.assets_dir / f"{stamp}_{token}_{stem}{suffix}"
