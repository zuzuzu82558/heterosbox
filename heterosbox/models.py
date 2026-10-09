from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field, model_validator
from jsonschema import Draft202012Validator
from .registry import EventType, validate_payload

Expr = str
OutcomeType = Literal["WINNER", "DRAW", "CONSENSUS", "GOAL_REACHED", "STALEMATE", "HUMAN_TERMINATED", "FAILED", "MAX_TURNS"]
SessionStatus = Literal["CREATED", "RUNNING", "PAUSING", "PAUSED", "WAITING_HUMAN", "STOPPING", "ERROR", "FINISHED"]


class StrictModel(BaseModel):
    model_config = {"extra": "forbid"}


class Rule(StrictModel):
    expr: Expr
    reason_code: str
    public_message: str = "该行动当前不可用。"


class Effect(StrictModel):
    op: Literal["set", "add", "sub", "append", "remove", "reveal"]
    path: str
    value: Any = None
    value_is_expression: bool = True


class FieldVisibility(StrictModel):
    audience: Literal["PUBLIC", "OWNER", "SPECIFIC", "OPERATOR_ONLY"]
    audience_ids: list[str] = Field(default_factory=list)
    reveal_on: list[str] = Field(default_factory=list)


class EventVisibility(StrictModel):
    audience: Literal["SELF", "ALL_AGENTS", "SPECIFIC", "OPERATOR_ONLY", "AUDIT_ONLY"]
    audience_ids: list[str] = Field(default_factory=list)
    owner_id: str | None = None


class ActionVisibility(StrictModel):
    proposal: EventVisibility
    result: EventVisibility


class PrivateStateUpdate(StrictModel):
    current_goal: str | None = None
    current_plan: str | None = None
    beliefs: dict[str, Any] = Field(default_factory=dict)
    relationships: dict[str, float] = Field(default_factory=dict)
    next_intent: str | None = None


class AgentResponse(StrictModel):
    target_channel: Literal["PUBLIC", "WHISPER"] = "PUBLIC"
    recipient_ids: list[str] = Field(default_factory=list)
    speech: str = ""
    private_update: PrivateStateUpdate | None = None


class ActionPayload(StrictModel):
    action_type: str
    parameters: dict[str, Any] = Field(default_factory=dict)


class EarlySemanticTriggerPolicy(StrictModel):
    enabled: bool = True
    max_early_checks_per_round: int = 1
    cooldown_rounds: int = 1


class HardCondition(StrictModel):
    condition_id: str
    expr: Expr
    outcome: OutcomeType
    reason: str


class GoalCheck(StrictModel):
    path: str
    op: Literal["GE", "LE", "EQ"]
    target: int | float
    group: Literal["SUCCESS", "FAILURE"]
    label: str


class SemanticTerminationResult(StrictModel):
    goal_completion: float = Field(ge=0, le=1)
    consensus_score: float = Field(ge=0, le=1)
    stalemate_score: float = Field(ge=0, le=1)
    should_terminate: bool
    outcome: OutcomeType | None = None
    reason: str
    evidence_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_outcome(self):
        if self.should_terminate and self.outcome is None:
            raise ValueError("terminating semantic decision requires outcome")
        if not self.should_terminate and self.outcome is not None:
            raise ValueError("nonterminating semantic decision must not set outcome")
        return self


class ModelFailurePolicy(StrictModel):
    provider_retries: int = 2
    fallback_model_ids: list[str] = Field(default_factory=list)
    on_exhausted: Literal["SKIP_TURN", "PAUSE", "TERMINATE"] = "PAUSE"


class RunIntegrity(StrictModel):
    pure_agent_run: bool = True
    human_takeover_count: int = 0
    operator_event_count: int = 0
    branch_forked_from_intervened_run: bool = False


