# quest-crt

一个完全独立于 Axol 的最小 Quest WebXR 姿态传输项目。

| | |
|--|--|
| 本仓远程 | `git@github.com:TianyuChai-creature/quest-crt.git` |
| 克隆 | `git clone git@github.com:TianyuChai-creature/quest-crt.git` |

**生态中的位置**（三仓分工，各自独立 git；账号均为 **TianyuChai-creature**）：

| 仓库 | 远程 | 职责 |
|------|------|------|
| **quest-crt**（本仓） | `git@github.com:TianyuChai-creature/quest-crt.git` | Quest WebXR 传感；`:8000` 页面、`:8001` `/ws` + `/ws/stream` |
| **real-Teleop** | `git@github.com:TianyuChai-creature/real-Teleop.git` | SEW 臂腕重定向、Meshcat/实机；消费 `/ws` JSON |
| **DIME** | `git@github.com:TianyuChai-creature/DIME.git` | 手指 21→20 训练与权重；采集/实时消费 `/ws/stream` |

在 real-Teleop 开发树中通常放在 `refs/quest-crt/`（见 real-Teleop README 安装与同步）。

完整的部署、协议、坐标系、下游接入和故障排查说明见
[Quest CRT 工程手册](ENGINEERING_MANUAL.md)。

功能：

- Quest Browser 读取左右手各 21 个关键点。
- 读取左右上臂和下臂关节位置，并分别作为肩、肘位置输出。
- 读取上背部和左右肩胛点，构造以上背部为原点的人体坐标系。
- Quest 的沉浸式 WebXR 画面直接显示手部关键点、骨架连线、双肩和双肘。
- 上行坐标为 X 前、Y 上、Z 右的右手系，单位为米。
- 无线主通道使用无序、30 ms 消息寿命的 WebRTC DataChannel 和固定 628 字节二进制帧。
- WebRTC 协商失败时自动回退到同源安全 WebSocket JSON。
- 上行采用单设备占用：第一台 Quest 连接期间，第二台 Quest 的 WebRTC/WSS 数据连接
  会被直接拒绝，第一台断开后才允许下一台接入。
- PC 端进行严格协议校验。
- 已处理的完整帧保存为 JSONL；过载时日志和入口分别按 latest-only 策略淘汰旧帧。
- 接收、校验和日志写盘彼此解耦；处理跟不上时只保留最新待处理姿态。
- JSONL 按 256 MiB 或 15 分钟分段，`logs/` 总量默认限制为 5 GiB。
- PC 终端每秒显示接收 FPS 和序号缺失统计。
- `8001 /ws` 有客户端连接并开始输出数据后，PC 终端每秒显示实际输出 FPS。
- `8001 /ws` 由新 Pose 或坐标变换事件驱动，只发送最新状态，不重复相同帧；源数据
  超过 250 ms 时不发送已有缓存（**Viewer / real-Teleop SEW 通道**）。
- `8001 /ws/stream`：**StablePoseStream 传感主通道**（**DIME / 固定帧率机器消费**）——
  默认 72 Hz 推送 `StreamEnvelope`（`quality=ok|held|stale|lost`），默认二进制 QSTR，
  与 Viewer 解耦；可选 UDP 旁路。
- PC 独立端口实时显示可旋转、缩放的 46 点 3D 视图。
- 提供单轴取反和带符号轴重排的 Python/HTTP 坐标转换 API。
- `8001` 输出统一为：肩/肘/腕世界位姿（body 系，默认 preset=`body`）+ 各手 21 腕部局部点。
- **双下游**：real-Teleop 接 `/ws` JSON（要肩肘腕）；DIME 接 `/ws/stream`（要腕部 21 点）。



## Quest 启动交互（方案 A）

为避免接通瞬间人手与机械臂位姿差过大，且考虑 **摆姿后双手无法再点 UI**：

