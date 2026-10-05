"""ArtifactStore 的内存实现（spec §4.4 / §7.2）。

按 `id` 存版本列表（登记顺序），交付物标记是产物级的粘性属性：任一版本标过，这个产物就是交付物
（§7.4——隐式追踪会在 `Deliver` 之后又登记新版本，那次没带标记，但产物已经是交付物了）。
"""

from __future__ import annotations

import asyncio

from syndica.kernel.context import ArtifactRef

__all__ = ['InMemoryArtifactStore']


class InMemoryArtifactStore:
    def __init__(self) -> None:
        self._versions: dict[str, list[ArtifactRef]] = {}
        self._order: list[str] = []
        self._deliverable: set[str] = set()
        self._lock = asyncio.Lock()

    async def register(self, ref: ArtifactRef) -> None:
        async with self._lock:
            versions = self._versions.get(ref.id)
            if versions is None:
                self._versions[ref.id] = [ref]
                self._order.append(ref.id)
            elif any(existing.version == ref.version for existing in versions):
                # 同一 (id, version) 重复登记幂等：保留先登记的那条，不追加重复版本（§7.2）。
                if ref.is_deliverable:
                    self._deliverable.add(ref.id)
                return
            else:
                versions.append(ref)
            if ref.is_deliverable:
                self._deliverable.add(ref.id)

    async def get(self, artifact_id: str) -> ArtifactRef | None:
        versions = self._versions.get(artifact_id)
        return versions[-1] if versions else None

    async def versions_of(self, artifact_id: str) -> list[ArtifactRef]:
        return list(self._versions.get(artifact_id, ()))

    async def list(
        self,
        *,
        kind: str | None = None,
        producer: str | None = None,
        deliverable_only: bool = False,
    ) -> list[ArtifactRef]:
        out: list[ArtifactRef] = []
        for artifact_id in self._order:
            latest = self._versions[artifact_id][-1]
            if kind is not None and latest.kind != kind:
                continue
            if producer is not None and latest.producer != producer:
                continue
            if deliverable_only and artifact_id not in self._deliverable:
                continue
            out.append(latest)
        return out
