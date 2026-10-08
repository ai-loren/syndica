// 由 scripts/gen-events.mjs 从 docs/reference/openapi.json 生成；不要手改。
export const EVENT_KINDS = ["agent_end","agent_error","agent_removed","agent_spawned","agent_start","agent_status","artifact_registered","budget_changed","budget_exhausted","budget_question","budget_question_resolved","compaction","goal_drift","human_request_resolved","message_appended","progress_measured","progress_regressed","progress_stalled","reset","session_closed","session_opened","tool_call_finished","tool_call_started","tool_confirmation_requested","transcript_reset","turn_end","turn_start","user_input_requested","user_question_requested"] as const;
export type EventKind = (typeof EVENT_KINDS)[number];
export const RPC_OPS = ["abort","activate_rubric","answer","auth","budget_answer","grant","send"] as const;
export type RpcOp = (typeof RPC_OPS)[number];
export interface EventPayloads {
  agent_end: { stop_reason: unknown; [key: string]: unknown };
  agent_error: { error_type: unknown; expected: unknown; received: unknown; next_action: unknown; [key: string]: unknown };
  agent_removed: {  [key: string]: unknown };
  agent_spawned: { parent_id: unknown; name: unknown; [key: string]: unknown };
  agent_start: {  [key: string]: unknown };
  agent_status: { status: unknown; [key: string]: unknown };
  artifact_registered: { ref: unknown; note: unknown; [key: string]: unknown };
  budget_changed: { view: unknown; [key: string]: unknown };
  budget_exhausted: { dimensions: unknown; disposition: unknown; [key: string]: unknown };
  budget_question: { request_id: unknown; view: unknown; [key: string]: unknown };
  budget_question_resolved: { request_id: unknown; [key: string]: unknown };
  compaction: { trigger: unknown; blocks_before: unknown; blocks_after: unknown; tokens_before: unknown; tokens_after: unknown; [key: string]: unknown };
  goal_drift: { confirmed: unknown; [key: string]: unknown };
  human_request_resolved: { request_id: unknown; outcome: unknown; [key: string]: unknown };
  message_appended: { generation: unknown; index: unknown; block: unknown; [key: string]: unknown };
  progress_measured: { satisfied: unknown; mandatory: unknown; pending_human: unknown; measured_turn: unknown; verdict: unknown; [key: string]: unknown };
  progress_regressed: { rubrics: unknown; [key: string]: unknown };
  progress_stalled: { turns: unknown; [key: string]: unknown };
  reset: {  [key: string]: unknown };
  session_closed: {  [key: string]: unknown };
  session_opened: {  [key: string]: unknown };
  tool_call_finished: { tool_call_id: unknown; outcome: unknown; [key: string]: unknown };
  tool_call_started: { tool_call_id: unknown; name: unknown; arguments: unknown; [key: string]: unknown };
  tool_confirmation_requested: { request_id: unknown; tool_call_id: unknown; tool_name: unknown; arguments: unknown; verdict: unknown; untrusted: unknown; [key: string]: unknown };
  transcript_reset: { generation: unknown; reason: unknown; [key: string]: unknown };
  turn_end: { turn: unknown; [key: string]: unknown };
  turn_start: { turn: unknown; [key: string]: unknown };
  user_input_requested: { request_id: unknown; question: unknown; [key: string]: unknown };
  user_question_requested: { request_id: unknown; question: unknown; options: unknown; [key: string]: unknown };
}