class FieldSpec(StrictModel):
    path: str
    type: Literal["int", "float", "bool", "str", "enum", "list", "object"]
    min: float | None = None
    max: float | None = None
    enum_values: list[str] | None = None
    default: Any = None
    visibility: FieldVisibility
    description: str = ""
    unit: str = ""
    owner_id: str | None = None  # explicit semantic owner for OWNER visibility
    preference: Literal["HIGHER", "LOWER", "TARGET_RANGE", "NEUTRAL"] = "NEUTRAL"
    target_min: float | None = None
    target_max: float | None = None

    @model_validator(mode="after")
    def check_constraints(self):
        if (self.min is not None or self.max is not None) and self.type not in ("int", "float"):
            raise ValueError("min/max require a numeric field")
        if self.type == "enum" and not self.enum_values:
            raise ValueError("enum_values required for enum field")
        if self.preference == "TARGET_RANGE" and (self.target_min is None or self.target_max is None or self.target_min > self.target_max):
            raise ValueError("TARGET_RANGE requires ordered target_min and target_max")
        if self.preference != "TARGET_RANGE" and (self.target_min is not None or self.target_max is not None):
            raise ValueError("target_min/target_max require TARGET_RANGE")
        if self.preference != "NEUTRAL" and self.type not in ("int", "float"):
            raise ValueError("field preference requires a numeric field")
        return self


class WorldStateSchema(StrictModel):
    schema_version: str
    fields: list[FieldSpec]
    invariants: list[Expr] = Field(default_factory=list)


class AgentProfile(StrictModel):
    agent_id: str
    display_name: str
    persona: str
    public_role: str
    personal_goal: str | None = None
    shared_goals: list[str] = Field(default_factory=list)
    model_id: str
    temperature: float = 0.7
    allowed_tools: list[str] = Field(default_factory=list)


class PrivateAgentState(StrictModel):
    current_goal: str | None = None
    current_plan: str | None = None
    beliefs: dict[str, Any] = Field(default_factory=dict)
    relationships: dict[str, float] = Field(default_factory=dict)
    observations: list[dict] = Field(default_factory=list)
    next_intent: str | None = None


class MessageEntry(StrictModel):
    message_id: str
    sender_id: str
    channel: Literal["PUBLIC", "WHISPER"]
    recipient_ids: list[str] = Field(default_factory=list)
    content: str
    committed_event_seq: int


class PendingResponse(StrictModel):
    response: AgentResponse
    write_action: ActionPayload | None = None
    speech_commit_policy: Literal["IMMEDIATE", "AFTER_ACTION"] = "AFTER_ACTION"


class ActionSpec(StrictModel):
    action_type: str
    kind: Literal["READ", "WRITE", "CONTROL"]
    description: str
    intent: str = ""
    params_schema: dict
    preconditions: list[Rule] = Field(default_factory=list)
    effects: list[Effect] = Field(default_factory=list)
    visibility: ActionVisibility
    control_signal: Literal["TASK_COMPLETION", "CONSENSUS_REACHED", "SEMANTIC_CHECK", "NONE"] = "NONE"
    retry_allowed_reasons: list[str] = Field(default_factory=list)
    cost: dict[str, float] = Field(default_factory=dict)


class TurnToolPolicy(StrictModel):
    max_read_calls: int = 3
    max_write_calls: int = 1
    allow_control_with_write: bool = True


class SettlementSpec(StrictModel):
    enabled: bool = False
    preconditions: list[Rule] = Field(default_factory=list)
    effects: list[Effect] = Field(default_factory=list)
    visibility: EventVisibility


class Perspective(StrictModel):
    kind: Literal["GOD", "AGENT", "OBSERVER"]
    agent_id: str | None = None
    principal_role: Literal["OBSERVER", "PARTICIPANT", "OPERATOR", "OWNER"]
    audit_level: Literal["NONE", "DEBUG", "FULL"] = "NONE"


class Principal(StrictModel):
    principal_id: str
    org_id: str | None = None


class SessionGrant(StrictModel):
    session_id: str
    principal_id: str
    role: Literal["OBSERVER", "PARTICIPANT", "OPERATOR", "OWNER"]
    allowed_perspectives: list[str]
    audit_level: Literal["NONE", "DEBUG", "FULL"]


class SimEvent(StrictModel):
    event_id: str
    session_id: str
    branch_id: str
    event_seq: int
    parent_event_id: str | None
    round_index: int
    turn_index: int
    actor_id: str | None
    type: EventType
    payload: dict
    visibility: EventVisibility
    created_at: float

    @model_validator(mode="after")
    def check_payload(self):
        validate_payload(self.type, self.payload)
        if self.type.value == "visibility_revealed" and self.payload.get("retroactive") is True:
            if type(self.payload.get("retroactive_from_event_seq")) is not int:
                raise ValueError("retroactive reveal requires retroactive_from_event_seq")
        return self


