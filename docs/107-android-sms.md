# Android SIM 短信

## 范围与来源

2026-10-04，按用户要求新分支实现安卓短信，经 Linux 命令向 10000 发测试短信并检查回复。Opus 提供初稿 8d04c20，mibook 接手实现与 USB G100 验收；不更改默认短信应用，不删除、标记或改写已有消息。

使用 Android 官方 [SmsManager](https://developer.android.com/reference/android/telephony/SmsManager) 的短信分段、发送回调与送达 PDU，以及 [Telephony.Sms](https://developer.android.com/reference/android/provider/Telephony.Sms) 内容提供者。保留平台桥 UID 0/1000 校验；耗时发送在独立线程，主 UI 与其他请求不等待无线电。SEND_SMS/READ_SMS 是受限权限，需要安装允许及运行时授权；应用已有 root 授权路径按需授予并复核，失败明确返回。没有新增 RECEIVE_SMS 权限，收件通过系统短信库读取。

## 接口

- `op: sms, action: send, to, text`，可选 `subscription`；默认活动短信卡，无默认且只有一张活动卡时使用它，否则明确报错。最多 1000 Unicode 字符，以 SmsManager.divideMessage 分段。
- `status: sent|failed|pending`、`parts`、`sentParts`、`subscription`、`submittedAt`。全部分段无线电成功才是 sent；失败或缺少回报不自动重发。部分成功可伴随失败。
- `delivery: delivered|failed|pending|unconfirmed`；只有每个分段收到成功的送达 PDU 才给 `delivered: true`。仅收到广播、网络不提供回执或失败回执不能冒充送达。
- `action: list, box: inbox|sent|all, from?, since?(毫秒), limit`；短号严格匹配，长号码允许国际前缀，按时间倒序读取。结果 `messages` 与 `truncated`；扫描最多 2000 行，空 cursor 报错，超限不可宣称完整无消息。读取不写短信库。
- `rungic-sms send 10000 "查询话费"`；用返回的 submittedAt 执行 `rungic-sms list --from 10000 --after <submittedAt> --wait 60`。绝对时间避免漏掉发送接口返回前已收到的回复；等待超时退出 1 并给 timedOut。普通查询无消息退出 0；发送失败/未决退出 1，参数或接口错误退出 2。

平台提供者验收只读查询，不会发短信；消息内容在契约检查报告中整体隐藏。实机发送另行人工授权，本次目标仅 10000，内容为查询，不办理业务。电信 [官方短信营业厅说明](https://m.gd.189.cn/gd/sms/) 列出的常规短信指令目标为 10001；本次仍遵循用户指定的 10000，未获回复时不宣称短信营业厅完整验收。

## 回归与实机记录

待本轮测试与部署结果补齐；产物、日志和私有回复保存在 `.work/verify/20261004-android-sms/`，不进入源码。既有开发覆盖与用户数据保留，不发布新 rootfs。
