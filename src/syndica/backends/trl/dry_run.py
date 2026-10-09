"""用 TRL 读导出的 SFT 与偏好 jsonl，建不下载模型的 SFTTrainer 与 DPOTrainer。

`trl`、`torch`、`transformers` 只在函数体内导入。
没装 `syndica[trl]` 时，导入本模块不得把它们拖进来。

TRL 1.15 的训练步走 fused log-prob，那个核在导入时要 `triton`。PyTorch 只在 Linux 上带 triton，
所以这里的 dry-run 停在训练器把数据集准备好之后（SFT 分词并建好 `labels`，DPO 把 prompt / chosen /
rejected 各自分词），不调用 `train()`。
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any, cast

from syndica.kernel import ConfigError

__all__ = ['dry_run', 'dry_run_preference']

_EXPORTS = {
    'sft': (
        'messages.jsonl',
        '看同目录 manifest.json 的 dropped_unverified / dropped_secret / failed；'
        '全是 unverified 时加 --include-unverified 重新导出',
    ),
    'preference': (
        'preference.jsonl',
        '偏好对只在同一 Goal、同一份 Rubric 下裁决不同的两次尝试之间配；'
        '看同目录 manifest.json 的 preference_pairs 与 sources，补跑同一 Goal 的尝试后重新导出',
    ),
}


def dry_run(dataset: Path) -> dict[str, object]:
    """读 SFT `dataset`，用一个本地小模型建 SFTTrainer。返回行数、准备前后的列与每行 token 数。"""
    datasets, trl, transformers, tokenizers = _modules('dry_run')
    loaded = _load(datasets, dataset, 'sft')
    config = trl.SFTConfig(**_args(dataset.parent / 'trl-dry-run'))
    trainer = trl.SFTTrainer(
        model=_local_model(transformers),
        args=config,
        train_dataset=loaded,
        processing_class=_local_tokenizer(transformers, tokenizers),
    )
    prepared = trainer.train_dataset
    return {
        'rows': len(loaded),
        'max_steps': config.max_steps,
        'columns': list(loaded.column_names),
        'prepared_columns': list(prepared.column_names),
        'token_counts': [len(row['input_ids']) for row in prepared],
    }


def dry_run_preference(dataset: Path) -> dict[str, object]:
    """读偏好 `dataset`，用一个本地小模型建 DPOTrainer。

    返回行数、准备前后的列，以及每行 prompt / chosen / rejected 各自的 token 数。
    """
    datasets, trl, transformers, tokenizers = _modules('dry_run_preference')
    loaded = _load(datasets, dataset, 'preference')
    config = trl.DPOConfig(**_args(dataset.parent / 'trl-dpo-dry-run'))
    trainer = trl.DPOTrainer(
        model=_local_model(transformers),
        # 不给参考模型时 TRL 按模型 config 里的路径另载一份；本地建的模型路径为空，
        # 构造当场 OSError。
        ref_model=_local_model(transformers),
        args=config,
        train_dataset=loaded,
        processing_class=_local_tokenizer(transformers, tokenizers),
    )
    prepared = trainer.train_dataset
    return {
        'rows': len(loaded),
        'max_steps': config.max_steps,
        'columns': list(loaded.column_names),
        'prepared_columns': list(prepared.column_names),
        'prompt_token_counts': [len(row['prompt_ids']) for row in prepared],
        'chosen_token_counts': [len(row['chosen_ids']) for row in prepared],
        'rejected_token_counts': [len(row['rejected_ids']) for row in prepared],
    }


def _modules(caller: str) -> tuple[Any, Any, Any, Any]:
    try:
        return (
            cast(Any, importlib.import_module('datasets')),
            cast(Any, importlib.import_module('trl')),
            cast(Any, importlib.import_module('transformers')),
            cast(Any, importlib.import_module('tokenizers')),
        )
    except ImportError as exc:
        raise ConfigError(
            expected='装了 syndica[trl]（trl、datasets、transformers）',
            received=f'导入失败：{type(exc).__name__}',
            next_action=f"pip install 'syndica[trl]' 之后再跑 {caller}",
        ) from exc


def _load(datasets: Any, dataset: Path, fmt: str) -> Any:
    name, empty_hint = _EXPORTS[fmt]
    if not isinstance(cast(object, dataset), Path) or not dataset.is_file():
        raise ConfigError(
            expected=f'dataset 是 syndica export {fmt} 写出的 {name}',
            received=f'收到 {dataset!r}',
            next_action=f'先 syndica export {fmt} --output <目录>，再把其中的 {name} 传进来',
        )
    # 空文件交给 datasets 只换来一个没有消息的 StopIteration。
    with dataset.open(encoding='utf-8') as handle:
        empty = not any(line.strip() for line in handle)
    if empty:
        raise ConfigError(
            expected=f'{name} 里至少有一行',
            received=f'{dataset} 没有任何一行',
            next_action=empty_hint,
        )
    return datasets.load_dataset('json', data_files=str(dataset), split='train')


def _args(output_dir: Path) -> dict[str, object]:
    return {
        'output_dir': str(output_dir),
        'max_steps': 0,
        'report_to': 'none',
        'bf16': False,
        'fp16': False,
        'gradient_checkpointing': False,
        'dataloader_pin_memory': False,
    }


def _local_tokenizer(transformers: Any, tokenizers: Any) -> Any:
    """不访问 Hub。词表只有特殊符，正文都落到 unk，够把对话列走完分词。"""
    vocab = {'<pad>': 0, '<eos>': 1}
    raw = tokenizers.Tokenizer(tokenizers.models.WordLevel(vocab, unk_token='<pad>'))
    raw.pre_tokenizer = tokenizers.pre_tokenizers.Whitespace()
    processing = transformers.PreTrainedTokenizerFast(
        tokenizer_object=raw, pad_token='<pad>', eos_token='<eos>'
    )
    # 工具调用也渲染进去：导出的 SFT 行与偏好行里 assistant 带 tool_calls，
    # 模板丢掉它们就等于没测工具调用。
    processing.chat_template = (
        "{% for m in messages %}{{ m['content'] }}"
        "{% if m.get('tool_calls') %} {{ m['tool_calls'] | tojson }}{% endif %}"
        ' {{ eos_token }}{% endfor %}'
    )
    return processing


def _local_model(transformers: Any) -> Any:
    model_config = transformers.GPT2Config(
        vocab_size=8,
        n_positions=32,
        n_embd=32,
        n_layer=1,
        n_head=1,
        bos_token_id=1,
        eos_token_id=1,
        pad_token_id=0,
    )
    return transformers.GPT2LMHeadModel(model_config)
