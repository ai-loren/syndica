import { useState } from 'react';
import type { Pending, SessionState } from '../session';

function Verdict({ pending }: { pending: Pending }) {
  const verdict = (pending.verdict ?? {}) as Record<string, string>;
  return (
    <dl className="verdict-detail">
      <dt>规则</dt>
      <dd>{verdict.rule_id}</dd>
      <dt>风险</dt>
      <dd>{verdict.level}</dd>
      <dt>原因</dt>
      <dd>{verdict.reason}</dd>
      <dt>来源</dt>
      <dd className={pending.untrusted ? 'untrusted' : undefined}>
        {pending.untrusted ? '这次调用的参数溯源到不可信内容' : '参数没有溯源到不可信内容'}
      </dd>
    </dl>
  );
}

export function PendingDialog(props: {
  state: SessionState;
  onAnswer: (pending: Pending, text: string) => Promise<void>;
  onBudget: (pending: Pending, turns: number | null) => Promise<void>;
}) {
  const [text, setText] = useState('');
  const [turns, setTurns] = useState('5');
  const queue = Object.values(props.state.pending);
  const pending = queue[0];
  if (!pending) return null;
  const agent = props.state.agents[pending.agent_id]?.name ?? pending.agent_id.slice(0, 8);
  return (
    <div className="overlay">
      <div className="dialog" role="dialog" aria-modal="true" aria-label="需要你答复" data-kind={pending.kind}>
        <header>
          {agent} 在等你{queue.length > 1 ? `（还有 ${queue.length - 1} 个）` : ''}
        </header>
        {pending.kind === 'confirmation' && (
          <>
            <p>
              要执行 <code>{String(pending.tool_name)}</code>：
            </p>
            <pre className="arguments">{String(pending.arguments)}</pre>
            <Verdict pending={pending} />
            <div className="actions">
              <button type="button" className="approve" onClick={() => void props.onAnswer(pending, 'approve')}>
                批准
              </button>
              <button type="button" className="reject" onClick={() => void props.onAnswer(pending, 'reject')}>
                拒绝
              </button>
            </div>
          </>
        )}
        {pending.kind === 'user_input' && (
          <form
            onSubmit={(event) => {
              event.preventDefault();
              void props.onAnswer(pending, text).then(() => setText(''));
            }}
          >
            <pre className="question">{String(pending.question)}</pre>
            <textarea aria-label="回答" value={text} onChange={(event) => setText(event.target.value)} />
            <div className="actions">
              <button type="submit">回答</button>
            </div>
          </form>
        )}
        {pending.kind === 'user_question' && (
          <>
            <pre className="question">{String(pending.question)}</pre>
            <div className="actions options">
              {(Array.isArray(pending.options) ? pending.options : []).map((option) => (
                <button type="button" key={String(option)} onClick={() => void props.onAnswer(pending, String(option))}>
                  {String(option)}
                </button>
              ))}
            </div>
          </>
        )}
        {pending.kind === 'budget' && (
          <form
            onSubmit={(event) => {
              event.preventDefault();
              void props.onBudget(pending, Number(turns));
            }}
          >
            <p>预算用完了。追加几轮？</p>
            <input
              aria-label="追加的轮数"
              type="number"
              min={1}
              value={turns}
              onChange={(event) => setTurns(event.target.value)}
            />
            <div className="actions">
              <button type="submit">追加</button>
              <button type="button" onClick={() => void props.onBudget(pending, null)}>
                不追加，收尾
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}
