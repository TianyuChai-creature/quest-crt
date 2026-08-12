# Phase 3 — ZED + NVENC 生产级视频路径（设计文档）

日期：2026-08-12。前置：Phase 2 PASS（`docs/phase2-quest-acceptance.md`，`docs/stage1-revise.md` §Phase 2 Gate）。

## 0. 目标重定义（Quest 实机约束）

Phase 2 实机 offer 显示 Quest H.264 接收 capability 最高到 **`64001f` = High Profile L4.0**（MaxFS 8,192 / MaxMBPS 245,760）。而 2560×720 SBS@60 = 7,200 MB/帧 = 432,000 MB/s，需要 **L4.2**——当前 Quest 不可满足。

**Phase 3 目标**：建立 ZED → GPU → NVENC → encoded packet → aiortc RTP → Quest 的生产级视频路径，并在 Quest H.264 **Level 4.0 约束内**实测后选择 resolution/fps operating point。**不以 2560×720@60 为默认目标**。

## 1. 双模式候选（第一轮都做，实测后返回数据，不自行选默认）

### Mode A — Quality

| 项 | 值 |
|---|---|
| ZED capture | 2560×720 SBS @60 |
| 输出 | 2560×720 SBS @30（每眼 1280×720@30） |
| 宏块率 | 7,200 MB/帧 × 30 = 216,000 MB/s（MaxMBPS L4.0 ✓；MaxFS 7,200 ≤ 8,192 ✓） |
| 目标 | 空间细节/清晰度优先 |
| Freshness | 从 60Hz 采集按 freshness 原则取最新帧输出 30fps，**不允许 frame backlog** |

### Mode B — Motion（重点候选）

| 项 | 值 |
|---|---|
| ZED capture | 2560×720 SBS @60 |
| GPU resize | 1920×540 SBS @60（每眼 960×540@60） |
| 宏块率 | ceil(1920/16)×ceil(540/16)=120×34=4,080 MB/帧；×60 = **244,800 MB/s** ≤ 245,760（L4.0 MaxMBPS 刚够）✓；MaxFS 4,080 ✓ |
| 目标 | 运动连续性/temporal fidelity 优先 |

**不要因为 Quest 不支持 L4.2 就退回 720p30——Mode B 是 L4.0 内的 60Hz 路径。** ZED 采集始终 720p60（保住 60Hz temporal sampling），不因输出 540p60 而降相机时间采样率。

## 2. 架构路径

```
ZED Mini (2560×720 SBS @60)
  ↓ latest-only 采集队列（容量 2~3）
Rectified L/R（ZED SDK 内部）
  ↓
SBS assembly / GPU resize / crop
  ↓
NVENC H.264（PyAV h264_nvenc，低延迟参数）
  ↓ encoded av.Packet
MediaStreamTrack.recv() → 返回 av.Packet（aiortc 直接走 packetizer/RTP）
  ↓
aiortc：RTP sequence / packetization / RTCP / NACK / RTX / transport lifecycle
  ↓
Quest
```

**不改写整个 RTP sender**。aiortc 继续负责传输层；我们只替换「编码」这一节（从内建 libx264 → NVENC）和「源」（synthetic → ZED）。

## 3. Freshness Contract（文档级原则，不止是实现细节）

> **宁可掉旧帧，也不要把旧世界排队播放给驾驶员。**

- ZED capture queue：**latest-only**，容量 2~3 上限，满则丢最旧
- GPU 预处理：不积压
- encoder 输入：**有界队列**，满则丢旧帧
- 网络：不通过增加大 buffer 追求连续播放
- Quest：显示最新可用帧

## 4. NVENC 低延迟配置（必须）

- `B frames = 0`；`lookahead = 0`；frame reordering off
- low-latency preset（p1）/ tune
- **有界 encoder 队列**；single-pass 优先
- 不为高压缩率引入 B-frame / lookahead

**short GOP 的正确语义**——恢复链路：

```
packet loss → NACK/RTX → 无法及时恢复 → PLI/FIR → NVENC 下一帧 IDR
```

**PLI/FIR → external NVENC keyframe request 必须真正闭环**：外部 av.Packet 编码路径下，aiortc 的 `force_keyframe` 不会自动控制 NVENC。需要自己的桥（检测 PLI/FIR → 通知编码器下一帧出 IDR）。「IDR 越频繁越好」不是目标。

## 5. H.264 profile / level 不写死

Phase 2 实机 offer 含多个 profile。Phase 3 流程：

