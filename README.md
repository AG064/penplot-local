# PenPlot Local

A working local reference implementation, not a compiled Windows product and not a printer controller. It converts selected PDF pages, common bitmap images and plain ASCII text/art into restricted pen-plot G-code, then re-reads that G-code to simulate its ink footprint. No LLM, OCR, network upload, telemetry or automatic printer execution is used by this application.

## Project structure

`app.py` is the Tk desktop interface. `penplot.py` is the reusable converter and command-line entry point. `test_penplot.py` contains the unit tests. `APP_PREVIEW.png` shows the interface using synthetic artwork.

Do not commit source labels, generated previews, toolpaths, G-code, addresses, tracking identifiers, or other private job data. Generated `PenPlot_*` folders and G-code files are ignored by Git.

## Windows start

Use a Python installation with Tk support. Automated tests cover Python 3.12 and 3.13 on Windows and Linux. Native packaging and printer firmware integration are outside this project. The first dependency installation requires Internet access. The application itself operates locally afterward.

From PowerShell in the extracted application folder:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

This avoids changing PowerShell activation policy. The pinned dependency versions are the versions used for the recorded conversion, not a claim that they are the newest. An isolated environment installation was not tested in this session because package-network access was unavailable. Do not silently change dependencies and claim byte-identical reproduction; run the tests and compare the manifests after any upgrade.

The `pyzbar` project documents that its Windows wheels include ZBar DLLs. On Linux it requires the ZBar shared library (usually `libzbar0`); Tk may require `python3-tk`. On macOS, ZBar must also be installed separately. Strict label mode refuses conversion when its decoder cannot load. See the primary dependency documentation under Sources.

## Workflow

Choose a file and a new output location. For PDF, leaving Ink width blank preserves the physical PDF scale of the selected page's ink. For bitmap/ASCII, enter the physical ink width; resolution metadata is never treated as a reliable physical measurement. Multi-page PDFs are one selected page per job, not an automatically concatenated print.

Enter the actual line of ink left on paper, in millimetres, in the pen-width field. This is NOT the pen tip's marketing size, pen barrel diameter, nozzle diameter or a calibration of writing pressure. Enter the native nozzle Z at which that particular fitted pen lightly contacts that paper. The application does not measure either value.

For the recorded label only, the exported files assume a 0.30 mm actual ink line, contact at native Z10 and lift to Z12. This is a stated example assumption, not a measurement of the user's pen. The earlier user-reported successful logo job establishes only that the previous motion workflow worked for that user; it does not validate these new label paths or postal scan quality.

Use filled mode for labels. It combines inward-offset contour passes for broad black regions with centreline strokes for narrow text, rules and accents. Outline mode is for artwork and is blocked when the source decoder detects a barcode/QR. Strict shipping-label checking requires a working decoder, at least one source symbol and exactly matching detected symbol type/payload multisets in the simulated exported drawing. For ordinary art or ASCII with no code, turn strict label checking off.

Click Generate files and preview. This only saves files. Inspect `SOURCE_CROP.png`, `BINARY_TARGET.png`, `SIMULATED_INK.png`, `MANIFEST.json`, the actual G-code and the air frame. The ordinary converter does not yet generate an automatic symbol-only test coupon; that operation is provided by the separate recorded-case replay script.

## Command line

Original physical scale of a PDF page:

```powershell
.\.venv\Scripts\python.exe penplot.py "label.pdf" --out "label_job" --pen-mm 0.30 --z-down 10 --require-symbols --ack-calibration
```

Bitmap or ASCII artwork at a selected physical width:

```powershell
.\.venv\Scripts\python.exe penplot.py "drawing.png" --out "drawing_job" --width-mm 100 --pen-mm 0.30 --z-down 10 --ack-calibration
.\.venv\Scripts\python.exe penplot.py "art.asc" --out "ascii_job" --width-mm 100 --pen-mm 0.30 --z-down 10 --ack-calibration
```

These example numbers are not a substitute for calibration. Output folders must not already exist. The converter never overwrites another job.

Run the test suite:

```powershell
.\.venv\Scripts\python.exe -m unittest -v test_penplot
```

## Supported inputs and deliberate limits

PDF page rendering uses the document's existing fonts, vectors and images. Text is not retyped, corrected or regenerated. PNG, JPEG, BMP, WebP and single-frame TIFF are supported. Transparency is composited on white and EXIF orientation is applied. ASCII TXT/ASC preserves monospaced relative layout and expands tabs to eight columns; it does not support Unicode terminal glyphs, ANSI escapes, arbitrary fonts or control codes. A local Consolas, DejaVu Sans Mono, Liberation Mono or Menlo font is used without copying the font into output.

