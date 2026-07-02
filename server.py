import argparse
import base64
import io
import json
import math
import mimetypes
import multiprocessing as mp
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen

PINNED_PILLOW_VERSION = "12.2.0"
PINNED_RENDER_DEPS = [
    ("numpy", "numpy", "1.26.4"),
    ("trimesh", "trimesh", "4.12.2"),
    ("pyrender", "pyrender", "0.1.45"),
]
RENDER_CACHE_MAX = 3
RENDER_CACHE = {}
RENDER_CACHE_ORDER = []
RENDER_CACHE_LOCK = threading.RLock()
API_VERSION = "1.0.0"

try:
    import PIL
    from PIL import Image
    INSTALLED_PILLOW_VERSION = PIL.__version__
except Exception:
    Image = None
    INSTALLED_PILLOW_VERSION = None


def ensure_pillow():
    global Image, INSTALLED_PILLOW_VERSION
    if Image is not None and INSTALLED_PILLOW_VERSION == PINNED_PILLOW_VERSION:
        return
    if INSTALLED_PILLOW_VERSION:
        print(f"Pillow {INSTALLED_PILLOW_VERSION} found; installing pinned Pillow {PINNED_PILLOW_VERSION}...")
    else:
        print(f"Installing pinned Pillow {PINNED_PILLOW_VERSION} for texture support...")
    try:
        subprocess.call([sys.executable, "-m", "ensurepip", "--upgrade"])
    except Exception:
        pass
    package = f"Pillow=={PINNED_PILLOW_VERSION}"
    install_commands = [
        [sys.executable, "-m", "pip", "install", "--force-reinstall", "--no-cache-dir", "--disable-pip-version-check", package],
        [sys.executable, "-m", "pip", "install", "--user", "--force-reinstall", "--no-cache-dir", "--disable-pip-version-check", package],
    ]
    last_error = None
    for command in install_commands:
        try:
            subprocess.check_call(command)
            last_error = None
            break
        except Exception as exc:
            last_error = exc
    if last_error is not None:
        raise RuntimeError(f"Could not auto-install Pillow {PINNED_PILLOW_VERSION}. Connect to the internet and run start_server.bat again.") from last_error
    for name in list(sys.modules):
        if name == "PIL" or name.startswith("PIL."):
            del sys.modules[name]
    import PIL as PillowPackage
    from PIL import Image as PillowImage
    if PillowPackage.__version__ != PINNED_PILLOW_VERSION:
        raise RuntimeError(f"Installed Pillow {PillowPackage.__version__}, but this package is pinned to Pillow {PINNED_PILLOW_VERSION}.")
    Image = PillowImage
    INSTALLED_PILLOW_VERSION = PillowPackage.__version__


def ensure_render_deps():
    missing = []
    for module_name, package_name, wanted_version in PINNED_RENDER_DEPS:
        try:
            module = __import__(module_name)
            found_version = getattr(module, "__version__", None)
            if found_version != wanted_version:
                missing.append(f"{package_name}=={wanted_version}")
        except Exception:
            missing.append(f"{package_name}=={wanted_version}")
    if not missing:
        return
    try:
        subprocess.call([sys.executable, "-m", "ensurepip", "--upgrade"])
    except Exception:
        pass
    command = [
        sys.executable, "-m", "pip", "install",
        "--disable-pip-version-check",
        "--no-cache-dir",
        *missing,
    ]
    try:
        subprocess.check_call(command)
    except Exception as exc:
        packages = ", ".join(missing)
        raise RuntimeError(f"Could not install render preview packages: {packages}") from exc
    for module_name, _, _ in PINNED_RENDER_DEPS:
        for name in list(sys.modules):
            if name == module_name or name.startswith(module_name + "."):
                del sys.modules[name]


ROOT = Path(__file__).resolve().parent
MODELS_DIR = ROOT / "3d_models"
BUILDS_DIR = ROOT / "builds"
STATE_FILE = BUILDS_DIR / ".tool_state.json"
GLB = MODELS_DIR / "model.glb"
OUT_DIR = BUILDS_DIR / "model"
OUT_BASE = "model"
CELL = (0.1, 0.1, 0.1)
FIT_SHRINK = 10.0
ROTATION_DEGREES = (0.0, 0.0, 0.0)
SPLIT_SIZE = 100_000
FACE_FILL = 2.0
SMALL_DETAIL_BOOST = 1.0
FILL_RADIUS = 1
MERGE_TOLERANCE = 12
CHUNKS_PER_CORE = 4
SURFACE_SLABS = False
SURFACE_OVERLAP = 1.08
MERGE_TRIANGLE_QUADS = False
ROBLOX_SLOPE_DROP = 0.5
ROBLOX_WEDGE_FILL = 2.0
ROBLOX_TERRAIN_DETAIL = 1.0
QUAD_NORMAL_DOT = 0.9995
MARKER_POINTS = [
    (-35.0, 6.100000381469727, -48.0),
    (-77.0, 6.100000381469727, -148.5),
    (78.0, 6.100000381469727, -148.5),
    (78.0, 6.100000381469727, 148.5),
    (-77.0, 6.100000381469727, 148.5),
]


def clean(value):
    rounded = round(value, 6)
    return 0 if rounded == -0.0 else rounded


def rgb_hex(color):
    return "".join(f"{max(0, min(255, round(c))):02x}" for c in color)


def color_hex(color):
    if isinstance(color, tuple) or isinstance(color, list):
        return rgb_hex(color[:3])
    return f"{color & 0xFFFFFF:06x}"


def color_transparency(color):
    if isinstance(color, tuple) or isinstance(color, list):
        return color[3] if len(color) >= 4 else 0
    return ((color >> 24) & 0xFF) / 4


def mat_identity():
    return [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]


def mat_mul(a, b):
    out = [0] * 16
    for row in range(4):
        for col in range(4):
            out[col * 4 + row] = sum(a[i * 4 + row] * b[col * 4 + i] for i in range(4))
    return out


def mat_vec_mul(m, p):
    return (
        m[0] * p[0] + m[4] * p[1] + m[8] * p[2] + m[12],
        m[1] * p[0] + m[5] * p[1] + m[9] * p[2] + m[13],
        m[2] * p[0] + m[6] * p[1] + m[10] * p[2] + m[14],
    )


def mat_add_scaled(acc, m, weight):
    for i in range(16):
        acc[i] += m[i] * weight


def trs_matrix(t=None, r=None, s=None):
    t = t or [0, 0, 0]
    r = r or [0, 0, 0, 1]
    s = s or [1, 1, 1]
    x, y, z, w = r
    x2, y2, z2 = x + x, y + y, z + z
    xx, xy, xz = x * x2, x * y2, x * z2
    yy, yz, zz = y * y2, y * z2, z * z2
    wx, wy, wz = w * x2, w * y2, w * z2
    return [
        (1 - (yy + zz)) * s[0], (xy + wz) * s[0], (xz - wy) * s[0], 0,
        (xy - wz) * s[1], (1 - (xx + zz)) * s[1], (yz + wx) * s[1], 0,
        (xz + wy) * s[2], (yz - wx) * s[2], (1 - (xx + yy)) * s[2], 0,
        t[0], t[1], t[2], 1,
    ]


def transform(m, p):
    return (
        m[0] * p[0] + m[4] * p[1] + m[8] * p[2] + m[12],
        m[1] * p[0] + m[5] * p[1] + m[9] * p[2] + m[13],
        m[2] * p[0] + m[6] * p[1] + m[10] * p[2] + m[14],
    )


def rotate_point(p):
    x, y, z = p
    rx, ry, rz = (math.radians(v) for v in ROTATION_DEGREES)
    if rx:
        c, s = math.cos(rx), math.sin(rx)
        y, z = y * c - z * s, y * s + z * c
    if ry:
        c, s = math.cos(ry), math.sin(ry)
        x, z = x * c + z * s, -x * s + z * c
    if rz:
        c, s = math.cos(rz), math.sin(rz)
        x, y = x * c - y * s, x * s + y * c
    return (x, y, z)


def rotate_vector(v):
    return rotate_point(v)


def read_glb(path):
    data = path.read_bytes()
    if data[:4] != b"glTF":
        raise ValueError("not a GLB")
    off = 12
    gltf = None
    bin_chunk = None
    while off + 8 <= len(data):
        length, chunk_type = struct.unpack_from("<I4s", data, off)
        off += 8
        chunk = data[off:off + length]
        off += length
        if chunk_type == b"JSON":
            gltf = json.loads(chunk.decode("utf-8"))
        elif chunk_type == b"BIN\x00":
            bin_chunk = chunk
    return gltf, bin_chunk


def read_gltf_asset(path):
    ext = path.suffix.lower()
    if ext == ".glb":
        gltf, bin_chunk = read_glb(path)
        gltf["_base_dir"] = str(path.parent)
        return gltf, [bin_chunk]
    if ext != ".gltf":
        raise ValueError(f"unsupported glTF file: {path.suffix}")
    gltf = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    gltf["_base_dir"] = str(path.parent)
    buffers = []
    for buffer in gltf.get("buffers", []):
        uri = buffer.get("uri")
        if not uri:
            raise ValueError("external .gltf buffer is missing a uri")
        buffers.append(read_uri_bytes(path.parent, uri))
    return gltf, buffers


def read_uri_bytes(base_dir, uri):
    if uri.startswith("data:"):
        _, data = uri.split(",", 1)
        return base64.b64decode(data)
    return (Path(base_dir) / unquote(uri)).read_bytes()


def view_data(gltf, buffers, buffer_view):
    data = buffers[buffer_view.get("buffer", 0)] if isinstance(buffers, list) else buffers
    return data


COMPONENT = {
    5120: ("b", 1),
    5121: ("B", 1),
    5122: ("h", 2),
    5123: ("H", 2),
    5125: ("I", 4),
    5126: ("f", 4),
}
WIDTH = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}


def accessor(gltf, bin_chunk, index):
    acc = gltf["accessors"][index]
    bv = gltf["bufferViews"][acc["bufferView"]]
    fmt, comp_size = COMPONENT[acc["componentType"]]
    width = WIDTH[acc["type"]]
    stride = bv.get("byteStride", width * comp_size)
    start = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
    data = view_data(gltf, bin_chunk, bv)
    out = []
    for i in range(acc["count"]):
        base = start + i * stride
        vals = [struct.unpack_from("<" + fmt, data, base + j * comp_size)[0] for j in range(width)]
        if acc.get("normalized"):
            if acc["componentType"] == 5121:
                vals = [v / 255 for v in vals]
            elif acc["componentType"] == 5123:
                vals = [v / 65535 for v in vals]
            elif acc["componentType"] == 5120:
                vals = [max(v / 127, -1) for v in vals]
            elif acc["componentType"] == 5122:
                vals = [max(v / 32767, -1) for v in vals]
        out.append(vals[0] if width == 1 else tuple(vals))
    return out


def load_texture(gltf, bin_chunk, texture_index):
    ensure_pillow()
    tex = gltf["textures"][texture_index]
    img = gltf["images"][tex["source"]]
    if "bufferView" in img:
        bv = gltf["bufferViews"][img["bufferView"]]
        start = bv.get("byteOffset", 0)
        data = view_data(gltf, bin_chunk, bv)
        raw = data[start:start + bv["byteLength"]]
    elif "uri" in img:
        raw = read_uri_bytes(gltf.get("_base_dir", "."), img["uri"])
    else:
        raise ValueError("texture image has no bufferView or uri")
    return Image.open(io.BytesIO(raw)).convert("RGBA")


def quantize_transparency(alpha):
    transparency = 1.0 - max(0.0, min(1.0, alpha))
    levels = [0.0, 0.25, 0.5, 0.75, 1.0]
    return min(levels, key=lambda level: abs(level - transparency))


def sample_texture(img, uv, fallback, alpha_factor=1.0):
    if img is None or uv is None:
        rgb = fallback[:3] if len(fallback) >= 3 else fallback
        transparency = fallback[3] if len(fallback) >= 4 else quantize_transparency(alpha_factor)
        return (rgb[0], rgb[1], rgb[2], transparency)
    u = uv[0] % 1.0
    v = uv[1] % 1.0
    x = max(0, min(img.width - 1, int(u * img.width)))
    y = max(0, min(img.height - 1, int(v * img.height)))
    pixel = img.getpixel((x, y))
    alpha = (pixel[3] / 255) * alpha_factor if len(pixel) >= 4 else alpha_factor
    return (pixel[0], pixel[1], pixel[2], quantize_transparency(alpha))


def parse_mesh(gltf, bin_chunk):
    textures = {}
    for i in range(len(gltf.get("textures", []))):
        textures[i] = load_texture(gltf, bin_chunk, i)
    materials = []
    for mat in gltf.get("materials", []):
        pbr = mat.get("pbrMetallicRoughness", {})
        factor = pbr.get("baseColorFactor", [1, 1, 1, 1])
        tex_index = pbr.get("baseColorTexture", {}).get("index")
        materials.append({
            "fallback": tuple(v * 255 for v in factor[:3]),
            "alpha": factor[3] if len(factor) > 3 else 1,
            "texture": textures.get(tex_index),
        })

    node_world = {}

    def compute_world(node_index, parent):
        node = gltf["nodes"][node_index]
        matrix = mat_mul(parent, node.get("matrix", trs_matrix(node.get("translation"), node.get("rotation"), node.get("scale"))))
        node_world[node_index] = matrix
        for child in node.get("children", []):
            compute_world(child, matrix)

    scene = gltf.get("scenes", [{}])[gltf.get("scene", 0)]
    for node in scene.get("nodes", range(len(gltf.get("nodes", [])))):
        compute_world(node, mat_identity())

    inverse_bind_mats = {}
    for skin_index, skin in enumerate(gltf.get("skins", [])):
        if "inverseBindMatrices" in skin:
            inverse_bind_mats[skin_index] = accessor(gltf, bin_chunk, skin["inverseBindMatrices"])
        else:
            inverse_bind_mats[skin_index] = [mat_identity() for _ in skin.get("joints", [])]

    verts = []
    uvs = []
    faces = []

    def skin_position(local_pos, joints, weights, skin_index, node_matrix):
        if skin_index is None or not joints or not weights:
            return transform(node_matrix, local_pos)
        skin = gltf["skins"][skin_index]
        ibms = inverse_bind_mats.get(skin_index, [])
        blended = [0.0] * 16
        total = sum(weights) or 1
        for joint_id, weight in zip(joints, weights):
            if weight <= 0:
                continue
            joint_index = skin["joints"][int(joint_id)]
            joint_world = node_world.get(joint_index, mat_identity())
            ibm = ibms[int(joint_id)] if int(joint_id) < len(ibms) else mat_identity()
            mat_add_scaled(blended, mat_mul(joint_world, ibm), weight / total)
        return mat_vec_mul(blended, local_pos)

    def add_primitive(prim, matrix, skin_index):
        if prim.get("mode", 4) != 4 or "POSITION" not in prim.get("attributes", {}):
            return
        positions = accessor(gltf, bin_chunk, prim["attributes"]["POSITION"])
        texcoords = accessor(gltf, bin_chunk, prim["attributes"]["TEXCOORD_0"]) if "TEXCOORD_0" in prim["attributes"] else []
        joints = accessor(gltf, bin_chunk, prim["attributes"]["JOINTS_0"]) if "JOINTS_0" in prim["attributes"] else []
        weights = accessor(gltf, bin_chunk, prim["attributes"]["WEIGHTS_0"]) if "WEIGHTS_0" in prim["attributes"] else []
        indices = accessor(gltf, bin_chunk, prim["indices"]) if "indices" in prim else list(range(len(positions)))
        v_off = len(verts)
        uv_off = len(uvs)
        for idx, p in enumerate(positions):
            verts.append(skin_position(p, joints[idx] if joints else None, weights[idx] if weights else None, skin_index, matrix))
            uvs.append(texcoords[idx] if idx < len(texcoords) else None)
        mat_index = prim["material"] if "material" in prim and prim["material"] < len(materials) else None
        mat = materials[mat_index] if mat_index is not None else {"fallback": (255, 255, 255), "texture": None}
        for i in range(0, len(indices) - 2, 3):
            ids = (indices[i], indices[i + 1], indices[i + 2])
            uv = [uvs[uv_off + idx] for idx in ids]
            avg_uv = None
            if all(item is not None for item in uv):
                avg_uv = (sum(item[0] for item in uv) / 3, sum(item[1] for item in uv) / 3)
            color = sample_texture(mat["texture"], avg_uv, mat["fallback"], mat.get("alpha", 1))
            faces.append((v_off + ids[0], v_off + ids[1], v_off + ids[2], color, mat_index, uv))

    def visit(node_index, parent):
        node = gltf["nodes"][node_index]
        matrix = node_world[node_index]
        if "mesh" in node:
            for prim in gltf["meshes"][node["mesh"]].get("primitives", []):
                add_primitive(prim, matrix, node.get("skin"))
        for child in node.get("children", []):
            visit(child, matrix)

    for node in scene.get("nodes", range(len(gltf.get("nodes", [])))):
        visit(node, mat_identity())
    return verts, faces, materials


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tga"}
ROBLOX_XML_EXTS = {".rbxlx", ".rbxmx"}
MODEL_EXTS = {".glb", ".gltf", ".obj", ".stl", ".ply", ".zip", *ROBLOX_XML_EXTS}


def find_file_by_name(root, name):
    wanted = Path(name).name.lower()
    for item in Path(root).rglob("*"):
        if item.is_file() and item.name.lower() == wanted:
            return item
    return None


def find_texture_candidate(root):
    for item in Path(root).rglob("*"):
        if item.is_file() and item.suffix.lower() in IMAGE_EXTS:
            return item
    return None


def open_texture_file(path, flip_v=False):
    ensure_pillow()
    if not path or not Path(path).exists():
        return None
    img = Image.open(path).convert("RGBA")
    if flip_v:
        img = img.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    return img


def resolve_asset_path(base, raw_name):
    raw_name = raw_name.strip().strip('"')
    direct = (Path(base) / raw_name).resolve()
    if direct.exists():
        return direct
    found = find_file_by_name(Path(base).parent, raw_name)
    if found:
        return found
    found = find_file_by_name(base, raw_name)
    return found


def map_texture_token(tokens):
    # OBJ map_Kd lines can contain options; the actual filename is normally last.
    for token in reversed(tokens):
        if not token.startswith("-"):
            return token
    return tokens[-1] if tokens else ""


