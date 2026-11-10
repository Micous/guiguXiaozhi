"""知识库清单 —— 支撑增量更新。

记录每个源文件的内容哈希与块数：
  * 哈希没变 → 该文件无需重建（增量更新的依据）
  * 哈希变了 → 删除该文件的旧块，写入新块
  * 文件消失 → 删除其全部旧块
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class FileEntry:
    """单个源文件的清单条目"""

    file_name: str
    content_hash: str
    chunk_count: int = 0
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Manifest:
    """知识库清单"""

    version: int = 0
    files: Dict[str, FileEntry] = field(default_factory=dict)
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "updated_at": self.updated_at,
            "files": {k: v.to_dict() for k, v in self.files.items()},
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Manifest":
        files = {
            name: FileEntry(**entry) for name, entry in (data.get("files") or {}).items()
        }
        return cls(
            version=int(data.get("version", 0)),
            files=files,
            updated_at=data.get("updated_at", ""),
        )

    def diff(self, new_files: Dict[str, FileEntry]) -> Dict[str, List[str]]:
        """对比新旧清单，得出要新增/更新/删除的文件"""
        to_update = [
            name
            for name, entry in new_files.items()
            if name not in self.files or self.files[name].content_hash != entry.content_hash
        ]
        to_remove = [name for name in self.files if name not in new_files]
        return {"update": to_update, "remove": to_remove}

    def is_empty(self) -> bool:
        return not self.files


def file_hash(path: Path) -> str:
    """文件内容哈希"""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def build_manifest(
    directory: Path, chunk_counts: Dict[str, int], patterns: tuple = ("*.md", "*.txt")
) -> Manifest:
    """根据目录内容构建清单"""
    from datetime import datetime

    files: Dict[str, FileEntry] = {}
    for pattern in patterns:
        for path in sorted(directory.glob(pattern)):
            files[path.name] = FileEntry(
                file_name=path.name,
                content_hash=file_hash(path),
                chunk_count=chunk_counts.get(path.name, 0),
                updated_at=datetime.now().isoformat(timespec="seconds"),
            )
    return Manifest(version=0, files=files, updated_at=datetime.now().isoformat(timespec="seconds"))


def load_manifest(path: Path) -> Manifest:
    """读取清单（不存在返回空清单）"""
    if not path.exists():
        return Manifest()
    try:
        return Manifest.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return Manifest()


def save_manifest(path: Path, manifest: Manifest) -> None:
    """保存清单"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