1. PC 通道就绪后，按钮显示 **Start prep**（唯一需要手部点击的步骤）
2. 进入 XR 后 **3 秒倒计时**（画面中央大数字 + 文案：Hold a pose close to the robot arms / Do not tap the UI）
3. **倒计时期间不向 PC 发送 pose**
4. 倒计时结束 **自动开始传输**，无需第二次点击
5. 结束 XR 后可点 **Prep again**

主机真机侧另有 `engage_s` 斜坡（默认 3s）从当前电机角平滑过渡到 SEW 目标，见仓库根目录 `README.md`。

## 运行

要求：

- Python 3.13+
- `uv`
- Quest 和 PC 位于可互相访问的网络

安装并启动：

```bash
cd /home/bot/mujoco_projects/quest-crt
uv sync
uv run python server.py
```

服务会自动检测当前 PC 局域网 IP，并生成包含该 IP SAN 的开发证书。终端会显示：

```text
Quest page: https://<PC-IP>:8000/
3D viewer:  https://<PC-IP>:8001/
```

使用方式：

1. 在 Quest Browser 中打开 `https://<PC-IP>:8000/`，首次访问时接受自签名证书；PC 通道就绪后点击 **Start prep**（唯一点击）。进入 XR 后 **3 秒倒计时**（摆姿，勿再点 UI），结束后 **自动开始传输**。
2. 在 PC 浏览器中打开 `https://<PC-IP>:8001/`，即可同时查看实时 46 点 3D 视图（倒计时结束前可能暂无新 pose）。

终端中的两类 FPS 含义不同：

```text
fps=...      Quest -> 8000 的原始接收速率
out_fps=...  8001 -> 当前 Viewer/下游客户端的实际发送速率
```

每个连接到 `8001 /ws` 的客户端分别统计；没有输出客户端或尚未发出第一帧时不会打印
`out_fps`。它表示实际最新帧发送率，不再由固定重采样频率维持。

3D 页面默认使用第一人称非镜像视角：人体右侧仍显示在屏幕右侧。`8001` 默认采用
`body` 坐标预设，保留采集端的 X 前、Y 上、Z 右右手系；如需 X 前、Y 左、Z 上的
右手系，可通过坐标 API 切换到 `flu`。页面支持鼠标拖动旋转、滚轮缩放、双击恢复
第一人称视角，以及悬停查看关键点名称和三维坐标。页面直接接收最新姿态帧，不读取
或轮询日志文件。

通过坐标 API 选择其他预设时，转换只作用于 `8001` 输出；Quest 上行、`8000`
接收协议和 JSONL 日志始终保留以上背部为原点的人体坐标。`8001` 在完成坐标变换后，
将每只手转换为“腕部上背部参考位姿 + 21 个腕部局部坐标”的 HTS 式表达。

PC 已处理的完整帧保存在：

```text
logs/pose_YYYYMMDD_HHMMSS_ffffff.jsonl
```

健康检查：

```text
https://<PC-IP>:8000/health
```

## 端口

项目使用两个固定 TCP 端口：

```text
8000  Quest采集网页 + WebRTC信令 + Pose WSS回退 /ws
8001  PC 3D可视化网页 + Viewer WSS /ws + 传感流 /ws/stream
```

### Viewer vs 传感流（请勿混用）

| 通道 | 路径 | 用途 | 节奏 | 格式 |
|------|------|------|------|------|
| **Viewer / SEW** | `wss://:8001/ws` | real-Teleop、3D 调试 | 事件驱动 latest | JSON（含 shoulders） |
| **传感主通道** | `wss://:8001/ws/stream` | DIME realtime / 固定 Hz | 固定 Hz + hold/stale | **二进制 QSTR**（默认）或 JSON |
| **传感 UDP**（可选） | `STREAM_UDP_HOST:PORT` | 同机/低开销消费 | 同上 | 同二进制 QSTR |

```bash
# 自检传感流（Quest 已在传数）
uv run python scripts/check_stream.py
```