def load_mtl_surface(path):
    materials = {}
    current = None
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        key = parts[0].lower()
        if key == "newmtl" and len(parts) > 1:
            current = " ".join(parts[1:])
            materials[current] = {"fallback": (255, 255, 255, 0.0), "alpha": 1.0, "texture": None}
        elif current and key == "kd" and len(parts) >= 4:
            rgb = tuple(max(0, min(255, round(float(v) * 255))) for v in parts[1:4])
            alpha = materials[current].get("alpha", 1.0)
            materials[current]["fallback"] = (*rgb, quantize_transparency(alpha))
        elif current and key == "d" and len(parts) >= 2:
            alpha = max(0.0, min(1.0, float(parts[1])))
            materials[current]["alpha"] = alpha
            rgb = materials[current]["fallback"][:3]
            materials[current]["fallback"] = (*rgb, quantize_transparency(alpha))
        elif current and key == "tr" and len(parts) >= 2:
            alpha = 1.0 - max(0.0, min(1.0, float(parts[1])))
            materials[current]["alpha"] = alpha
            rgb = materials[current]["fallback"][:3]
            materials[current]["fallback"] = (*rgb, quantize_transparency(alpha))
        elif current and key in ("map_kd", "mapka") and len(parts) >= 2:
            tex_name = map_texture_token(parts[1:])
            tex_path = resolve_asset_path(path.parent, tex_name)
            if tex_path:
                materials[current]["texture"] = open_texture_file(tex_path, flip_v=True)
    return materials


def parse_obj_surface(path):
    verts = []
    uvs = []
    face_specs = []
    material_defs = {}
    current_material = ""

    def fix_index(value, size):
        idx = int(value)
        return idx - 1 if idx > 0 else size + idx

    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        key = parts[0].lower()
        if key == "v" and len(parts) >= 4:
            verts.append((float(parts[1]), float(parts[2]), float(parts[3])))
        elif key == "vt" and len(parts) >= 3:
            uvs.append((float(parts[1]), float(parts[2])))
        elif key == "mtllib" and len(parts) >= 2:
            mtl_name = " ".join(parts[1:])
            mtl_path = resolve_asset_path(path.parent, mtl_name)
            if mtl_path:
                material_defs.update(load_mtl_surface(mtl_path))
        elif key == "usemtl" and len(parts) >= 2:
            current_material = " ".join(parts[1:])
        elif key == "f" and len(parts) >= 4:
            refs = []
            for token in parts[1:]:
                bits = token.split("/")
                vi = fix_index(bits[0], len(verts))
                ti = fix_index(bits[1], len(uvs)) if len(bits) > 1 and bits[1] else None
                refs.append((vi, ti))
            for i in range(1, len(refs) - 1):
                tri = (refs[0], refs[i], refs[i + 1])
                face_specs.append((tri, current_material))

    fallback_texture = find_texture_candidate(path.parent)
    if "__default__" not in material_defs:
        material_defs["__default__"] = {
            "fallback": (255, 255, 255, 0.0),
            "alpha": 1.0,
            "texture": open_texture_file(fallback_texture, flip_v=True) if fallback_texture else None,
        }

    material_names = list(material_defs.keys())
    material_indices = {name: i for i, name in enumerate(material_names)}
    materials = [material_defs[name] for name in material_names]
    faces = []
    for tri, material_name in face_specs:
        mat_index = material_indices.get(material_name, material_indices["__default__"])
        mat = materials[mat_index]
        vert_ids = (tri[0][0], tri[1][0], tri[2][0])
        tri_uvs = [uvs[item[1]] if item[1] is not None and 0 <= item[1] < len(uvs) else None for item in tri]
        avg_uv = None
        if all(item is not None for item in tri_uvs):
            avg_uv = (sum(item[0] for item in tri_uvs) / 3, sum(item[1] for item in tri_uvs) / 3)
        color = sample_texture(mat["texture"], avg_uv, mat["fallback"], mat.get("alpha", 1.0))
        faces.append((*vert_ids, color, mat_index, tri_uvs))
    return verts, faces, materials


def parse_ascii_stl_surface(text):
    verts = []
    faces = []
    current = []
    for raw in text.splitlines():
        parts = raw.strip().split()
        if len(parts) == 4 and parts[0].lower() == "vertex":
            verts.append((float(parts[1]), float(parts[2]), float(parts[3])))
            current.append(len(verts) - 1)
            if len(current) == 3:
                faces.append((current[0], current[1], current[2], (255, 255, 255, 0.0), None, [None, None, None]))
                current = []
    return verts, faces


def parse_stl_surface(path):
    data = path.read_bytes()
    verts, faces = parse_ascii_stl_surface(data.decode("utf-8", errors="ignore"))
    if faces:
        return verts, faces, [{"fallback": (255, 255, 255, 0.0), "alpha": 1.0, "texture": None}]
    if len(data) < 84:
        raise ValueError("STL file is too small")
    count = struct.unpack_from("<I", data, 80)[0]
    verts = []
    faces = []
    off = 84
    for _ in range(count):
        if off + 50 > len(data):
            break
        off += 12
        ids = []
        for _j in range(3):
            verts.append(struct.unpack_from("<fff", data, off))
            ids.append(len(verts) - 1)
            off += 12
        faces.append((ids[0], ids[1], ids[2], (255, 255, 255, 0.0), None, [None, None, None]))
        off += 2
    return verts, faces, [{"fallback": (255, 255, 255, 0.0), "alpha": 1.0, "texture": None}]


def parse_ply_surface(path):
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    if not lines or lines[0].strip() != "ply":
        raise ValueError("not a PLY file")
    vertex_count = 0
    face_count = 0
    i = 1
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("element vertex"):
            vertex_count = int(line.split()[-1])
        elif line.startswith("element face"):
            face_count = int(line.split()[-1])
        elif line == "end_header":
            i += 1
            break
        i += 1
    verts = []
    for _ in range(vertex_count):
        parts = lines[i].split()
        verts.append((float(parts[0]), float(parts[1]), float(parts[2])))
        i += 1
    faces = []
    for _ in range(face_count):
        parts = lines[i].split()
        n = int(parts[0])
        ids = [int(v) for v in parts[1:1 + n]]
        for j in range(1, len(ids) - 1):
            faces.append((ids[0], ids[j], ids[j + 1], (255, 255, 255, 0.0), None, [None, None, None]))
        i += 1
    return verts, faces, [{"fallback": (255, 255, 255, 0.0), "alpha": 1.0, "texture": None}]


ROBLOX_BRICK_COLORS = {
    1: (242, 243, 243), 5: (215, 197, 154), 18: (204, 142, 105),
    21: (196, 40, 28), 23: (13, 105, 172),
    24: (245, 205, 48), 26: (27, 42, 53), 28: (40, 127, 71),
    37: (75, 151, 75), 38: (160, 95, 53), 45: (180, 210, 228),
    101: (218, 133, 65),
    102: (110, 153, 202), 103: (199, 193, 183), 104: (107, 50, 124),
    105: (226, 155, 64), 106: (218, 134, 122), 107: (0, 143, 156),
    119: (164, 189, 71), 125: (234, 184, 146), 194: (163, 162, 165), 199: (99, 95, 98),
    208: (229, 228, 223), 217: (124, 92, 70), 226: (253, 234, 141),
    1001: (248, 248, 248), 1002: (205, 205, 205), 1003: (17, 17, 17),
    1004: (255, 0, 0), 1005: (255, 175, 0), 1006: (180, 128, 255),
    1007: (163, 75, 75), 1008: (193, 190, 66), 1009: (255, 255, 0),
    1010: (0, 0, 255), 1011: (0, 32, 96), 1012: (33, 84, 185),
    1013: (4, 175, 236), 1014: (170, 85, 0), 1015: (170, 0, 170),
    1016: (255, 102, 204), 1017: (255, 175, 0), 1018: (18, 238, 212),
    1019: (0, 255, 255), 1020: (0, 255, 0), 1021: (58, 125, 21),
    1022: (127, 142, 100), 1023: (140, 91, 159), 1024: (175, 221, 255),
}


def xml_tag_name(element):
    return element.tag.rsplit("}", 1)[-1]


def rbx_props(item):
    for child in item:
        if xml_tag_name(child) == "Properties":
            return {
                prop.attrib.get("name", "").lower(): prop
                for prop in child
                if prop.attrib.get("name")
            }
    return {}


ROBLOX_SKIPPED_CONTAINERS = {"replicatedstorage"}


def rbx_item_name(item):
    props = rbx_props(item)
    prop = rbx_prop(props, "Name")
    return (prop.text or "").strip() if prop is not None else item.attrib.get("class", "")


def rbx_should_skip_container(item):
    class_name = item.attrib.get("class", "").strip().lower()
    name = rbx_item_name(item).strip().lower()
    return class_name in ROBLOX_SKIPPED_CONTAINERS or name in ROBLOX_SKIPPED_CONTAINERS


def iter_roblox_visible_items(root):
    def visit(parent, skip=False):
        for child in parent:
            if xml_tag_name(child) != "Item":
                yield from visit(child, skip)
                continue
            child_skip = skip or rbx_should_skip_container(child)
            if not child_skip:
                yield child
                yield from visit(child, False)
    yield from visit(root)


def rbx_prop(props, name):
    return props.get(name.lower())


def rbx_float_text(text, default=0.0):
    try:
        return float((text or "").strip())
    except Exception:
        return default


def rbx_prop_float(props, name, default=0.0):
    prop = rbx_prop(props, name)
    if prop is None:
        return default
    return rbx_float_text(prop.text, default)


def rbx_prop_bool(props, name, default=False):
    prop = rbx_prop(props, name)
    if prop is None:
        return default
    raw = (prop.text or "").strip().lower()
    if raw in {"true", "1", "yes"}:
        return True
    if raw in {"false", "0", "no"}:
        return False
    return default


def rbx_child_float(prop, name, default=0.0):
    if prop is None:
        return default
    for child in prop:
        if xml_tag_name(child).lower() == name.lower():
            return rbx_float_text(child.text, default)
    return default


def rbx_vector3(props, name, default=(1.0, 1.0, 1.0)):
    prop = rbx_prop(props, name)
    if prop is None:
        return default
    return (
        rbx_child_float(prop, "X", default[0]),
        rbx_child_float(prop, "Y", default[1]),
        rbx_child_float(prop, "Z", default[2]),
    )


def rbx_cframe(props):
    prop = rbx_prop(props, "CFrame")
    if prop is None:
        return (0.0, 0.0, 0.0), ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    pos = (
        rbx_child_float(prop, "X", 0.0),
        rbx_child_float(prop, "Y", 0.0),
        rbx_child_float(prop, "Z", 0.0),
    )
    rot = (
        (rbx_child_float(prop, "R00", 1.0), rbx_child_float(prop, "R01", 0.0), rbx_child_float(prop, "R02", 0.0)),
        (rbx_child_float(prop, "R10", 0.0), rbx_child_float(prop, "R11", 1.0), rbx_child_float(prop, "R12", 0.0)),
        (rbx_child_float(prop, "R20", 0.0), rbx_child_float(prop, "R21", 0.0), rbx_child_float(prop, "R22", 1.0)),
    )
    return pos, rot


def rbx_transform_point(cframe, point):
    pos, rot = cframe
    x, y, z = point
    return (
        pos[0] + rot[0][0] * x + rot[0][1] * y + rot[0][2] * z,
        pos[1] + rot[1][0] * x + rot[1][1] * y + rot[1][2] * z,
        pos[2] + rot[2][0] * x + rot[2][1] * y + rot[2][2] * z,
    )


def rbx_color_uint(value):
    raw = int(rbx_float_text(value, 0))
    return ((raw >> 16) & 255, (raw >> 8) & 255, raw & 255)


def rbx_transparency(value):
    value = max(0.0, min(1.0, float(value)))
    levels = [0.0, 0.25, 0.5, 0.75, 1.0]
    return min(levels, key=lambda level: abs(level - value))


def rbx_color(props):
    prop = rbx_prop(props, "Color")
    if prop is not None:
        if list(prop):
            rgb = (
                round(rbx_child_float(prop, "R", 0.64) * 255),
                round(rbx_child_float(prop, "G", 0.64) * 255),
                round(rbx_child_float(prop, "B", 0.64) * 255),
            )
        else:
            values = [rbx_float_text(item, 0.64) for item in (prop.text or "").split()]
            rgb = tuple(round(values[i] * 255) for i in range(3)) if len(values) >= 3 else (163, 162, 165)
    elif rbx_prop(props, "Color3uint8") is not None:
        rgb = rbx_color_uint(rbx_prop(props, "Color3uint8").text)
    elif rbx_prop(props, "BrickColor") is not None:
        brick = int(rbx_float_text(rbx_prop(props, "BrickColor").text, 194))
        rgb = ROBLOX_BRICK_COLORS.get(brick, (163, 162, 165))
    else:
        rgb = (163, 162, 165)
    trans = rbx_transparency(rbx_prop_float(props, "Transparency", 0.0))
    return (rgb[0], rgb[1], rgb[2], trans)


def rbx_shape(props, class_name):
    cls = class_name.lower()
    if "cornerwedgepart" in cls:
        return "corner_wedge"
    if "wedgepart" in cls:
        return "wedge"
    prop = rbx_prop(props, "Shape")
    if prop is None:
        prop = rbx_prop(props, "shape")
    raw = (prop.text or "").strip().lower() if prop is not None else ""
    if raw in {"0", "ball", "sphere"} or "ball" in cls:
        return "sphere"
    if raw in {"2", "cylinder"} or "cylinder" in cls:
        return "cylinder"
    return "box"


def rbx_child_mesh_shape(item):
    for child in item:
        if xml_tag_name(child) != "Item":
            continue
        cls = child.attrib.get("class", "").strip().lower()
        if cls == "blockmesh":
            return "box"
        if cls == "cylindermesh":
            return "cylinder"
        if cls == "specialmesh":
            props = rbx_props(child)
            prop = rbx_prop(props, "MeshType")
            raw = (prop.text or "").strip().lower() if prop is not None else "0"
            if raw in {"0", "head", "3", "sphere"}:
                return "sphere"
            if raw in {"2", "wedge"}:
                return "wedge"
            if raw in {"4", "cylinder"}:
                return "cylinder"
            if raw in {"6", "brick", "block"}:
                return "box"
            return "mesh"
    return None


def color_looks_leafy(color):
    r, g, b = color[:3]
    return g > r * 1.08 and g > b * 0.85 and g >= 70


def color_looks_wood(color):
    r, g, b = color[:3]
    return r >= g >= b and r > 70 and b < 120


def rbx_custom_approx_shape(name, class_key, color, size, shape):
    text = (name or "").strip().lower()
    if any(word in text for word in ("cone", "spike", "pyramid")):
        return "cone"
    if any(word in text for word in ("ring", "cog", "gear")):
        return "ring"
    if any(word in text for word in ("leaf", "leaves", "bush", "foliage", "canopy")):
        return "sphere"
    if any(word in text for word in ("rock", "boulder", "stone")):
        return "sphere"
    if any(word in text for word in ("trunk", "log", "barrel", "branch", "wheel", "tire", "rim", "pipe", "tube", "axle", "round", "circle")):
        return "cylinder"
    if any(word in text for word in ("train", "rail", "track", "engine", "locomotive", "carriage", "cart", "chassis")):
        return "box"
    return shape


def rbx_custom_cylinder_axis(name, size):
    text = (name or "").strip().lower()
    axes = ("x", "y", "z")
    shortest_axis = min(range(3), key=lambda index: size[index])
    longest_axis = max(range(3), key=lambda index: size[index])
    disk_words = ("ring", "cog", "gear", "wheel", "tire", "rim", "circle", "disc", "disk")
    rod_words = ("trunk", "log", "barrel", "branch", "pipe", "tube", "axle", "pole", "rod", "post", "column", "cylinder")
    if any(word in text for word in disk_words):
        return axes[shortest_axis]
    if any(word in text for word in rod_words):
        return axes[longest_axis]
    if max(size) >= min(size) * 2.2:
        return axes[longest_axis]
    return axes[shortest_axis]


def rbx_native_cylinder_axis(size, default_axis="x"):
    axes = ("x", "y", "z")
    ordered = sorted(range(3), key=lambda index: size[index])
    shortest_axis = ordered[0]
    longest_axis = ordered[-1]
    shortest = max(size[shortest_axis], 0.000001)
    middle = max(size[ordered[1]], 0.000001)
    longest = max(size[longest_axis], 0.000001)
    if shortest <= middle * 0.78:
        return axes[shortest_axis]
    if longest >= middle * 1.35:
        return axes[longest_axis]
    return default_axis


def add_surface(vertices, faces, local_vertices, polygons, cframe, color, mat_index):
    start = len(vertices)
    vertices.extend(rbx_transform_point(cframe, point) for point in local_vertices)
    for poly in polygons:
        if len(poly) < 3:
            continue
        for i in range(1, len(poly) - 1):
            faces.append((start + poly[0], start + poly[i], start + poly[i + 1], color, mat_index, [None, None, None]))


def add_box_surface(vertices, faces, size, cframe, color, mat_index):
    sx, sy, sz = (max(0.001, value) for value in size)
    x, y, z = sx / 2, sy / 2, sz / 2
    local = [
        (-x, -y, -z), (x, -y, -z), (x, y, -z), (-x, y, -z),
        (-x, -y, z), (x, -y, z), (x, y, z), (-x, y, z),
    ]
    polys = [(0, 1, 2, 3), (4, 7, 6, 5), (0, 4, 5, 1), (1, 5, 6, 2), (2, 6, 7, 3), (3, 7, 4, 0)]
    add_surface(vertices, faces, local, polys, cframe, color, mat_index)


def add_wedge_surface(vertices, faces, size, cframe, color, mat_index):
    sx, sy, sz = (max(0.001, value) for value in size)
    x, y, z = sx / 2, sy / 2, sz / 2
    local = [(-x, -y, -z), (x, -y, -z), (x, -y, z), (-x, -y, z), (-x, y, z), (x, y, z)]
    polys = [(0, 1, 2, 3), (3, 2, 5, 4), (0, 3, 4), (1, 5, 2), (0, 4, 5, 1)]
    add_surface(vertices, faces, local, polys, cframe, color, mat_index)


def add_corner_wedge_surface(vertices, faces, size, cframe, color, mat_index):
    sx, sy, sz = (max(0.001, value) for value in size)
    x, y, z = sx / 2, sy / 2, sz / 2
    local = [(-x, -y, -z), (x, -y, -z), (x, -y, z), (-x, -y, z), (-x, y, z)]
    polys = [(0, 1, 2, 3), (0, 3, 4), (2, 4, 3), (1, 4, 2), (0, 4, 1)]
    add_surface(vertices, faces, local, polys, cframe, color, mat_index)


