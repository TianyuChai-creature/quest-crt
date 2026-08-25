# Quest CRT 工程手册

本文面向部署、联调、数据消费和维护人员，说明 Quest CRT 的运行方式、接口协议、JSONL 数据格式、坐标系和常见故障。项目是一个独立的 Quest WebXR 姿态采集服务。

## 1. 系统概览

Quest CRT 采集以下位置数据：

- 左手 21 个关节点；
- 右手 21 个关节点；
- 左、右上臂关节位置（工程中作为肩部位置使用）；
- 左、右下臂关节位置（工程中作为肘部位置使用）；
- 上背部与左右肩胛位置（仅用于逐帧构造人体坐标系）。

Quest 在 WebXR `local-floor` 中同帧读取这些点，发送前转换为以上背部为原点、X 前、
Y 上、Z 右的右手人体坐标，单位为米；腕部四元数同步换基。`8001` 按 HTS 坐标语义
输出腕部人体坐标位姿和腕部局部21点。系统不传输其他关节旋转、速度、置信度和手势分类。

```text
Quest Browser
  │  HTTPS: 采集页面
  │  WebRTC DataChannel: 628字节、30ms寿命二进制Pose（无线主通道）
  │  WSS /ws: Pose v4 JSON（自动回退）
  ▼
8000 / Pose 服务 ──── 后台线程写入 logs/*.jsonl（原始上背部人体坐标）
  │
  └── 内存中仅保留最新一帧
          │
          │ 新 Pose 事件驱动、latest-only
          │ 坐标变换（可选）
          ▼
8001 / Viewer 服务
  ├── HTTPS /: 46 点实时 Viewer
  └── WSS /ws: Viewer、质量监视和控制统一最新帧输出
```

两个 FastAPI/Uvicorn 服务运行在同一进程中。`8001` 服务位于守护线程中，两个服务通过
带锁的“最新帧”内存槽交换数据。唯一的下游 `/ws` 由新 Pose 事件驱动且不重复同一帧；
慢客户端恢复后读取最新状态，不追赶中间历史帧。它不是历史消息队列。

8000 上行采用全局单设备占用。第一条成功建立的 Quest WebRTC DataChannel 或 WSS 回退
连接成为 active source；其断开并完成处理线程清理前，其他 Quest 上行连接会被拒绝。

## 2. 工程目录

```text
quest-crt/
├── server.py                  # 服务入口、WebRTC/WSS、协议校验、日志和证书
├── quest_crt/                 # 协议、坐标、StablePoseStream、UDP 与遥测
├── static/                    # Quest operator UI 与 PC 端 3D Viewer
├── cloudxr/                   # ZED 配置与同会话 QCRT exporter
├── scripts/                   # 硬件预检、客户端生成、启动与契约检查
├── tests/                     # 单元、公开接口与 CloudXR 边界回归
├── certs/                     # 自动生成的开发证书和私钥
├── logs/                      # 原始姿态 JSONL 日志
├── CLOUDXR_INTEGRATION_WORKFLOW.md # H1–H6 实机验证记录
├── pyproject.toml             # Python 版本和依赖
└── uv.lock                    # 锁定依赖版本
```

`certs/`、`logs/` 和本地虚拟环境已在 `.gitignore` 中排除。

## 3. 环境与网络要求

