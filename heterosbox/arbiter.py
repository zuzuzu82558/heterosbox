"""Single atomic gate for action and settlement world writes."""
import copy
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator

from .expr import ExpressionError, ExpressionEvaluator, freeze
from .models import ActionPayload, ActionSpec, EventVisibility, ExecutionFeedback, SceneRuntimeConfig, SettlementSpec, WorldStateSchema


class SceneEvaluationError(RuntimeError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code


@dataclass(frozen=True)
class TransactionResult:
    feedback: ExecutionFeedback
    world_state: dict
    changes: dict[str, Any]
    reveals: list[dict] = field(default_factory=list)
    before: dict[str, Any] = field(default_factory=dict)


def _get(state: dict, path: str):
    value = state
    for part in path.split("."):
        value = value[part]
    return value


def _set(state: dict, path: str, value: Any):
    parts = path.split(".")
    target = state
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = value


def validate_world(state: dict, schema: WorldStateSchema):
    for field in schema.fields:
        try:
            value = _get(state, field.path)
        except (KeyError, TypeError):
            raise ValueError(f"missing field {field.path}") from None
        valid = {
            "int": type(value) is int,
            "float": type(value) is float,
            "bool": type(value) is bool,
            "str": type(value) is str,
            "enum": type(value) is str and value in (field.enum_values or []),
            "list": type(value) is list,
            "object": type(value) is dict,
        }[field.type]
        if not valid:
            raise ValueError(f"invalid type at {field.path}")
        if field.min is not None and value < field.min:
            raise ValueError(f"below minimum at {field.path}")
        if field.max is not None and value > field.max:
            raise ValueError(f"above maximum at {field.path}")
        ExpressionEvaluator()._check_value(value)


class WriteArbiter:
    def __init__(self, scene: SceneRuntimeConfig):
        self.scene = scene
        self.evaluator = ExpressionEvaluator()
        validate_world(scene.initial_world_state, scene.world_state_schema)

    def execute(self, world: dict, action: ActionPayload, actor: str, session: dict, *, dry_run: bool = False) -> TransactionResult:
        spec = next((s for s in self.scene.action_specs if s.action_type == action.action_type), None)
        if spec is None or spec.kind != "WRITE":
            return self._reject(world, "UNKNOWN_ACTION", "Action unavailable.")
        agent = next((a for a in self.scene.agents if a.agent_id == actor), None)
        if agent is None or spec.action_type not in agent.allowed_tools:
            return self._reject(world, "PERMISSION_DENIED", "Action unavailable.")
        errors = list(Draft202012Validator(spec.params_schema).iter_errors(action.parameters))
        if errors:
            return self._reject(world, "INVALID_PARAMS", "Invalid action parameters.")
        return self._transact(world, action.parameters, actor, session, spec.preconditions, spec.effects,
                              spec.retry_allowed_reasons, spec.cost)

    def settle(self, world: dict, session: dict) -> TransactionResult:
        spec = self.scene.settlement_spec
        if spec is None or not spec.enabled:
            return TransactionResult(ExecutionFeedback(status="ACCEPTED", message="No settlement."), copy.deepcopy(world), {})
        # Older compiled scenes used one boundary rule per effect. A single saturated
        # metric must not cancel unrelated environmental changes in the same tick.
        boundary_rules = [rule for rule in spec.preconditions if rule.reason_code == "SETTLEMENT_BOUNDARY"]
        rules = [rule for rule in spec.preconditions if rule.reason_code != "SETTLEMENT_BOUNDARY"]
        return self._transact(world, {}, "__system__", session, rules, spec.effects, [],
                              clamp_to_bounds=bool(boundary_rules))

    def _reject(self, world: dict, code: str, message: str, retry: bool = False) -> TransactionResult:
        return TransactionResult(ExecutionFeedback(status="REJECTED", reason_code=code, message=message, retry_allowed=retry), copy.deepcopy(world), {})

    def _transact(self, world: dict, params: dict, actor: str, session: dict, rules, effects, retry_reasons,
                  costs: dict[str, float] | None = None, *, clamp_to_bounds: bool = False) -> TransactionResult:
        original = copy.deepcopy(world)
        immutable_world = freeze(original)
        base = {"world": immutable_world, "params": freeze(params), "actor": actor, "session": freeze(session)}
        for rule in rules:
            try:
                accepted = self.evaluator.evaluate(rule.expr, base, phase="precondition")
            except ExpressionError as exc:
                raise SceneEvaluationError("RULE_EVAL_ERROR", str(exc)) from exc
            if not isinstance(accepted, bool):
                raise SceneEvaluationError("RULE_EVAL_ERROR", "precondition must return bool")
            if not accepted:
                return self._reject(world, rule.reason_code, rule.public_message, rule.reason_code in retry_reasons)
        working = copy.deepcopy(original)
        for path, amount in (costs or {}).items():
            before = _get(working, path)
            if before < amount:
                return self._reject(world, "INSUFFICIENT_RESOURCE", "资源不足")
            if type(before) is int:
                amount = int(amount)
            _set(working, path, before - amount)
        reveals = []
        for effect in effects:
            try:
                if effect.op == "reveal":
                    visibility = EventVisibility.model_validate(effect.value)
                    if (visibility.audience == "SPECIFIC" and len(self.scene.agents) > 2
                            and set(visibility.audience_ids) >= {a.agent_id for a in self.scene.agents}):
                        return self._reject(world, "INVALID_REVEAL_SCOPE", "定向披露不能发送给所有角色")
                    reveals.append({"field_path": effect.path, "new_visibility": visibility.model_dump(),
                                    "duration": "PERMANENT", "retroactive": False})
                    continue
                context = {**base, "working": freeze(working)}
                value = (self.evaluator.evaluate(effect.value, context, phase="effect")
                         if isinstance(effect.value, str) and effect.value_is_expression else copy.deepcopy(effect.value))
                before = _get(working, effect.path)
                new = {"set": lambda: value, "add": lambda: before + value,
                       "sub": lambda: before - value, "append": lambda: before + [value],
                       "remove": lambda: [v for v in before if v != value]}[effect.op]()
                if clamp_to_bounds and effect.op in ("add", "sub"):
                    field_spec = next((field for field in self.scene.world_state_schema.fields
                                       if field.path == effect.path), None)
                    if field_spec is not None:
                        if field_spec.min is not None:
                            new = max(new, field_spec.min)
                        if field_spec.max is not None:
                            new = min(new, field_spec.max)
                        if type(before) is int and type(new) is float and new.is_integer():
                            new = int(new)
                _set(working, effect.path, new)
            except Exception as exc:
                raise SceneEvaluationError("EFFECT_EVAL_ERROR", str(exc)) from exc
        try:
            validate_world(working, self.scene.world_state_schema)
        except Exception as exc:
            raise SceneEvaluationError("SCHEMA_VALIDATION_ERROR", str(exc)) from exc
        for invariant in self.scene.world_state_schema.invariants:
            try:
                result = self.evaluator.evaluate(invariant, {**base, "working": freeze(working)}, phase="invariant")
                if type(result) is not bool or not result:
                    raise ExpressionError("invariant failed")
            except ExpressionError as exc:
                raise SceneEvaluationError("INVARIANT_EVAL_ERROR", str(exc)) from exc
        changes = {f.path: _get(working, f.path) for f in self.scene.world_state_schema.fields if _get(original, f.path) != _get(working, f.path)}
        before = {path: _get(original, path) for path in changes}
        return TransactionResult(ExecutionFeedback(status="ACCEPTED", message="Action accepted."), working, changes, reveals, before)
