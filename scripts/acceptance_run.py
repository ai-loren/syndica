"""发布验收：从构建出的 wheel 跑路线图 M12 的四项 E2E，逐项打 PASS / FAIL 与用时。

由 scripts/acceptance.sh 调用。

- (a) README 五分钟：干净的 Linux 容器（缺省 python:3.12-slim，只拷进 wheel 与假端点），照 README
  的安装与上手命令跑到一次成功的 `syndica run`（stdout 有回答、有 trace），从容器启动计时，不超过
  300 秒。README 里的命令怎么取、换了什么，见 scripts/acceptance_readme.py。
- (b) Web UI：干净 venv 装 `wheel[openai,server,mcp]`，在 examples/web-multi-agent 的工作区上
  `syndica up`；Playwright（web/e2e/acceptance）驱动页面：Orchestrator 派活给 Worker、页面上批准
  一次确认、打开产物预览；批准的那次调用真的执行了。最后 `syndica down`。
- (c) MCP / OpenAI SDK：对 (b) 那个 `syndica up --api-token`，只装了 SDK 的客户端 venv 跑
  scripts/acceptance_clients.py。
- (d) 批量与导出：干净 venv 装 `wheel[openai]`，examples/training-export 的四个任务（剧本让它们
  分别得 pass / block / partial / unverified），`syndica export` 四种格式到工作区之外，由
  scripts/acceptance_export.py 核对；环境里放一个密钥，剧本让模型把它说出来。

模型是 tests/e2e/fake_openai.py 的拷贝：它是测试夹具，不属于产品，所以不从 wheel 里来。环境里有
OPENAI_API_KEY、OPENAI_BASE_URL 与 OPENAI_MODEL（或 GEMINI_API_KEY）时，(a) 与 (d) 再对真模型各跑
一次并报告；key 只经环境变量与 stdin 传给子进程，不写进任何文件或日志。

只用标准库。产物与日志留在 `--out` 指定（缺省新建的临时）目录里，结束时打出路径。
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIVE_MINUTES = 300.0
_DUMMY_KEY = 'acceptance-dummy-key-0123456789'
_URL = re.compile(r'http://127\.0\.0\.1:(\d+)/#login=([A-Za-z0-9_-]+)')
_TOKEN = re.compile(r'^[A-Za-z0-9_-]{40,}$', re.M)
_DIGEST = re.compile(r'--sha256 ([0-9a-f]{64})')
_VERDICT = re.compile(r'验收：(pass|partial|block|unverified)')
_REAL_OPENAI = ('OPENAI_API_KEY', 'OPENAI_BASE_URL', 'OPENAI_MODEL')
_REAL_GEMINI = ('GEMINI_API_KEY',)


class Failed(Exception):
    pass


def check(condition: bool, expected: str, received: object, next_action: str) -> None:
    if not condition:
        raise Failed(f'期望: {expected}\n实际: {received}\n下一步: {next_action}')


@dataclass
class Run:
    out: Path
    wheel: Path
    fake: Path
    python: str
    image: str
    real: dict[str, str] | None
    log: list[str] = field(default_factory=list[str])

    def note(self, text: str) -> None:
        print(f'      {text}', flush=True)
        self.log.append(text)


def sh(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    stdin: str | None = None,
    timeout: float = 600,
    log: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=None if env is None else dict(env),
        input=stdin,
        stdin=None if stdin is not None else subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if log is not None:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open('a', encoding='utf-8') as stream:
            stream.write(f'$ {" ".join(command)}\n[exit {completed.returncode}]\n')
            stream.write(completed.stdout)
            stream.write(completed.stderr)
            stream.write('\n')
    return completed


def must(completed: subprocess.CompletedProcess[str], what: str, next_action: str) -> str:
    said = (completed.stdout + completed.stderr).strip()
    check(
        completed.returncode == 0,
        f'{what} 退出 0',
        f'退出 {completed.returncode}：{said[-1500:]}',
        next_action,
    )
    return completed.stdout


# --- 构建 -----------------------------------------------------------------------------------


def build(out: Path, dist: Path | None) -> Path:
    """`uv build` 到私有目录（不碰 dist/），再跑 check_dist；给了 `--dist` 就只检查那份。"""
    if dist is None:
        dist = out / 'dist'
        must(
            sh(['uv', 'build', '--out-dir', str(dist)], cwd=ROOT, log=out / 'build.log'),
            'uv build',
            f'看 {out / "build.log"}',
        )
    must(
        sh(
            [
                sys.executable,
                str(ROOT / 'scripts' / 'check_dist.py'),
                '--root',
                str(ROOT),
                '--dist',
                str(dist),
            ],
            log=out / 'build.log',
        ),
        'scripts/check_dist.py',
        f'看 {out / "build.log"}；产物要从干净的、已提交的工作区构建',
    )
    [wheel] = sorted(dist.glob('syndica-*-py3-none-any.whl'))
    return wheel


# --- 公共装置 -------------------------------------------------------------------------------


def venv(run: Run, path: Path, spec: str) -> Path:
    log = path.parent / 'install.log'
    must(
        sh(['uv', 'venv', '--quiet', '--python', run.python, str(path)], log=log),
        'uv venv',
        f'看 {log}',
    )
    must(
        sh(
            ['uv', 'pip', 'install', '--quiet', '--python', str(path / 'bin' / 'python'), spec],
            log=log,
        ),
        f'uv pip install {spec}',
        f'看 {log}',
    )
    return path / 'bin'


def isolated_env(bin_dir: Path, home: Path, model: Mapping[str, str]) -> dict[str, str]:
    """syndica 子进程只拿到这些：不带调用者的 SYNDICA_ / OPENAI_ / GEMINI_ / XDG_ 变量与 HOME。"""
    home.mkdir(parents=True, exist_ok=True)
    env = {
        'PATH': f'{bin_dir}:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin',
        'HOME': str(home),
        'XDG_CONFIG_HOME': str(home / '.config'),
        'TMPDIR': str(home),
        'LANG': os.environ.get('LANG') or 'en_US.UTF-8',
    }
    env.update(model)
    return env


def fake_model(port: int) -> dict[str, str]:
    return {
        'SYNDICA_LLM_ORDER': 'openai',
        'OPENAI_BASE_URL': f'http://127.0.0.1:{port}/v1',
        'OPENAI_MODEL': 'fake',
        'OPENAI_API_KEY': _DUMMY_KEY,
    }


@contextlib.contextmanager
def fake_endpoint(run: Run, scripts: list[Path], log: Path) -> Iterator[int]:
    env = {**os.environ, 'FAKE_OPENAI_SCRIPT': os.pathsep.join(map(str, scripts))}
    with log.open('w', encoding='utf-8') as errors:
        process = subprocess.Popen(
            [sys.executable, str(run.fake)],
            stdout=subprocess.PIPE,
            stderr=errors,
            stdin=subprocess.DEVNULL,
            text=True,
            env=env,
        )
    try:
        assert process.stdout is not None
        yield int(process.stdout.readline().strip())
    finally:
        process.terminate()
        process.wait(timeout=10)


def trust(syndica: Path, workspace: Path, env: Mapping[str, str], log: Path) -> None:
    probe = sh([str(syndica), 'config', 'trust'], cwd=workspace, env=env, log=log)
    digest = _DIGEST.search(probe.stdout + probe.stderr)
    check(digest is not None, 'config trust 报出要确认的摘要', probe.stderr[-500:], f'看 {log}')
    assert digest is not None
    must(
        sh(
            [str(syndica), 'config', 'trust', '--sha256', digest.group(1)],
            cwd=workspace,
            env=env,
            log=log,
        ),
        'syndica config trust --sha256',
        f'看 {log}',
    )


def real_model() -> dict[str, str] | None:
    if all(os.environ.get(name) for name in _REAL_OPENAI):
        return {'SYNDICA_LLM_ORDER': 'openai', **{name: os.environ[name] for name in _REAL_OPENAI}}
    if all(os.environ.get(name) for name in _REAL_GEMINI):
        found = {'SYNDICA_LLM_ORDER': 'gemini', 'GEMINI_API_KEY': os.environ['GEMINI_API_KEY']}
        if os.environ.get('GEMINI_MODEL'):
            found['GEMINI_MODEL'] = os.environ['GEMINI_MODEL']
        return found
    return None


def scrub(text: str, model: Mapping[str, str] | None) -> str:
    """真模型的 key 不进输出：子进程的输出原样打印之前先换掉。"""
    for name, value in (model or {}).items():
        if 'KEY' in name and value:
            text = text.replace(value, f'[{name}]')
    return text


# --- (a) README 五分钟 -----------------------------------------------------------------------


def check_a(run: Run, model: dict[str, str] | None = None) -> str:
    """model 为 None 时用夹具端点；否则是真模型的环境变量，经 stdin 交进容器。"""
    label = 'a' if model is None else 'a-real'
    here = run.out / label
    accept = here / 'accept'
    accept.mkdir(parents=True)
    shutil.copy2(run.wheel, accept / run.wheel.name)
    names: list[str] = []
    if model is None:
        shutil.copy2(run.fake, accept / 'fake_openai.py')
        shutil.copy2(ROOT / 'scripts' / 'acceptance_quickstart.json', accept / 'quickstart.json')
        planned = sh(
            [
                sys.executable,
                str(ROOT / 'scripts' / 'acceptance_readme.py'),
                str(ROOT / 'README.md'),
                f'/accept/{run.wheel.name}',
                str(accept / 'run.sh'),
            ],
        )
    else:
        names = list(model)
        planned = sh(
            [
                sys.executable,
                str(ROOT / 'scripts' / 'acceptance_readme.py'),
                str(ROOT / 'README.md'),
                f'/accept/{run.wheel.name}',
                str(accept / 'run.sh'),
                '--from-stdin',
                ','.join(names),
            ],
        )
    plan = json.loads(
        must(planned, 'scripts/acceptance_readme.py 从 README 取出命令', '看它的报错')
    )
    (here / 'plan.json').write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    for old, new in plan['substitutions']:
        run.note(f'README install: {old} -> {new}')
    name = f'syndica-acceptance-{secrets.token_hex(4)}'
    pulled = time.monotonic()
    must(
        sh(['docker', 'pull', '--quiet', run.image], timeout=900),
        f'docker pull {run.image}',
        '确认 docker 在跑、能联网',
    )
    run.note(f'image {run.image} ready in {time.monotonic() - pulled:.1f} s (not counted)')
    must(
        sh(
            ['docker', 'create', '--interactive', '--name', name, run.image, 'sh', '/accept/run.sh']
        ),
        'docker create',
        '确认 docker 在跑',
    )
    try:
        must(
            sh(['docker', 'cp', f'{accept}/.', f'{name}:/accept']),
            'docker cp 进容器',
            '确认 docker 在跑',
        )
        stdin = ''.join(f'{model[key]}\n' for key in names) if model is not None else ''
        started = time.monotonic()
        completed = sh(
            ['docker', 'start', '--attach', '--interactive', name], stdin=stdin, timeout=900
        )
        elapsed = time.monotonic() - started
        (here / 'container.log').write_text(
            scrub(completed.stdout + completed.stderr, model), 'utf-8'
        )
        sh(['docker', 'cp', f'{name}:/accept/out', str(here / 'out')])
    finally:
        sh(['docker', 'rm', '--force', name])
    out = here / 'out'
    codes = {n: value.split('\t') for n, value in _tsv(out / 'codes.tsv').items()}
    steps = _tsv(out / 'steps.tsv')
    failed = [steps.get(n, n) for n, (code, _span) in codes.items() if code != '0']
    for n, (code, span) in codes.items():
        began, ended = (float(part) for part in span.split())
        run.note(f'{ended - began:5.1f} s  exit {code}  {steps.get(n, n)}')
    check(
        completed.returncode == 0 and not failed,
        '容器里 README 的每条命令退出 0',
        f'容器退出 {completed.returncode}，失败的命令 {failed}；末尾输出：'
        f'{scrub(completed.stdout + completed.stderr, model)[-1500:]}',
        f'看 {here / "container.log"} 与 {out}',
    )
    run_step = next(n for n, command in steps.items() if command.startswith('syndica run '))
    answer = (out / f'{run_step}.out').read_text('utf-8').strip()
    said = (out / f'{run_step}.err').read_text('utf-8')
    traces = sorted((out / 'workspace' / '.syndica' / 'traces').glob('*/trace.json'))
    check(
        bool(answer) and 'trace：.syndica/traces/' in said and len(traces) == 1,
        'syndica run 在 stdout 给出回答、在 stderr 给出 trace 目录，工作区里有这一个 trace',
        f'回答 {answer[:120]!r}；stderr {said[-300:]!r}；trace {len(traces)} 个',
        f'看 {out}',
    )
    if model is None:
        check(
            (out / 'workspace' / 'poem.md').is_file(),
            '上手页说的 poem.md 写进了工作区',
            sorted(path.name for path in (out / 'workspace').iterdir()),
            '看 scripts/acceptance_quickstart.json 是否命中',
        )
    check(
        elapsed < FIVE_MINUTES,
        f'从容器启动到 README 跑完不超过 {FIVE_MINUTES:.0f} 秒',
        f'{elapsed:.1f} 秒',
        '看 container.log 里哪一步慢（多半是 pip 下载）',
    )
    run.note(f'answer: {scrub(answer, model)[:100]!r}')
    return f'container {elapsed:.1f} s < {FIVE_MINUTES:.0f} s'


def _tsv(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    rows = [line.split('\t', 1) for line in path.read_text('utf-8').splitlines() if '\t' in line]
    return {key: value for key, value in rows}


# --- (b) Web UI 与 (c) SDK ------------------------------------------------------------------


@dataclass
class Server:
    url: str
    login: str
    token: str


def check_bc(run: Run) -> tuple[tuple[bool, str, float], tuple[bool, str, float]]:
    """(b) 与 (c) 共用一个 `syndica up`；各自交回 (通过, 说明, 秒)。"""
    here = run.out / 'b'
    demo = ROOT / 'examples' / 'web-multi-agent'
    started = time.monotonic()
    bin_dir = venv(run, here / 'venv', f'{run.wheel}[openai,server,mcp]')
    syndica = bin_dir / 'syndica'
    workspace = here / 'ws'
    shutil.copytree(demo / 'workspace', workspace)
    (here / 'outbox').mkdir()
    shots = here / 'screenshots'
    shots.mkdir()
    log = here / 'syndica.log'
    scripts = [demo / 'offline' / 'script.json', ROOT / 'scripts' / 'acceptance_interop.json']
    with fake_endpoint(run, scripts, here / 'fake.log') as port:
        env = isolated_env(bin_dir, here / 'home', fake_model(port))
        trust(syndica, workspace, env, log)
        up = must(
            sh([str(syndica), 'up', '--no-open', '--api-token'], cwd=workspace, env=env, log=log),
            'syndica up --no-open --api-token',
            f'看 {log}',
        )
        located, token = _URL.search(up), _TOKEN.search(up)
        check(
            located is not None and token is not None,
            'syndica up 打出登录链接与机器令牌',
            up[-500:],
            f'看 {log}',
        )
        assert located is not None and token is not None
        server = Server(f'http://127.0.0.1:{located.group(1)}', located.group(2), token.group(0))
        run.note(f'syndica up at {server.url} (installed from {run.wheel.name}[openai,server,mcp])')
        try:
            b = _timed(lambda: _browser(run, server, demo, here, shots), started)
            c = _timed(lambda: _clients(run, server), time.monotonic())
        finally:
            must(
                sh([str(syndica), 'down'], cwd=workspace, env=env, log=log),
                'syndica down',
                f'看 {log}',
            )
    return b, c


def _timed(body: Callable[[], str], started: float) -> tuple[bool, str, float]:
    try:
        detail = body()
    except Failed as failure:
        return False, str(failure), time.monotonic() - started
    return True, detail, time.monotonic() - started


def _browser(run: Run, server: Server, demo: Path, here: Path, shots: Path) -> str:
    web = ROOT / 'web'
    if not (web / 'node_modules' / '@playwright' / 'test').is_dir():
        must(
            sh(['npm', 'ci', '--no-audit', '--no-fund'], cwd=web, log=here / 'npm.log'),
            'npm ci',
            '装 Node 26',
        )
    env = {
        **os.environ,
        'ACCEPTANCE_URL': server.url,
        'ACCEPTANCE_LOGIN': server.login,
        'ACCEPTANCE_PROMPT': (demo / 'prompt.txt').read_text('utf-8').strip(),
        'ACCEPTANCE_OUT': str(shots),
    }
    must(
        sh(
            [
                'npx',
                'playwright',
                'test',
                '-c',
                'e2e/acceptance/playwright.config.ts',
                '--reporter=line',
            ],
            cwd=web,
            env=env,
            timeout=600,
            log=here / 'playwright.log',
        ),
        'Playwright 验收用例（web/e2e/acceptance）',
        f'看 {here / "playwright.log"} 与 {shots}/playwright 里的 trace；'
        '浏览器没装就 npx playwright install chromium',
    )
    copied = here / 'outbox' / 'incident.md'
    check(
        copied.is_file(),
        '页面上批准的那次 cp 真的执行了',
        '../outbox/incident.md 不存在',
        f'看 {here}',
    )
    pictures = sorted(path.name for path in shots.glob('*.png'))
    run.note(f'screenshots: {", ".join(pictures)} in {shots}')
    return 'delegated, approved in the page, artifact previewed'


def _clients(run: Run, server: Server) -> str:
    here = run.out / 'c'
    bin_dir = venv(run, here / 'venv', 'openai>=3.26,<4')
    must(
        sh(
            [
                'uv',
                'pip',
                'install',
                '--quiet',
                '--python',
                str(bin_dir / 'python'),
                'mcp>=2.3,<3',
                'httpx>=0.27,<1',
            ],
            log=here / 'install.log',
        ),
        'uv pip install mcp httpx',
        f'看 {here / "install.log"}',
    )
    env = {
        'PATH': f'{bin_dir}:/usr/bin:/bin',
        'HOME': str(here),
        'ACCEPTANCE_URL': server.url,
        'ACCEPTANCE_TOKEN': server.token,
    }
    completed = sh(
        [str(bin_dir / 'python'), str(ROOT / 'scripts' / 'acceptance_clients.py')],
        env=env,
        timeout=300,
        log=here / 'clients.log',
    )
    for line in completed.stdout.splitlines():
        if line.startswith('ok '):
            run.note(line)
    must(completed, 'scripts/acceptance_clients.py', f'看 {here / "clients.log"}')
    return 'openai client tool round trip + MCP list/Read, untrusted where documented'


# --- (d) 批量与导出 -------------------------------------------------------------------------


def check_d(run: Run, model: dict[str, str] | None = None) -> str:
    label = 'd' if model is None else 'd-real'
    here = run.out / label
    demo = ROOT / 'examples' / 'training-export'
    bin_dir = venv(run, here / 'venv', f'{run.wheel}[openai]')
    syndica = bin_dir / 'syndica'
    workspace = here / 'ws'
    shutil.copytree(demo / 'workspace', workspace)
    secret = f'sk-acceptance-quiz-{secrets.token_hex(12)}'
    script = here / 'script.json'
    must(
        sh(
            [
                sys.executable,
                str(ROOT / 'scripts' / 'acceptance_export.py'),
                'prepare',
                str(demo / 'offline' / 'script.json'),
                str(script),
                secret,
            ]
        ),
        '给剧本埋密钥',
        '看 scripts/acceptance_export.py 的报错',
    )
    log = here / 'syndica.log'
    with contextlib.ExitStack() as stack:
        if model is None:
            port = stack.enter_context(fake_endpoint(run, [script], here / 'fake.log'))
            backend = fake_model(port)
        else:
            backend = model
        env = isolated_env(bin_dir, here / 'home', backend)
        env['ACCEPTANCE_DEPLOY_TOKEN'] = secret
        trust(syndica, workspace, env, log)
        batch = sh(
            [str(syndica), 'batch', str(demo / 'tasks.jsonl')], cwd=workspace, env=env, timeout=1800
        )
        (here / 'batch.log').write_text(scrub(batch.stdout + batch.stderr, model), 'utf-8')
        rows = [json.loads(line) for line in batch.stdout.splitlines() if line.startswith('{')]
        verdicts = _VERDICT.findall(batch.stderr)
        check(
            batch.returncode == 0 and len(rows) == 4 and all(row.get('ok') for row in rows),
            'syndica batch 把四个任务都跑完（退出 0，每行 ok）',
            f'退出 {batch.returncode}，结果 {rows}',
            f'看 {here / "batch.log"}',
        )
        if model is None:
            check(
                verdicts == ['pass', 'block', 'partial', 'unverified'],
                '剧本跑出的裁决依次是 pass、block、partial、unverified',
                verdicts,
                f'看 {here / "batch.log"}',
            )
        run.note(f'batch verdicts: {verdicts}')
        exports = here / 'exports'
        for fmt in ('sft', 'preference', 'rl', 'trajectory'):
            must(
                sh(
                    [str(syndica), 'export', fmt, '--output', str(exports / fmt)],
                    cwd=workspace,
                    env=env,
                    log=log,
                ),
                f'syndica export {fmt}',
                f'看 {log}',
            )
    checked = sh(
        [
            sys.executable,
            str(ROOT / 'scripts' / 'acceptance_export.py'),
            'check',
            str(exports),
            secret,
            str(verdicts.count('unverified')),
            *(['--scripted'] if model is None else []),
        ],
    )
    summary = json.loads(must(checked, 'scripts/acceptance_export.py check', f'看 {exports}'))
    for fmt, facts in summary.items():
        run.note(
            f'{fmt}: {facts["rows"]} rows, samples {facts["samples"]}, '
            f'unverified left out {facts["dropped_unverified"]}'
        )
    if model is not None:
        run.note(
            'the secret check is vacuous with a real model: nothing asks it to repeat the secret'
        )
    return f'verdicts {verdicts}; exports outside the workspace; secret absent'


# --- 汇总 ----------------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description='发布验收：四项 E2E，从构建出的 wheel 跑')
    parser.add_argument('--only', default='a,b,c,d', help='只跑这几项（逗号分隔），缺省 a,b,c,d')
    parser.add_argument(
        '--dist', type=Path, help='用这个目录里已构建的产物（照样跑 check_dist），不重新构建'
    )
    parser.add_argument('--out', type=Path, help='产物与日志目录，缺省新建一个临时目录')
    parser.add_argument('--python', default=os.environ.get('ACCEPTANCE_PYTHON', '3.12'))
    parser.add_argument('--image', default=os.environ.get('ACCEPTANCE_IMAGE', 'python:3.12-slim'))
    args = parser.parse_args()
    wanted = set(args.only.split(','))
    unknown = wanted - {'a', 'b', 'c', 'd'}
    if unknown:
        print(
            f'期望: --only 是 a、b、c、d 的组合。实际: {sorted(unknown)}。'
            '下一步: 改成比如 --only a,d',
            file=sys.stderr,
        )
        return 2
    out = args.out or Path(tempfile.mkdtemp(prefix='syndica-acceptance-'))
    out.mkdir(parents=True, exist_ok=True)
    print(f'acceptance: output in {out}', flush=True)
    results: list[tuple[str, bool, str, float]] = []
    began = time.monotonic()
    try:
        wheel = build(out, args.dist)
    except Failed as failure:
        print(f'FAIL build\n{failure}', flush=True)
        return 1
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    print(
        f'wheel: {wheel.name} sha256 {digest} '
        f'(built and checked in {time.monotonic() - began:.1f} s)'
    )
    fake = out / 'fixture' / 'fake_openai.py'
    fake.parent.mkdir()
    shutil.copy2(ROOT / 'tests' / 'e2e' / 'fake_openai.py', fake)
    model = real_model()
    run = Run(out, wheel, fake, args.python, args.image, model)

    def record(key: str, title: str, body: Callable[[], str]) -> None:
        print(f'... ({key}) {title}', flush=True)
        ok, detail, seconds = _timed(body, time.monotonic())
        results.append((key, ok, detail, seconds))
        _report(key, title, ok, detail, seconds)

    if 'a' in wanted:
        record('a', 'new machine from the README in five minutes', lambda: check_a(run))
    if wanted & {'b', 'c'}:
        print(
            '... (b) web UI: multi-agent task, confirmation, artifact  /  (c) MCP and OpenAI SDK',
            flush=True,
        )
        try:
            (b_ok, b_detail, b_seconds), (c_ok, c_detail, c_seconds) = check_bc(run)
        except Failed as failure:
            b_ok = c_ok = False
            b_detail = c_detail = f'syndica up 没能起来：\n{failure}'
            b_seconds = c_seconds = 0.0
        for key, title, ok, detail, seconds in (
            ('b', 'web UI: multi-agent task, confirmation, artifact', b_ok, b_detail, b_seconds),
            ('c', 'MCP and OpenAI SDK against syndica up', c_ok, c_detail, c_seconds),
        ):
            if key in wanted:
                results.append((key, ok, detail, seconds))
                _report(key, title, ok, detail, seconds)
    if 'd' in wanted:
        record('d', 'batch and training export', lambda: check_d(run))
    if model is not None:
        print(
            'real model: '
            + (model.get('OPENAI_MODEL') or model.get('GEMINI_MODEL') or 'the Gemini default')
        )
        if 'a' in wanted:
            record(
                'a-real',
                'README in a container against the real model',
                lambda: check_a(run, model),
            )
        if 'd' in wanted:
            record('d-real', 'batch and export against the real model', lambda: check_d(run, model))
    else:
        print(
            'real model: skipped '
            '(no OPENAI_API_KEY + OPENAI_BASE_URL + OPENAI_MODEL, no GEMINI_API_KEY)'
        )
    print(f'\nacceptance summary ({time.monotonic() - began:.1f} s, output in {out}):')
    for key, ok, _detail, seconds in results:
        print(f'  {"PASS" if ok else "FAIL"}  ({key})  {seconds:6.1f} s')
    (out / 'summary.json').write_text(
        json.dumps(
            {
                'wheel': wheel.name,
                'sha256': digest,
                'results': [
                    {
                        'check': key,
                        'pass': ok,
                        'seconds': round(seconds, 1),
                        'detail': scrub(detail, model),
                    }
                    for key, ok, detail, seconds in results
                ],
                'notes': [scrub(note, model) for note in run.log],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding='utf-8',
    )
    return 0 if results and all(ok for _key, ok, _detail, _seconds in results) else 1


def _report(key: str, title: str, ok: bool, detail: str, seconds: float) -> None:
    if ok:
        print(f'PASS  ({key}) {title}  {seconds:.1f} s  [{detail}]', flush=True)
    else:
        print(f'FAIL  ({key}) {title}  {seconds:.1f} s\n{detail}', flush=True)


if __name__ == '__main__':
    sys.exit(main())