- Python 3.13 或更高版本；
- 已安装 [`uv`](https://docs.astral.sh/uv/)；
- 支持 WebXR 手部追踪的 Quest Browser；
- Quest 与服务 PC 网络互通；
- Quest 能访问PC的TCP `8000`、`8001`，并能直连WebRTC协商出的PC侧UDP ICE端口。

正式采集前建议：

1. 关闭 PC 休眠和会中断网络的节能策略；
2. 确认防火墙允许两个TCP端口和局域网WebRTC UDP流量；
3. 尽量让 Quest 和 PC 使用稳定的同一局域网；
4. 为长时间采集预留足够磁盘空间。

实际样本显示姿态日志可能达到数 MB/分钟，数据量随帧率和可见关节点数量明显变化。
工程会按 256 MiB 或 15 分钟自动分段，并将 `logs/` 总量限制为 5 GiB；超限时删除
最旧的已关闭分段。

## 4. 安装与启动

在工程根目录执行：

```bash
uv sync
uv run python server.py
```

### 4.1 ZED + CloudXR 一体化启动

一体化链路为：

```text
ZED Mini -> camera_viz/Televiz -> CloudXR Runtime -> CloudXR.js
Quest body/hand tracking ---------> QCRT -> /ws + /ws/stream
```

两条传输连接相互独立，但共享 Quest Browser 中唯一的沉浸式 `XRSession`。仓库不复制
NVIDIA Web Client；`scripts/prepare_cloudxr_client.py` 从已安装缓存生成
`.cloudxr-client/`，在官方 bundle 之前嵌入 `cloudxr/qcrt-exporter.js`。

依赖准备完成后：

```bash
CAMERA_VIZ_DIR=/path/to/IsaacTeleop/examples/camera_viz \
  ./scripts/run_cloudxr_zed.sh --check
CAMERA_VIZ_DIR=/path/to/IsaacTeleop/examples/camera_viz \
  ./scripts/run_cloudxr_zed.sh
```

启动器会重启 CloudXR 后台服务以确保使用生成客户端，并让 quest-crt 复用 CloudXR
证书，避免 Quest Browser 对 `48322` 和 `8000` 分别放行两张自签名证书。默认关闭
姿态 JSONL 以减少实时链路 I/O；需要记录时设置 `POSE_LOG_ENABLED=1`。可通过
`CAMERA_CONFIG` 覆盖相机配置，但验收基线是
`cloudxr/camera_viz_zed_60fps.yaml`。启动器会观察 Quest 的 WSS `/sign_in`，再创建
camera_viz OpenXR 应用，避免单纯打开页面就提前触发
`XR_ERROR_FORM_FACTOR_UNAVAILABLE`。
默认入口 `https://<PC-IP>:8000/` 立即提供姿态-only；视频开关默认关闭。启动器无限等待
可选视频连接，正整数 `QUEST_WAIT_SECONDS` 可设置超时；视频进程退出不会带停姿态服务。
CloudXR 页面默认显示与 main 主线一致的 Quest CRT 简洁入口；NVIDIA 原始表单通过
**Advanced settings** 进入，不参与日常操作。勾选视频会立即切换到视频准备页；由于
WebXR 要求目标页面上的用户手势，操作者在该页只点击一次 **Start prep** 即可进入 XR。

CloudXR Runtime/CloudXR.js、IsaacTeleop/Televiz 与 ZED SDK/pyzed 均须单独安装并遵守
各自上游许可；本仓不分发这些组件。NVIDIA CloudXR EULA 必须由使用者明确接受，
启动器不会代为接受。若视频链路不可用，传统 `https://<PC-IP>:8000/` 页面仍可独立
采集人体数据。

服务启动时会输出类似：

```text
Quest page: https://192.168.8.230:8000/
3D viewer:  https://192.168.8.230:8001/
Health:     https://192.168.8.230:8000/health
Certificate: /path/to/quest-crt/certs/cert.pem
```

程序通过一个 UDP 路由探测选择供局域网访问的 IP。探测不发送姿态数据；如果探测失败，会回退到主机名解析结果，最终回退到 `127.0.0.1`。多网卡、VPN 或特殊路由环境下，应确认输出 IP 确实可由 Quest 访问。

### 4.2 环境变量

| 变量 | 默认值 | 作用 |
|---|---:|---|
| `POSE_HOST` | `0.0.0.0` | 两个 Uvicorn 服务共同使用的监听地址 |
| `POSE_PORT` | `8000` | Quest页面、WebRTC信令、Pose WSS回退和API端口 |
| `OUTPUT_PORT` | `8001` | Viewer 页面、下游 WSS 和 API 端口 |
| `POSE_LOG_ENABLED` | `1` | 是否记录JSONL；设为 `0` 可临时关闭 |
| `POSE_LOG_QUEUE_FRAMES` | `2048` | 日志后台有界队列容量；满时淘汰最旧日志帧 |
| `POSE_CERT_FILE` / `POSE_KEY_FILE` | 自动生成证书 | 成对指定外部 TLS 证书与私钥；一体化模式使用 CloudXR 证书 |

示例：

```bash
POSE_HOST=0.0.0.0 POSE_PORT=8000 OUTPUT_PORT=8001 \
  uv run python server.py
```

JSONL默认启用。临时关闭日志时使用：

```bash
POSE_LOG_ENABLED=0 uv run python server.py
```

修改端口后，以终端打印的 URL 为准。两个端口不能设置为相同值。

### 4.3 健康检查

两个服务都提供健康检查：

```bash
curl -k https://127.0.0.1:8000/health
curl -k https://127.0.0.1:8001/health
```

8000 正常响应还包含当前单设备占用状态，例如：

```json
{
  "status": "ok",
  "active_source": {
    "active": true,
    "transport": "webrtc",
    "client": "192.168.8.120:49152",
    "connected_for_ms": 1234.5
  }
}
```

`active_source.active` 表示某条 Quest 数据连接已占用入口，不等同于当前手部一定正在追踪。

### 4.4 停止服务

在启动终端按 `Ctrl+C`。主 Pose 服务退出后进程结束，Viewer 守护线程也随之退出。采集日志使用逐行缓冲，正常停止或意外中断时，已写入的完整行通常仍可直接读取。

## 5. 首次使用流程

1. 启动服务，记录终端打印的局域网 IP。
2. 在 Quest Browser 打开 `https://<PC-IP>:8000/`。
3. 首次使用自签名证书时，在浏览器中确认继续访问。
4. 等页面的传输通道变为 `WebRTC open`；协商失败时会显示 `WSS fallback open`。
5. 点击 **Start prep**，同意所需权限。
6. 确认 WebXR 状态为 `running`，已发送计数持续增加。
7. 在 PC 浏览器打开 `https://<PC-IP>:8001/` 查看实时 46 点画面与坐标。
8. 查看服务终端中的接收 FPS、序号和追踪状态。
9. 采集结束后退出 XR 或关闭页面，再停止服务。

Quest 页面进入的是 `immersive-ar` 会话，并要求 `local-floor` 和 `hand-tracking`；
`body-tracking` 是可选请求，但当前输出坐标依赖 `spine-upper`（上背部）和左右
`scapula`。设备、
浏览器或当前追踪状态不能同时提供这三点时，页面仍可进入 XR 并显示可用手部数据，
但会跳过上行帧，直到人体坐标系可以建立。

### 5.1 Quest 页面状态

| 项目 | 含义 |
|---|---|
| `Transport` | 优先显示 `WebRTC open`，不可用时自动显示WSS回退状态 |
| `WebXR` | XR 会话状态 |
| `Sent` | 当前 XR 会话成功交给活动传输通道的帧数 |
| `Local drops` | 因活动通道存在未清空发送缓冲而主动跳过的XR帧数 |

通道断开后页面每秒自动重新尝试WebRTC，失败才回退WSS。XR会话仍在运行时，重连后
继续沿用当前 `session_id` 和序号。

### 5.2 Viewer 操作

- 鼠标拖动：旋转；
- 滚轮：缩放；
- 双击：恢复默认第一人称、非镜像视角；
- 悬停关节点：显示名称和当前输出坐标。

Viewer 直接订阅 `8001/ws`，不读取 `logs/`。打开 Viewer 时如果 Pose 服务已经收到过数据，它会立即发送内存中的最新一帧；服务重启后，在新 Pose 帧到达前没有可输出数据。

## 6. 服务端点

| 端口 | 方法/协议 | 路径 | 用途 |
|---:|---|---|---|
| 8000 | HTTPS GET | `/` | Quest 采集页 |
| 8000 | HTTPS POST | `/api/webrtc/offer` | WebRTC SDP协商 |
| 8000 | WebRTC DataChannel | `pose` | 无序、30 ms消息寿命二进制Pose上行 |
| 8000 | WSS | `/ws` | Quest Pose v4 JSON回退通道；兼容旧v2/v3发送端 |
| 8000 | HTTPS GET | `/health` | Pose 服务健康检查 |
| 8000 | GET/PUT | `/api/coordinate-transform` | 查询或修改 Viewer 输出坐标 |
| 8001 | HTTPS GET | `/` | PC 端 Viewer |
| 8001 | WSS | `/ws` | Viewer、质量监视和控制统一的最新帧下游输出 |
| 8001 | HTTPS GET | `/health` | Viewer 服务健康检查 |
| 8001 | GET/PUT | `/api/coordinate-transform` | 查询或修改 Viewer 输出坐标 |

FastAPI 的 Swagger 和 ReDoc 页面均已关闭。两个坐标 API 路径操作的是同一份进程内状态。

## 7. 原始 Pose v4 数据格式

Quest默认通过WebRTC发送固定长度二进制帧；协商失败时向 `8000/ws` 发送UTF-8 JSON
文本帧。两种通道解码后都进入相同的Pose v4严格校验，随后才会写日志并发布给Viewer。
一个完整的未追踪JSON帧如下：

```json
{
  "type": "pose",
  "version": 4,
  "session_id": "407b7a3f-4791-479c-aa81-48e813aca057",
  "seq": 1,
  "timestamp_ms": 339989.138,
  "capture_epoch_ms": 1784628000123.456,
  "reference_space": "spine-upper-scapula",
  "units": "meters",
  "hands": {
    "left": {
      "tracked": false,
      "points": [
        null, null, null, null, null, null, null,
        null, null, null, null, null, null, null,
        null, null, null, null, null, null, null
      ],
      "wrist_orientation": null
    },
    "right": {
      "tracked": false,
      "points": [
        null, null, null, null, null, null, null,
        null, null, null, null, null, null, null,
        null, null, null, null, null, null, null
      ],
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

### 7.1 WebRTC二进制封装

采集页创建名为 `pose` 的DataChannel，配置为 `ordered=false`、
`maxPacketLifeTime=30`。这使旧姿态包不会因有序交付长期阻塞新姿态包，并限制消息
交给协议栈后的有效传输时间。SDP通过同源HTTPS
`POST /api/webrtc/offer` 交换；局域网路径默认不配置外部STUN/TURN。

当前采集页每包固定628字节，所有多字节数值使用little-endian：

| 偏移 | 长度 | 类型 | 内容 |
|---:|---:|---|---|
| 0 | 4 | bytes | ASCII `QCRT` |
| 4 | 1 | uint8 | 二进制协议版本，当前为3 |
| 5 | 1 | uint8 | 左手、右手、左肘、右肘、左肩、右肩追踪位 |
| 6 | 2 | uint16 | 保留，必须为0 |
| 8 | 4 | uint32 | `seq` |
| 12 | 8 | float64 | `timestamp_ms` |
| 20 | 8 | float64 | `capture_epoch_ms` |
| 28 | 16 | bytes | `session_id` UUID |
| 44 | 584 | 146×float32 | 位置和腕部四元数 |

146个float32的顺序为：左手21×XYZ、右手21×XYZ、左腕XYZW四元数、右腕XYZW
四元数、左肘XYZ、右肘XYZ、左肩XYZ、右肩XYZ。不可用向量的全部分量写为NaN；混合
有限值与NaN会被服务端拒绝。服务端解码后重建下面的Pose v4对象，因此日志与8001
消费者无需理解该二进制布局。服务端继续接受二进制版本2的628字节/Pose v3帧，以及
二进制版本1的604字节/Pose v2帧；Pose v2不包含 `shoulders`。

### 7.2 顶层字段

| 字段 | 类型 | 约束 | 含义 |
|---|---|---|---|
| `type` | string | 固定为 `"pose"` | 消息类型 |
| `version` | integer | 当前发送端固定为 `4` | 协议版本；服务端兼容旧v2/v3 |
| `session_id` | string | 非空 | 每次进入 XR 时生成的 UUID |
| `seq` | integer | `>= 1` | 会话内每次可发送XR采样的递增序号 |
| `timestamp_ms` | number | `>= 0` | WebXR 帧回调的单调时间戳，单位毫秒 |
| `capture_epoch_ms` | number/null | `>= 0` | Quest 采样时的 Unix epoch 毫秒；旧发送端可省略 |
| `reference_space` | string | 固定为 `"spine-upper-scapula"` | 上背部人体参考空间 |
| `units` | string | 固定为 `"meters"` | 位置单位 |
| `hands` | object | 必须含 `left/right` | 双手数据 |
| `elbows` | object | 必须含 `left/right` | 双肘数据 |
| `shoulders` | object | v3/v4必须含 `left/right` | 双肩数据 |

`timestamp_ms` **不是 Unix 时间戳，也不是服务端接收时间**，不能直接转换为日期。同一 XR 会话内可以用差值计算帧间隔：

```text
dt_seconds = (timestamp_ms[n] - timestamp_ms[n-1]) / 1000
```

当前采集页按下面的方式把同一个 WebXR 回调时间映射为 Quest 的 Unix epoch：

```text
capture_epoch_ms = performance.timeOrigin + timestamp_ms
```

服务端模型对 `capture_epoch_ms` 保持向后兼容：旧发送端省略该字段时仍接受帧，但无法
计算跨设备传输时间。

服务端模型对未知字段使用 `extra="forbid"`：顶层或嵌套对象中增加未定义字段，整帧会被
判为无效。新增必填字段或改变既有字段语义时应提升版本；像 `capture_epoch_ms` 这样的
可选兼容字段也必须同步修改发送端、校验模型和消费端。

### 7.3 三维点

有效位置为三个 JSON 数字组成的数组：

```json
[x, y, z]
```

Pose v4 的三维点使用上背部人体坐标：

- `x`：人体前方为正；
- `y`：向上为正；
- `z`：人体右方为正；
- 原点：当前帧的 `spine-upper`（上背部）；
- 手性：右手系，满足 `X × Y = Z`；
- 单位：米。

采集端先在同一个 WebXR 帧和 `local-floor` 中读取上背部 `spine-upper` 点 `O`、
左肩胛 `L`、右肩胛 `R`。有方向平面法向量为：

```text
n = (L - O) × (R - O)
X = normalize(n)
Y = normalize(world_up - dot(world_up, X) X)
Z = normalize(X × Y)
p_body = [dot(X, p-O), dot(Y, p-O), dot(Z, p-O)]
```

叉乘固定使用“左肩胛向量 × 右肩胛向量”，正常站姿下使 X 指向前方。X 严格采用测得
的平面法向，Y 是与 X 垂直且最接近 WebXR 世界向上的方向。`spine-upper`、任一
`scapula` 缺失，或三点导致法向量/向上投影退化时，采集端跳过当前帧。人体移动和
转身会逐帧更新原点与朝向；该协议不再保留房间中的绝对平移和偏航，坐标 API 也不能
恢复这些已经丢弃的信息。

### 7.4 手部对象

```json
{
  "tracked": true,
  "points": [[0.1, 1.2, -0.3], "... 共 21 项 ..."],
  "wrist_orientation": [0.0, 0.0, 0.0, 1.0]
}
```

`points` 始终严格包含 21 项，每项是 `[x, y, z]` 或 `null`。

- `tracked=true`：21 项全部有效，服务端要求不能包含 `null`；
- `tracked=true`：`wrist_orientation` 也必须有效；
- `tracked=false`：没有形成完整的 21 点追踪，但数组中仍可能存在部分有效点；
- 消费端无论 `tracked` 值如何，都应逐项判断该点是否为 `null`；
- `tracked=false` 不应直接解释为整只手完全不可见。

`wrist_orientation` 为腕部相对于上背部人体坐标基的 `[qx,qy,qz,qw]` 四元数。腕部点
`points[0]` 与四元数必须同时有效或同时为 `null`，四元数不能为零四元数。

手部关节点顺序遵循采集页中的 WebXR joint 名称：

| 索引 | WebXR 名称 | 工程简写 |
|---:|---|---|
| 0 | `wrist` | wrist |
| 1 | `thumb-metacarpal` | thumb-metacarpal |
| 2 | `thumb-phalanx-proximal` | thumb-proximal |
| 3 | `thumb-phalanx-distal` | thumb-distal |
| 4 | `thumb-tip` | thumb-tip |
| 5 | `index-finger-phalanx-proximal` | index-proximal |
| 6 | `index-finger-phalanx-intermediate` | index-intermediate |
| 7 | `index-finger-phalanx-distal` | index-distal |
| 8 | `index-finger-tip` | index-tip |
| 9 | `middle-finger-phalanx-proximal` | middle-proximal |
| 10 | `middle-finger-phalanx-intermediate` | middle-intermediate |
| 11 | `middle-finger-phalanx-distal` | middle-distal |
| 12 | `middle-finger-tip` | middle-tip |
| 13 | `ring-finger-phalanx-proximal` | ring-proximal |
| 14 | `ring-finger-phalanx-intermediate` | ring-intermediate |
| 15 | `ring-finger-phalanx-distal` | ring-distal |
| 16 | `ring-finger-tip` | ring-tip |
| 17 | `pinky-finger-phalanx-proximal` | pinky-proximal |
| 18 | `pinky-finger-phalanx-intermediate` | pinky-intermediate |
| 19 | `pinky-finger-phalanx-distal` | pinky-distal |
| 20 | `pinky-finger-tip` | pinky-tip |

### 7.5 肩部和肘部对象

```json
{
  "tracked": true,
  "position": [-0.18, 0.92, -0.18]
}
```

| 字段 | 类型 | 约束 |
|---|---|---|
| `tracked` | boolean | 必填 |
| `position` | 三维点或 `null` | 必填，且必须与 `tracked` 一致 |

`shoulders.left/right` 和 `elbows.left/right` 使用相同的对象结构。采集端从可选 body
tracking 的 `left-arm-upper`、`right-arm-upper` 读取位置并在协议中作为 shoulder
输出，从 `left-arm-lower`、`right-arm-lower` 读取位置并在协议中作为 elbow 输出。
两类关节与用于坐标构造的 `spine-upper`（上背部）、左右 `scapula` 来自同一
`frame.body`，先在同一个 WebXR 帧和 `local-floor` 参考空间中读取，再统一转换到
上背部人体坐标。它们不包含姿态旋转，也不应被理解
为经过人体测量学标定的精确骨性关节点。是否可用取决于 Quest/浏览器的 body tracking
实现和当前追踪质量；即使 body tracking 存在，单个关节的 Pose 仍可能暂时不可用。

## 8. 序号、丢帧与会话边界

### 8.1 `session_id`

每次成功进入新的 XR 会话时生成一个 UUID。重新进入 XR 会生成新 ID，并将 `seq`
重置为 0，首个发送包的序号为 1。

### 8.2 `seq`

`seq` 在确认当前有活动传输通道并成功构造人体坐标后、检查发送缓冲前递增。因此：

- Quest 页显示的“本地缓冲丢帧”会制造 `seq` 缺口；
- 缺少 `spine-upper`/`scapula` 而无法构造人体坐标的采样不会分配序号；
- 同一会话中服务端观察到的正向缺口表示某些已编号消息没有被该接收流程记入；
- 重复或倒退的序号会被服务端静默忽略，不写日志也不转发；
- 服务端看到新的 `session_id` 时会重置序号和统计状态。

WebRTC主通道允许乱序且不重传，WSS回退基于可靠有序的TCP。服务端丢弃重复或倒退
`seq`，始终采用更新帧。`effective_loss` 在WebRTC模式下可以反映未交付的已编号姿态
包，但仍不应直接等价为无线链路物理丢包率。

### 8.3 日志文件与会话不是一一对应

日志在WebRTC `pose` 通道或WSS回退连接建立时开始写入一组分段，而 `session_id` 在
每次进入XR时创建：

- 通道重连、文件轮转但XR会话未结束：同一 `session_id` 可能跨多个日志文件；
- 通道保持连接但退出并重新进入XR：一个日志分段可能包含多个 `session_id`；
- 消费日志时应以 `session_id` 和 `seq` 划分、排序数据，不能只依赖文件名。

## 9. JSONL 日志

通过严格校验且序号可接受的原始帧写入：

```text
logs/pose_YYYYMMDD_HHMMSS_ffffff_partNNNN.jsonl
```

文件名时间是服务端本地时间，精确到微秒，表示该上行通道建立后创建日志的时间。
`partNNNN` 从 `part0001` 开始递增。单个分段在写入下一帧将超过 256 MiB，或持续时间
达到 15 分钟时轮转；文件名时间不是每帧采集时间。

`logs/` 中 `pose_*.jsonl` 的默认总容量上限为 5 GiB。写入导致总量超限时，服务按
修改时间从旧到新删除已关闭分段，当前正在写入的分段不会被删除。

协议校验、JSON 序列化和磁盘写入均不在 RTC 接收事件循环内执行。入口处理槽和日志
队列都是有界的：处理线程忙时入口只保留最大待处理序号；日志队列满时淘汰最旧日志
记录。终端中的 `ingress_drop`、`log_drop` 分别统计这两类主动丢弃。该策略优先保证
控制数据新鲜度，因此过载期间 JSONL 不保证包含每一个 Quest 采样。

JSONL（JSON Lines）的规则是“一行一个完整 JSON 对象”。优点是可以流式追加和逐行
处理；不应把整个 `.jsonl` 文件当作一个 JSON 数组传给 `json.load()`。

日志保存 `8000` 接收到的原始上背部人体坐标数据，并为每帧增加以下 PC 接收时序字段；不
包含 `coordinate_transform`，运行时修改坐标预设也不会改写已有或后续原始日志。

| 日志字段 | 含义 |
|---|---|
| `server_received_epoch_ms` | PC收到完整WebRTC或WSS姿态消息后的Unix epoch毫秒 |
| `server_received_monotonic_ms` | 同一接收时刻的 PC 单调时钟毫秒，用于可靠计算到达间隔 |
| `estimated_transport_latency_ms` | `server_received_epoch_ms - capture_epoch_ms` |
| `relative_transport_delay_ms` | 当前差值减去本次上行连接内该会话迄今最小差值 |

`estimated_transport_latency_ms` 只有在 Quest 与 PC 墙上时钟已经同步时，才能近似作为
单向传输延迟；它还包含浏览器发送排队和服务端传输事件调度时间。两台设备存在
的固定时钟偏差也会进入该数值，甚至可能使它为负数。

`relative_transport_delay_ms` 消除了本会话内近似恒定的时钟偏差，适合观察相对最佳到达
路径额外增加了多少排队延迟，但它不是绝对网络延迟。分析到达采样率和批量到达时，应
优先对相邻帧的 `server_received_monotonic_ms` 做差。

### 9.1 命令行检查

统计行数：

```bash
wc -l logs/pose_*.jsonl
```

查看首帧并格式化：

```bash
head -n 1 logs/pose_20260717_145500_333416.jsonl | python -m json.tool
```

检查每行是否为合法 JSON：

```bash
uv run python - <<'PY'
import json
from pathlib import Path

for path in sorted(Path("logs").glob("pose_*.jsonl")):
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            try:
                json.loads(line)
            except json.JSONDecodeError as error:
                print(f"{path}:{line_number}: {error}")
PY
```

### 9.2 Python 流式读取

```python
import json
from pathlib import Path

path = Path("logs/pose_20260717_145500_333416.jsonl")

with path.open(encoding="utf-8") as stream:
    for line_number, line in enumerate(stream, 1):
        frame = json.loads(line)
        session_id = frame["session_id"]
        seq = frame["seq"]

        left_wrist = frame["hands"]["left"]["points"][0]
        if left_wrist is not None:
            x, y, z = left_wrist
            print(line_number, session_id, seq, x, y, z)
```

对于大文件应保持这种逐行处理方式，避免一次性加载全部日志。

### 9.3 导出长表 CSV

以下示例把非空手部点导出为每行一个关节点：

```python
import csv
import json
from pathlib import Path

source = Path("logs/pose_20260717_145500_333416.jsonl")
target = source.with_suffix(".hands.csv")

with source.open(encoding="utf-8") as src, target.open(
    "w", newline="", encoding="utf-8"
) as dst:
    writer = csv.writer(dst)
    writer.writerow(
        ["session_id", "seq", "timestamp_ms", "side", "joint_index", "x", "y", "z"]
    )
    for line in src:
        frame = json.loads(line)
        for side in ("left", "right"):
            for index, point in enumerate(frame["hands"][side]["points"]):
                if point is not None:
                    writer.writerow(
                        [
                            frame["session_id"],
                            frame["seq"],
                            frame["timestamp_ms"],
                            side,
                            index,
                            *point,
                        ]
                    )
```

## 10. `8001/ws` 下游输出

下游客户端连接：

```text
wss://<PC-IP>:8001/ws
```

输出以原始 Pose v4 为基础，先对人体参考位置和腕部姿态应用当前坐标变换，再把每只手转换
为 HTS 式腕部局部表达。顶层增加：

```json
{
  "representation": "hts-wrist-relative"
}
```

每只手的输出结构为：

```json
{
  "tracked": true,
  "wrist": {
    "position": [0.1, 1.2, -0.3],
    "orientation": [0.0, 0.0, 0.0, 1.0]
  },
  "landmarks": [
    [0.0, 0.0, 0.0],
    [0.01, 0.02, 0.03]
  ]
}
```

实际 `landmarks` 始终包含 21 项。左右手分别以自己的腕部为原点：

```text
p_local = inverse(R_wrist) × (p_reference - t_wrist)
p_reference = R_wrist × p_local + t_wrist
```

因此索引 0 的 wrist 局部坐标固定为 `[0,0,0]`；左右手局部点不能脱离各自的
`wrist.position` 和 `wrist.orientation` 直接比较人体参考位置。双肩和双肘仍为当前
以上背部为原点的输出坐标。

输出还包含坐标描述：

```json
{
  "coordinate_transform": {
    "name": "body",
    "source": "spine-upper-scapula",
    "axes": ["x", "y", "z"],
    "matrix": [
      [1.0, 0.0, 0.0],
      [0.0, 1.0, 0.0],
      [0.0, 0.0, 1.0]
    ],
    "determinant": 1,
    "changes_handedness": false
  }
}
```

该对象位于完整输出帧的顶层。`matrix` 满足：

```text
point_output = matrix × point_body
```

每个 Viewer `/ws` 连接由新 Pose 或坐标变换事件驱动。发送时读取当前最新 Pose，不进行插值，不重复相同 generation；客户端发送阻塞期间的多次更新通过共享事件和LatestPose 合并为最新状态。连接建立时只在缓存 Pose 年龄不超过 250 ms 时发送初始帧。Viewer 浏览器在 250 ms 没有收到新帧后显示 `stale`，同时继续绘制最后姿态以便观察。实际输出速率取决于有效上游更新率，不再固定为 72 Hz。

控制程序也连接 `/ws`。客户端发送阻塞期间的多次更新会合并成最新状态，不附加控制质量元数据。控制客户端还应维护容量为 1 的输入槽，并根据本地最后接收时间实现安全超时，不得依次执行网络读取队列中的历史姿态。

### 10.1 JavaScript 接入示例

浏览器使用开发自签名证书时，应先访问并信任 `https://<PC-IP>:8001/`，再建立 WSS：

```javascript
const socket = new WebSocket("wss://192.168.8.230:8001/ws")

socket.onmessage = (event) => {
  const frame = JSON.parse(event.data)
  const right = frame.hands.right
  if (right.wrist.position !== null) {
    console.log(
      frame.session_id,
      frame.seq,
      right.wrist.position,
      right.wrist.orientation,
      right.landmarks
    )
  }
}
```

### 10.2 Python 接入提示

工程本身没有声明通用 WSS 客户端依赖。如需 Python 实时消费，可在下游工程选择
WebSocket 客户端库，并为开发证书配置显式信任。不要在生产环境长期使用“关闭 TLS
校验”的做法。

## 11. 坐标变换

默认预设为 `body`，因此 `8001` 默认保留 X 前、Y 上、Z 右的上背部人体右手系。
需要 X 前、Y 左、Z 上右手系时，可选择 `flu`。坐标配置只保存在内存中，进程重启后
恢复为 `body`。这些规则只改变轴方向与顺序，不会把原点恢复到 `local-floor`。

每个轴规则描述一个输出分量：

```text
["x", "-z", "y"]
```

表示：

```text
x_out =  x_in
y_out = -z_in
z_out =  y_in
```

规则必须正好包含三个条目，并且 `x/y/z` 各使用一次；允许值为
`x, -x, y, -y, z, -z`。

### 11.1 内置预设

| 名称 | 轴规则 | 约定 |
|---|---|---|
| `body`（默认） | `["x","y","z"]` | X 前、Y 上、Z 右，右手系 |
| `webxr` | `["z","y","-x"]` | X 右、Y 上、Z 后，右手系；原点仍为上背部 |
| `rfu` | `["z","x","y"]` | X 右、Y 前、Z 上，右手系 |
| `flu` | `["x","-z","y"]` | X 前、Y 左、Z 上，右手系 |

`determinant=-1` 或 `changes_handedness=true` 表示变换包含反射，会改变手性。

### 11.2 查询配置

```bash
curl -k https://192.168.8.230:8001/api/coordinate-transform
```

响应包括配置代次 `generation`、名称、轴规则、矩阵、手性和全部预设。`generation`
在每次成功更新时递增。

### 11.3 选择预设

```bash
curl -k -X PUT https://192.168.8.230:8001/api/coordinate-transform \
  -H 'Content-Type: application/json' \
  -d '{"preset":"flu"}'
```

预设名会先去除首尾空白并转为小写。

### 11.4 自定义变换

```bash
curl -k -X PUT https://192.168.8.230:8001/api/coordinate-transform \
  -H 'Content-Type: application/json' \
  -d '{"name":"custom","axes":["-z","x","-y"]}'
```

`name` 可省略，省略时为 `custom`。请求必须在 `preset` 和 `axes` 中二选一，不能同时
提供，也不能都不提供。非法请求返回 HTTP `422`。

更新后，已连接的 `8001/ws` 客户端会立即收到按新配置转换的最新 Pose 帧，即使上游
此刻没有产生新帧。因此消费端可能看到相同 `seq` 携带不同
`coordinate_transform`；这是配置更新，不是重复采样。

### 11.5 Python API

```python
from quest_crt import (
    flip_axis,
    remap_axes,
    to_hts_wrist_relative_frame,
    transform_pose_frame,
)

flip_z = flip_axis("z")
assert flip_z.apply((1, 2, 3)) == (1.0, 2.0, -3.0)

body_to_flu = remap_axes(("x", "-z", "y"))
assert body_to_flu.apply((1, 2, 3)) == (1.0, -3.0, 2.0)

flu_frame = transform_pose_frame(raw_frame, body_to_flu)
output_frame = to_hts_wrist_relative_frame(flu_frame)
```

`transform_pose_frame()` 返回深拷贝，不修改输入对象；它转换双手所有非空参考点、
左右腕四元数、双肩和双肘非空位置。`to_hts_wrist_relative_frame()` 再生成腕部参考位姿与腕部局部 21 点。

## 12. 服务端校验与错误处理

服务端接受帧的主要约束：

- 所有协议对象都拒绝未知字段；
- `type` 必须为 `pose`，`version` 必须为 `2`、`3` 或 `4`；
- Pose v3/v4 必须包含双肩，兼容的 Pose v2 必须不包含 `shoulders`；
- Pose v4 的 `reference_space` 必须为 `spine-upper-scapula`，v2/v3 必须为 `local-floor`；
- `session_id` 非空；
- `seq >= 1`、`timestamp_ms >= 0`；
- 每只手必须正好有 21 个点；
- `tracked=true` 的手不能含 `null`，且必须有腕部四元数；
- `points[0]` 和 `wrist_orientation` 必须同时有效或同时为空；
- 腕部四元数必须为四个数且不能是零四元数；
- 肘部 `tracked` 必须与 `position` 是否非空完全一致；
- 肩部 `tracked` 必须与 `position` 是否非空完全一致；
- 每个有效点必须正好包含三个可转换为浮点数的数值。

无效帧会在终端输出 `Invalid pose frame ...`，随后丢弃；连接不会因此自动关闭。无效帧不会写入 JSONL，也不会发布到 `8001`。

当前实现没有对 `session_id` 格式强制验证 UUID，也没有对浮点数做人体空间范围校验。不可信输入接入前应增加消息大小限制、速率限制、认证和数值有限性/范围检查。

## 13. 终端指标

原始接收端约每秒输出：

```text
fps= 89.9 | seq=8641 | effective_loss= 0.00%
```

| 指标 | 含义 |
|---|---|
| `fps` | `8000` 在当前统计窗口接受并写入的有效帧率 |
| `seq` | 最近接受的会话序号 |
| `effective_loss` | `missing / (received + missing)` |

Viewer 每个已输出数据的客户端约每秒输出：

```text
out_fps= 89.7 | seq=8641 | delivery=latest-only | transform=body
```

`out_fps` 是该 Viewer 客户端收到新 Pose 后的实际发送速率，不含固定频率重复帧。没有
连接客户端，或连接后尚无新鲜 Pose 可发时，不打印这些指标。

## 14. 证书与安全

启动时会检查 `certs/cert.pem` 是否包含当前探测到的局域网 IP：

- 证书和私钥可复用且证书 SAN 含当前 IP：继续使用；
- 私钥缺失或证书不含当前 IP：生成新的 RSA 2048 位自签名证书；
- 证书有效期约 365 天；
- SAN 包含 `localhost`、`quest-crt.local`、`127.0.0.1` 和当前局域网 IP；
- 私钥权限会设置为 `0600`。

当前启动检查只核对证书 SAN 中是否含当前 IP，不主动检查到期时间或证书与私钥是否匹配。长期运行环境应由外部证书管理流程负责续期和完整性检查。

自动证书仅适合受控开发网络。当前服务：

- 默认监听所有网卡；
- 没有用户认证和访问控制；
- Pose WebRTC/WSS和Viewer WSS均可被网络可达的客户端连接；
- 坐标变换 API 可被网络可达的客户端修改；
- 姿态日志属于敏感的人体运动数据。

正式部署至少应使用受终端信任的证书、限制监听/防火墙范围、增加认证授权、保护日志目录，并制定数据保留和删除策略。不要提交或分发 `certs/key.pem`。

如 IP 变化后 Quest 仍看到旧证书，可停止服务，删除开发环境中的 `certs/cert.pem`和 `certs/key.pem` 后重新启动。删除前确认这些文件不是正式环境证书。

## 15. 常见故障

### 15.1 Quest 无法打开页面

依次检查：

1. PC 终端中服务是否仍在运行；
2. 使用的是终端打印的 IP 和 `https://`；
3. Quest 与 PC 是否网络互通；
4. 防火墙是否允许 Pose 端口；
5. PC 浏览器能否打开相同 URL；
6. 多网卡环境下自动探测 IP 是否选错。

### 15.2 页面停在 **Waiting for PC**

这表示WebRTC和WSS回退均未建立。检查证书是否已被Quest接受、端口是否被代理或
防火墙阻断，以及服务端是否打印 `Quest connected ... via webrtc`。如果页面显示
`WSS fallback open`，说明WebRTC协商失败但兼容通道仍可使用。

如果终端打印 `Rejected Quest ... active Quest is ...`，表示已有第一台 Quest 占用入口。
第二台设备会被直接拒绝；退出或关闭第一台的数据连接后，第二台自动重连才会成功。

### 15.3 `immersive-ar unsupported` 或权限错误

确认在 Quest Browser 中运行，而不是普通 PC 浏览器；更新浏览器/系统，并检查
WebXR、手部追踪及站点权限。采集页要求 `hand-tracking`，不支持时无法进入会话。

### 15.4 XR 中有手，但没有上行帧或肩肘

人体参考系以及肩、肘数据都依赖可选 `body-tracking`。浏览器允许进入 XR 并不表示该
能力可用。只要 `spine-upper` 或任一 `scapula` 暂时缺失，采集端就跳过整帧；三点可用
但 `left/right-arm-upper` 或 `left/right-arm-lower` 缺失时，帧仍会发送，对应
`shoulders`/`elbows` 显示未追踪。检查设备能力、权限和当前身体追踪质量。

### 15.5 Viewer 打开但没有点

检查：

1. Viewer 的 WSS 是否为 `open`；
2. Quest 页是否为 `running` 且发送计数增加；
3. 服务终端是否出现 `fps=...`；
4. 是否所有点当前均为 `null`；
5. Viewer 是否连接到与 Quest 相同进程的 `8001`。

### 15.6 FPS 低、“本地缓冲丢帧”增加或批量到达

优先检查无线网络质量、PC CPU/磁盘占用、日志磁盘写入速度和浏览器后台限频。关闭不
必要的下游客户端并缩短网络路径。`8000` 输入帧率由 XR 帧回调和系统负载决定；
`8001 /ws` 只在最新 Pose 更新时发送，不重复旧帧。Viewer 在超过 250 ms 没有新帧后
显示 `stale`；控制客户端应根据本地最后接收时间独立进入安全状态。

查看 `https://<PC-IP>:8000/health`：应确认 `latest_pose.transport` 为 `webrtc`，并比较
`event_loop_lag.recent_p99_ms`、`maximum_ms` 与批量到达间隔。先用
`POSE_LOG_ENABLED=0` 做一次对照；若卡顿显著下降，继续检查磁盘或日志后台是否出现
`log_drop`。控制链路使用统一的 `/ws`，并按最后接收时间而不是平均 FPS 判断健康状态。

### 15.7 日志存在但末行损坏

异常断电可能留下不完整末行。JSONL 可保留此前全部有效行。处理时逐行解析并记录坏行，
不要因最后一行错误而丢弃整个文件。

### 15.8 坐标方向不符合下游系统

先用已知动作确认轴方向，例如右移、上抬、向前伸手，再选择 `rfu`/`flu` 或自定义
规则。记录每份导出数据采用的规则。原始日志永远是上背部人体坐标，不要误以为修改
API 会改变日志，也不要误以为 `webxr` 轴预设能恢复已经丢弃的房间平移和偏航。

## 16. 测试与维护

运行当前单元测试：

```bash
uv run python -m unittest discover -s tests -v
```

当前测试主要覆盖：

- 单轴翻转；
- 带符号轴重排；
- 内置预设；
- 非法轴规则拒绝；
- Pose 帧坐标转换与输入不变性；
- 腕部局部坐标转换、参考坐标重建和换基后姿态一致性；
- Pose v4人体参考空间、Pose v3肩部兼容、Pose v2兼容和旧协议版本拒绝；
- 二进制v3/v2/v1姿态编解码、628/604字节兼容、空值表示和非法数据拒绝；
- 跨线程通知唤醒 Viewer 订阅者。
- latest-only 入口覆盖、无序二进制包最大序号保留和异步日志写入。

修改工程时建议至少完成以下回归：

1. 单元测试通过；
2. 两个 `/health` 正常；
3. Quest 能进入 XR 并产生新日志；
4. 日志每行可解析，手数组长度恒为 21；
5. Viewer 能显示 46 点中的可见点；
6. 坐标 API 更新后 Viewer 立即更新；
7. 切换 `session_id` 和通道重连时日志及序号行为符合预期；
8. Quest页面显示 `WebRTC open`，服务端显示 `ordered=False maxPacketLifeTime=30`；
9. `/health` 的入口类型、Pose 年龄和事件循环卡顿指标合理；
10. `/ws` 不重复同一 Pose，慢读期间恢复后直接得到最新序号；
11. 第一台 Quest 活动时第二条 WebRTC/WSS 上行被拒绝，第一台断开后新连接可接管。

如果修改关节点顺序，必须同步修改采集页、Viewer 标签、下游文档和消费代码。若修改
协议字段或校验语义，应提升 `version`，避免旧消费端静默误读。

## 17. 当前边界

本工程刻意保持为最小实现，当前不包含：

- 用户认证与多租户隔离；
- 数据库、消息队列或历史帧回放；
- 日志压缩和外部上传；
- 公网WebRTC所需的STUN/TURN；局域网DataChannel不依赖它们；
- 姿态插值或跨设备时钟同步；
- 关节旋转、速度、置信度和手势识别；
- 多设备同时采集和空间标定；当前只允许一个 active source；
- 生产级 TLS 证书管理；
- React/Vite 或 Axol 依赖。

需要接入机器人、动作捕捉或多传感器系统时，建议把原始 JSONL 作为不可变数据源，
在独立下游层完成时间同步、坐标标定、滤波、缺失点处理和目标协议转换。
