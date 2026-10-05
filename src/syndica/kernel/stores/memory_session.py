"""SessionStore 的内存实现：单机默认，零依赖。"""

from __future__ import annotations

import copy
import time

from syndica.kernel.errors import ContractViolation
from syndica.kernel.session_redaction import RedactedContent, RedactedSessionRecord
from syndica.kernel.session_types import SessionMeta, SessionRecord, SessionStatus

__all__ = ['InMemorySessionStore']


class InMemorySessionStore:
    def __init__(self) -> None:
        self._records: dict[str, SessionRecord] = {}
        # 每个会话归过几次档：序号只增不补，取当前最大值 +1。
        self._archives: dict[str, list[SessionRecord]] = {}

    async def exists(self, sid: str) -> bool:
        return sid in self._records

    async def load_meta(self, sid: str) -> SessionMeta | None:
        record = self._records.get(sid)
        return copy.deepcopy(record.meta) if record is not None else None

    async def load(self, sid: str) -> SessionRecord | None:
        record = self._records.get(sid)
        return copy.deepcopy(record) if record is not None else None

    async def save(self, sid: str, record: RedactedSessionRecord) -> None:
        inner = record.record
        if inner.meta.sid != sid:
            raise ContractViolation(
                expected=f'record.meta.sid 与 save 的 sid 一致（都是 {sid!r}）',
                received=f'record.meta.sid={inner.meta.sid!r}，与 {sid!r} 不符',
                next_action='它们指同一会话；对齐两者，否则记录按一个 id 存、按另一个 id 找不回',
            )
        self._records[sid] = copy.deepcopy(inner)

    async def archive(self, sid: str, record: RedactedSessionRecord) -> int:
        inner = record.record
        if inner.meta.sid != sid:
            raise ContractViolation(
                expected=f'record.meta.sid 与 archive 的 sid 一致（都是 {sid!r}）',
                received=f'record.meta.sid={inner.meta.sid!r}，与 {sid!r} 不符',
                next_action='它们指同一会话；对齐两者，否则归档按一个 id 存、按另一个 id 找不回',
            )
        history = self._archives.setdefault(sid, [])
        seq = (history[-1].meta.archived if history else 0) + 1
        snapshot = copy.deepcopy(record.record)
        snapshot.meta.status = SessionStatus.ARCHIVED
        snapshot.meta.archived = seq
        history.append(snapshot)
        live = self._records.get(sid)
        if live is not None:
            live.meta.archived = seq
        return seq

    async def list_metas(
        self,
        *,
        status: SessionStatus | None = None,
        agent: str | None = None,
        workspace: str | None = None,
    ) -> list[SessionMeta]:
        metas = [
            copy.deepcopy(record.meta)
            for record in self._records.values()
            if (status is None or record.meta.status is status)
            and (agent is None or record.meta.agent == agent)
            and (workspace is None or record.meta.workspace == workspace)
        ]
        metas.sort(key=lambda meta: meta.updated_at, reverse=True)
        return metas

    async def replace_content(self, sid: str, content: RedactedContent) -> None:
        record = self._records.get(sid)
        if record is None:
            raise ContractViolation(
                expected=f'会话 {sid!r} 已存在',
                received='存储里没有这个会话',
                next_action='先 save 再 replace_content，或检查 sid 是否拼错',
            )
        record.content = copy.deepcopy(content.content)
        record.meta.updated_at = time.time()

    async def delete(self, sid: str) -> None:
        self._records.pop(sid, None)
        self._archives.pop(sid, None)

    async def read_archived(self, sid: str, seq: int) -> SessionRecord | None:
        for snapshot in self._archives.get(sid, []):
            if snapshot.meta.archived == seq:
                return copy.deepcopy(snapshot)
        return None
