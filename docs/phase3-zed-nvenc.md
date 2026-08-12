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
| resize（swscale，CPU） | 1920×540 SBS @60（每眼 960×540@60） |
| 宏块率 | ceil(1920/16)×ceil(540/16)=120×34=4,080 MB/帧；×60 = **244,800 MB/s** ≤ 245,760（L4.0 MaxMBPS 刚够）✓；MaxFS 4,080 ✓ |
| 目标 | 运动连续性/temporal fidelity 优先 |

**不要因为 Quest 不支持 L4.2 就退回 720p30——Mode B 是 L4.0 内的 60Hz 路径。** ZED 采集始终 720p60（保住 60Hz temporal sampling），不因输出 540p60 而降相机时间采样率。

## 2. 架构路径

```
ZED Mini (2560×720 SBS @60)
  ↓ latest-only 采集队列（容量 2~3）
Rectified L/R（ZED SDK 内部）
  ↓
SBS assembly / resize（swscale，CPU）/ crop
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

**实测（2026-08-12）**：
- aiortc 1.15.0 的 PLI/FIR 会调 `sender._send_keyframe()`，但 av.Packet 路径（`pack()`）忽略其 `__force_keyframe` 标志（`rtcrtpsender.py:316-323`）→ 桥 = 包装 `sender._send_keyframe`，PLI 时同时调用 `encoder.request_idr()`
- **Observed packet-output delay**（不是硬件结论）：当前 PyAV/FFmpeg h264_nvenc 集成中，输入 N 的包在提交 N+2 时返回（preset/rc/tune/zerolatency 全组合实测同形态；`tune=ull` 下 pts 正确透传）。**根因尚未证明是 NVENC 硬件本身**。延迟 ~66ms@30fps / ~33ms@60fps 计入 Mode A/B 对比
- **两个指标严格区分**：
  - **A. `encode_compute_ms`**：单次 encoder call（含拷贝/swscale）耗时，Mode A p95 ~12ms / Mode B p95 ~7ms
  - **B. `frame_to_packet_ms`**：某个输入 frame → 其对应 encoded packet 真正可供 RTP sender 使用。**遥操作真正关心的是 B**。当前观测值 ≈ 2 帧 + 编码耗时（Mode A @30fps ≈ 66.7ms；Mode B @60fps ≈ 33.3ms），保留为 "current path observed value"，暂不展开大规模 encoder rewrite
  - pts FIFO 同时记录提交墙钟，B 可直接从 FIFO 测量，且保证 delayed packet 归属到原始 capture frame（有测试 `test_delayed_packet_pts_traceable_to_capture` 验证）
- PyAV 17 无法设置 `AV_FRAME_FLAG_KEY`（无 `flags` 属性），且 h264_nvenc 实测忽略 `pict_type=I` 与 `key_frame=True`（CLI 的 `-force_key_frames` 能强制 IDR，走的是 FLAG_KEY）→ **IDR 实现 = 重建编码器上下文**（新上下文首帧必为 IDR；在途帧直接丢弃，符合 Freshness）
- **重建成本实测（2026-08-12）**：NVENC 上下文 open 本身 **~425ms**（本机 driver 580.178.04 / RTX 5060；close+open 全程 360–470ms）→ PLI 到首 IDR 包 **≈0.5s**（重建 + 2 帧输出延迟），期间 recv() 阻塞、RTP 停顿。仅 PLI 事件发生（罕见），正常流不受影响；如实记录影响，本轮不优化（不为此切换原生 NVENC SDK；若后续要攻，候选是预热备用上下文而非重建）

**normal-path vs recovery-path latency（明确区分，2026-08-12）**：

| 路径 | 定义 | 实测 |
|---|---|---|
| **normal-path**（每帧发生） | frame 提交 → 其 encoded packet 可供 RTP sender（= f2p，含 observed ~2 帧输出延迟） | Mode B avg **33.4ms** / Mode A ≈ **66.7ms**；encode_compute_ms（A 指标）单独可查 |
| **recovery-path**（仅 PLI/FIR） | RTCP PLI 被服务端接收 → encoder rebuild（close+open ~425ms）→ 新 encoder 首 IDR packet | **~360–470ms**（`pli_to_idr_ms_last` 直接测量） |

`pli_to_idr_ms` 起止点已核实（aiortc 1.15.0 `rtcrtpsender.py:279-281` 在 RTCP 接收处同步调 `_send_keyframe`）：起点 = **RTCP PLI 被服务端接收**（注入后 2–9ms 内可观测），终点 = **新 encoder 产生首个 IDR packet**（非仅 close/open 时长）。恢复路径**不阻塞 Mode A/B 实机**；**暂不优化**。若实机丢包/重连时明显出现 ~0.5s freeze，再单独开优化任务，优先调查「不重建 encoder、直接 force next IDR」的实现路径。
- PLI→IDR 遥测（为后续测 PLI→IDR latency 准备）：`pli_count`、`keyframes`（== encoder rebuild 次数）、`last_pli_wall_ns`、`last_idr_wall_ns`、`pli_to_idr_ms_last`。不为此切换到原生 NVENC SDK

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

**实际数据路径（2026-08-12 实测代码事实，不是 "GPU zero-copy pipeline"）：**

```
ZED sl.Mat（CPU 内存，sl.MEM.CPU retrieve）
  → numpy view（零拷贝，无 copy）
  → AVFrame from_ndarray（CPU copy：numpy RGBA → AVFrame）
  → swscale（CPU：Mode B resize + RGB→yuv420p 一次完成）
  → h264_nvenc（GPU upload 发生在 encoder 内部）
