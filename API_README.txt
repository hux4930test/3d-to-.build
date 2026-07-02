3D Build Converter API
======================

This folder is portable. Keep these files together:

- start_server.bat
- server.py
- build_renderer.py
- index.html
- README.txt
- API_README.txt
- 3d_models
- builds

Start The API
-------------

Normal local mode:

1. Double-click start_server.bat.
2. Pick Website mode.
3. API base URL is:

   http://127.0.0.1:8765

Advanced host/port:

Set these before running start_server.bat:

set BUILD_TOOL_HOST=127.0.0.1
set BUILD_TOOL_PORT=8765

To listen on your LAN, use:

set BUILD_TOOL_HOST=0.0.0.0

Only use 0.0.0.0 on a trusted network. The API can read models from 3d_models and write files into builds.


Machine-Readable API Docs
-------------------------

OpenAPI JSON:

GET /api/openapi.json

Docs text:

GET /api/docs

Health check:

GET /api/health


CORS
----

The API sends:

Access-Control-Allow-Origin: *

That lets local websites and tools call the API from JavaScript.


Supported Model Uploads
-----------------------

The converter accepts:

- .zip
- .glb
- .gltf
- .obj
- .stl
- .ply
- .rbxlx
- .rbxmx

Use .zip when the model has separate texture files.


Settings JSON
-------------

All settings are optional. Missing values use defaults from GET /api/models.

{
  "cell": 0.075,
  "shrink": 5.0,
  "split_size": 150000,
  "surface_slabs": true,
  "face_fill": 2.0,
  "small_detail_boost": 1.0,
  "fill_radius": 1,
  "merge_tolerance": 12.0,
  "surface_overlap": 1.08,
  "merge_triangle_quads": true,
  "roblox_slope_drop": 0.5,
  "roblox_wedge_fill": 2.0,
  "roblox_terrain_detail": 1.0,
  "rot_x": 0,
  "rot_y": 0,
  "rot_z": 0
}


GET /api/health
---------------

Returns server status, paths, version, and defaults.

Example:

curl http://127.0.0.1:8765/api/health


GET /api/models
---------------

Lists model files inside 3d_models and returns default settings.

Example:

curl http://127.0.0.1:8765/api/models


POST /api/convert
-----------------

Converts a model into .build files.

You can either:

1. Convert a model already inside 3d_models.
2. Upload a model or ZIP with the request.

Convert a model from 3d_models:

curl -X POST http://127.0.0.1:8765/api/convert ^
  -F "model=senku_dr_stone_3d_model.glb" ^
  -F "settings={\"cell\":0.25,\"shrink\":5,\"split_size\":150000,\"surface_slabs\":true}"

Upload and convert a file:

curl -X POST http://127.0.0.1:8765/api/convert ^
  -F "file=@C:\path\to\model.zip" ^
  -F "settings={\"cell\":0.1,\"shrink\":5,\"split_size\":150000}"

Response includes:

- ok
- output_dir
- files
- blocks
- faces
- seconds
- logs

Each item in files has a download URL.


GET /builds/...
---------------

Downloads a generated .build file.

Example:

curl -L "http://127.0.0.1:8765/builds/folder/file.build" -o file.build


GET /api/render-build
---------------------

Renders one or more .build files to a PNG preview.

Query parameters:

- file: .build URL or relative builds path. Repeat it for multiple files.
- az: camera azimuth, default 45
- el: camera elevation, default 28
- dist: zoom/distance multiplier, default 1.15
- cpu: set 1 to force multi-core CPU preview

One file:

curl -L "http://127.0.0.1:8765/api/render-build?file=/builds/folder/file_part001.build&az=45&el=28&dist=1.15" -o preview.png

Multiple split files:

curl -L "http://127.0.0.1:8765/api/render-build?file=/builds/folder/file_part001.build&file=/builds/folder/file_part002.build&cpu=1" -o preview.png


POST /api/roblox-user
---------------------

Creates a Roblox avatar model ZIP in 3d_models.

Example:

curl -X POST http://127.0.0.1:8765/api/roblox-user ^
  -F "username=rixuba"

Then call /api/convert with the created output name.


JavaScript Example
------------------

const base = "http://127.0.0.1:8765";

const settings = {
  cell: 0.25,
  shrink: 5,
  split_size: 150000,
  surface_slabs: true
};

const form = new FormData();
form.append("model", "senku_dr_stone_3d_model.glb");
form.append("settings", JSON.stringify(settings));

const res = await fetch(`${base}/api/convert`, {
  method: "POST",
  body: form
});

const data = await res.json();
console.log(data.files);


Python Example
--------------

import requests
import json

base = "http://127.0.0.1:8765"

settings = {
    "cell": 0.25,
    "shrink": 5,
    "split_size": 150000,
    "surface_slabs": True,
}

with open(r"C:\path\to\model.glb", "rb") as f:
    response = requests.post(
        base + "/api/convert",
        files={"file": ("model.glb", f, "application/octet-stream")},
        data={"settings": json.dumps(settings)},
        timeout=3600,
    )

print(response.json())


Notes
-----

- The API is local by default.
- It does not upload models to a cloud server.
- Converting big models can take a long time.
- The render preview tries GPU/OpenGL first unless cpu=1 is used.
- If OpenGL fails, preview falls back to CPU mode.
