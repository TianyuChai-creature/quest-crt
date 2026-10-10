import { createCameraRenderer } from "/static/video-layer.js"

export async function offerError(response, label) {
  let detail = ""
  try {
    const body = await response.json()
    if (typeof body.detail === "string") detail = body.detail
  } catch {}
  return new Error(`${label} (${response.status})${detail ? ": " + detail : ""}`)
}

export function waitForIceGathering(peer, timeoutMs = 4000) {
  if (peer.iceGatheringState === "complete") return Promise.resolve()
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => finish(new Error("ICE gathering timed out")), timeoutMs)
    function finish(error) {
      clearTimeout(timeout)
      peer.removeEventListener("icegatheringstatechange", changed)
      error ? reject(error) : resolve()
    }
    function changed() {
      if (peer.iceGatheringState === "complete") finish()
    }
    peer.addEventListener("icegatheringstatechange", changed)
  })
}

function hasGeometry(snapshot) {
  const config = snapshot?.config
  return !!config && (snapshot.display?.projection === "plane" ||
    !!(config.left_intrinsics && (config.mode === "mono" || config.right_intrinsics)))
}

export class VideoReturn {
  constructor({ onStatus = () => {}, onConfig = () => {} } = {}) {
    this.onStatus = onStatus
    this.onConfig = onConfig
    this.video = document.createElement("video")
    this.video.muted = true
    this.video.playsInline = true
    this.enabled = false
    this.snapshot = null
    this.session = null
    this.peer = null
    this.frameReady = false
    this.attempt = 0
    this.lastFrameAt = null
    this.sourceKey = null
    this.retryAt = 0
    this.closed = false
    this.revision = 0
    this.pollTimer = setInterval(() => this.refresh(), 1000)
    this.refresh()
  }

  setEnabled(enabled) {
    this.enabled = enabled
    if (!enabled) this.disconnect()
    this.connectIfReady()
    this.tick()
  }

  setSession(session, glLayer = null, viewerSpace = null, gl = null) {
    this.session = null
    this.disconnect()
    this.binding?.dispose()
    this.session = session
    this.glLayer = glLayer
    this.viewerSpace = viewerSpace
    this.gl = gl
    this.binding = null
    this.capabilityError = null
    if (session) {
      if (!gl) {
        this.capabilityError = "Camera video unavailable: WebGL2 unsupported"
      } else if (typeof this.video.requestVideoFrameCallback !== "function") {
        this.capabilityError = "Camera video unavailable: decoded frame updates unsupported"
      } else {
        try {
          this.binding = createCameraRenderer(gl)
        } catch (error) {
          this.binding?.dispose()
          this.binding = null
          this.capabilityError = `Camera video unavailable: ${error.message}`
        }
      }
    }
    this.connectIfReady()
    this.tick()
  }

  async refresh() {
    if (this.closed || this.polling || this.saving) return
    this.polling = true
    const revision = this.revision
    try {
      const response = await fetch("/api/video/config", {
        cache: "no-store", signal: AbortSignal.timeout(4000),
      })
      if (!response.ok) throw new Error(`Camera status unavailable (${response.status})`)
      const snapshot = await response.json()
      if (!this.closed && revision === this.revision) this.acceptSnapshot(snapshot)
    } catch (error) {
      if (!this.closed && revision === this.revision) {
        this.snapshot = null
        this.disconnect()
        this.setStatus(error.message)
      }
    } finally {
      this.polling = false
    }
  }

  async configure(display) {
    this.saving = true
    this.revision += 1
    try {
      const response = await fetch("/api/video/config", {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(display), signal: AbortSignal.timeout(4000),
      })
      if (!response.ok) throw new Error(`Camera settings rejected (${response.status})`)
      const snapshot = await response.json()
      if (!this.closed) this.acceptSnapshot(snapshot)
    } finally {
      this.saving = false
    }
  }

