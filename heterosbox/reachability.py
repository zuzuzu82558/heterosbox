"""Bounded, optimistic search for a generated scene's deterministic goal path."""
from __future__ import annotations

import json
import re
import time

from .arbiter import SceneEvaluationError, WriteArbiter
from .expr import ExpressionError, freeze
from .models import ActionPayload, SceneRuntimeConfig


def goal_reachable(scene: SceneRuntimeConfig, *, max_states: int = 20000,
        max_seconds: float = 4.0) -> bool | None:
    """Return False only after an exhaustive search; None means the search was skipped.

    Agents may choose any authorized no-argument WRITE or do nothing. This ignores
    what an agent knows or chooses, so a failed search proves that model behavior
    cannot reach the deterministic goal under the same action rules.
    """
    rounds = scene.termination_policy.max_rounds
    goals = [item for item in scene.termination_policy.hard_conditions if item.outcome == "GOAL_REACHED"]
    if (rounds is None or rounds > 12 or scene.scheduler_policy != "fixed_round_robin"
            or not goals or any(action.params_schema.get("properties") or action.params_schema.get("required")
                for action in scene.action_specs)
            or any(effect.value_is_expression for action in scene.action_specs
                for effect in action.effects if effect.op != "reveal")):
        return None
    arbiter = WriteArbiter(scene)
    deadline = time.monotonic() + max_seconds

    read_paths = set()
    expressions = [condition.expr for condition in scene.termination_policy.hard_conditions]
    expressions.extend(rule.expr for action in scene.action_specs for rule in action.preconditions)
    if scene.settlement_spec:
        expressions.extend(rule.expr for rule in scene.settlement_spec.preconditions)
    for expression in expressions:
        read_paths.update(re.findall(r"(?:world|working)\[['\"]([^'\"]+)['\"]\]", expression))

    def key(world: dict) -> str:
        return json.dumps({path: world[path] for path in read_paths}, ensure_ascii=False,
            sort_keys=True, separators=(",", ":"))

    def outcome(world: dict, round_index: int, turn_index: int) -> str | None:
        context = {"world": freeze(world), "working": freeze(world), "params": freeze({}),
            "actor": "__system__", "session": freeze({"round_index": round_index,
                "turn_index": turn_index, "branch_id": "main"})}
        for condition in scene.termination_policy.hard_conditions:
            if arbiter.evaluator.evaluate(condition.expr, context, phase="termination"):
                return condition.outcome
        return None

    states = {key(scene.initial_world_state): scene.initial_world_state}
    try:
        if outcome(scene.initial_world_state, 0, 0) == "GOAL_REACHED":
            return True
        for round_index in range(rounds):
            for offset, agent in enumerate(scene.agents):
                turn_index = round_index * len(scene.agents) + offset
                next_states = {}
                for world in states.values():
                    if time.monotonic() >= deadline:
                        return None
                    choices = [world]
                    for action in scene.action_specs:
                        if action.kind != "WRITE" or action.action_type not in agent.allowed_tools:
                            continue
                        result = arbiter.execute(world, ActionPayload(action_type=action.action_type),
                            agent.agent_id, {"round_index": round_index, "turn_index": turn_index,
                                "branch_id": "main"})
                        if result.feedback.status == "ACCEPTED":
                            choices.append(result.world_state)
                    for candidate in choices:
                        ending = outcome(candidate, round_index, turn_index + 1)
                        if ending == "GOAL_REACHED":
                            return True
                        if ending is None:
                            next_states[key(candidate)] = candidate
                if len(next_states) > max_states:
                    return None
                states = next_states
                if not states:
                    return False
            settled = {}
            for world in states.values():
                if time.monotonic() >= deadline:
                    return None
                result = arbiter.settle(world, {"round_index": round_index,
                    "turn_index": (round_index + 1) * len(scene.agents), "branch_id": "main"})
                candidate = result.world_state if result.feedback.status == "ACCEPTED" else world
                ending = outcome(candidate, round_index, (round_index + 1) * len(scene.agents))
                if ending == "GOAL_REACHED":
                    return True
                if ending is None:
                    settled[key(candidate)] = candidate
            if len(settled) > max_states:
                return None
            states = settled
            if not states:
                return False
    except (SceneEvaluationError, ExpressionError, ValueError):
        return None
    return False