def add_cylinder_surface(vertices, faces, size, cframe, color, mat_index, axis="x"):
    sx, sy, sz = (max(0.001, value) for value in size)
    rx, ry, rz = sx / 2, sy / 2, sz / 2
    segments = 24
    axis = (axis or "x").lower()
    if axis == "y":
        local = [(0, -ry, 0), (0, ry, 0)]
        for y in (-ry, ry):
            for i in range(segments):
                angle = math.tau * i / segments
                local.append((math.cos(angle) * rx, y, math.sin(angle) * rz))
    else:
        local = [(-rx, 0, 0), (rx, 0, 0)]
        for x in (-rx, rx):
            for i in range(segments):
                angle = math.tau * i / segments
                local.append((x, math.cos(angle) * ry, math.sin(angle) * rz))
    left = 2
    right = 2 + segments
    polys = []
    for i in range(segments):
        j = (i + 1) % segments
        polys.append((left + i, left + j, right + j, right + i))
        polys.append((0, left + j, left + i))
        polys.append((1, right + i, right + j))
    add_surface(vertices, faces, local, polys, cframe, color, mat_index)


def add_sphere_surface(vertices, faces, size, cframe, color, mat_index):
    sx, sy, sz = (max(0.001, value) for value in size)
    rx, ry, rz = sx / 2, sy / 2, sz / 2
    segments = 24
    rings = 12
    local = []
    for ring in range(rings + 1):
        phi = -math.pi / 2 + math.pi * ring / rings
        cp = math.cos(phi)
        sp = math.sin(phi)
        for seg in range(segments):
            theta = math.tau * seg / segments
            local.append((math.cos(theta) * cp * rx, sp * ry, math.sin(theta) * cp * rz))
    polys = []
    for ring in range(rings):
        for seg in range(segments):
            a = ring * segments + seg
            b = ring * segments + (seg + 1) % segments
            c = (ring + 1) * segments + (seg + 1) % segments
            d = (ring + 1) * segments + seg
            polys.append((a, b, c, d))
    add_surface(vertices, faces, local, polys, cframe, color, mat_index)


def parse_roblox_studio_surface(path):
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise ValueError("Roblox binary .rbxl/.rbxm files are not supported yet; save/export as .rbxlx or .rbxmx XML") from exc
    vertices = []
    faces = []
    materials = []
    material_lookup = {}

    def material_index(color):
        key = tuple(color)
        if key not in material_lookup:
            material_lookup[key] = len(materials)
            materials.append({"fallback": color, "alpha": 1.0 - color[3], "texture": None})
        return material_lookup[key]

    supported = {
        "part", "meshpart", "unionoperation", "partoperation", "negateoperation",
        "wedgepart", "cornerwedgepart", "spawnlocation", "seat", "vehicleseat",
        "trusspart",
    }
    skipped_meshes = 0
    for item in iter_roblox_visible_items(root):
        class_name = item.attrib.get("class", "")
        if class_name.lower() not in supported and not class_name.lower().endswith("part"):
            continue
        props = rbx_props(item)
        if not props:
            continue
        size = rbx_vector3(props, "Size", (1.0, 1.0, 1.0))
        color = rbx_color(props)
        if color[3] >= 1.0:
            continue
        cframe = rbx_cframe(props)
        mat_index = material_index(color)
        shape = rbx_shape(props, class_name)
        child_shape = rbx_child_mesh_shape(item)
        cylinder_axis = "x"
        if child_shape:
            shape = child_shape
            if child_shape == "cylinder":
                cylinder_axis = "y"
        if class_name.lower() == "meshpart" or class_name.lower() in {"unionoperation", "partoperation", "negateoperation"}:
            skipped_meshes += 1
            shape = "box"
        if shape == "sphere":
            add_sphere_surface(vertices, faces, size, cframe, color, mat_index)
        elif shape == "cylinder":
            add_cylinder_surface(vertices, faces, size, cframe, color, mat_index, axis=cylinder_axis)
        elif shape == "wedge":
            add_wedge_surface(vertices, faces, size, cframe, color, mat_index)
        elif shape == "corner_wedge":
            add_corner_wedge_surface(vertices, faces, size, cframe, color, mat_index)
        else:
            add_box_surface(vertices, faces, size, cframe, color, mat_index)
    if not faces:
        raise ValueError("Roblox Studio XML did not contain any visible supported parts")
    if skipped_meshes:
        print(f"Roblox Studio import: {skipped_meshes} MeshPart/Union items used bounding boxes because embedded Roblox mesh assets are not stored in XML.")
    return vertices, faces, materials


def parse_roblox_studio_blocks(path):
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise ValueError("Roblox binary .rbxl/.rbxm files are not supported yet; save/export as .rbxlx or .rbxmx XML") from exc

    supported = {
        "part", "meshpart", "unionoperation", "partoperation", "negateoperation",
        "wedgepart", "cornerwedgepart", "spawnlocation", "seat", "vehicleseat",
        "trusspart",
    }
    blocks = []
    bounded_shapes = 0
    for item in iter_roblox_visible_items(root):
        class_name = item.attrib.get("class", "")
        class_key = class_name.lower()
        if class_key not in supported and not class_key.endswith("part"):
            continue
        props = rbx_props(item)
        if not props:
            continue
        size = tuple(max(0.001, value) for value in rbx_vector3(props, "Size", (1.0, 1.0, 1.0)))
        color = rbx_color(props)
        if color[3] >= 1.0:
            continue
        center, rot = rbx_cframe(props)
        name = rbx_item_name(item)
        can_collide = rbx_prop_bool(props, "CanCollide", True)
        axes = (
            vec_norm((rot[0][0], rot[1][0], rot[2][0])),
            vec_norm((rot[0][1], rot[1][1], rot[2][1])),
            vec_norm((rot[0][2], rot[1][2], rot[2][2])),
        )
        shape = rbx_shape(props, class_name)
        child_shape = rbx_child_mesh_shape(item)
        cylinder_axis = "x"
        mesh_cylinder = False
        if child_shape and child_shape != "mesh":
            shape = child_shape
            if child_shape == "cylinder":
                cylinder_axis = "y"
                mesh_cylinder = True
        elif shape == "cylinder":
            cylinder_axis = rbx_native_cylinder_axis(size, cylinder_axis)
        if class_key in {"meshpart", "unionoperation", "partoperation", "negateoperation"} or child_shape == "mesh":
            shape = rbx_custom_approx_shape(name, class_key, color, size, shape)
            if shape in {"cylinder", "ring"}:
                cylinder_axis = rbx_custom_cylinder_axis(name, size)
                mesh_cylinder = shape == "cylinder"
        if shape != "box" or class_key in {"meshpart", "unionoperation", "partoperation", "negateoperation"} or child_shape:
            bounded_shapes += 1
        blocks.append({
            "center": center,
            "axes": axes,
            "size": size,
            "color": color,
            "shape": shape,
            "cylinder_axis": cylinder_axis,
            "mesh_cylinder": mesh_cylinder,
            "class": class_name,
            "name": name,
            "can_collide": can_collide,
        })
    if not blocks:
        raise ValueError("Roblox Studio XML did not contain any visible supported parts")
    if bounded_shapes:
        print(f"Roblox optimized import: {bounded_shapes} non-box parts used primitive approximations, wedge surfaces, or mesh bounding blocks.", flush=True)
    return blocks


def direct_block_corners(block):
    center = block["center"]
    axes = block["axes"]
    size = block["size"]
    corners = []
    for sx in (-0.5, 0.5):
        for sy in (-0.5, 0.5):
            for sz in (-0.5, 0.5):
                p = center
                p = vec_add(p, vec_mul(axes[0], size[0] * sx))
                p = vec_add(p, vec_mul(axes[1], size[1] * sy))
                p = vec_add(p, vec_mul(axes[2], size[2] * sz))
                corners.append(p)
    return corners


