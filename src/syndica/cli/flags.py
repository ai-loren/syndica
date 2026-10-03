"""Flag 的取值：`.syndica.yaml` 的 `flags:` 与命令行的 `--flag` 走同一个校验（M4 D15）。

两处收的东西不同、判的规则相同：命令行给的是字符串，先按声明的类型转好；配置里的 YAML 已经
带类型，类型不对就拒（`yes` 会被 YAML 读成 `True`，不能静默当成 1）。之后同一套规则：取值范围、
上下限、CLI 拒收的取值。项目配置另外只收收紧的取值：它可能来自不受信任的人，放宽安全边界要用户
自己在命令行上决定。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence

from syndica.cli.terminal import one_line
from syndica.kernel import ConfigError, FlagDeclaration, FlagRow, FlagValue

__all__ = [
    'FlagValue',
    'flag_details',
    'flag_lines',
    'parse_assignments',
    'refused',
    'render_reference',
    'resolved_flags',
]

# 项目配置只能收紧的 Flag → 它在配置里能取的值。
_CONFIG_TIGHTENS: Mapping[str, frozenset[FlagValue]] = {
    'untrusted_high_risk': frozenset({'reject'}),
}
# 参考文档的「CLI 的限制」一栏：声明之外、CLI 自己加的约束。
_CLI_LIMITS: Mapping[str, str] = {
    'untrusted_high_risk': '不收 allow；项目配置里只收 reject',
    'llm_fallbacks': '只收空值：按名字启用的 retry 没有备用模型',
}


def refused(key: str, value: FlagValue) -> bool:
    """CLI 拒收的取值：`--flag` 与 `flags:` 都报错，`flags show` 与参考文档不把它列成可选值。
    `untrusted_high_risk=allow` 会让 Agent 读过命令输出之后的每一条高风险命令不经询问就执行；
    CLI 按名字启用 `retry` 时不给备用模型，`llm_fallbacks` 点名的模型没有人造得出来。"""
    if key == 'llm_fallbacks':
        return value != ''
    return (key, value) == ('untrusted_high_risk', 'allow')


def _error(expected: str, received: str, next_action: str) -> ConfigError:
    return ConfigError(expected=expected, received=received, next_action=next_action)


def parse_assignments(assignments: Sequence[str]) -> dict[str, str]:
    """命令行的 `--flag key=value` → {key: 原样的字符串}。只查形状，取值等装好 Module 再对着
    声明判。"""
    parsed: dict[str, str] = {}
    for index, assignment in enumerate(assignments):
        key, separator, raw = assignment.partition('=')
        if not separator or not key:
            raise _error(
                '每个 --flag 使用非空 key=value',
                f'第 {index + 1} 项是 {assignment!r}',
                '改成 --flag key=value；用 flags list 查看可用 key',
            )
        if key in parsed:
            raise _error(
                '每个 --flag key 只出现一次',
                f'key {key!r} 重复',
                '删除重复项，每个 key 只保留一个值',
            )
        parsed[key] = raw
    return parsed


def _from_command_line(key: str, raw: str, declaration: FlagDeclaration) -> FlagValue:
    default = declaration.default
    if type(default) is bool:
        if raw not in {'true', 'false'}:
            raise _error(
                f'Flag {key!r} 是 bool，值只能是 true 或 false',
                f'收到 {raw!r}',
                f'改成 --flag {key}=true 或 --flag {key}=false',
            )
        return raw == 'true'
    if type(default) is int:
        try:
            return int(raw)
        except ValueError:
            raise _error(
                f'Flag {key!r} 是 int', f'收到 {raw!r}', f'改成 --flag {key}=<整数>'
            ) from None
    return raw


def _from_config(key: str, raw: object, declaration: FlagDeclaration) -> FlagValue:
    kind = type(declaration.default)
    if type(raw) is not kind:
        raise _error(
            f'flags.{key} 是 {kind.__name__}',
            f'收到 {raw!r}，类型是 {type(raw).__name__}',
            f'照声明的类型写（缺省是 {declaration.default!r}）；YAML 的 yes / on 会被读成布尔值',
        )
    return raw  # pyright: ignore[reportReturnType]


def _checked(key: str, value: FlagValue, declaration: FlagDeclaration, where: str) -> FlagValue:
    if declaration.choices and value not in declaration.choices:
        raise _error(
            f'Flag {key!r} 是 {list(declaration.choices)!r} 之一',
            f'{where} 给的是 {value!r}',
            '改成声明列出的值之一',
        )
    if refused(key, value) and key == 'llm_fallbacks':
        raise _error(
            'CLI 的 llm_fallbacks 是空的：按名字启用的 retry 没有备用模型',
            f'{where} 给的是 {value!r}',
            '删掉这一项；要换一家模型就把角色的 model 写成 <平台>:<型号>',
        )
    if refused(key, value):
        raise _error(
            f'CLI 的 {key} 不取 {value!r}',
            f'{where} 给的是 {key}={value}',
            (
                '改用 --flag untrusted_high_risk=ask（每次问你），或省略它（默认 reject）。'
                'allow 会让 Agent 读过命令输出之后的每一条高风险命令不经询问就在你的机器上执行'
            ),
        )
    if type(value) is int and declaration.minimum is not None and value < declaration.minimum:
        raise _error(
            f'Flag {key!r} 大于或等于 {declaration.minimum}',
            f'{where} 给的是 {value}',
            '提高这个值',
        )
    if type(value) is int and declaration.maximum is not None and value > declaration.maximum:
        raise _error(
            f'Flag {key!r} 小于或等于 {declaration.maximum}',
            f'{where} 给的是 {value}',
            '降低这个值',
        )
    return value


def resolved_flags(
    configured: Mapping[str, object],
    command_line: Mapping[str, str],
    declarations: Iterable[FlagDeclaration],
) -> dict[str, FlagValue]:
    """Runtime 级的覆盖：声明的缺省 < `flags:` < `--flag`。key 必须是已装 Module 声明过的。"""
    declared = {item.key: item for item in declarations}
    resolved: dict[str, FlagValue] = {}
    for source, entries in (('flags:', configured), ('--flag', command_line)):
        for key, raw in entries.items():
            declaration = declared.get(key)
            if declaration is None:
                raise _error(
                    f'{source} 的 key 是已装 Module 声明的 Flag 之一：{sorted(declared)!r}',
                    f'收到未知 key {key!r}',
                    '运行 syndica flags list 查看可用 key；'
                    '第三方 Module 的 Flag 要它在 modules 里启用',
                )
            if source == 'flags:':
                value = _checked(key, _from_config(key, raw, declaration), declaration, 'flags:')
                allowed = _CONFIG_TIGHTENS.get(key)
                if allowed is not None and value not in allowed:
                    raise _error(
                        f'flags.{key} 只能取 {sorted(map(str, allowed))}：项目配置只能收紧',
                        f'收到 {value!r}',
                        f'删掉 flags.{key}；要放宽就由你自己在命令行上用 --flag {key}=...',
                    )
            else:
                value = _checked(
                    key, _from_command_line(key, str(raw), declaration), declaration, '--flag'
                )
            resolved[key] = value
    return resolved


def _shown(value: FlagValue) -> str:
    return json.dumps(value, ensure_ascii=False)


def _choices(row: FlagRow) -> list[FlagValue]:
    return [choice for choice in row.choices if not refused(row.key, choice)]


def _range(row: FlagRow) -> str:
    if row.minimum is not None and row.maximum is not None:
        return f'{row.minimum} 到 {row.maximum}'
    if row.minimum is not None:
        return f'≥ {row.minimum}'
    if row.maximum is not None:
        return f'≤ {row.maximum}'
    return ''


def flag_lines(rows: Sequence[FlagRow]) -> str:
    """`flags list`：每行 key、所属 Module、类型、缺省，制表符分隔。第三方写的字符串过
    `one_line`。"""
    return '\n'.join(
        '\t'.join(one_line(part) for part in (row.key, row.module, row.kind, _shown(row.default)))
        for row in rows
    )


def flag_details(row: FlagRow, effective: FlagValue, source: str) -> dict[str, object]:
    """`flags show` 的一条：声明加上这份配置下的生效值与它的来源。"""
    return {
        'key': row.key,
        'module': row.module,
        'type': row.kind,
        'default': row.default,
        'effective': effective,
        'source': source,
        'description': row.description,
        'choices': _choices(row),
        'minimum': row.minimum,
        'maximum': row.maximum,
    }


def _cell(text: str) -> str:
    return text.replace('|', '\\|')


def render_reference(rows: Sequence[FlagRow]) -> str:
    """`docs/reference/flags.md` 的全文。只由内置 Module 的声明生成：参考文档不随谁装了什么而变。"""
    lines = [
        '# Flag 参考',
        '',
        '<!-- 由 scripts/gen_flag_reference.py 从内置 Module 的声明生成；'
        '改声明之后重新生成，不要手改。 -->',
        '',
        '内置 Module 声明的全部 Flag，按 Module 的装配次序、再按各自声明的次序。',
        '`.syndica.yaml` 的 `flags:` 与命令行的 `--flag key=value` 都按这里的类型与取值校验，',
        '命令行覆盖配置；项目配置只能收紧。第三方 Module 的 Flag 用 `syndica flags list` 查看',
        '（要先确认配置）。',
        '',
        '| key | Module | 类型 | 缺省 | 取值 | CLI 的限制 | 说明 |',
        '| --- | --- | --- | --- | --- | --- | --- |',
    ]
    for row in rows:
        choices = ' / '.join(f'`{_shown(choice)}`' for choice in _choices(row))
        lines.append(
            '| '
            + ' | '.join(
                (
                    f'`{row.key}`',
                    f'`{row.module}`',
                    row.kind,
                    f'`{_shown(row.default)}`',
                    choices or _range(row) or '—',
                    _CLI_LIMITS.get(row.key, '—'),
                    _cell(row.description),
                )
            )
            + ' |'
        )
    return '\n'.join(lines) + '\n'
