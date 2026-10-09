import type { components } from './generated/openapi';
import type { Frame } from './sse';

// 会话的界面状态只由两样东西归并出来：快照（带 as_of）与之后的事件。同一条事件到两次（快照与流的
// 交界处）结果不变：块按（代，位置）放，工具调用按 id，请求按 request_id，Agent 按 id。

export type Snapshot = components['schemas']['Snapshot'];
export type Block = components['schemas']['Block'];
export type AgentNode = components['schemas']['AgentNode'];

export interface Pending {
  request_id: string;
  kind: 'confirmation' | 'user_input' | 'user_question' | 'budget';
  agent_id: string;
  [key: string]: unknown;
}

export interface ToolState {
  name: string;
  arguments: string;
  outcome: string | null;
  agent_id: string;
}

export interface TimelineItem {
  id: string | null;
  kind: string;
  agent_id: string | null;
  time_ns: number;
  detail: string;
}

export interface AgentError {
  error_type: string;
  expected: string;
  received: string;
  next_action: string;
}

export interface SessionState {
  sid: string;
  rootId: string;
  agents: Record<string, AgentNode>;
  transcripts: Record<string, { generation: number; blocks: (Block | undefined)[] }>;
  pending: Record<string, Pending>;
  tools: Record<string, ToolState>;
  budgets: Record<string, unknown>;
  progress: unknown;
  verdicts: Record<string, string>;
  stops: Record<string, string>;
  errors: Record<string, AgentError>;
  timeline: TimelineItem[];
  stale: boolean;
}

const TIMELINE = 500;

export function without<T>(record: Record<string, T>, key: string): Record<string, T> {
  return Object.fromEntries(Object.entries(record).filter(([name]) => name !== key));
}

export function fromSnapshot(snapshot: Snapshot, previous?: SessionState): SessionState {
  const agents: Record<string, AgentNode> = {};
  for (const agent of snapshot.agents) agents[agent.agent_id] = agent;
  const transcripts: SessionState['transcripts'] = {};
  for (const [agentId, transcript] of Object.entries(snapshot.transcripts)) {
    transcripts[agentId] = { generation: transcript.generation, blocks: [...transcript.blocks] };
  }
  const pending: Record<string, Pending> = {};
  for (const item of snapshot.pending) pending[item.request_id] = item as Pending;
  return {
    sid: snapshot.sid,
    rootId: snapshot.root_id,
    agents,
    transcripts,
    pending,
    tools: previous?.sid === snapshot.sid ? previous.tools : {},
    budgets: snapshot.budget ? { [snapshot.root_id]: snapshot.budget } : {},
    progress: snapshot.progress,
    verdicts: previous?.sid === snapshot.sid ? previous.verdicts : {},
    stops: previous?.sid === snapshot.sid ? previous.stops : {},
    errors: previous?.sid === snapshot.sid ? previous.errors : {},
    timeline: previous?.sid === snapshot.sid ? previous.timeline : [],
    stale: false,
  };
}