def fit_direct_blocks_to_build_area(source_blocks):
    rotated = []
    corners = []
    for block in source_blocks:
        rb = {
            "center": rotate_point(block["center"]),
            "axes": tuple(vec_norm(rotate_vector(axis)) for axis in block["axes"]),
            "size": block["size"],
            "color": block["color"],
            "shape": block.get("shape", "box"),
            "cylinder_axis": block.get("cylinder_axis", "x"),
            "mesh_cylinder": bool(block.get("mesh_cylinder", False)),
            "class": block.get("class", ""),
            "name": block.get("name", ""),
            "can_collide": bool(block.get("can_collide", True)),
        }
        rotated.append(rb)
        corners.extend(direct_block_corners(rb))
    mn = [min(p[i] for p in corners) for i in range(3)]
    mx = [max(p[i] for p in corners) for i in range(3)]
    size = [mx[i] - mn[i] for i in range(3)]
    marker_min = [min(p[i] for p in MARKER_POINTS) for i in range(3)]
    marker_max = [max(p[i] for p in MARKER_POINTS) for i in range(3)]
    marker_center = [(marker_min[i] + marker_max[i]) * 0.5 for i in range(3)]
    scale = min(
        (marker_max[0] - marker_min[0] - CELL[0]) / max(size[0], 0.000001),
        (marker_max[2] - marker_min[2] - CELL[2]) / max(size[2], 0.000001),
    ) / FIT_SHRINK
    center = ((mn[0] + mx[0]) / 2, mn[1], (mn[2] + mx[2]) / 2)
    origin = (marker_center[0], marker_min[1], marker_center[2])
    output = []

    def roblox_layer_count(scaled_length, cap_per_detail=4, hard_cap=36):
        detail = max(0.25, float(ROBLOX_TERRAIN_DETAIL))
        cap = max(1, min(hard_cap, int(math.ceil(detail * cap_per_detail))))
        minimum = max(1, min(cap, int(math.ceil(detail / 2.0))))
        target = max(0.02, min(CELL) * max(0.75, 3.0 / detail))
        return max(minimum, min(cap, int(math.ceil(abs(scaled_length) / target))))

    def append_local_box(block, local_center, local_size, axes=None):
        u_axis, v_axis, n_axis = block["axes"]
        out_u, out_v, out_n = axes or (u_axis, v_axis, n_axis)
        lx, ly, lz = local_center
        sx, sy, sz = local_size
        world = block["center"]
        world = vec_add(world, vec_mul(u_axis, lx))
        world = vec_add(world, vec_mul(v_axis, ly))
        world = vec_add(world, vec_mul(n_axis, lz))
        center_pt = fit_point(world)
        output.append({
            "color": block["color"],
            "can_collide": bool(block.get("can_collide", True)),
            "cframe": [
                clean(center_pt[0]), clean(center_pt[1]), clean(center_pt[2]),
                clean(out_u[0]), clean(out_v[0]), clean(out_n[0]),
                clean(out_u[1]), clean(out_v[1]), clean(out_n[1]),
                clean(out_u[2]), clean(out_v[2]), clean(out_n[2]),
            ],
            "size": [
                clean(max(0.01, sx * scale)),
                clean(max(0.01, sy * scale)),
                clean(max(0.01, sz * scale)),
            ],
        })

    def append_plain_block(block):
        u_axis, v_axis, n_axis = block["axes"]
        center_pt = fit_point(block["center"])
        output.append({
            "color": block["color"],
            "can_collide": bool(block.get("can_collide", True)),
            "cframe": [
                clean(center_pt[0]), clean(center_pt[1]), clean(center_pt[2]),
                clean(u_axis[0]), clean(v_axis[0]), clean(n_axis[0]),
                clean(u_axis[1]), clean(v_axis[1]), clean(n_axis[1]),
                clean(u_axis[2]), clean(v_axis[2]), clean(n_axis[2]),
            ],
            "size": [
                clean(max(0.01, block["size"][0] * scale)),
                clean(max(0.01, block["size"][1] * scale)),
                clean(max(0.01, block["size"][2] * scale)),
            ],
        })

    def is_tiny_roblox_part(block):
        return max(block["size"]) * scale < min(CELL) * 1.25

    def smooth_slab_thickness(scaled_span):
        base = min(CELL)
        if scaled_span < base * 1.25:
            return max(0.01, scaled_span * 0.55)
        return max(0.01, min(base * 4.0, max(base * max(1.0, ROBLOX_WEDGE_FILL * 0.75), abs(scaled_span) * 0.08)))

    def append_panel(points, color, thickness_span=None):
        if len(points) < 3:
            return
        p0, p1 = points[0], points[1]
        u_axis = vec_norm(vec_sub(p1, p0))
        if vec_len(u_axis) < 0.001:
            return
        normal_source = None
        for point in points[2:]:
            candidate = vec_cross(vec_sub(p1, p0), vec_sub(point, p0))
            if vec_len(candidate) >= 0.001:
                normal_source = candidate
                break
        if normal_source is None:
            return
        normal = vec_norm(normal_source)
        v_axis = vec_norm(vec_cross(normal, u_axis))
        coords = []
        for point in points:
            rel = vec_sub(point, p0)
            coords.append((vec_dot(rel, u_axis), vec_dot(rel, v_axis)))
        min_u = min(coord[0] for coord in coords)
        max_u = max(coord[0] for coord in coords)
        min_v = min(coord[1] for coord in coords)
        max_v = max(coord[1] for coord in coords)
        width = max(0.01, max_u - min_u)
        height = max(0.01, max_v - min_v)
        center_pt = vec_add(p0, vec_add(vec_mul(u_axis, (min_u + max_u) * 0.5), vec_mul(v_axis, (min_v + max_v) * 0.5)))
        thickness = smooth_slab_thickness(thickness_span or max(width, height))
        overlap = min(max(1.0, SURFACE_OVERLAP), 1.08)
        output.append({
            "color": color,
            "cframe": [
                clean(center_pt[0]), clean(center_pt[1]), clean(center_pt[2]),
                clean(u_axis[0]), clean(v_axis[0]), clean(normal[0]),
                clean(u_axis[1]), clean(v_axis[1]), clean(normal[1]),
                clean(u_axis[2]), clean(v_axis[2]), clean(normal[2]),
            ],
            "size": [
                clean(width * overlap),
                clean(height * overlap),
                clean(thickness),
            ],
        })

    def fit_point(p):
        return (
            (p[0] - center[0]) * scale + origin[0],
            (p[1] - center[1]) * scale + origin[1],
            (p[2] - center[2]) * scale + origin[2],
        )

    def append_wedge_slab(block):
        u_axis, v_axis, n_axis = block["axes"]
        sx, sy, sz = block["size"]
        x, y, z = sx / 2, sy / 2, sz / 2
        local = [
            (-x, -y, -z), (x, -y, -z), (x, -y, z),
            (-x, -y, z), (-x, y, z), (x, y, z),
        ]

        def local_to_fit(point):
            lx, ly, lz = point
            world = block["center"]
            world = vec_add(world, vec_mul(u_axis, lx))
            world = vec_add(world, vec_mul(v_axis, ly))
            world = vec_add(world, vec_mul(n_axis, lz))
            return fit_point(world)

        verts = [local_to_fit(point) for point in local]
        wedge_center = fit_point(block["center"])
        thickness = 0.01
        hover = 0.01
        overlap = 1.0

        def orient_outward(normal, face_points):
            face_center = (
                sum(point[0] for point in face_points) / len(face_points),
                sum(point[1] for point in face_points) / len(face_points),
                sum(point[2] for point in face_points) / len(face_points),
            )
            if vec_dot(normal, vec_sub(face_center, wedge_center)) < 0:
                normal = vec_mul(normal, -1)
            return normal

        def append_face_quad(indices):
            pts = [verts[index] for index in indices]
            p0, p1 = pts[0], pts[1]
            u = vec_norm(vec_sub(p1, p0))
            if vec_len(u) < 0.001:
                return
            normal = None
            for point in pts[2:]:
                candidate = vec_cross(vec_sub(p1, p0), vec_sub(point, p0))
                if vec_len(candidate) >= 0.001:
                    normal = vec_norm(candidate)
                    break
            if normal is None:
                return
            normal = orient_outward(normal, pts)
            v = vec_norm(vec_cross(normal, u))
            coords = []
            for point in pts:
                rel = vec_sub(point, p0)
                coords.append((vec_dot(rel, u), vec_dot(rel, v)))
            min_u = min(coord[0] for coord in coords)
            max_u = max(coord[0] for coord in coords)
            min_v = min(coord[1] for coord in coords)
            max_v = max(coord[1] for coord in coords)
            width = max(0.01, (max_u - min_u) * overlap)
            height = max(0.01, (max_v - min_v) * overlap)
            center_pt = vec_add(p0, vec_add(vec_mul(u, (min_u + max_u) * 0.5), vec_mul(v, (min_v + max_v) * 0.5)))
            center_pt = vec_add(center_pt, vec_mul(normal, thickness * 0.5 - hover))
            output.append({
                "color": block["color"],
                "can_collide": bool(block.get("can_collide", True)),
                "cframe": [
                    clean(center_pt[0]), clean(center_pt[1]), clean(center_pt[2]),
                    clean(u[0]), clean(v[0]), clean(normal[0]),
                    clean(u[1]), clean(v[1]), clean(normal[1]),
                    clean(u[2]), clean(v[2]), clean(normal[2]),
                ],
                "size": [
                    clean(width),
                    clean(height),
                    clean(thickness),
                ],
            })

        def append_face_triangle(indices):
            pts = [verts[index] for index in indices]
            edges = [
                (0, 1, vec_len(vec_sub(pts[1], pts[0]))),
                (1, 2, vec_len(vec_sub(pts[2], pts[1]))),
                (2, 0, vec_len(vec_sub(pts[0], pts[2]))),
            ]
            edges.sort(key=lambda item: item[2], reverse=True)
            i0, i1, base_len = edges[0]
            i2 = 3 - i0 - i1
            if base_len < 0.01:
                return
            base0, base1, apex = pts[i0], pts[i1], pts[i2]
            u = vec_norm(vec_sub(base1, base0))
            normal = vec_norm(vec_cross(vec_sub(base1, base0), vec_sub(apex, base0)))
            if vec_len(normal) < 0.001:
                return
            normal = orient_outward(normal, pts)
            v = vec_norm(vec_cross(normal, u))
            apex_rel = vec_sub(apex, base0)
            apex_u = vec_dot(apex_rel, u)
            apex_v = vec_dot(apex_rel, v)
            if abs(apex_v) < 0.01:
                return
            if apex_v < 0:
                base0, base1 = base1, base0
                u = vec_mul(u, -1)
                v = vec_norm(vec_cross(normal, u))
                apex_rel = vec_sub(apex, base0)
                apex_u = vec_dot(apex_rel, u)
                apex_v = vec_dot(apex_rel, v)
                if apex_v < 0:
                    return
            target_width = max(0.012, min(CELL) * 0.55)
            rows = max(1, min(96, int(math.ceil(apex_v / target_width))))
            strip_height = max(0.01, (apex_v / rows) * overlap)
            for row in range(rows):
                t = (row + 0.5) / rows
                length = base_len * (1.0 - t)
                if length < 0.01:
                    continue
                center_u = base_len * 0.5 * (1.0 - t) + apex_u * t
                center_v = apex_v * t
                center_pt = vec_add(base0, vec_add(vec_mul(u, center_u), vec_mul(v, center_v)))
                center_pt = vec_add(center_pt, vec_mul(normal, thickness * 0.5 - hover))
                output.append({
                    "color": block["color"],
                    "can_collide": bool(block.get("can_collide", True)),
                    "cframe": [
                        clean(center_pt[0]), clean(center_pt[1]), clean(center_pt[2]),
                        clean(u[0]), clean(v[0]), clean(normal[0]),
                        clean(u[1]), clean(v[1]), clean(normal[1]),
                        clean(u[2]), clean(v[2]), clean(normal[2]),
                    ],
                    "size": [
                        clean(max(0.01, length * overlap)),
                        clean(strip_height),
                        clean(thickness),
                    ],
                })

        append_face_quad((0, 1, 5, 4))
        append_face_quad((0, 3, 2, 1))
        append_face_quad((3, 4, 5, 2))
        append_face_triangle((0, 3, 4))
        append_face_triangle((1, 5, 2))

    def append_wedge_fill_layers(block):
        sx, sy, sz = (max(0.001, value) for value in block["size"])
        if ROBLOX_TERRAIN_DETAIL < 4.0:
            return
        rows = roblox_layer_count(sz * scale, cap_per_detail=8, hard_cap=64)
        overlap = min(max(1.0, SURFACE_OVERLAP), 1.15)
        min_local = 0.01 / max(scale, 0.000001)
        min_width = (min(CELL) * max(0.0, ROBLOX_WEDGE_FILL)) / max(scale, 0.000001)
        width = max(sx, min_local)
        if sx * scale < min(CELL) * 0.6 and sz * scale >= min(CELL) * 2:
            width = max(width, min_width)
        depth = max(sz / rows * overlap, min_local)
        for row in range(rows):
            t0 = row / rows
            t1 = (row + 1) / rows
            tm = (t0 + t1) * 0.5
            height = max(sy * t1, min_local)
            local_y = -sy * 0.5 + height * 0.5
            local_z = -sz * 0.5 + tm * sz
            append_local_box(block, (0.0, local_y, local_z), (width, height * overlap, depth))

    def append_wedge_steps(block):
        append_wedge_fill_layers(block)

    def append_corner_wedge_surface(block):
        u_axis, v_axis, n_axis = block["axes"]
        sx, sy, sz = (max(0.001, value) for value in block["size"])
        x, y, z = sx / 2, sy / 2, sz / 2
        # CornerWedgePart's sloped corner was mirrored in optimized mode.
        # Using the opposite top corner is the 180-degree local flip the game expects.
        local = [
            (-x, -y, -z), (x, -y, -z), (x, -y, z),
            (-x, -y, z), (x, y, -z),
        ]
        polys = [(0, 3, 2, 1), (0, 4, 3), (3, 4, 2), (2, 4, 1), (1, 4, 0)]
        verts = []
        for lx, ly, lz in local:
            world = block["center"]
            world = vec_add(world, vec_mul(u_axis, lx))
            world = vec_add(world, vec_mul(v_axis, ly))
            world = vec_add(world, vec_mul(n_axis, lz))
            verts.append(fit_point(world))

        def append_triangle_cover(a, b, c):
            pts = [verts[a], verts[b], verts[c]]
            edges = [
                (0, 1, vec_len(vec_sub(pts[1], pts[0]))),
                (1, 2, vec_len(vec_sub(pts[2], pts[1]))),
                (2, 0, vec_len(vec_sub(pts[0], pts[2]))),
            ]
            edges.sort(key=lambda item: item[2], reverse=True)
            i0, i1 = edges[0][0], edges[0][1]
            i2 = 3 - i0 - i1
            p0, p1, p2 = pts[i0], pts[i1], pts[i2]
            strip_u = vec_norm(vec_sub(p1, p0))
            normal = vec_norm(vec_cross(vec_sub(p1, p0), vec_sub(p2, p0)))
            if vec_len(normal) < 0.001:
                return
            strip_v = vec_norm(vec_cross(normal, strip_u))
            local_2d = []
            for point in (p0, p1, p2):
                rel = vec_sub(point, p0)
                local_2d.append((vec_dot(rel, strip_u), vec_dot(rel, strip_v)))
            min_u = min(point[0] for point in local_2d)
            max_u = max(point[0] for point in local_2d)
            min_v = min(point[1] for point in local_2d)
            max_v = max(point[1] for point in local_2d)
            width = max(0.01, max_u - min_u)
            height = max(0.01, max_v - min_v)
            center = vec_add(p0, vec_add(vec_mul(strip_u, (min_u + max_u) * 0.5), vec_mul(strip_v, (min_v + max_v) * 0.5)))
            thickness = smooth_slab_thickness(max(width, height))
            output.append({
                "color": block["color"],
                "can_collide": bool(block.get("can_collide", True)),
                "cframe": [
                    clean(center[0]), clean(center[1]), clean(center[2]),
                    clean(strip_u[0]), clean(strip_v[0]), clean(normal[0]),
                    clean(strip_u[1]), clean(strip_v[1]), clean(normal[1]),
                    clean(strip_u[2]), clean(strip_v[2]), clean(normal[2]),
                ],
                "size": [
                    clean(width * min(max(1.0, SURFACE_OVERLAP), 1.08)),
                    clean(height * min(max(1.0, SURFACE_OVERLAP), 1.08)),
                    clean(thickness),
                ],
            })

        if ROBLOX_TERRAIN_DETAIL < 4.0:
            candidates = []
            for poly in polys:
                if len(poly) == 4:
                    tris = [(poly[0], poly[1], poly[2]), (poly[0], poly[2], poly[3])]
                else:
                    tris = [(poly[0], poly[1], poly[2])]
                for tri in tris:
                    pts = [verts[i] for i in tri]
                    area_vec = vec_cross(vec_sub(pts[1], pts[0]), vec_sub(pts[2], pts[0]))
                    area = vec_len(area_vec) * 0.5
                    normal = vec_norm(area_vec)
                    horizontal = abs(vec_dot(normal, (0.0, 1.0, 0.0))) > 0.92
                    if area > 0.00001 and not horizontal:
                        candidates.append((area, tri))
            candidates.sort(reverse=True, key=lambda item: item[0])
            if candidates:
                max_area = candidates[0][0]
                used = 0
                for area, tri in candidates:
                    if used >= 2 or area < max_area * 0.80:
                        break
                    append_triangle_cover(*tri)
                    used += 1
                return

        def append_limited_triangle(a, b, c):
            pts = [verts[a], verts[b], verts[c]]
            edges = [
                (0, 1, vec_len(vec_sub(pts[1], pts[0]))),
                (1, 2, vec_len(vec_sub(pts[2], pts[1]))),
                (2, 0, vec_len(vec_sub(pts[0], pts[2]))),
            ]
            edges.sort(key=lambda item: item[2], reverse=True)
            i0, i1 = edges[0][0], edges[0][1]
            i2 = 3 - i0 - i1
            p0, p1, p2 = pts[i0], pts[i1], pts[i2]
            strip_u = vec_norm(vec_sub(p1, p0))
            normal = vec_norm(vec_cross(vec_sub(p1, p0), vec_sub(p2, p0)))
            if vec_len(normal) < 0.001:
                return
            strip_v = vec_norm(vec_cross(normal, strip_u))
            local_2d = []
            for point in (p0, p1, p2):
                rel = vec_sub(point, p0)
                local_2d.append((vec_dot(rel, strip_u), vec_dot(rel, strip_v)))
            min_v = min(point[1] for point in local_2d)
            max_v = max(point[1] for point in local_2d)
            height = max_v - min_v
            max_rows = max(1, min(24, int(math.ceil(max(0.25, ROBLOX_TERRAIN_DETAIL) * 3))))
            target = max(0.02, min(CELL) * max(0.75, 3.5 / max(0.25, ROBLOX_TERRAIN_DETAIL)))
            min_rows = 1
            rows = max(min_rows, min(max_rows, math.ceil(height / target)))
            overlap = min(max(1.0, SURFACE_OVERLAP), 1.15)
            thickness = max(0.01, min(CELL) * 0.55 * overlap)
            for row in range(rows):
                v = min_v + (row + 0.5) * height / rows
                hits = []
                for idx in range(3):
                    p = local_2d[idx]
                    q = local_2d[(idx + 1) % 3]
                    if min(p[1], q[1]) <= v <= max(p[1], q[1]) and abs(p[1] - q[1]) > 0.000001:
                        t = (v - p[1]) / (q[1] - p[1])
                        hits.append(p[0] + (q[0] - p[0]) * t)
                if len(hits) < 2:
                    continue
                hits.sort()
                width = max(0.01, hits[-1] - hits[0])
                center = vec_add(p0, vec_add(vec_mul(strip_u, (hits[0] + hits[-1]) * 0.5), vec_mul(strip_v, v)))
                output.append({
                    "color": block["color"],
                    "can_collide": bool(block.get("can_collide", True)),
                    "cframe": [
                        clean(center[0]), clean(center[1]), clean(center[2]),
                        clean(strip_u[0]), clean(strip_v[0]), clean(normal[0]),
                        clean(strip_u[1]), clean(strip_v[1]), clean(normal[1]),
                        clean(strip_u[2]), clean(strip_v[2]), clean(normal[2]),
                    ],
                    "size": [
                        clean(width * overlap),
                        clean(max(0.01, height / rows) * overlap),
                        clean(thickness),
                    ],
                })

        for poly in polys:
            for i in range(1, len(poly) - 1):
                append_limited_triangle(poly[0], poly[i], poly[i + 1])

    def append_corner_wedge_fill_layers(block):
        sx, sy, sz = (max(0.001, value) for value in block["size"])
        if ROBLOX_TERRAIN_DETAIL < 4.0:
            return
        cols = roblox_layer_count(sx * scale, cap_per_detail=5, hard_cap=36)
        rows = roblox_layer_count(sz * scale, cap_per_detail=5, hard_cap=36)
        overlap = min(max(1.0, SURFACE_OVERLAP), 1.15)
        min_local = 0.01 / max(scale, 0.000001)
        cell_x = sx / cols
        cell_z = sz / rows
        for rz in range(rows):
            w = (rz + 0.5) / rows
            run_start = None
            run_end = None
            run_height = None
            for cx in range(cols):
                u = (cx + 0.5) / cols
                height_ratio = max(0.0, min(u, 1.0 - w))
                if height_ratio <= 0.0001:
                    if run_start is not None:
                        run_cols = run_end - run_start + 1
                        mid_u = (run_start + run_cols * 0.5) / cols
                        local_x = -sx * 0.5 + mid_u * sx
                        local_z = -sz * 0.5 + (rz + 0.5) * cell_z
                        local_y = -sy * 0.5 + run_height * 0.5
                        append_local_box(block, (local_x, local_y, local_z), (cell_x * run_cols * overlap, run_height * overlap, cell_z * overlap))
                    run_start = run_end = run_height = None
                    continue
                height = max(sy * height_ratio, min_local)
                quantized_height = max(min_local, round(height / max(min_local, sy / max(cols, rows, 1))) * max(min_local, sy / max(cols, rows, 1)))
                if run_start is None or abs(quantized_height - run_height) > max(min_local, sy * 0.035):
                    if run_start is not None:
                        run_cols = run_end - run_start + 1
                        mid_u = (run_start + run_cols * 0.5) / cols
                        local_x = -sx * 0.5 + mid_u * sx
                        local_z = -sz * 0.5 + (rz + 0.5) * cell_z
                        local_y = -sy * 0.5 + run_height * 0.5
                        append_local_box(block, (local_x, local_y, local_z), (cell_x * run_cols * overlap, run_height * overlap, cell_z * overlap))
                    run_start = cx
                    run_end = cx
                    run_height = quantized_height
                else:
                    run_end = cx
            if run_start is not None:
                run_cols = run_end - run_start + 1
                mid_u = (run_start + run_cols * 0.5) / cols
                local_x = -sx * 0.5 + mid_u * sx
                local_z = -sz * 0.5 + (rz + 0.5) * cell_z
                local_y = -sy * 0.5 + run_height * 0.5
                append_local_box(block, (local_x, local_y, local_z), (cell_x * run_cols * overlap, run_height * overlap, cell_z * overlap))

    def append_corner_wedge_steps(block):
        append_corner_wedge_fill_layers(block)

    def append_sphere_stack(block):
        u_axis, v_axis, n_axis = block["axes"]
        sx, sy, sz = block["size"]
        scaled = (sx * scale, sy * scale, sz * scale)
        if max(scaled) < min(CELL) * 1.5:
            return False
        layers = 17
        layer_height = sy / layers
        rows = 17
        overlap = min(max(1.0, SURFACE_OVERLAP), 1.08)
        min_local = 0.01 / max(scale, 0.000001)
        for i in range(layers):
            y_t = -1.0 + (i + 0.5) * 2.0 / layers
            layer_radius = math.sqrt(max(0.0, 1.0 - y_t * y_t))
            rx = max(min_local * 0.5, sx * 0.5 * layer_radius)
            rz = max(min_local * 0.5, sz * 0.5 * layer_radius)
            local_y = y_t * sy * 0.5
            step_x = (rx * 2.0) / rows
            for row in range(rows):
                local_x = -rx + (row + 0.5) * step_x
                x_t = local_x / max(rx, 0.000001)
                chord = rz * 2.0 * math.sqrt(max(0.0, 1.0 - x_t * x_t))
                if chord * scale < 0.01:
                    continue
                append_local_box(
                    block,
                    (local_x, local_y, 0.0),
                    (max(step_x * overlap, min_local), max(layer_height * 1.08, min_local), max(chord * overlap, min_local)),
                    axes=(u_axis, v_axis, n_axis),
                )
        return True

    def append_cylinder_shell(block):
        u_axis, v_axis, n_axis = block["axes"]
        sx, sy, sz = block["size"]
        axis_name = (block.get("cylinder_axis") or "x").lower()
        if axis_name == "y":
            length_size = sy
            radius_a_size = sx
            radius_b_size = sz
            length_axis = v_axis
            circle_a_axis = u_axis
            circle_b_axis = n_axis

            def local_point(length_value, a_value, b_value):
                return (a_value, length_value, b_value)

            def cap_size(step_value, chord_value, wall_value):
                return (step_value, wall_value, chord_value)
        elif axis_name == "z":
            length_size = sz
            radius_a_size = sx
            radius_b_size = sy
            length_axis = n_axis
            circle_a_axis = u_axis
            circle_b_axis = v_axis

            def local_point(length_value, a_value, b_value):
                return (a_value, b_value, length_value)

            def cap_size(step_value, chord_value, wall_value):
                return (step_value, chord_value, wall_value)
        else:
            length_size = sx
            radius_a_size = sy
            radius_b_size = sz
            length_axis = u_axis
            circle_a_axis = v_axis
            circle_b_axis = n_axis

            def local_point(length_value, a_value, b_value):
                return (length_value, a_value, b_value)

            def cap_size(step_value, chord_value, wall_value):
                return (wall_value, step_value, chord_value)

        scaled_radius = (radius_a_size * scale, radius_b_size * scale)
        if min(scaled_radius) < min(CELL) * 1.35:
            return False
        detail = max(0.25, float(ROBLOX_TERRAIN_DETAIL))
        radius_hint = max(scaled_radius)
        segments = max(65, min(96, int(math.ceil(radius_hint / max(min(CELL) * 0.8, 0.01)))))
        segments = max(segments, min(96, int(math.ceil(detail * 16))))
        cap_rows = max(65, min(96, int(math.ceil(segments * 0.9))))
        overlap = 1.0
        min_local = 0.01 / max(scale, 0.000001)
        wall = max(min_local, min(radius_a_size, radius_b_size) * 0.055, min(CELL) * 0.55 / max(scale, 0.000001))
        wall = min(wall, max(min_local, min(radius_a_size, radius_b_size) * 0.22))
        radius_a = radius_a_size * 0.5
        radius_b = radius_b_size * 0.5
        panel_a = max(min_local * 0.5, radius_a - wall * 0.5)
        panel_b = max(min_local * 0.5, radius_b - wall * 0.5)
        ring_mode = block.get("shape") == "ring"
        flat_disk = ring_mode or length_size <= min(radius_a_size, radius_b_size) * 0.75
        long_cylinder = length_size >= max(radius_a_size, radius_b_size) * 2.25

        def append_cap_scanlines(cap_length, rows, cap_wall, inner_ratio=0.0):
            step_a = radius_a_size / rows
            inner_a = radius_a * max(0.0, min(0.9, inner_ratio))
            inner_b = radius_b * max(0.0, min(0.9, inner_ratio))
            for i in range(rows):
                local_a = -radius_a_size * 0.5 + (i + 0.5) * step_a
                t = local_a / max(radius_a, 0.000001)
                chord = radius_b_size * math.sqrt(max(0.0, 1.0 - t * t))
                if chord * scale < 0.01:
                    continue
                segments_to_add = [(0.0, chord)]
                if inner_ratio > 0 and abs(local_a) < inner_a:
                    inner_t = local_a / max(inner_a, 0.000001)
                    inner_chord = inner_b * 2.0 * math.sqrt(max(0.0, 1.0 - inner_t * inner_t))
                    half_outer = chord * 0.5
                    half_inner = inner_chord * 0.5
                    outer_piece = max(0.0, half_outer - half_inner)
                    segments_to_add = []
                    if outer_piece * scale >= 0.01:
                        offset = half_inner + outer_piece * 0.5
                        segments_to_add.append((-offset, outer_piece))
                        segments_to_add.append((offset, outer_piece))
                for local_b, piece_chord in segments_to_add:
                    append_local_box(
                        block,
                        local_point(cap_length, local_a, local_b),
                        cap_size(max(step_a * overlap, min_local), max(piece_chord * overlap, min_local), cap_wall),
                    )

        if flat_disk:
            # Flat circles use one clumped scanline set. Fewer wider bands avoid
            # the comb/spike look caused by hundreds of skinny visible strips.
            disk_rows = 11
            cap_wall = max(min_local, length_size * 0.48)
            inner_ratio = 0.46 if ring_mode else 0.0
            append_cap_scanlines(0.0, disk_rows, cap_wall, inner_ratio=inner_ratio)
            return True

        if not ring_mode and long_cylinder:
            # Long cylinders use one fake-cylinder set: each plank runs through
            # the full shape to the opposite side.
            rod_slices = 60
            plank_width = max(radius_a_size, radius_b_size) * overlap
            plank_thickness = max(min_local, min(radius_a_size, radius_b_size) / max(10.0, rod_slices * 0.45))
            for i in range(rod_slices):
                angle = math.radians(i * 3.0)
                ca = math.cos(angle)
                sa = math.sin(angle)
                cross_a = vec_norm(vec_add(vec_mul(circle_a_axis, ca), vec_mul(circle_b_axis, sa)))
                cross_b = vec_norm(vec_add(vec_mul(circle_a_axis, -sa), vec_mul(circle_b_axis, ca)))
                append_local_box(
                    block,
                    (0.0, 0.0, 0.0),
                    (length_size * overlap, plank_width, plank_thickness),
                    axes=(length_axis, cross_a, cross_b),
                )
            return True

        if not ring_mode:
            # Broken/ambiguous chunky cylinders fall back to one simple long
            # cuboid instead of trying to approximate a round mesh badly.
            append_local_box(
                block,
                (0.0, 0.0, 0.0),
                (length_size * overlap, radius_a_size, radius_b_size),
                axes=(length_axis, circle_a_axis, circle_b_axis),
            )
            return True

        # Short/chunky cylinders use one continuous wall with caps. Keeping
        # chunk_count at 1 avoids stacked cylinders along the axis, and using
        # clumped panels avoids the bowtie/star from through-center planks.
        chunk_count = 1
        chunk_len = length_size / chunk_count
        for i in range(segments):
            a0 = math.tau * i / segments
            a1 = math.tau * (i + 1) / segments
            am = (a0 + a1) * 0.5
            p0 = (math.cos(a0) * panel_a, math.sin(a0) * panel_b)
            p1 = (math.cos(a1) * panel_a, math.sin(a1) * panel_b)
            chord = math.hypot(p1[0] - p0[0], p1[1] - p0[1])
            if chord * scale < 0.01:
                continue
            local_a = math.cos(am) * panel_a
            local_b = math.sin(am) * panel_b
            tangent = vec_norm(vec_add(vec_mul(circle_a_axis, p1[0] - p0[0]), vec_mul(circle_b_axis, p1[1] - p0[1])))
            radial_hint = vec_norm(vec_add(vec_mul(circle_a_axis, math.cos(am)), vec_mul(circle_b_axis, math.sin(am))))
            radial = vec_norm(vec_cross(length_axis, tangent))
            if vec_dot(radial, radial_hint) < 0:
                radial = vec_mul(radial, -1)
            for chunk in range(chunk_count):
                local_len = -length_size * 0.5 + (chunk + 0.5) * chunk_len
                append_local_box(
                    block,
                    local_point(local_len, local_a, local_b),
                    (chunk_len * overlap, max(chord * overlap, min_local), wall),
                    axes=(length_axis, tangent, radial),
                )

        for side in (-1, 1):
            cap_length = side * (length_size * 0.5 + wall * 0.5)
            append_cap_scanlines(cap_length, cap_rows, wall)
        return True

    def append_cone_stack(block):
        sx, sy, sz = block["size"]
        scaled = (sx * scale, sy * scale, sz * scale)
        if max(scaled) < min(CELL) * 1.25:
            return False
        detail = max(0.25, float(ROBLOX_TERRAIN_DETAIL))
        layers = max(3, min(32, int(math.ceil(scaled[1] / max(min(CELL) * 1.4, 0.01)))))
        layers = max(layers, min(32, int(math.ceil(detail * 4))))
        overlap = min(max(1.0, SURFACE_OVERLAP), 1.10)
        min_local = 0.01 / max(scale, 0.000001)
        layer_h = sy / layers
        for i in range(layers):
            t = (i + 0.5) / layers
            radius = max(0.06, 1.0 - t)
            ly = -sy * 0.5 + (i + 0.5) * layer_h
            append_local_box(
                block,
                (0.0, ly, 0.0),
                (max(sx * radius * overlap, min_local), max(layer_h * overlap, min_local), max(sz * radius * overlap, min_local)),
            )
        return True

    for block in rotated:
        out_center = fit_point(block["center"])
        u_axis, v_axis, n_axis = block["axes"]
        if ROBLOX_PRIMITIVES:
            if block.get("shape") == "sphere" and append_sphere_stack(block):
                continue
            if block.get("shape") == "cone" and append_cone_stack(block):
                continue
            if block.get("shape") in {"cylinder", "ring"} and append_cylinder_shell(block):
                continue
        if block.get("shape") == "corner_wedge":
            if is_tiny_roblox_part(block):
                append_plain_block(block)
                continue
            append_corner_wedge_surface(block)
            append_corner_wedge_fill_layers(block)
            continue
        if block.get("shape") == "wedge":
            if is_tiny_roblox_part(block):
                append_plain_block(block)
                continue
            append_wedge_slab(block)
            append_wedge_fill_layers(block)
            continue
        append_plain_block(block)
    return output, scale


