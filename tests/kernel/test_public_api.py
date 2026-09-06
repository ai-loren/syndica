"""`syndica.kernel.__all__` 是公开 API：列进去就是永久兼容承诺，导不进来就是发布即坏。"""

from __future__ import annotations

import syndica.kernel as kernel


def test_every_exported_name_resolves():
    assert [name for name in kernel.__all__ if not hasattr(kernel, name)] == []


def test_export_list_is_sorted():
    assert list(kernel.__all__) == sorted(kernel.__all__)
