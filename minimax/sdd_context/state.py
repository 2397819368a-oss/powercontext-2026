"""任务状态：优先用 PowerContext 的 Artifact 记录进展，落盘后可在新进程读回。

实测约束（这两个决定了下面的实现方式）：

1. ``PowerContext`` 里挂着 ``SourceCatalog``/store 等运行时对象，**整体不可序列化**，
   所以持久化只落在 ``Artifact`` 数据上；
2. ``Artifacts.add`` / ``revise`` / ``latest`` / ``revisions`` **全部是协程**。

每条任务用 ``artifact_id = task:<task_id>``，每次进展追加一个 revision，
因此「重启后读回最新状态」等价于 ``latest``，而历史可经 ``revisions`` 追溯。

**为什么这里要写两套后端（2026-10-30 补充）：**
第一版只依赖 ``powercontext``，结果在一台没装它的机器上，``import powercontext``
直接 ModuleNotFoundError，**整个流水线连启动都做不到**——而且它还会连带要求
``pydantic``、``rfc8785`` 等一串依赖。对外交付的项目不该因为可选依赖缺失就
整体崩掉，所以 PowerContext 降级为「有就用，没有也能跑」：

- 有 → 走 ``_PowerContextBackend``，行为和之前完全一致（revision 语义来自 pc）；
- 没有 → 走 ``_JsonBackend``，用同样语义的纯 Python 实现，对外接口一模一样。

两条路径产生的 JSON 结构是兼容的（都是 task_id → revision 列表），所以
中途切换后端不会丢历史。选用哪条会写进运行报告，不会掩盖差异。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:  # 可选依赖：缺了不能让整个模块挂掉
    import powercontext as pc

    HAS_POWERCONTEXT = True
    POWERCONTEXT_IMPORT_ERROR = ""
except Exception as exc:  # noqa: BLE001 - 无论缺包还是包自身报错，都降级
    pc = None  # type: ignore[assignment]
    HAS_POWERCONTEXT = False
    POWERCONTEXT_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


# ============================ 后端一：PowerContext ============================


class _EmptySourceBackend:
    """本模块不用 Source 存资料（工程资料在 seekdb 里），给一个空实现即可。"""

    async def list(self) -> tuple:
        return ()

    async def get(self, source, /):
        raise pc.SourceNotFoundError(source)


class _MemorySourceStore:
    def __init__(self) -> None:
        self.rows: list = []

    async def add(self, value, /):
        self.rows.append(value)
        return value


class _MemoryArtifactStore:
    def __init__(self) -> None:
        self.rows: dict[str, list] = {}

    async def add(self, draft, /):
        artifact_id = (draft.content or {}).get("_artifact_id") if isinstance(draft.content, dict) else None
        artifact_id = artifact_id or f"task-{len(self.rows) + 1}"
        artifact = pc.Artifact(
            artifact_id=artifact_id,
            revision=1,
            content=draft.content,
            lineage=pc.ArtifactLineage(sources=tuple(draft.sources), artifacts=tuple(draft.artifacts)),
        )
        self.rows.setdefault(artifact_id, []).append(artifact)
        return artifact

    async def revise(self, artifact, draft, /):
        history = self.rows[artifact.artifact_id]
        new = history[-1].model_copy(
            update={"revision": history[-1].revision + 1, "content": draft.content}
        )
        history.append(new)
        return new


class _MemoryArtifactCatalog:
    def __init__(self, store: _MemoryArtifactStore) -> None:
        self._store = store

    async def get(self, artifact, /):
        for row in self._store.rows.get(artifact.artifact_id, []):
            if row.revision == artifact.revision:
                return row
        raise pc.ArtifactNotFoundError(artifact.artifact_id)

    async def latest(self, artifact, /):
        return self._store.rows[artifact.artifact_id][-1]

    async def revisions(self, artifact, /):
        return tuple(self._store.rows[artifact.artifact_id])


class _PowerContextBackend:
    """原实现：revision 语义完全由 PowerContext 的 Artifact 提供。"""

    def __init__(self) -> None:
        self.store = _MemoryArtifactStore()
        self.ctx = pc.PowerContext(
            sources=pc.Sources(
                catalog=pc.SourceCatalog(backend=_EmptySourceBackend()),
                store=_MemorySourceStore(),
            ),
            artifacts=pc.Artifacts(
                catalog=_MemoryArtifactCatalog(self.store),
                store=self.store,
            ),
            triggers=(),
        )

    # 两个后端对外都暴露可读写的 ``rows``，这样持久化代码不用分支。
    @property
    def rows(self) -> dict[str, list]:
        return self.store.rows

    @rows.setter
    def rows(self, value: dict[str, list]) -> None:
        self.store.rows = value

    async def record(self, artifact_id: str, body: dict[str, Any]) -> Any:
        existing = self.store.rows.get(artifact_id)
        if existing:
            return await self.ctx.artifacts.revise(existing[-1], pc.ArtifactDraft(content=body))
        payload = dict(body)
        payload["_artifact_id"] = artifact_id
        return await self.ctx.artifacts.add(pc.ArtifactDraft(content=payload))

    def _has(self, artifact_id: str) -> bool:
        return artifact_id in self.store.rows

    async def latest(self, artifact_id: str) -> Any:
        return await self.ctx.artifacts.latest(self.store.rows[artifact_id][-1])

    async def history(self, artifact_id: str) -> list[Any]:
        return list(await self.ctx.artifacts.revisions(self.store.rows[artifact_id][-1]))

    def content_of(self, artifact: Any) -> dict[str, Any]:
        return dict(artifact.content)

    def ident_of(self, artifact: Any) -> tuple[str, int]:
        return artifact.artifact_id, artifact.revision

    def to_raw(self, rows: dict[str, list]) -> dict[str, list]:
        return {aid: [json.loads(item.model_dump_json()) for item in revs] for aid, revs in rows.items()}

    def load_rows(self, raw: dict[str, list]) -> dict[str, list]:
        return {aid: [pc.Artifact.model_validate(item) for item in revs] for aid, revs in raw.items()}


# ========================== 后端二：纯 Python 降级 ==========================


class _StubArtifact:
    """只保留 advert 了的字段里我们真正用到的那几个。"""

    __slots__ = ("artifact_id", "revision", "content")

    def __init__(self, artifact_id: str, revision: int, content: dict[str, Any]) -> None:
        self.artifact_id = artifact_id
        self.revision = revision
        self.content = content

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "revision": self.revision,
            "content": self.content,
        }


class _JsonBackend:
    """不依赖任何第三方包的等价实现。

    revision 语义手工维护：``record`` 时取当前最大 revision + 1，
    与 PowerContext 的行为一致（首次=1，每次追加 +1）。
    """

    def __init__(self) -> None:
        self.rows: dict[str, list[_StubArtifact]] = {}

    async def record(self, artifact_id: str, body: dict[str, Any]) -> _StubArtifact:
        history = self.rows.setdefault(artifact_id, [])
        revision = (history[-1].revision + 1) if history else 1
        artifact = _StubArtifact(artifact_id, revision, dict(body))
        history.append(artifact)
        return artifact

    def _has(self, artifact_id: str) -> bool:
        return bool(self.rows.get(artifact_id))

    async def latest(self, artifact_id: str) -> _StubArtifact:
        return self.rows[artifact_id][-1]

    async def history(self, artifact_id: str) -> list[_StubArtifact]:
        return list(self.rows[artifact_id])

    def content_of(self, artifact: _StubArtifact) -> dict[str, Any]:
        return dict(artifact.content)

    def ident_of(self, artifact: _StubArtifact) -> tuple[str, int]:
        return artifact.artifact_id, artifact.revision

    def to_raw(self, rows: dict[str, list]) -> dict[str, list]:
        return {aid: [item.to_dict() for item in revs] for aid, revs in rows.items()}

    def load_rows(self, raw: dict[str, list]) -> dict[str, list]:
        return {
            aid: [
                _StubArtifact(item["artifact_id"], item["revision"], item.get("content") or {})
                for item in revs
            ]
            for aid, revs in raw.items()
        }


# ================================ 对外入口 ================================


class TaskStateStore:
    """任务状态的读写入口；后端随 PowerContext 是否可用自动切换。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._log: list[dict[str, Any]] = []
        self.backend = _PowerContextBackend() if HAS_POWERCONTEXT else _JsonBackend()

    @property
    def backend_name(self) -> str:
        return "powercontext" if HAS_POWERCONTEXT else "json-fallback"

    @property
    def degraded_reason(self) -> str:
        return "" if HAS_POWERCONTEXT else POWERCONTEXT_IMPORT_ERROR

    # ---------- 写 ----------
    async def record(self, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """追加一次进展；同一任务多次记录会形成 revision 链。"""

        artifact_id = f"task:{task_id}"
        body = dict(payload)
        if not self.backend._has(artifact_id):
            body["_artifact_id"] = artifact_id
        artifact = await self.backend.record(artifact_id, body)
        aid, revision = self.backend.ident_of(artifact)
        self._log.append(
            {
                "task_id": task_id,
                "artifact_id": aid,
                "revision": revision,
                "stage": (dict(body) or {}).get("stage"),
            }
        )
        return {"artifact_id": aid, "revision": revision}

    # ---------- 读 ----------
    async def latest(self, task_id: str) -> dict[str, Any] | None:
        artifact_id = f"task:{task_id}"
        if not self.backend._has(artifact_id):
            return None
        artifact = await self.backend.latest(artifact_id)
        _, revision = self.backend.ident_of(artifact)
        return self.backend.content_of(artifact) | {"_revision": revision}

    async def history(self, task_id: str) -> list[dict[str, Any]]:
        artifact_id = f"task:{task_id}"
        if not self.backend._has(artifact_id):
            return []
        revisions = await self.backend.history(artifact_id)
        return [
            self.backend.content_of(item) | {"_revision": self.backend.ident_of(item)[1]}
            for item in revisions
        ]

    # ---------- 持久化 ----------
    def persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        rows = self.backend.rows
        payload = self.backend.to_raw(rows)
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def restore(self) -> int:
        if not self.path.exists():
            return 0
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.backend.rows = self.backend.load_rows(raw)
        return len(self.backend.rows)

    def dump_log(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self._log, ensure_ascii=False, indent=2), encoding="utf-8")
