# Voice Agent Execution Plan

## Objective
在不破坏现有文本 Agent 的前提下，增量交付实时语音交互能力，优先满足：
1. 1s 级响应
2. 自然交互与可打断
3. 说话人识别与说话状态识别可扩展

## Milestones
1. M1 - Skeleton and Integration
- 新建 `voice_agent/` 并落地状态机、事件模型、配置模型
- 接入 `external_agent_chat_ui.py`，默认关闭语音开关
- 完成基础单元测试

2. M2 - Main Realtime Path
- 接入实时语音主通道 SDK（WebRTC/WebSocket）
- 跑通音频输入、回复输出、barge-in
- 建立首包和端到端延迟打点

3. M3 - Sidecar Intelligence
- 接入流式 ASR 与 diarization
- 建立事件融合策略并回注状态机
- 增加日志与可观测指标

4. M4 - Hardening
- 压测与参数调优（chunk/buffer/vad）
- 完成回滚演练与运行手册
- 达到验收阈值并灰度启用

## Acceptance Criteria
- 首包响应 P50 <= 700ms，P95 <= 1200ms（本地网络）
- 打断成功率 >= 95%
- 出错时可自动降级至文本模式

