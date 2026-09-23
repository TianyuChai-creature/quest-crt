// Run: node tests/test_transport_override.cjs
const assert = require("node:assert/strict")
const fs = require("node:fs")
const path = require("node:path")
const vm = require("node:vm")

const html = fs.readFileSync(path.join(__dirname, "../static/index.html"), "utf8")
const source = html.slice(html.indexOf("async function connectTransport()"), html.indexOf("function readPose("))

async function check(search, failRTC, expected) {
  const calls = []
  const context = vm.createContext({
    URLSearchParams, location: { search }, clearTimeout,
    reconnectTimer: null, connectionAttempt: 0,
    dataChannel: null, peerConnection: null,
    closeCurrentTransport() { calls.push("close") },
    setConnectionStatus() {},
    async connectWebRTC() {
      calls.push("webrtc")
      if (failRTC) throw new Error("connection failed")
    },
    connectWebSocket() { calls.push("wss") },
  })
  await vm.runInContext(source + "\nconnectTransport()", context)
  assert.deepEqual(calls, expected)
}

;(async () => {
  await check("?transport=wss", false, ["close", "wss"])
  await check("", false, ["close", "webrtc"])
  await check("", true, ["close", "webrtc", "wss"])
  console.log("Transport selection checks passed")
})().catch(error => { console.error(error); process.exitCode = 1 })
