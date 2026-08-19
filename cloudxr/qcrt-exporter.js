;(() => {
  "use strict"

  const params = new URLSearchParams(location.search)
  if (params.get("qcrt") === "off" || !navigator.xr) return

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
    if (!badge) return
    badge.textContent = `QCRT ${state.phase} | ${state.transport} | ${state.sent}`
    badge.style.background = state.lastError ? "#8b1d1d" : "#173b20"
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

  function mountBadge() {
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
    renderStatus()
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mountBadge, { once: true })
  } else {
    mountBadge()
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
