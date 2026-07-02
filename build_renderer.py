import json
import math
import multiprocessing as mp
import os
import numpy as np
np.infty = np.inf
import trimesh
import pyrender
from PIL import Image, ImageDraw

PREVIEW_COORD_LIMIT = 1_000_000.0
CPU_PREVIEW_BLOCK_LIMIT = 120000
CPU_RENDER_PARALLEL_MIN_BLOCKS = 4000

CPU_FACES = (
    (0, 3, 2, 1),
    (4, 5, 6, 7),
    (0, 1, 5, 4),
    (3, 7, 6, 2),
    (0, 4, 7, 3),
    (1, 2, 6, 5),
)
CPU_FACE_SHADES = (0.72, 1.02, 0.78, 1.08, 0.88, 0.94)


def cpu_worker_count():
    return max(1, os.cpu_count() or 4)


def chunk_list(items, chunk_count):
    if not items:
        return []
    chunk_count = max(1, min(int(chunk_count), len(items)))
    chunk_size = math.ceil(len(items) / chunk_count)
    return [items[i:i + chunk_size] for i in range(0, len(items), chunk_size)]


def cpu_preview_polygons(args):
    instances, eye, right, up, forward, width, height, focal = args
    eye = np.array(eye, dtype=float)
    right = np.array(right, dtype=float)
    up = np.array(up, dtype=float)
    forward = np.array(forward, dtype=float)
    polygons = []

    for b in instances:
        transparency = b.get('transparency', 0.0)
        if transparency >= 0.99:
            continue
        sx, sy, sz = b['size']['x'], b['size']['y'], b['size']['z']
        dx, dy, dz = sx / 2.0, sy / 2.0, sz / 2.0
        local_verts = np.array([
            [-dx, -dy, -dz], [ dx, -dy, -dz], [ dx,  dy, -dz], [-dx,  dy, -dz],
            [-dx, -dy,  dz], [ dx, -dy,  dz], [ dx,  dy,  dz], [-dx,  dy,  dz]
        ], dtype=float)

        cf = b.get('cframe')
        if cf and len(cf) >= 12:
            px, py, pz = cf[0:3]
            R = np.array([cf[3:6], cf[6:9], cf[9:12]], dtype=float)
            verts = np.dot(local_verts, R.T) + np.array([px, py, pz], dtype=float)
        else:
            pos = b['position']
            verts = local_verts + np.array([pos['x'], pos['y'], pos['z']], dtype=float)

        camera = verts - eye
        x = np.dot(camera, right)
        y = np.dot(camera, up)
        z = np.dot(camera, forward)
        if np.max(z) <= 0.05:
            continue
        z = np.maximum(z, 0.05)
        screen = np.column_stack((
            width * 0.5 + (x / z) * focal,
            height * 0.5 - (y / z) * focal,
        ))
        if (np.max(screen[:, 0]) < -width or np.min(screen[:, 0]) > width * 2 or
            np.max(screen[:, 1]) < -height or np.min(screen[:, 1]) > height * 2):
            continue

        base = np.array([
            int(max(0, min(255, b['color']['r'] * 255))),
            int(max(0, min(255, b['color']['g'] * 255))),
            int(max(0, min(255, b['color']['b'] * 255))),
            int(max(20, min(255, (1.0 - transparency) * 255))),
        ])

        for i, face in enumerate(CPU_FACES):
            pts3 = verts[list(face)]
            normal = np.cross(pts3[1] - pts3[0], pts3[2] - pts3[0])
            center = pts3.mean(axis=0)
            if np.dot(normal, eye - center) <= 0:
                continue
            shade = CPU_FACE_SHADES[i]
            color = (
                int(max(0, min(255, base[0] * shade))),
                int(max(0, min(255, base[1] * shade))),
                int(max(0, min(255, base[2] * shade))),
                int(base[3]),
            )
            pts2 = [(float(screen[j, 0]), float(screen[j, 1])) for j in face]
            depth = float(np.mean(z[list(face)]))
            polygons.append((depth, pts2, color))

    return polygons