function text(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

export function apply(state: SessionState, frame: Frame): SessionState {
  if (frame.kind === 'reset') return { ...state, stale: true };
  const data = frame.data;
  const agentId = typeof data.agent_id === 'string' ? data.agent_id : null;
  const payload = (data.payload ?? {}) as Record<string, unknown>;
  const next: SessionState = {
    ...state,
    timeline: [
      ...state.timeline.slice(-(TIMELINE - 1)),
      {
        id: frame.id,
        kind: frame.kind,
        agent_id: agentId,
        time_ns: typeof data.time_ns === 'number' ? data.time_ns : 0,
        detail: summary(frame.kind, payload),
      },
    ],
  };
  switch (frame.kind) {
    case 'message_appended': {
      if (!agentId) return next;
      const generation = Number(payload.generation);
      const index = Number(payload.index);
      const current = state.transcripts[agentId];
      if (current && generation < current.generation) return next;
      if (current && generation > current.generation) return { ...next, stale: true };
      const blocks = [...(current?.blocks ?? [])];
      blocks[index] = payload.block as Block;
      return { ...next, transcripts: { ...state.transcripts, [agentId]: { generation, blocks } } };
    }
    case 'transcript_reset':
      return { ...next, stale: true };
    case 'agent_spawned':
      if (!agentId) return next;
      return {
        ...next,
        agents: {
          ...state.agents,
          [agentId]: {
            agent_id: agentId,
            name: text(payload.name),
            parent_id: typeof payload.parent_id === 'string' ? payload.parent_id : null,
            status: state.agents[agentId]?.status ?? 'idle',
          },
        },
      };
    case 'agent_status': {
      if (!agentId) return next;
      const known = state.agents[agentId];
      if (!known) return { ...next, stale: true };
      return {
        ...next,
        agents: { ...state.agents, [agentId]: { ...known, status: text(payload.status) } },
      };
    }
    case 'agent_removed':
      if (!agentId) return next;
      return { ...next, agents: without(state.agents, agentId) };
    case 'tool_call_started': {
      const id = text(payload.tool_call_id);
      return {
        ...next,
        tools: {
          ...state.tools,
          [id]: {
            name: text(payload.name),
            arguments: text(payload.arguments),
            outcome: null,
            agent_id: agentId ?? '',
          },
        },
      };
    }
    case 'tool_call_finished': {
      const id = text(payload.tool_call_id);
      const known = state.tools[id];
      if (!known) return next;
      return { ...next, tools: { ...state.tools, [id]: { ...known, outcome: text(payload.outcome) } } };
    }
    case 'tool_confirmation_requested':
    case 'user_input_requested':
    case 'user_question_requested':
    case 'budget_question': {
      const kind =
        frame.kind === 'tool_confirmation_requested'
          ? 'confirmation'
          : frame.kind === 'user_input_requested'
            ? 'user_input'
            : frame.kind === 'user_question_requested'
              ? 'user_question'
              : 'budget';
      const id = text(payload.request_id);
      return {
        ...next,
        pending: { ...state.pending, [id]: { ...payload, request_id: id, kind, agent_id: agentId ?? '' } },
      };
    }
    case 'human_request_resolved':
    case 'budget_question_resolved':
      return { ...next, pending: without(state.pending, text(payload.request_id)) };
    case 'budget_changed':
      if (!agentId) return next;
      return { ...next, budgets: { ...state.budgets, [agentId]: payload.view } };
    case 'agent_end': {
      if (!agentId) return next;
      const verdicts = { ...state.verdicts };
      if (typeof payload.verdict === 'string') verdicts[agentId] = payload.verdict;
      return { ...next, verdicts, stops: { ...state.stops, [agentId]: text(payload.stop_reason) } };
    }
    case 'progress_measured': {
      if (!agentId) return next;
      const progress = { ...((state.progress ?? {}) as Record<string, unknown>), [agentId]: payload };
      return { ...next, progress };
    }
    case 'agent_error':
      if (!agentId) return next;
      return { ...next, errors: { ...state.errors, [agentId]: payload as unknown as AgentError } };
    default:
      return next;
  }
}

function summary(kind: string, payload: Record<string, unknown>): string {
  switch (kind) {
    case 'message_appended': {
      const block = (payload.block ?? {}) as Record<string, unknown>;
      return `${text(block.role)}: ${text(block.text).slice(0, 80)}`;
    }
    case 'tool_call_started':
      return text(payload.name);
    case 'tool_call_finished':
      return `${text(payload.tool_call_id)} → ${text(payload.outcome)}`;
    case 'agent_status':
      return text(payload.status);
    case 'agent_end':
      return text(payload.stop_reason);
    case 'agent_spawned':
      return text(payload.name);
    case 'external_activity':
      return [payload.source, payload.name, payload.kind, payload.status, payload.title]
        .map(text)
        .filter((part) => part.length > 0)
        .join(' ');
    default:
      return '';
  }
}
