"""路径与运行参数。

**seekdb 的 Windows 构建不认非 ASCII 路径**，所以库目录必须落在纯 ASCII
路径上。项目本身可以放在中文目录里，只有库目录需要规避。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# minimax/ 包的上一级就是项目根（含 workspace/、samples/、records/）
PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = PROJECT_ROOT / "workspace"
SAMPLES = PROJECT_ROOT / "samples"
RECORDS = PROJECT_ROOT / "records"


def _ascii_safe(path: Path) -> Path:
    """确保路径可用于 seekdb。

    seekdb 的 Windows 构建按 UTF-8 解释路径，遇到 GBK 编码的非 ASCII 目录
    （例如中文用户名下的 ``AppData\\Local\\Temp``）会直接
    ``seekdb_open failed: code=-1``。所以一旦发现非 ASCII，就退到根目录下的
    纯 ASCII 路径。
    """

    if str(path).isascii():
        return path
    fallback_root = Path("C:/Temp") if os.name == "nt" else Path("/tmp")
    return fallback_root / "sdd_context_seekdb"


@dataclass(frozen=True, slots=True)
class Settings:
    """一次运行的全部可调项。"""

    db_dir: Path
    workspace: Path
    records: Path
    top_k_spec: int = 4
    top_k_code: int = 4
    top_k_test: int = 4
    max_context_chars: int = 12000

    @classmethod
    def load(cls, *, db_dir: Path | None = None, workspace: Path | None = None) -> Settings:
        root = workspace or WORKSPACE
        env_db = os.environ.get("SDD_DB_DIR")
        if db_dir is not None:
            chosen = db_dir
        elif env_db:
            chosen = Path(env_db)
        else:
            chosen = root.parent / "var" / "seekdb"
        return cls(db_dir=_ascii_safe(chosen), workspace=root, records=RECORDS)
