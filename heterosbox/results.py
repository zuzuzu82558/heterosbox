"""Evidence-backed deterministic leaderboard for configured numeric world metrics."""
from __future__ import annotations

from collections import defaultdict

from .models import SceneRuntimeConfig, SimEvent


def _read_path(world: dict, path: str):
    value = world
    for piece in path.split("."):
        value = value[piece]
    return value


def leaderboard(scene: SceneRuntimeConfig, events: list[SimEvent]) -> dict | None:
    config = scene.leaderboard_config
    if not config.enabled:
        return None
    fields = {field.path: field for field in scene.world_state_schema.fields}
    metrics = [metric for metric in config.metrics if metric.source == "DETERMINISTIC"
               and metric.metric_id in fields and fields[metric.metric_id].type in ("int", "float")
               and fields[metric.metric_id].preference != "NEUTRAL"]
    if not metrics:
        return {"status": "UNAVAILABLE", "reason": "没有可计算的确定性指标", "rows": []}
    previous = {metric.metric_id: _read_path(scene.initial_world_state, metric.metric_id) for metric in metrics}
    scores = defaultdict(float)
    evidence = defaultdict(list)
    breakdown = defaultdict(lambda: defaultdict(float))
    agents = {agent.agent_id for agent in scene.agents}
    for event in events:
        if event.type.value != "world_state_delta":
            continue
        for metric in metrics:
            path = metric.metric_id
            if path not in event.payload.get("changes", {}):
                continue
            value = event.payload["changes"][path]
            old = previous[path]
            previous[path] = value
            if event.actor_id in agents and isinstance(value, (int, float)) and isinstance(old, (int, float)):
                field = fields[path]
                if field.preference == "HIGHER":
                    improvement = value - old
                elif field.preference == "LOWER":
                    improvement = old - value
                else:
                    distance = lambda number: max(field.target_min - number, 0, number - field.target_max)
                    improvement = distance(old) - distance(value)
                if improvement:
                    contribution = improvement * metric.weight
                    scores[event.actor_id] += contribution
                    breakdown[event.actor_id][path] += contribution
                    evidence[event.actor_id].append(event.event_id)
    rows = []
    for agent in scene.agents:
        rows.append({"agent_id": agent.agent_id, "name": agent.display_name,
                     "score": round(scores[agent.agent_id], 3),
                     "breakdown": {key: round(value, 3) for key, value in breakdown[agent.agent_id].items()},
                     "evidence_refs": evidence[agent.agent_id]})
    rows.sort(key=lambda item: (-item["score"], item["name"]))
    rank = 0
    last = None
    for index, row in enumerate(rows):
        if row["score"] != last:
            rank = index + 1
            last = row["score"]
        row["rank"] = rank
    return {"status": "READY", "type": config.leaderboard_type,
            "method": "按指标改善方向或目标区间累计实际变化；有害变化扣分，不计发言次数。",
            "metrics": [metric.model_dump() for metric in metrics], "rows": rows}