def finite_number(value):
    try:
        value = float(value)
    except Exception:
        return False
    return math.isfinite(value)

def sane_vector3(vec, limit=PREVIEW_COORD_LIMIT):
    return (
        finite_number(vec.get('x')) and
        finite_number(vec.get('y')) and
        finite_number(vec.get('z')) and
        abs(float(vec.get('x'))) <= limit and
        abs(float(vec.get('y'))) <= limit and
        abs(float(vec.get('z'))) <= limit
    )

def sane_cframe(cframe, limit=PREVIEW_COORD_LIMIT):
    if not isinstance(cframe, list) or len(cframe) < 12:
        return False
    return all(finite_number(v) and abs(float(v)) <= limit for v in cframe[:3])

def parse_vector3(val):
    if isinstance(val, list) and len(val) >= 3:
        return {'x': float(val[0]), 'y': float(val[1]), 'z': float(val[2])}
    if isinstance(val, str):
        val = val.replace('(', '').replace(')', '')
        values = [float(v.strip()) if v.strip() else 0.0 for v in val.split(',')]
        return {
            'x': values[0] if len(values) > 0 else 0.0,
            'y': values[1] if len(values) > 1 else 0.0,
            'z': values[2] if len(values) > 2 else 0.0
        }
    elif isinstance(val, dict):
        return {
            'x': float(val.get('X', val.get('x', 0.0))),
            'y': float(val.get('Y', val.get('y', 0.0))),
            'z': float(val.get('Z', val.get('z', 0.0)))
        }
    return {'x': 0.0, 'y': 0.0, 'z': 0.0}

def to_linear(c):
    return c ** 2.2

def parse_color(val):
    if isinstance(val, str):
        val_clean = val.replace('#', '').strip()
        if len(val_clean) == 6 and all(c in '0123456789abcdefABCDEF' for c in val_clean):
            return {
                'r': to_linear(int(val_clean[0:2], 16) / 255.0),
                'g': to_linear(int(val_clean[2:4], 16) / 255.0),
                'b': to_linear(int(val_clean[4:6], 16) / 255.0)
            }
        val = val.replace('(', '').replace(')', '')
        values = []
        for v in val.split(','):
            v = v.strip()
            try:
                values.append(float(v))
            except ValueError:
                values.append(0.5)
        return {
            'r': to_linear(max(0.0, min(1.0, values[0])) if len(values) > 0 else 0.5),
            'g': to_linear(max(0.0, min(1.0, values[1])) if len(values) > 1 else 0.5),
            'b': to_linear(max(0.0, min(1.0, values[2])) if len(values) > 2 else 0.5)
        }
    elif isinstance(val, dict):
        return {
            'r': to_linear(max(0.0, min(1.0, float(val.get('R', val.get('r', 0.5)))))),
            'g': to_linear(max(0.0, min(1.0, float(val.get('G', val.get('g', 0.5)))))),
            'b': to_linear(max(0.0, min(1.0, float(val.get('B', val.get('b', 0.5))))))
        }
    return {'r': 0.5, 'g': 0.5, 'b': 0.5}