This is NOT yet an "any file gives a production-ready print" implementation. Photographs are converted by black/white thresholding, not photographic halftoning. SVG ingestion, native vector extraction, multicolour passes, grayscale hatching, page batching, editable crop selection, a general symbol coupon generator and automatic pen-width calibration are not implemented. The app can reject oversized or excessively complex inputs rather than guess.

Current budgets: 32 MiB input, 24 million raster pixels, 8,000 strokes, 1 million path points, 250 x 250 ASCII cells. Full-size A1 only; the coordinate guard is 20..236 mm on both axes. This is not a physical collision model of the pen holder. No auto-shrink to fit. Raster resolution is 600 DPI by default; the CLI accepts 300..600 DPI.

Automatic cropping removes outer empty margins and adds a fixed white margin. This can change a standalone symbol's quiet zone. Inspect it, especially for isolated barcode/QR images; decoder success is not a standards-based quiet-zone check. The recorded whole-label case preserves the symbol areas inside the label frame and checks them separately. The production app should have a verified protected-symbol-region stage before crop/scale export.

## Machine contract

Existing native homing and the user's calibrated pen are preconditions. Home only with the pen, holder and paper removed. The printer must be cold. Heater-off commands do not wait for cooldown. Keep the normal build plate properly seated; do not move axes by hand or reset coordinates after homing.

The exported program uses millimetres and native absolute XYZ. It sets heaters/fan off, feed override 100%, selected acceleration 300 mm/s^2, raises Z before XY travel, lowers for each stroke, lifts between strokes and finishes lifted in place. It contains no G28, probing, wiping, extrusion, G92 origin reset, motor disable or normal slicer start/end sequence. Contact/lift defaults are configurable; final Z is contact + lift + 3 mm. No XY parking move is appended.

An XY offset, when supplied, is pen-tip position minus nozzle position in native machine axes. Both axes must be supplied. Commanded nozzle position equals desired pen position minus that offset. With offsets absent, the path is nozzle-centred and the actual pen position is UNKNOWN, not measured zero. Put the paper under the actual pen sweep.

Use only the unchanged G-code transfer route already tested on the actual printer. Do not re-slice it or append normal Bambu Studio start/end code. The app does not package `.gcode.3mf` or invent a compatible sender. Before a new footprint, run the air frame and inspect the complete holder/pen/paper clearance. Remove the attachment before later homing or normal printing. Recalibrate if pen insertion, paper thickness, coordinate state or holder changes.

## Interpretation of validation

The simulator re-reads the actual exported G-code and renders an ideal, round, constant-width ink footprint. It does not render the source file and call that a G-code preview. The parser rejects unknown commands/parameters and checks bounds, feeds, pen-up travel, separate Z moves, final lift and planned/exported coordinate agreement.

The decoded-symbol comparison checks only symbols detected by that decoder. It cannot prove that every symbol present in an arbitrary source was found. It does not grade contrast, bar growth, reflective ink, paper texture, ink pooling, position error, pen force, skew, shipping-carrier acceptance or real scanning. Thin text can become heavier than the original. A label job is not approved for actual shipping until the physical result is checked with suitable readers and its legibility is confirmed.

## Before distributing a product

Dependency licensing requires a deliberate decision. PyMuPDF/MuPDF is offered under AGPL and commercial licensing. Do not assume a closed-source distributed product can use this prototype's PDF backend without reviewing that choice. An alternative backend can be evaluated behind the same raster/geometry interface and regression tests. No commercial legal clearance is asserted here.

PenPlot Local is licensed under the GNU Affero General Public License v3.0. This does not change the licences of its dependencies or any rights in imported documents. PyMuPDF/MuPDF is available under AGPL and commercial licensing; review the dependency licences before distributing a modified product.

## Primary sources consulted

- PyMuPDF page rendering, dimensions, rotation, vector and image inspection: https://pymupdf.readthedocs.io/en/latest/page.html
- PyMuPDF licensing: https://pymupdf.readthedocs.io/en/latest/about.html#license-and-copyright
- Pillow image loading/limits/compositing: https://pillow.readthedocs.io/en/stable/reference/Image.html
- scikit-image skeletonization: https://scikit-image.org/docs/stable/auto_examples/edges/plot_skeleton.html
- pyzbar decoder and platform installation: https://github.com/NaturalHistoryMuseum/pyzbar
- DENSO WAVE QR code quiet-zone guidance: https://www.qrcode.com/en/howto/code.html

The motion template is adapted from the earlier, user-reported successful A1 Z10 plotting workflow in this conversation, not a general-purpose stock slicer profile.
