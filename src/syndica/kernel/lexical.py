"""零 LLM 的词面相关度：目标导向型压缩挑要留的旧轮次、漂移的锚点检查判「锚不锚得回去」都用它。

只是近似：认不出同义改写，也认不出「锚得上却方式错了」——后者正是漂移检测第二层（一次语义比对）存在的
理由（spec §8.2）。拉丁字母与数字按词切（至少 3 个字符，路径按分隔符拆开），汉字按相邻两字切，这样中
文要求与中文动作也有可比的单位。"""

from __future__ import annotations

import re
from collections.abc import Iterable

__all__ = ['overlap', 'terms']

# 拉丁字母含带重音的（Größe、café）；汉字、假名、谚文按相邻两字切。
_WORD = re.compile(r'[A-Za-z0-9_\u00c0-\u024f]{3,}')
_CJK = re.compile(r'[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]+')
# 几乎出现在任何动作里、不带指向的词：留着它们，什么都「锚得上」。
_STOP = frozenset(
    {
        'the',
        'and',
        'for',
        'with',
        'this',
        'that',
        'from',
        'into',
        'true',
        'false',
        'none',
        'null',
        'path',
        'content',
        'command',
        'file',
        'text',
        'query',
        'agent',
        'isolation',
        'pattern',
    }
)


def terms(text: str) -> frozenset[str]:
    """`text` 的词面单位：小写的拉丁词（≥ 3 字符、去掉无指向的常用词）与汉字二元组。"""
    # 纯数字（行号、计数、年份）几乎和什么都能撞上，不带指向。
    found = {word.lower() for word in _WORD.findall(text) if not word.isdigit()} - _STOP
    for run in _CJK.findall(text):
        if len(run) == 1:
            found.add(run)
        found.update(run[i : i + 2] for i in range(len(run) - 1))
    return frozenset(found)


def overlap(text: str, vocabulary: Iterable[str]) -> int:
    """`text` 与 `vocabulary`（已经是 `terms` 的结果）共有的单位数。"""
    return len(terms(text) & frozenset(vocabulary))
