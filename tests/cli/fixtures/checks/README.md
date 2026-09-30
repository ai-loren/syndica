# 检查结果样例

`tests/cli/test_checks.py` 用的输入：真实工具经 `LocalWorkspace.run`（Agent 的 Bash 走的同一条路，
`/bin/sh`）跑出的 `(command, exit_code, stdout, stderr, timed_out)`。样例是数据文件，不带 `.py` 后缀，
ruff、pyright 与 pytest 都不收它们。

## 重新生成

```bash
uv run python tests/cli/fixtures/checks/generate.py
```

`generate.py` 在临时目录里造几个小项目（通过、失败、收集错误、没有测试、超时、依赖方向违规），
逐条跑它 `_CASES` 里的命令，覆盖本目录的全部 JSON。离线可跑：`uv run` 用 `VIRTUAL_ENV` 指向的仓库
虚拟环境，不建项目、不装依赖；pyright-python 查新版本的提示用 `PYRIGHT_PYTHON_IGNORE_WARNINGS` 关掉。
落盘前仓库路径、临时目录与家目录换成 `/repo`、`/work`、`/home/user`，用时换成固定的 `0.01s`；
替换之后仍留着本机路径时拒绝落盘。重新生成两次，样例逐字节相同。

工具升级后汇总行的写法可能变，重新生成、再看 `test_checks.py` 的期望表是否要跟着改——这张表是人对
每个样例「应该解析成什么」的判断，不从解析器的输出反推。

## 生成时的版本

2026-10-01，macOS 26.6（arm64）：

| 工具 | 版本 |
|---|---|
| uv | 0.12.19 |
| Python | 3.11.16 |
| pytest | 9.1.1 |
| ruff | 0.16.6 |
| pyright | 1.1.411 |
