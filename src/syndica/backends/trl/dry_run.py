"""用 TRL 读一份 SFT jsonl，并建一个不下载模型的 SFTTrainer。

`trl`、`torch`、`transformers` 只在函数体内导入。
没装 `syndica[trl]` 时，导入本模块不得把它们拖进来。

TRL 1.15 的训练步走 fused log-prob，那个核在导入时要 `triton`。PyTorch 只在 Linux 上带 triton，
所以这里的 dry-run 停在训练器把数据集分词并标好 label 之后，不调用 `train()`。
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any, cast

from syndica.kernel import ConfigError

__all__ = ['dry_run']


def dry_run(dataset: Path) -> dict[str, object]:
    """读 `dataset`，用一个本地小模型建 SFTTrainer。返回行数和分词后的列。"""
    try:
        datasets = cast(Any, importlib.import_module('datasets'))
        trl = cast(Any, importlib.import_module('trl'))
        transformers = cast(Any, importlib.import_module('transformers'))
        tokenizers = cast(Any, importlib.import_module('tokenizers'))
    except ImportError as exc:
        raise ConfigError(
            expected='装了 syndica[trl]（trl、datasets、transformers）',
            received=f'导入失败：{type(exc).__name__}',
            next_action="pip install 'syndica[trl]' 之后再跑 dry_run",
        ) from exc
    if not isinstance(cast(object, dataset), Path) or not dataset.is_file():
        raise ConfigError(
            expected='dataset 是一份 SFT messages jsonl 文件',
            received=f'收到 {dataset!r}',
            next_action='先 syndica export sft --output <目录>，再把其中的 messages.jsonl 传进来',
        )
    loaded = datasets.load_dataset('json', data_files=str(dataset), split='train')
    config = trl.SFTConfig(
        output_dir=str(dataset.parent / 'trl-dry-run'),
        max_steps=0,
        report_to='none',
        bf16=False,
        fp16=False,
        gradient_checkpointing=False,
        dataloader_pin_memory=False,
    )
    model, processing = _local_model(transformers, tokenizers)
    trainer = trl.SFTTrainer(
        model=model,
        args=config,
        train_dataset=loaded,
        processing_class=processing,
    )
    prepared = trainer.train_dataset
    return {
        'rows': len(loaded),
        'max_steps': config.max_steps,
        'columns': list(loaded.column_names),
        'prepared_columns': list(prepared.column_names),
    }


def _local_model(transformers: Any, tokenizers: Any) -> tuple[Any, Any]:
    """不访问 Hub。词表只有特殊符，正文都落到 unk，够把 messages 列走完分词。"""
    vocab = {'<pad>': 0, '<eos>': 1}
    raw = tokenizers.Tokenizer(tokenizers.models.WordLevel(vocab, unk_token='<pad>'))
    raw.pre_tokenizer = tokenizers.pre_tokenizers.Whitespace()
    processing = transformers.PreTrainedTokenizerFast(
        tokenizer_object=raw, pad_token='<pad>', eos_token='<eos>'
    )
    processing.chat_template = (
        "{% for m in messages %}{{ m['content'] }} {{ eos_token }}{% endfor %}"
    )
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
    return transformers.GPT2LMHeadModel(model_config), processing
