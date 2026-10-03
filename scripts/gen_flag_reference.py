"""重新生成 docs/reference/flags.md：内置 Module 的 Flag 声明改了之后跑一次。

用例 `tests/cli/test_flag_reference.py` 断言这份文件与生成器的输出逐字相同，忘了重新生成会在
那里变红。
"""

from __future__ import annotations

from pathlib import Path

from syndica.cli.flags import render_reference
from syndica.cli.host import reference_modules
from syndica.kernel import flag_reference

TARGET = Path(__file__).resolve().parent.parent / 'docs' / 'reference' / 'flags.md'


def main() -> None:
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(render_reference(flag_reference(reference_modules())), encoding='utf-8')


if __name__ == '__main__':
    main()