  acceptSnapshot(snapshot) {
    const config = snapshot.config
    const key = config ? JSON.stringify([
      snapshot.stream_generation, config.width, config.height, config.mode, config.fps,
    ]) : null
    const displayKey = JSON.stringify(snapshot.display)
    if (this.binding && (key !== this.sourceKey || displayKey !== this.displayKey)) this.capabilityError = null
    if (key !== this.sourceKey || !snapshot.enabled || !snapshot.running || !hasGeometry(snapshot)) this.disconnect()
    this.sourceKey = key
    this.snapshot = snapshot
    this.displayKey = displayKey
    this.onConfig(snapshot)
    this.connectIfReady()
    this.tick()
  }

  connectIfReady() {
    if (this.closed || this.peer || !this.enabled || !this.binding || this.capabilityError ||
        !this.snapshot?.enabled || !this.snapshot.running || !hasGeometry(this.snapshot) ||
        performance.now() < this.retryAt) return
    this.connect().catch(error => {
      if (!this.closed) {
        this.connectionError = `Camera video: ${error.message}`
        this.setStatus(this.connectionError)
      }
    })
  }

  async connect() {
    const capabilities = globalThis.RTCRtpReceiver?.getCapabilities?.("video")?.codecs || []
    const h265 = capabilities.filter(codec => codec.mimeType.toLowerCase() === "video/h265")
    const profileRank = codec => {
      const profile = codec.sdpFmtpLine?.match(/(?:^|;)\s*profile-id\s*=\s*(\d+)/)?.[1] || "1"
      return profile === "1" ? 0 : profile === "2" ? 1 : 2
    }
    h265.sort((left, right) => profileRank(left) - profileRank(right))
    const codecs = [...h265, ...capabilities.filter(codec => codec.mimeType.toLowerCase() === "video/h264")]
    if (!codecs.length) {
      this.capabilityError = "Camera video unavailable: H.265 / H.264 receive unsupported"
      throw new Error("H.265 / H.264 receive unsupported")
    }
    const peer = new RTCPeerConnection({ iceServers: [] })
    const peerId = crypto.randomUUID()
    const attempt = ++this.attempt
    const abort = new AbortController()
    this.peer = peer
    this.peerId = peerId
    this.offerAbort = abort
    this.connectionError = null
    this.setStatus("Camera connecting")
    try {
      const transceiver = peer.addTransceiver("video", { direction: "recvonly" })
      if (typeof transceiver.setCodecPreferences !== "function") {
        this.capabilityError = "Camera video unavailable: codec preference negotiation unsupported"
        throw new Error("Codec preference negotiation unsupported")
      }
      transceiver.setCodecPreferences(codecs)
      peer.addEventListener("connectionstatechange", () => {
        if (this.peer !== peer) return
        if (["failed", "disconnected", "closed"].includes(peer.connectionState)) {
          this.retryAt = performance.now() + 1000
          this.disconnect()
          this.connectionError = "Camera disconnected; reconnecting"
          this.setStatus(this.connectionError)
        }
      })
      peer.addEventListener("track", event => {
        if (this.peer !== peer || event.track.kind !== "video") return
        this.video.srcObject = new MediaStream([event.track])
        event.track.addEventListener("ended", () => {
          if (this.peer !== peer) return
          this.disconnect()
          this.connectionError = "Camera stream ended"
          this.setStatus(this.connectionError)
        })
        const decoded = () => {
          if (this.peer !== peer || attempt !== this.attempt) return
          this.lastFrameAt = performance.now()
          this.frameCallback = this.video.requestVideoFrameCallback(decoded)
        }
        this.frameCallback = this.video.requestVideoFrameCallback(decoded)
        this.video.play().catch(error => {
          if (this.peer !== peer) return
          this.disconnect()
          this.connectionError = `Camera playback: ${error.message}`
          this.setStatus(this.connectionError)
        })
      })
      await peer.setLocalDescription(await peer.createOffer())
      await waitForIceGathering(peer)
      if (attempt !== this.attempt) return
      const response = await fetch("/api/video/offer", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sdp: peer.localDescription.sdp, type: peer.localDescription.type, peer_id: peerId }),
        signal: AbortSignal.any([abort.signal, AbortSignal.timeout(8000)]),
      })
      if (!response.ok) throw await offerError(response, "Video offer rejected")
      const answer = await response.json()
      if (attempt === this.attempt) await peer.setRemoteDescription(answer)
    } catch (error) {
      if (this.peer !== peer) return
      this.retryAt = performance.now() + 1000
      this.disconnect()
      throw error
    }
  }

  tick(now = performance.now()) {
    const sourceReady = this.snapshot?.enabled && this.snapshot.running && this.snapshot.config
    const fresh = this.lastFrameAt !== null && now - this.lastFrameAt <= 250 &&
      this.snapshot?.frame_age_ms !== null && this.snapshot?.frame_age_ms <= 250
    if (!this.enabled || !this.session || this.capabilityError || !sourceReady ||
        !hasGeometry(this.snapshot) || !fresh) {
      if (!this.enabled) this.setStatus("Camera off")
      else if (this.capabilityError) this.setStatus(this.capabilityError)
      else if (!sourceReady) this.setStatus(this.snapshot?.error || "Waiting for SDK camera")
      else if (!hasGeometry(this.snapshot)) this.setStatus("Camera projection needs calibrated per-eye intrinsics")
      else if (!this.session) this.setStatus("Camera ready for XR")
      else this.setStatus(this.connectionError || (this.lastFrameAt === null ? "Waiting for camera frames" : "Camera stale"))
      return
    }
    this.setStatus(this.snapshot.config.mode === "stereo" ? "Stereo camera live" : "Camera live")
  }

  beginFrame(frame, viewerPose) {
    this.frameReady = false
    if (!this.enabled || !this.session || !this.binding || this.capabilityError ||
        frame.session !== this.session || !viewerPose ||
        !this.snapshot?.enabled || !this.snapshot.running || this.lastFrameAt === null ||
        !hasGeometry(this.snapshot) ||
        performance.now() - this.lastFrameAt > 250 ||
        this.snapshot.frame_age_ms === null || !(this.snapshot.frame_age_ms <= 250)) return
    try {
      const config = this.snapshot.config
      if (this.video.videoWidth !== config.width * (config.mode === "stereo" ? 2 : 1) ||
          this.video.videoHeight !== config.height) {
        throw new Error("Decoded camera dimensions differ from the configured source")
      }
      this.binding.prepare(this.video, this.lastFrameAt)
      this.frameReady = true
    } catch (error) { this.failVideo(error) }
  }

  renderView(view, cameraView) {
    if (!this.frameReady || !cameraView) return
    try { this.binding.drawView(view, cameraView, this.snapshot.config, this.snapshot.display) }
    catch (error) { this.failVideo(error) }
  }

  failVideo(error) {
    this.capabilityError = `Camera video unavailable: ${error.message}`
    this.disconnect()
    if (this.gl?.isContextLost()) {
      this.binding.dispose()
      this.binding = null
    }
    this.setStatus(this.capabilityError)
  }

  setStatus(status) {
    if (status === this.status) return
    this.status = status
    this.onStatus(status)
  }

  disconnect() {
    this.attempt += 1
    const peerId = this.peerId
    this.peerId = null
    if (peerId) fetch("/api/video/close", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ peer_id: peerId }), keepalive: true,
    }).catch(() => {})
    this.offerAbort?.abort()
    this.offerAbort = null
    const peer = this.peer
    this.peer = null
    peer?.close()
    if (this.frameCallback !== undefined) this.video.cancelVideoFrameCallback?.(this.frameCallback)
    this.frameCallback = undefined
    this.video.pause()
    this.video.srcObject?.getTracks().forEach(track => track.stop())
    this.video.srcObject = null
    this.lastFrameAt = null
    this.frameReady = false
  }

  close() {
    this.closed = true
    clearInterval(this.pollTimer)
    this.setSession(null)
  }
}
