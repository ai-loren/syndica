"""ArtifactStore 的内存实现（spec §4.4 / §7.2）。

按 `id` 存版本列表（登记顺序），交付物标记是产物级的粘性属性：任一版本标过，这个产物就是交付物
（§7.4——隐式追踪会在 `Deliver` 之后又登记新版本，那次没带标记，但产物已经是交付物了）。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

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
            elif versions[-1].version == ref.version:
                # 与当前版本相同：幂等，不追加（§7.2）；交付标记照样置上（Deliver 走这条路）。
                pass
            else:
                # 新版本，或改回了早先的某一版（A→B→A）：都追加一条，最新一条就是当前版本。只看「有
                # 没有登记过」会让改回 A 之后 get() 仍交出已不存在的 B（评审 Medium 7）。
                versions.append(ref)
            if ref.is_deliverable:
                self._deliverable.add(ref.id)

    async def get(self, artifact_id: str) -> ArtifactRef | None:
        versions = self._versions.get(artifact_id)
        return self._current(artifact_id, versions[-1]) if versions else None

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
            out.append(self._current(artifact_id, latest))
        return out

    def _current(self, artifact_id: str, latest: ArtifactRef) -> ArtifactRef:
        # 交付标记是产物级的：最新版本是隐式追踪登记的（没带标记）时，交出去的 ref 也要说它是交付
        # 物。
        return replace(latest, is_deliverable=artifact_id in self._deliverable)