class Checkpoint(StrictModel):
    checkpoint_id: str
    session_id: str
    branch_id: str
    parent_checkpoint_id: str | None
    event_seq: int
    round_index: int
    turn_index: int
    world_state: dict
    private_states: dict[str, dict]
    public_blackboard: str
    scheduler_state: dict
    counters: dict
    leaderboard_snapshot: dict | None
    judger_state: dict
    schema_versions: dict
    budget_state: dict
    pending_commands: list[dict]
    visibility_cursor: int
    kind: Literal["TURN", "ROUND"]
    run_integrity: RunIntegrity
    takeover_state: dict | None
    last_consumed_command_seq: int


class TakeoverPolicy(StrictModel):
    timeout_s: int = 300
    on_timeout: Literal["FALLBACK_AGENT", "SKIP_TURN"] = "FALLBACK_AGENT"
    mode: Literal["SINGLE_TURN", "UNTIL_RELEASE"] = "UNTIL_RELEASE"


class BudgetPolicy(StrictModel):
    max_total_tokens: int | None = None
    max_cost_usd: float | None = None
    max_llm_calls: int | None = None
    warn_at_ratio: float = 0.8
    on_exhausted: Literal["TERMINATE", "PAUSE"] = "PAUSE"


class TerminationPolicy(StrictModel):
    max_turns: int | None = Field(default=30, ge=1)
    max_rounds: int | None = Field(default=None, ge=1)
    round_limit_outcome: Literal["MAX_TURNS", "FAILED"] = "MAX_TURNS"
    hard_conditions: list[HardCondition] = Field(default_factory=list)
    stalemate_no_delta_rounds: int | None = 3
    goal_reached: bool = True
    allow_consensus: bool = True
    allow_stalemate: bool = True
    allow_human_stop: bool = True

    @model_validator(mode="after")
    def check_time_limit(self):
        if self.max_turns is None and self.max_rounds is None:
            raise ValueError("a max turn or round limit is required")
        return self


class PrivateMemoryPolicy(StrictModel):
    max_observations: int = 100
    max_beliefs: int = 100
    max_relationship_entries: int = 50


class ArtifactCapability(StrictModel):
    enabled: bool = False
    final_deliverable_required: bool = False


class JudgingPolicy(StrictModel):
    enabled: bool = True
    semantic_judging_enabled: bool = True
    semantic_check_interval_rounds: int = Field(default=2, ge=1)
    judging_dimensions: list[str] = Field(default_factory=list)
    require_evidence: bool = True
    allow_user_edit: bool = True


class LeaderboardMetric(StrictModel):
    metric_id: str
    name: str
    source: Literal["DETERMINISTIC", "SEMANTIC"]
    weight: float = Field(ge=0, le=1)


class LeaderboardConfig(StrictModel):
    enabled: bool = True
    leaderboard_type: Literal["contribution", "competitive", "mixed", "custom"]
    metrics: list[LeaderboardMetric] = Field(default_factory=list)
    update_interval_rounds: int = Field(default=1, ge=1)
    show_score_breakdown: bool = True


class SchedulerState(StrictModel):
    round_index: int
    turn_index: int
    actual_turn_order: list[str]
    next_agent_offset: int
    random_seed: int
    settlement_done: bool = False


