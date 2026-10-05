# 114 随时介入正在做的任务

2026-10-05，用户请我分析 [firstmate](https://github.com/kunchenguid/firstmate)。我的结论是不把它整个当 Agent App 的底座：它是终端编码 Agent 的发行版，靠 tmux、git worktree 和 PR 工作，中途指挥要往终端输入框里打字。我们直接调用 Codex app-server 的 turn/steer，更可靠。值得借的是四个设计，用户要求全部做完并自己验收（task #20）：

1. 导播台里直接对某块屏上的任务说话、接手这块屏再交还、停止它。
2. 给任务的话先存盘再送，不会丢（firstmate 的 steering inbox）。
3. 停止这类控制动作和说的话分开，并且要确认真的生效（firstmate 的 control plane 和 postcondition）。
4. 服务重启后不丢正在做的工作。

按住说话、电话和导播台共用同一套实现（AGENTS.md：复用优先）。

## 结构

`agent/assistant/task_control.py` 的 `TaskControl` 是唯一的负责者，由 `VoiceAgent` 持有。它收到 Codex 的每条通知，知道每个线程正在跑哪一轮。

- **话（数据面）**：`steer(thread, input)` 先把记录写进 `~/.local/share/rungic-voice-agent/task-control.json` 的 `inbox`，再送：
  - 线程上有一轮在跑：`turn/steer` 并绑定 `expectedTurnId`。
  - 被拒（这一轮刚结束或换了）：最多等 3 秒，等 Codex 通知；没等到就 `thread/read`。有新一轮就并进去，没有就在同一线程 `turn/start` 新一轮，带上这句话。
  - Codex 不在：记录留下。之后每 5 秒重试，共 2 分钟；这个线程下一轮结束时也送；服务启动时再全部送一遍（`flush`）。
  - 每条记录的 id 作为 `clientUserMessageId` 一起发给 Codex。
- **停止（控制面）**：`stop(thread)` 先发 `turn/interrupt`，等 `turn/completed`（10 秒），没等到就 `thread/read` 看这一轮的状态，必要时再中断一次。确认结束才返回 `stopped: true`。停止时还清掉这个线程存着的话。按住说话的停止按钮（`stop_task`）和导播台的停止都走这条路；电话的 `stop_task` 原本就有 stopping 到 stopped 的确认（docs/101）。
- **工作记录**：`turn/started` 时，用户的工作写进同一文件的 `work`，包括对话的主线程、电话的并行任务（及其工作区）和只读任务；后台整理等内部线程不记。`turn/completed` 时删除。服务启动时，文件里还留着的就是被重启切断的工作。
  - 30 分钟内的（以最后一次通知为准），`VoiceAgent.resume_work` 用原线程接着做：主线程打开对话；并行任务重新占一个工作区（优先原来那个），带它的设置 `thread/resume`；然后发一轮 `RESUME_TEXT`，让它先看屏幕和文件，再从停下的地方继续。
  - 组长继续时，团队面板不结束，由组长重新叫起没做完的成员。
  - 更早的工作只在对话里说明，用户说“继续”再接着做。

### 电话（phone_session.py、session.cpp）

- 新任务在主线程上：用 `steer(durable=False)` 开新一轮或并进当前一轮（协调器自己跟踪这个任务，送不出去时直接报错）。
- `steer_task` 的 `turn/steer`：由适配器转给 `steer()`。
  - 那一轮已结束时，返回 `{turn: 新一轮, restarted: true}`，协调器把任务挂到新一轮上，状态回到 running。
  - Codex 不在时，返回 `{queued: true}`，模型被告知话已存下。
- 对账（Reconcile）：
  - 线程记录里是 inProgress、线程状态又是 active，才算在跑；不 active 的是被切断的回合，不再显示为进行中。
  - 主线程上的任务带 `shared` 恢复，不会被当成电话自己的线程（否则主线程的通知会离开按住说话的任务卡）。
  - 已加载的并行或只读线程不会用默认设置重新 resume。
- 已结束任务自己的线程上开始了新一轮（话在结束后送到，或重启后继续），就是这个任务在继续：状态回到 running。
- 并行任务的工作区占用每分钟随进展刷新一次（占用 20 分钟不刷新就失效），任务结束后有新一轮时再占回来。占用记录写明 Codex 线程，导播台据此找到这块屏上的任务。

### 导播台（agent/screen、rungic_cua/hold.py）

- 语音服务的 D-Bus 方法：
  - `ScreenWork(i)`：这块屏上谁在工作。0 和 1 是对话的主线程。其他工作区看占用记录：带 `parent` 的是团队成员，话和停止都交给组长；只有 `thread` 的是电话的并行任务。
  - `SteerScreen(i, s)`、`StopScreen(i)`、`HoldScreen(i, b, s)`。
- `AgentScreen` 每 1.5 秒查一次 `ScreenWork`，并读 `hold-wsN.json`。工具栏在有任务时多出三个按钮：
  - “告诉助理”（document-edit）：在全屏顶部输入，用全屏自己的键盘。
  - “接手 / 交还”（transform-browse）。
  - “停止”（media-playback-stop），只在任务正在做时出现。
- 在助理工作时触摸或打字到它的屏幕，就是接手，免得两边抢同一个指针。离开全屏或关掉窗口时自动交还。窗口持有期间每 60 秒刷新一次接手状态，超过 10 分钟没刷新就失效。
- `rungic_cua.hold`：接手期间，`router.py` 对这块屏上会改变画面的工具调用先等待，最长 120 秒（截图、窗口列表这些只看的调用照常）：
  - 等到交还：这次调用不执行原来的动作，告诉 Agent 屏幕可能变了，先截图再动手，不要撤销用户做的事。
  - 一直没交还：返回“用户还在操作，什么都没做”，Agent 可以再调用来继续等。
  - 交还的消息只说一次，也会附在下一次截图里。
  - 交还时用户说的话走数据面（`steer`），不放在交还记录里。

## 检查

- 离线：
  - `tools/tests/test_task_control.py`：送达、轮次结束后接着做、断开时存盘、停止确认、切断工作的记录。
  - `tools/test_hold.py`：等待、交还消息、过期、router。
  - `tools/tests/test_phone_session.py`：更正碰上轮次结束、重启后主线程仍归按住说话、已加载线程。
  - `tools/tests/test_agent_screen_window.py` 的 `Controls`：按钮、输入条、接手提示、离开全屏交还。
- 无头系统测试（Mac mini）：
  - `phone_session_units`：协调器的新轮次、重开、对账。
  - `screen_controls`：真实 `AgentScreen` 对 D-Bus 替身做接手、告诉、交还、停止。

## 实机验收

2026-10-06，G100 S，开发覆盖 rungic-voice-agent、rungic-cua、rungic-agent-screen（feat/task-control 合上像素画技能分支）。用 `com.rungic.VoiceAgent` 的 D-Bus 方法和导播台界面驱动，记录对照对话日志和 Codex rollout：

1. **主屏的话、接手和停止**（Krita 画灯塔）：
   - 画画中途 `SteerScreen(1)` 补的话直接并进正在跑的那一轮（steered）。
   - 接手 70 秒里，助理的 `desktop_act` 一直在等，交还后才返回；它随后先截图。
   - `StopScreen(1)` 在那一轮结束后才回报 stopped。
   - 另一次任务已经结束时补的话，开了新一轮带上（started），交还的提示也随下一次截图送到。
2. **重启后接着做**（Krita 画雪山小屋）：画到一半时 `systemctl --user restart rungic-voice-agent`，重启 4 秒后对话里出现说明，它看了画面后接着画完并保存。第一次接着做时它改用了英文，已在 `RESUME_TEXT` 里要求用用户的语言，复测为中文。
3. **电话里的并行任务**（2 号屏画橘猫，主屏画樱花）：
   - `ScreenWork(2)` 给出电话的并行任务，补的话直接并进去。
   - 重启服务后，主线程和并行任务都自己接着做，并行任务重新占回 2 号屏，电话的任务列表跟上了新一轮。
   - 两块屏的停止都确认后才回报 stopped，2 号屏随之空出。
4. **导播台界面**：
   - 浮窗工具栏多出“告诉助理”（铅笔）和“停止”。点铅笔进入全屏，顶部出现输入条和键盘，输入的话作为用户消息送到了这块屏上的任务。
   - 全屏里在任务工作时触摸画面即接手，出现“你在操作 · 助理在等 / 交还”。点“交还”后，被挡住的操作返回，助理收到“接手了 90 s”。
   - 离开全屏用 Esc。

验收中另外发现一个原有问题，不在本次范围：按住说话的语音模型把 Agent 自己的完成消息（“要再画一颗月亮来配它吗？”）当成了用户请求，自行委派了一轮（rollout 里的 `realtime_delegation`）。
