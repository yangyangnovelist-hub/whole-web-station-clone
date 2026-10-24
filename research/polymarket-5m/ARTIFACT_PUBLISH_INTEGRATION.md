# 录制包逐片发布接入

本次只改研究录制的数据传输，不改任何策略、订单、仓位、风控、录制币种/时长/停止日期。原始采集文件 allowlist 不扩大，原始包 1 天、转换包 30 天的既有保留期不变。不是永久原始数据归档方案，也不证明 24 小时连续覆盖。

## 已接入的两条路径

- `record` 将 `capture.tar` 交给本地 composite action；默认每片 400 MiB、各片一个独立 artifact，所有片成功才上传 `source-recording-RUN-index`。
- `analyze` 从受信任 record job 输出取得 manifest SHA256，下载 index 和 `source-recording-RUN-part-*`，先重组及校验 SHA256，再交给原 `capture_checkpoint.py restore`。
- 转换包 `raw.tgz` 同样发布成 `recording-RUN-part-*` 和 `recording-RUN-index`。不再产生超过下载接口上限的单一转换包。

每组最多 16 片；超限、缺片、失败、重复 artifact ID、篡改、缺索引都明确失败，不截断或假装成功。原有 capture/checkpoint 连续失败计数使用整个 composite 的最终 outcome。

## 验收

`polymarket-recorder-check.yml` 增加无市场连接的合成端到端测试：生成闭合原始交接包 → 1 MiB 测试分片独立上传 → 另一个 job 下载 → 验证可信清单 → 重组源 SHA256 → 恢复原始 capture。合成数据不是回测。原有 recorder 合约测试继续运行。

本次代码提交不取消/重派现有采集。现有接力的下一次运行 checkout 最新分支后采用新路径；仍用旧 checkout 的在途任务不会被中途改写。接入代码通过不等于首个真实录制包已验收，须分别登记。

## 旧大包

历史超大 artifact 不会被本次未来发布修改自动重写。若需恢复旧包，必须在有合法读取权且允许该下载大小的发布环境取得原件，按同一协议重新发布分片。不探查 AWS 认证，不扩展存储权限，不把私有原始数据提交公共 Git。

只重组与校验不会使数据自动进入策略筛选。BTC5m 市场覆盖、接收时间、双侧盘口、费用和官方结算仍要逐项验收；正式留出保持隔离。
