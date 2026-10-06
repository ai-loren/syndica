import type { AgentNode, SessionState } from '../session';
import { VERDICTS, verdictOf } from '../verdict';

function Verdict({ raw }: { raw: string | undefined }) {
  const verdict = verdictOf(raw);
  if (!verdict) return null;
  const shown = VERDICTS[verdict];
  return (
    <span className={`verdict ${shown.tone}`} data-verdict={verdict}>
      {shown.label}
    </span>
  );
}

function Node(props: {
  agent: AgentNode;
  state: SessionState;
  target: string;
  depth: number;
  onTarget: (id: string) => void;
  onAbort: (id: string) => void;
}) {
  const children = Object.values(props.state.agents).filter(
    (agent) => agent.parent_id === props.agent.agent_id,
  );
  const error = props.state.errors[props.agent.agent_id];
  return (
    <li data-agent-id={props.agent.agent_id}>
      <div className={`agent ${props.agent.agent_id === props.target ? 'current' : ''}`}>
        <button type="button" onClick={() => props.onTarget(props.agent.agent_id)}>
          {props.agent.name || props.agent.agent_id.slice(0, 8)}
        </button>
        <span className={`status ${props.agent.status}`}>{props.agent.status}</span>
        <Verdict raw={props.state.verdicts[props.agent.agent_id]} />
        {(props.agent.status === 'running' || props.agent.status === 'waiting') && (
          <button type="button" onClick={() => props.onAbort(props.agent.agent_id)}>
            中止
          </button>
        )}
      </div>
      {error && (
        <div className="problem" role="alert">
          <dl>
            <dt>期望</dt>
            <dd>{error.expected}</dd>
            <dt>实际</dt>
            <dd>{error.received}</dd>
            <dt>下一步</dt>
            <dd>{error.next_action}</dd>
          </dl>
        </div>
      )}
      {children.length > 0 && (
        <ul>
          {children.map((child) => (
            <Node key={child.agent_id} {...props} agent={child} depth={props.depth + 1} />
          ))}
        </ul>
      )}
    </li>
  );
}

export function Agents(props: {
  state: SessionState;
  target: string;
  onTarget: (id: string) => void;
  onAbort: (id: string) => void;
}) {
  const roots = Object.values(props.state.agents).filter(
    (agent) => !agent.parent_id || !props.state.agents[agent.parent_id],
  );
  return (
    <ul className="agents" aria-label="Agent 树">
      {roots.map((agent) => (
        <Node key={agent.agent_id} {...props} agent={agent} depth={0} />
      ))}
    </ul>
  );
}
