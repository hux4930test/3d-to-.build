yes this is ALL with ai 
i dont care if you skid this 
this was just for fun


3D Build Converter - Full Guide
===============================
It has two ways to use it:
1. Website mode
2. Python tool mode
Both modes use the same converter. Website mode opens the browser website and also opens the Python tool menu in another command window. Python tool mode opens only the command-window menu.
What Is In This Folder
----------------------

Keep this whole folder together:

- start_server.bat
- server.py
- build_renderer.py
- index.html
- README.txt
- API_README.txt
- 3d_models
- builds

Do not move server.py away from index.html.
Do not delete the 3d_models folder.
Do not delete the builds folder.
API_README.txt explains how to call the converter from another program.


How to Give This to Someone Else
--------------------------------

1. Right-click the portable_build_tool folder.

2. Make a ZIP of the whole folder.

3. Send that ZIP to the other person.

4. Tell them to extract the ZIP first.

5. They double-click start_server.bat.

Do not run it from inside the ZIP preview. Extract it into a real folder first.


Auto Setup
----------

The tool tries to download/install what it needs.

This package is pinned to the same versions used when it was made:

- Python 3.13.11
- Pillow 12.2.0
- NumPy 1.26.4
- Trimesh 4.12.2
- Pyrender 0.1.45

What start_server.bat does:

1. Checks if exact Python 3.13.11 is already installed.

2. If Python is missing or the wrong version, it tries to install Python 3.13.11 automatically with winget.

3. If winget is not available, it opens the Python 3.13.11 download page.

4. When the converter needs texture support, server.py force-installs Pillow 12.2.0 with pip.
5. When you use Build preview for the first time, server.py installs NumPy 1.26.4, Trimesh 4.12.2, and Pyrender 0.1.45 if they are missing.

Internet is needed the first time if Python, Pillow, or preview-render packages are missing.
After everything is installed, normal converting can work offline.

The tool does this so your friend's setup matches this setup as closely as possible.
That helps avoid bugs caused by different Python, Pillow, or render-library versions.


If Auto Setup Fails
-------------------

Install Python manually:

https://www.python.org/downloads/release/python-31311/

During Python install, enable:

Add python.exe to PATH

Then run start_server.bat again.

If Pillow fails to install, open Command Prompt and run:

python -m pip install --force-reinstall Pillow==12.2.0

Then run start_server.bat again.


Quick Start
-----------

1. Put a model file into the 3d_models folder.

2. Double-click start_server.bat.

3. Pick:

   1 = Website mode
   2 = Python tool mode

4. Convert the model.

5. Open the builds folder and use the .build files.

API Quick Start
---------------

After website mode starts, API docs are available here:

http://127.0.0.1:8765/api/docs

OpenAPI JSON is available here:

http://127.0.0.1:8765/api/openapi.json

The API is localhost-only by default. Advanced users can set BUILD_TOOL_HOST and BUILD_TOOL_PORT before running start_server.bat.


Supported Model Files
---------------------

The converter supports:

- .glb
- .gltf
- .obj
- .stl
- .ply
- .rbxlx
- .rbxmx
- .zip

Best formats:

- Use .glb when textures are already inside the model.
- Use .zip when the model has separate texture files.
- Use .obj with .mtl and texture images inside a ZIP.
- Use .rbxlx or .rbxmx for Roblox Studio XML files.

Roblox Studio support:

- .rbxlx = Roblox place saved as XML
- .rbxmx = Roblox model saved as XML
- normal Parts keep their size, color, transparency, rotation, and CanCollide setting
- Roblox files are optimized automatically by preserving simple Studio parts as one .build block
- If a Roblox part has CanCollide=false, every .build block generated from that part also writes "CanCollide":false
- Cell size and Face fill do not break normal Roblox parts into millions of triangle blocks
- Cell size does not voxelize Roblox Studio parts. It mainly affects preview/conversion defaults and non-Roblox model work.
- Fit shrink scales the whole Roblox build into the allowed build area, the same as normal 3D files
- Parts inside ReplicatedStorage are skipped so template/storage objects do not get built into the map
- WedgePart uses the old triangle-strip shell/surface mode, with thin 0.01 side thickness, much higher strip detail on the triangular end caps, and no extra end overlap
- CornerWedgePart becomes capped corner surface strips by default
- Roblox wedge fill helps very thin terrain wedges avoid wire/grid-looking lines
- Roblox terrain detail 4+ can add solid under-fill if a wedge or corner looks hollow
- Ball/Sphere parts become a lower-detail filled 17x17 ellipsoid scanline build to use fewer blocks.
- Cylinder parts and CylinderMesh children use the Roblox part axis. Long cylinders use one centered fake-cylinder set made from long rotated planks that pass all the way through to the other side. Broken/ambiguous short or chunky cylinders fall back to one long cuboid.
- Turn off Roblox primitive shapes on the website to skip special cylinder/ring/cone/sphere conversion and export those parts as normal box blocks.
- Native Roblox Cylinder parts use the Studio cylinder X axis; CylinderMesh and SpecialMesh cylinder children use the mesh Y axis
- Custom MeshPart/UnionOperation round guesses choose their cylinder axis from the name: wheels/rings/circles use the shortest thickness axis, while pipes/barrels/logs/posts/columns use the longest length axis.
- Thin disks/circles/rings use 11 clumped scanline bands to use fewer blocks while still filling the round face.
- Custom UnionOperation/MeshPart names like ring, cog, gear, round, and circle get special round approximations instead of plain bounding boxes
- Cone/spike/pyramid mesh names become stacked cone approximations
- MeshPart and UnionOperation use rounded/cylinder/cone approximations when the name looks like leaves, rocks, trunks, branches, barrels, wheels, pipes, cones, spikes, or bushes
- Other MeshPart and UnionOperation objects still use bounding blocks because the external Roblox mesh asset is not stored inside the XML file