可通过环境变量修改：

```bash
POSE_PORT=8000 OUTPUT_PORT=8001 uv run python server.py
```

JSONL默认启用；临时关闭日志使用：

```bash
POSE_LOG_ENABLED=0 uv run python server.py
```

日志后台队列容量可配置：

```bash
POSE_LOG_QUEUE_FRAMES=2048 uv run python server.py
```

实时诊断可从 `8000/health` 读取，其中包括入口实际使用的 `webrtc`/`wss`、最新 Pose
年龄、`active_source` 当前占用者，以及 Pose 服务事件循环的 p99/最大调度卡顿。控制
联调时应确认入口为 `webrtc`。

## 证书

证书位于：

```text
certs/cert.pem
certs/key.pem
```

如果 PC 的局域网 IP 改变，服务启动时会自动重新生成包含新 IP 的证书。

这是开发环境自签名证书。正式部署建议改用受 Quest 信任的局域网 CA 或正式域名证书。

## 协议

Quest采集页优先通过 `POST /api/webrtc/offer` 协商名为 `pose` 的DataChannel：

```javascript
peer.createDataChannel("pose", { ordered: false, maxPacketLifeTime: 30 })
```

每个WebRTC姿态包固定628字节，包括 `seq`、两个时间戳、UUID、追踪标志、46个位置和
两个腕部四元数；无效位置使用全NaN向量。服务端解码后进入下面的Pose v4校验模型。
WebRTC不可用时，页面自动用WSS发送以下JSON。服务端仍兼容旧的628字节/Pose v3和
604字节/Pose v2上行。

`seq` 在有活动传输通道且成功构造人体坐标的 XR 采样上递增；即使发送缓冲不为空而
主动丢帧也会留下序号缺口，便于 PC 区分源端主动丢帧与连续发送。WebRTC 发送缓冲
只要非空就跳过当前采样。无法构造人体坐标的采样不会分配序号。

Quest 内部仍通过 `local-floor` 同帧读取所有关节，然后在发送前构造人体坐标。令
`O` 为上背部关节 `spine-upper`，`L/R` 为左右 `scapula`，先计算有方向平面法向量
`X=normalize((L-O)×(R-O))`。Y 取世界向上方向在 X 的正交平面内的归一化投影，
`Z=X×Y`。左右点的叉乘顺序使正常站姿下 X 指向人体前方，最终得到 X 前、Y 上、
Z 右的右手正交系。所有位置减去 `O` 后分别投影到 X/Y/Z；腕部四元数也同步换基。
三点缺失或几何退化时跳过当前帧。

## 8001 下游接入

控制程序连接：

```text
wss://<PC-IP>:8001/ws
```

Viewer、质量监视器和控制程序统一使用该端点。它采用事件驱动、latest-only 发送，
并输出经过坐标变换的 HTS 腕部局部表达；不添加控制专用元数据。

客户端仍应使用独立读取任务覆盖一个单帧变量；控制循环每周期只读取该变量，不要建立
待执行 Pose 队列。服务端不会替控制程序决定安全时限；超过客户端自己的安全阈值未
收到新帧时应保持或停止控制。

每帧格式：

```json
{
  "type": "pose",
  "version": 4,
  "session_id": "uuid",
  "seq": 1,
  "timestamp_ms": 1000.0,
  "capture_epoch_ms": 1784628000123.456,
  "reference_space": "spine-upper-scapula",
  "units": "meters",
  "hands": {
    "left": {
      "tracked": false,
      "points": [null],
      "wrist_orientation": null
    },
    "right": {
      "tracked": false,
      "points": [null],
      "wrist_orientation": null
    }
  },
  "elbows": {
    "left": {"tracked": false, "position": null},
    "right": {"tracked": false, "position": null}
  },
  "shoulders": {
    "left": {"tracked": false, "position": null},
    "right": {"tracked": false, "position": null}
  }
}
```

