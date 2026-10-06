import { Problem } from '../api';

export function ProblemBox({ problem, onDismiss }: { problem: unknown; onDismiss?: () => void }) {
  const known = problem instanceof Problem ? problem : null;
  return (
    <div className="problem" role="alert">
      <dl>
        <dt>期望</dt>
        <dd>{known ? known.expected : '操作成功'}</dd>
        <dt>实际</dt>
        <dd>{known ? known.received : String(problem)}</dd>
        <dt>下一步</dt>
        <dd>{known ? known.nextAction : '刷新页面重试；仍不行就看 syndica status 给出的日志'}</dd>
      </dl>
      {onDismiss && (
        <button type="button" onClick={onDismiss}>
          知道了
        </button>
      )}
    </div>
  );
}
