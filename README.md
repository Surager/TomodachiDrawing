# TomodachiDrawing

Tomodachi Life drawing macro generator.

**Chinese README:** [README_zh.md](README_zh.md)

## Abstract

This repository implements tooling for converting raster artwork into timed controller macros compatible with *Tomodachi Life*’s drawing interface. The pipeline performs color quantization to the in-game palette, optional brush-aware path generation, and replay via Bluetooth-emulated Nintendo Switch controllers. **No dedicated peripheral hardware is required beyond a general-purpose computer equipped with a Bluetooth radio.** Host-side execution commonly relies on Linux userspace stacks that expose a virtual Pro Controller (for example via `nxbt`); on Windows or macOS, comparable connectivity is routinely achieved by running such software inside a Linux virtual machine and forwarding the host Bluetooth adapter into the guest—e.g. with USB/IP (`usbip`)—so that the adapter appears as a USB device to the VM and can pair as the Switch expects.

## Repository structure

- `main.py`: default entrypoint; `--mode pixel` for exact 1×1 drawing, `--mode brush` for coverage drawing
- `main_fast.py`: faster pixel generator with basic path planning
- `main_brush.py`: brush-aware generator using six brush sizes
- `ctrl.py`: `nxbt` macro runner
- `preview.py`: quantized image preview and pixel dump
- `sequence_preview.py`: replays a generated button macro into a preview PNG
- `webui.py`: Flask Web UI for controller connection, button testing, image upload, preview, and drawing progress

## Modeling assumptions

- Canvas size is `256×256`.
- Input images are interpreted as `RGBA`; fully transparent samples are discarded.
- Colors are quantized to the drawing panel grid:
  - Hue: 200 steps
  - Saturation: 214 steps
  - Brightness: 112 steps
- Brush footprints are square with odd side lengths: `1`, `3`, `5`, `13`, `19`, `27`.
- Brush selector state and color selector state persist across UI openings (as modeled by the generator).
- Macro lines retain the `BUTTON 0.075s` convention, including `0.075s` dwell intervals.

## Typical workflow

```bash
python main.py --mode pixel picture.png > macro.txt
python main.py --mode brush picture.png > macro.txt
python main.py --mode brush --merge-threshold 8 picture.png > macro.txt
python ctrl.py macro.txt
python preview.py picture.png -o preview.png --dump points.json
python sequence_preview.py macro.txt -o sequence_preview.png
python webui.py
```

After starting `webui.py`, open `http://127.0.0.1:50000`.

If controller attachment fails, retain the terminal session and inspect the printed traceback. Prefer uninstrumented operation first; `--debug` disables Flask’s reloader to avoid extraneous processes around `nxbt`.

## Initial drawing setup

Before replaying a macro against the in-game canvas, align the editor state with the generator’s assumptions:

1. Select the **smallest square** brush (minimum footprint).
2. Move the active **custom color** indicator to the **bottom-left** slot of the custom-color grid.
3. Place the brush cursor at the **top-left** corner of the drawing canvas.

## Dependency management (`uv`)

```bash
uv sync
uv run tomodachi-webui
```

Equivalent script entry points are exposed under `uv run`, for example:

```bash
uv run tomodachi-generate --mode brush picture.png
uv run tomodachi-generate --mode brush --merge-threshold 8 picture.png
uv run tomodachi-preview picture.png -o preview.png
uv run tomodachi-sequence-preview macro.txt -o sequence_preview.png
uv run tomodachi-control macro.txt
```

## Remarks

- `main.py` defaults to pixel mode.
- `ctrl.py` depends on `nxbt` and `tqdm`.
- For throughput-oriented generation where strict per-pixel fidelity is secondary, `main_brush.py` is the recommended entrypoint.

## Limitations

Macro replay is mediated by Bluetooth links and host/console-side controller emulation. In deployment, variable latency, occasional loss or reordering of HID reports, and short-lived disconnects are routinely encountered. **Accordingly, end-to-end drawing fidelity cannot be guaranteed to match the synthesized macro or the generator’s idealized timing with probability one**—pixel- or stroke-level identity between intent and on-console outcome should not be assumed.

## License

This project is released under the **[PolyForm Noncommercial License 1.0.0](LICENSE)**. Use is **noncommercial only**; the canonical terms live in `LICENSE` at the repository root (and are declared in `pyproject.toml` as `PolyForm-Noncommercial-1.0.0`).

**If you commercially exploit this software in breach of that license, you’re a greed-blind parasite—so fuck right off.**  
**Don’t play dumb about the terms; you know exactly what kind of grift you’re running.**

## Provenance

This project was produced with approximately **99.9%** AI-assisted generation.
