# 安全策略

## 支持范围

目前只维护 `main` 分支的最新版本。

## 报告漏洞

如果发现安全问题（例如密钥泄露、越权读取私有状态、表达式沙箱逃逸），请**不要**创建公开 Issue，而是通过 GitHub 的 [私密漏洞报告](https://github.com/zuzuzu82558/heterosbox/security/advisories/new) 提交，并尽量附上复现步骤和影响范围。

## 已知边界

- 当前 API 使用 `X-Principal-Id: local-owner` 作为本机开发身份，**不适合直接部署到公网**。对外部署前必须接入正式的认证与授权，并限制网络入口。
- 模型密钥只应通过环境变量、`.env.local` 或 `--prompt-key` 提供，切勿写入场景 JSON、源码或提交到仓库。