```

结论表述：**hardware encode validated；GPU / low-copy capture-to-encoder path not yet optimized**。这不阻塞 Phase 3——当前为了「绝对 zero-copy」重写 ZED pipeline 没有收益，第一阶段先用可测路径跑出数据。

copy ledger（telemetry 已实现）：`copies_rgb_to_av`（from_ndarray CPU copy）、`copies_swscale`（resize+格式转换）；每一步 memory location 如上（全部 CPU，除 NVENC 内部 upload）。不引入 GPU 端 ZED→NVENC 直连（Stereolabs 的 CUDA/GL 路径留待后续评估）。

## 7. ZED exposure 纳入 telemetry

从真实 ZED 接入后记录：`exposure_level` / `gain` / `capture_fps` / `capture_interval_ms`。

**`EXPOSURE` 语义（sl/Camera.hpp VIDEO_SETTINGS，2026-08-12 核对）**：0–100 的**级别**，线性映射为当前帧率下最大曝光值的百分比（60fps：100 → 10.84ms，0 → 0.171ms）。**不是微秒**。真实曝光时间 µs 仅 GMSL2 ZED-X 系列可通过 `EXPOSURE_TIME` 读取——ZED Mini（USB）不可用。因此 telemetry 报告 `exposure_level = 44`（级别），**不标 µs、也不直接声称等于帧周期百分比**（SDK 语义是"最大曝光值的百分比"，60fps 时最大曝光 10.84ms < 帧周期 16.67ms）。

`capture_interval_ms` = grab 循环两次成功 grab() 返回的平均间隔（60fps 稳态 ≈16.67ms = 帧间隔），**不是 camera latency**。真实 capture latency 需独立测量：

```
ZED IMAGE timestamp（camera clock） → host receive timestamp（perf_counter）
```

telemetry 已保留两端时钟（`image_timestamp_ns` / `host_receive_timestamp_ns`，均单调，只做差值），供 Phase 5 建立 capture-to-display latency 测量。

完整 capture 侧指标：`camera_fps`（capture_fps）、`exposure_level`、`gain`、`image_timestamp`、`host_receive_timestamp`、`capture_interval_ms`、`frames_captured`、`frames_dropped`。

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

本地验证（2026-08-12，本机 ZED Mini + RTX 5060）：
- [x] ZED Mini real capture：`VIEW.SIDE_BY_SIDE` 720p **@60fps 实测成立**（capture_fps 59.9–60.0，capture_interval_ms 16.67ms = grab 循环间隔/帧间隔，非 camera latency；HD720 SBS 无降帧）
- [x] NVENC：h264_nvenc 生效（healthz encoder.name，非 x264）；encode avg 6.5–9.7ms / p95 6.9–12.1ms
- [x] WebRTC：encoded av.Packet 正常进入 aiortc RTP（本地探针经真实 RTP/RTCP 解码 Mode A 137+ 帧、Mode B 240 帧）
- [x] 协商一致性记录：`negotiated profile-level-id=42001f vs NVENC profile=baseline`（Baseline 子集合法；Quest 实机最终确认待做）
- [x] Mode A：2560×720 SBS@30 本地解码 ✓（fps 实测 ~27–28，探针解码开销；编码端 30fps）
- [x] Mode B：1920×540 SBS@60 本地解码 ✓（编码端 286 帧/4.8s ≈ 60fps，p95 6.85ms 远在 16.7ms 预算内）
- [x] Freshness：capture 60fps 恒定、drop 计数即丢弃语义（空转期 drop≈captured，流式期 ≈0），无累积延迟
- [x] Recovery：**PLI → NVENC IDR 闭环**——本地向真实 RTP 注入 RTCP PLI，两模式各验证一次（forced_idr_keyframes 0→1）

待 Quest 实机（用户设备）：
- [ ] Quest 上 Mode A / Mode B 稳定解码（fps 符合模式）
- [ ] Quest getStats：active codec H264、framesDecoded 持续增加、dropped/lost/jitter 可观测
- [ ] Quest negotiated codec 与 NVENC bitstream 最终一致确认

第一轮完成后返回两档模式真实数据对比（encoder latency、actual bitrate、Quest decoder fps、framesDropped、packet loss、CPU/GPU usage、主观静态清晰度、主观快速运动表现），**不自行选最终默认模式**。

## 12. 纪律

> 施工中发现 Quest / NVENC / PyAV / ZED SDK / aiortc 实际行为与上述假设冲突：**先报告真实代码/实机事实和影响，再修改设计**，不要为了完成既定方案绕过协议或驱动限制。
