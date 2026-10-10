export function cameraProjection(view, cameraView, config, swapEyes, display = {}) {
  const stereo = config.mode === "stereo"
  let eye = stereo && view.eye === "right" ? 1 : 0
  if (stereo && swapEyes) eye = 1 - eye
  const intrinsics = (eye ? config.right_intrinsics : config.left_intrinsics) ??
    (display.projection === "plane" ? {fx: 1, fy: 1, cx: 0, cy: 0} : null)
  const projection = view.projectionMatrix, rotation = cameraView.transform.matrix
  return {
    projection: new Float32Array([projection[0], projection[5], projection[8], projection[9]]),
    rotation: new Float32Array([
      rotation[0], rotation[1], rotation[2], rotation[4], rotation[5], rotation[6],
      rotation[8], rotation[9], rotation[10],
    ]),
    intrinsics: new Float32Array([intrinsics.fx, intrinsics.fy, intrinsics.cx, intrinsics.cy]),
    imageSize: [config.width, config.height],
    crop: [stereo ? 0.5 : 1, stereo ? eye * 0.5 : 0],
    eyePosition: [rotation[12], rotation[13], rotation[14]],
    plane: [(display.height_m ?? 1) * (display.aspect_ratio ?? config.width / config.height),
      display.height_m ?? 1, display.offset_x_m ?? 0, display.offset_y_m ?? 0],
  }
}