Binary Roblox files:

- .rbxl and .rbxm are not supported yet
- open them in Roblox Studio and save/export as .rbxlx or .rbxmx


How ZIP Models Should Look
--------------------------

OBJ model example:

- model.obj
- model.mtl
- texture.png
- any other texture images

GLTF model example:

- model.gltf
- model.bin
- texture.png
- any other texture images

The ZIP can contain folders. The converter searches inside it and finds the supported model file.

If there are multiple models in one ZIP, it will usually pick in this order:

1. .glb
2. .gltf
3. .obj
4. .stl
5. .ply
6. .rbxlx
7. .rbxmx


Roblox Avatar Models
--------------------

The website has a Roblox avatar username box.

Type a username and press:

Make Avatar

That creates a simplified 3D avatar model and saves it into:

3d_models

Then press Convert to make .build files from it.

The Python tool can also do this.
When it starts, answer yes to:

Create a Roblox avatar model first

This uses public Roblox username/avatar data:

- body colors
- R6/R15 avatar type
- avatar scale values
- equipped accessories list
- public avatar thumbnail colors for shirt/pants color hints

The generated avatar is a blocky 3D Studio model, not just the username text.
It saves as .rbxmx, so the Roblox optimized converter turns each avatar part into one .build block.


Website Mode
------------

Pick option 1 in start_server.bat.

It opens:

http://127.0.0.1:8765/

It also opens the Python tool mode in a second command window.
You can use the website, the Python menu, or both.

In the website you can:

- drag a model or ZIP onto the page
- drag a Roblox Studio .rbxlx or .rbxmx file onto the page
- make a Roblox avatar model from a username
- pick a model already in 3d_models
- change block/detail settings
- press Convert
- download output .build files
- press Render on an output .build file to preview it as an image in the website
- press Render all to preview every split .build file together as one model
- drag the preview image to rotate the camera
- mouse-wheel over the preview or change Zoom to move the camera closer/farther
- turn on CPU preview if the GPU/OpenGL preview fails or if you want the preview renderer to use CPU worker processes

The website runs on your own PC only.
It does not upload your model to an online server.
The build preview also runs locally. It only reads .build files from the builds folder.
The preview uses the GPU/OpenGL renderer when your Windows graphics driver supports it.
If Windows, Remote Desktop, or the GPU driver cannot create the OpenGL preview context, the tool falls back to a local CPU preview instead of crashing.
The CPU preview mode splits projection work across logical CPU cores, then draws the final image.

Conversion uses CPU workers, not the GPU.
Smooth triangle conversion splits the triangle-strip work across your logical CPU cores.
Some steps still run partly single-core or I/O-bound, such as loading textures, matching square triangle pairs, and writing the final .build JSON files, so Task Manager may not sit at 100% for the whole run.


Python Tool Mode
----------------

Pick option 2 in start_server.bat.

The command window will:

1. Ask if you want to create a Roblox avatar model first.

2. Show all models inside 3d_models.

3. Put new/unseen models first.

4. Ask which model to convert.

5. Ask if you want the same settings as last time.

6. Convert the model.

7. Save output to builds.

This mode is good when you want a simple numbered menu.


Where Output Goes
-----------------

Every conversion creates a folder inside:

builds

Example:

builds/model_name_cell0.35_shrink5.0_triangles

Inside that folder you get one or more files ending in:

.build

Example:

model_name_cell0.35_shrink5.0_triangles_part001.build

If the model is huge, it may make:

- part001.build
- part002.build
- part003.build

That is controlled by Split cap.


Important Settings
------------------

Cell size:

This controls detail.

Smaller cell size means:

- smoother model
- more blocks
- bigger .build files
- more lag risk

Bigger cell size means:

