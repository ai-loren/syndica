"""发布前检查 `uv build` 产出的 wheel 与 sdist，全部通过才写出 `dist/SHA256SUMS`。

查三类问题，每条按「期望 / 实际 / 下一步」报：

- 文件：wheel 里的 `syndica/` 与 git 里 `src/syndica/` 的文件逐个对上、逐字节相同（漏了前端
  产物或 `py.typed`、混进没提交的本地文件、构建之后又改了源码都在这里查出来），且只有包与
  dist-info 两个顶层目录；sdist 只装重建 wheel 要的那几样，没有测试、文档、前端源码。
- 元数据：名字、版本（规范的 PEP 440 公开版本，等于 pyproject.toml）、requires-python、SPDX
  许可证、必需的 classifiers、不声明 Windows、入口点 `syndica`、各个 extra、项目链接；wheel 与
  sdist 的元数据一致。
- dist/ 里只有这一版的一个 wheel 和一个 sdist：`uv publish dist/*` 会把留在那里的旧产物一起
  传上去。

给有问题的产物算校验和只会让它看起来可以发布，所以有问题时不写、并删掉上一次留下的 SHA256SUMS。
只用标准库：验收在干净的容器里从 wheel 起步，那里不一定装着开发依赖。

通过时退出 0；有问题时逐条报到 stderr、退出 1；dist 目录、pyproject.toml 或 git 用不了时退出 2。
"""

from __future__ import annotations

import argparse
import configparser
import hashlib
import io
import re
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from email.message import Message
from email.parser import HeaderParser
from pathlib import Path

NAME = 'syndica'
REQUIRES_PYTHON = '>=3.11'
LICENSE_EXPRESSION = 'MIT'
REQUIRED_CLASSIFIERS = (
    'Framework :: AsyncIO',
    'Intended Audience :: Developers',
    'Operating System :: MacOS',
    'Operating System :: POSIX :: Linux',
    'Programming Language :: Python :: 3',
    'Programming Language :: Python :: 3.11',
    'Programming Language :: Python :: 3.12',
    'Programming Language :: Python :: 3.13',
    'Typing :: Typed',
)
# Windows 不支持（用 WSL2）；OS Independent 等于声明了 Windows；PEP 639 下许可证只由 SPDX
# 表达式给出，与 License :: 分类并存时 PyPI 拒收。
FORBIDDEN_CLASSIFIERS = (
    'Operating System :: Microsoft',
    'Operating System :: OS Independent',
    'License ::',
)
EXTRAS = ('openai', 'server', 'otel', 'mcp', 'acp', 'a2a', 'trl')
PROJECT_URLS = ('Homepage', 'Repository', 'Issues', 'Changelog', 'Documentation')
URL_PREFIX = 'https://github.com/ai-loren/syndica'
ENTRY_POINT = ('syndica', 'syndica.__main__:entrypoint')
# git 的集合对得上也不够：这几样从仓库里删掉时两边一起少，集合照样相等。
REQUIRED_PACKAGE_FILES = (
    'syndica/__init__.py',
    'syndica/__main__.py',
    'syndica/py.typed',
    'syndica/server/static/index.html',
    'syndica/server/static/build-info.json',
)
STATIC_ASSETS = 'syndica/server/static/assets/'
# .gitignore 由 hatchling 自己放进 sdist：从 sdist 构建 wheel 时靠它排除缓存与本地文件。
SDIST_TOP_LEVEL = (
    '.gitignore',
    'CHANGELOG.md',
    'LICENSE',
    'PKG-INFO',
    'README.md',
    'pyproject.toml',
    'src',
)
SDIST_COPIES = ('.gitignore', 'CHANGELOG.md', 'LICENSE', 'README.md', 'pyproject.toml')
DIST_INFO_FILES = ('METADATA', 'WHEEL', 'RECORD', 'entry_points.txt', 'licenses/LICENSE')
SUMS = 'SHA256SUMS'
# 规范形式的公开版本：没有 epoch、没有本地标签（PyPI 不收）、没有前导零、分隔符已归一
# （1.0.0rc1 而不是 1.0.0-rc1）。
_CANONICAL_VERSION = re.compile(
    r'(0|[1-9]\d*)(\.(0|[1-9]\d*))*((a|b|rc)(0|[1-9]\d*))?(\.post(0|[1-9]\d*))?(\.dev(0|[1-9]\d*))?'
)
_PYTHON_CLASSIFIER = re.compile(r'Programming Language :: Python :: 3\.(\d+)')


