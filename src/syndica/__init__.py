"""Syndica — a multi-agent harness for verifiable knowledge work."""

from importlib import metadata as _metadata

__all__ = ['__version__']

try:
    __version__ = _metadata.version('syndica')
except _metadata.PackageNotFoundError:
    # 没有装成发行版（只把 src 放进 PYTHONPATH）就读不到元数据。版本号只在 pyproject.toml 里写一次，
    # 这里不再写第二份：写了就会和发行版对不上。
    __version__ = '0+unknown'