- less detail
- fewer blocks
- faster loading

Good values:

- 0.75 = very safe test size
- 0.5 = quick low-block test
- 0.35 = better shape test
- 0.1 = detailed model
- 0.075 = high detail but still reasonable
- 0.05 = very detailed and can get huge
- 0.01 = extreme, usually too many blocks


Fit shrink:

This controls model size in the build area. It works on normal 3D files and Roblox XML files.

Lower number = bigger model.
Higher number = smaller model.

Examples:

- 0.05 = huge, for testing only
- 0.5 = bigger than normal
- 1 = largest normal size that fits
- 2 = about 2x smaller
- 5 = safe default
- 10 = small
- 20 = tiny

If the model is too big, raise Fit shrink.
If the model is too small, lower Fit shrink.


Roblox slope drop:

This only affects normal Roblox WedgePart slopes.

It moves the diagonal slope slab inward/down so the slab is not floating above the wedge.

Examples:

- 0 = old centered slope
- 0.5 = default
- 1 = lower slope
- 2 = much lower

If wedges have gaps under the slope, raise Roblox slope drop.
If wedges look buried too deep, lower Roblox slope drop.


Roblox wedge fill:

This only affects normal Roblox WedgePart terrain slopes.

Some old Roblox maps fake terrain with very skinny WedgeParts. After scaling down, those wedges can look like white wireframe/grid lines instead of filled ground.

Examples:

- 0 = old exact thin width
- 1 = light fill
- 2 = default, stronger fill for skinny terrain wedges
- 3+ = very chunky, only use if the terrain still has gaps

If terrain looks like outlines, raise Roblox wedge fill.
If slopes look too fat or overlap too much, lower Roblox wedge fill.


Roblox terrain detail:

This affects Roblox WedgePart, CornerWedgePart, cylinders, cones, and rounded primitive approximations.

Default value 1 keeps wedges/corner wedges in the smoother surface mode. Long cylinders use one centered through-shape fake-cylinder plank set, broken/chunky cylinders fall back to one long cuboid, and thin disks/rings use 11 clumped scanline bands. Values 4 and higher add solid wedge under-fill, which can help hollow terrain but may look more blocky.

Examples:

- 1 = default, smooth wedge surfaces, fewer blocks
- 2 = smoother primitives
- 3 = smoother cylinders/cones
- 4+ = adds wedge/corner under-fill and more primitive detail
- 8+ = heavy, only use for small tests or when the cap is high

Raise this when:

- wedges/corners look hollow underneath
- cylinders/cones look too square
- rounded Roblox primitives need more detail

Lower this when:

- block count gets too high
- wedges look too chunky or stair-stepped
- the build starts overlapping too much


Split cap:

This controls how many blocks can go into one .build file.

Examples:

- 20000 = safe testing
- 100000 = medium
- 150000 = good big single-file test
- 1000000 = WONT WORK

If total blocks are higher than Split cap, it makes multiple files.

Example:

Total blocks: 84528
Split cap: 20000
Output: 5 files

Total blocks: 84528
Split cap: 150000
Output: 1 file


Smooth triangles:

Usually keep this ON.

This makes thin strips that fill triangle faces.
It looks more like the actual model than dot/voxel mode.

Turn it off only if you specifically want blocky voxel-style output.


Merge square pairs:

Usually keep this ON.

This combines two matching triangles into one square/rectangle block only when they form a clean flat side.
It helps models where a square face is split diagonally into two triangles.

If a model creates strange plates anyway, turn this OFF and convert again.


Surface overlap:

This slightly overlaps smooth triangle strips to hide cracks.

Good values:

- 1.00 = no overlap, can show small gaps
- 1.03 = good for flat test shapes
- 1.05 = light overlap
- 1.08 = normal default
- 1.20+ = may look chunky or overlapped

If there are holes, increase it a little.
If faces overlap badly, lower it.


Color merge:

This controls how close two colors can be and still count as the same color.

Lower value means more exact colors.
Higher value can reduce blocks, but colors become less accurate.

Good values:

- 0 = exact color matching
- 12 = default
- 25 = more compression
- 50 = aggressive, colors may look wrong


Face fill:

This controls how many strips fill each triangle.

Higher value:

- smoother filled faces
- more blocks

Lower value:

- fewer blocks
- more risk of gaps

Good values:

- 1 = low block count
- 2 = default
- 3 = smoother
- 4+ = more blocks


Small detail boost:

This is for tiny model parts like hands, fingers, hair tips, thin clothes, horns, and small accessories.

It adds extra triangle strips on small faces.

Higher value means:

- fewer tiny holes
- better filled small details
- more blocks

Good values:

- 1 = normal
- 2 = helps hands/fingers
- 3 = stronger hole fix, more blocks
- 4+ = heavy, can get expensive

