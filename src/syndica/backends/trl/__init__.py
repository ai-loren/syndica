"""TRL 对接。导入本包不导入 torch。"""

from syndica.backends.trl.dry_run import dry_run

__all__ = ['dry_run']
