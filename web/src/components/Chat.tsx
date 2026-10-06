import { useEffect, useRef, useState } from 'react';
import type { Block, SessionState } from '../session';

function Badge({ block }: { block: Block }) {
  return block.trust === 'untrusted' ? (
    <span className="badge untrusted" title="这一块溯源到不可信内容（网页、命令输出、第三方 Skill）">
      不可信
    </span>
  ) : null;
}

function ToolCalls({ block, state }: { block: Block; state: SessionState }) {
  if (block.tool_calls.length === 0) return null;
  return (
    <ul className="tool-calls">
      {block.tool_calls.map((call) => {
        const outcome = state.tools[call.id]?.outcome;
        return (
          <li key={call.id} data-tool-call-id={call.id}>
            <code>{call.name}</code> <span className="args">{call.arguments}</span>{' '}
            <span className={`outcome ${outcome ?? 'running'}`}>{outcome ?? '进行中'}</span>
          </li>
        );
      })}
    </ul>
  );
}

function Entry({ block, state }: { block: Block; state: SessionState }) {
  if (block.kind) {
    return (
      <div className="block note">
        <span className="label">{block.kind}</span> <Badge block={block} />
        <pre>{block.text}</pre>
      </div>
    );
  }
  if (block.role === 'tool') {
    return (
      <details className="block tool" data-tool-call-id={block.tool_call_id ?? ''}>
        <summary>
          工具结果 <Badge block={block} />
          {block.artifacts.map((artifact) => (
            <span key={artifact.id} className="artifact-ref">
              {artifact.path}
            </span>
          ))}
        </summary>
        <pre>{block.text}</pre>
      </details>
    );
  }
  return (
    <div className={`block ${block.role}`}>
      <Badge block={block} />
      <pre>{block.text}</pre>
      <ToolCalls block={block} state={state} />
    </div>
  );
}

export function Chat(props: {
  state: SessionState;
  target: string;
  onSend: (text: string) => Promise<void>;
}) {
  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);
  const transcript = props.state.transcripts[props.target];
  const agent = props.state.agents[props.target];
  const bottom = useRef<HTMLDivElement>(null);
  const count = transcript?.blocks.length ?? 0;
  useEffect(() => {
    bottom.current?.scrollIntoView({ block: 'end' });
  }, [count, props.target]);
  return (
    <section className="chat" aria-label="对话">
      <div className="transcript">
        {(transcript?.blocks ?? []).map((block, index) =>
          block ? <Entry key={`${transcript?.generation}-${index}`} block={block} state={props.state} /> : null,
        )}
        <div ref={bottom} />
      </div>
      <form
        className="composer"
        onSubmit={(event) => {
          event.preventDefault();
          const text = draft.trim();
          if (!text) return;
          setSending(true);
          void props
            .onSend(text)
            .then(() => setDraft(''))
            .finally(() => setSending(false));
        }}
      >
        <span className="target" title="消息发给这个 Agent">
          → {agent?.name ?? props.target.slice(0, 8)}
        </span>
        <textarea
          aria-label="消息"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder="给 Agent 的任务或消息"
          rows={2}
        />
        <button type="submit" disabled={sending}>
          发送
        </button>
      </form>
    </section>
  );
}