If hands have little holes, try Small detail boost = 2 first.
If they still have holes, try 3.


Fill radius:

This mostly affects voxel/block mode.

Higher value fills holes by adding nearby blocks.

Good values:

- 0 = fewer blocks
- 1 = fills small holes
- 2 = bulky

If Smooth triangles is on, this setting matters less.


Rotation:

Use Rotate X, Rotate Y, and Rotate Z if the model is sideways.

Common fixes:

- Rotate X = 90
- Rotate X = -90
- Rotate Y = 180
- Rotate Z = 90

Try one at a time.


Recommended Presets
-------------------

Safe quick test:

- Cell size: 0.5
- Fit shrink: 5
- Split cap: 20000
- Smooth triangles: on
- Merge square pairs: on
- Surface overlap: 1.08


Better test:

- Cell size: 0.35
- Fit shrink: 5
- Split cap: 20000
- Smooth triangles: on
- Merge square pairs: on
- Surface overlap: 1.03 to 1.08


Detailed model:

- Cell size: 0.075 to 0.1
- Fit shrink: 5
- Split cap: 150000
- Smooth triangles: on
- Merge square pairs: on
- Surface overlap: 1.08


Very detailed:

- Cell size: 0.05
- Fit shrink: 5
- Split cap: 150000
- Smooth triangles: on
- Merge square pairs: on
- Surface overlap: 1.05 to 1.08
- Small detail boost: 2 if hands/fingers have holes


Small hands/fingers fix:

- Cell size: 0.05 to 0.1
- Fit shrink: 5
- Smooth triangles: on
- Merge square pairs: on
- Surface overlap: 1.08
- Face fill: 2 to 3
- Small detail boost: 2 to 3


Stress test:

- Cell size: 0.05 or smaller
- Fit shrink: 5
- Split cap: 1000000
- Smooth triangles: on
- Merge square pairs: on

Only use this if you are testing how much the game can load.


Common Problems and Fixes
-------------------------

Problem: It looks like dots.

Fix:

- Turn Smooth triangles on.
- Use a smaller Cell size.
- Raise Face fill.


Problem: There are holes.

Fix:

- Increase Surface overlap to 1.08.
- Use a smaller Cell size.
- Raise Face fill.
- Raise Small detail boost to 2 or 3 for hands/fingers.


Problem: It has ugly overlapping plates.

Fix:

- Turn Merge square pairs off.
- Lower Surface overlap to 1.03 or 1.05.


Problem: Too many blocks.

Fix:

- Increase Cell size.
- Increase Fit shrink.
- Lower Face fill.
- Lower Small detail boost.
- Keep Merge square pairs on for flat square sides; turn it off only if a model makes bad plates.


Problem: The model is too small.

Fix:

- Lower Fit shrink.


Problem: The model is too big.

Fix:

- Raise Fit shrink.


Problem: Colors look wrong.

Fix:

- Prefer .glb if available.
- For OBJ, make sure .obj, .mtl, and texture images are inside the ZIP.
- Open the .mtl file and check that texture filenames match the real files.
- Try Color merge = 0 for exact colors.


Problem: Texture does not show.

Fix:

- Make a ZIP with the model and every texture file.
- Keep texture filenames unchanged.
- Do not only upload the .obj by itself if it needs .mtl or image files.


Problem: The model is sideways.

Fix:

- Try Rotate X = 90.
- Try Rotate X = -90.
- Try Rotate Y = 180.


Problem: start_server.bat opens then closes.

Fix:

- Extract the folder from the ZIP first.
- Install Python manually if auto setup failed.
- Run start_server.bat again.


Privacy / Internet Use
----------------------

The converter website runs locally at:

http://127.0.0.1:8765/

That means the website is on your own PC.

It does not send your models to an online conversion server.

Internet is only used when:

- Python is missing and start_server.bat tries winget install
- Pillow is missing and server.py installs it with pip
- you use Make Avatar from a Roblox username
- you personally download a model from the web

Make Avatar uses public Roblox web APIs to look up the username and avatar data.
The converter still runs locally on your PC.


Test Models Included
--------------------

The 3d_models folder may include:

- triangle_color_ball.zip
- octagon_test.zip
- hexagon_test.zip
- flat_hexagon_test.zip
- utah_teapot_test.zip

These are for testing shape, colors, triangle strips, and block count.

The teapot test is a derived Utah Teapot test model.
The Utah Teapot is the classic computer graphics test model originally developed at the University of Utah.


Good First Test
---------------

Use:

- utah_teapot_test.zip
- Cell size: 0.5
- Fit shrink: 5
- Split cap: 20000
- Smooth triangles: on
- Merge square pairs: on

Then try:

- Cell size: 0.05
- Split cap: 150000

That makes a more detailed teapot in one file if the total blocks are below 150000.
