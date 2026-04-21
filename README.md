# X-Plane 11 Co-Pilot (XPC + LLM Tools)

本仓库用于构建一个可感知飞行状态、可进行受控动作执行的 X-Plane 11 Co-Pilot。
当前主链路已经支持：
- 持续采样 X-Plane 状态（XPC）
- 态势推断（phase / confidence / risks / evidence）
- 外部聊天 UI（Tkinter）
- LLM 工具调用（读取状态 + 受 Guard 保护的动作执行）
- 向 X-Plane 插件发送摘要 overlay

## 1. 当前推荐入口

```powershell
python external_agent_chat_ui.py
```

该入口同时包含：
1. 聊天 UI（气泡样式、状态灯、DPI 优化）
2. 监控采样与态势推断
3. LLM 工具调用（tool call）
4. Guard + Executor 的受控执行

## 2. 目录结构（精简视角）

```text
agent_core/                    # 核心能力：状态采样、态势推断、Guard/Executor
code_test/                     # 单元测试
docs/                          # 架构说明
xplane_agent_chat_plugin/      # X-Plane 内部聊天插件桥接代码
external_agent_chat_ui.py      # 主入口：外部聊天 + tool calling + 执行链路
xplane_copilot_demo.py         # 早期 XPC 基础读写 demo
xplane_llm_agent.py            # 早期 LLM 周期播报 demo
xplane_auto_takeoff.py         # 独立自动起飞脚本（实验性）
```

## 3. Tool Calling 能力（已接入）

`external_agent_chat_ui.py` 当前暴露以下工具给模型：
- `get_flight_state`
- `set_throttle`
- `set_flaps`
- `set_gear`
- `release_brakes`

执行路径：
`LLM tool call -> AgentToolBridge -> ActionGuard -> ActionExecutor -> XPC`

## 4. 运行前置条件

- X-Plane 11 已安装
- NASA XPlaneConnect 插件已安装并启用（默认端口 `49009`）
- Python 3.10+
- `.env` 中至少包含：
  - `OPENAI_API_KEY=...`
  - 可选 `OPENAI_BASE_URL=...`

依赖安装：

```powershell
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 5. 测试

推荐最小测试集：

```powershell
python -m unittest -v ^
  code_test/test_external_agent_chat_ui.py ^
  code_test/test_agent_tool_bridge.py ^
  code_test/test_copilot_situation.py ^
  code_test/test_xpc_text_encoding.py
```

## 6. Demo 脚本保留建议（用于仓库精简）

### 建议保留
1. `external_agent_chat_ui.py`
- 当前主入口，覆盖你们现在的核心目标（聊天 + 工具调用 + 受控执行）。

2. `agent_core/` 全部
- 这是系统核心层，且已被主流程直接使用。

### 可考虑删除（若目标是聚焦“主产品链路”）
1. `xplane_copilot_demo.py`
- 仅早期基础读写示例，功能已被 `agent_core + external_agent_chat_ui.py` 覆盖。
- 注意：删除后需同步删除/调整 `code_test/test_xplane_copilot_demo.py`。

2. `xplane_llm_agent.py`
- 属于旧版“定时播报”路径，与当前工具调用式 Agent 重叠。
- 注意：删除后需同步删除/调整 `code_test/test_xplane_llm_agent.py`。

### 视团队目标决定是否保留
1. `xplane_auto_takeoff.py`
- 这是独立任务脚本，不在当前聊天 Agent 主链路中。
- 若你们后续计划把“自动起飞”作为可调用任务/工具，建议保留并改造成 `agent_core` 的能力模块。
- 若短期不做自动化飞行，可删以降低维护负担。

## 7. 下一步建议（重构方向）

如果你准备进一步精简仓库，推荐按顺序执行：
1. 先删 `xplane_copilot_demo.py` 和 `xplane_llm_agent.py`（并删对应测试）
2. 将 `xplane_auto_takeoff.py` 决策为“并入 agent_core”或“彻底移除”
3. 同步更新 `docs/AGENT_ARCHITECTURE.md`，确保文档只描述当前真实链路