def load_surface_model(path):
    ext = path.suffix.lower()
    if ext in {".glb", ".gltf"}:
        gltf, buffers = read_gltf_asset(path)
        return parse_mesh(gltf, buffers)
    if ext == ".obj":
        return parse_obj_surface(path)
    if ext == ".stl":
        return parse_stl_surface(path)
    if ext == ".ply":
        return parse_ply_surface(path)
    if ext in ROBLOX_XML_EXTS:
        return parse_roblox_studio_surface(path)
    raise ValueError(f"unsupported model format: {ext}")


def tri_area(a, b, c):
    ab = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
    ac = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
    cr = (
        ab[1] * ac[2] - ab[2] * ac[1],
        ab[2] * ac[0] - ab[0] * ac[2],
        ab[0] * ac[1] - ab[1] * ac[0],
    )
    return math.sqrt(cr[0] ** 2 + cr[1] ** 2 + cr[2] ** 2) * 0.5


def cpu_worker_count():
    return max(1, os.cpu_count() or 4)


def identity_tx(v):
    return v


def chunk_list(items, chunk_count):
    if not items:
        return []
    chunk_count = max(1, min(int(chunk_count), len(items)))
    chunk_size = math.ceil(len(items) / chunk_count)
    return [items[i:i + chunk_size] for i in range(0, len(items), chunk_size)]


def vec_add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def vec_sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def vec_mul(a, s):
    return (a[0] * s, a[1] * s, a[2] * s)


def vec_dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def vec_cross(a, b):
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def vec_len(a):
    return math.sqrt(vec_dot(a, a))


def vec_norm(a):
    n = vec_len(a) or 1
    return (a[0] / n, a[1] / n, a[2] / n)


def interp_uv(uvs, weights):
    if not uvs or any(item is None for item in uvs):
        return None
    return (
        uvs[0][0] * weights[0] + uvs[1][0] * weights[1] + uvs[2][0] * weights[2],
        uvs[0][1] * weights[0] + uvs[1][1] * weights[1] + uvs[2][1] * weights[2],
    )


def barycentric_2d(p, a, b, c):
    v0 = (b[0] - a[0], b[1] - a[1])
    v1 = (c[0] - a[0], c[1] - a[1])
    v2 = (p[0] - a[0], p[1] - a[1])
    d00 = v0[0] * v0[0] + v0[1] * v0[1]
    d01 = v0[0] * v1[0] + v0[1] * v1[1]
    d11 = v1[0] * v1[0] + v1[1] * v1[1]
    d20 = v2[0] * v0[0] + v2[1] * v0[1]
    d21 = v2[0] * v1[0] + v2[1] * v1[1]
    denom = d00 * d11 - d01 * d01
    if abs(denom) < 0.00000001:
        return (1 / 3, 1 / 3, 1 / 3)
    v = (d11 * d20 - d01 * d21) / denom
    w = (d00 * d21 - d01 * d20) / denom
    u = 1 - v - w
    return (max(0.0, min(1.0, u)), max(0.0, min(1.0, v)), max(0.0, min(1.0, w)))


def triangle_slab_blocks(verts, faces, materials, tx):
    detail_boost = max(1.0, SMALL_DETAIL_BOOST)
    strip_height = min(CELL) / max(0.1, FACE_FILL * detail_boost)
    min_rows = max(1, math.ceil(detail_boost))
    overlap = max(1.0, SURFACE_OVERLAP)
    thickness = max(0.01, min(CELL) * 0.55 * overlap)
    blocks = []
    for face in faces:
        ai, bi, ci, face_color = face[:4]
        mat_index = face[4] if len(face) > 4 else None
        face_uvs = face[5] if len(face) > 5 else None
        mat = materials[mat_index] if mat_index is not None and mat_index < len(materials) else {"fallback": unpack_color(face_color) if isinstance(face_color, int) else face_color, "texture": None}
        pts = [tx(verts[ai]), tx(verts[bi]), tx(verts[ci])]
        edges = [
            (0, 1, vec_len(vec_sub(pts[1], pts[0]))),
            (1, 2, vec_len(vec_sub(pts[2], pts[1]))),
            (2, 0, vec_len(vec_sub(pts[0], pts[2]))),
        ]
        edges.sort(key=lambda item: item[2], reverse=True)
        i0, i1 = edges[0][0], edges[0][1]
        i2 = 3 - i0 - i1
        p0 = pts[i0]
        p1 = pts[i1]
        p2 = pts[i2]
        uv_ordered = [face_uvs[i0], face_uvs[i1], face_uvs[i2]] if face_uvs else None
        u_axis = vec_norm(vec_sub(p1, p0))
        normal = vec_norm(vec_cross(vec_sub(p1, p0), vec_sub(p2, p0)))
        if vec_len(normal) < 0.001:
            continue
        v_axis = vec_norm(vec_cross(normal, u_axis))
        local = []
        for p in (p0, p1, p2):
            rel = vec_sub(p, p0)
            local.append((vec_dot(rel, u_axis), vec_dot(rel, v_axis)))
        min_v = min(p[1] for p in local)
        max_v = max(p[1] for p in local)
        rows = max(min_rows, math.ceil((max_v - min_v) / max(strip_height, 0.001)))
        for row in range(rows):
            v = min_v + (row + 0.5) * (max_v - min_v) / rows
            hits = []
            for i in range(3):
                p = local[i]
                q = local[(i + 1) % 3]
                if min(p[1], q[1]) <= v <= max(p[1], q[1]) and abs(p[1] - q[1]) > 0.000001:
                    t = (v - p[1]) / (q[1] - p[1])
                    hits.append(p[0] + (q[0] - p[0]) * t)
            if len(hits) < 2:
                continue
            hits.sort()
            width = max(0.01, hits[-1] - hits[0]) + strip_height * (overlap - 1)
            u = (hits[0] + hits[-1]) * 0.5
            center = vec_add(p0, vec_add(vec_mul(u_axis, u), vec_mul(v_axis, v)))
            weights = barycentric_2d((u, v), local[i0], local[i1], local[i2])
            uv = interp_uv(uv_ordered, weights)
            color = sample_texture(mat["texture"], uv, mat["fallback"])
            blocks.append({
                "color": color,
                "cframe": [
                    clean(center[0]), clean(center[1]), clean(center[2]),
                    clean(u_axis[0]), clean(v_axis[0]), clean(normal[0]),
                    clean(u_axis[1]), clean(v_axis[1]), clean(normal[1]),
                    clean(u_axis[2]), clean(v_axis[2]), clean(normal[2]),
                ],
                "size": [clean(width), clean(max(strip_height, (max_v - min_v) / rows) * overlap), clean(thickness)],
            })
    return blocks


SLAB_WORKER_VERTS = None
SLAB_WORKER_MATERIALS = None


def init_triangle_slab_worker(verts, materials, cell, face_fill, small_detail_boost, surface_overlap):
    global CELL, FACE_FILL, SMALL_DETAIL_BOOST, SURFACE_OVERLAP
    global SLAB_WORKER_VERTS, SLAB_WORKER_MATERIALS
    SLAB_WORKER_VERTS = verts
    SLAB_WORKER_MATERIALS = materials
    CELL = cell
    FACE_FILL = face_fill
    SMALL_DETAIL_BOOST = small_detail_boost
    SURFACE_OVERLAP = surface_overlap


def triangle_slab_worker(faces):
    return triangle_slab_blocks(SLAB_WORKER_VERTS, faces, SLAB_WORKER_MATERIALS, identity_tx)


def triangle_slab_blocks_parallel(verts, faces, materials, progress=None, label="triangle strips"):
    if not faces:
        return []
    cores = cpu_worker_count()
    if cores <= 1 or len(faces) < max(cores * 8, 256):
        return triangle_slab_blocks(verts, faces, materials, identity_tx)

    chunks = chunk_list(faces, cores * CHUNKS_PER_CORE)
    blocks = []
    try:
        with mp.Pool(
            processes=cores,
            initializer=init_triangle_slab_worker,
            initargs=(verts, materials, CELL, FACE_FILL, SMALL_DETAIL_BOOST, SURFACE_OVERLAP),
        ) as pool:
            for i, partial in enumerate(pool.imap_unordered(triangle_slab_worker, chunks), 1):
                blocks.extend(partial)
                if progress:
                    progress(f"{label} worker chunk {i}/{len(chunks)} done; blocks={len(blocks)}")
        return blocks
    except Exception as exc:
        if progress:
            progress(f"{label} multiprocessing fallback: {exc}")
        return triangle_slab_blocks(verts, faces, materials, identity_tx)


def face_normal(points):
    return vec_norm(vec_cross(vec_sub(points[1], points[0]), vec_sub(points[2], points[0])))


def ordered_quad_points(points, normal):
    center = (
        sum(p[0] for p in points) / 4,
        sum(p[1] for p in points) / 4,
        sum(p[2] for p in points) / 4,
    )
    axis = vec_norm(vec_sub(points[0], center))
    if vec_len(axis) < 0.001:
        axis = (1, 0, 0)
    axis2 = vec_norm(vec_cross(normal, axis))
    return sorted(points, key=lambda p: math.atan2(vec_dot(vec_sub(p, center), axis2), vec_dot(vec_sub(p, center), axis)))


def ordered_quad_items(ids, points, normal):
    center = (
        sum(p[0] for p in points) / 4,
        sum(p[1] for p in points) / 4,
        sum(p[2] for p in points) / 4,
    )
    axis = vec_norm(vec_sub(points[0], center))
    if vec_len(axis) < 0.001:
        axis = (1, 0, 0)
    axis2 = vec_norm(vec_cross(normal, axis))
    return sorted(
        zip(ids, points),
        key=lambda item: math.atan2(
            vec_dot(vec_sub(item[1], center), axis2),
            vec_dot(vec_sub(item[1], center), axis),
        ),
    )


def positions_are_opposite(a, b):
    return {a, b} in ({0, 2}, {1, 3})


def quad_block_from_pair(verts, face_a, face_b, tx):
    ids = list(dict.fromkeys([face_a[0], face_a[1], face_a[2], face_b[0], face_b[1], face_b[2]]))
    if len(ids) != 4:
        return None
    shared = set(face_a[:3]) & set(face_b[:3])
    if len(shared) != 2:
        return None
    pts = [tx(verts[i]) for i in ids]
    n1 = face_normal([tx(verts[face_a[0]]), tx(verts[face_a[1]]), tx(verts[face_a[2]])])
    n2 = face_normal([tx(verts[face_b[0]]), tx(verts[face_b[1]]), tx(verts[face_b[2]])])
    if abs(vec_dot(n1, n2)) < 0.9999:
        return None
    normal = vec_norm(vec_add(n1, n2))
    if vec_len(normal) < 0.001:
        normal = n1
    ordered_items = ordered_quad_items(ids, pts, normal)
    ordered_ids = [item[0] for item in ordered_items]
    shared_positions = [i for i, item_id in enumerate(ordered_ids) if item_id in shared]
    if len(shared_positions) != 2 or not positions_are_opposite(shared_positions[0], shared_positions[1]):
        return None
    ordered = [item[1] for item in ordered_items]
    plane_origin = ordered[0]
    for point in ordered[1:]:
        if abs(vec_dot(vec_sub(point, plane_origin), normal)) > max(min(CELL) * 0.20, 0.002):
            return None
    edge_lengths = [vec_len(vec_sub(ordered[(i + 1) % 4], ordered[i])) for i in range(4)]
    if min(edge_lengths) < 0.001:
        return None
    width = (edge_lengths[0] + edge_lengths[2]) * 0.5
    height = (edge_lengths[1] + edge_lengths[3]) * 0.5
    if abs(edge_lengths[0] - edge_lengths[2]) > max(width, 0.001) * 0.06:
        return None
    if abs(edge_lengths[1] - edge_lengths[3]) > max(height, 0.001) * 0.06:
        return None
    u_axis = vec_norm(vec_sub(ordered[1], ordered[0]))
    v_axis = vec_norm(vec_sub(ordered[2], ordered[1]))
    if abs(vec_dot(u_axis, v_axis)) > 0.035:
        return None
    diagonal_a = vec_len(vec_sub(ordered[2], ordered[0]))
    diagonal_b = vec_len(vec_sub(ordered[3], ordered[1]))
    if abs(diagonal_a - diagonal_b) > max(diagonal_a, diagonal_b, 0.001) * 0.06:
        return None
    normal = vec_norm(vec_cross(u_axis, v_axis))
    center = (
        sum(p[0] for p in ordered) / 4,
        sum(p[1] for p in ordered) / 4,
        sum(p[2] for p in ordered) / 4,
    )
    overlap = min(max(1.0, SURFACE_OVERLAP), 1.015)
    thickness = max(0.01, min(CELL) * 0.55 * overlap)
    return {
        "color": face_a[3],
        "cframe": [
            clean(center[0]), clean(center[1]), clean(center[2]),
            clean(u_axis[0]), clean(v_axis[0]), clean(normal[0]),
            clean(u_axis[1]), clean(v_axis[1]), clean(normal[1]),
            clean(u_axis[2]), clean(v_axis[2]), clean(normal[2]),
        ],
        "size": [clean(width * overlap), clean(height * overlap), clean(thickness)],
    }


def triangle_surface_blocks(verts, faces, materials, tx, progress=None):
    transformed = [tx(v) for v in verts]
    if not MERGE_TRIANGLE_QUADS:
        return triangle_slab_blocks_parallel(transformed, faces, materials, progress, "triangle strips")
    edge_to_faces = {}
    for idx, face in enumerate(faces):
        a, b, c = face[:3]
        for edge in ((a, b), (b, c), (c, a)):
            edge_to_faces.setdefault(tuple(sorted(edge)), []).append(idx)
    used = set()
    blocks = []
    leftovers = []
    for idx, face in enumerate(faces):
        if idx in used:
            continue
        mate = None
        for edge in ((face[0], face[1]), (face[1], face[2]), (face[2], face[0])):
            for other_idx in edge_to_faces.get(tuple(sorted(edge)), []):
                if other_idx == idx or other_idx in used:
                    continue
                other = faces[other_idx]
                if close_color(face[3], other[3]):
                    block = quad_block_from_pair(transformed, face, other, identity_tx)
                    if block:
                        mate = (other_idx, block)
                        break
            if mate:
                break
        if mate:
            used.add(idx)
            used.add(mate[0])
            blocks.append(mate[1])
        else:
            used.add(idx)
            leftovers.append(face)
    quad_blocks = len(blocks)
    blocks.extend(triangle_slab_blocks_parallel(transformed, leftovers, materials, progress, "leftover triangle strips"))
    print(f"quad merged triangles={len(faces) - len(leftovers)} leftovers={len(leftovers)} quad_blocks={quad_blocks}", flush=True)
    return blocks


