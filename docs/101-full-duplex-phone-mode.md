# Agent 电话模式（2026-10-02）

本次按用户要求继续使用 `gpt-realtime-2.1-mini` 和 Codex。电话模式持续采集与播放；用户开口先停止播报，已有任务继续。只有明确的停止任务请求或任务卡片的停止按钮取消执行。挂断会关闭话音连接；和模型的网络连接失败时也关闭，任务调度器继续运行，恢复必须再次点击电话模式。通话音频卡顿或断开时不结束通话，而是自动恢复（2026-10-05 起，见文末「通话音频以恢复为目标」）。锁屏或 Plasma 退到后台时通话继续，像打电话一样（2026-10-05 起，见文末「锁屏继续通话」；此前 Plasma 隐藏也会关闭话音连接）。

## 选型依据与边界

本地核对 Codex 0.156.1、0.159.3 的协议与固定源码 rust-v0.159.2：app-server 的 Realtime 封装将任务入口固定为 background_agent/remain_silent，VAD 参数也由内部设置，截断依据收到的 PCM，而不是设备播放位置。`clientManagedHandoffs` 不能阻止原始请求先被后台执行器接走。因此只为电话模式直接连接同一 OpenAI Realtime 模型；原来的按住说话仍使用现有封装。Codex 登录、模型偏好、开发者指令、用量与聊天历史继续复用 resident service。

官方资料：

