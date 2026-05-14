# Voice Agent Runbook

## Startup
1. 设置环境变量
2. 启动 `external_agent_chat_ui.py`
3. 检查终端日志中 `[voice] enabled` 状态

## Troubleshooting
1. No voice state updates
- 检查 `VOICE_ENABLED=true`
- 检查日志是否持续输出状态刷新

2. Slow response
- 降低音频分片大小（`VOICE_FRAME_MS`）
- 调整 VAD 静默阈值（`VOICE_VAD_SILENCE_MS`）

3. Runtime errors in voice path
- 启用 `VOICE_FALLBACK_TEXT=true`
- 保持文本模式服务可用

## Rollback
1. 设置 `VOICE_ENABLED=false`
2. 重启应用
3. 验证文本链路正常收发