实际 `hands.*.points` 始终包含 21 项。有效的 `wrist_orientation` 使用
`[qx,qy,qz,qw]` 顺序。

写入 JSONL 和转发到 `8001 /ws` 前，PC 还会加入 `server_received_epoch_ms`、
`server_received_monotonic_ms`、`estimated_transport_latency_ms` 和
`relative_transport_delay_ms`。其中绝对延迟估计要求 Quest 与 PC 墙上时钟同步；分析
批量到达时应优先使用 PC 单调接收时间戳的相邻差值。

`8001 /ws` 中每只手改为：

```json
{
  "tracked": true,
  "wrist": {
    "position": [0.1, 1.2, -0.3],
    "orientation": [0.0, 0.0, 0.0, 1.0]
  },
  "landmarks": [
    [0.0, 0.0, 0.0]
  ]
}
```

实际 `landmarks` 仍为 21 项，分别以左腕和右腕为局部原点。肩部、肘部和腕部位置继续
使用以上背部为原点的输出坐标。

## 坐标转换 API

转换使用三个带符号的轴描述输出坐标。例如：

```text
["x", "y", "-z"]   => x_out=x,  y_out=y,  z_out=-z
["-z", "-x", "y"]  => x_out=-z, y_out=-x, z_out=y
```

每个规则必须恰好使用一次 `x/y/z`，允许的值为：

```text
x, -x, y, -y, z, -z
```

内置预设：

| 名称 | 轴规则 | 结果 |
|---|---|---|
| `body`（默认） | `["x","y","z"]` | X前、Y上、Z右，右手系 |
| `webxr` | `["z","y","-x"]` | X右、Y上、Z后，右手系；原点仍为上背部 |
| `rfu` | `["z","x","y"]` | X右、Y前、Z上，右手系 |
| `flu` | `["x","-z","y"]` | X前、Y左、Z上，右手系 |

查询当前配置：

```bash
curl -k https://192.168.8.230:8001/api/coordinate-transform
```

选择预设：

```bash
curl -k -X PUT https://192.168.8.230:8001/api/coordinate-transform \
  -H 'Content-Type: application/json' \
  -d '{"preset":"flu"}'
```

自定义单轴取反：

```bash
curl -k -X PUT https://192.168.8.230:8001/api/coordinate-transform \
  -H 'Content-Type: application/json' \
  -d '{"name":"flip-z","axes":["x","y","-z"]}'
```

自定义轴重排和符号：

```bash
curl -k -X PUT https://192.168.8.230:8001/api/coordinate-transform \
  -H 'Content-Type: application/json' \
  -d '{"name":"custom","axes":["-z","x","-y"]}'
```

配置更新后，已打开的 Viewer 会立即收到按新坐标系转换的最新一帧。相同 API 也可通过 `8000` 端口访问。

Python API：

```python
from quest_crt import (
    flip_axis,
    remap_axes,
    to_hts_wrist_relative_frame,
    transform_pose_frame,
)

flip_z = flip_axis("z")
point = flip_z.apply((1.0, 2.0, 3.0))  # (1.0, 2.0, -3.0)

body_to_flu = remap_axes(("x", "-z", "y"))
point_flu = body_to_flu.apply((1.0, 2.0, 3.0))  # (1.0, -3.0, 2.0)

flu_frame = transform_pose_frame(raw_frame, body_to_flu)
viewer_frame = to_hts_wrist_relative_frame(flu_frame)
```

`AxisTransform.determinant` 返回 `+1` 或 `-1`；`changes_handedness` 表示该规则是否改变坐标系手性。

## 当前范围

为了保持最小实现，当前没有：

- React/Vite
- Axol依赖
- STUN/TURN
- 用户认证
- Quest捏合暂停

公网NAT穿透需要STUN/TURN；当前WebRTC主通道面向同一局域网，WSS保留为自动回退。
