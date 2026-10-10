const assert = require("node:assert/strict")
const fs = require("node:fs")
const vm = require("node:vm")
const source = fs.readFileSync(__dirname + "/../static/video.js", "utf8")
const flush = () => new Promise(setImmediate)

function environment({prepareFails = false, drawFails = false, contextLost = false,
                      initFails = false, calibrated = true, h264 = true,
                      offerDetail = null, deferredOffer = false, codecs = null, projection = "camera"} = {}) {
  let now = 0, sequence = 0, callbackId = 0, resolveOffer
  let snapshot = {enabled: true, running: true, frame_age_ms: 0, stream_generation: 1,
    config: {width: 1280, height: 720, fps: 60, mode: "stereo",
      left_intrinsics: calibrated ? {fx: 727, fy: 728, cx: 627, cy: 361} : null,
      right_intrinsics: calibrated ? {fx: 726, fy: 729, cx: 631, cy: 360} : null},
    display: {swap_eyes: false, saturation: 1, gamma: 1, projection}}
  const callbacks = new Map(), peers = [], requests = [], renderers = [], prepared = [], drawn = []
  const gl = {isContextLost: () => contextLost}
  const session = {ended: false}, viewerPose = {views: [{eye: "left"}, {eye: "right"}]}
  const frame = {session}, glLayer = {name: "shared pose framebuffer"}
  const video = {videoWidth: 2560, videoHeight: 720, srcObject: null,
    play: async () => {}, pause() {},
    requestVideoFrameCallback(callback) {callbacks.set(++callbackId, callback); return callbackId},
    cancelVideoFrameCallback(id) {callbacks.delete(id)}}
  class Peer {
    constructor() {this.events = {}; this.iceGatheringState = "complete"; peers.push(this)}
    addTransceiver(kind, options) {
      assert.equal(kind, "video"); assert.equal(options.direction, "recvonly")
      return {setCodecPreferences: value => {this.codecs = value}}
    }
    addEventListener(name, handler) {this.events[name] = handler}
    async createOffer() {return {type: "offer", sdp: "unaltered browser offer"}}
    async setLocalDescription(offer) {this.localDescription = offer}
    async setRemoteDescription(answer) {this.answer = answer}
    close() {this.closed = true}
  }
  const context = vm.createContext({performance: {now: () => now},
    crypto: {randomUUID: () => `00000000-0000-4000-8000-${String(++sequence).padStart(12, "0")}`},
    document: {createElement: () => video}, AbortSignal, AbortController, setTimeout, clearTimeout,
    setInterval: () => 1, clearInterval() {}, RTCPeerConnection: Peer,
    RTCRtpReceiver: {getCapabilities: () => ({codecs: codecs || (h264 ? [{mimeType: "video/H264"}] : [])})},
    MediaStream: class {constructor(tracks) {this.tracks = tracks} getTracks() {return this.tracks}},
    createCameraRenderer(actualGL) {
      assert.equal(actualGL, gl)
      if (initFails) throw new Error("GPU initialization failed")
      const renderer = {
        prepare(input, stamp) {
          if (prepareFails || contextLost) throw new Error("GPU upload failed")
          assert.equal(input, video); prepared.push(stamp)
        },
        drawView(view, cameraView, config, display) {
          if (drawFails) throw new Error("GPU draw failed")
          drawn.push({view, cameraView, config, display: {...display}})
        },
        dispose() {this.disposed = true},
      }
      renderers.push(renderer)
      return renderer
    },
    async fetch(url, options = {}) {
      requests.push({url, ...options})
      if (url === "/api/video/close") return {ok: true, json: async () => ({closed: true})}
      if (url === "/api/video/offer") {
        if (deferredOffer) await new Promise(resolve => {resolveOffer = resolve})
        if (offerDetail) return {ok: false, status: 400, json: async () => ({detail: offerDetail})}
        return {ok: true, json: async () => ({type: "answer", sdp: "answer"})}
      }
      if (options.method === "PUT") snapshot = {...snapshot, display: JSON.parse(options.body)}
      return {ok: true, json: async () => structuredClone(snapshot)}
    },
  })
  vm.runInContext(source.replace(/^import .*$/gm, "").replace(/^export /gm, "") +
    "\nthis.VideoReturn = VideoReturn; this.offerError = offerError", context)
  const receiver = new context.VideoReturn()
  return {receiver, video, peers, requests, renderers, prepared, drawn, session, viewerPose, frame, glLayer, gl,
    get snapshot() {return structuredClone(snapshot)}, setSnapshot(value) {snapshot = value},
    time(value) {now = value}, resolveOffer: () => resolveOffer(), offerError: context.offerError,
    track(peer = peers.at(-1)) {
      const track = {kind: "video", addEventListener() {}, stop() {this.stopped = true}}
      peer.events.track({track}); return track
    },
    decoded() {
      const [id, callback] = callbacks.entries().next().value
      callbacks.delete(id); callback()
    },
    start() {receiver.setSession(session, glLayer, {}, gl); receiver.setEnabled(true)},
  }
}