def worker(args):
    tasks, cell, step, fill_radius = args
    grid = {}
    denom = max(step * step, 0.00000001)
    fill_radius = max(0, int(fill_radius))

    def set_voxel(x, y, z, color):
        if not fill_radius:
            grid[(x, y, z)] = color
            return
        for dx in range(-fill_radius, fill_radius + 1):
            for dy in range(-fill_radius, fill_radius + 1):
                for dz in range(-fill_radius, fill_radius + 1):
                    grid[(x + dx, y + dy, z + dz)] = color

    for a, b, c, color in tasks:
        samples = max(1, int(tri_area(a, b, c) / denom))
        n = max(1, int(math.sqrt(samples) * 1.6))
        trans_code = round(color_transparency(color) * 4)
        packed = (trans_code << 24) | (round(color[0]) << 16) | (round(color[1]) << 8) | round(color[2])
        for i in range(n + 1):
            for j in range(n - i + 1):
                u = i / n
                v = j / n
                w = 1 - u - v
                px = a[0] * w + b[0] * u + c[0] * v
                py = a[1] * w + b[1] * u + c[1] * v
                pz = a[2] * w + b[2] * u + c[2] * v
                set_voxel(round(px / cell[0]), round(py / cell[1]), round(pz / cell[2]), packed)
    return grid


def unpack_color(color):
    if isinstance(color, tuple) or isinstance(color, list):
        return color[:3]
    return ((color >> 16) & 255, (color >> 8) & 255, color & 255)


def close_color(a, b):
    if MERGE_TOLERANCE <= 0:
        return a == b
    ar, ag, ab = unpack_color(a)
    br, bg, bb = unpack_color(b)
    return color_transparency(a) == color_transparency(b) and (ar - br) ** 2 + (ag - bg) ** 2 + (ab - bb) ** 2 <= MERGE_TOLERANCE ** 2


def merge_voxels(grid):
    remaining = set(grid.keys())
    blocks = []
    ordered = sorted(remaining)
    for first in ordered:
        if first not in remaining:
            continue
        x, y, z = first
        color = grid[(x, y, z)]
        sx = 1
        while (x + sx, y, z) in remaining and close_color(grid[(x + sx, y, z)], color):
            sx += 1
        sy = 1
        while True:
            row_ok = True
            for xx in range(x, x + sx):
                if (xx, y + sy, z) not in remaining or not close_color(grid[(xx, y + sy, z)], color):
                    row_ok = False
                    break
            if not row_ok:
                break
            sy += 1
        sz = 1
        while True:
            layer_ok = True
            for xx in range(x, x + sx):
                for yy in range(y, y + sy):
                    if (xx, yy, z + sz) not in remaining or not close_color(grid[(xx, yy, z + sz)], color):
                        layer_ok = False
                        break
                if not layer_ok:
                    break
            if not layer_ok:
                break
            sz += 1
        for xx in range(x, x + sx):
            for yy in range(y, y + sy):
                for zz in range(z, z + sz):
                    remaining.discard((xx, yy, zz))
        blocks.append((x, y, z, sx, sy, sz, color))
    return blocks


def merge_worker(items):
    return merge_voxels(dict(items))


def split_grid_for_parallel_merge(grid, parts):
    if parts <= 1 or len(grid) < 50000:
        return [list(grid.items())]
    zs = [key[2] for key in grid.keys()]
    min_z, max_z = min(zs), max(zs)
    width = max(1, math.ceil((max_z - min_z + 1) / parts))
    buckets = [[] for _ in range(parts)]
    for item in grid.items():
        z = item[0][2]
        idx = min(parts - 1, max(0, (z - min_z) // width))
        buckets[idx].append(item)
    return [bucket for bucket in buckets if bucket]


DEFAULT_SETTINGS = {
    "cell": 0.075,
    "shrink": 5.0,
    "split_size": 150000,
    "surface_slabs": True,
    "roblox_primitives": True,
    "face_fill": 2.0,
    "small_detail_boost": 1.0,
    "fill_radius": 1,
    "merge_tolerance": 12.0,
    "surface_overlap": 1.08,
    "merge_triangle_quads": True,
    "roblox_slope_drop": 0.5,
    "roblox_wedge_fill": 2.0,
    "roblox_terrain_detail": 1.0,
    "rot_x": 0.0,
    "rot_y": 0.0,
    "rot_z": 0.0,
}


def safe_stem(name):
    stem = Path(name).stem
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in stem).strip("_")
    return cleaned or "model"


USERNAME_FONT = {
    "A": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
    "B": ["11110", "10001", "10001", "11110", "10001", "10001", "11110"],
    "C": ["01111", "10000", "10000", "10000", "10000", "10000", "01111"],
    "D": ["11110", "10001", "10001", "10001", "10001", "10001", "11110"],
    "E": ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
    "F": ["11111", "10000", "10000", "11110", "10000", "10000", "10000"],
    "G": ["01111", "10000", "10000", "10111", "10001", "10001", "01110"],
    "H": ["10001", "10001", "10001", "11111", "10001", "10001", "10001"],
    "I": ["11111", "00100", "00100", "00100", "00100", "00100", "11111"],
    "J": ["00111", "00010", "00010", "00010", "10010", "10010", "01100"],
    "K": ["10001", "10010", "10100", "11000", "10100", "10010", "10001"],
    "L": ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
    "M": ["10001", "11011", "10101", "10101", "10001", "10001", "10001"],
    "N": ["10001", "11001", "10101", "10011", "10001", "10001", "10001"],
    "O": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
    "P": ["11110", "10001", "10001", "11110", "10000", "10000", "10000"],
    "Q": ["01110", "10001", "10001", "10001", "10101", "10010", "01101"],
    "R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
    "S": ["01111", "10000", "10000", "01110", "00001", "00001", "11110"],
    "T": ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
    "U": ["10001", "10001", "10001", "10001", "10001", "10001", "01110"],
    "V": ["10001", "10001", "10001", "10001", "10001", "01010", "00100"],
    "W": ["10001", "10001", "10001", "10101", "10101", "11011", "10001"],
    "X": ["10001", "01010", "00100", "00100", "00100", "01010", "10001"],
    "Y": ["10001", "01010", "00100", "00100", "00100", "00100", "00100"],
    "Z": ["11111", "00001", "00010", "00100", "01000", "10000", "11111"],
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "10000", "11110", "00001", "00001", "11110"],
    "6": ["01110", "10000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00001", "01110"],
    "_": ["00000", "00000", "00000", "00000", "00000", "00000", "11111"],
    "-": ["00000", "00000", "00000", "11111", "00000", "00000", "00000"],
}


def create_roblox_username_model(username):
    username = (username or "").strip()
    if not username:
        raise ValueError("enter a Roblox username")
    cleaned = "".join(ch for ch in username if ch.isalnum() or ch in "_-")
    if not cleaned:
        raise ValueError("username must contain letters or numbers")
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    base = f"roblox_user_{safe_stem(cleaned)}_3d_text"
    out_zip = MODELS_DIR / f"{base}.zip"
    obj_name = f"{base}.obj"
    mtl_name = f"{base}.mtl"
    materials = [
        "roblox_red", "roblox_yellow", "roblox_green",
        "roblox_cyan", "roblox_blue", "roblox_purple",
    ]
    mtl = """newmtl roblox_red
Kd 0.950000 0.120000 0.120000
d 1.000000

newmtl roblox_yellow
Kd 1.000000 0.780000 0.120000
d 1.000000

newmtl roblox_green
Kd 0.160000 0.820000 0.280000
d 1.000000

newmtl roblox_cyan
Kd 0.050000 0.850000 1.000000
d 1.000000

newmtl roblox_blue
Kd 0.120000 0.320000 1.000000
d 1.000000

newmtl roblox_purple
Kd 0.620000 0.200000 1.000000
d 1.000000

newmtl dark_back
Kd 0.020000 0.025000 0.030000
d 1.000000
"""
    obj_lines = [f"mtllib {mtl_name}", f"o {base}"]
    vertices = []
    current_mat = [None]

    def use_mat(mat):
        if current_mat[0] != mat:
            obj_lines.append(f"usemtl {mat}")
            current_mat[0] = mat

    def add_box(x, y, z, sx, sy, sz, mat):
        use_mat(mat)
        start = len(vertices) + 1
        x0, x1 = x - sx / 2, x + sx / 2
        y0, y1 = y - sy / 2, y + sy / 2
        z0, z1 = z - sz / 2, z + sz / 2
        cube = [
            (x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
            (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1),
        ]
        vertices.extend(cube)
        obj_lines.extend(f"v {vx:.4f} {vy:.4f} {vz:.4f}" for vx, vy, vz in cube)
        for face in [
            (1, 2, 3, 4), (5, 8, 7, 6), (1, 5, 6, 2),
            (2, 6, 7, 3), (3, 7, 8, 4), (4, 8, 5, 1),
        ]:
            obj_lines.append("f " + " ".join(str(start + index - 1) for index in face))

    cell = 0.42
    gap = 0.045
    letter_gap = 0.42
    depth = 0.30
    letters = cleaned.upper()
    patterns = [USERNAME_FONT.get(ch, USERNAME_FONT["-"]) for ch in letters]
    widths = [len(pattern[0]) * (cell + gap) - gap for pattern in patterns]
    total_width = sum(widths) + letter_gap * max(0, len(widths) - 1)
    add_box(0, 1.45, -0.19, total_width + 0.55, 3.35, 0.12, "dark_back")
    x_cursor = -total_width / 2
    for idx, pattern in enumerate(patterns):
        mat = materials[idx % len(materials)]
        for row, line in enumerate(pattern):
            for col, bit in enumerate(line):
                if bit != "1":
                    continue
                x = x_cursor + col * (cell + gap) + cell / 2
                y = (len(pattern) - 1 - row) * (cell + gap) + cell / 2
                add_box(x, y, 0, cell, cell, depth, mat)
        x_cursor += widths[idx] + letter_gap

    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(obj_name, "\n".join(obj_lines) + "\n")
        zf.writestr(mtl_name, mtl)
    return out_zip


def roblox_api_json(url, data=None):
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    raw = None
    method = "GET"
    if data is not None:
        raw = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
        method = "POST"
    request = Request(url, data=raw, headers=headers, method=method)
    with urlopen(request, timeout=25) as response:
        return json.loads(response.read().decode("utf-8"))


def roblox_download_bytes(url):
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=25) as response:
        return response.read()


def roblox_user_from_username(username):
    username = (username or "").strip()
    if not username:
        raise ValueError("enter a Roblox username")
    data = roblox_api_json(
        "https://users.roblox.com/v1/usernames/users",
        {"usernames": [username], "excludeBannedUsers": False},
    )
    users = data.get("data") or []
    if not users:
        raise ValueError(f"Roblox user not found: {username}")
    return users[0]


def roblox_avatar_thumbnail(user_id):
    data = roblox_api_json(
        f"https://thumbnails.roblox.com/v1/users/avatar?userIds={int(user_id)}&size=720x720&format=Png&isCircular=false"
    )
    items = data.get("data") or []
    if not items or not items[0].get("imageUrl"):
        return None
    ensure_pillow()
    raw = roblox_download_bytes(items[0]["imageUrl"])
    return Image.open(io.BytesIO(raw)).convert("RGBA")


def avg_image_region(img, left, top, right, bottom, fallback):
    if img is None:
        return fallback
    x0 = max(0, min(img.width - 1, int(img.width * left)))
    y0 = max(0, min(img.height - 1, int(img.height * top)))
    x1 = max(x0 + 1, min(img.width, int(img.width * right)))
    y1 = max(y0 + 1, min(img.height, int(img.height * bottom)))
    total = [0, 0, 0]
    count = 0
    for y in range(y0, y1):
        for x in range(x0, x1):
            r, g, b, a = img.getpixel((x, y))
            if a < 30:
                continue
            total[0] += r
            total[1] += g
            total[2] += b
            count += 1
    if count < 8:
        return fallback
    return tuple(round(v / count) for v in total)


def color_from_brick_id(value, fallback=(163, 162, 165)):
    try:
        return ROBLOX_BRICK_COLORS.get(int(value), fallback)
    except Exception:
        return fallback


def avatar_asset_color(asset):
    name = (asset.get("name") or "").lower()
    type_name = ((asset.get("assetType") or {}).get("name") or "").lower()
    if any(word in name for word in ("glasses", "aviator", "shade", "mask")):
        return (18, 22, 28)
    if any(word in name for word in ("cap", "baseball", "hat")):
        return (196, 40, 28) if "roblox" in name or "r " in name else (35, 40, 46)
    if "hair" in type_name or "hair" in name:
        if "blonde" in name or "yellow" in name:
            return (226, 190, 80)
        if "green" in name:
            return (70, 150, 80)
        if "blue" in name:
            return (70, 130, 220)
        if "pink" in name:
            return (230, 110, 170)
        return (35, 28, 24)
    if "back" in type_name:
        return (45, 50, 58)
    if "shoulder" in type_name:
        return (245, 205, 48)
    if "front" in type_name:
        return (70, 90, 120)
    if "waist" in type_name:
        return (30, 30, 34)
    return (80, 86, 96)


def xml_escape(value):
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def avatar_part_xml(referent, class_name, name, position, size, color, transparency=0.0, shape=None):
    r, g, b = [max(0, min(255, int(round(v)))) for v in color[:3]]
    x, y, z = position
    sx, sy, sz = size
    lines = [
        f'<Item class="{xml_escape(class_name)}" referent="RBX{referent}">',
        "<Properties>",
        f'<string name="Name">{xml_escape(name)}</string>',
        f'<Vector3 name="Size"><X>{sx:.6f}</X><Y>{sy:.6f}</Y><Z>{sz:.6f}</Z></Vector3>',
        "<CoordinateFrame name=\"CFrame\">",
        f"<X>{x:.6f}</X><Y>{y:.6f}</Y><Z>{z:.6f}</Z>",
        "<R00>1</R00><R01>0</R01><R02>0</R02>",
        "<R10>0</R10><R11>1</R11><R12>0</R12>",
        "<R20>0</R20><R21>0</R21><R22>1</R22>",
        "</CoordinateFrame>",
        f'<Color3 name="Color"><R>{r / 255:.6f}</R><G>{g / 255:.6f}</G><B>{b / 255:.6f}</B></Color3>',
        f'<float name="Transparency">{max(0.0, min(1.0, float(transparency))):.6f}</float>',
        '<bool name="Anchored">true</bool>',
        '<bool name="CanCollide">true</bool>',
    ]
    if shape is not None:
        lines.append(f'<token name="Shape">{shape}</token>')
    lines.extend(["</Properties>", "</Item>"])
    return "\n".join(lines)