class CheckError(Exception):
    pass


@dataclass(frozen=True)
class Problem:
    expected: str
    received: str
    next_action: str

    def render(self) -> str:
        return f'期望: {self.expected}\n实际: {self.received}\n下一步: {self.next_action}'


@dataclass(frozen=True)
class Artifact:
    path: Path
    files: dict[str, bytes]
    duplicates: tuple[str, ...] = ()


class _EntryPoints(configparser.ConfigParser):
    def optionxform(self, optionstr: str) -> str:
        return optionstr


_REBUILD = '删掉 dist/ 后在干净的工作区里重新 uv build'


def _project(root: Path) -> dict[str, object]:
    try:
        data = tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8'))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise CheckError(
            f'期望 {root / "pyproject.toml"} 是可读的 TOML，实际读不了：{exc}。'
            ' 从仓库根目录运行，或用 --root 指向仓库根目录。'
        ) from exc
    project = data.get('project')
    if not isinstance(project, dict):
        raise CheckError('期望 pyproject.toml 有 [project] 表，实际没有。从仓库根目录运行。')
    return project


def _tracked_sources(root: Path) -> dict[str, bytes]:
    """git 里 src/syndica 下的文件（去掉 `src/` 前缀）→ 工作区里的字节。"""
    try:
        completed = subprocess.run(
            ['git', 'ls-files', '-z', '--cached', '--', 'src/syndica'],
            cwd=root,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise CheckError(
            f'期望能运行 git 列出 src/syndica，实际起不来：{exc}。装好 git 后再跑。'
        ) from exc
    if completed.returncode != 0:
        raise CheckError(
            f'期望 {root} 是 git 工作区，实际 git ls-files 失败：'
            f'{completed.stderr.decode(errors="replace").strip()}。在仓库根目录运行。'
        )
    names = [name for name in completed.stdout.decode('utf-8').split('\0') if name]
    sources: dict[str, bytes] = {}
    for name in names:
        path = root / name
        if path.is_file():
            sources[name.removeprefix('src/')] = path.read_bytes()
    return sources


def _read_wheel(path: Path) -> Artifact:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        files = {name: archive.read(name) for name in names if not name.endswith('/')}
    duplicates = tuple(sorted({name for name in names if names.count(name) > 1}))
    return Artifact(path, files, duplicates)


def _read_sdist(path: Path) -> tuple[Artifact, list[str]]:
    """(文件, 不是普通文件也不是目录的成员)。"""
    files: dict[str, bytes] = {}
    odd: list[str] = []
    with tarfile.open(path, 'r:gz') as archive:
        for member in archive.getmembers():
            if member.isdir():
                continue
            if not member.isfile():
                odd.append(member.name)
                continue
            extracted = archive.extractfile(member)
            files[member.name] = b'' if extracted is None else extracted.read()
    return Artifact(path, files), odd


def _metadata(raw: bytes) -> Message:
    return HeaderParser().parsestr(raw.decode('utf-8'))


def _metadata_problems(where: str, meta: Message, version: str) -> list[Problem]:
    problems: list[Problem] = []

    def field(name: str, expected: str, fix: str) -> None:
        actual = meta.get(name)
        if actual != expected:
            problems.append(Problem(f'{where} 的 {name} 是 {expected!r}', f'是 {actual!r}', fix))

    field('Name', NAME, '把 pyproject.toml 的 [project] name 改回 syndica')
    field('Version', version, f'{_REBUILD}；dist/ 里的产物不是这一版')
    field(
        'Requires-Python', REQUIRES_PYTHON, f'把 [project] requires-python 改回 "{REQUIRES_PYTHON}"'
    )
    field(
        'License-Expression',
        LICENSE_EXPRESSION,
        f'把 [project] license 写成 SPDX 表达式 "{LICENSE_EXPRESSION}"，不要写成 {{ file = ... }}',
    )
    field('Description-Content-Type', 'text/markdown', '[project] readme 指向 README.md')
    try:
        metadata_version = tuple(
            int(part) for part in (meta.get('Metadata-Version') or '').split('.')
        )
    except ValueError:
        metadata_version = ()
    if metadata_version < (2, 4):
        problems.append(
            Problem(
                f'{where} 的 Metadata-Version 至少是 2.4（License-Expression 从 2.4 起才有）',
                f'是 {meta.get("Metadata-Version")!r}',
                '升级 hatchling（build-system.requires）后重新构建',
            )
        )
    if meta.get('License') is not None:
        problems.append(
            Problem(
                f'{where} 只用 License-Expression 声明许可证',
                '同时还有旧的 License 字段',
                '[project] license 只写 SPDX 表达式，删掉 license 表的写法',
            )
        )
    if 'LICENSE' not in (meta.get_all('License-File') or []):
        problems.append(
            Problem(
                f'{where} 带 License-File: LICENSE',
                f'License-File 是 {meta.get_all("License-File")!r}',
                '在 [project] 里写 license-files = ["LICENSE"]',
            )
        )
    if not (meta.get('Summary') or '').strip():
        problems.append(Problem(f'{where} 有 Summary', '是空的', '填上 [project] description'))

    classifiers = meta.get_all('Classifier') or []
    missing = [name for name in REQUIRED_CLASSIFIERS if name not in classifiers]
    if missing:
        problems.append(
            Problem(
                f'{where} 的 classifiers 包含 {list(REQUIRED_CLASSIFIERS)}',
                f'缺 {missing}',
                '把缺的几条加进 [project] classifiers',
            )
        )
    forbidden = [
        name for name in classifiers if any(name.startswith(p) for p in FORBIDDEN_CLASSIFIERS)
    ]
    if forbidden:
        problems.append(
            Problem(
                f'{where} 不声明以 {list(FORBIDDEN_CLASSIFIERS)} 开头的 classifier'
                '（只支持 Linux 与 macOS；许可证由 SPDX 表达式给出）',
                f'声明了 {forbidden}',
                '从 [project] classifiers 里删掉它们',
            )
        )
    statuses = [name for name in classifiers if name.startswith('Development Status ::')]
    if len(statuses) != 1:
        problems.append(
            Problem(
                f'{where} 恰好有一条 Development Status classifier',
                f'有 {statuses}',
                '在 [project] classifiers 里只留一条 Development Status',
            )
        )
    floor = int(REQUIRES_PYTHON.removeprefix('>=3.'))
    below = [
        name
        for name in classifiers
        if (match := _PYTHON_CLASSIFIER.fullmatch(name)) and int(match.group(1)) < floor
    ]
    if below:
        problems.append(
            Problem(
                f'{where} 声明的 Python 版本不低于 requires-python（{REQUIRES_PYTHON}）',
                f'声明了 {below}',
                '删掉这些 classifier，或者放宽 requires-python 并在那个版本上跑一遍门禁',
            )
        )

    extras = meta.get_all('Provides-Extra') or []
    requirements = meta.get_all('Requires-Dist') or []
    for extra in EXTRAS:
        if extra not in extras:
            problems.append(
                Problem(
                    f'{where} 提供 extra {extra!r}',
                    f'Provides-Extra 是 {extras!r}',
                    f'把 {extra} 加回 [project.optional-dependencies]',
                )
            )
        elif not any(f"extra == '{extra}'" in requirement for requirement in requirements):
            problems.append(
                Problem(
                    f'{where} 的 extra {extra!r} 至少带一个依赖',
                    '它是空的',
                    f'在 [project.optional-dependencies] 的 {extra} 里写上它要装的包',
                )
            )

    urls: dict[str, str] = {}
    for entry in meta.get_all('Project-URL') or []:
        label, _, url = entry.partition(',')
        urls[label.strip()] = url.strip()
    for label in PROJECT_URLS:
        url = urls.get(label)
        if url is None or not url.startswith(URL_PREFIX):
            problems.append(
                Problem(
                    f'{where} 的 Project-URL 有 {label}，指向 {URL_PREFIX} 之下',
                    f'{label} 是 {url!r}',
                    f'在 [project.urls] 里写上 {label}',
                )
            )
    return problems


def _package_problems(
    where: str, files: dict[str, str], sources: dict[str, bytes], fix_extra: str
) -> list[Problem]:
    """`files`：包内相对路径（syndica/...）→ 产物里的成员名。逐个与 git 里的源文件对。"""
    problems: list[Problem] = []
    required_missing = [name for name in REQUIRED_PACKAGE_FILES if name not in files]
    if required_missing:
        problems.append(
            Problem(
                f'{where} 里有 {list(REQUIRED_PACKAGE_FILES)}',
                f'缺 {required_missing}',
                '确认这些文件在 src/syndica 下且已提交，没有被 [tool.hatch.build] 排除；'
                '前端产物缺了先跑 sh scripts/check_web.sh',
            )
        )
    if not any(name.startswith(STATIC_ASSETS) for name in files):
        problems.append(
            Problem(
                f'{where} 里有 {STATIC_ASSETS} 下的前端产物',
                '一个都没有',
                '跑 sh scripts/check_web.sh 重新构建前端，提交 src/syndica/server/static 后'
                '重新 uv build',
            )
        )
    missing = sorted(set(sources) - set(files) - set(required_missing))
    if missing:
        problems.append(
            Problem(
                f'{where} 装着 git 里 src/syndica 下的全部 {len(sources)} 个文件',
                f'缺 {len(missing)} 个：{_sample(missing)}',
                '确认它们没有被 .gitignore 或 [tool.hatch.build] 排除，然后' + _REBUILD,
            )
        )
    extra = sorted(set(files) - set(sources))
    if extra:
        problems.append(
            Problem(
                f'{where} 只装 git 里 src/syndica 下的文件',
                f'多了 {len(extra)} 个：{_sample(extra)}',
                fix_extra,
            )
        )
    return problems


def _sample(names: list[str], limit: int = 8) -> str:
    shown = ', '.join(names[:limit])
    return shown if len(names) <= limit else f'{shown} ……（另有 {len(names) - limit} 个）'


def _changed(where: str, pairs: Iterable[tuple[str, bytes, bytes]]) -> list[Problem]:
    changed = sorted(name for name, built, source in pairs if built != source)
    if not changed:
        return []
    return [
        Problem(
            f'{where} 里的文件与工作区里的逐字节相同',
            f'{len(changed)} 个不同：{_sample(changed)}',
            f'构建之后改过源码，或 dist/ 是旧的构建：{_REBUILD}',
        )
    ]


def check_wheel(
    wheel: Artifact, version: str, sources: dict[str, bytes], root: Path
) -> list[Problem]:
    where = wheel.path.name
    dist_info = f'{NAME}-{version}.dist-info/'
    problems: list[Problem] = []
    if wheel.duplicates:
        problems.append(
            Problem(
                f'{where} 里每个成员只出现一次',
                f'重复的成员：{_sample(list(wheel.duplicates))}',
                _REBUILD,
            )
        )
    outside = sorted(
        name
        for name in wheel.files
        if not (name.startswith(f'{NAME}/') or name.startswith(dist_info))
        or '..' in name.split('/')
    )
    if outside:
        problems.append(
            Problem(
                f'{where} 只有 {NAME}/ 与 {dist_info} 两个顶层目录（不带 tests、docs、web）',
                f'多了 {_sample(outside)}',
                '[tool.hatch.build.targets.wheel] 只写 packages = ["src/syndica"]，然后' + _REBUILD,
            )
        )
    package = {name: name for name in wheel.files if name.startswith(f'{NAME}/')}
    problems += _package_problems(
        where,
        package,
        sources,
        '这些文件在工作区里但没提交（或是 __pycache__ 之类的缓存）：提交或删掉它们，然后'
        + _REBUILD,
    )
    problems += _changed(
        where, ((name, wheel.files[name], sources[name]) for name in package if name in sources)
    )

    info = {
        name.removeprefix(dist_info): data
        for name, data in wheel.files.items()
        if name.startswith(dist_info)
    }
    absent = [name for name in DIST_INFO_FILES if name not in info]
    surplus = sorted(set(info) - set(DIST_INFO_FILES))
    if absent or surplus:
        problems.append(
            Problem(
                f'{where} 的 {dist_info} 恰好有 {list(DIST_INFO_FILES)}',
                f'缺 {absent}，多 {surplus}',
                '检查 [project] 的 license-files 与 [project.scripts]，然后' + _REBUILD,
            )
        )
    if 'licenses/LICENSE' in info and info['licenses/LICENSE'] != (root / 'LICENSE').read_bytes():
        problems.append(Problem(f'{where} 里的 LICENSE 与仓库里的相同', '内容不同', _REBUILD))
    if 'WHEEL' in info:
        wheel_meta = _metadata(info['WHEEL'])
        tags = wheel_meta.get_all('Tag') or []
        if tags != ['py3-none-any'] or wheel_meta.get('Root-Is-Purelib') != 'true':
            problems.append(
                Problem(
                    f'{where} 是纯 Python wheel（Tag: py3-none-any，Root-Is-Purelib: true）',
                    f'Tag 是 {tags}，Root-Is-Purelib 是 {wheel_meta.get("Root-Is-Purelib")!r}',
                    '不要往包里加编译产物或平台相关的构建钩子',
                )
            )
    if 'entry_points.txt' in info:
        parser = _EntryPoints(interpolation=None)
        parser.read_string(info['entry_points.txt'].decode('utf-8'))
        scripts = dict(parser['console_scripts']) if parser.has_section('console_scripts') else {}
        if scripts != dict([ENTRY_POINT]):
            problems.append(
                Problem(
                    f'{where} 的 console_scripts 恰好是 {ENTRY_POINT[0]} = {ENTRY_POINT[1]}',
                    f'是 {scripts!r}',
                    f'[project.scripts] 里只写 {ENTRY_POINT[0]} = "{ENTRY_POINT[1]}"',
                )
            )
    if 'METADATA' in info:
        problems += _metadata_problems(where, _metadata(info['METADATA']), version)
    return problems


def check_sdist(
    sdist: Artifact, odd: list[str], version: str, sources: dict[str, bytes], root: Path
) -> list[Problem]:
    where = sdist.path.name
    prefix = f'{NAME}-{version}/'
    problems: list[Problem] = []
    if odd:
        problems.append(
            Problem(
                f'{where} 里只有普通文件与目录',
                f'有符号链接或特殊文件：{_sample(sorted(odd))}',
                '把它们换成普通文件或从 src/ 里删掉，然后' + _REBUILD,
            )
        )
    stray = sorted(name for name in sdist.files if not name.startswith(prefix))
    if stray:
        problems.append(
            Problem(f'{where} 的成员都在 {prefix} 之下', f'有 {_sample(stray)}', _REBUILD)
        )
    inner = {
        name.removeprefix(prefix): data
        for name, data in sdist.files.items()
        if name.startswith(prefix)
    }
    top = sorted({name.split('/', 1)[0] for name in inner})
    absent = [name for name in SDIST_TOP_LEVEL if name not in top]
    surplus = [name for name in top if name not in SDIST_TOP_LEVEL]
    if absent:
        problems.append(
            Problem(
                f'{where} 有重建 wheel 要的 {list(SDIST_TOP_LEVEL)}',
                f'缺 {absent}',
                '把缺的写进 [tool.hatch.build.targets.sdist] only-include，然后' + _REBUILD,
            )
        )
    if surplus:
        problems.append(
            Problem(
                f'{where} 只有 {list(SDIST_TOP_LEVEL)}（不带 tests、docs、web、scripts）',
                f'多了 {surplus}',
                '从 [tool.hatch.build.targets.sdist] only-include 里删掉它们，然后' + _REBUILD,
            )
        )
    package = {name.removeprefix('src/'): name for name in inner if name.startswith(f'src/{NAME}/')}
    others = sorted(
        name for name in inner if name.startswith('src/') and not name.startswith(f'src/{NAME}/')
    )
    if others:
        problems.append(
            Problem(f'{where} 的 src/ 下只有 {NAME}/', f'还有 {_sample(others)}', _REBUILD)
        )
    problems += _package_problems(
        f'{where} 的 src/',
        package,
        sources,
        '这些文件在工作区里但没提交：提交或删掉它们，然后' + _REBUILD,
    )
    pairs = [
        (f'src/{name}', inner[f'src/{name}'], sources[name]) for name in package if name in sources
    ]
    pairs += [
        (name, inner[name], (root / name).read_bytes()) for name in SDIST_COPIES if name in inner
    ]
    problems += _changed(where, pairs)
    if 'PKG-INFO' in inner:
        problems += _metadata_problems(where, _metadata(inner['PKG-INFO']), version)
    return problems


def _same_metadata(wheel: Artifact, sdist: Artifact, version: str) -> list[Problem]:
    wheel_raw = wheel.files.get(f'{NAME}-{version}.dist-info/METADATA')
    sdist_raw = sdist.files.get(f'{NAME}-{version}/PKG-INFO')
    if wheel_raw is None or sdist_raw is None:
        return []
    keys = ('Name', 'Version', 'Requires-Python', 'License-Expression', 'Summary')
    lists = ('Classifier', 'Provides-Extra', 'Requires-Dist', 'Project-URL')
    left, right = _metadata(wheel_raw), _metadata(sdist_raw)
    differ = [key for key in keys if left.get(key) != right.get(key)]
    differ += [
        key for key in lists if sorted(left.get_all(key) or []) != sorted(right.get_all(key) or [])
    ]
    if not differ:
        return []
    return [
        Problem(
            'wheel 的 METADATA 与 sdist 的 PKG-INFO 一致',
            f'{differ} 不同',
            f'两者不是同一次构建的产物：{_REBUILD}',
        )
    ]


def _dist_files(dist: Path, version: str) -> tuple[Path | None, Path | None, list[Problem]]:
    wheel_name = f'{NAME}-{version}-py3-none-any.whl'
    sdist_name = f'{NAME}-{version}.tar.gz'
    problems: list[Problem] = []
    present = sorted(path.name for path in dist.iterdir() if not path.name.startswith('.'))
    unexpected = [name for name in present if name not in (wheel_name, sdist_name, SUMS)]
    if unexpected:
        problems.append(
            Problem(
                f'dist/ 里只有这一版（{version}）的 {wheel_name} 与 {sdist_name}',
                f'还有 {unexpected}',
                f'uv publish dist/* 会把它们一起传上去：{_REBUILD}',
            )
        )
    found: list[Path | None] = []
    for name in (wheel_name, sdist_name):
        if name in present:
            found.append(dist / name)
        else:
            found.append(None)
            problems.append(
                Problem(
                    f'dist/ 里有 {name}',
                    f'没有；dist/ 里是 {present}',
                    '在仓库根目录运行 uv build（同时构建 sdist 与从它构建的 wheel）',
                )
            )
    return found[0], found[1], problems


def _sums(paths: Iterable[Path]) -> str:
    lines = [f'{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}' for path in paths]
    return '\n'.join(sorted(lines, key=lambda line: line.split('  ', 1)[1])) + '\n'


def check(root: Path, dist: Path) -> tuple[list[Problem], list[Artifact]]:
    if not dist.is_dir():
        raise CheckError(
            f'期望 {dist} 是 uv build 写出的目录，实际不存在。先在仓库根目录运行 uv build。'
        )
    project = _project(root)
    version = str(project.get('version', ''))
    problems: list[Problem] = []
    if not _CANONICAL_VERSION.fullmatch(version):
        problems.append(
            Problem(
                'pyproject.toml 的 version 是规范的 PEP 440 公开版本（如 1.0.0rc1、1.0.0、1.0.1）',
                f'是 {version!r}',
                '改成规范形式：不带 v 前缀、epoch 与 +本地标签，预发布写成 rc1 而不是 -rc1',
            )
        )
    sources = _tracked_sources(root)
    wheel_path, sdist_path, found = _dist_files(dist, version)
    problems += found
    artifacts: list[Artifact] = []
    wheel = sdist = None
    if wheel_path is not None:
        wheel = _read_wheel(wheel_path)
        artifacts.append(wheel)
        problems += check_wheel(wheel, version, sources, root)
    if sdist_path is not None:
        sdist, odd = _read_sdist(sdist_path)
        artifacts.append(sdist)
        problems += check_sdist(sdist, odd, version, sources, root)
    if wheel is not None and sdist is not None:
        problems += _same_metadata(wheel, sdist, version)
    return problems, artifacts


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding='utf-8', errors='backslashreplace')
    parser = argparse.ArgumentParser(description='检查 wheel 与 sdist，通过后写出 dist/SHA256SUMS')
    parser.add_argument('--root', default='.', help='仓库根目录，默认当前目录')
    parser.add_argument('--dist', help='产物目录，默认 <root>/dist')
    parser.add_argument('--list', action='store_true', help='通过后列出两个产物里的全部文件')
    args = parser.parse_args()
    root = Path(args.root).resolve()
    dist = Path(args.dist).resolve() if args.dist else root / 'dist'
    try:
        problems, artifacts = check(root, dist)
    except CheckError as exc:
        print(f'产物检查无法执行：{exc}', file=sys.stderr)
        return 2
    sums = dist / SUMS
    if problems:
        for problem in problems:
            print(problem.render() + '\n', file=sys.stderr)
        if sums.exists():
            sums.unlink()
            print(f'已删除上一次的 {SUMS}：它对应的不是这批产物。', file=sys.stderr)
        print(f'共 {len(problems)} 处问题。', file=sys.stderr)
        return 1
    sums.write_text(_sums(artifact.path for artifact in artifacts), encoding='utf-8')
    for artifact in artifacts:
        digest = hashlib.sha256(artifact.path.read_bytes()).hexdigest()
        print(f'{artifact.path.name}: {len(artifact.files)} 个文件，sha256 {digest}')
        if args.list:
            for name in sorted(artifact.files):
                print(f'  {name}')
    print(f'check_dist: ok，已写出 {sums}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