def create_complex_block_mesh(b):
    block_type = b.get('blockType', 'Normal')
    sx, sy, sz = b['size']['x'], b['size']['y'], b['size']['z']
    
    if block_type == 'Piston':
        meshes = []
        mvalues = b.get('mvalues', {})
        extend_len = float(mvalues.get('ExtendLength', 0.0))
        
        radius = min(sx, sz) / 2.0
        
        # Base outer (Yellow)
        base_outer = trimesh.creation.cylinder(radius=radius, segment=[[0, -sy/2.0, 0], [0, sy/2.0, 0]])
        base_outer.visual.face_colors = [255, 200, 0, 255]
        meshes.append(base_outer)
        
        # Base inner (Gray)
        base_inner = trimesh.creation.cylinder(radius=radius - 0.2, segment=[[0, -sy/2.0, 0], [0, sy/2.0 + 0.01, 0]])
        base_inner.visual.face_colors = [120, 120, 120, 255]
        meshes.append(base_inner)
        
        if extend_len > 0.01:
            arm_radius = radius * 0.4
            arm_start = sy/2.0
            arm_end = sy/2.0 + extend_len
            arm = trimesh.creation.cylinder(radius=arm_radius, segment=[[0, arm_start, 0], [0, arm_end, 0]])
            arm.visual.face_colors = [150, 150, 150, 255]
            meshes.append(arm)
            
            head_radius = radius * 0.9
            head_start = arm_end
            head_end = arm_end + sy
            head = trimesh.creation.cylinder(radius=head_radius, segment=[[0, head_start, 0], [0, head_end, 0]])
            head.visual.face_colors = [140, 140, 140, 255]
            meshes.append(head)
        else:
            head_radius = radius * 0.9
            head_start = sy/2.0
            head_end = sy/2.0 + sy
            head = trimesh.creation.cylinder(radius=head_radius, segment=[[0, head_start, 0], [0, head_end, 0]])
            head.visual.face_colors = [140, 140, 140, 255]
            meshes.append(head)
            
        combined = trimesh.util.concatenate(meshes)
    else:
        # Fallback
        combined = trimesh.creation.box(extents=(sx, sy, sz))
        r, g, b_col = int(b['color']['r']*255), int(b['color']['g']*255), int(b['color']['b']*255)
        combined.visual.face_colors = [r, g, b_col, 255]

    # Transform
    matrix = np.eye(4)
    cf = b.get('cframe')
    if cf and len(cf) >= 12:
        px, py, pz = cf[0:3]
        R = np.array([cf[3:6], cf[6:9], cf[9:12]])
        matrix[0:3, 0:3] = R
        matrix[0:3, 3] = [px, py, pz]
    else:
        px, py, pz = b['position']['x'], b['position']['y'], b['position']['z']
        matrix[0:3, 3] = [px, py, pz]
        rot = b.get('rotation')
        if rot:
            rx = math.radians(rot['x'])
            ry = math.radians(rot['y'])
            rz = math.radians(rot['z'])
            rot_matrix = trimesh.transformations.euler_matrix(rx, ry, rz, axes='rxyz')
            matrix[0:3, 0:3] = rot_matrix[0:3, 0:3]
        
    combined.apply_transform(matrix)
    return combined

