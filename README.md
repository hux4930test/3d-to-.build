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
- index.html
- README.txt
- 3d_models
- builds

Do not move server.py away from index.html.
Do not delete the 3d_models folder.
Do not delete the builds folder.


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

What start_server.bat does:

1. Checks if exact Python 3.13.11 is already installed.

2. If Python is missing or the wrong version, it tries to install Python 3.13.11 automatically with winget.

3. If winget is not available, it opens the Python 3.13.11 download page.

4. When the converter needs texture support, server.py force-installs Pillow 12.2.0 with pip.

Internet is needed the first time if Python or Pillow is missing.
After everything is installed, normal converting can work offline.

The tool does this so your friend's setup matches this setup as closely as possible.
That helps avoid bugs caused by different Python or Pillow versions.


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


Supported Model Files
---------------------

The converter supports:

- .glb
- .gltf
- .obj
- .stl
- .ply
- .zip

Best formats:

- Use .glb when textures are already inside the model.
- Use .zip when the model has separate texture files.
- Use .obj with .mtl and texture images inside a ZIP.


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


Website Mode
------------

Pick option 1 in start_server.bat.

It opens:

http://127.0.0.1:8765/

It also opens the Python tool mode in a second command window.
You can use the website, the Python menu, or both.

In the website you can:

- drag a model or ZIP onto the page
- pick a model already in 3d_models
- change block/detail settings
- press Convert
- download output .build files

The website runs on your own PC only.
It does not upload your model to an online server.


Python Tool Mode
----------------

Pick option 2 in start_server.bat.

The command window will:

1. Show all models inside 3d_models.

2. Put new/unseen models first.

3. Ask which model to convert.

4. Ask if you want the same settings as last time.

5. Convert the model.

6. Save output to builds.

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

This controls model size in the build area.

Lower number = bigger model.
Higher number = smaller model.

Examples:

- 1 = largest size that fits
- 2 = about 2x smaller
- 5 = safe default
- 10 = small
- 20 = tiny

If the model is too big, raise Fit shrink.
If the model is too small, lower Fit shrink.


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

Usually keep this OFF.

This tries to combine two triangles into one square/rectangle block.
It can reduce block count, but it can also make ugly overlapping plates.

Use it only on simple flat models.


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
- Merge square pairs: off
- Surface overlap: 1.08


Better test:

- Cell size: 0.35
- Fit shrink: 5
- Split cap: 20000
- Smooth triangles: on
- Merge square pairs: off
- Surface overlap: 1.03 to 1.08


Detailed model:

- Cell size: 0.075 to 0.1
- Fit shrink: 5
- Split cap: 150000
- Smooth triangles: on
- Merge square pairs: off
- Surface overlap: 1.08


Very detailed:

- Cell size: 0.05
- Fit shrink: 5
- Split cap: 150000
- Smooth triangles: on
- Merge square pairs: off
- Surface overlap: 1.05 to 1.08
- Small detail boost: 2 if hands/fingers have holes


Small hands/fingers fix:

- Cell size: 0.05 to 0.1
- Fit shrink: 5
- Smooth triangles: on
- Merge square pairs: off
- Surface overlap: 1.08
- Face fill: 2 to 3
- Small detail boost: 2 to 3


Stress test:

- Cell size: 0.05 or smaller
- Fit shrink: 5
- Split cap: 1000000
- Smooth triangles: on
- Merge square pairs: off

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
- Keep Merge square pairs off unless the model is simple and flat.


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
- you personally download a model from the web


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
- Merge square pairs: off

Then try:

- Cell size: 0.05
- Split cap: 150000

That makes a more detailed teapot in one file if the total blocks are below 150000.
