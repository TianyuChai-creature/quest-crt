// Phase 2: video-link ownership lifecycle test (docs/stage1-revise.md §4).
//
// The page's module script is evaluated under stubbed browser APIs and the
// video-link ownership rules are asserted:
//   - `?video-test=1` / test button starts an owner="test" link: exactly
//     ONE live PeerConnection, no pending reconnect timer
//   - entering XR while the test link runs (acquireVideoForXR): the test
//     link is torn down first, then a fresh xr-owned link starts — never
//     two live PCs, never a reused connection
//   - XR end (videoTransport.stop()): closes only the XR-owned link,
//     owner returns to "off"
//   - start() over a live link is refused (tears down first) — no way to
//     double-create a PeerConnection or double-schedule a reconnect timer
//
// Run with: node tests/test_video_lifecycle.mjs

import { readFileSync } from "node:fs"
import { fileURLToPath } from "node:url"
import path from "node:path"

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const htmlPath = path.join(__dirname, "..", "quest_crt", "static", "index.html")
const html = readFileSync(htmlPath, "utf8")
const script = html.match(/<script type="module">([\s\S]*?)<\/script>/)[1]

// --- browser API stubs ------------------------------------------------------

function makeElement(id) {
  return {
    id,
    textContent: "",
    disabled: false,
    srcObject: null,
    classList: {
      add() {},
      remove() {},
      toggle() {},
    },
    addEventListener() {},
    play() {
      return Promise.resolve()
    },
  }
}

const allPCs = []
const livePCs = new Set()

class FakePC {
  constructor() {
    this.labels = []
    this.iceGatheringState = "complete"
    this.connectionState = "new"
    this.localDescription = { sdp: "fake", type: "offer" }
    this._stateCb = null
    allPCs.push(this)
    livePCs.add(this)
  }

  createDataChannel(label) {
    this.labels.push(label)
    return { label, readyState: "open", send() {}, close() {}, addEventListener() {} }
  }

  addTransceiver() {
    return { setCodecPreferences() {} }
  }

  createOffer() {
    return Promise.resolve({ sdp: "fake", type: "offer" })
  }

  setLocalDescription() {
    return Promise.resolve()
  }

  setRemoteDescription() {
    return Promise.resolve()
  }

  getStats() {
    return Promise.resolve(new Map())
  }

  addEventListener(type) {
    if (type === "connectionstatechange") this._stateCb = null
  }

  close() {
    livePCs.delete(this)
  }
}

const videoPCs = () => allPCs.filter((pc) => pc.labels.includes("video-control"))
const liveVideoPCs = () => videoPCs().filter((pc) => livePCs.has(pc))

globalThis.document = { querySelector: (sel) => makeElement(sel) }
globalThis.location = {
  protocol: "https:",
  hostname: "quest.test",
  host: "quest.test:8443",
  search: "?video-test=1",
}
Object.defineProperty(globalThis, "navigator", { value: { xr: undefined } })
globalThis.RTCPeerConnection = FakePC
globalThis.RTCRtpReceiver = {
  getCapabilities: () => ({
    codecs: [
      { mimeType: "video/H264", payloadType: 99 },
      { mimeType: "video/rtx", payloadType: 96 },
    ],
  }),
}
globalThis.WebSocket = class {
  static OPEN = 1
  readyState = 1
  addEventListener() {}
  close() {}
  send() {}
}
globalThis.fetch = async (url) => {
  const pathName = String(url)
  if (pathName === "/health") return { json: async () => ({ video: { port: 8002 } }) }
  if (pathName.includes("/api/webrtc/")) {
    return { ok: true, json: async () => ({ sdp: "fake", type: "answer" }) }
  }
  return { ok: false }
}

// --- evaluate the page script -----------------------------------------------

const exposedLine = `
globalThis.__exposed = {
  videoTransport,
  acquireVideoForXR,
  syncVideoTestButton,
  videoStatus,
}`
;(0, eval)(script + exposedLine) // eslint-disable-line no-eval
const { videoTransport, acquireVideoForXR, videoStatus } = globalThis.__exposed

const flush = () => new Promise((resolve) => setTimeout(resolve, 0))

// --- assertions --------------------------------------------------------------

let failures = 0
function assert(condition, message) {
  if (condition) {
    console.log(`  ok: ${message}`)
  } else {
    failures += 1
    console.error(`  FAIL: ${message}`)
  }
}

// 1. ?video-test=1 auto-starts an owner="test" link.
await flush()
await flush()
assert(videoTransport.owner === "test", "URL param starts an owner=\"test\" link")
assert(liveVideoPCs().length === 1, "exactly one live video PeerConnection in test mode")
assert(videoTransport.reconnectTimer == null, "no pending reconnect timer after success")
assert(videoStatus.textContent !== "off", "video status line shows a live state")

// 2. Entering XR over a running test link: explicit teardown, then fresh
//    xr-owned link. Never two live PCs; the connection is not reused.
const pcsBeforeXr = videoPCs().length
acquireVideoForXR()
await flush()
await flush()
assert(videoTransport.owner === "xr", "XR owns the link after acquireVideoForXR()")
assert(liveVideoPCs().length === 1, "still exactly one live video PC (old one closed)")
assert(
  videoPCs().length === pcsBeforeXr + 1,
  "XR link is a fresh PeerConnection, not the reused test one"
)
assert(videoTransport.reconnectTimer == null, "single reconnect timer discipline holds")

// 3. XR end: stop() closes the XR-owned link only; owner returns to "off".
videoTransport.stop()
await flush()
assert(videoTransport.owner === "off", "owner returns to \"off\" after XR end")
assert(liveVideoPCs().length === 0, "no live video PC after XR end")
assert(videoStatus.textContent === "off", "video status line shows off")

// 4. start() over a live link is refused: tears down first, never doubles.
videoTransport.start("test")
await flush()
await flush()
assert(videoTransport.owner === "test", "start() after XR end opens a test link")
assert(liveVideoPCs().length === 1, "start() while stopped: one live PC")
videoTransport.start("xr") // illegal double start — must tear down, not stack
await flush()
await flush()
assert(videoTransport.owner === "xr", "double start() hands ownership to xr, not a second link")
assert(liveVideoPCs().length === 1, "double start() never leaves two live PCs")
assert(videoTransport.reconnectTimer == null, "no reconnect timer after double start")

videoTransport.stop()
await flush()

if (failures > 0) {
  console.error(`\n${failures} assertion(s) failed`)
  process.exit(1)
}
console.log("\nall video-link ownership assertions passed")
process.exit(0)