export function createCameraRenderer(gl) {
  const program = gl.createProgram(), shaders = []
  const texture = gl.createTexture(), vao = gl.createVertexArray()
  let lastFrame = null

  function dispose() {
    gl.bindTexture(gl.TEXTURE_2D, null)
    gl.bindVertexArray(null)
    gl.deleteTexture(texture)
    gl.deleteVertexArray(vao)
    gl.deleteProgram(program)
    shaders.forEach(shader => gl.deleteShader(shader))
  }

  function checkGL(operation) {
    const error = gl.getError()
    if (error !== gl.NO_ERROR) throw new Error(`Camera ${operation}: WebGL 0x${error.toString(16)}`)
  }

  function compile(type, source) {
    const shader = gl.createShader(type)
    if (!shader) throw new Error("Camera shader allocation failed")
    shaders.push(shader)
    gl.shaderSource(shader, source)
    gl.compileShader(shader)
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
      throw new Error(gl.getShaderInfoLog(shader) || "Camera shader compilation failed")
    }
    gl.attachShader(program, shader)
  }

  try {
    if (!program || !texture || !vao) throw new Error("Camera GPU allocation failed")
    compile(gl.VERTEX_SHADER, `#version 300 es
      out vec2 ndc;
      void main() {
        vec2 p = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2)) * 2.0 - 1.0;
        ndc = p;
        gl_Position = vec4(p, 0.0, 1.0);
      }`)
    compile(gl.FRAGMENT_SHADER, `#version 300 es
      precision highp float;
      in vec2 ndc;
      uniform sampler2D videoTexture;
      uniform vec4 nativeProjection;
      uniform mat3 eyeToHead;
      uniform vec4 intrinsics;
      uniform vec2 imageSize;
      uniform vec2 crop;
      uniform bool usePlane;
      uniform vec3 eyePosition;
      uniform vec4 plane;
      uniform float planeDistance;
      uniform float saturation;
      uniform float gamma;
      out vec4 color;
      void main() {
        vec3 eyeRay = vec3((ndc + nativeProjection.zw) / nativeProjection.xy, -1.0);
        vec3 headRay = eyeToHead * eyeRay;
        vec3 cameraRay = vec3(headRay.x, -headRay.y, -headRay.z);
        if (cameraRay.z <= 0.0) { color = vec4(0.0, 0.0, 0.0, 1.0); return; }
        vec2 pixel;
        if (usePlane) {
          float t = (-planeDistance - eyePosition.z) / headRay.z;
          if (t <= 0.0) { color = vec4(0.0, 0.0, 0.0, 1.0); return; }
          vec2 point = (eyePosition + t * headRay).xy;
          vec2 planeUv = vec2((point.x - plane.z) / plane.x + 0.5,
                             0.5 - (point.y - plane.w) / plane.y);
          pixel = planeUv * imageSize - 0.5;
        } else {
          pixel = intrinsics.xy * cameraRay.xy / cameraRay.z + intrinsics.zw;
        }
        if (any(lessThan(pixel, vec2(-0.5))) || any(greaterThan(pixel, imageSize - 0.5))) {
          color = vec4(0.0, 0.0, 0.0, 1.0); return;
        }
        vec2 uv = clamp((pixel + 0.5) / imageSize, 0.5 / imageSize, 1.0 - 0.5 / imageSize);
        uv = vec2(uv.x * crop.x + crop.y, 1.0 - uv.y);
        vec3 rgb = texture(videoTexture, uv).rgb;
        float luma = dot(rgb, vec3(0.2126, 0.7152, 0.0722));
        rgb = clamp(mix(vec3(luma), rgb, saturation), 0.0, 1.0);
        color = vec4(pow(rgb, vec3(1.0 / gamma)), 1.0);
      }`)
    gl.linkProgram(program)
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      throw new Error(gl.getProgramInfoLog(program) || "Camera shader linking failed")
    }
    gl.bindTexture(gl.TEXTURE_2D, texture)
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR)
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR)
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE)
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE)
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 1, 1, 0, gl.RGBA,
                  gl.UNSIGNED_BYTE, new Uint8Array([0, 0, 0, 255]))
    gl.bindTexture(gl.TEXTURE_2D, null)
    checkGL("initialization")
  } catch (error) {
    dispose()
    throw error
  }

  const uniforms = Object.fromEntries([
    "videoTexture", "nativeProjection", "eyeToHead", "intrinsics", "imageSize", "crop", "saturation", "gamma",
    "usePlane", "eyePosition", "plane", "planeDistance",
  ].map(name => [name, gl.getUniformLocation(program, name)]))

  return {
    prepare(video, frameStamp) {
      if (gl.isContextLost()) throw new Error("Camera WebGL context is lost")
      // Pose/HUD share this context; only our new errors belong to camera video.
      for (let count = 0; count < 16; count++) if (gl.getError() === gl.NO_ERROR) break
      const framebuffer = gl.getParameter(gl.FRAMEBUFFER_BINDING)
      try {
        gl.bindFramebuffer(gl.FRAMEBUFFER, null)
        gl.useProgram(program)
        gl.bindVertexArray(vao)
        gl.activeTexture(gl.TEXTURE0)
        gl.bindTexture(gl.TEXTURE_2D, texture)
        checkGL("setup")
        if (lastFrame !== frameStamp) {
          gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true)
          checkGL("upload setup")
          // shortcut: Quest 152 HEVC rejects texSubImage; revisit after browser upload tests pass.
          gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, video)
          checkGL("upload")
          lastFrame = frameStamp
        }
      } finally {
        gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false)
        gl.bindTexture(gl.TEXTURE_2D, null)
        gl.bindVertexArray(null)
        gl.bindFramebuffer(gl.FRAMEBUFFER, framebuffer)
      }
    },

    drawView(view, cameraView, config, display) {
      const parameters = cameraProjection(view, cameraView, config, display.swap_eyes, display)
      try {
        gl.useProgram(program)
        gl.bindVertexArray(vao)
        gl.activeTexture(gl.TEXTURE0)
        gl.bindTexture(gl.TEXTURE_2D, texture)
        gl.disable(gl.BLEND)
        gl.disable(gl.DEPTH_TEST)
        gl.disable(gl.CULL_FACE)
        gl.disable(gl.SCISSOR_TEST)
        gl.uniform1i(uniforms.videoTexture, 0)
        gl.uniform4fv(uniforms.nativeProjection, parameters.projection)
        gl.uniformMatrix3fv(uniforms.eyeToHead, false, parameters.rotation)
        gl.uniform4fv(uniforms.intrinsics, parameters.intrinsics)
        gl.uniform2f(uniforms.imageSize, ...parameters.imageSize)
        gl.uniform2f(uniforms.crop, ...parameters.crop)
        gl.uniform1i(uniforms.usePlane, display.projection === "plane" ? 1 : 0)
        gl.uniform3f(uniforms.eyePosition, ...parameters.eyePosition)
        gl.uniform4fv(uniforms.plane, parameters.plane)
        gl.uniform1f(uniforms.planeDistance, display.distance_m ?? 1)
        gl.uniform1f(uniforms.saturation, display.saturation ?? 1)
        gl.uniform1f(uniforms.gamma, display.gamma ?? 1)
        checkGL("draw setup")
        gl.drawArrays(gl.TRIANGLES, 0, 3)
        checkGL("draw")
      } finally {
        gl.bindTexture(gl.TEXTURE_2D, null)
        gl.bindVertexArray(null)
      }
    },

    dispose,
  }
}
