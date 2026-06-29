import argparse
import base64
import io
import json
import math
import mimetypes
import multiprocessing as mp
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

PINNED_PILLOW_VERSION = "12.2.0"

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
MODEL_EXTS = {".glb", ".gltf", ".obj", ".stl", ".ply", ".zip"}


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


def quad_block_from_pair(verts, face_a, face_b, tx):
    ids = list(dict.fromkeys([face_a[0], face_a[1], face_a[2], face_b[0], face_b[1], face_b[2]]))
    if len(ids) != 4:
        return None
    pts = [tx(verts[i]) for i in ids]
    n1 = face_normal([tx(verts[face_a[0]]), tx(verts[face_a[1]]), tx(verts[face_a[2]])])
    n2 = face_normal([tx(verts[face_b[0]]), tx(verts[face_b[1]]), tx(verts[face_b[2]])])
    if abs(vec_dot(n1, n2)) < QUAD_NORMAL_DOT:
        return None
    normal = vec_norm(vec_add(n1, n2))
    if vec_len(normal) < 0.001:
        normal = n1
    ordered = ordered_quad_points(pts, normal)
    edge_lengths = [vec_len(vec_sub(ordered[(i + 1) % 4], ordered[i])) for i in range(4)]
    if min(edge_lengths) < 0.001:
        return None
    width = (edge_lengths[0] + edge_lengths[2]) * 0.5
    height = (edge_lengths[1] + edge_lengths[3]) * 0.5
    if abs(edge_lengths[0] - edge_lengths[2]) > max(width, 0.001) * 0.25:
        return None
    if abs(edge_lengths[1] - edge_lengths[3]) > max(height, 0.001) * 0.25:
        return None
    u_axis = vec_norm(vec_sub(ordered[1], ordered[0]))
    v_axis = vec_norm(vec_sub(ordered[2], ordered[1]))
    if abs(vec_dot(u_axis, v_axis)) > 0.15:
        return None
    normal = vec_norm(vec_cross(u_axis, v_axis))
    center = (
        sum(p[0] for p in ordered) / 4,
        sum(p[1] for p in ordered) / 4,
        sum(p[2] for p in ordered) / 4,
    )
    overlap = max(1.0, SURFACE_OVERLAP)
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


def triangle_surface_blocks(verts, faces, materials, tx):
    if not MERGE_TRIANGLE_QUADS:
        return triangle_slab_blocks(verts, faces, materials, tx)
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
                    block = quad_block_from_pair(verts, face, other, tx)
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
    blocks.extend(triangle_slab_blocks(verts, leftovers, materials, tx))
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
    "face_fill": 2.0,
    "small_detail_boost": 1.0,
    "fill_radius": 1,
    "merge_tolerance": 12.0,
    "surface_overlap": 1.08,
    "merge_triangle_quads": False,
    "rot_x": 0.0,
    "rot_y": 0.0,
    "rot_z": 0.0,
}


def safe_stem(name):
    stem = Path(name).stem
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in stem).strip("_")
    return cleaned or "model"


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
    priority = {".glb": 0, ".gltf": 1, ".obj": 2, ".stl": 3, ".ply": 4}
    candidates = [
        p for p in temp_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in priority and "__macosx" not in str(p).lower()
    ]
    if not candidates:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise ValueError("zip did not contain a supported model (.glb, .gltf, .obj, .stl, .ply)")
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
                "CanCollide": True,
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
    global GLB, OUT_DIR, OUT_BASE, CELL, FIT_SHRINK, ROTATION_DEGREES, SURFACE_SLABS, SPLIT_SIZE, FACE_FILL, SMALL_DETAIL_BOOST, FILL_RADIUS, MERGE_TOLERANCE, SURFACE_OVERLAP, MERGE_TRIANGLE_QUADS
    ensure_pillow()
    settings = {**DEFAULT_SETTINGS, **(settings or {})}
    GLB = Path(input_path)
    OUT_BASE = f"{safe_stem(GLB.name)}_cell{settings['cell']}_shrink{settings['shrink']}"
    if settings.get("surface_slabs", True):
        OUT_BASE += "_triangles"
    OUT_DIR = BUILDS_DIR / OUT_BASE
    CELL = (float(settings["cell"]), float(settings["cell"]), float(settings["cell"]))
    FIT_SHRINK = float(settings["shrink"])
    SPLIT_SIZE = max(1, int(settings["split_size"]))
    FACE_FILL = max(0.1, float(settings["face_fill"]))
    SMALL_DETAIL_BOOST = max(1.0, float(settings["small_detail_boost"]))
    FILL_RADIUS = max(0, int(settings["fill_radius"]))
    MERGE_TOLERANCE = max(0.0, float(settings["merge_tolerance"]))
    SURFACE_OVERLAP = max(1.0, float(settings["surface_overlap"]))
    MERGE_TRIANGLE_QUADS = bool(settings["merge_triangle_quads"])
    ROTATION_DEGREES = (float(settings["rot_x"]), float(settings["rot_y"]), float(settings["rot_z"]))
    SURFACE_SLABS = bool(settings["surface_slabs"])

    start = time.time()
    model_path, cleanup_dir = resolve_model_source(GLB)
    try:
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
            progress(f"scale={scale:.6f}; building triangle strips...")
            blocks = triangle_surface_blocks(verts, faces, materials, tx)
            progress(f"triangle strip blocks={len(blocks)}; writing .build files...")
            files, total = write_build_files(blocks, OUT_DIR, OUT_BASE, SPLIT_SIZE)
        else:
            tasks = [(tx(verts[face[0]]), tx(verts[face[1]]), tx(verts[face[2]]), face[3]) for face in faces]
            cores = os.cpu_count() or 4
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
        raise ValueError("upload must be .zip, .glb, .gltf, .obj, .stl, or .ply")
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


