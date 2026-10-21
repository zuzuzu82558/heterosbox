# 拟界（HeteroSbox）

[![CI](https://github.com/zuzuzu82558/heterosbox/actions/workflows/ci.yml/badge.svg)](https://github.com/zuzuzu82558/heterosbox/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

拟界是一个多智能体推演沙盒：用自然语言或 JSON 描述场景，让多个由大模型驱动的角色在受约束的世界规则下行动，并提供观战、回放、裁判和排行。

项目包含 Python 3.11+ 后端与桌面网页，按内部架构设计文档（V1.8.2，未随仓库公开）实现。前端位于 `frontend/`，由 FastAPI 同源提供，不需要 npm 安装或前端构建步骤。

## 桌面网页

运行服务后打开 `http://127.0.0.1:8000/app/`。页面提供首页、自然语言生成初稿、逐项配置与完整 JSON 编辑、角色模型分配、可保存的舞台样式、推演观战、视角切换、事件时间线、结果排行及证据回放。窄屏布局也可使用，但舞台按桌面操作设计。

服务端新增 `GET /models`（从已配置的 DeepSeek 账号取得可用模型）、`POST /scenes/draft`、`POST /scenes/validate`、`GET/PUT /scenes/{id}/visual`、`GET /sessions`、`GET /sessions/{id}/overview` 与 `GET /sessions/{id}/report`。新场景的可执行规则由受约束的场景编译器生成；模型只提供故事、角色、指标和动作语义，不能直接写执行表达式。配置在启动前验证，浏览器从服务端授权快照与事件读取观战数据。

自然语言生成和真实推演需要至少配置一个模型密钥：`DEEPSEEK_API_KEY`、`MIMO_API_KEY` 或 `GEMINI_API_KEY`。网页不会读取或存储密钥。模型列表按厂商展示原始英文模型 ID；MiMo 与 Google 模型在场景中分别保存为 `mimo:<model>` 和 `google:<model>`，旧场景的 DeepSeek ID 保持兼容。`GET /models` 按厂商独立检查，某个连接失败不会隐藏其他厂商的模型。结果缓存 60 秒，以减少重复请求。当前身份仍是本机开发用的 `local-owner`；对外部署前必须替换为正式认证与授权，并限制网络入口。

结果中的 Arbiter 统计来自真实裁定事件；Judger 使用独立的系统判断并过滤无效证据引用；Leaderboard 按指标的改善方向或目标区间累计已提交的变化，有害变化扣分。若场景未启用或没有可计算指标，页面显示不可用状态，不生成占位分数。语义判断调用失败会留下模型错误事件，该次会话继续按确定性规则与上限推演，并在结果中说明语义裁判不可用。

## 场景规则与时间口径

自然语言初稿的结构化计划现可表达角色专属行动、公开与私有事实、定向信息揭示、一个行动的多个 Effect、资源消耗、指标改善方向、多条件成功与失败，以及每轮独立执行的环境结算。Compiler 只把受限结构编译为执行规则；行动的自然语言意图单独保存，界面中的“可执行说明”以结构化 Effect 为准。无法表达的设定需要在草案中补充或修改，不能把故事文字当成已执行的世界规则。

一个 Round 是所有角色各获得一次 Turn 后的完整轮次，随后执行一次环境结算。Turn 是一个角色的决策机会，即使该角色没有提交 Action 也计数；Action 是其中提交的具体调用。`max_turns` 限制角色决策机会数，`max_rounds` 限制完整轮次。回放接口和事件接口都支持 `at_seq=N`，会按所选事件序列重建当时的授权视图。

## 已实现

- Pydantic V2 场景模型与启动前校验；`EventType` 从唯一 Event Registry 动态生成。
- 受限表达式、只读 `world`、顺序更新的 `working`、原子 WRITE / Settlement 与 invariant 校验。
- 按字段、事件和私有状态投影的 ACL；默认不追溯的 Reveal，以及显式追溯的历史查询。
- 统一模型网关、READ Loop、动作修正、Speech Commit 和私有 Observation 提交；DeepSeek Chat Completions 适配器。
- PAUSE / STEP / RESUME / STOP、预算暂停、Checkpoint、SQLite 事件与快照、恢复及分支基础。
- FastAPI REST 控制、SSE 与 WebSocket 事件流。默认仅监听本机地址。

## 运行

Linux / macOS：

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/python -m heterosbox validate examples/cooperative.json
.venv/bin/python -m heterosbox serve --prompt-key
```

Windows（PowerShell，使用 uv）：

```powershell
uv venv .venv --python 3.12
uv pip install --python .venv\Scripts\python.exe -e '.[test]'
.venv\Scripts\python.exe -m heterosbox validate examples\cooperative.json
.venv\Scripts\python.exe -m heterosbox serve --prompt-key
```

`--prompt-key` 从终端无回显读取 DeepSeek Key，只放入该服务进程内存。也可以设置上述任意环境变量，或把它们逐行放在项目根目录的 `.env.local` 中。该文件已加入 `.gitignore`，只用于本机开发，请勿分享。MiMo 默认使用官方按量付费 OpenAI 兼容地址；若你的 Token Plan 要求专属地址，可设置 `MIMO_BASE_URL` 为控制台提供的 OpenAI 兼容地址。服务默认位于 `http://127.0.0.1:8000`，API 文档位于 `/docs`。不要将 Key 写进场景 JSON、源码或 HTTP 请求。示例场景是 `examples/cooperative.json` 和 `examples/competitive.json`。

本地 API 采用 `X-Principal-Id: local-owner` 身份头，仅适合本机开发。若部署到其他主机，必须接入真正的身份认证与授权系统，并限制网络入口。

创建暂停的 Session：

```powershell
$scene = Get-Content examples\cooperative.json -Raw | ConvertFrom-Json
$body = @{scene=$scene; paused=$true} | ConvertTo-Json -Depth 30
$headers = @{'X-Principal-Id'='local-owner'}
$created = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/sessions -Headers $headers -ContentType 'application/json' -Body $body
$created
```

控制命令为 `POST /sessions/{session_id}/commands`，请求体示例 `{"name":"STEP"}`。快照为 `GET /sessions/{session_id}/snapshot?perspective=AGENT:builder`；只读历史重建为 `GET /sessions/{session_id}/replay?at_seq=N&perspective=AGENT:builder`；事件历史为 `GET /sessions/{session_id}/events?after_seq=N`，历史事件可加 `at_seq=N`；SSE 为 `/stream?after_seq=N`；WebSocket 为 `/ws?after_seq=N`。`POST /sessions/{session_id}/fork` 接受 `{"checkpoint_id":"..."}`，返回独立 `branch_id`；后续接口传入 `branch_id` 查询参数即可访问该分支。切换视角时重新取该视角的快照，以其 `event_seq` 作为续流游标。

## 测试

```powershell
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --cov=heterosbox --cov-report=term
```

测试完全离线运行，不需要模型密钥，也不会调用任何付费 API。Linux / macOS 下把 `.venv\Scripts\python.exe` 换成 `.venv/bin/python` 即可。每次推送和 PR 都会在 GitHub Actions 上以 Ubuntu / Windows × Python 3.11 / 3.12 运行同一套测试。

测试覆盖 ACL Canary、事件揭示、原子事务、READ Loop、动作失败发言抑制、STEP Round 边界、预算阻塞、重启恢复、在途 STOP 与 DeepSeek 协议解析，并包含“地底三十日”固定回归场景，验证角色权限、私有事实、定向传播、多 Effect、资源、环境结算、三种终局和排行方向。DeepSeek 协议测试使用模拟 HTTP，不消耗真实模型额度。

## 当前边界

这是一套可运行的本机产品基础，尚未覆盖架构设计的全部扩展契约：TAKEOVER / OPERATOR_EVENT 的完整命令流程、生产级认证、Blackboard 压缩、语义指标排行、供应商级费用计量与所有断线保留窗口策略仍需继续实现。当前排行榜只支持有明确世界字段映射的确定性数值指标。SQLite 的恢复以已保存的 Checkpoint 为基础；生产环境还需故障注入验证事务持久化边界。

## 许可证

本项目基于 [MIT 许可证](LICENSE) 开源。