def create_roblox_avatar_model(username):
    user = roblox_user_from_username(username)
    user_id = int(user["id"])
    avatar = roblox_api_json(f"https://avatar.roblox.com/v1/users/{user_id}/avatar")
    image = None
    try:
        image = roblox_avatar_thumbnail(user_id)
    except Exception:
        image = None

    scales = avatar.get("scales") or {}
    width = max(0.55, float(scales.get("width", 1.0)))
    height = max(0.65, float(scales.get("height", 1.0)))
    depth = max(0.55, float(scales.get("depth", 1.0)))
    head_scale = max(0.7, float(scales.get("head", 1.0)))
    body_colors = avatar.get("bodyColors") or {}
    head_color = color_from_brick_id(body_colors.get("headColorId"), (234, 184, 146))
    torso_color = color_from_brick_id(body_colors.get("torsoColorId"), (13, 105, 172))
    left_arm_color = color_from_brick_id(body_colors.get("leftArmColorId"), head_color)
    right_arm_color = color_from_brick_id(body_colors.get("rightArmColorId"), head_color)
    left_leg_color = color_from_brick_id(body_colors.get("leftLegColorId"), (27, 42, 53))
    right_leg_color = color_from_brick_id(body_colors.get("rightLegColorId"), (27, 42, 53))

    shirt_color = avg_image_region(image, 0.40, 0.38, 0.60, 0.58, torso_color)
    left_pants_color = avg_image_region(image, 0.40, 0.62, 0.50, 0.82, left_leg_color)
    right_pants_color = avg_image_region(image, 0.50, 0.62, 0.60, 0.82, right_leg_color)

    parts = []

    def add(name, pos, size, color, transparency=0.0, shape=None, class_name="Part"):
        parts.append((class_name, name, pos, size, color, transparency, shape))

    leg_w = 0.46 * width
    arm_w = 0.42 * width
    body_d = 0.70 * depth
    limb_d = 0.42 * depth
    torso_w = 1.35 * width
    upper_torso_h = 1.05 * height
    lower_torso_h = 0.62 * height
    upper_leg_h = 0.86 * height
    lower_leg_h = 0.90 * height
    foot_h = 0.22 * height
    upper_arm_h = 0.88 * height
    lower_arm_h = 0.78 * height
    hand_h = 0.22 * height
    head_size = 1.05 * head_scale

    foot_y = foot_h / 2
    lower_leg_y = foot_h + lower_leg_h / 2
    upper_leg_y = foot_h + lower_leg_h + upper_leg_h / 2
    lower_torso_y = foot_h + lower_leg_h + upper_leg_h + lower_torso_h / 2
    upper_torso_y = foot_h + lower_leg_h + upper_leg_h + lower_torso_h + upper_torso_h / 2
    neck_y = upper_torso_y + upper_torso_h / 2 + 0.08
    head_y = neck_y + head_size / 2 + 0.10
    shoulder_y = upper_torso_y + upper_torso_h * 0.28
    upper_arm_y = shoulder_y - upper_arm_h / 2
    lower_arm_y = upper_arm_y - upper_arm_h / 2 - lower_arm_h / 2
    hand_y = lower_arm_y - lower_arm_h / 2 - hand_h / 2

    leg_x = 0.31 * width
    arm_x = torso_w / 2 + arm_w / 2 + 0.08
    front_z = -body_d / 2 - 0.035

    add("LeftFoot", (-leg_x, foot_y, -0.06), (leg_w, foot_h, limb_d * 1.35), left_leg_color)
    add("RightFoot", (leg_x, foot_y, -0.06), (leg_w, foot_h, limb_d * 1.35), right_leg_color)
    add("LeftLowerLeg", (-leg_x, lower_leg_y, 0), (leg_w, lower_leg_h, limb_d), left_leg_color)
    add("RightLowerLeg", (leg_x, lower_leg_y, 0), (leg_w, lower_leg_h, limb_d), right_leg_color)
    add("LeftUpperLeg", (-leg_x, upper_leg_y, 0), (leg_w * 1.05, upper_leg_h, limb_d * 1.05), left_leg_color)
    add("RightUpperLeg", (leg_x, upper_leg_y, 0), (leg_w * 1.05, upper_leg_h, limb_d * 1.05), right_leg_color)
    add("LowerTorso", (0, lower_torso_y, 0), (torso_w * 0.88, lower_torso_h, body_d), torso_color)
    add("UpperTorso", (0, upper_torso_y, 0), (torso_w, upper_torso_h, body_d * 1.05), torso_color)
    add("Neck", (0, neck_y, 0), (0.26 * width, 0.20 * height, 0.25 * depth), head_color)
    add("Head", (0, head_y, 0), (head_size, head_size, head_size), head_color, shape=0)
    add("LeftUpperArm", (-arm_x, upper_arm_y, 0), (arm_w, upper_arm_h, limb_d), left_arm_color)
    add("RightUpperArm", (arm_x, upper_arm_y, 0), (arm_w, upper_arm_h, limb_d), right_arm_color)
    add("LeftLowerArm", (-arm_x, lower_arm_y, 0), (arm_w * 0.95, lower_arm_h, limb_d * 0.95), left_arm_color)
    add("RightLowerArm", (arm_x, lower_arm_y, 0), (arm_w * 0.95, lower_arm_h, limb_d * 0.95), right_arm_color)
    add("LeftHand", (-arm_x, hand_y, 0), (arm_w * 1.03, hand_h, limb_d * 1.03), left_arm_color)
    add("RightHand", (arm_x, hand_y, 0), (arm_w * 1.03, hand_h, limb_d * 1.03), right_arm_color)

    add("ShirtFront", (0, upper_torso_y, front_z), (torso_w * 0.95, upper_torso_h * 0.82, 0.055), shirt_color)
    add("LowerShirtFront", (0, lower_torso_y, front_z), (torso_w * 0.78, lower_torso_h * 0.72, 0.055), shirt_color)
    add("LeftPantsFront", (-leg_x, upper_leg_y - 0.08, -limb_d / 2 - 0.035), (leg_w * 0.88, upper_leg_h * 0.90, 0.055), left_pants_color)
    add("RightPantsFront", (leg_x, upper_leg_y - 0.08, -limb_d / 2 - 0.035), (leg_w * 0.88, upper_leg_h * 0.90, 0.055), right_pants_color)
    add("LeftEye", (-head_size * 0.22, head_y + head_size * 0.08, -head_size / 2 - 0.03), (head_size * 0.12, head_size * 0.10, 0.045), (20, 25, 30))
    add("RightEye", (head_size * 0.22, head_y + head_size * 0.08, -head_size / 2 - 0.03), (head_size * 0.12, head_size * 0.10, 0.045), (20, 25, 30))
    add("Mouth", (0, head_y - head_size * 0.20, -head_size / 2 - 0.03), (head_size * 0.28, head_size * 0.055, 0.045), (90, 35, 35))

    hat_count = 0
    hair_count = 0
    for asset in avatar.get("assets", []):
        type_name = ((asset.get("assetType") or {}).get("name") or "")
        type_key = type_name.lower()
        asset_name = asset.get("name") or type_name or "Accessory"
        color = avatar_asset_color(asset)
        if type_key in {"hat", "hairaccessory"}:
            offset = (hat_count + hair_count) * 0.08
            if type_key == "hairaccessory":
                hair_count += 1
                add(f"Accessory_{asset_name}", (0, head_y + head_size * 0.17 + offset, head_size * 0.08),
                    (head_size * 1.12, head_size * 0.38, head_size * 1.12), color)
                add(f"Accessory_{asset_name}_Back", (0, head_y - head_size * 0.12, head_size * 0.45),
                    (head_size * 1.02, head_size * 0.82, head_size * 0.25), color)
            else:
                hat_count += 1
                add(f"Accessory_{asset_name}", (0, head_y + head_size * 0.56 + offset, 0),
                    (head_size * 1.10, head_size * 0.22, head_size * 1.10), color)
                add(f"Accessory_{asset_name}_Brim", (0, head_y + head_size * 0.42 + offset, -head_size * 0.48),
                    (head_size * 0.78, head_size * 0.08, head_size * 0.42), color)
        elif type_key == "faceaccessory":
            add(f"Accessory_{asset_name}_LeftLens", (-head_size * 0.23, head_y + head_size * 0.08, -head_size / 2 - 0.07),
                (head_size * 0.28, head_size * 0.18, 0.055), color)
            add(f"Accessory_{asset_name}_RightLens", (head_size * 0.23, head_y + head_size * 0.08, -head_size / 2 - 0.07),
                (head_size * 0.28, head_size * 0.18, 0.055), color)
            add(f"Accessory_{asset_name}_Bridge", (0, head_y + head_size * 0.08, -head_size / 2 - 0.075),
                (head_size * 0.18, head_size * 0.06, 0.06), color)
        elif type_key == "neckaccessory":
            add(f"Accessory_{asset_name}", (0, neck_y - 0.04, -body_d / 2 - 0.08),
                (torso_w * 0.70, 0.18, 0.10), color)
        elif type_key == "shoulderaccessory":
            add(f"Accessory_{asset_name}_Left", (-torso_w * 0.55, shoulder_y, 0),
                (0.34, 0.34, 0.34), color, shape=0)
            add(f"Accessory_{asset_name}_Right", (torso_w * 0.55, shoulder_y, 0),
                (0.34, 0.34, 0.34), color, shape=0)
        elif type_key == "frontaccessory":
            add(f"Accessory_{asset_name}", (0, upper_torso_y, -body_d / 2 - 0.12),
                (torso_w * 0.80, upper_torso_h * 0.55, 0.13), color)
        elif type_key == "backaccessory":
            add(f"Accessory_{asset_name}", (0, upper_torso_y + 0.05, body_d / 2 + 0.18),
                (torso_w * 0.92, upper_torso_h * 1.25, 0.20), color)
        elif type_key == "waistaccessory":
            add(f"Accessory_{asset_name}", (0, lower_torso_y - lower_torso_h * 0.45, -body_d / 2 - 0.08),
                (torso_w * 0.95, 0.18, 0.10), color)

    display_name = user.get("displayName") or user.get("name") or username
    base = f"roblox_avatar_{safe_stem(user.get('name') or username)}_{user_id}"
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = MODELS_DIR / f"{base}.rbxmx"
    lines = [
        '<roblox version="4">',
        f'<Item class="Model" referent="RBXROOT"><Properties><string name="Name">{xml_escape(display_name)} avatar</string></Properties>',
    ]
    for idx, part in enumerate(parts, 1):
        lines.append(avatar_part_xml(idx, *part))
    lines.append("</Item>")
    lines.append("</roblox>")
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path


def load_state():
    if not STATE_FILE.exists():
        return {"seen": {}, "last_settings": DEFAULT_SETTINGS.copy()}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    data.setdefault("seen", {})
    data.setdefault("last_settings", DEFAULT_SETTINGS.copy())
    return data


def save_state(state):
    BUILDS_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def model_id(path):
    stat = Path(path).stat()
    return f"{Path(path).name}|{stat.st_size}|{int(stat.st_mtime)}"


def list_model_files():
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()
    files = [p for p in MODELS_DIR.iterdir() if p.is_file() and p.suffix.lower() in MODEL_EXTS]
    files.sort(key=lambda p: (model_id(p) in state["seen"], -p.stat().st_mtime))
    result = []
    for path in files:
        stat = path.stat()
        result.append({
            "name": path.name,
            "size": stat.st_size,
            "mtime": int(stat.st_mtime),
            "new": model_id(path) not in state["seen"],
        })
    return result


def extract_zip_model(path):
    temp_dir = Path(tempfile.mkdtemp(prefix="build_model_zip_"))
    temp_root = temp_dir.resolve()
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            dest = (temp_dir / info.filename).resolve()
            if dest != temp_root and temp_root not in dest.parents:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, dest.open("wb") as out:
                shutil.copyfileobj(src, out)
    priority = {".glb": 0, ".gltf": 1, ".obj": 2, ".stl": 3, ".ply": 4, ".rbxlx": 5, ".rbxmx": 6}
    candidates = [
        p for p in temp_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in priority and "__macosx" not in str(p).lower()
    ]
    if not candidates:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise ValueError("zip did not contain a supported model (.glb, .gltf, .obj, .stl, .ply, .rbxlx, .rbxmx)")
    candidates.sort(key=lambda p: (priority[p.suffix.lower()], len(p.parts), p.name.lower()))
    return candidates[0], temp_dir


def resolve_model_source(path):
    path = Path(path)
    if path.suffix.lower() == ".zip":
        return extract_zip_model(path)
    return path, None


def write_build_files(blocks, out_dir, out_base, split_size):
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob(f"{out_base}_part*.build"):
        old.unlink()
    files = []
    items = []
    part = 1
    total = 0
    for i, block in enumerate(blocks, 1):
        if isinstance(block, dict):
            item = {
                "ID": i,
                "Transparency": color_transparency(block["color"]),
                "Anchored": True,
                "CanCollide": bool(block.get("can_collide", True)),
                "Color": color_hex(block["color"]),
                "CFrame": block["cframe"],
                "CastShadow": True,
                "Size": block["size"],
            }
        else:
            x, y, z, sx, sy, sz, color = block
            item = {
                "ID": i,
                "Transparency": color_transparency(color),
                "Anchored": True,
                "CanCollide": True,
                "Color": color_hex(color),
                "CFrame": [
                    clean((x + (sx - 1) * 0.5) * CELL[0]),
                    clean((y + (sy - 1) * 0.5) * CELL[1]),
                    clean((z + (sz - 1) * 0.5) * CELL[2]),
                    1, 0, 0, 0, 1, 0, 0, 0, 1,
                ],
                "CastShadow": True,
                "Size": [clean(sx * CELL[0]), clean(sy * CELL[1]), clean(sz * CELL[2])],
            }
        items.append(item)
        if len(items) == split_size:
            file_path = out_dir / f"{out_base}_part{part:03d}.build"
            file_path.write_text(json.dumps({"Data": {"GoldBlock": items}, "AutoBuild_Version": "v1"}, separators=(",", ":")), encoding="utf-8")
            total += len(items)
            files.append(file_path)
            items = []
            part += 1
    if items:
        file_path = out_dir / f"{out_base}_part{part:03d}.build"
        file_path.write_text(json.dumps({"Data": {"GoldBlock": items}, "AutoBuild_Version": "v1"}, separators=(",", ":")), encoding="utf-8")
        total += len(items)
        files.append(file_path)
    return files, total


def convert_model(input_path, settings=None, progress=print):
    global GLB, OUT_DIR, OUT_BASE, CELL, FIT_SHRINK, ROTATION_DEGREES, SURFACE_SLABS, SPLIT_SIZE, FACE_FILL, SMALL_DETAIL_BOOST, FILL_RADIUS, MERGE_TOLERANCE, SURFACE_OVERLAP, MERGE_TRIANGLE_QUADS, ROBLOX_PRIMITIVES, ROBLOX_SLOPE_DROP, ROBLOX_WEDGE_FILL, ROBLOX_TERRAIN_DETAIL
    ensure_pillow()
    settings = {**DEFAULT_SETTINGS, **(settings or {})}
    GLB = Path(input_path)
    OUT_BASE = f"{safe_stem(GLB.name)}_cell{settings['cell']}_shrink{settings['shrink']}"
    if settings.get("surface_slabs", True):
        OUT_BASE += "_triangles"
    OUT_DIR = BUILDS_DIR / OUT_BASE
    CELL = (float(settings["cell"]), float(settings["cell"]), float(settings["cell"]))
    FIT_SHRINK = max(0.05, float(settings["shrink"]))
    settings["shrink"] = FIT_SHRINK
    SPLIT_SIZE = max(1, int(settings["split_size"]))
    FACE_FILL = max(0.1, float(settings["face_fill"]))
    SMALL_DETAIL_BOOST = max(1.0, float(settings["small_detail_boost"]))
    FILL_RADIUS = max(0, int(settings["fill_radius"]))
    MERGE_TOLERANCE = max(0.0, float(settings["merge_tolerance"]))
    SURFACE_OVERLAP = max(1.0, float(settings["surface_overlap"]))
    MERGE_TRIANGLE_QUADS = bool(settings["merge_triangle_quads"])
    ROBLOX_PRIMITIVES = bool(settings["roblox_primitives"])
    ROBLOX_SLOPE_DROP = max(0.0, float(settings["roblox_slope_drop"]))
    ROBLOX_WEDGE_FILL = max(0.0, float(settings["roblox_wedge_fill"]))
    ROBLOX_TERRAIN_DETAIL = max(0.25, float(settings["roblox_terrain_detail"]))
    ROTATION_DEGREES = (float(settings["rot_x"]), float(settings["rot_y"]), float(settings["rot_z"]))
    SURFACE_SLABS = bool(settings["surface_slabs"])

    start = time.time()
    model_path, cleanup_dir = resolve_model_source(GLB)
    try:
        if model_path.suffix.lower() in ROBLOX_XML_EXTS:
            source_blocks = parse_roblox_studio_blocks(model_path)
            progress(f"parsed roblox parts={len(source_blocks)} source={model_path.name}")
            OUT_BASE = f"{safe_stem(GLB.name)}_roblox_parts_shrink{FIT_SHRINK}"
            OUT_DIR = BUILDS_DIR / OUT_BASE
            blocks, scale = fit_direct_blocks_to_build_area(source_blocks)
            progress(f"roblox optimized shrink={FIT_SHRINK:g} slope_drop={ROBLOX_SLOPE_DROP:g} wedge_fill={ROBLOX_WEDGE_FILL:g} terrain_detail={ROBLOX_TERRAIN_DETAIL:g} scale={scale:.6f}; direct blocks={len(blocks)}; writing .build files...")
            files, total = write_build_files(blocks, OUT_DIR, OUT_BASE, SPLIT_SIZE)
            seconds = time.time() - start
            progress(f"done blocks={total} files={len(files)} seconds={seconds:.1f}")
            state = load_state()
            state["seen"][model_id(GLB)] = {"name": GLB.name, "last_output": OUT_BASE}
            state["last_settings"] = settings
            save_state(state)
            return {
                "ok": True,
                "input": str(GLB),
                "source": str(model_path),
                "output_dir": str(OUT_DIR),
                "files": [{"name": p.name, "url": "/builds/" + p.relative_to(BUILDS_DIR).as_posix(), "blocks": None} for p in files],
                "blocks": total,
                "vertices": 0,
                "faces": len(source_blocks),
                "seconds": seconds,
                "settings": settings,
                "roblox_optimized": True,
            }

        verts, faces, materials = load_surface_model(model_path)
        if not verts or not faces:
            raise ValueError("model has no usable triangles")
        progress(f"parsed vertices={len(verts)} faces={len(faces)} source={model_path.name}")
        verts = [rotate_point(v) for v in verts]
        mn = [min(v[i] for v in verts) for i in range(3)]
        mx = [max(v[i] for v in verts) for i in range(3)]
        size = [mx[i] - mn[i] for i in range(3)]
        marker_min = [min(p[i] for p in MARKER_POINTS) for i in range(3)]
        marker_max = [max(p[i] for p in MARKER_POINTS) for i in range(3)]
        marker_center = [(marker_min[i] + marker_max[i]) * 0.5 for i in range(3)]
        scale = min(
            (marker_max[0] - marker_min[0] - CELL[0]) / max(size[0], 0.000001),
            (marker_max[2] - marker_min[2] - CELL[2]) / max(size[2], 0.000001),
        ) / FIT_SHRINK
        center = ((mn[0] + mx[0]) / 2, mn[1], (mn[2] + mx[2]) / 2)
        origin = (marker_center[0], marker_min[1], marker_center[2])

        def tx(v):
            return (
                (v[0] - center[0]) * scale + origin[0],
                (v[1] - center[1]) * scale + origin[1],
                (v[2] - center[2]) * scale + origin[2],
            )

        if SURFACE_SLABS:
            cores = cpu_worker_count()
            progress(f"scale={scale:.6f}; building triangle strips on {cores} CPU workers...")
            blocks = triangle_surface_blocks(verts, faces, materials, tx, progress=progress)
            progress(f"triangle strip blocks={len(blocks)}; writing .build files...")
            files, total = write_build_files(blocks, OUT_DIR, OUT_BASE, SPLIT_SIZE)
        else:
            tasks = [(tx(verts[face[0]]), tx(verts[face[1]]), tx(verts[face[2]]), face[3]) for face in faces]
            cores = cpu_worker_count()
            chunk_count = max(cores, cores * CHUNKS_PER_CORE)
            chunk_size = math.ceil(len(tasks) / chunk_count)
            chunks = [(tasks[i:i + chunk_size], CELL, min(CELL) / FACE_FILL, FILL_RADIUS) for i in range(0, len(tasks), chunk_size)]
            progress(f"scale={scale:.6f}; voxelizing on {cores} cores in {len(chunks)} chunks...")
            grid = {}
            with mp.Pool(processes=cores) as pool:
                for i, partial in enumerate(pool.imap_unordered(worker, chunks), 1):
                    grid.update(partial)
                    progress(f"chunk {i}/{len(chunks)} merged; voxels={len(grid)}")
            clipped = {}
            for key, color in grid.items():
                x, _, z = key
                wx = x * CELL[0]
                wz = z * CELL[2]
                if (
                    wx - CELL[0] * 0.5 >= marker_min[0]
                    and wx + CELL[0] * 0.5 <= marker_max[0]
                    and wz - CELL[2] * 0.5 >= marker_min[2]
                    and wz + CELL[2] * 0.5 <= marker_max[2]
                ):
                    clipped[key] = color
            merge_parts = split_grid_for_parallel_merge(clipped, cores * CHUNKS_PER_CORE)
            progress(f"clipped voxels={len(clipped)}; merging in {len(merge_parts)} slices...")
            blocks = []
            with mp.Pool(processes=cores) as pool:
                for i, partial_blocks in enumerate(pool.imap_unordered(merge_worker, merge_parts), 1):
                    blocks.extend(partial_blocks)
                    progress(f"merge slice {i}/{len(merge_parts)} done; blocks={len(blocks)}")
            files, total = write_build_files(blocks, OUT_DIR, OUT_BASE, SPLIT_SIZE)
        seconds = time.time() - start
        progress(f"done blocks={total} files={len(files)} seconds={seconds:.1f}")
        state = load_state()
        state["seen"][model_id(GLB)] = {"name": GLB.name, "last_output": OUT_BASE}
        state["last_settings"] = settings
        save_state(state)
        return {
            "ok": True,
            "input": str(GLB),
            "source": str(model_path),
            "output_dir": str(OUT_DIR),
            "files": [{"name": p.name, "url": "/builds/" + p.relative_to(BUILDS_DIR).as_posix(), "blocks": None} for p in files],
            "blocks": total,
            "vertices": len(verts),
            "faces": len(faces),
            "seconds": seconds,
            "settings": settings,
        }
    finally:
        if cleanup_dir:
            shutil.rmtree(cleanup_dir, ignore_errors=True)


