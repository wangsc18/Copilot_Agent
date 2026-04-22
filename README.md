# X-Plane 11 Co-Pilot

本项目实现了一个面向 X-Plane 11 的副驾驶 Agent 系统，核心目标是：
- 持续采样飞行状态并识别飞行阶段/风险
- 在 Guard 保护下执行可控动作
- 支持快慢双系统（低延迟规则路径 + LLM 推理路径）
- 支持主动异常检测与告警
- 支持前台先回复、后台继续执行闭环工具

## 目录结构

```text
agent_core/                    核心能力（状态监测、态势推断、工具桥、执行/守卫、主动告警）
code_test/                     单元测试
docs/                          架构文档
xplane_agent_chat_plugin/      X-Plane 插件与桥接
external_agent_chat_ui.py      主入口（UI + 快慢路径 + 工具调用编排）
fast_path_policy.json          快系统策略配置
control_axis_config.json       控制轴映射配置
requirements.txt               Python 依赖
```

## 主要功能

### 1. 快慢双系统

- 快系统：规则驱动，优先处理状态查询与低风险动作，提供低延迟反馈。
- 慢系统：LLM tool-calling 推理与决策，用于复杂指令和风险场景。
- 路由策略由 `fast_path_policy.json` 控制，不需要改代码即可调整。

### 2. 工具层（AgentToolBridge）

暴露给 LLM 的主要工具：
- `get_flight_state`
- `set_throttle`
- `set_roll_cmd`
- `set_pitch_cmd`
- `set_rudder_cmd`
- `set_speedbrake`
- `set_flaps`
- `set_gear`
- `release_brakes`
- `set_target_pitch_deg`（闭环目标俯仰）
- `turn_to_heading`（闭环目标航向）

其中目标类闭环工具支持异步后台执行：前台先回复，后台继续观测控制。

### 3. 主动告警

`ProactiveWatchdog` 持续监测风险，支持防抖与冷却，检测异常后：
- 触发主动告警事件
- 可执行受 Guard 约束的自动缓解动作
- 由慢系统生成用户可读告警文案

### 4. 对话连续性（后台工具）

- 当轮：Agent 会告知“后台正在执行哪些工具”。
- 后台完成：结果写入终端日志并缓存。
- 下一轮：将后台结果摘要并入正常回复，不会突然插入打断对话。

## 快系统策略配置

文件：`fast_path_policy.json`

关键字段：
- `state_query_keywords`：状态查询关键词
- `action_policies`：每个动作的执行模式（`direct` 或 `llm`）
- `max_abs_target_pitch_deg_fast`：快系统允许的最大目标俯仰幅度
- `max_heading_delta_deg_fast`：快系统允许的最大航向改变量
- `blocked_phases_for_fast_control`：这些阶段禁止快执行
- `blocked_risks_for_fast_control`：这些风险存在时禁止快执行

## 控制轴映射配置

文件：`control_axis_config.json`

```json
{
  "roll_cmd_sign": 1.0,
  "pitch_cmd_sign": 1.0,
  "rudder_cmd_sign": 1.0
}
```

说明：
- `roll_cmd_sign`：横滚方向符号
- `pitch_cmd_sign`：俯仰方向符号
- `rudder_cmd_sign`：方向舵方向符号

注意：XPC `sendCTRL` 槽位顺序是 `[pitch, roll, yaw, throttle, gear, flaps, speedbrake]`。

## 运行前置条件

- X-Plane 11
- NASA XPlaneConnect 插件
- Python 3.10+
- `.env` 至少包含：
  - `OPENAI_API_KEY=...`
  - 可选 `OPENAI_BASE_URL=...`

## 安装依赖

```powershell
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 运行

```powershell
python external_agent_chat_ui.py
```

插件桥接可单独运行：

```powershell
cd xplane_agent_chat_plugin
python bridge_agent_chat.py --model gpt-4o-mini
```

## 测试

```powershell
python -m unittest -v `
  code_test/test_external_agent_chat_ui.py `
  code_test/test_agent_tool_bridge.py `
  code_test/test_action_executor_mapping.py `
  code_test/test_proactive_watchdog.py `
  code_test/test_copilot_situation.py `
  xplane_agent_chat_plugin/test_bridge_agent_chat.py
```