class SceneRuntimeConfig(StrictModel):
    scene_id: str
    scene_version: str
    scenario_name: str
    background_story: str
    public_story: str = ""
    interaction_mode: Literal["cooperative", "competitive", "mixed"]
    shared_goals: list[str] = Field(default_factory=list)
    goal_checks: list[GoalCheck] = Field(default_factory=list)
    grounding_rules: list[str] = Field(default_factory=list)
    world_state_schema: WorldStateSchema
    initial_world_state: dict
    action_specs: list[ActionSpec]
    settlement_spec: SettlementSpec | None = None
    agents: list[AgentProfile] = Field(min_length=2, max_length=7)
    scheduler_policy: Literal["fixed_round_robin", "shuffle_round_robin", "priority"]
    random_seed: int
    judging_policy: JudgingPolicy
    leaderboard_config: LeaderboardConfig
    termination_policy: TerminationPolicy
    early_semantic_trigger_policy: EarlySemanticTriggerPolicy
    turn_tool_policy: TurnToolPolicy = Field(default_factory=TurnToolPolicy)
    takeover_policy: TakeoverPolicy = Field(default_factory=TakeoverPolicy)
    budget_policy: BudgetPolicy = Field(default_factory=BudgetPolicy)
    model_failure_policy: ModelFailurePolicy = Field(default_factory=ModelFailurePolicy)
    failure_broadcast_policy: Literal["SILENT", "PUBLIC_GENERIC", "OPERATOR_ONLY"] = "SILENT"
    private_memory_policy: PrivateMemoryPolicy = Field(default_factory=PrivateMemoryPolicy)
    artifact_capability: ArtifactCapability | None = None

    @model_validator(mode="after")
    def validate_scene(self):
        agent_ids = [a.agent_id for a in self.agents]
        tools = [a.action_type for a in self.action_specs]
        paths = [f.path for f in self.world_state_schema.fields]
        if any(check.path not in paths for check in self.goal_checks):
            raise ValueError("goal check path absent from WorldStateSchema")
        if len(agent_ids) != len(set(agent_ids)) or len(tools) != len(set(tools)) or len(paths) != len(set(paths)):
            raise ValueError("duplicate agent, action or field path")
        if any(left.startswith(right + ".") or right.startswith(left + ".") for index, left in enumerate(paths) for right in paths[index + 1:]):
            raise ValueError("overlapping field paths can bypass visibility")
        if any(set(a.allowed_tools) - set(tools) for a in self.agents):
            raise ValueError("allowed_tools must reference ActionSpec")
        for field in self.world_state_schema.fields:
            if field.visibility.audience == "OWNER" and field.owner_id not in agent_ids:
                raise ValueError(f"OWNER field {field.path} requires a valid unique owner_id")
            if field.visibility.audience == "SPECIFIC" and not set(field.visibility.audience_ids) <= set(agent_ids):
                raise ValueError("unknown field audience")
        for action in self.action_specs:
            Draft202012Validator.check_schema(action.params_schema)
            if action.kind != "WRITE" and action.effects:
                raise ValueError("only WRITE ActionSpec may have effects")
            if any(effect.path not in paths for effect in action.effects):
                raise ValueError("effect path absent from WorldStateSchema")
            if any(path not in paths or next(f for f in self.world_state_schema.fields if f.path == path).type not in ("int", "float")
                   or amount < 0 for path, amount in action.cost.items()):
                raise ValueError("action cost requires a nonnegative numeric field")
            if any(next(f for f in self.world_state_schema.fields if f.path == path).type == "int"
                   and not amount.is_integer() for path, amount in action.cost.items()):
                raise ValueError("integer resource cost must be whole")
            if set(action.cost) & {effect.path for effect in action.effects if effect.op != "reveal"}:
                raise ValueError("cost and effects must not write the same field")
            for effect in action.effects:
                field = next(f for f in self.world_state_schema.fields if f.path == effect.path)
                if effect.op in ("add", "sub") and field.type not in ("int", "float"):
                    raise ValueError("numeric effect requires numeric field")
                if effect.op in ("append", "remove") and field.type != "list":
                    raise ValueError("list effect requires list field")
                if effect.op == "reveal":
                    visibility = EventVisibility.model_validate(effect.value)
                    if visibility.audience == "SPECIFIC" and not set(visibility.audience_ids) <= set(agent_ids):
                        raise ValueError("unknown reveal audience")
                    if visibility.audience == "SELF" and visibility.owner_id not in agent_ids:
                        raise ValueError("unknown reveal owner")
        if self.settlement_spec and any(e.path not in paths for e in self.settlement_spec.effects):
            raise ValueError("settlement path absent from WorldStateSchema")
        return self


class ExecutionFeedback(StrictModel):
    status: Literal["ACCEPTED", "REJECTED"]
    reason_code: str | None = None
    message: str
    observed_state: dict[str, Any] = Field(default_factory=dict)
    retry_allowed: bool = False
