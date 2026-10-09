"""Constrained natural-language scene drafting.

The provider supplies story semantics. Executable rules are assembled here from a
small, validated vocabulary so provider output never becomes arbitrary Expr code.
"""
from __future__ import annotations

import re
import uuid
from math import gcd
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .arbiter import WriteArbiter
from .models import SceneRuntimeConfig


class DraftAgent(BaseModel):
    name: str = Field(min_length=1, max_length=32)
    role: str = Field(min_length=1, max_length=80)
    persona: str = Field(min_length=1, max_length=180)
    goal: str = Field(min_length=1, max_length=180)


class DraftMetric(BaseModel):
    name: str = Field(min_length=1, max_length=32)
    initial: int
    minimum: int = 0
    maximum: int
    unit: str = Field(default="", max_length=20)
    preference: Literal["HIGHER", "LOWER", "TARGET_RANGE", "NEUTRAL"] = "HIGHER"
    target_min: int | None = None
    target_max: int | None = None
    visibility: Literal["PUBLIC", "OWNER", "SPECIFIC", "OPERATOR_ONLY"] = "PUBLIC"
    owner_agent_index: int | None = None
    visible_to_agent_indices: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_bounds(self):
        if self.minimum >= self.maximum or not self.minimum <= self.initial <= self.maximum:
            raise ValueError("metric bounds must contain initial")
        if self.preference == "TARGET_RANGE" and (self.target_min is None or self.target_max is None or self.target_min > self.target_max):
            raise ValueError("target range is incomplete")
        return self


class DraftFact(BaseModel):
    name: str = Field(min_length=1, max_length=48)
    value: str | int | bool
    visibility: Literal["PUBLIC", "OWNER", "SPECIFIC", "OPERATOR_ONLY"] = "OWNER"
    owner_agent_index: int | None = None
    visible_to_agent_indices: list[int] = Field(default_factory=list)


class DraftCondition(BaseModel):
    metric_index: int = Field(ge=0)
    op: Literal["GE", "LE", "EQ"]
    value: int


class DraftEffect(BaseModel):
    target: Literal["METRIC", "FACT"] = "METRIC"
    index: int = Field(ge=0)
    op: Literal["add", "sub", "set", "reveal"]
    value: str | int | bool | None = None
    visible_to_agent_indices: list[int] = Field(default_factory=list)
    reveal_public: bool = False


class DraftAction(BaseModel):
    name: str = Field(min_length=1, max_length=32)
    description: str = Field(min_length=1, max_length=120)
    metric_index: int | None = Field(default=None, ge=0)
    delta: int | None = Field(default=None, ge=1, le=5)
    allowed_agent_indices: list[int] | None = None
    conditions: list[DraftCondition] = Field(default_factory=list)
    effects: list[DraftEffect] = Field(default_factory=list)


