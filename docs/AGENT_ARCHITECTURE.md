# X-Plane Co-Pilot Agent 架构说明

## 1. 目标

本架构用于让 Agent 从“仅聊天”升级到“可理解飞行状态、可给出有依据建议、可在安全约束下辅助操控”。

核心原则：

- 分层解耦：感知、态势理解、决策、执行隔离
- 安全优先：执行前必须经过 Guard 校验
- 可追溯：每条建议都应有 evidence 支撑

## 2. 目录结构

```text
agent_core/
  __init__.py
  copilot_core.py              # 核心数据结构与枚举
  copilot_state_monitor.py     # XPC持续采样与窗口缓存
  copilot_situation.py         # 飞行阶段推断与风险识别
  copilot_guard_executor.py    # 动作白名单校验与执行器

code_test/
  test_external_agent_chat_ui.py
  test_copilot_situation.py
  test_xpc_text_encoding.py
  test_xplane_copilot_demo.py
  test_xplane_llm_agent.py

docs/
  AGENT_ARCHITECTURE.md

external_agent_chat_ui.py      # 外部主聊天界面（注入state_context）
```

## 3. 模块职责

### 3.1 `agent_core/copilot_state_monitor.py`

- 持续通过 XPC 采样（默认 2Hz）
- 缓存最近 30 秒 `FlightSnapshot`
- 提供 `get_latest()` / `get_window(10s|30s)` 给上层使用

### 3.2 `agent_core/copilot_situation.py`

- 计算趋势特征 `TrendMetrics`
- 阶段推断（评分模型）：
  - `ground_hold`
  - `takeoff_roll`
  - `initial_climb`
  - `cruise`
  - `approach`
  - `landing_roll`
- 风险识别：
  - `stall_risk`
  - `overspeed_risk`
  - `throttle_ineffective`
  - `unstable_approach`
  - `runway_excursion_risk`
- 输出 `SituationReport {phase, confidence, evidence, risks}`

### 3.3 `agent_core/copilot_guard_executor.py`

- `ActionGuard`：动作类型、参数范围、场景约束校验
- `ActionExecutor`：将通过校验的动作映射到 XPC 控制命令
- 当前支持动作：
  - `SET_THROTTLE`
  - `SET_PITCH_CMD`
  - `SET_GEAR`
  - `SET_FLAPS`
  - `RELEASE_BRAKES`

### 3.4 `external_agent_chat_ui.py`

- 外部详细对话 UI
- 每次用户发问时注入 `state_context`（来自 `SituationReport`）
- 要求模型返回：
  - `reply`：外部详细回复
  - `overlay`：X-Plane 内摘要短句
- 摘要通过 UDP `AGENT|...` 发送到 X-Plane 内部聊天插件

## 4. 数据流

1. Monitor 持续采样 XPC -> 快照窗口  
2. Situation 引擎在用户发问时推断 phase/risk/evidence  
3. UI 将 `state_context` 注入 LLM prompt  
4. LLM 输出 `reply + overlay`  
5. `overlay` 发往 X-Plane 内部聊天插件  
6. （后续）若启用辅助操控：`ActionPlan -> Guard -> Executor -> XPC`

## 5. 为什么这套架构更稳

- Agent 不直接“猜”状态，而是消费结构化 `SituationReport`
- 建议与执行解耦，避免误判直接触发危险操作
- 每层都可独立测试，便于定位与迭代

## 6. 建议后续迭代

- 加入阶段迟滞（hysteresis）与最小驻留时间，降低 phase 抖动
- 引入机型参数配置（Vr/Vref/襟翼策略）
- 在同一架构上接入语音输入（替换输入通道，不改决策链路）
