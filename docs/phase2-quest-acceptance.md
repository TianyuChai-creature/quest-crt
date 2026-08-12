# Phase 2 实机验收（Quest 3）——精简版

代码块均可整段复制（`LAN_IP`/`TSID` 块内自动推导）。服务端日志已 `tee` 到 `/tmp/quest-server.log`，事后可 grep。

**Quest 端**（无法粘贴）：单标签页单会话；自动休眠改 15 分钟后/永不；首次打开信任自签证书。

## 0. 启动（终端 1，保持前台）

```bash
cd /home/creature/Desktop/quest-crt
uv run python server.py 2>&1 | tee /tmp/quest-server.log
```

## 1. 环境 + 健康检查（终端 2）

```bash
LAN_IP=$(python3 -c "import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.connect(('8.8.8.8',80));print(s.getsockname()[0])")
echo "页面: https://$LAN_IP:8000/?video-test=1"
curl -k -s https://$LAN_IP:8000/health | python3 -m json.tool | head -20
curl -k -s https://$LAN_IP:8002/healthz | python3 -m json.tool | head -20
```

## 2. 头显观察（目标 4）

打开 `https://$LAN_IP:8000/?video-test=1` → 点「开始视频测试（非 XR）」，观察：
- 左右半屏各 7 色条、颜色一致；左侧 marker 2px/帧、右侧 3px/帧（右侧更快）
- 无翻转/裁剪/宽高比错误；状态行持续刷新 `H264 30fps d=… dr=… lost=…`
- 页面上**没有**叫「pose」的按钮；pose 入口是顶部主按钮「开始准备」（进 XR 后自动传数）。
  非 XR 模式下不传 pose 数据（服务端 `latest_pose` 为空属正常）；「已发送」计数=pose 包数，仅 XR 内累加。

## 3. offer 与协商（目标 1/2/3）

```bash
grep -A 60 "Received video offer" /tmp/quest-server.log | head -65
grep "Negotiated video codec\|WARNING" /tmp/quest-server.log
```

判定：m=video 全部 payload 为 H264（±rtx）；协商为 `H264 pt=103 profile=42001f packetization-mode=1`；无 WARNING。

## 4. 10 分钟轮询（目标 5）

```bash
{ for i in $(seq 0 10); do
    [ $i -gt 0 ] && sleep 60
    echo "== t+${i}min $(date +%T)"
    curl -k -s https://$LAN_IP:8000/health |
      python3 -c 'import json,sys
d=json.load(sys.stdin)
v=d["video"]["registry"]
print("peers",v["peers"],"codecs",v["negotiated_codecs"])
print("ch",[s["channels"] for s in d["transport_sessions"]["sessions"]])'
  done
} | tee /tmp/phase2-10min.log
```

判定：每行 `peers 1`、ch 恒含 `pose`+`video`、codec 恒 H264；期间头显状态行无 `closed/failed`。

## 5. 断线重连（目标 6）

```bash
curl -k -s https://$LAN_IP:8000/health > /tmp/health.json
TSID=$(python3 -c 'import json
d = json.load(open("/tmp/health.json"))
ss = d["transport_sessions"]["sessions"]
ts = [s["transport_session_id"] for s in ss
      if {"pose","video"} <= set(s.get("channels") or [])]
print(ts[0] if ts else (ss[0]["transport_session_id"] if ss else ""))')
echo "TSID=$TSID"
curl -k -s -X POST https://$LAN_IP:8002/api/webrtc/video/disconnect \
  -H 'Content-Type: application/json' \
  -d "{\"transport_session_id\":\"$TSID\"}"
echo   # 预期 {"closed":1}
sleep 4
curl -k -s https://$LAN_IP:8002/healthz | grep -o 'mimeType[^,]*'   # 预期 H264
# 头显确认：视频自动恢复；pose 无抖动
```

```bash
curl -k -s -X POST https://$LAN_IP:8000/api/webrtc/pose/disconnect \
  -H 'Content-Type: application/json' \
  -d "{\"transport_session_id\":\"$TSID\"}"
echo   # 预期 {"closed":1}
sleep 4
grep -c "Quest connected" /tmp/quest-server.log   # 应比断线前多 1
# 头显确认：视频无中断
```

（可选 5c）关 Wi-Fi 5–10s 再开：不刷新页面，pose/video 均自动恢复。

## 6. 收尾

```bash
grep -c "WARNING" /tmp/quest-server.log; grep -c "Negotiated video codec" /tmp/quest-server.log
```

## 实机结果（2026-08-12，Quest 3）

| 目标 | 结果 | 证据 |
|---|---|---|
| 1. Quest H.264 capabilities | ✅ 通过 | offer 8 个 payload 全 H264：`42001f`（pm 0/1）、`42e01f`（pm 0/1）、`4d001f`、`64001f`（High L4.0 为最高） |
| 2. offer SDP | ✅ 已落盘 | `/tmp/quest-server.log` 58–123 行（本机运行时） |
| 3. active codec | ✅ 通过 | 9 次协商全部 `H264 pt=103 profile=42001f packetization-mode=1`，WARNING=0 |
| 4. 非 XR SBS 显示 | ⛔ 用户放弃 | 彩条/d 计数正常；**marker 不动，未解决**（见开放项） |
| 5. 5–10 分钟连续 | ⛔ 用户放弃 | 未测 |
| 6. 独立断线重连 | ✅ 5a/5b 通过 | 5a：video 断→自动重连新 peer 重新协商 H264，pose 无影响；5b：pose 断→重连（`Quest connected` +1），video 同 peer 未动。5c 未测 |
| （范围外）XR 会话 | ✅ 顺带验证 | pose 88Hz、`age_ms` 6–9ms、test→xr ownership 切换正常、退出清场干净 |

**开放项 / 记录**：
- 目标 4 的 marker 不动：本地验证生成器相邻帧在动（diff 10800B），生成端无问题；怀疑实际送达帧率低（状态行 30fps 为编码器标称值）。未用探针接收端复测。挂起。
- 5a 恢复耗时 >4s（4s 采样点 peers=0），头显目视约 5–10s；恢复时 `<video>` 闪烁属预期（重建 stream），记录为轻量 UX 项。
- 5b 头显目视「视频无中断」未回报（服务端已确认无中断）。
- Phase 3 输入：Quest 最高广告 `64001f`（High L4.0，MaxMBPS 245,760），2560×720@60 需 L4.2（432,000）→ 60fps 可能需降 30fps 或换编码。

## Phase 2 结论（2026-08-12 用户裁决）

```
Phase 2 PASS

Validated:
- Quest 3 real-device H.264 capabilities
- H.264-only offer / negotiation
- pose / video dual-PC independence
- reconnect behavior
- XR ownership lifecycle

Deferred to real-video validation:
- stereo visual correctness
- long-duration playback stability
- final decoder/render FPS
```
