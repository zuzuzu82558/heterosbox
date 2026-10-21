"""受限表达式：AST 白名单允许的集合字面量必须真正可求值，且仍受资源上限约束。"""
import pytest

from heterosbox.expr import ExpressionError, ExpressionEvaluator, freeze


@pytest.mark.parametrize("expression, expected", [
    ("1 in [1, 2]", True),
    ("world['stage'] in ('repair', 'done')", True),
    ("world['stage'] not in ['idle']", True),
    ("{'low': 1, 'high': 3}['high']", 3),
    ("(10, 20)[1]", 20),
])
def test_collection_literals_evaluate(expression, expected):
    context = {"world": freeze({"stage": "repair"})}
    assert ExpressionEvaluator().evaluate(expression, context, phase="effect") == expected


@pytest.mark.parametrize("expression", [
    "[0] * 50000000",       # 大列表分配被拒绝
    "(0,) * 50000000",
    "[0] * 2000",           # 超过 max_collection_scan
    "[x for x in [1, 2]]",  # 推导式不在白名单
    "{1, 2}",               # 集合字面量不在白名单
    "world.keys()",         # 属性与调用不在白名单
])
def test_collection_limits_still_enforced(expression):
    with pytest.raises(ExpressionError):
        ExpressionEvaluator().evaluate(expression, {"world": freeze({})}, phase="effect")
