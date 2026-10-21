# 贡献指南

欢迎提交 Issue 和 Pull Request。

## 开发环境

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/python -m pytest -q -p no:cacheprovider
```

Windows 下请把 `.venv/bin/` 换成 `.venv\Scripts\`。测试完全离线，不需要任何模型密钥。

## 提交 PR 前

1. 从 `main` 新建分支，一个 PR 只解决一件事。
2. 修复 Bug 时附带能复现该 Bug 的回归测试；新增功能附带对应测试。
3. 本地运行完整测试并确保通过；CI 会在 Ubuntu 和 Windows 上重复验证。
4. 不要提交密钥、`.env.local`、SQLite 数据库或 `test_output/` 下的产物。
5. PR 描述写清楚：问题、修改内容、如何验证、尚未覆盖的风险。

## 报告问题

请在 Issue 中说明运行环境（操作系统、Python 版本）、复现步骤、预期结果和实际结果。涉及安全问题请参考 [SECURITY.md](SECURITY.md)，不要公开披露。
