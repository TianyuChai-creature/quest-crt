;(() => {
  "use strict"

  const params = new URLSearchParams(location.search)
  if (params.get("qcrt") === "off" || !navigator.xr) return
  const PRODUCT_DEFAULTS = {
    panelHiddenAtStart: "true",
    controllerModelVisibility: "hide",
    showTraceInXR: "false",
    showRecordingControls: "false",
    autoRefreshMode: "never",
  }
  if (params.get("qcrtUi") !== "nvidia") {
    const productUrl = new URL(location.href)
    let changed = false
    for (const [key, value] of Object.entries(PRODUCT_DEFAULTS)) {
      if (params.has(key)) continue
      params.set(key, value)
      productUrl.searchParams.set(key, value)
      changed = true
    }
    if (changed) history.replaceState(null, "", productUrl)
  }

  const HAND_JOINTS = [
    "wrist",
    "thumb-metacarpal",
    "thumb-phalanx-proximal",
    "thumb-phalanx-distal",
    "thumb-tip",
    "index-finger-phalanx-proximal",
    "index-finger-phalanx-intermediate",
    "index-finger-phalanx-distal",
    "index-finger-tip",
    "middle-finger-phalanx-proximal",
    "middle-finger-phalanx-intermediate",
    "middle-finger-phalanx-distal",
    "middle-finger-tip",
    "ring-finger-phalanx-proximal",
    "ring-finger-phalanx-intermediate",
    "ring-finger-phalanx-distal",
    "ring-finger-tip",
    "pinky-finger-phalanx-proximal",
    "pinky-finger-phalanx-intermediate",
    "pinky-finger-phalanx-distal",
    "pinky-finger-tip",
  ]
  const BINARY_PACKET_SIZE = 628
  const BODY_FRAME_EPSILON = 1e-6
  const RTC_PACKET_LIFETIME_MS = 30
  const WS_MAX_BUFFERED_BYTES = 16 * 1024
  const PREP_COUNTDOWN_MS = 3000
  const qcrtHost = params.get("qcrtHost") || location.hostname
  const qcrtPort = params.get("qcrtPort") || "8000"
  const qcrtHttpOrigin = `${location.protocol}//${qcrtHost}:${qcrtPort}`
  const qcrtWsOrigin = `${location.protocol === "https:" ? "wss" : "ws"}://${qcrtHost}:${qcrtPort}`

  const state = (window.__qcrt = {
    phase: "waiting-xr",
    transport: "closed",
    sent: 0,
    dropped: 0,
    lastError: null,
  })
  let badge = null
  let shellValues = null
  let activeSession = null
  let referenceSpace = null
  let sessionId = null
  let prepDeadlineMs = 0
  let seq = 0
  let peerConnection = null
  let dataChannel = null
  let socket = null
  let reconnectTimer = null
  let connectionAttempt = 0

  function renderStatus() {
    if (badge) {
      badge.textContent = `QCRT ${state.phase} | ${state.transport} | ${state.sent}`
      badge.style.background = state.lastError ? "#8b1d1d" : "#173b20"
    }
    if (!shellValues) return
    const phases = {
      "waiting-xr": "待进入 XR",
      preparing: "准备中",
      streaming: "传输中",
      "waiting-body": "等待人体追踪",
      ended: "已结束",
      error: "异常",
    }
    shellValues.phase.textContent = phases[state.phase] || state.phase
    shellValues.transport.textContent = state.transport
    shellValues.sent.textContent = state.sent.toLocaleString()
    shellValues.dropped.textContent = state.dropped.toLocaleString()
    shellValues.poseDot.dataset.state = state.lastError ? "error" : state.phase
  }

  function setState(values) {
    let changed = false
    for (const [key, value] of Object.entries(values)) {
      if (state[key] === value) continue
      state[key] = value
      changed = true
    }
    if (changed) renderStatus()
  }

  function mountAdvancedBadge() {
    badge = document.createElement("div")
    badge.id = "qcrt-status"
    Object.assign(badge.style, {
      position: "fixed",
      right: "12px",
      bottom: "12px",
      zIndex: "2147483647",
      padding: "8px 10px",
      borderRadius: "4px",
      color: "white",
      font: "12px monospace",
      pointerEvents: "none",
    })
    document.body.appendChild(badge)

    const back = document.createElement("button")
    back.type = "button"
    back.textContent = "返回 Quest CRT 入口"
    Object.assign(back.style, {
      position: "fixed",
      left: "12px",
      bottom: "12px",
      zIndex: "2147483647",
      padding: "9px 12px",
      border: "1px solid #4f7cff",
      borderRadius: "6px",
      background: "#101114",
      color: "white",
      font: "600 12px system-ui",
      cursor: "pointer",
    })
    back.addEventListener("click", () => {
      const next = new URL(location.href)
      next.searchParams.delete("qcrtUi")
      location.href = next
    })
    document.body.appendChild(back)
    renderStatus()
  }

  function mountShell() {
    document.title = "Quest CRT · ZED Teleop"
    document.documentElement.lang = "zh-CN"
    document.body.classList.add("qcrt-shell")

    const style = document.createElement("style")
    style.textContent = `
      body.qcrt-shell {
        margin: 0 !important;
        padding: 0 !important;
        overflow: hidden !important;
        color: #f4f4f5;
        background: #0d0f14 !important;
      }
      body.qcrt-shell [id="2d-ui"] { display: none !important; }
      body.xr-mode #qcrt-entry { display: none !important; }
      #qcrt-entry {
        position: fixed;
        inset: 0;
        z-index: 2147483000;
        display: grid;
        place-items: center;
        min-height: 100vh;
        padding: max(24px, env(safe-area-inset-top)) max(24px, env(safe-area-inset-right))
          max(24px, env(safe-area-inset-bottom)) max(24px, env(safe-area-inset-left));
        overflow: auto;
        background:
          linear-gradient(#ffffff08 1px, transparent 1px),
          linear-gradient(90deg, #ffffff08 1px, transparent 1px),
          radial-gradient(circle at 50% -10%, #294475 0, transparent 43%),
          #0d0f14;
        background-size: 36px 36px, 36px 36px, auto, auto;
        font-family: "Avenir Next", "Noto Sans SC", ui-sans-serif, system-ui, sans-serif;
      }
      #qcrt-entry * { box-sizing: border-box; }
      #qcrt-entry .qcrt-panel {
        position: relative;
        width: min(92vw, 560px);
        padding: 30px;
        overflow: hidden;
        border: 1px solid #ffffff20;
        border-radius: 20px;
        background: #17191ee8;
        box-shadow: 0 28px 100px #000a, inset 0 1px #ffffff0d;
        animation: qcrt-arrive 360ms ease-out both;
      }
      #qcrt-entry .qcrt-panel::before {
        content: "";
        position: absolute;
        top: 0;
        left: 30px;
        width: 96px;
        height: 3px;
        background: #4f7cff;
        box-shadow: 0 0 24px #4f7cffaa;
      }
      #qcrt-entry .qcrt-kicker {
        display: flex;
        align-items: center;
        gap: 8px;
        margin-bottom: 10px;
        color: #9db2f8;
        font-size: 11px;
        font-weight: 750;
        letter-spacing: .16em;
        text-transform: uppercase;
      }
      #qcrt-entry .qcrt-mark {
        width: 7px;
        height: 7px;
        border-radius: 50%;
        background: #86efac;
        box-shadow: 0 0 14px #86efacaa;
      }
      #qcrt-entry h1 {
        margin: 0 0 10px;
        color: #fafafa;
        font-size: clamp(27px, 5vw, 34px);
        font-weight: 720;
        letter-spacing: -.035em;
      }
      #qcrt-entry .qcrt-lede {
        margin: 0;
        color: #a1a1aa;
        font-size: 14px;
        line-height: 1.65;
      }
      #qcrt-entry .qcrt-modes {
        display: grid;
        gap: 10px;
        margin: 22px 0 4px;
      }
      #qcrt-entry .qcrt-mode {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 18px;
        min-height: 72px;
        padding: 14px 16px;
        border: 1px solid #ffffff17;
        border-radius: 14px;
        background: #ffffff08;
      }
      #qcrt-entry .qcrt-mode strong,
      #qcrt-entry .qcrt-mode small { display: block; }
      #qcrt-entry .qcrt-mode strong {
        margin-bottom: 4px;
        color: #f4f4f5;
        font-size: 15px;
      }
      #qcrt-entry .qcrt-mode small { color: #858894; font-size: 12px; line-height: 1.4; }
      #qcrt-entry .qcrt-primary-tag {
        flex: none;
        padding: 5px 8px;
        border-radius: 999px;
        background: #4f7cff20;
        color: #b2c2ff;
        font-size: 11px;
        font-weight: 700;
      }
      #qcrt-entry .qcrt-video { cursor: pointer; }
      #qcrt-entry .qcrt-video:focus-within {
        border-color: #4f7cffaa;
        box-shadow: 0 0 0 3px #4f7cff24;
      }
      #qcrt-entry .qcrt-video input { position: absolute; opacity: 0; pointer-events: none; }
      #qcrt-entry .qcrt-switch {
        position: relative;
        flex: none;
        width: 48px;
        height: 28px;
        border-radius: 999px;
        background: #4f7cff;
      }
      #qcrt-entry .qcrt-switch::after {
        content: "";
        position: absolute;
        top: 4px;
        left: 24px;
        width: 20px;
        height: 20px;
        border-radius: 50%;
        background: white;
        box-shadow: 0 2px 8px #0008;
      }
      #qcrt-entry .qcrt-start {
        width: 100%;
        min-height: 54px;
        margin: 18px 0 0;
        padding: 14px 18px;
        border: 0;
        border-radius: 12px;
        background: #4f7cff;
        color: white;
        font: inherit;
        font-size: 15px;
        font-weight: 720;
        letter-spacing: .01em;
        cursor: pointer;
        box-shadow: 0 12px 34px #355ac64d;
        transition: transform 140ms ease, background 140ms ease;
      }
      #qcrt-entry .qcrt-start:hover:not(:disabled) { transform: translateY(-1px); background: #638bff; }
      #qcrt-entry .qcrt-start:focus-visible { outline: 3px solid #9db2f866; outline-offset: 3px; }
      #qcrt-entry .qcrt-start:disabled { cursor: wait; opacity: .48; box-shadow: none; }
      #qcrt-entry .qcrt-hint {
        margin: 12px 0 0;
        padding: 11px 13px;
        border: 1px solid #fbbf2433;
        border-radius: 11px;
        background: #fbbf2413;
        color: #f5d988;
        font-size: 12px;
        line-height: 1.55;
      }
      #qcrt-entry .qcrt-status {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 1px;
        margin: 16px 0 0;
        overflow: hidden;
        border: 1px solid #ffffff12;
        border-radius: 12px;
        background: #ffffff12;
      }
      #qcrt-entry .qcrt-stat { padding: 11px 13px; background: #111318; }
      #qcrt-entry .qcrt-stat span { display: block; color: #747782; font-size: 10px; letter-spacing: .08em; }
      #qcrt-entry .qcrt-stat strong { display: block; margin-top: 4px; color: #b9f6ca; font: 600 12px ui-monospace, monospace; }
      #qcrt-entry .qcrt-error {
        margin: 12px 0 0;
        color: #fca5a5;
        font-size: 12px;
        line-height: 1.45;
      }
      #qcrt-entry .qcrt-advanced {
        display: block;
        width: auto;
        min-height: 0;
        margin: 16px auto 0;
        padding: 4px;
        border: 0;
        background: transparent;
        color: #777b87;
        font: 600 11px inherit;
        letter-spacing: .03em;
        text-transform: none;
        cursor: pointer;
      }
      #qcrt-entry .qcrt-advanced:hover { color: #b7bac4; background: transparent; }
      @keyframes qcrt-arrive { from { opacity: 0; transform: translateY(10px) scale(.99); } }
      @media (max-height: 720px) {
        #qcrt-entry { place-items: start center; }
        #qcrt-entry .qcrt-panel { padding: 24px; }
        #qcrt-entry .qcrt-mode { min-height: 62px; }
      }
      @media (prefers-reduced-motion: reduce) {
        #qcrt-entry .qcrt-panel { animation: none; }
        #qcrt-entry .qcrt-start { transition: none; }
      }
    `
    document.head.appendChild(style)

    const shell = document.createElement("main")
    shell.id = "qcrt-entry"
    shell.innerHTML = `
      <section class="qcrt-panel" aria-labelledby="qcrt-title">
        <div class="qcrt-kicker"><span class="qcrt-mark"></span>Quest CRT · Operator link</div>
        <h1 id="qcrt-title">人体姿态采集</h1>
        <p class="qcrt-lede">双手、肩与肘的低延迟传输始终开启；当前额外启用 ZED 现场视频。</p>
        <div class="qcrt-modes">
          <div class="qcrt-mode">
            <div><strong>人体姿态传输</strong><small>QCRT · WebRTC 优先 · 72 Hz</small></div>
            <span class="qcrt-primary-tag">主功能</span>
          </div>
          <label class="qcrt-mode qcrt-video" for="qcrt-video-toggle">
            <div><strong>视频回传</strong><small>ZED Mini · 720p60 · CloudXR</small></div>
            <input id="qcrt-video-toggle" type="checkbox" checked />
            <span class="qcrt-switch" aria-hidden="true"></span>
          </label>
        </div>
        <button class="qcrt-start" id="qcrt-start" type="button" disabled>正在检查视频链路…</button>
        <p class="qcrt-hint">进入 XR 后留出 3 秒摆姿时间，随后自动开始人体数据传输，无需第二次点击。</p>
        <div class="qcrt-status" role="status" aria-live="polite">
          <div class="qcrt-stat"><span>VIDEO</span><strong id="qcrt-cloudxr-value">checking</strong></div>
          <div class="qcrt-stat"><span>POSE SESSION</span><strong id="qcrt-phase-value">待进入 XR</strong></div>
          <div class="qcrt-stat"><span>POSE LINK</span><strong id="qcrt-transport-value">closed</strong></div>
          <div class="qcrt-stat"><span>SENT / DROPPED</span><strong><b id="qcrt-sent-value">0</b> / <b id="qcrt-dropped-value">0</b></strong></div>
        </div>
        <p class="qcrt-error" id="qcrt-shell-error" hidden></p>
        <button class="qcrt-advanced" id="qcrt-advanced" type="button">NVIDIA CloudXR 高级设置 →</button>
      </section>
    `
    document.body.appendChild(shell)

    shellValues = {
      phase: document.querySelector("#qcrt-phase-value"),
      transport: document.querySelector("#qcrt-transport-value"),
      sent: document.querySelector("#qcrt-sent-value"),
      dropped: document.querySelector("#qcrt-dropped-value"),
      poseDot: document.querySelector(".qcrt-mark"),
    }
    const start = document.querySelector("#qcrt-start")
    const cloudxrValue = document.querySelector("#qcrt-cloudxr-value")
    const shellError = document.querySelector("#qcrt-shell-error")
    const officialStart = document.querySelector("#startButton")
    const officialRoot = document.getElementById("2d-ui")

    function syncCloudXR() {
      const ready = officialStart && !officialStart.disabled
      start.disabled = !ready
      start.textContent = ready ? "开始准备" : "正在检查视频链路…"
      cloudxrValue.textContent = ready ? "ready" : "checking"
      const error = document.querySelector("#errorMessageText")?.textContent?.trim()
      const validation = document.querySelector("#validationMessageText")?.textContent?.trim()
      shellError.textContent = error || validation || ""
      shellError.hidden = !shellError.textContent
    }

    start.addEventListener("click", () => {
      if (!officialStart || officialStart.disabled) return
      start.disabled = true
      start.textContent = "正在进入 XR…"
      officialStart.click()
    })
    document.querySelector("#qcrt-video-toggle").addEventListener("change", () => {
      location.href = `${location.protocol}//${location.hostname}:8000/`
    })
    document.querySelector("#qcrt-advanced").addEventListener("click", () => {
      const next = new URL(location.href)
      next.searchParams.set("qcrtUi", "nvidia")
      for (const key of Object.keys(PRODUCT_DEFAULTS)) next.searchParams.delete(key)
      location.href = next
    })
    if (officialRoot) {
      new MutationObserver(syncCloudXR).observe(officialRoot, {
        attributes: true,
        childList: true,
        subtree: true,
        characterData: true,
      })
    }
    syncCloudXR()
    renderStatus()
  }

  function mountInterface() {
    if (params.get("qcrtUi") === "nvidia") mountAdvancedBadge()
    else mountShell()
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mountInterface, { once: true })
  } else {
    mountInterface()
  }

  function transportIsOpen() {
    return dataChannel?.readyState === "open" || socket?.readyState === WebSocket.OPEN
  }

  function closeCurrentTransport() {
    const channel = dataChannel
    const peer = peerConnection
    const ws = socket
    dataChannel = null
    peerConnection = null
    socket = null
    try {
      channel?.close()
    } catch {}
    try {
      peer?.close()
    } catch {}
    try {
      ws?.close()
    } catch {}
  }

  function scheduleReconnect() {
    clearTimeout(reconnectTimer)
    if (!activeSession) return
    reconnectTimer = setTimeout(connectTransport, 1000)
  }

  function waitForIceGathering(peer, timeoutMs = 4000) {
    if (peer.iceGatheringState === "complete") return Promise.resolve()
    return new Promise((resolve) => {
      const timeout = setTimeout(done, timeoutMs)
      function done() {
        clearTimeout(timeout)
        peer.removeEventListener("icegatheringstatechange", changed)
        resolve()
      }
      function changed() {
        if (peer.iceGatheringState === "complete") done()
      }
      peer.addEventListener("icegatheringstatechange", changed)
    })
  }

  function waitForDataChannelOpen(channel, timeoutMs = 8000) {
    if (channel.readyState === "open") return Promise.resolve()
    return new Promise((resolve, reject) => {
      const timeout = setTimeout(() => finish(new Error("WebRTC channel timeout")), timeoutMs)
      function finish(error) {
        clearTimeout(timeout)
        channel.removeEventListener("open", opened)
        channel.removeEventListener("close", closed)
        error ? reject(error) : resolve()
      }
      function opened() {
        finish()
      }
      function closed() {
        finish(new Error("WebRTC channel closed during setup"))
      }
      channel.addEventListener("open", opened)
      channel.addEventListener("close", closed)
    })
  }

  async function connectWebRTC(attempt) {
    const peer = new RTCPeerConnection({ iceServers: [] })
    const channel = peer.createDataChannel("pose", {
      ordered: false,
      maxPacketLifeTime: RTC_PACKET_LIFETIME_MS,
    })
    peerConnection = peer
    dataChannel = channel
    peer.addEventListener("connectionstatechange", () => {
      if (peerConnection !== peer) return
      if (["failed", "closed", "disconnected"].includes(peer.connectionState)) {
        closeCurrentTransport()
        setState({ transport: "WebRTC closed" })
        scheduleReconnect()
      }
    })

    const offer = await peer.createOffer()
    await peer.setLocalDescription(offer)
    await waitForIceGathering(peer)
    if (attempt !== connectionAttempt) throw new Error("superseded connection")
    const response = await fetch(`${qcrtHttpOrigin}/api/webrtc/offer`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        sdp: peer.localDescription.sdp,
        type: peer.localDescription.type,
      }),
    })
    if (!response.ok) throw new Error(`WebRTC offer rejected (${response.status})`)
    await peer.setRemoteDescription(await response.json())
    await waitForDataChannelOpen(channel)
    if (attempt !== connectionAttempt) throw new Error("superseded connection")
    setState({ transport: "WebRTC open", lastError: null })
    channel.addEventListener("close", () => {
      if (dataChannel !== channel) return
      closeCurrentTransport()
      setState({ transport: "WebRTC closed" })
      scheduleReconnect()
    })
  }

  function connectWebSocket(attempt) {
    if (attempt !== connectionAttempt || !activeSession) return
    setState({ transport: "WSS connecting" })
    const ws = new WebSocket(`${qcrtWsOrigin}/ws`)
    socket = ws
    ws.onopen = () => {
      if (socket === ws) setState({ transport: "WSS open", lastError: null })
    }
    ws.onerror = () => {
      if (socket === ws) setState({ transport: "WSS error" })
    }
    ws.onclose = () => {
      if (socket !== ws) return
      socket = null
      setState({ transport: "WSS closed" })
      scheduleReconnect()
    }
  }

  async function connectTransport() {
    clearTimeout(reconnectTimer)
    const attempt = ++connectionAttempt
    closeCurrentTransport()
    setState({ transport: "WebRTC connecting" })
    try {
      await connectWebRTC(attempt)
    } catch (error) {
      if (attempt !== connectionAttempt || !activeSession) return
      try {
        dataChannel?.close()
        peerConnection?.close()
      } catch {}
      dataChannel = null
      peerConnection = null
      setState({ lastError: String(error) })
      connectWebSocket(attempt)
    }
  }

  function readPose(frame, space) {
    if (!space || !referenceSpace) return null
    try {
      const pose = frame.getPose(space, referenceSpace)
      if (!pose) return null
      const { x, y, z } = pose.transform.position
      const orientation = pose.transform.orientation
      return {
        position: [x, y, z],
        orientation: [orientation.x, orientation.y, orientation.z, orientation.w],
      }
    } catch {
      return null
    }
  }

  function readPosition(frame, space) {
    return readPose(frame, space)?.position ?? null
  }

  function readHand(frame, source) {
    if (!source?.hand) {
      return {
        tracked: false,
        points: HAND_JOINTS.map(() => null),
        wrist_orientation: null,
      }
    }
    const poses = HAND_JOINTS.map((jointName) => readPose(frame, source.hand.get(jointName)))
    const points = poses.map((pose) => pose?.position ?? null)
    const wristOrientation = poses[0]?.orientation ?? null
    return {
      tracked: wristOrientation !== null && points.every((point) => point !== null),
      points,
      wrist_orientation: wristOrientation,
    }
  }

  function subtractVectors(left, right) {
    return left.map((value, index) => value - right[index])
  }

  function dotVectors(left, right) {
    return left.reduce((sum, value, index) => sum + value * right[index], 0)
  }

  function crossVectors(left, right) {
    return [
      left[1] * right[2] - left[2] * right[1],
      left[2] * right[0] - left[0] * right[2],
      left[0] * right[1] - left[1] * right[0],
    ]
  }

  function normalizeVector(vector) {
    const length = Math.hypot(...vector)
    if (!Number.isFinite(length) || length < BODY_FRAME_EPSILON) return null
    return vector.map((value) => value / length)
  }

  function createUpperBackBodyFrame(upperBack, leftScapula, rightScapula) {
    if (!upperBack || !leftScapula || !rightScapula) return null
    const worldUp = [0, 1, 0]
    const xAxis = normalizeVector(
      crossVectors(
        subtractVectors(leftScapula, upperBack),
        subtractVectors(rightScapula, upperBack)
      )
    )
    if (!xAxis) return null
    const orthogonalUp = worldUp.map(
      (value, index) => value - dotVectors(worldUp, xAxis) * xAxis[index]
    )
    const yAxis = normalizeVector(orthogonalUp)
    if (!yAxis) return null
    const zAxis = normalizeVector(crossVectors(xAxis, yAxis))
    return zAxis ? { origin: upperBack, matrix: [xAxis, yAxis, zAxis] } : null
  }

  function transformPointToBodyFrame(point, bodyFrame) {
    if (!point) return null
    const delta = subtractVectors(point, bodyFrame.origin)
    return bodyFrame.matrix.map((axis) => dotVectors(axis, delta))
  }

  function quaternionToMatrix([x, y, z, w]) {
    const length = Math.hypot(x, y, z, w)
    if (!Number.isFinite(length) || length < BODY_FRAME_EPSILON) return null
    x /= length
    y /= length
    z /= length
    w /= length
    return [
      [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
      [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
      [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ]
  }

  function multiplyMatrix3(left, right) {
    return left.map((row) =>
      right[0].map((_, column) =>
        row.reduce((sum, value, index) => sum + value * right[index][column], 0)
      )
    )
  }

  function matrixToQuaternion(matrix) {
    const trace = matrix[0][0] + matrix[1][1] + matrix[2][2]
    let x
    let y
    let z
    let w
    if (trace > 0) {
      const scale = Math.sqrt(trace + 1) * 2
      x = (matrix[2][1] - matrix[1][2]) / scale
      y = (matrix[0][2] - matrix[2][0]) / scale
      z = (matrix[1][0] - matrix[0][1]) / scale
      w = 0.25 * scale
    } else if (matrix[0][0] > matrix[1][1] && matrix[0][0] > matrix[2][2]) {
      const scale = Math.sqrt(1 + matrix[0][0] - matrix[1][1] - matrix[2][2]) * 2
      x = 0.25 * scale
      y = (matrix[0][1] + matrix[1][0]) / scale
      z = (matrix[0][2] + matrix[2][0]) / scale
      w = (matrix[2][1] - matrix[1][2]) / scale
    } else if (matrix[1][1] > matrix[2][2]) {
      const scale = Math.sqrt(1 + matrix[1][1] - matrix[0][0] - matrix[2][2]) * 2
      x = (matrix[0][1] + matrix[1][0]) / scale
      y = 0.25 * scale
      z = (matrix[1][2] + matrix[2][1]) / scale
      w = (matrix[0][2] - matrix[2][0]) / scale
    } else {
      const scale = Math.sqrt(1 + matrix[2][2] - matrix[0][0] - matrix[1][1]) * 2
      x = (matrix[0][2] + matrix[2][0]) / scale
      y = (matrix[1][2] + matrix[2][1]) / scale
      z = 0.25 * scale
      w = (matrix[1][0] - matrix[0][1]) / scale
    }
    const length = Math.hypot(x, y, z, w)
    if (!Number.isFinite(length) || length < BODY_FRAME_EPSILON) return null
    const quaternion = [x / length, y / length, z / length, w / length]
    return quaternion[3] < 0 ? quaternion.map((value) => -value) : quaternion
  }

  function transformOrientationToBodyFrame(orientation, bodyFrame) {
    if (!orientation) return null
    const rotation = quaternionToMatrix(orientation)
    return rotation ? matrixToQuaternion(multiplyMatrix3(bodyFrame.matrix, rotation)) : null
  }

  function transformHandToBodyFrame(hand, bodyFrame) {
    return {
      tracked: hand.tracked,
      points: hand.points.map((point) => transformPointToBodyFrame(point, bodyFrame)),
      wrist_orientation: transformOrientationToBodyFrame(hand.wrist_orientation, bodyFrame),
    }
  }

  function encodePosePacket(packet) {
    const buffer = new ArrayBuffer(BINARY_PACKET_SIZE)
    const view = new DataView(buffer)
    let offset = 0
    for (const byte of [0x51, 0x43, 0x52, 0x54]) view.setUint8(offset++, byte)
    view.setUint8(offset++, 3)
    let flags = 0
    if (packet.hands.left.tracked) flags |= 1 << 0
    if (packet.hands.right.tracked) flags |= 1 << 1
    if (packet.elbows.left.tracked) flags |= 1 << 2
    if (packet.elbows.right.tracked) flags |= 1 << 3
    if (packet.shoulders.left.tracked) flags |= 1 << 4
    if (packet.shoulders.right.tracked) flags |= 1 << 5
    view.setUint8(offset++, flags)
    view.setUint16(offset, 0, true)
    offset += 2
    view.setUint32(offset, packet.seq, true)
    offset += 4
    view.setFloat64(offset, packet.timestamp_ms, true)
    offset += 8
    view.setFloat64(offset, packet.capture_epoch_ms, true)
    offset += 8
    const sessionHex = packet.session_id.replaceAll("-", "")
    for (let index = 0; index < 16; index += 1) {
      view.setUint8(offset++, Number.parseInt(sessionHex.slice(index * 2, index * 2 + 2), 16))
    }
    function writeVector(vector, size) {
      for (let index = 0; index < size; index += 1) {
        view.setFloat32(offset, vector?.[index] ?? Number.NaN, true)
        offset += 4
      }
    }
    for (const hand of [packet.hands.left, packet.hands.right]) {
      for (const point of hand.points) writeVector(point, 3)
    }
    for (const hand of [packet.hands.left, packet.hands.right]) {
      writeVector(hand.wrist_orientation, 4)
    }
    writeVector(packet.elbows.left.position, 3)
    writeVector(packet.elbows.right.position, 3)
    writeVector(packet.shoulders.left.position, 3)
    writeVector(packet.shoulders.right.position, 3)
    if (offset !== BINARY_PACKET_SIZE) throw new Error(`binary pose size mismatch: ${offset}`)
    return buffer
  }

  function makePacket(timestamp, frame) {
    const sources = Array.from(activeSession.inputSources)
    const leftSource = sources.find((source) => source.handedness === "left" && source.hand)
    const rightSource = sources.find((source) => source.handedness === "right" && source.hand)
    const leftHand = readHand(frame, leftSource)
    const rightHand = readHand(frame, rightSource)
    const body = frame.body
    const leftElbow = readPosition(frame, body?.get("left-arm-lower"))
    const rightElbow = readPosition(frame, body?.get("right-arm-lower"))
    const leftShoulder = readPosition(frame, body?.get("left-arm-upper"))
    const rightShoulder = readPosition(frame, body?.get("right-arm-upper"))
    const bodyFrame = createUpperBackBodyFrame(
      readPosition(frame, body?.get("spine-upper")),
      readPosition(frame, body?.get("left-scapula")),
      readPosition(frame, body?.get("right-scapula"))
    )
    if (!bodyFrame) return null
    return {
      type: "pose",
      version: 4,
      session_id: sessionId,
      seq: ++seq,
      timestamp_ms: timestamp,
      capture_epoch_ms: performance.timeOrigin + timestamp,
      reference_space: "spine-upper-scapula",
      units: "meters",
      hands: {
        left: transformHandToBodyFrame(leftHand, bodyFrame),
        right: transformHandToBodyFrame(rightHand, bodyFrame),
      },
      elbows: {
        left: {
          tracked: leftElbow !== null,
          position: transformPointToBodyFrame(leftElbow, bodyFrame),
        },
        right: {
          tracked: rightElbow !== null,
          position: transformPointToBodyFrame(rightElbow, bodyFrame),
        },
      },
      shoulders: {
        left: {
          tracked: leftShoulder !== null,
          position: transformPointToBodyFrame(leftShoulder, bodyFrame),
        },
        right: {
          tracked: rightShoulder !== null,
          position: transformPointToBodyFrame(rightShoulder, bodyFrame),
        },
      },
    }
  }

  function onXRFrame(timestamp, frame) {
    const session = activeSession
    if (!session) return
    session.requestAnimationFrame(onXRFrame)
    if (!referenceSpace || performance.now() < prepDeadlineMs) return
    const transport = dataChannel?.readyState === "open" ? dataChannel : socket
    if (!transportIsOpen() || !transport) return
    const packet = makePacket(timestamp, frame)
    if (!packet) {
      setState({ phase: "waiting-body" })
      return
    }
    const backpressured =
      transport === dataChannel
        ? transport.bufferedAmount > 0
        : transport.bufferedAmount >= WS_MAX_BUFFERED_BYTES
    if (backpressured) {
      state.dropped += 1
      if (state.dropped % 15 === 0) renderStatus()
      return
    }
    transport.send(transport === dataChannel ? encodePosePacket(packet) : JSON.stringify(packet))
    state.sent += 1
    if (state.phase !== "streaming" || state.sent % 15 === 0) {
      setState({ phase: "streaming" })
    }
  }

  async function attachToSession(session) {
    if (activeSession) return
    activeSession = session
    sessionId = crypto.randomUUID()
    prepDeadlineMs = performance.now() + PREP_COUNTDOWN_MS
    seq = 0
    setState({ phase: "preparing", sent: 0, dropped: 0, lastError: null })
    session.addEventListener(
      "end",
      () => {
        if (activeSession !== session) return
        activeSession = null
        referenceSpace = null
        connectionAttempt += 1
        clearTimeout(reconnectTimer)
        closeCurrentTransport()
        setState({ phase: "ended", transport: "closed" })
      },
      { once: true }
    )
    connectTransport()
    try {
      referenceSpace = await session.requestReferenceSpace("local-floor")
      if (activeSession === session) session.requestAnimationFrame(onXRFrame)
    } catch (error) {
      setState({ phase: "error", lastError: String(error) })
    }
  }

  const requestSession = navigator.xr.requestSession.bind(navigator.xr)
  navigator.xr.requestSession = async (mode, options) => {
    const session = await requestSession(mode, options)
    if (mode.startsWith("immersive")) attachToSession(session)
    return session
  }
})()
