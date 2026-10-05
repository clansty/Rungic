# 115 按住说话改走自己的语音协调器

2026-10-06，task #21。起因：按住说话的语音模型把 Agent 自己的完成消息（“要再画一颗月亮来配它吗？”）当成了用户的请求，自己开了一轮（#41 修了症状）。根源在 Codex 的 Realtime V2 封装：它把 Agent 的输出作为 role=user 的消息放进语音对话，只加 `[BACKEND]` 前缀，然后让模型立即回应（`codex-rs/core/src/realtime_conversation.rs`，`StandaloneHandoff`）；委派工具在任何回应里都能调用。

调研结论（OpenAI Realtime 文档与语音提示指南、OpenAI realtime-agents 的 chat-supervisor、LiveKit、Pipecat）：后台结果不以用户身份进入语音对话。结果作为工具调用的返回值、developer 消息，或者带外回应（`conversation:"none"`）回来；开工只依据用户自己的话。电话模式的协调器（`agent/assistant/session`，docs/101）已经这样做。用户同意（#rungic:a67a9690）：按住说话也改用这个协调器，作为它的一种模式。这是复用，不是另写一份（AGENTS.md）。

## 分工

- **Python（VoiceAgent）照旧负责按住说话的前端**：
  - 麦克风（`android_microphone`）、按下 / 松开 / 取消 / 免提 / 转文字；
  - 回复的播放和出声设备（手机或电视）；
  - “朗读回答”开关；
  - 聊天里的气泡事件（press id）；
  - 进度播报的节奏（`progress_tick`）；
  - 替用户打电话（call_proxy）。
- **C++ 协调器负责语音这一侧的大脑**：
  - 和 OpenAI Realtime 的连接；
  - 一句话的提交和转写；
  - 工具（start_task / steer_task / stop_task / task_status / answer_task / stop_speaking）和任务表；
  - 带外播报（进度、结果、朗读）；
  - 打断时截断回复。
  电话模式原样不动。

一个协调器进程同一时间只有一个会话：`mode` 为 `call`（电话）或 `press`（按住说话）。电话开始前先结束按住说话的会话；电话中按住说话不可用，这一点不变。

## 协调器的 press 模式

- `StartPhoneMode {conversationId, instructions, language, mode:"press"}`：不打开通信音频（不进入 Android 通话模式），关闭服务端的语音活动检测（`turn_detection: null`），由按键决定一句话的起止。
- 命令：
  - `PressStart {press, playedMs}`：新的一句。打断正在说的回复，按用户实际听到的长度截断。
  - `PressAudio {pcm}`：24 kHz 单声道 16 位，按住期间实时上传。以前要等松开才发，因为 Codex 封装不能清空输入；现在能清空，所以边按边发，延迟更低。
  - `PressCommit`：松开，提交这一句。转写出来以后才把它当成一句话处理（沿用电话的提交逻辑）。
  - `PressCancel`：清空，什么都不发。
- 事件（只发给 VoiceAgent，App 看不到通话条）：
  - `voice-state`：连接状态；
  - `voice-audio`：回复音频，收到就转发，播放节奏由 GStreamer 管；
  - `voice-flush`：打断，停止播放；
  - `voice-delta`：转写增量；
  - `message`：用户和助理的完整句子，带 press id；
  - `aloud`：朗读的转写，只出声不显示。
- `Narrate {text, quiet, exact}`：带外说一句话。`quiet` 是需要的安静时长（电话里默认 12 秒；按住说话由 progress_tick 控制节奏，传 0）；`exact` 表示原样朗读（“朗读”按钮）。

## 结果怎么说

对话主线程（按住说话、打字、电话里的动手任务都在这条线程上）的一轮结束时，由 VoiceAgent 统一说结果：用带外回应，结果在指令里，没有工具可用，结尾是提问就问用户、等回答。协调器的任务通知只说它自己线程上的任务（只读任务、并行任务），避免同一个结果说两遍。

## 提示词

`phone.md` 改为两种方式共用：开头说明用户在通话中或按住按钮和你说话，协调器在会话开始时注明是哪一种。`realtime.md`（Codex 封装的提示词）不再使用，删除。

## 迁移清单

见 task #21 盘点：Codex `thread/realtime/*` 的每个用处都有对应：
- start / stop → StartPhoneMode / StopPhoneMode（press）；
- appendAudio → PressAudio；
- appendSpeech → Narrate；
- outputAudio、转写通知 → voice-* 事件；
- 委派工具 → start_task / steer_task；
- 最终结果 → VoiceAgent 的结果播报。

## 检查

- 离线：
  - 协调器的 press 模式单元测试（session-test.cpp）；
  - VoiceAgent 对协调器替身的按住说话流程（test_voice_agent_service：按、松、取消、打断、朗读、结果播报、不会自己开工）。
- 实机（G100 S）：
  - 按住说话交代任务；
  - 中途打断；
  - 画完问“要不要再画一个”，它不会自己开工，用户说“好”才开工；
  - 朗读；
  - 电视上按住说话，从电视出声；
  - 免提；
  - 转文字；
  - 按住说话和电话之间来回切换。
