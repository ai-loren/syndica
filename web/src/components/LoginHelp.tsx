export function LoginHelp({ expired }: { expired: boolean }) {
  return (
    <main className="login-help">
      <h1>Syndica</h1>
      <div className="problem" role="alert">
        <dl>
          <dt>期望</dt>
          <dd>用 syndica up 打印的一次性链接打开这个页面</dd>
          <dt>实际</dt>
          <dd>{expired ? '登录已失效（server 重启后旧令牌作废）' : '这个浏览器还没有登录'}</dd>
          <dt>下一步</dt>
          <dd>
            在终端运行 <code>syndica down</code> 与 <code>syndica up</code>，打开它打印的链接
          </dd>
        </dl>
      </div>
    </main>
  );
}