class BuildRenderer:
    def __init__(self, file_path):
        self.valid = False
        self.gpu_error = None
        self.renderer = None
        self.scene = None
        self.cam_node = None
        self.dl_node = None
        self.block_instances = []
        file_paths = file_path if isinstance(file_path, (list, tuple)) else [file_path]

        used_block_types = set()
        block_instances = []

        for path in file_paths:
            print(f"Reading file: {path}")
            if not os.path.exists(path):
                print(f"Error: {path} not found.")
                continue

            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()

            print("Parsing JSON...")
            json_data = json.loads(content)

            if isinstance(json_data, list):
                block_data = json_data[1]
            elif isinstance(json_data, dict):
                if 'Data' in json_data:
                    block_data = json_data['Data']
                else:
                    block_data = json_data
            else:
                raise Exception('Invalid build file format')

            for block_type, blocks in block_data.items():
                if not isinstance(blocks, list):
                    continue

                # Only render blocks with 'Block' in the name, or ones we explicitly support
                if 'Block' not in block_type and block_type not in ['Piston']:
                    continue

                used_block_types.add(block_type)
                for block in blocks:
                    try:
                        color = parse_color(block.get('Color')) if 'Color' in block else {'r': 0.7, 'g': 0.7, 'b': 0.7}
                        size = parse_vector3(block.get('Size', '2,2,2')) if 'Size' in block else {'x':2,'y':2,'z':2}
                        cframe = block.get('CFrame')

                        position = {'x': 0, 'y': 0, 'z': 0}
                        if cframe and isinstance(cframe, list) and len(cframe) >= 3:
                            position = {'x': cframe[0], 'y': cframe[1], 'z': cframe[2]}
                        else:
                            position = parse_vector3(block.get('Position', '0,0,0'))

                        mvalues = block.get('MValues', {})
                        transparency = float(block.get('Transparency', 0.0))

                        rotation = None
                        if 'Rotation' in block:
                            rotation = parse_vector3(block['Rotation'])

                        if not sane_vector3(position) or not sane_vector3(size):
                            continue
                        if cframe and not sane_cframe(cframe):
                            continue

                        block_instances.append({
                            'position': position,
                            'size': size,
                            'color': color,
                            'cframe': cframe,
                            'rotation': rotation,
                            'blockType': block_type,
                            'mvalues': mvalues,
                            'transparency': transparency
                        })
                    except Exception:
                        continue

        if len(block_instances) > 0:
            xs = np.array([b['position']['x'] for b in block_instances])
            ys = np.array([b['position']['y'] for b in block_instances])
            zs = np.array([b['position']['z'] for b in block_instances])
            
            med_x, mad_x = np.median(xs), np.median(np.abs(xs - np.median(xs)))
            med_y, mad_y = np.median(ys), np.median(np.abs(ys - np.median(ys)))
            med_z, mad_z = np.median(zs), np.median(np.abs(zs - np.median(zs)))
            
            std_x, std_y, std_z = max(mad_x * 1.4826, 1), max(mad_y * 1.4826, 1), max(mad_z * 1.4826, 1)
            
            threshold = 6
            valid_instances = []
            for b in block_instances:
                if (abs(b['position']['x'] - med_x) <= threshold * std_x and
                    abs(b['position']['y'] - med_y) <= threshold * std_y and
                    abs(b['position']['z'] - med_z) <= threshold * std_z):
                    valid_instances.append(b)
                    
            block_instances = valid_instances

        rendered_blocks = len(block_instances)
        
        if rendered_blocks == 0:
            return
        self.block_instances = block_instances

        print("\nBuilding 3D mesh...")
        
        opaque_vertices = []
        opaque_faces = []
        opaque_colors = []
        opaque_offset = 0
        
        glass_vertices = []
        glass_faces = []
        glass_colors = []
        glass_offset = 0
        
        for b in block_instances:
            transparency = b.get('transparency', 0.0)
            if transparency >= 0.99: 
                continue
                
            block_type = b.get('blockType', 'Normal')
            is_glass = (transparency > 0 or block_type == 'Glass Block')
            
            if block_type not in ['Piston']:
                sx, sy, sz = b['size']['x'], b['size']['y'], b['size']['z']
                dx, dy, dz = sx/2.0, sy/2.0, sz/2.0
                
                local_verts = np.array([
                    [-dx, -dy, -dz], [ dx, -dy, -dz], [ dx,  dy, -dz], [-dx,  dy, -dz],
                    [-dx, -dy,  dz], [ dx, -dy,  dz], [ dx,  dy,  dz], [-dx,  dy,  dz]
                ])
                
                cf = b.get('cframe')
                if cf and len(cf) >= 12:
                    px, py, pz = cf[0:3]
                    R = np.array([cf[3:6], cf[6:9], cf[9:12]])
                    verts = np.dot(local_verts, R.T) + np.array([px, py, pz])
                else:
                    px, py, pz = b['position']['x'], b['position']['y'], b['position']['z']
                    rot = b.get('rotation')
                    if rot:
                        rx = math.radians(rot['x'])
                        ry = math.radians(rot['y'])
                        rz = math.radians(rot['z'])
                        R = trimesh.transformations.euler_matrix(rx, ry, rz, axes='rxyz')[0:3, 0:3]
                        verts = np.dot(local_verts, R.T) + np.array([px, py, pz])
                    else:
                        verts = local_verts + np.array([px, py, pz])
                    
                faces = np.array([
                    [0, 3, 2], [0, 2, 1], # Front
                    [4, 5, 6], [4, 6, 7], # Back
                    [0, 1, 5], [0, 5, 4], # Bottom
                    [3, 7, 6], [3, 6, 2], # Top
                    [0, 4, 7], [0, 7, 3], # Left
                    [1, 2, 6], [1, 6, 5]  # Right
                ])
                
                r = int(b['color']['r'] * 255)
                g = int(b['color']['g'] * 255)
                b_col = int(b['color']['b'] * 255)
                    
                alpha = int((1.0 - transparency) * 255)
                color_array = np.tile([r, g, b_col, alpha], (12, 1))
                
                if is_glass:
                    glass_vertices.append(verts)
                    glass_faces.append(faces + glass_offset)
                    glass_colors.append(color_array)
                    glass_offset += 8
                else:
                    opaque_vertices.append(verts)
                    opaque_faces.append(faces + opaque_offset)
                    opaque_colors.append(color_array)
                    opaque_offset += 8
            else:
                mesh = create_complex_block_mesh(b)
                verts = mesh.vertices
                f = mesh.faces
                c = mesh.visual.face_colors
                if len(c) != len(f):
                    c = np.tile(c[0], (len(f), 1))
                    
                if is_glass:
                    glass_vertices.append(verts)
                    glass_faces.append(f + glass_offset)
                    glass_colors.append(c)
                    glass_offset += len(verts)
                else:
                    opaque_vertices.append(verts)
                    opaque_faces.append(f + opaque_offset)
                    opaque_colors.append(c)
                    opaque_offset += len(verts)

        self.scene = pyrender.Scene(bg_color=[0.5, 0.7, 0.9, 1.0], ambient_light=[0.4, 0.4, 0.4])
        self.centroid = np.zeros(3)
        self.extents = np.ones(3)
        
        if len(opaque_vertices) > 0:
            vertices = np.vstack(opaque_vertices)
            faces = np.vstack(opaque_faces)
            colors = np.vstack(opaque_colors)
            mesh_opq = trimesh.Trimesh(vertices=vertices, faces=faces, face_colors=colors)
            
            pmesh_opq = pyrender.Mesh.from_trimesh(mesh_opq, smooth=False)
            self.scene.add(pmesh_opq)
            
            self.centroid = mesh_opq.centroid
            self.extents = mesh_opq.extents
            
        if len(glass_vertices) > 0:
            vertices = np.vstack(glass_vertices)
            faces = np.vstack(glass_faces)
            colors = np.vstack(glass_colors)
            mesh_glass = trimesh.Trimesh(vertices=vertices, faces=faces, face_colors=colors)
            
            glass_mat = pyrender.MetallicRoughnessMaterial(
                alphaMode='BLEND',
                metallicFactor=0.8,
                roughnessFactor=0.1,
                baseColorFactor=[1.0, 1.0, 1.0, 0.5],
                emissiveFactor=[0.1, 0.3, 0.6]
            )
            
            pmesh_glass = pyrender.Mesh.from_trimesh(mesh_glass, material=glass_mat, smooth=False)
            self.scene.add(pmesh_glass)
            
        self.max_extent = np.max(self.extents)

        self.cam_node = pyrender.Node(camera=pyrender.PerspectiveCamera(yfov=np.pi / 3.0, aspectRatio=1.0))
        self.scene.add_node(self.cam_node)
        
        dl = pyrender.DirectionalLight(color=[1.0, 1.0, 1.0], intensity=2.0)
        self.dl_node = pyrender.Node(light=dl)
        self.scene.add_node(self.dl_node)
        
        try:
            self.renderer = pyrender.OffscreenRenderer(1024, 1024)
        except Exception as exc:
            self.gpu_error = str(exc)
            self.renderer = None
            print(f"GPU/OpenGL preview unavailable, using CPU fallback: {self.gpu_error}")
        self.valid = True

    def look_at_spherical(self, cent, dist, az, el):
        az_rad = math.radians(az)
        el_rad = math.radians(el)
        
        dy = dist * math.sin(el_rad)
        dx = dist * math.cos(el_rad) * math.sin(az_rad)
        dz = dist * math.cos(el_rad) * math.cos(az_rad)
        
        eye = cent + np.array([dx, dy, dz])
        forward = (cent - eye)
        if np.linalg.norm(forward) > 0:
            forward = forward / np.linalg.norm(forward)
        
        up = np.array([0, 1, 0])
        if abs(forward[1]) > 0.99:
            up = np.array([0, 0, -1])
            
        right = np.cross(forward, up)
        if np.linalg.norm(right) > 0:
            right = right / np.linalg.norm(right)
        new_up = np.cross(right, forward)
        
        matrix = np.eye(4)
        matrix[:3, 0] = right
        matrix[:3, 1] = new_up
        matrix[:3, 2] = -forward
        matrix[:3, 3] = eye
        return matrix

    def render(self, azimuth, elevation, dist_multiplier=1.2, force_cpu=False):
        if not self.valid:
            return None
        import io
        if force_cpu or self.renderer is None:
            return self.render_cpu(azimuth, elevation, dist_multiplier)

        fov = np.pi / 3.0
        radius = max(float(np.linalg.norm(self.extents)) * 0.5, float(self.max_extent) * 0.5, 1.0)
        dist = (radius / max(0.001, math.sin(fov * 0.5))) * max(0.25, float(dist_multiplier))
        pose = self.look_at_spherical(self.centroid, dist, azimuth, elevation)
        self.scene.set_pose(self.cam_node, pose=pose)
        self.scene.set_pose(self.dl_node, pose=pose)
        try:
            color, depth = self.renderer.render(self.scene)
        except Exception as exc:
            self.gpu_error = str(exc)
            print(f"GPU/OpenGL render failed, using CPU fallback: {self.gpu_error}")
            try:
                self.renderer.delete()
            except Exception:
                pass
            self.renderer = None
            return self.render_cpu(azimuth, elevation, dist_multiplier)
        
        img = Image.fromarray(color)
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)
        return buf

    def render_cpu(self, azimuth, elevation, dist_multiplier=1.2):
        import io
        width = 1024
        height = 1024
        img = Image.new('RGB', (width, height), (128, 178, 229))
        draw = ImageDraw.Draw(img, 'RGBA')

        fov = np.pi / 3.0
        radius = max(float(np.linalg.norm(self.extents)) * 0.5, float(self.max_extent) * 0.5, 1.0)
        dist = (radius / max(0.001, math.sin(fov * 0.5))) * max(0.25, float(dist_multiplier))
        pose = self.look_at_spherical(self.centroid, dist, azimuth, elevation)
        eye = pose[:3, 3]
        right = pose[:3, 0]
        up = pose[:3, 1]
        forward = -pose[:3, 2]
        focal = (width * 0.5) / math.tan(fov * 0.5)

        instances = self.block_instances
        if len(instances) > CPU_PREVIEW_BLOCK_LIMIT:
            step = int(math.ceil(len(instances) / CPU_PREVIEW_BLOCK_LIMIT))
            instances = instances[::step]

        polygons = []
        common = (
            tuple(float(v) for v in eye),
            tuple(float(v) for v in right),
            tuple(float(v) for v in up),
            tuple(float(v) for v in forward),
            width,
            height,
            focal,
        )
        cores = cpu_worker_count()
        if len(instances) >= CPU_RENDER_PARALLEL_MIN_BLOCKS and cores > 1:
            chunks = chunk_list(instances, cores * 4)
            try:
                with mp.Pool(processes=cores) as pool:
                    args = [(chunk, *common) for chunk in chunks]
                    for partial in pool.imap_unordered(cpu_preview_polygons, args):
                        polygons.extend(partial)
            except Exception as exc:
                print(f"CPU preview multiprocessing fallback: {exc}")
                polygons = cpu_preview_polygons((instances, *common))
        else:
            polygons = cpu_preview_polygons((instances, *common))

        for _, pts2, color in sorted(polygons, key=lambda item: item[0], reverse=True):
            draw.polygon(pts2, fill=color)
            edge = (max(0, color[0] - 28), max(0, color[1] - 28), max(0, color[2] - 28), min(180, color[3]))
            draw.line(pts2 + [pts2[0]], fill=edge, width=1)

        buf = io.BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)
        return buf
        
    def cleanup(self):
        if hasattr(self, 'renderer') and self.renderer:
            self.renderer.delete()
            self.renderer = None