- [Realtime VAD](https://developers.openai.com/api/docs/guides/realtime-vad)：semantic VAD、create_response 与 interrupt_response。
- [Realtime conversations](https://developers.openai.com/api/docs/guides/realtime-conversations)：取消与 conversation.item.truncate。
- [当前模型](https://developers.openai.com/api/docs/models/gpt-realtime-2.1-mini)。
- [Codex app-server](https://learn.chatgpt.com/docs/app-server)：thread/turn、expectedTurnId、interrupt。
- [GStreamer webrtcdsp](https://gstreamer.freedesktop.org/documentation/webrtcdsp/webrtcdsp.html)：AEC 与本地 voice-activity；核对 1.28.2 实际源码。voice-detection 在 HAVE_WEBRTC1 构建中有效；不能只根据属性存在推断 VAD 可用，也不能依赖已经无效的 frame-size/likelihood 调节。
- [Android AcousticEchoCanceler](https://developer.android.com/reference/android/media/audiofx/AcousticEchoCanceler)：可用性和实际 enable 状态单独核验。

以上服务沿用 OpenAI API 条款。新增 C++ 源码和协议适配采用 GPL-2.0-or-later；Qt、GStreamer、WebRTC DSP 复用系统软件包，未复制上游源码。研究缓存、固定源码哈希、协议和云端探针保存在 `.work/verify/20261001-realtime-feasibility/`；本次构建和验收保存在 `.work/verify/20261002-phone-mode/`。

此前云端探针覆盖 10 个中文场景，延长静默后的独立场景路由均通过；这不是大样本准确率验收。实测 semantic VAD 可延迟较久，故采用应用主动创建回应：等待本地静默和完整最终 ASR，2 秒静默仍未提交时手动 commit。未收到完整转写则不开始执行。模型 original_words 只作说明，执行器始终得到最终转写原文。默认由 ASR 检测实际说话语言，不把桌面界面语言强制当作识别语言。

## 实现

`agent/assistant/session/` 是 Qt/GStreamer 常驻 C++ 协调器。`phone_session.py` 只传递本地 JSON、Codex RPC、历史和前台状态，不承载语音或调度逻辑。D-Bus 提供 StartPhoneMode、StopPhoneMode、SetPhoneMuted、StopSpeaking、FocusTask、StopTaskById、AnswerTask 和 PhoneSnapshot。会话归属显式绑定 conversationId，事件按任务的来源入库，切换界面不迁移事件。

任务有独立 taskId/threadId/turnId。只读任务最多两个并行，并使用 Codex read-only sandbox，禁用配置中的所有 MCP；有修改、桌面或设备操作的任务独占，先到先执行。更正使用 turn/steer 并绑定 expectedTurnId。取消经历 stopping，直到 Codex turn 终止且任务工具不再活动，才显示 stopped。同一完整话语重复调用 start_task 不会重复执行。本版一次话语只更正或取消一个已有任务；第二个不同目标被拒绝，同一目标的重复更正也不会重复发送。需要控制多个已有任务时先询问目标，任务卡片仍可分别操作。已有只读任务执行期间，可以继续开始另一个独立只读任务。

独占任务的桌面 MCP 经过 `rungic-task-tools`。它用私有进程组和进程身份绑定的租约管理 worker；撤销租约或 MCP cancelled 通知只结束这个 worker 的进程组。worker 意外退出或先于后代退出时，仍清理私有进程组；超过退出期限会终止忽略 SIGTERM 的后代。已经独立运行的用户应用不在此组内。旧全局 CUA abort 文件不作用于这些任务。其他自定义 MCP 暂不开放给电话模式的独占任务。

request_user_input 通过任务卡片和 answer_task 处理，绑定任务及服务端请求，不自动选择默认答案；秘密答案只能在卡片输入。重启后不重放 journal 中的任务，查询实际后端状态后恢复显示。服务认证变化会中止原连接的任务状态，要求显式重新发起。

共享媒体链路为：

`Agent/GStreamer → PulseAudio android_communication / android_communication_microphone → rungic-communication-audio → Android CaptureBridge → AudioTrack/AudioRecord`。

Android 使用 MODE_IN_COMMUNICATION、VOICE_COMMUNICATION、AEC/NS 和有线/USB 优先的路由；没有硬件 AEC 时在共享后端使用 WebRTC echo probe/dsp。共享 DSP 的两个 appsink 使用 async=false、sync=false，并先用静音数据协商 echo reference，避免麦克风等待尚未播放的分支完成 preroll。其他标准 microphone 客户端复用同一次采集，不再各开 AudioRecord。静音关闭物理采集，保持播放。后台隐藏会关闭 Android capture sockets，通话的除外（2026-10-05，见文末）。蓝牙不作为本版验收范围。

播放控制具有独立 session/epoch；flush 后旧帧不再进入 AudioTrack。截断采用 Android playback-head cursor，并限制在实际提交的模型音频长度内。当前 PA 管道起点与 Android 100 ms 游标更新仍有边界误差，必须经声学实测，不能把 cursor 读取等同于已证明“听到了什么”。

### 应用里的入口和通话状态（2026-10-04）

界面按用户确认的 Claude Design 画布“Agent 通话”实现，取代原来对话底部的一排电话模式按钮。

- **入口**：对话顶栏的电话按钮在当前对话里开始通话；对话列表里的“和 Agent 通话”先新开一段对话再开始。Agent 正在替用户打电话、另一段对话在通话或正按住说话时，按钮显示为不可用，点一下说明原因（`call.js` 的 `blocked`）。
- **通话条**（设计系统的 `CallBar`）：位于顶栏下方，显示状态、时长、静音和挂断按钮。状态按以下顺序取第一个成立的：正在连接 → 等你回答（这次通话里有任务在等用户回答）→ 正在回答 → 正在听 → 正在处理 → 麦克风已关 → 通话中。通话在别的对话时只显示“前往”。点通话条打开 `CallSheet`（设计系统的 `CallPanel`），里面有打断、这次通话的任务和去回答。通话中输入条显示“通话中 · 直接说就行”，键盘仍可输入；按住说话停用。
- **后端字段**：`phone-state` 和 PhoneSnapshot 增加 `startedAt`（开始时间，秒）和 `thinking`。`thinking` 在用户说完、回复还没开始时为真：本地语音转写已完成但还没提交，或者已提交、正在等回复。“这次通话的任务”指本对话里、创建时间不早于 `startedAt` 的任务。
- **摘要**：挂断时协调器发出 `phone-ended`（时长、这次通话的任务、结束原因），存进对话历史。对话里显示为“通话结束”卡片，可去回答等待中的任务，或者再打给它。
- **边界**：断线会直接结束通话，没有重连，所以 `CallBar` 的 reconnecting 状态目前不会出现。对话列表还没有“通话中”标记。离线验证：`tools/tests/test_agent_call.py` 和 `session-test` 的相应断言，以及浅色和深色两种主题的截图。真人通话下的状态切换还没有在手机上验收。

## 构建、回退与验收

开发机现场核验为 mibook/x86_64；Linux ARM64 包在 macmini 的 rungic-build 容器构建，使用其系统 Surge 代理。G100 为 ZY32M9MRVP，Android 16、portov_cn。G100 S 不用于首轮部署。

Linux 包通过 `rungic_dev.py deploy rungic-voice-agent rungic-plasma-bridges rungic-cua --host macmini` 开发覆盖；回退同一设备执行 `rungic_dev.py reset`。这不是正式发布，不提交或接受 rootfs 快照。开发前基线为 20260930.19，已有 integrity drift（missing_files=308、unowned_usr=5、unowned_etc=9），与本次新增问题区分。

本轮最终开发覆盖为 `20260930.19+dev20261001t190954`：voice-agent `0.510+dev20261001t190954.d24326e.dirty`，bridges `0.358+dev20261001t184851.d24326e.dirty`，CUA `0.358+dev20261001t181635.d24326e.dirty`，Codex 入口 `0.278+dev20261001t181635.d24326e`，design `0.393+dev20261001t181635.d24326e`。设备原来的内置 Codex 0.156.1 不符合当前独立安装入口，按 99 篇安装官方 standalone 0.159.3，保留认证和 `gpt-6-luna` 任务模型偏好；语音仍是 `gpt-realtime-2.1-mini`。补齐 design 是因为当前 ChatEntry 引用了旧安装包没有的 Picture 类型，运行日志已核验修复。

已记录的失败及修正：Ubuntu APT 索引和 ffmpeg 缺失；transient APT unit 未继承代理；通信 FIFO 提前创建使 PulseAudio 拒绝加载模块；批量转发 PA 数据导致队列溢出；问答清空 QJsonObject 后仍持有 QJsonValueRef 导致崩溃。分别补索引/依赖、在部署工具显式传入手机代理、由 PA 创建 FIFO 后收紧权限、按 20 ms 节奏转发、在清空前复制 QJsonValue。后台简报沿用只读模式并禁用配置中的 MCP，避免绕过电话任务的工具租约。

APK 2.29/77 单独构建安装。Java-only 构建复用 G100 已安装 APK 的三个 ARM64 JNI 库和原有 OCR 资产，哈希保存在 native-libs/SHA256SUMS；不重新编译或更换 JNI。原 APK 备份在 `.work/verify/20261002-phone-mode/before.apk`，回退用明确 G100 序列号的 `adb install -r -d`，不清数据。

离线验证：Python 协议适配和现有回归；CMake session-core/session-state 测试；真实原生 MCP worker 的 cancelled/租约撤销/无租约拒绝测试。最终 worker 探针覆盖 cancelled、租约撤销、两者各自的顽固后代、worker 意外退出及无租约拒绝；控制通知和租约测试实测约 2.1–7.8 ms，独立 sleep 进程保持运行。这些结果不代表声学打断延迟。

本轮最终合并 Python 回归 85 项、18 个子测试通过，另有 1 项因宿主缺少 Debian 工具而排除。宿主缺少 dpkg-parsechangelog 的那一项在 Macmini 使用真实 Debian 工具另验通过。原生两个测试目标通过，覆盖任务限额/公平调度、完整转写、重复调用、过期回复、连接前半句、问答身份、停止后的迟到问答、挂断继续执行和协议故障关闭。最终开发部署 APT Installed/Candidate 一致，三个用户服务 active，SSH socket 仍 enabled。

源码同步补充：拉取 `d0b15620` 的 21 个上游提交后，合并保留音频跟随服务和 communication 服务；提交前 94 项 Python 回归、18 个子测试通过，1 项 Debian changelog 检查沿用此前构建机通过的证据。包清单、脚本语法、差异和 Git 源码范围检查通过。同步后的源码未重新部署，以上开发覆盖版本和下述实机证据仍对应同步前的候选。

实机分层证据：

| 检查 | 结果与验收边界 |
| --- | --- |
| Realtime + 生产端点判定/路由 | 同一会话输入 10 个中文 TTS 样本。最终轮讨论、附和、停止播报、否定停止和指定取消均无错误开始/取消；文件统计与天气以两个只读任务并行，0.8 秒句中停顿未提前执行。找今天照片的样本仍追问位置，没有自动开始；严格预期路由为 9/10，不能宣称达到 95% 大样本目标。 |
| ASR 保真与首音 | 实际执行文本取最终 ASR，保留“不修改/先别移动”和最后的纠正。修正探针后样本的云端首音约 2.2–5.7 秒，部分超过目标；旧探针曾把前一回复计入，最终轮已限定当前实际接收的音频。六场景最终轮为 2.240–5.073 秒。云端 delta 不是实际扬声器首音，目标仍未通过。 |
| 连续六场景与执行去重 | 最终云端轮讨论不执行，统计与天气正确并行；模型给天气重复调用 start_task 两次，实际只新增一个天气任务。更正只发送到统计线程，取消统计后天气继续，句中停顿后的“先别移动”完整保留；全部 RPC 均发生在输入结束后。此前轮次仍出现多目标更正或不必要的优先级追问，不能用最终小样本代替大样本质量验收。 |
| 长连接 | 合成 PCM 和静音输入的 Realtime 连接保持 1800.004 秒，configured/connected 均为 true；是传输稳定性探针，不是 30 分钟真人连续双工对话。 |
| 原生 Agent 与 Android 音频 | 最终共享 DSP 修正后，真实 Realtime 回应进入 AudioTrack，playedFrames=960、writtenFrames=6720；停止播报使 epoch 递增且游标归零，无播放时恢复原生 GStreamer 采集成功。静音释放物理麦克风，挂断后 communication 与 microphoneActive 均为 false。真人音质和回声效果尚未验收。 |
| PulseAudio 标准接口 | pacat/parecord 检查单 owner、静音开始仍播放、恢复采集、epoch 递增与关闭清理。1.5 秒收到 72,000 帧；该轮 PCM 全零，证明传输连续性，不证明真人声音可识别。flush 约 108/109 ms 为控制确认，不能当作声学 P95。静音后 microphoneActive=false；停止客户端并关闭会话后模块为零、通信和物理采集均 inactive。 |
| 真实 Codex 执行 | 只读终端等待 20 秒并计算 137×29。挂断时仍 running，之后 completed，结果 3973。另一个等待任务从 stopping 到后端确认 stopped；不是仅验证发送 interrupt 成功。 |
| 前后台 | （2026-10-05 前的规则，现已改为通话继续，见文末）用 Android Settings 实际遮住 Plasma，话音关闭而任务仍 running；返回后没有自动恢复，任务可明确停止。G100 的 Home 键返回同一个入口，不能用它当作真正隐藏测试。 |
| 界面 | 真实聊天窗口显示电话入口、任务结果与状态；共享设计状态图库离屏渲染通过，截图保存在本轮 .work。 |

用户随后要求低音量：G100 的 `android` 和 `android_phone` sink 为 10%。新 communication sink 在创建时继承 android_phone 音量，不因开始通话恢复为 100%；Android 系统音量命令未观察到实际改变，因此这里记录的是已核验的 Rungic/PulseAudio 输出设置。

证据目录中的 final-dev.json 核对最终 Installed/Candidate；native-audio-final.log、integration-final.log、foreground3.log、task-tools-verified.log、cloud-capacity.log 和 cloud.log 分别对应原生音频、真实执行、前后台、工具清理、小样本云端路由与长连接。仍须验收真人双工音质/回声、声学打断和首音、真实跨对话使用与长期交互稳定性。目标仍为：有效语音到实际静音 P95≤200 ms、完整话语到首音正常 P95≤2.5 s/兜底≤4 s；意图大样本≥95%，关键错误开始/取消为零，以及连续 30 分钟会话。这些指标不能用当前少量云端或离线样本代替。

## 故障：打开 App 提示 “Phone session service stopped; reopen the app”（2026-10-03）

**现象（用户报告）**：一打开 Agent App 就提示这句话。服务日志从 11:09 起，每次打开对话时调用 `PhoneSnapshot` 都失败；手机上 `rungic-voice-agent.service` 一直在运行（从 03:30 起），但它的子进程 `rungic-agent-session` 已经不在了。

**原因（日志、源码，并用协调器实测确认）**：
- 03:34:47 打开新对话，服务创建了 `PhoneSession`。03:35:04 Agent 回合开始（`turn/started`），`rungic_voice_agent.py` 直接调用 `self.phone._write({"type": "command", "method": "ExternalBusy", ...})`，没有带 `id`。回合结束时（`turn/completed`）也有同样的一处。这两处是 `c40326e`（2026-10-02，Agent 忙时保持唤醒）加的。
- 协调器对每条指令都回复 `{"type": "reply", "id": o["id"], ...}`。请求里没有 `id` 时，Qt 会把值为 undefined 的键省掉，回复就成了 `{"result":{"ok":true},"type":"reply"}`。在手机上用临时 `HOME` 单独运行 `rungic-agent-session` 已复现：不带 `id` 的 `ExternalBusy` 得到不带 `id` 的回复，带 `id` 的原样带回。
- `phone_session.py` 的读取线程执行 `self.pending.get(message['id'])`，抛出 `KeyError: 'id'` 后退出；退出时的 `finally` 主动 `terminate()` 了协调器。
- `PhoneSession` 在服务里只创建一次，没有重建。从那以后每条指令都报 “stopped; reopen the app”，而重开 App 并不能恢复，因为会话属于常驻服务。
- 所以只要 `PhoneSession` 已经存在，Agent 的下一个回合就会把它弄坏，每次都能复现。

**修法**：
- `PhoneSession.post()`：只发送、不等回复，但同样分配 `id`。`ExternalBusy` 改用它；协调器已停止时，这个提示直接丢弃，不把异常抛进 Agent 的回合处理。
- 读取线程逐行处理：解析不了或处理出错的一行记进日志（`phone session: dropped a message …`，带这一行的开头）后跳过，不再结束会话、不再杀协调器。
- `phone_session()` 发现协调器已退出时，记日志后重建，并带上原来任务的 Codex 线程对应关系；提示文字改为 “try again”。
- 测试：`tools/tests/test_phone_session.py` 新增 4 项（无 `id` 的回复和非 JSON 行被跳过，之后的回复和事件照常处理；`post()` 编号；协调器停止时 `post()` 不抛异常；Agent 代码里不再直接调用 `phone._write(`），共 9 项通过。

**部署**：待用户同意。部署 `rungic-voice-agent` 会重启 `rungic-voice-agent.service`、`rungic-voice-overlay.service`，并关闭正在运行的 Agent App。

## 故障：通话中 Agent 说话一卡一卡（2026-10-04）

**现象**：用户反映，和 Agent 通话的整个过程中，Agent 说话都一卡一卡。

**证据（G100 S，只读）**：AudioFlinger 的轨道记录里，Rungic APK 的通话播放轨道（单声道 48 kHz，`USAGE_VOICE_COMMUNICATION`）欠载很多：

| 时间（手机） | 播放时长 | 欠载 |
|---|---|---|
| 10-03 13:19 | 11.3 s | 1.17 s（10.3%） |
| 10-04 10:47 | 5.3 s | 1.12 s（21.3%） |

同一时间的 logcat 有 `BUFFER TIMEOUT: remove track … due to underrun` 和 `AudioTrack … disabled due to previous underrun, restarting`。作为对比，平时语音回复用的媒体轨道（`phone-output`）是 7.8 s 里欠载 0.16 s（2.1%）。

**原因**：两层都在给播放定节奏，而且都会落后。
- `shared/media/communication-audio.cpp` 的 `readOutput()`（bd35b7a，2026-10-02）：
  - 每次从 `android_communication` 的管道最多读 1920 字节（20 ms），然后固定停 20 ms 再读。
  - PulseAudio 的 pipe sink 往管道里写多快，完全取决于这边读多快，所以每一轮实际是 20 ms 加上事件循环的延迟。
  - 结果是送出的数据一直比播放慢。
- 会话的 `Session::tick()` 每 20 ms 只推 20 ms 的回复，而且只在 Android 缓冲不到 100 ms（`written − played < 4800` 帧）时才推。定时器晚了也不补。
- Android 的通话轨道只有 100 ms 缓冲，很快被吃空。

**第一次修复失败（同日）**：只改了服务端的节奏（按时钟读，始终让 Android 领先 80 ms），以开发覆盖装到 G100 S 后，通话只剩开头一声，之后一直静音。
- 原因：Android 一直保持着 80 ms 的余量，再加上播放位置 100 ms 一跳，会话看到的“缓冲”常常超过 100 ms，于是停止推送。
- 会话一停，PulseAudio 就往管道里填静音，服务照样按时把静音送给 Android，Android 一直显得满，会话就再也不推了。
- AudioFlinger 的记录是：播了 7.26 s，欠载为 0，内容却是静音。
- 处理：回滚到旧版本，用户确认回滚后有声音但仍卡。
- 当时的系统测试没发现：它直接用 PulseAudio 播放，绕过了会话的门槛；替身也没有按实时播放、回报位置。

**修法**（两处一起部署，缺一不可）：
- **服务**（`rungic-plasma-bridges`）：按时钟读管道。允许送出的量 = 从开始到现在应播放的量 + 80 ms − 已送出的量，一次最多 20 ms。管道空了以后，放弃欠下的部分；flush 后重新计时。
- **会话**（`rungic-voice-agent`）：不再看 Android 的缓冲（里面有 PulseAudio 的静音），改看“这段回复已推出的量 − Android 已播放的量”（`Session::chunksDue`）。领先不到 300 ms 就补足差额，一次 tick 最多推 8 块，晚了的 tick 下一次补上。打断时这些都会被 flush 清掉，打断的速度和“听到多少”的计算不受影响。

**验证**：在 Mac mini 的无头系统测试 `communication_audio` 里，用真实的 `Session`（`tools/system/call_playback.cpp`）、真的 PulseAudio、通信音频服务，以及按实时播放、缓冲 100 ms、位置 100 ms 一跳的 Android 替身，播放 3 s 的回复：

| 组合 | 结果 |
|---|---|
| 旧服务 + 旧会话（原状态） | Android 等数据共 900 ms（卡顿） |
| 新服务 + 旧会话（第一次修复） | 每 100 ms 的音量为 `[0, 3569, 0, 0, …]`，即开头一声后静音，回复剩 101760 字节没播 |
| 新服务 + 新会话 | 3 次运行，3.0–3.1 s 全部听到，Android 等数据 17–20 ms（含开头和结尾） |

另有两项：持续写满管道时，送达始终领先播放 29–63 ms，没有漂移；不静音时（经过回声消除参照），2 s 正弦波逐字节到达。`phone_session_units` 里有 `chunksDue` 的单元断言。手机上的复核见下文“部署”。

**部署与实机复核（G100 S，2026-10-04）**：
- `rungic-plasma-bridges` 和 `rungic-voice-agent` 一起以开发覆盖安装（`…dev20261004t145738.a8a0c74`，由 PR #10 合并本修复后构建），apt 核对正常。
- 实机复核：用户和 Agent 通话后确认“不卡了”。AudioFlinger 记录里，这次通话（手机时间 23:06）的通话播放轨道累计播放 14.72 s，欠载为 0（修复前为 10%–21%）。

## 锁屏继续通话（2026-10-05）

**用户反馈**：一锁屏，和 Agent 的对话就断了。用户要求像打电话一样，并按 Android 的最佳实践做。

**原因**：锁屏会从两处断开，只改一处不够。
- 会话每 0.5 秒问一次 APK 是否在前台（`phone_session.py` 的前台监视，`Foreground` 指令），锁屏后回答否，会话就结束（“Voice paused while Plasma is hidden”）。
- APK 在退到后台时关掉所有采集连接，包括通话的输出、控制和麦克风；Linux 侧随即报“通话音频断开”。
- 这是此前按隐私定的规则：只在用户看得到界面时收音。

**做法**：通话期间不再要求前台，挂断才停。
- **APK 把通话登记为 Android 的一通电话**：Telecom 的自管理通话（`AgentCall`，self-managed `ConnectionService`，权限 `MANAGE_OWN_CALLS`），聊天应用的网络通话也是这样做的。
  - 通话音频打开时登记（`placeCall`，带开扬声器的请求），关闭时结束。Telecom 刚接通若走听筒，改为扬声器一次；耳机或蓝牙由它选。
  - 采集服务在通话期间加上 `phoneCall` 类型，通知改为 Android 的通话通知（`CallStyle`），上面有挂断键。
  - Android 一侧挂断（通知、耳机、Telecom）时，通话位置回报里 `hungUp` 为真，经通信音频服务转给会话，会话按普通挂断结束（不是失败）。
  - Android 为来电挂起通话时（`held`），APK 把麦克风数据清零、通话播放音量设为 0，双方都听不到；恢复后照常。会话不另做处理。
  - Telecom 拒绝这通电话时（比如正在紧急通话），通话不经 Telecom 照常进行，只少了上面这些系统行为。
- **APK 的采集规则**：通话的连接（`communication-*`）在后台不关闭；通话打开后，在后台也允许打开通话麦克风和控制连接（取消静音时要用）。采集服务在整个通话期间保持麦克风类型，静音时也保持，因为 Android 只允许前台应用启动它。其他采集仍要求 Plasma 在前台。
- **不休眠**：通话期间 APK 持有部分唤醒锁（上限 4 小时，以防漏掉结束）；会话写 `$XDG_RUNTIME_DIR/rungic-call.busy`（会话进程的 pid），`rungic-agent-wakelock` 因此让手机保持唤醒，Linux 侧在锁屏时不被冻结。
- **会话**：`Foreground` 指令只回答 ok，不再结束通话；`phone_session.py` 去掉前台监视。开始通话仍要求在前台。

**隐私**：收音期间 Android 的通话通知和麦克风指示一直显示，挂断或静音即停。

**扬声器与麦克风的另一条路**：默认输出 `android` 走手机上的 PulseAudio（OpenSL ES，docs/42），本来就不要求前台；麦克风只有 APK 这一条路。Android 14 起后台进程不能自己开麦克风，必须由在前台时启动的前台服务持有，所以麦克风这一侧绕不开 APK。

**验证**：
- 会话测试（Mac mini）：隐藏时通话保持、继续播放；位置回报 `hungUp` 后会话按普通挂断结束，音频释放。
- `test_phone_session`：通话开着时 `rungic-call.busy` 写着会话进程的 pid，结束后删除；唤醒锁测试通过。
- APK 2.40/88 编译通过。
- 尚未在手机上实测：锁屏超过 3 分钟、通知挂断、来电挂起与恢复。

**首次实机：一开口就挂断（同日）**：用户一说话，通话就结束，记录的原因是“Communication playback stalled”。
- 两次通话里，APK 的通话播放轨道都只放了约 0.3 s 就不再取数据。Agent 一开始回答，通信音频服务送往 Android 的积压就超过 100 ms，它按当时的规则结束了通话。
- 原因：播放轨道在 Telecom 接管之前就开始了。Telecom 随后成为音频模式的持有者，APK 先前设的扬声器被清掉（`setNewModeOwner`），路由在听筒和扬声器之间切换，轨道在切换中停顿。第二次通话时，Telecom 显示扬声器，Android 实际却走了听筒。
- 复现：在容器里用无声的探针（只播静音，经通信音频服务与 APK，有间歇），四次中一次在开头约 1 s 停住并被挂断。不经间歇、持续播放时没有复现。
- 修正（APK）：先登记通话，等 Telecom 接通并设好模式（最多 2 s），确认通话设备是扬声器（有耳机或蓝牙时除外；否则请 Telecom 切换并等待，最多 1 s），然后才建播放轨道。Telecom 接管时 APK 不再自己设模式和通话设备。修正后探针连续多次都正常，Telecom 记录为 uid 1000 把通话设备设成扬声器。

## 通话音频以恢复为目标（2026-10-05）

**用户要求**：顿挫不论 1 秒还是 10 秒都不应该断开，以恢复为目标。

此前的规则（10-02 电话模式最初的实现）是“音频出问题就关闭话音，由用户再点一次恢复”。这是当时的设计选择，不是外部的最佳实践。现在改为：
- **卡顿时丢弃，不挂断**：
  - 通信音频服务送往平台的播放积压超过 100 ms 时，丢掉放不下的部分（实时声音，已经过时），通话继续。积压不放宽：放宽会让之后整通电话都延迟。
  - 回声消除处理和麦克风的消费方一时跟不上时，同样丢弃。
  - 会话里播放管道积压时丢弃；连续 3 s 不取数据才重新打开通话音频。
  - 网络一时跟不上时，丢掉这段麦克风声音，而不是结束通话。
  会话按平台实际播放的进度推进回复，所以丢掉的只是卡住的那一小段，回复不会错位。
- **断开时重连**：音频服务断开、报错、管道出错、打断未被确认时，会话重新打开通话音频（`Audio::recover`）。第一次等 0.25 s，之后逐次加长，最长 5 s，一直重试到挂断。期间通话条显示“正在重新连接”。重新接上后，回复从还没播放的地方接着放。麦克风一时打不开时，每秒重试，播放照常。
- **只有挂断才结束**：用户挂断，或平台挂断（Android 上是通话通知或耳机）。
- **还没做**：和 Realtime 模型的网络连接断开时，仍然结束通话。重连需要恢复模型侧的对话，另做。

**平台无关**：会话只认通信音频服务的接口。`hungUp`、`held` 是可选字段，没有时就是否；PC 上的 Linux 发行版没有这类平台通话，可以不提供。“通话中”标记由平台落实：手机上是唤醒锁服务，PC 上可用 systemd-inhibit。

**验证**：
- 会话测试（Mac mini）：音频服务断开时通话进入“正在重新连接”，会话不结束，自动重新打开；服务报错后同样重新打开；挂断后不再重试。
- 系统测试 `communication_audio`：服务的错误到达会话后，会话重新打开通话音频。
- 通话条测试：“正在重新连接”优先显示，并带通话时长。

## 电话任务的桌面工具全部被拒（2026-10-05）

**现象**：用户在通话里要求用 Agent 团队做游戏。执行端读了 rungic-agent-team 技能，写好了分工，但它的桌面工具（`desktop_launch`、`desktop_where`、`team_post`）每次都失败，返回“Task has no desktop operation lease”，团队面板也就没有出现。

**原因**：Codex 启动 MCP 服务时只传一小组环境变量，不包括 `XDG_RUNTIME_DIR`。`rungic-task-tools` 用这个变量找任务的操作租约，于是去 `/rungic-task-leases` 下找，永远找不到。会话写的租约（`$XDG_RUNTIME_DIR/rungic-task-leases/<task>.json`）其实是有效的，pid 和启动时间都对得上。在手机上查任务工具进程的环境确认了这一点。

**修正**：`rungic-task-tools` 在没有 `XDG_RUNTIME_DIR` 时使用 `/run/user/<uid>`（会话写租约的位置），并把它传给工具进程。`native_task_tools_probe.py` 增加 `codex_env` 一项：不带 `XDG_RUNTIME_DIR` 启动时，租约照样有效。修正前这一项失败，修正后通过（Mac mini）。

## 电话任务和按住说话共用任务卡（2026-10-05）

**用户反馈**：通话里交出去的团队任务，看不到计划，也看不到正在做什么。按住说话的任务卡一直有这些。

**原因**：电话模式（10-02）为低延迟语音另写了 C++ 协调器，任务的进度没有复用按住说话的 `task_state.py`，App 里也另做了一张简化的任务卡：只有原话、状态和结果。Codex 发来的计划（`turn/plan/updated`）、进展说明和命令输出，协调器都丢掉了。用户要求：能复用就复用，要拆开先问（AGENTS.md「复用优先，拆开先问」）。

**做法**：
- **同一个进度模型**：`phone_session.py` 把电话任务那条线程的 Codex 通知交给 `task_state.TurnState`，和按住说话的回合用同一套逻辑：计划、命令、工具调用、文件改动、进展说明。快照按 0.4 s 节流，以 `task` 事件发给 App，带上 `taskId`；回合结束时留下最终的卡片（计划、文件），和按住说话一样。
- **同一张卡**：ChatEntry 里抽出 `TaskProgress`（计划清单、当前活动、实时预览、改动的文件）。按住说话的回合和电话任务都用它。电话任务卡只多了提问与作答。
- **语音知道进度**：同一个模型的 `facts()`（已完成、正在做、还没开始）通过 `TaskFacts` 交给协调器，放进语音模型的可信任务快照，最多每 5 s 更新一次。用户问「做到哪了」时据此回答。
- **还没共用的**：助理屏上的操作说明和渲染预览（`on_screen`、`on_live`），由按住说话的进度循环轮询，电话任务还没接上。

**验证**：`test_phone_session`（计划和进展说明生成卡片与 TaskFacts，结束时留下最终卡）、`test_assistant_app`（电话任务卡显示计划清单和当前活动，任务状态更新时卡片保留）、会话测试（TaskFacts 只在任务运行时进入快照）。

**通话结束后，输入栏被锁（同日）**：通话挂断后，通话里交代的团队任务还在跑。输入栏仍显示“You're on a call”，打字和按住说话都不可用。原因是输入栏把“有通话任务没结束”当作在通话中。服务端其实早已把这时打的字交给通话的任务调度（`send_text` → `SubmitTask`）。现在这种情况下，输入栏显示“通话任务进行中 · 可打字补充”（原先的长文案把输入栏撑开，已改短，放不下时截断），打字和附件照常可用；按住说话要等任务结束，因为通话的语音已经关闭。测试：`test_agent_call` 的 `test_after_the_call_its_task_runs_on_and_typing_adds_to_it`。

**关掉的导播台又自己打开，组长的工作区被关（同日）**：通话里交代的团队任务进行中，用户点了导播台的 ✕，导播台随后又出现了。日志（19:30:26）显示：
- ✕ 只执行了 `rungic-agent-screen dismiss 1`，即导播台当前焦点的那一块屏。团队成员在 2、3、4 号屏工作，这几块屏仍然开着，导播台也就一直在。
- 1 号屏是组长（通话任务）在用，却被当成空闲关掉了，里面的应用也被要求退出（`closed: true`）。原因是“Agent 在干活”只看按住说话的回合（`agentBusy`），不看通话任务。组长的下一个桌面操作又把它拉了起来。

修正，沿用原有规则，不另写：
- **导播台的 ✕ 就是导播台里的每一块屏**（`Director.close` → `rungic-agent-screen dismiss director`）。每块屏按单个浮窗的规则处理：有 Agent 在干活的只隐藏，本次任务里不再弹出；空闲的关闭。
- **“Agent 在干活”统一**（`VoiceAgent.at_work`）：按住说话的回合，或者正在进行、会动桌面的通话任务（只读任务不算），通话结束后也算。State 里新增 `atWork`，`rungic-agent-screen` 据此判断。唤醒锁标记（`rungic-agent.busy`）也算上通话任务和后台的通话步骤。
- **全部结束后**，清掉所有“不再弹出”的记录（每块屏的，不只 1 号），下一个任务照常显示。

测试：`test_workspace_dismiss`（通话任务算在干活；导播台 ✕ 后，在干活的屏隐藏，空闲的关闭）、`test_agent_at_work`、`test_agent_screen_window`（导播台的 ✕ 调用 `Director.close`，不只是焦点屏）。

**导播台点叉后立刻又弹出来（同日，装上上面的修正之后）**：
- 当时屏幕上的导播台窗口 19:11 就已启动，仍是旧程序，点叉执行的还是“只关焦点”。
- 还有一个新旧版都有的缺陷：窗口关闭时调用 `QCoreApplication::quit()`。Qt 6 中，只要有一个窗口拒绝关闭，`quit()` 就会被取消。全屏窗口的 `onClosing` 会拒绝关闭（它的设计是“关全屏 = 回到浮窗”），于是从全屏点叉时程序没有退出，反而回到浮窗。手机上该进程（`--director`，19:11 起）在两次点叉后仍在运行，主线程停在事件循环里。
- 用一个最小的 PySide6 6.11 程序复现：一个可见窗口拒绝关闭时，`quit()` 不退出，`exit(0)` 退出。
- 修正：AgentScreen 和 Director 的关闭及其他退出路径都改用 `QCoreApplication::exit(0)`。

## 电话和按住说话共用执行端（2026-10-05）

**用户要求**：电话模式和按住说话没有本质区别，只是交互换成了电话，能力应该对齐；不要等用户一个个发现问题。并行处理只读请求的能力要保留。

**对比**（逐项对照代码）：电话模式另做了一套执行端，比按住说话少了或做法不同的共有 15 处。影响最大的是：
- 干活中途说的话要排队，不并进正在跑的那一轮（投屏排在 Krita 后面）。
- 语音提示词只有按住说话的一小部分。
- 几乎没有进度播报。
- 只读任务被关进沙盒，却被告知“完全权限”。
- 别的 MCP 工具被停用。
- 每个任务一个新线程，只带 20 条历史。
- 两种模式互相卡住。
- 审批被直接拒绝，错误被吞掉。
- 团队面板和命令明细卡片都没有。

**做法：同一个执行端**
- **要动手的任务**（`phone_session.py`）：`thread/start` 就是这个对话自己的 Codex 线程，也就是按住说话和打字用的那条（`VoiceAgent.main_thread`）。`turn/start` 在这条线程上开新的一轮；已有一轮在跑时改成 `turn/steer` 并进去，和按住说话中途再说话一样。两条请求同时到时，后到的等这一轮开始后再并进去。
  - 这条线程的通知仍由按住说话的代码处理，也就是它的任务卡、计划进度、助理屏说明、审批、错误和团队面板；同时抄送给协调器，让语音模型知道任务状态。
  - 任务的提问（`requestUserInput`）交给通话来问。
- **调度**（`Tasks::schedule`）：要动手的任务不再互相等，也不再等按住说话（`ExternalBusy` 不再挡队列）。只读任务仍在各自的只读线程里并行，最多两个，这就是保留的并行能力。只读任务会被告知自己只读、不能用桌面工具，不会再说“做不到”。
- **协调器**：共用线程上的通知按“这一轮”分给对应的任务；更早的任务不受影响。共用任务不再单独出电话任务卡，也不用工具租约，停止就是中断这一轮。
- **进度播报**：按住说话的进度规则（计划、换步骤、长步骤、久等致歉）在通话中改由通话的语音说出（`Narrate`）。同一份进度事实也会放进语音模型的任务快照。
- **语音提示词合一**：两种语音的共用部分写在 `prompts/voice-common.md` 里，由 `tools/agent_capabilities.py` 写进 `realtime.md` 和 `phone.md`，内容包括：你在哪、先应一声、替你解决而不是教你做、能力清单、主动、如何汇报结果、任务级偏好、语气、语言。`phone.md` 自己只保留通话特有的身份和任务工具规则。
- **按住说话不再被通话任务挡住**：没有通话时，通话里交代的任务就是这个对话正在进行的一轮，按住说话和打字照常并进去。
- **导播台成员**：被打断的团队（组长这一轮结束了，却没说完成）以前会让面板一直停在“进行中”，导播台于是一直留着它的成员，投到电视上就显示成好几块屏。现在：
  - 组长这一轮结束时，面板随之结束（`team.end`）；服务重启时，结束停在进行中的面板；
  - APK 只在团队进行中显示占位成员，过期的无界面工作区也会按时移除（APK 2.41/89）。

**还没对齐的**：
- 在通话里让它替你打电话（打给餐厅之类），仍会结束和 Agent 的这通电话。两通电话要同时用通话音频，需要 Telecom 的挂起来配合，另做。
- 通话中仍不能朗读；电话的提示文字还没翻译。

**验证**：
- 会话测试：第二个要动手的请求不等第一个；两者并进同一轮，随这一轮一起结束；更早的任务不受影响；不出单独的任务卡；不写租约。
- 核心调度测试：要动手的不等待，只读的最多两个。
- `test_phone_session`：任务走对话自己的线程，进行中则并进去，同时开始时会等待；只读任务的说明；通知不被独占；提问交给通话。
- `test_agent_capabilities`：两种语音的共用部分一致。
- `test_router`：组长停止时面板结束。

**验收（同日，G100 S，用打字代替说话）**：新开对话、发起 Agent 电话后，发“以河南信阳的茶山为像素背景，在 Krita 里面画一幅竖屏的画”；画到一半时再发“把画面投屏到电视上”。
- **通过的部分**：
  - 画图任务走对话自己的线程，有按住说话的任务卡、命令卡、计划（3 步全部完成）和进度播报，最后的结果带图片和可编辑的 .kra 文件。
  - 投屏请求并进了正在进行的这一轮，没有排队，约 30 秒后执行了 `rungic-agent-screen tv`。电视上只有 1 号屏（导播台成员 `[1]`）。
  - 结束后按住说话不再被挡住。
- **发现的问题**：
  1. 要求“在 Krita 里画”，执行端却先用图像生成做出底图，再放进 Krita。Codex 自带的 imagegen 技能写着“插画、像素图都用它”，而我们的说明里没有“用户点名的应用就在那个应用里亲手做”。
  2. 投屏请求刚交出去，语音就让用户“根据电视提示选择输入源”。这是在教用户操作，而且是编造的。
  3. 进度播报提到了“我已经读过 SKILL.md”和“桌面操作技能”。
- **修正**：
  - `agent.md` 加上：用户点名的应用就在里面用它自己的工具做；只有用户要生成的图，或者没点名应用时，才用图像生成。进展说明要用用户的话，不提读过的文件、技能、工具和命令。
  - `voice-common.md` 加上：开始或并进任务时只说“好，我来做”，不给用户操作步骤，也不猜怎么做；进度只说做出了什么、正在做什么。
  - `test_agent_capabilities` 检查这几条规则都在。

## 语音自己的回复也守规则（2026-10-05）

**现象**：通话中让它“用 Ardour 给这幅画配背景音乐”，它答“我没法直接执行软件里的操作”，并且没开任务。此前投屏时它让用户“看电视的投屏选项”，进度播报中还冒出一句韩语。

**原因**：
- 那句话说得长、有停顿，10 秒内转写没拼完整。会话按当时的规则把整句丢掉（“That utterance was not completed”）。
- 随后一条进度回复在对话里看到了这句没处理的请求，就顺口回答了。但进度回复是禁用工具的，而且回复自带的 `instructions` 会**整个替换**会话的说明。“一个助手、不说做不到、不给用户操作步骤、用用户的语言”这些规则都不在了。确认工具结果的回复也是这样。

**修正**（`session.cpp`）：
- 进度、汇报和确认这类回复（`replyRequest`）带上完整的会话说明和任务快照，后面再接这条回复自己的要求。
- 不属于某句话的汇报（进度、任务结束）放在对话之外说（`conversation: "none"`），输入里附上用户最后说的话，好让它用同一种语言。这样它不会再回答用户刚提、还没交出去的请求。
- 一句话 10 秒内没拼完整时，已经听到的部分照样交给执行端；只有一点都没听到才请用户重说。
- 语音服务拒绝某一个请求时，只放弃那条回复，不挂断通话。只有会话过期、密钥或额度被拒时才结束。

**验证**：会话测试新增两项：一是确认和进度回复带着会话规则，进度放在对话之外，带用户语言；二是单个请求被拒不挂断，会话过期才挂断。

**新的工作并行进行（同日，用户要求）**：在验证通话里，“再用 Ardour 给这幅画配一段背景音乐”并进了正在画的那一轮，要等画完才做。用户要求改成并行。
- 有工作在跑时，`start_task` 交来的新工作单独开一条线程，进入一个空闲的助理工作区（`rungic_cua.workspace.claim`，不用主工作的 1 号）。它的桌面工具和命令都在那个工作区里（`RUNGIC_WORKSPACE` 和显示变量），说明里也写明“和主工作同时进行，别碰主工作的应用”。
- 这条线程有自己的任务卡（TurnState），桌面工具受租约约束；它这一轮结束后，工作区随即归还。
- 跟当前工作有关的话（更正、“投到电视”、“放大点”、“用团队”）仍然用 `steer_task` 并进当前这一轮。没有空闲工作区时，新工作也并进当前这一轮。
- `phone.md` 写明了这两种情况的区别。测试：`test_phone_session` 的 `test_a_new_job_while_work_runs_goes_on_at_the_same_time`。

**验证通话中修掉的另外几处**：
- 确认回复问了一句用户已经说过的主题，现在要求它只说“我来做”，不问工具结果里没有的问题。
- 一条进度用了英文：现在按用户最后说的话判断语言，并在回复要求里写明。
- 进度播报压在它自己的确认上：现在要等它说完、安静 12 秒后再播。
- “已经读取 SKILL.md”：读取 `~/.codex` 里的说明不再算作一步。

