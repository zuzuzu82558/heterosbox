"""Restricted expression profile shared by dry runs and committed transactions."""
import ast
import copy
import math
from types import MappingProxyType
from typing import Any

from simpleeval import SimpleEval


class ExpressionError(ValueError):
    pass


def freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({k: freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(freeze(v) for v in value)
    return copy.deepcopy(value)


class _BoundedSimpleEval(SimpleEval):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.steps = 0

    def _eval(self, node):
        self.steps += 1
        if self.steps > 2048:
            raise ExpressionError("max_eval_steps exceeded")
        return super()._eval(node)


class ExpressionEvaluator:
    _allowed = (ast.Expression, ast.Constant, ast.Name, ast.Load, ast.Subscript,
                ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp,
                ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod,
                ast.USub, ast.UAdd, ast.Not, ast.And, ast.Or, ast.Eq,
                ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In,
                ast.NotIn, ast.List, ast.Tuple, ast.Dict)

    def evaluate(self, expression: str, context: dict[str, Any], *, phase: str) -> Any:
        try:
            for value in context.values():
                self._check_value(value)
            parsed = ast.parse(expression, mode="eval")
            nodes = list(ast.walk(parsed))
            if len(nodes) > 256 or any(not isinstance(n, self._allowed) for n in nodes):
                raise ExpressionError("disallowed expression or max_expr_nodes exceeded")
            if phase == "precondition" and any(isinstance(n, ast.Name) and n.id == "working" for n in nodes):
                raise ExpressionError("precondition cannot read working")
            if any(isinstance(n, ast.Name) and n.id not in context for n in nodes):
                raise ExpressionError("unknown name")
            evaluator = _BoundedSimpleEval(names=context, functions={}, allowed_attrs=[])
            result = evaluator.eval(expression)
            self._check_value(result)
            return result
        except (SyntaxError, TypeError, ValueError, KeyError, IndexError, ArithmeticError, Exception) as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise ExpressionError(f"{phase}: {exc}") from exc

    def _check_value(self, value: Any):
        if isinstance(value, int) and not isinstance(value, bool) and not -(2**63) <= value < 2**63:
            raise ExpressionError("int64 overflow")
        if isinstance(value, float) and not math.isfinite(value):
            raise ExpressionError("non-finite float")
        if isinstance(value, (list, tuple, dict, MappingProxyType)):
            if len(value) > 1024:
                raise ExpressionError("max_collection_scan exceeded")
            iterable = value.values() if isinstance(value, (dict, MappingProxyType)) else value
            for item in iterable:
                self._check_value(item)