class BuildHandler(BaseHTTPRequestHandler):
    server_version = "BuildTool/1.0"

    def log_message(self, fmt, *args):
        print(fmt % args)

    def send_json(self, data, status=200):
        raw = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
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
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(raw)))
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self.send_file(ROOT / "index.html")
        elif parsed.path == "/api/models":
            self.send_json({"ok": True, "models": list_model_files(), "defaults": DEFAULT_SETTINGS})
        elif parsed.path.startswith("/builds/"):
            try:
                self.send_file(build_url_path(parsed.path), download=True)
            except Exception:
                self.send_error(400)
        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/convert":
            self.send_error(404)
            return
        try:
            fields, files = parse_multipart(self)
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
        "shrink": ask("Fit shrink, 1 biggest, 5 is 5x smaller", last["shrink"], float),
        "split_size": ask("Max blocks per .build file", last["split_size"], int),
        "surface_slabs": ask_yes("Smooth triangle strips", last["surface_slabs"]),
        "face_fill": ask("Face fill density", last["face_fill"], float),
        "small_detail_boost": ask("Small detail boost, 1 normal, 2-3 fills hands/fingers more", last["small_detail_boost"], float),
        "fill_radius": ask("Fill radius for voxel mode", last["fill_radius"], int),
        "merge_tolerance": ask("Merge color tolerance", last["merge_tolerance"], float),
        "surface_overlap": ask("Surface overlap", last["surface_overlap"], float),
        "merge_triangle_quads": ask_yes("Merge matching triangle pairs into square blocks", last["merge_triangle_quads"]),
        "rot_x": ask("Rotate X degrees", last["rot_x"], float),
        "rot_y": ask("Rotate Y degrees", last["rot_y"], float),
        "rot_z": ask("Rotate Z degrees", last["rot_z"], float),
    }


def cli_mode():
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    BUILDS_DIR.mkdir(parents=True, exist_ok=True)
    models = list_model_files()
    if not models:
        print(f"No models found. Drop .zip/.glb/.gltf/.obj files into:\n{MODELS_DIR}")
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


def run_server(port):
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    BUILDS_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", port), BuildHandler)
    print(f"Website running: http://127.0.0.1:{port}/")
    print(f"Drop files into: {MODELS_DIR}")
    print("Press Ctrl+C to stop.")
    server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description="Portable 3D model to .build converter")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--cli", action="store_true", help="run the interactive Python tool")
    parser.add_argument("--convert", type=Path, help="convert one model or zip directly")
    parser.add_argument("--cell", type=float, default=DEFAULT_SETTINGS["cell"])
    parser.add_argument("--shrink", type=float, default=DEFAULT_SETTINGS["shrink"])
    parser.add_argument("--split-size", type=int, default=DEFAULT_SETTINGS["split_size"])
    parser.add_argument("--surface-overlap", type=float, default=DEFAULT_SETTINGS["surface_overlap"])
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
    if args.convert:
        settings = {
            **DEFAULT_SETTINGS,
            "cell": args.cell,
            "shrink": args.shrink,
            "split_size": args.split_size,
            "surface_overlap": args.surface_overlap,
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
    run_server(args.port)


if __name__ == "__main__":
    mp.freeze_support()
    main()