class DraftPlan(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    story: str = Field(min_length=8, max_length=1200)
    public_story: str | None = Field(default=None, max_length=1200)
    mode: Literal["cooperative", "competitive", "mixed"]
    shared_goal: str = Field(min_length=4, max_length=240)
    agents: list[DraftAgent] = Field(min_length=2, max_length=7)
    metrics: list[DraftMetric] = Field(min_length=1, max_length=8)
    facts: list[DraftFact] = Field(default_factory=list, max_length=8)
    actions: list[DraftAction] = Field(min_length=1, max_length=12)
    target_metric_index: int = Field(ge=0)
    target_value: int
    success_conditions: list[DraftCondition] = Field(default_factory=list)
    failure_conditions: list[DraftCondition] = Field(default_factory=list)
    settlement_effects: list[DraftEffect] = Field(default_factory=list)
    max_rounds: int | None = Field(default=None, ge=1, le=100)
    round_limit_outcome: Literal["MAX_TURNS", "FAILED"] = "MAX_TURNS"

    @model_validator(mode="after")
    def check_links(self):
        if self.target_metric_index >= len(self.metrics):
            raise ValueError("target metric missing")
        target = self.metrics[self.target_metric_index]
        if not target.minimum <= self.target_value <= target.maximum or self.target_value == target.initial:
            raise ValueError("target must be in bounds and differ from initial")
        if any(action.metric_index is not None and action.metric_index >= len(self.metrics) for action in self.actions):
            raise ValueError("action metric missing")
        if any(not action.effects and (action.metric_index is None or action.delta is None) for action in self.actions):
            raise ValueError("each action needs structured effects or a legacy metric and delta")
        if not self.success_conditions and not any(action.metric_index == self.target_metric_index or any(
                effect.target == "METRIC" and effect.index == self.target_metric_index for effect in action.effects)
                for action in self.actions):
            raise ValueError("an action must advance the target metric")
        if self.facts and not self.public_story:
            raise ValueError("private facts require a separate public_story")
        for fact in self.facts:
            if fact.visibility != "PUBLIC" and isinstance(fact.value, str) and len(fact.value) >= 4:
                if fact.value in (self.public_story or "") or fact.value in self.shared_goal or fact.value in fact.name:
                    raise ValueError("private fact value appears in public text")
        if any(action.effects and action.allowed_agent_indices is None for action in self.actions):
            raise ValueError("structured actions require explicit allowed_agent_indices")
        for metric in self.metrics:
            if metric.name.endswith(("风险", "伤亡", "损失", "污染", "故障数")) and metric.preference == "HIGHER":
                raise ValueError(f"{metric.name} must not be scored as HIGHER")
            if metric.owner_agent_index is not None and metric.owner_agent_index >= len(self.agents):
                raise ValueError("metric owner missing")
            if any(index >= len(self.agents) for index in metric.visible_to_agent_indices):
                raise ValueError("metric audience missing")
        for fact in self.facts:
            if fact.visibility == "OWNER" and fact.owner_agent_index is None:
                raise ValueError("private fact requires owner_agent_index")
            if fact.owner_agent_index is not None and fact.owner_agent_index >= len(self.agents):
                raise ValueError("fact owner missing")
            if any(index >= len(self.agents) for index in fact.visible_to_agent_indices):
                raise ValueError("fact audience missing")
        for action in self.actions:
            if action.allowed_agent_indices is not None and (not action.allowed_agent_indices or any(index >= len(self.agents) for index in action.allowed_agent_indices)):
                raise ValueError("action allowed_agent_indices invalid")
            for effect in action.effects:
                if effect.index >= len(self.metrics if effect.target == "METRIC" else self.facts):
                    raise ValueError("action effect target missing")
                if effect.op == "reveal" and effect.target != "FACT":
                    raise ValueError("only facts can be revealed")
                if any(index >= len(self.agents) for index in effect.visible_to_agent_indices):
                    raise ValueError("reveal audience missing")
                if effect.op == "reveal" and not effect.reveal_public and len(self.agents) > 2:
                    owner = self.facts[effect.index].owner_agent_index
                    recipients = set(effect.visible_to_agent_indices)
                    if owner is not None:
                        recipients.add(owner)
                    if len(recipients) == len(self.agents):
                        raise ValueError("targeted reveal cannot include every agent")
            writes = [(effect.target, effect.index) for effect in action.effects if effect.op != "reveal"]
            if len(writes) != len(set(writes)):
                raise ValueError("an action cannot write the same fact twice")
            for index, metric in enumerate(self.metrics):
                positive = re.search(rf"(?:增加|提升|提高)\s*{re.escape(metric.name)}|{re.escape(metric.name)}\s*(?:增加|提升|提高)", action.description)
                negative = re.search(rf"(?:降低|减少|消耗|削减)\s*{re.escape(metric.name)}|{re.escape(metric.name)}\s*(?:降低|减少|消耗|削减)", action.description)
                if not positive and not negative:
                    continue
                effects = [effect for effect in action.effects if effect.target == "METRIC" and effect.index == index]
                if not effects and action.metric_index == index and action.delta is not None:
                    effects = [DraftEffect(index=index, op="add", value=action.delta)]
                if len(effects) != 1 or effects[0].op not in ("add", "sub") or (positive and effects[0].op != "add") or (negative and effects[0].op != "sub"):
                    raise ValueError(f"action {action.name} contradicts claimed change to {metric.name}")
        for effect in self.settlement_effects:
            if effect.target != "METRIC" or effect.op == "reveal" or effect.index >= len(self.metrics):
                raise ValueError("settlement requires a numeric metric effect")
        for condition in self.success_conditions + self.failure_conditions + [c for action in self.actions for c in action.conditions]:
            if condition.metric_index >= len(self.metrics):
                raise ValueError("condition metric missing")
            metric = self.metrics[condition.metric_index]
            if not metric.minimum <= condition.value <= metric.maximum:
                raise ValueError("condition threshold must be within metric bounds")
        def met(condition):
            current = self.metrics[condition.metric_index].initial
            return {"GE": current >= condition.value, "LE": current <= condition.value,
                    "EQ": current == condition.value}[condition.op]
        if any(met(condition) for condition in self.failure_conditions):
            raise ValueError("failure condition already holds at start")
        if self.success_conditions and all(met(condition) for condition in self.success_conditions):
            raise ValueError("all success conditions already hold at start")
        success = self.success_conditions or [DraftCondition(metric_index=self.target_metric_index,
            op="GE" if self.target_value > self.metrics[self.target_metric_index].initial else "LE",
            value=self.target_value)]
        if any(re.search(r"机会|概率|可能性|成功率", self.metrics[c.metric_index].name) for c in success):
            raise ValueError("a chance or probability metric cannot prove that the goal happened")
        for failure in self.failure_conditions:
            metric = self.metrics[failure.metric_index]
            lower, upper = metric.minimum, metric.maximum
            for condition in success:
                if condition.metric_index != failure.metric_index:
                    continue
                if condition.op == "GE":
                    lower = max(lower, condition.value)
                elif condition.op == "LE":
                    upper = min(upper, condition.value)
                else:
                    lower, upper = max(lower, condition.value), min(upper, condition.value)
            if lower <= upper and ((failure.op == "GE" and lower >= failure.value)
                or (failure.op == "LE" and upper <= failure.value)
                or (failure.op == "EQ" and lower == upper == failure.value)):
                raise ValueError("success conditions imply a failure condition")
        capacity_indices = [index for index, metric in enumerate(self.metrics)
            if re.search(r"剩余(?:容量|空间)", metric.name)]
        target_condition = next((condition for condition in success
            if condition.metric_index == self.target_metric_index), None)
        if (capacity_indices and target_condition is not None
                and target_condition.op in ("EQ", "LE", "GE")
                and any(failure.metric_index in capacity_indices and failure.op == "LE"
                    and failure.value == self.metrics[failure.metric_index].minimum
                    for failure in self.failure_conditions)):
            target_change = abs(self.metrics[self.target_metric_index].initial - target_condition.value)
            capacity_total = sum(self.metrics[index].initial - self.metrics[index].minimum
                for index in capacity_indices)
            progress_op = "sub" if target_condition.value < self.metrics[self.target_metric_index].initial else "add"
            related = [action for action in self.actions if any(effect.target == "METRIC"
                and effect.index == self.target_metric_index and effect.op == progress_op
                for effect in action.effects)]
            conserved = bool(related) and all(
                sum(effect.value for effect in action.effects if effect.target == "METRIC"
                    and effect.index == self.target_metric_index and effect.op == progress_op
                    and type(effect.value) is int) ==
                sum(effect.value for effect in action.effects if effect.target == "METRIC"
                    and effect.index in capacity_indices and effect.op == "sub"
                    and type(effect.value) is int)
                for action in related)
            no_capacity_refill = not any(effect.target == "METRIC" and effect.index in capacity_indices
                and effect.op in ("add", "set") for action in self.actions for effect in action.effects)
            no_capacity_refill &= not any(effect.index in capacity_indices and effect.op in ("add", "set")
                for effect in self.settlement_effects)
            if target_change == capacity_total and conserved and no_capacity_refill:
                raise ValueError("success consumes all available capacity but full capacity is a failure condition")
        if not self.success_conditions:
            advances = any((effect.target == "METRIC" and effect.index == self.target_metric_index and
                ((self.target_value > target.initial and effect.op in ("add", "set")) or
                 (self.target_value < target.initial and effect.op in ("sub", "set"))))
                for action in self.actions for effect in action.effects)
            advances |= self.target_value > target.initial and any(
                action.metric_index == self.target_metric_index and action.delta is not None for action in self.actions)
            if not advances:
                raise ValueError("target has no action that advances toward it")
        for condition in self.success_conditions:
            if met(condition):
                continue
            candidates = [effect for action in self.actions for effect in (action.effects or
                [DraftEffect(index=action.metric_index, op="add", value=action.delta)])] + self.settlement_effects
            relevant = [effect for effect in candidates if effect.target == "METRIC" and effect.index == condition.metric_index]
            moves_toward = any((condition.op == "GE" and effect.op == "add")
                or (condition.op == "LE" and effect.op == "sub")
                or (condition.op == "EQ" and effect.op in ("add", "sub", "set")) for effect in relevant)
            if not moves_toward:
                raise ValueError("success condition has no advancing action or environment effect")
            metric = self.metrics[condition.metric_index]
            exact_target = (condition.op == "EQ" or
                (condition.op == "LE" and condition.value == metric.minimum) or
                (condition.op == "GE" and condition.value == metric.maximum))
            if exact_target:
                steps = relevant
                if not any(effect.op == "set" for effect in steps):
                    step_gcd = 0
                    for effect in steps:
                        if effect.op in ("add", "sub") and type(effect.value) is int:
                            step_gcd = gcd(step_gcd, abs(effect.value))
                    if step_gcd and (condition.value - metric.initial) % step_gcd:
                        raise ValueError(f"exact success target unreachable with action step sizes: "
                            f"{metric.name} starts at {metric.initial}, target is {condition.value}, "
                            f"and all changes are multiples of {step_gcd}")
            if any(effect.index == condition.metric_index
                    for effect in self.settlement_effects):
                continue
            needed = abs(condition.value - self.metrics[condition.metric_index].initial)
            direction = "add" if condition.op == "GE" or (condition.op == "EQ" and
                condition.value > self.metrics[condition.metric_index].initial) else "sub"
            if any(not action.effects and action.metric_index == condition.metric_index for action in self.actions):
                continue
            if self.max_rounds is not None and not any(effect.target == "METRIC"
                    and effect.index == condition.metric_index and effect.op == "set"
                    for action in self.actions for effect in action.effects) and not any(
                    effect.index == condition.metric_index and effect.op == "set"
                    for effect in self.settlement_effects):
                per_round_actions = sum(max((effect.value for action in self.actions
                    if action.allowed_agent_indices is None or agent_index in action.allowed_agent_indices
                    for effect in action.effects if effect.target == "METRIC"
                    and effect.index == condition.metric_index and effect.op == direction
                    and type(effect.value) is int and effect.value > 0), default=0)
                    for agent_index in range(len(self.agents)))
                per_round_settlement = sum(effect.value for effect in self.settlement_effects
                    if effect.index == condition.metric_index and effect.op == direction
                    and type(effect.value) is int and effect.value > 0)
                if self.max_rounds * (per_round_actions + per_round_settlement) < needed:
                    upper = self.max_rounds * (per_round_actions + per_round_settlement)
                    raise ValueError(f"success target unreachable within round limit: {metric.name} "
                        f"needs a change of {needed}, but current authorized action effects can change "
                        f"it by at most {upper} in {self.max_rounds} rounds")
            advancing = [(action, effect) for action in self.actions for effect in action.effects
                if effect.target == "METRIC" and effect.index == condition.metric_index
                and effect.op == direction and type(effect.value) is int and effect.value > 0]
            if not advancing or any(any(effect.target == "METRIC" and effect.index == condition.metric_index
                    and effect.op == "set" for effect in action.effects) for action in self.actions):
                continue
            for resource_index, resource in enumerate(self.metrics):
                if resource_index == condition.metric_index:
                    continue
                if any(effect.target == "METRIC" and effect.index == resource_index and effect.op == "set"
                        for action in self.actions for effect in action.effects) or any(
                        effect.index == resource_index and effect.op == "set"
                        for effect in self.settlement_effects):
                    continue
                costs = []
                for action, progress in advancing:
                    consumed = [effect.value for effect in action.effects if effect.target == "METRIC"
                        and effect.index == resource_index and effect.op == "sub"
                        and type(effect.value) is int and effect.value > 0]
                    if not consumed:
                        break
                    costs.append((progress.value, min(consumed)))
                if len(costs) != len(advancing):
                    continue
                replenishment = max((effect.value for action in self.actions for effect in action.effects
                    if effect.target == "METRIC" and effect.index == resource_index
                    and effect.op == "add" and type(effect.value) is int and effect.value > 0), default=0)
                settlement_replenishment = sum(effect.value for effect in self.settlement_effects
                    if effect.index == resource_index and effect.op == "add"
                    and type(effect.value) is int and effect.value > 0)
                if replenishment == settlement_replenishment == 0:
                    if (resource.initial - resource.minimum) * max(progress / cost for progress, cost in costs) < needed:
                        raise ValueError("success target unreachable with nonrenewable resource")
                elif self.max_rounds is not None:
                    turns = self.max_rounds * len(self.agents)
                    budget = resource.initial - resource.minimum + self.max_rounds * settlement_replenishment
                    min_cost = min(cost for _, cost in costs)
                    max_progress = max(progress for progress, _ in costs)
                    progress_turns = min(turns, (budget + turns * replenishment) // (min_cost + replenishment))
                    if progress_turns * max_progress < needed:
                        raise ValueError("success target unreachable after resource replenishment turns")
            if self.max_rounds is not None and self.max_rounds <= 12:
                for resource_index in range(len(self.metrics)):
                    if resource_index == condition.metric_index:
                        continue
                    upper = self._progress_upper_bound_with_resource(
                        condition.metric_index, direction, resource_index, needed)
                    if upper is not None and upper < needed:
                        raise ValueError("success target unreachable with role and resource turns")
        if self.max_rounds is not None:
            round_metrics = {index for index, metric in enumerate(self.metrics)
                if re.search(r"(?:轮数|回合数|剩余轮|剩余回合)", metric.name)}
            settled = {effect.index for effect in self.settlement_effects
                if effect.target == "METRIC" and effect.op in ("add", "sub")}
            acted = {effect.index for action in self.actions for effect in action.effects
                if effect.target == "METRIC" and effect.op in ("add", "sub")}
            if round_metrics & settled & acted:
                raise ValueError("round countdown must not be consumed by both actions and round settlement")
        return self

    def _progress_upper_bound_with_resource(self, target_index: int, direction: str,
            resource_index: int, needed: int) -> int | None:
        """Optimistic one-resource bound; ignores preconditions and other resources."""
        if any(not action.effects and action.metric_index == target_index for action in self.actions):
            return None
        resource = self.metrics[resource_index]
        states = {resource.initial: 0}
        for _round in range(self.max_rounds or 0):
            for agent_index in range(len(self.agents)):
                next_states = dict(states)  # Speaking or doing nothing is always possible.
                for action in self.actions:
                    if action.allowed_agent_indices is not None and agent_index not in action.allowed_agent_indices:
                        continue
                    progress = 0
                    resource_effect = None
                    for effect in action.effects:
                        if effect.target != "METRIC":
                            continue
                        if effect.index == target_index:
                            if effect.op == "set":
                                return None
                            if effect.op == direction and type(effect.value) is int and effect.value > 0:
                                progress += effect.value
                        elif effect.index == resource_index:
                            if effect.op not in ("add", "sub", "set") or type(effect.value) is not int:
                                return None
                            resource_effect = effect
                    if progress == 0 and resource_effect is None:
                        continue
                    for amount, current_progress in states.items():
                        next_amount = amount
                        if resource_effect is not None:
                            if resource_effect.op == "add":
                                next_amount = min(resource.maximum, amount + resource_effect.value)
                            elif resource_effect.op == "sub":
                                next_amount = amount - resource_effect.value
                            else:
                                next_amount = resource_effect.value
                            if not resource.minimum <= next_amount <= resource.maximum:
                                continue
                        next_states[next_amount] = max(next_states.get(next_amount, 0), current_progress + progress)
                states = next_states
                if max(states.values()) >= needed:
                    return needed
            for effect in self.settlement_effects:
                if effect.index == target_index:
                    if effect.op == "set":
                        return None
                    if effect.op == direction and type(effect.value) is int and effect.value > 0:
                        states = {amount: progress + effect.value for amount, progress in states.items()}
                elif effect.index == resource_index and type(effect.value) is int:
                    if effect.op == "add":
                        updated = dict(states)
                        for amount, progress in states.items():
                            next_amount = min(resource.maximum, amount + effect.value)
                            updated[next_amount] = max(updated.get(next_amount, 0), progress)
                        states = updated
                    elif effect.op == "set":
                        updated = dict(states)
                        for amount, progress in states.items():
                            next_amount = max(amount, effect.value)
                            updated[next_amount] = max(updated.get(next_amount, 0), progress)
                        states = updated
            if max(states.values()) >= needed:
                return needed
        return max(states.values())


def _slug(text: str, fallback: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return value[:32] or fallback


def compile_plan(plan: DraftPlan, model_id: str) -> SceneRuntimeConfig:
    metric_ids = [f"metric_{i + 1}" for i in range(len(plan.metrics))]
    fact_ids = [f"fact_{i + 1}" for i in range(len(plan.facts))]
    action_ids = [f"action_{i + 1}" for i in range(len(plan.actions))]
    agent_ids = [f"agent_{i + 1}" for i in range(len(plan.agents))]

    def field_visibility(item):
        return {"audience": item.visibility,
                "audience_ids": [agent_ids[index] for index in item.visible_to_agent_indices]}

    fields = [{"path": key, "type": "int", "min": item.minimum, "max": item.maximum,
               "description": item.name, "unit": item.unit, "visibility": field_visibility(item),
               "owner_id": agent_ids[item.owner_agent_index] if item.owner_agent_index is not None else None,
               "preference": item.preference, "target_min": item.target_min, "target_max": item.target_max}
              for key, item in zip(metric_ids, plan.metrics)]
    fields += [{"path": key, "type": "bool" if type(item.value) is bool else "int" if type(item.value) is int else "str",
                "description": item.name, "visibility": field_visibility(item),
                "owner_id": agent_ids[item.owner_agent_index] if item.owner_agent_index is not None else None}
               for key, item in zip(fact_ids, plan.facts)]

    def compiled_effect(item: DraftEffect) -> dict:
        path = (metric_ids if item.target == "METRIC" else fact_ids)[item.index]
        if item.op == "reveal":
            owner = plan.facts[item.index].owner_agent_index
            recipients = set(item.visible_to_agent_indices)
            if owner is not None:
                recipients.add(owner)
            visibility = ({"audience": "ALL_AGENTS"} if item.reveal_public else
                          {"audience": "SPECIFIC", "audience_ids": [agent_ids[i] for i in sorted(recipients)]})
            return {"op": "reveal", "path": path, "value": visibility}
        if item.target == "METRIC" and (type(item.value) is not int):
            raise ValueError("metric effects require an integer value")
        return {"op": item.op, "path": path, "value": item.value,
                "value_is_expression": False}

    def condition_expr(condition: DraftCondition) -> str:
        symbol = {"GE": ">=", "LE": "<=", "EQ": "=="}[condition.op]
        return f"world['{metric_ids[condition.metric_index]}'] {symbol} {condition.value}"

    actions = []
    for key, item in zip(action_ids, plan.actions):
        effect_items = item.effects or [DraftEffect(index=item.metric_index, op="add", value=item.delta)]
        effects = [compiled_effect(effect) for effect in effect_items]
        rules = [{"expr": condition_expr(condition), "reason_code": "CONDITION_NOT_MET",
                  "public_message": f"需要{plan.metrics[condition.metric_index].name} "
                      f"{ {'GE': '≥', 'LE': '≤', 'EQ': '='}[condition.op]} {condition.value}"} for condition in item.conditions]
        for effect in effects:
            if effect["path"] not in metric_ids or effect["op"] not in ("add", "sub"):
                continue
            metric = plan.metrics[metric_ids.index(effect["path"])]
            delta = effect["value"] if effect["op"] == "add" else -effect["value"]
            rules.append({"expr": f"world['{effect['path']}'] + ({delta}) >= {metric.minimum} and world['{effect['path']}'] + ({delta}) <= {metric.maximum}",
                          "reason_code": "METRIC_OUT_OF_BOUNDS", "public_message": "资源不足或指标达到边界"})
        summary = "、".join(f"{next(f['description'] for f in fields if f['path'] == effect['path'])} " +
            ("公开" if effect["op"] == "reveal" and effect["value"]["audience"] == "ALL_AGENTS" else
             "定向传递" if effect["op"] == "reveal" else
             f"{'+' if effect['op'] == 'add' else '-' if effect['op'] == 'sub' else '设为'}{effect['value']}")
            for effect in effects)
        actions.append({"action_type": key, "kind": "WRITE", "description": f"{item.name}（效果：{summary}）",
            "intent": item.description,
            "params_schema": {"type": "object", "properties": {}, "additionalProperties": False},
            "preconditions": rules, "effects": effects,
            "visibility": {"proposal": {"audience": "ALL_AGENTS"}, "result": {"audience": "ALL_AGENTS"}}})
    hard_conditions = [{"condition_id": f"failure_{index + 1}", "expr": condition_expr(condition),
                        "outcome": "FAILED", "reason": "世界触发失败条件"}
                       for index, condition in enumerate(plan.failure_conditions)]
    success = plan.success_conditions or [DraftCondition(metric_index=plan.target_metric_index,
        op="GE" if plan.target_value > plan.metrics[plan.target_metric_index].initial else "LE", value=plan.target_value)]
    hard_conditions.append({"condition_id": "goal_reached", "expr": " and ".join(f"({condition_expr(c)})" for c in success),
                            "outcome": "GOAL_REACHED", "reason": "所有可验证成功条件已满足"})
    settlement = None
    if plan.settlement_effects:
        settlement_effects = []
        for item in plan.settlement_effects:
            effect = compiled_effect(item)
            if effect["op"] in ("add", "sub"):
                metric = plan.metrics[item.index]
                delta = effect["value"] if effect["op"] == "add" else -effect["value"]
                value = f"working['{effect['path']}'] + ({delta})"
                effect = {"op": "set", "path": effect["path"], "value":
                    f"{metric.minimum} if {value} < {metric.minimum} else "
                    f"{metric.maximum} if {value} > {metric.maximum} else {value}",
                    "value_is_expression": True}
            settlement_effects.append(effect)
        settlement = {"enabled": True, "preconditions": [],
                      "effects": settlement_effects,
                      "visibility": {"audience": "ALL_AGENTS"}}
    scene = SceneRuntimeConfig.model_validate({
        "scene_id": str(uuid.uuid4()), "scene_version": "1", "scenario_name": plan.name,
        "background_story": plan.story,
        "public_story": plan.public_story or (plan.story if not plan.facts else plan.name),
        "interaction_mode": plan.mode,
        "shared_goals": [plan.shared_goal],
        "goal_checks": [{"path": metric_ids[c.metric_index], "op": c.op, "target": c.value,
                         "group": group, "label": plan.metrics[c.metric_index].name}
                        for group, conditions in (("SUCCESS", success), ("FAILURE", plan.failure_conditions))
                        for c in conditions],
        "grounding_rules": ["行动须经规则裁定；仅把授权视图当作已知事实，私有信息可通过正式行动传播。"],
        "world_state_schema": {"schema_version": "1", "fields": fields},
        "initial_world_state": {**{key: item.initial for key, item in zip(metric_ids, plan.metrics)},
                                **{key: item.value for key, item in zip(fact_ids, plan.facts)}},
        "action_specs": actions,
        "agents": [{"agent_id": f"agent_{i + 1}", "display_name": a.name, "persona": a.persona,
                    "public_role": a.role, "personal_goal": a.goal, "model_id": model_id,
                    "allowed_tools": [key for key, action in zip(action_ids, plan.actions)
                        if action.allowed_agent_indices is None or i in action.allowed_agent_indices]}
                   for i, a in enumerate(plan.agents)],
        "settlement_spec": settlement,
        "scheduler_policy": "fixed_round_robin", "random_seed": 42,
        "judging_policy": {"enabled": True, "semantic_judging_enabled": True,
                           "semantic_check_interval_rounds": 2,
                           "judging_dimensions": ["目标达成", "贡献", "共识"], "require_evidence": True},
        "leaderboard_config": {"enabled": True,
            "leaderboard_type": "contribution" if plan.mode == "cooperative" else plan.mode,
            "metrics": [{"metric_id": key, "name": item.name, "source": "DETERMINISTIC",
                         "weight": 1 / max(1, sum(m.preference != "NEUTRAL" for m in plan.metrics))} for key, item in zip(metric_ids, plan.metrics)
                        if item.preference != "NEUTRAL"]},
        "termination_policy": {"max_turns": max(8, len(plan.agents) * 4) if plan.max_rounds is None else None,
            "max_rounds": plan.max_rounds, "round_limit_outcome": plan.round_limit_outcome,
            "hard_conditions": hard_conditions},
        "early_semantic_trigger_policy": {"enabled": False},
    })
    WriteArbiter(scene)
    return scene


def draft_prompt(description: str) -> str:
    return ("Return ONE JSON object only for a small executable multi-agent simulation. Human-readable text is Simplified Chinese. "
        "Required keys: name, story, public_story, mode, shared_goal, agents, metrics, facts, actions, "
        "target_metric_index, target_value, success_conditions, failure_conditions, settlement_effects, max_rounds, round_limit_outcome. "
        "mode MUST be exactly one of cooperative, competitive, mixed. "
        "agents: 2-7 objects {name,role,persona,goal}. metrics: 1-8 objects {name,initial,minimum,maximum,unit,preference}; "
        "Use real-world numeric bounds and units; initial may equal either bound. "
        "preference is HIGHER, LOWER, TARGET_RANGE or NEUTRAL. For TARGET_RANGE include target_min,target_max. "
        "facts: up to 8 objects {name,value,visibility,owner_agent_index}; visibility is PUBLIC, OWNER, SPECIFIC or OPERATOR_ONLY; "
        "for SPECIFIC include visible_to_agent_indices. Keep secrets out of public_story. "
        "actions: 1-12 objects {name,description,allowed_agent_indices,conditions,effects}; agent indices are zero-based. "
        "Each condition is {metric_index,op,value}, op GE,LE,EQ. Each effect is {target,index,op,value}; "
        "target METRIC or FACT; op add,sub,set,reveal. For reveal of a FACT include reveal_public or visible_to_agent_indices. "
        "A targeted reveal must list only intended recipients; do not list every agent unless reveal_public is true. "
        "Descriptions express intent; effects are the only guaranteed results. Include resource consumption as a sub effect "
        "and a required condition. success_conditions are AND; failure_conditions are OR. "
        "Characters can discuss, negotiate, or refuse in speech without an action. Never list a speech-only or refusal-only "
        "action with empty effects; every listed action must make a concrete state change or reveal a fact. "
        "Proposing or refusing a plan must not itself move people, goods, or other physical resources. "
        "Check that goals are reachable: if each unit of progress consumes a resource, provide enough initial units "
        "or a replenishment action. Reusable workers should not be permanently consumed by every move. "
        "Within max_rounds, each agent gets at most one action per round. Ensure the largest possible action progress "
        "times agent count times max_rounds can reach every success threshold, including any turns needed to replenish resources. "
        "Only agents listed in allowed_agent_indices may perform each action; a recovery action also consumes that agent's turn. "
        "Every failure condition must be false at the initial metric values. A condition like remaining_work >= 1 "
        "is NOT a valid failure condition when remaining_work starts above zero. If failure means unfinished at the "
        "round deadline, use max_rounds and omit that failure condition; the engine ends the run at the limit. "
        "Set round_limit_outcome to FAILED if the user says unfinished work at the deadline is failure; "
        "otherwise use MAX_TURNS. "
        "No failure condition may also be true when all success conditions are met. In particular, do not put a "
        "successful progress target into failure_conditions. "
        "Every condition threshold must be within its metric's minimum and maximum; omit an impossible failure "
        "condition instead of using an out-of-range threshold. "
        "settlement_effects run once after every complete round for independent time or crisis evolution; use [] if irrelevant. "
        "max_rounds is the engine's round limit. If a remaining-rounds metric is also decremented in settlement_effects, "
        "do not decrement that metric in any action; one complete round must consume exactly one round. "
        "Use distinct role permissions; do not give every role every action unless the action is genuinely shared. "
        "target_metric_index and target_value provide a simple visible progress indicator even with multiple success conditions. "
        "A chance, probability, or opportunity metric cannot establish actual success. If the goal requires rescue, "
        "evacuation, delivery, or completion, include a metric recording that achieved outcome and require it in success_conditions. "
        "target_value MUST differ from the target metric initial and remain within its bounds. "
        "At least one success condition must be false initially; model essential goals as verifiable world state and actions. "
        "All indices must exist. Do not invent facts omitted by the user. "
        "User description: " + description)
