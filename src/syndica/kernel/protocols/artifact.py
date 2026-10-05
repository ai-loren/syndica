"""ArtifactStore：产物身份与版本的存储契约（spec §4.4 / §7.2）。

只存**索引与元数据**（路径、版本、producer、是否交付物），产物正文在 Workspace（§6）——所以写入面收
明文 `ArtifactRef` 而非脱敏类型（没有正文可泄）。`id` 跨版本稳定（同一路径的多次写是同一 `id` 的新
`version`），`version` 不透明（git SHA 不可排序），内核只按**登记顺序**排序、只做相等比较（§7.2）。

与别的 Store 同两条不变式：

- **读己所写**：`register` 返回后紧接着的 `get` / `versions_of` 必须看到它。
- **拒绝即无副作用**：抛异常的写不得留下部分写入。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from syndica.kernel.context import ArtifactRef

__all__ = ['ArtifactStore']


@runtime_checkable
class ArtifactStore(Protocol):
    async def register(self, ref: ArtifactRef) -> None:
        """登记产物的一个版本。按 `ref.id` 归一（同 id 追加新 version，登记顺序即版本顺序）；同一
        `(id, version)` 重复登记幂等（§7.2）。`producer` / `is_deliverable` 逐版本记录。"""
        ...

    async def get(self, artifact_id: str) -> ArtifactRef | None:
        """这个产物的最新登记版本；没登记过是 None。返回快照，改它不影响存储。"""
        ...

    async def versions_of(self, artifact_id: str) -> list[ArtifactRef]:
        """这个产物的全部版本，**按登记顺序**（不排序 version 字符串，它不透明）。没有则空列表。"""
        ...

    async def list(
        self,
        *,
        kind: str | None = None,
        producer: str | None = None,
        deliverable_only: bool = False,
    ) -> list[ArtifactRef]:
        """每个产物一条（最新版本），按 `kind` / `producer` / 是否交付物过滤。`deliverable_only`
        收「任一版本被标过交付物」的产物（交付标记是产物级的粘性属性，§7.4）。"""
        ...
