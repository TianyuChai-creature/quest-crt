"""Isolated Mesa pixel regression for the production stereo shader."""
import ctypes.util
import shutil
import subprocess
import sys
import unittest
from pathlib import Path


class VideoShaderTests(unittest.TestCase):
    @unittest.skipUnless(
        sys.platform == "linux" and shutil.which("node")
        and Path("/usr/share/glvnd/egl_vendor.d/50_mesa.json").is_file()
        and ctypes.util.find_library("EGL") and ctypes.util.find_library("GLESv2"),
        "requires Node and Linux Mesa/EGL",
    )
    def test_production_shader_pixels(self):
        result = subprocess.run([sys.executable, __file__, "--render"],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


def render():
    import ctypes as C
    import json
    import os
    from pathlib import Path
    import re
    import subprocess

    os.environ.update({"__EGL_VENDOR_LIBRARY_FILENAMES": "/usr/share/glvnd/egl_vendor.d/50_mesa.json",
                       "EGL_PLATFORM": "surfaceless", "LIBGL_ALWAYS_SOFTWARE": "1", "GALLIUM_DRIVER": "llvmpipe"})
    ROOT = Path(__file__).resolve().parents[1]
    source = (ROOT / "static/video-layer.js").read_text()
    egl, gl = C.CDLL("libEGL.so.1"), C.CDLL("libGLESv2.so.2")
    I, U, P, F = C.c_int, C.c_uint, C.c_void_p, C.c_float


    def api(lib, name, result, *arguments):
        function = getattr(lib, name)
        function.restype, function.argtypes = result, arguments
        return function


    def integers(*values):
        return (I * len(values))(*values)


    display = api(egl, "eglGetDisplay", P, P)(None)
    assert api(egl, "eglInitialize", U, P, C.POINTER(I), C.POINTER(I))(display, C.byref(I()), C.byref(I()))
    assert api(egl, "eglBindAPI", U, U)(0x30A0)
    config, count = P(), I()
    attributes = integers(0x3024, 8, 0x3023, 8, 0x3022, 8, 0x3021, 8, 0x3033, 1, 0x3040, 0x40, 0x3038)
    assert api(egl, "eglChooseConfig", U, P, C.POINTER(I), C.POINTER(P), I, C.POINTER(I))(
        display, attributes, C.byref(config), 1, C.byref(count)) and count.value
    surface = api(egl, "eglCreatePbufferSurface", P, P, P, C.POINTER(I))(display, config, integers(0x3057, 1, 0x3056, 1, 0x3038))
    context = api(egl, "eglCreateContext", P, P, P, P, C.POINTER(I))(display, config, None, integers(0x3098, 3, 0x3038))
    assert surface and context
    assert api(egl, "eglMakeCurrent", U, P, P, P, P)(display, surface, surface, context)
    renderer_name = api(gl, "glGetString", C.c_char_p, U)(0x1F01).decode()
    assert "llvmpipe" in renderer_name.lower(), renderer_name

    create_shader = api(gl, "glCreateShader", U, U)
    shader_source = api(gl, "glShaderSource", None, U, I, C.POINTER(C.c_char_p), C.POINTER(I))
    compile_shader = api(gl, "glCompileShader", None, U)
    shader_status = api(gl, "glGetShaderiv", None, U, U, C.POINTER(I))
    shader_log = api(gl, "glGetShaderInfoLog", None, U, I, C.POINTER(I), P)
    shaders = []
    for kind, text in re.findall(r"compile\(gl\.(VERTEX_SHADER|FRAGMENT_SHADER), `([\s\S]*?)`\)", source):
        shader = create_shader(0x8B31 if kind == "VERTEX_SHADER" else 0x8B30)
        encoded = C.c_char_p(text.encode())
        shader_source(shader, 1, C.byref(encoded), None)
        compile_shader(shader)
        success = I()
        shader_status(shader, 0x8B81, C.byref(success))
        log = C.create_string_buffer(4096)
        shader_log(shader, len(log), None, log)
        assert success.value, log.value.decode()
        shaders.append(shader)
    assert len(shaders) == 2
    program = api(gl, "glCreateProgram", U)()
    for shader in shaders:
        api(gl, "glAttachShader", None, U, U)(program, shader)
    api(gl, "glLinkProgram", None, U)(program)
    linked = I()
    api(gl, "glGetProgramiv", None, U, U, C.POINTER(I))(program, 0x8B82, C.byref(linked))
    assert linked.value
    api(gl, "glUseProgram", None, U)(program)
    vao, texture = U(), U()
    api(gl, "glGenVertexArrays", None, I, C.POINTER(U))(1, C.byref(vao))
    api(gl, "glBindVertexArray", None, U)(vao)
    api(gl, "glGenTextures", None, I, C.POINTER(U))(1, C.byref(texture))
    api(gl, "glBindTexture", None, U, U)(0x0DE1, texture)
    for property, value in ((0x2801, 0x2601), (0x2800, 0x2601), (0x2802, 0x812F), (0x2803, 0x812F)):
        api(gl, "glTexParameteri", None, U, U, I)(0x0DE1, property, value)
    # GLES has no WebGL UNPACK_FLIP_Y; reverse upload rows to emulate production's true flag.
    pixels = []
    for y in reversed(range(8)):
        for x in range(16):
            pixels.extend((32 * x, 32 * y, 0, 255) if x < 8 else (0, 32 * (x - 8), 32 * y, 255))
    data = (C.c_ubyte * len(pixels))(*pixels)
    api(gl, "glTexImage2D", None, U, I, I, I, I, I, U, U, P)(0x0DE1, 0, 0x1908, 16, 8, 0, 0x1908, 0x1401, data)
    api(gl, "glViewport", None, I, I, I, I)(0, 0, 1, 1)
    location = api(gl, "glGetUniformLocation", I, U, C.c_char_p)
    uniform1i = api(gl, "glUniform1i", None, I, I)
    uniform1f = api(gl, "glUniform1f", None, I, F)
    uniform2f = api(gl, "glUniform2f", None, I, F, F)
    uniform3f = api(gl, "glUniform3f", None, I, F, F, F)
    uniform4fv = api(gl, "glUniform4fv", None, I, I, C.POINTER(F))
    uniform_matrix = api(gl, "glUniformMatrix3fv", None, I, I, U, C.POINTER(F))

    config_data = {"width": 8, "height": 8, "mode": "stereo",
                   "left_intrinsics": {"fx": 4, "fy": 4, "cx": 2, "cy": 3},
                   "right_intrinsics": {"fx": 4, "fy": 4, "cx": 5, "cy": 1}}
    cases = [
        {"name": "left principal and Y flip", "eye": "left", "swap": False, "px": 0, "py": 0, "expected": [64, 96, 0, 255]},
        {"name": "right independent K", "eye": "right", "swap": False, "px": 0, "py": 0, "expected": [0, 160, 32, 255]},
        {"name": "swap half and K together", "eye": "left", "swap": True, "px": 0, "py": 0, "expected": [0, 160, 32, 255]},
        {"name": "swap right to left", "eye": "right", "swap": True, "px": 0, "py": 0, "expected": [64, 96, 0, 255]},
        {"name": "off-axis projection signs", "eye": "left", "swap": False, "px": .25, "py": .25, "expected": [96, 64, 0, 255]},
        {"name": "outside optical coverage black", "eye": "left", "swap": False, "px": 1.5, "py": 0, "expected": [0, 0, 0, 255]},
        {"name": "half-texel edge stays in left eye", "eye": "left", "swap": False, "px": 1.3725, "py": 0, "expected": [224, 96, 0, 255]},
        {"name": "plane center left", "eye": "left", "swap": False, "plane": True, "px": .033 / 7, "py": -1 / 7, "expected": [112, 112, 0, 255]},
        {"name": "plane center right", "eye": "right", "swap": False, "plane": True, "px": -.033 / 7, "py": -1 / 7, "expected": [0, 112, 112, 255]},
        {"name": "plane swap eyes", "eye": "left", "swap": True, "plane": True, "px": .033 / 7, "py": -1 / 7, "expected": [0, 112, 112, 255]},
        {"name": "outside plane black", "eye": "left", "swap": False, "plane": True, "px": 2, "py": 0, "expected": [0, 0, 0, 255]},
    ]
    node = r'''
    const fs = require("node:fs"), vm = require("node:vm");
    const input = JSON.parse(fs.readFileSync(0,"utf8")), source = fs.readFileSync(input.source,"utf8");
    const context = vm.createContext({Float32Array});
    vm.runInContext(source.replace(/^export /gm,"") + "\nthis.project = cameraProjection", context);
    const values = input.cases.map(c => {
      const p = new Float32Array([1,0,0,0,0,1,0,0,c.px,c.py,-1,-1,0,0,-.2,0]);
      const camera = {transform:{matrix:new Float32Array([1,0,0,0,0,1,0,0,0,0,1,0,c.eye === "right" ? .033 : -.033,0,0,1])}};
      const display = c.plane ? {projection:"plane", height_m:8, distance_m:7, aspect_ratio:1.66667, offset_y_m:-1} : {};
      const value = context.project({eye:c.eye, projectionMatrix:p},camera,input.config,c.swap,display);
      return Object.fromEntries(Object.entries(value).map(([k,v])=>[k,Array.from(v)]));
    });
    process.stdout.write(JSON.stringify(values));
    '''
    parameters = json.loads(subprocess.run(["node", "-e", node], input=json.dumps(
        {"source": str(ROOT / "static/video-layer.js"), "config": config_data, "cases": cases}), text=True,
        capture_output=True, check=True).stdout)
    uniform1i(location(program, b"videoTexture"), 0)
    uniform1f(location(program, b"saturation"), 1)
    uniform1f(location(program, b"gamma"), 1)
    for case, values in zip(cases, parameters):
        for field, uniform in (("projection", "nativeProjection"), ("intrinsics", "intrinsics")):
            array = (F * 4)(*values[field])
            uniform4fv(location(program, uniform.encode()), 1, array)
        uniform_matrix(location(program, b"eyeToHead"), 1, 0, (F * 9)(*values["rotation"]))
        uniform2f(location(program, b"imageSize"), *values["imageSize"])
        uniform2f(location(program, b"crop"), *values["crop"])
        uniform1i(location(program, b"usePlane"), int(case.get("plane", False)))
        uniform3f(location(program, b"eyePosition"), *values["eyePosition"])
        uniform4fv(location(program, b"plane"), 1, (F * 4)(*values["plane"]))
        uniform1f(location(program, b"planeDistance"), 7)
        api(gl, "glDrawArrays", None, U, I, I)(0x0004, 0, 3)
        rgba = (C.c_ubyte * 4)()
        api(gl, "glReadPixels", None, I, I, I, I, U, U, P)(0, 0, 1, 1, 0x1908, 0x1401, rgba)
        error = api(gl, "glGetError", U)()
        assert error == 0, hex(error)
        actual = list(rgba)
        assert max(abs(a - b) for a, b in zip(actual, case["expected"])) <= 1, (case["name"], actual, case["expected"])
        print(case["name"], actual)
    api(gl, "glDeleteTextures", None, I, C.POINTER(U))(1, C.byref(texture))
    api(gl, "glDeleteVertexArrays", None, I, C.POINTER(U))(1, C.byref(vao))
    api(gl, "glDeleteProgram", None, U)(program)
    for shader in shaders:
        api(gl, "glDeleteShader", None, U)(shader)
    api(egl, "eglMakeCurrent", U, P, P, P, P)(display, None, None, None)
    api(egl, "eglDestroyContext", U, P, P)(display, context)
    api(egl, "eglDestroySurface", U, P, P)(display, surface)
    api(egl, "eglTerminate", U, P)(display)
    print("Actual production shader:", len(cases), "pixel cases passed on", renderer_name)


if __name__ == "__main__":
    if "--render" in sys.argv:
        render()
    else:
        unittest.main()