async function check() {
  const env = environment(), {receiver} = env
  await flush()
  assert.equal(env.peers.length, 0)
  receiver.setSession(env.session, env.glLayer, {}, env.gl)
  assert.equal(env.peers.length, 0, "video defaults off")
  receiver.setEnabled(true); await flush()
  const peer = env.peers[0], offer = env.requests.find(request => request.url.endsWith("/offer"))
  const offered = JSON.parse(offer.body)
  assert.equal(offered.sdp, "unaltered browser offer")
  assert.equal(offered.type, "offer")
  assert.match(offered.peer_id, /^[0-9a-f-]{36}$/)
  const track = env.track()
  receiver.beginFrame(env.frame, env.viewerPose)
  receiver.renderView({eye: "left"}, {})
  assert.equal(env.drawn.length, 0, "metadata alone cannot show a frame")
  env.time(10); env.decoded(); receiver.tick()
  receiver.beginFrame(env.frame, env.viewerPose)
  receiver.renderView({eye: "left"}, {eye: "left"})
  receiver.renderView({eye: "right"}, {eye: "right"})
  assert.deepEqual(env.prepared, [10], "prepare once, then use that frame for both eyes")
  assert.equal(env.drawn.length, 2)
  env.time(261); receiver.beginFrame(env.frame, env.viewerPose); receiver.renderView({eye: "left"}, {})
  assert.equal(env.drawn.length, 2, "250 ms without a decoded frame hides the previous image")
  assert.equal(peer.closed, undefined)
  env.time(270); env.decoded()
  const changed = env.snapshot
  changed.display = {swap_eyes: true, saturation: 1.4, gamma: 1.2}
  receiver.acceptSnapshot(changed)
  receiver.beginFrame(env.frame, env.viewerPose); receiver.renderView({eye: "left"}, {})
  assert.equal(env.drawn.at(-1).display.swap_eyes, true)
  assert.equal(env.drawn.at(-1).display.saturation, 1.4)
  assert.equal(env.drawn.at(-1).display.gamma, 1.2)
  assert.equal(env.renderers.length, 1)
  assert.equal(env.peers.length, 1, "display updates allocate no connection or native layer")
  changed.config.right_intrinsics = null
  receiver.acceptSnapshot(changed)
  assert.equal(peer.closed, true)
  assert.equal(track.stopped, true)
  assert(receiver.status.includes("needs calibrated"))
  receiver.beginFrame(env.frame, env.viewerPose); receiver.renderView({eye: "left"}, {})
  assert.equal(env.drawn.length, 3, "loss of calibration hides the source and releases its peer")
  const close = env.requests.find(request => request.url === "/api/video/close")
  assert.equal(JSON.parse(close.body).peer_id, offered.peer_id)
  assert.equal(close.keepalive, true)
  assert.equal(env.session.ended, false)
  changed.config.mode = "mono"
  env.video.videoWidth = 1280
  receiver.acceptSnapshot(changed); await flush()
  assert.equal(env.peers.length, 2, "mono requires only its declared left intrinsics")
  assert.notEqual(JSON.parse(env.requests.filter(request => request.url.endsWith("/offer")).at(-1).body).peer_id, offered.peer_id)
  env.track(); env.decoded(); receiver.beginFrame(env.frame, env.viewerPose)
  receiver.renderView({eye: "right"}, {eye: "right"})
  assert.equal(env.drawn.at(-1).config.mode, "mono")
  receiver.setSession(null)
  assert.equal(env.peers.at(-1).closed, true)
  assert.equal(env.renderers[0].disposed, true)
  receiver.close()

  const retry = environment()
  await flush(); retry.start(); await flush()
  const old = retry.peers[0]
  old.connectionState = "failed"; old.events.connectionstatechange()
  assert.equal(old.closed, true)
  retry.time(2000); await retry.receiver.refresh(); await flush()
  assert.equal(retry.peers.length, 2)
  const restart = retry.snapshot
  restart.stream_generation += 1
  retry.receiver.acceptSnapshot(restart); await flush()
  assert.equal(retry.peers[1].closed, true)
  assert.equal(retry.peers.length, 3)
  retry.receiver.close()

  for (const flags of [{prepareFails: true}, {drawFails: true}, {contextLost: true}]) {
    const failed = environment(flags)
    await flush(); failed.start(); await flush(); failed.track(); failed.decoded()
    failed.receiver.beginFrame(failed.frame, failed.viewerPose)
    failed.receiver.renderView({eye: "left"}, {})
    assert.equal(failed.peers[0].closed, true)
    assert.equal(failed.session.ended, false, "video failures preserve XR and pose")
    assert.equal(failed.receiver.frameReady, false)
    if (flags.contextLost) assert.equal(failed.renderers[0].disposed, true)
    failed.receiver.close()
    assert.equal(failed.renderers[0].disposed, true)
  }
  for (const flags of [{initFails: true}, {calibrated: false}, {h264: false}]) {
    const missing = environment(flags)
    await flush(); missing.start(); await flush()
    assert.equal(missing.session.ended, false)
    if (flags.h264) assert.equal(missing.peers[0]?.closed, undefined)
    else assert.equal(missing.peers.length, 0)
    missing.receiver.close()
  }
  const pending = environment({deferredOffer: true})
  const plane = environment({calibrated: false, projection: "plane"})
  await flush(); plane.start(); await flush(); plane.track(); plane.decoded()
  plane.receiver.beginFrame(plane.frame, plane.viewerPose)
  plane.receiver.renderView({eye: "right"}, {})
  assert.equal(plane.drawn.length, 1, "an explicit video plane needs no fabricated camera intrinsics")
  plane.receiver.close()
  await flush(); pending.start(); await flush()
  const pendingId = JSON.parse(pending.requests.find(request => request.url.endsWith("/offer")).body).peer_id
  pending.receiver.setSession(null)
  assert.equal(JSON.parse(pending.requests.find(request => request.url.endsWith("/close")).body).peer_id, pendingId)
  pending.resolveOffer(); await flush()
  assert.equal(pending.peers[0].answer, undefined, "late offer replies cannot revive an exited XR session")
  pending.receiver.close()
  const rejected = environment({offerDetail: "receiver codec level is insufficient"})
  await flush(); rejected.start(); await flush()
  assert(rejected.receiver.connectionError.includes("receiver codec level is insufficient"))
  assert.equal(rejected.peers[0].closed, true)
  rejected.receiver.close()
  const codecs = [{mimeType: "video/H264"}, {mimeType: "video/H265", sdpFmtpLine: "profile-id=2"},
    {mimeType: "video/H265", sdpFmtpLine: "profile-id=1"}].map(Object.freeze)
  const hevc = environment({codecs})
  await flush(); hevc.start(); await flush()
  assert.equal(hevc.peers[0].codecs[0], codecs[2])
  assert.equal(hevc.peers[0].codecs[1], codecs[1])
  assert.equal(hevc.peers[0].codecs[2], codecs[0])
  hevc.receiver.close()
  console.log("Calibrated video freshness, paired views, nonce cleanup, reconnect and isolation checks passed")
}
check().catch(error => {console.error(error); process.exitCode = 1})