def parse_content_disposition(value):
    result = {}
    for part in value.split(";"):
        part = part.strip()
        if "=" in part:
            key, raw = part.split("=", 1)
            result[key.lower()] = raw.strip().strip('"')
    return result


def parse_multipart(handler):
    content_type = handler.headers.get("Content-Type", "")
    marker = "boundary="
    if marker not in content_type:
        raise ValueError("expected multipart/form-data")
    boundary = content_type.split(marker, 1)[1].split(";", 1)[0].strip().strip('"').encode()
    length = int(handler.headers.get("Content-Length", "0"))
    body = handler.rfile.read(length)
    fields = {}
    files = {}
    for part in body.split(b"--" + boundary):
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        if b"\r\n\r\n" not in part:
            continue
        header_blob, data = part.split(b"\r\n\r\n", 1)
        headers = {}
        for raw_line in header_blob.decode("utf-8", errors="ignore").split("\r\n"):
            if ":" in raw_line:
                key, value = raw_line.split(":", 1)
                headers[key.lower()] = value.strip()
        disposition = parse_content_disposition(headers.get("content-disposition", ""))
        name = disposition.get("name")
        filename = disposition.get("filename")
        if not name:
            continue
        if data.endswith(b"\r\n"):
            data = data[:-2]
        if filename:
            files[name] = {"filename": Path(filename).name, "data": data}
        else:
            fields[name] = data.decode("utf-8", errors="ignore")
    return fields, files


def coerce_settings(raw):
    settings = DEFAULT_SETTINGS.copy()
    for key, value in (raw or {}).items():
        if key not in settings:
            continue
        if isinstance(settings[key], bool):
            settings[key] = bool(value)
        elif isinstance(settings[key], int):
            settings[key] = int(value)
        else:
            settings[key] = float(value)
    return settings


def load_settings_text(text):
    text = text or "{}"
    candidates = [text]
    stripped = text.strip()
    if stripped.startswith('"') and stripped.endswith('"'):
        candidates.append(stripped[1:-1])
    candidates.append(stripped.replace('\\"', '"'))
    try:
        candidates.append(stripped.encode("utf-8").decode("unicode_escape"))
    except Exception:
        pass
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except Exception:
            continue
    raise ValueError("settings must be JSON")


def save_upload(upload):
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    suffix = Path(upload["filename"]).suffix.lower()
    if suffix not in MODEL_EXTS:
        raise ValueError("upload must be .zip, .glb, .gltf, .obj, .stl, .ply, .rbxlx, or .rbxmx")
    base = safe_stem(upload["filename"])
    dest = MODELS_DIR / f"{base}{suffix}"
    if dest.exists():
        dest = MODELS_DIR / f"{base}_{int(time.time())}{suffix}"
    dest.write_bytes(upload["data"])
    return dest


def build_url_path(url_path):
    rel = unquote(url_path[len("/builds/"):]).replace("/", os.sep)
    target = (BUILDS_DIR / rel).resolve()
    root = BUILDS_DIR.resolve()
    if target != root and root not in target.parents:
        raise ValueError("bad build path")
    return target


def build_file_from_param(value):
    value = (value or "").strip()
    if not value:
        raise ValueError("missing build file")
    if value.startswith("/builds/"):
        path = build_url_path(value)
    else:
        path = build_url_path("/builds/" + value.lstrip("/\\"))
    if path.suffix.lower() != ".build":
        raise ValueError("preview target must be a .build file")
    return path


def build_files_from_params(values):
    if isinstance(values, str):
        values = [values]
    paths = []
    for raw in values:
        for value in str(raw or "").split("||"):
            value = value.strip()
            if value:
                paths.append(build_file_from_param(value))
    if not paths:
        raise ValueError("missing build file")
    return paths


def render_cache_key(paths):
    key = []
    for path in paths:
        path = Path(path).resolve()
        stat = path.stat()
        key.append((str(path), stat.st_mtime_ns, stat.st_size))
    return tuple(key)


def get_cached_renderer(paths):
    ensure_render_deps()
    from build_renderer import BuildRenderer
    key = render_cache_key(paths)
    renderer = RENDER_CACHE.get(key)
    if renderer is not None:
        if key in RENDER_CACHE_ORDER:
            RENDER_CACHE_ORDER.remove(key)
        RENDER_CACHE_ORDER.append(key)
        return renderer

    renderer = BuildRenderer([str(path) for path in paths])
    if not renderer.valid:
        renderer.cleanup()
        raise ValueError("renderer could not parse this build")
    RENDER_CACHE[key] = renderer
    RENDER_CACHE_ORDER.append(key)
    while len(RENDER_CACHE_ORDER) > RENDER_CACHE_MAX:
        old_key = RENDER_CACHE_ORDER.pop(0)
        old_renderer = RENDER_CACHE.pop(old_key, None)
        if old_renderer is not None:
            try:
                old_renderer.cleanup()
            except Exception:
                pass
    return renderer


def truthy(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def render_build_png(file_value, azimuth=45.0, elevation=28.0, distance=1.35, force_cpu=False):
    paths = build_files_from_params(file_value)
    with RENDER_CACHE_LOCK:
        renderer = get_cached_renderer(paths)
        image = renderer.render(float(azimuth), float(elevation), float(distance), bool(force_cpu))
        if image is None:
            raise ValueError("renderer returned no image")
        return image.getvalue()


def openapi_spec(host="127.0.0.1", port=8765):
    base_url = f"http://{host}:{port}"
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "3D Build Converter API",
            "version": API_VERSION,
            "description": "Local portable API for converting 3D/Roblox files into .build files and rendering build previews.",
        },
        "servers": [{"url": base_url}],
        "paths": {
            "/api/health": {
                "get": {
                    "summary": "Health check",
                    "responses": {"200": {"description": "Server status"}},
                }
            },
            "/api/models": {
                "get": {
                    "summary": "List models in 3d_models and default settings",
                    "responses": {"200": {"description": "Model list and defaults"}},
                }
            },
            "/api/convert": {
                "post": {
                    "summary": "Convert a model to .build files",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "multipart/form-data": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "model": {"type": "string", "description": "Filename from the 3d_models folder"},
                                        "file": {"type": "string", "format": "binary", "description": "Model or ZIP upload"},
                                        "settings": {"type": "string", "description": "JSON settings object"},
                                    },
                                }
                            }
                        },
                    },
                    "responses": {"200": {"description": "Conversion result and output file URLs"}},
                }
            },
            "/api/render-build": {
                "get": {
                    "summary": "Render one or more .build files to PNG",
                    "parameters": [
                        {"name": "file", "in": "query", "schema": {"type": "array", "items": {"type": "string"}}, "description": "Repeat this query parameter for multiple .build files"},
                        {"name": "az", "in": "query", "schema": {"type": "number", "default": 45}},
                        {"name": "el", "in": "query", "schema": {"type": "number", "default": 28}},
                        {"name": "dist", "in": "query", "schema": {"type": "number", "default": 1.15}},
                        {"name": "cpu", "in": "query", "schema": {"type": "boolean", "default": False}, "description": "Force multi-core CPU preview"},
                    ],
                    "responses": {"200": {"description": "PNG image", "content": {"image/png": {}}}},
                }
            },
            "/api/roblox-user": {
                "post": {
                    "summary": "Create a Roblox avatar model ZIP in 3d_models",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "multipart/form-data": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"username": {"type": "string"}},
                                    "required": ["username"],
                                }
                            }
                        },
                    },
                    "responses": {"200": {"description": "Created avatar model"}},
                }
            },
            "/builds/{path}": {
                "get": {
                    "summary": "Download a generated .build file",
                    "parameters": [{"name": "path", "in": "path", "required": True, "schema": {"type": "string"}}],
                    "responses": {"200": {"description": "Generated build file"}},
                }
            },
        },
    }


class BuildHandler(BaseHTTPRequestHandler):
    server_version = "BuildTool/1.0"

    def log_message(self, fmt, *args):
        print(fmt % args)

    def send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")

    def send_json(self, data, status=200):
        raw = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def send_text(self, text, status=200, content_type="text/plain; charset=utf-8"):
        raw = str(text).encode("utf-8")
        self.send_response(status)
        self.send_cors_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def send_file(self, path, download=False):
        path = Path(path)
        if not path.exists() or not path.is_file():
            self.send_error(404)
            return
        raw = path.read_bytes()
        self.send_response(200)
        self.send_cors_headers()
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(raw)))
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.end_headers()
        self.wfile.write(raw)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_cors_headers()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self.send_file(ROOT / "index.html")
        elif parsed.path in ("/api", "/api/docs"):
            self.send_file(ROOT / "API_README.txt")
        elif parsed.path == "/api/health":
            self.send_json({
                "ok": True,
                "api_version": API_VERSION,
                "server": self.server_version,
                "models_dir": str(MODELS_DIR),
                "builds_dir": str(BUILDS_DIR),
                "defaults": DEFAULT_SETTINGS,
            })
        elif parsed.path == "/api/openapi.json":
            host, port = self.server.server_address
            self.send_json(openapi_spec(host, port))
        elif parsed.path == "/api/models":
            self.send_json({"ok": True, "models": list_model_files(), "defaults": DEFAULT_SETTINGS})
        elif parsed.path == "/api/render-build":
            try:
                query = parse_qs(parsed.query)
                raw = render_build_png(
                    query.get("file", [""]),
                    query.get("az", ["45"])[0],
                    query.get("el", ["28"])[0],
                    query.get("dist", ["1.15"])[0],
                    truthy(query.get("cpu", ["0"])[0]),
                )
                self.send_response(200)
                self.send_cors_headers()
                self.send_header("Content-Type", "image/png")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except Exception as exc:
                self.send_json({"ok": False, "error": str(exc)}, status=500)
        elif parsed.path.startswith("/builds/"):
            try:
                self.send_file(build_url_path(parsed.path), download=True)
            except Exception:
                self.send_error(400)
        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path not in ("/api/convert", "/api/roblox-user"):
            self.send_error(404)
            return
        try:
            fields, files = parse_multipart(self)
            if parsed.path == "/api/roblox-user":
                output = create_roblox_avatar_model(fields.get("username", ""))
                self.send_json({
                    "ok": True,
                    "kind": "avatar",
                    "output": output.name,
                    "output_path": str(output),
                    "models": list_model_files(),
                })
                return
            raw_settings = load_settings_text(fields.get("settings", "{}"))
            settings = coerce_settings(raw_settings)
            if "file" in files:
                model_path = save_upload(files["file"])
            else:
                name = Path(fields.get("model", "")).name
                model_path = (MODELS_DIR / name).resolve()
                if model_path.parent != MODELS_DIR.resolve() or not model_path.exists():
                    raise ValueError("pick or upload a model first")
            logs = []
            result = convert_model(model_path, settings, progress=lambda line: logs.append(str(line)))
            result["logs"] = logs
            result["models"] = list_model_files()
            self.send_json(result)
        except Exception as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=500)


class ExclusiveThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def ask(prompt, default=None, cast=str):
    suffix = f" [{default}]" if default is not None else ""
    raw = input(f"{prompt}{suffix}: ").strip()
    if not raw and default is not None:
        return default
    try:
        return cast(raw)
    except Exception:
        print("Bad input, using default.")
        return default


def ask_yes(prompt, default=True):
    raw = input(f"{prompt} (y/n) [{'y' if default else 'n'}]: ").strip().lower()
    if not raw:
        return default
    return raw.startswith("y")


def cli_settings():
    state = load_state()
    last = {**DEFAULT_SETTINGS, **state.get("last_settings", {})}
    if ask_yes("Use same settings as last time", True):
        return last
    return {
        "cell": ask("Block/cell size", last["cell"], float),
        "shrink": ask("Fit shrink, lower is bigger, 5 is default, Roblox uses this too", last["shrink"], float),
        "split_size": ask("Max blocks per .build file", last["split_size"], int),
        "surface_slabs": ask_yes("Smooth triangle strips", last["surface_slabs"]),
        "face_fill": ask("Face fill density", last["face_fill"], float),
        "small_detail_boost": ask("Small detail boost, 1 normal, 2-3 fills hands/fingers more", last["small_detail_boost"], float),
        "fill_radius": ask("Fill radius for voxel mode", last["fill_radius"], int),
        "merge_tolerance": ask("Merge color tolerance", last["merge_tolerance"], float),
        "surface_overlap": ask("Surface overlap", last["surface_overlap"], float),
        "roblox_slope_drop": ask("Roblox slope drop, 0.5 normal, 1 lower", last["roblox_slope_drop"], float),
        "roblox_wedge_fill": ask("Roblox wedge fill, 2 default, 3+ stronger, 0 old width", last["roblox_wedge_fill"], float),
        "roblox_terrain_detail": ask("Roblox terrain detail, 1-3 smooth/low-block, 4+ adds solid under-fill", last["roblox_terrain_detail"], float),
        "merge_triangle_quads": ask_yes("Merge matching triangle pairs into square blocks", last["merge_triangle_quads"]),
        "rot_x": ask("Rotate X degrees", last["rot_x"], float),
        "rot_y": ask("Rotate Y degrees", last["rot_y"], float),
        "rot_z": ask("Rotate Z degrees", last["rot_z"], float),
    }


def cli_mode():
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    BUILDS_DIR.mkdir(parents=True, exist_ok=True)
    if ask_yes("Create a Roblox avatar model first", False):
        username = ask("Roblox username", "", str)
        output = create_roblox_avatar_model(username)
        print(f"Saved Roblox avatar model: {output}")
    models = list_model_files()
    if not models:
        print(f"No models found. Drop .zip/.glb/.gltf/.obj/.stl/.ply/.rbxlx/.rbxmx files into:\n{MODELS_DIR}")
        input("Press Enter to exit...")
        return
    print("\nModels:")
    for i, item in enumerate(models, 1):
        tag = "NEW" if item["new"] else "seen"
        print(f"{i:>2}. [{tag}] {item['name']} ({item['size'] / 1024 / 1024:.1f} MB)")
    choice = ask("Pick model number", 1, int)
    if choice < 1 or choice > len(models):
        print("Invalid model.")
        return
    settings = cli_settings()
    result = convert_model(MODELS_DIR / models[choice - 1]["name"], settings)
    print("\nDone.")
    print(f"Blocks: {result['blocks']}")
    print(f"Output: {result['output_dir']}")
    input("Press Enter to exit...")


def run_server(host, port):
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    BUILDS_DIR.mkdir(parents=True, exist_ok=True)
    server = ExclusiveThreadingHTTPServer((host, port), BuildHandler)
    display_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    print(f"Website running: http://{display_host}:{port}/")
    print(f"API docs: http://{display_host}:{port}/api/docs")
    print(f"OpenAPI JSON: http://{display_host}:{port}/api/openapi.json")
    print(f"Drop files into: {MODELS_DIR}")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("Warning: this server is listening beyond localhost. Only use that on a trusted network.")
    print("Press Ctrl+C to stop.")
    server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description="Portable 3D model to .build converter")
    parser.add_argument("--host", default=os.environ.get("BUILD_TOOL_HOST", "127.0.0.1"), help="server bind host, default 127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--cli", action="store_true", help="run the interactive Python tool")
    parser.add_argument("--convert", type=Path, help="convert one model or zip directly")
    parser.add_argument("--roblox-user", help="create a Roblox avatar model in 3d_models")
    parser.add_argument("--cell", type=float, default=DEFAULT_SETTINGS["cell"])
    parser.add_argument("--shrink", type=float, default=DEFAULT_SETTINGS["shrink"])
    parser.add_argument("--split-size", type=int, default=DEFAULT_SETTINGS["split_size"])
    parser.add_argument("--surface-overlap", type=float, default=DEFAULT_SETTINGS["surface_overlap"])
    parser.add_argument("--roblox-slope-drop", type=float, default=DEFAULT_SETTINGS["roblox_slope_drop"])
    parser.add_argument("--roblox-wedge-fill", type=float, default=DEFAULT_SETTINGS["roblox_wedge_fill"])
    parser.add_argument("--roblox-terrain-detail", type=float, default=DEFAULT_SETTINGS["roblox_terrain_detail"])
    parser.add_argument("--small-detail-boost", type=float, default=DEFAULT_SETTINGS["small_detail_boost"])
    parser.add_argument("--rot-x", type=float, default=0.0)
    parser.add_argument("--rot-y", type=float, default=0.0)
    parser.add_argument("--rot-z", type=float, default=0.0)
    parser.add_argument("--surface-slabs", action=argparse.BooleanOptionalAction, default=DEFAULT_SETTINGS["surface_slabs"])
    parser.add_argument("--merge-triangle-quads", action=argparse.BooleanOptionalAction, default=DEFAULT_SETTINGS["merge_triangle_quads"])
    args = parser.parse_args()

    if args.cli:
        cli_mode()
        return
    if args.roblox_user:
        output = create_roblox_avatar_model(args.roblox_user)
        print(output)
        return
    if args.convert:
        settings = {
            **DEFAULT_SETTINGS,
            "cell": args.cell,
            "shrink": args.shrink,
            "split_size": args.split_size,
            "surface_overlap": args.surface_overlap,
            "roblox_slope_drop": args.roblox_slope_drop,
            "roblox_wedge_fill": args.roblox_wedge_fill,
            "roblox_terrain_detail": args.roblox_terrain_detail,
            "small_detail_boost": args.small_detail_boost,
            "rot_x": args.rot_x,
            "rot_y": args.rot_y,
            "rot_z": args.rot_z,
            "surface_slabs": args.surface_slabs,
            "merge_triangle_quads": args.merge_triangle_quads,
        }
        result = convert_model(args.convert, settings)
        print(json.dumps(result, indent=2))
        return
    run_server(args.host, args.port)


if __name__ == "__main__":
    mp.freeze_support()
    main()