```
Quest offer capabilities
  → 按当前输出 resolution/fps 选择双方兼容的 profile/level
  → 配置 NVENC
  → SPS/PPS/bitstream 必须与 SDP 契约一致
```

- 不再默认锁 `42001f`，也不写死 High L4.0
- 实测 Baseline / Main / High 在 Quest 上的兼容性与解码表现
- High L4.0 可作为更高压缩效率候选，但不牺牲 latency 或协议一致性
- 禁止「SDP 宣称一种 profile/level、实际 bitstream 发另一种」

## 6. 真实路径 copy 记账（先测，不强求 zero-copy）

```
ZED SDK frame → CPU/GPU? → rectification → SBS assembly → resize → NVENC input
```

记录每一步：memory location、CPU copy 次数、GPU copy 次数、format conversion、平均耗时、p95 耗时、queue depth。

原则：**zero-copy where possible，otherwise bounded-copy**。若一次 GPU blit 成本很低，不为理论 zero-copy 把实现复杂化。第一轮先用可测路径跑出数据。

## 7. ZED exposure 纳入 telemetry

从真实 ZED 接入后记录：`exposure` / `gain` / `capture fps` / `grab latency`。

原因：最终 motion blur / capture latency 不一定来自编码器。Mode A/B 对比不能只看码率与分辨率，还要观察：快速运动时的模糊、低光下 exposure 是否自动拉长。**先可观测，不自动调参**。

## 8. video reconnect 分段计时（不阻塞 Phase 3，但开始记录）

Phase 2 实机：video 断线恢复约 5–10s 并有闪烁（记录为后续优化项）。Phase 3 把重连过程拆段记录：

```
failure detected → reconnect timer → offer start → ICE connected
→ DTLS ready → first RTP → first IDR → first decoded frame
```

重点验证：**新连接建立 → 立即获得 IDR**，不要让 decoder 在恢复后等自然 GOP。

## 9. 非目标（本轮不做）

Depth、XR stereo rendering、XRMediaBinding、IPD compensation、view reprojection、QUIC/MoQ、FEC、dual video track、adaptive bitrate。

## 10. 环境事实（2026-08-12 摸底）

- GPU：NVIDIA RTX 5060 Laptop（Blackwell），driver 580.178.04；**PyAV `h264_nvenc` 可用**
- ZED Mini **已连接**（lsusb 2b03:f682 + HID f681）；SDK 在 `/usr/local/zed`；**pyzed 未装入 uv env**（uv python 3.13.8 大概率不支持 pyzed；系统 python3 = 3.12.3）→ 用 `get_python_api.py` 装 3.12 版，必要时 ZED 采集走独立解释器/进程
- aiortc 1.15.0：outbound-rtp stats **无 `framesEncoded` 属性**（实测 None）→ 编码计数在 track 层自计数
- synthetic 管线探针（2026-08-12）：30fps 实际送达、帧内容在变、pts 步进 3,000（90kHz）→ **管线存活，synthetic source 不再优化**
- `frame.pts` 直接进入 RTP 时间戳（90kHz 单位）——encoder 输出的 av.Packet 必须给正确 pts/time_base

## 11. Phase 3 第一轮 Gate（全部满足才算第一轮完成）

- [ ] ZED Mini real capture：左右 rectified frame 正确
- [ ] NVENC：H.264 hardware encode 生效（**非 CPU x264 fallback**，能证明在 GPU 上）
- [ ] WebRTC：encoded av.Packet 正常进入 aiortc RTP
- [ ] Quest negotiated codec 与 NVENC bitstream 一致
- [ ] Mode A：2560×720 SBS@30 可稳定解码
- [ ] Mode B：1920×540 SBS@60 可稳定解码
- [ ] Freshness：queue 不持续增长、不出现逐渐累积延迟
- [ ] Quest getStats：active codec H264、framesDecoded 持续增加、实际 fps 符合模式、dropped/lost/jitter 可观测
- [ ] Recovery：NACK/RTX 路径可观察；**PLI/FIR → NVENC IDR 至少完成一次验证**

第一轮完成后返回两档模式真实数据对比（encoder latency、actual bitrate、Quest decoder fps、framesDropped、packet loss、CPU/GPU usage、主观静态清晰度、主观快速运动表现），**不自行选最终默认模式**。

## 12. 纪律

> 施工中发现 Quest / NVENC / PyAV / ZED SDK / aiortc 实际行为与上述假设冲突：**先报告真实代码/实机事实和影响，再修改设计**，不要为了完成既定方案绕过协议或驱动限制。
