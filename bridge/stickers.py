"""Bounded sticker conversion in a separate, credential-free worker process."""
import gzip
from io import BytesIO
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from .api import APIError, MAX_STICKER_BYTES

MAX_OUTPUT = 8 * 1024 * 1024
MAX_JSON = 2 * 1024 * 1024
SIDE = 320
FPS = 15
MAX_FRAMES = 45


def convert_sticker(content, sticker):
    if len(content) > MAX_STICKER_BYTES or sticker.get("file_size", 0) > MAX_STICKER_BYTES:
        raise APIError("Telegram", "STICKER_TOO_LARGE")
    kind = "tgs" if sticker.get("is_animated") else "webm" if sticker.get("is_video") else "static"
    output_kind = "png" if kind == "static" else "gif"
    with tempfile.TemporaryDirectory(prefix="bridge-sticker-") as directory:
        source, destination = Path(directory) / "input", Path(directory) / ("output." + output_kind)
        source.write_bytes(content)
        # Native decoders need no service credentials or access to a user's messages.
        env = {key: value for key, value in os.environ.items()
               if key.upper() in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "LANG")}
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        try:
            result = subprocess.run([sys.executable, "-m", "bridge.stickers", kind, str(source), str(destination)],
                                    cwd=directory, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=45,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except subprocess.TimeoutExpired:
            raise APIError("Sticker", "CONVERSION_TIMEOUT") from None
        except OSError:
            raise APIError("Sticker", "CONVERTER_UNAVAILABLE") from None
        if result.returncode or not destination.is_file():
            raise APIError("Sticker", "INVALID_OR_UNSUPPORTED_DATA")
        if not 0 < destination.stat().st_size <= MAX_OUTPUT:
            raise APIError("Sticker", "OUTPUT_TOO_LARGE")
        return destination.read_bytes(), output_kind


def opaque_frame(image):
    from PIL import Image
    rgba = image.convert("RGBA")
    white = Image.new("RGBA", rgba.size, "white")
    return Image.alpha_composite(white, rgba).convert("RGB")


def save_gif(frames, destination):
    if not frames:
        raise ValueError("No frames")
    # 60/70/70 ms preserves 15 fps using GIF's 10 ms time resolution.
    durations = [round((i + 1) * 100 / FPS) * 10 - round(i * 100 / FPS) * 10 for i in range(len(frames))]
    frames[0].save(destination, format="GIF", save_all=True, append_images=frames[1:],
                   duration=durations, loop=0, disposal=2, optimize=False)


def render_tgs(content, destination):
    from rlottie_python import LottieAnimation
    with gzip.GzipFile(fileobj=BytesIO(content)) as compressed:
        raw = compressed.read(MAX_JSON + 1)
    if len(raw) > MAX_JSON:
        raise ValueError("Animation too large")
    doc = json.loads(raw)
    width, height, fps = (float(doc[k]) for k in ("w", "h", "fr"))
    start, end = float(doc.get("ip", 0)), float(doc["op"])
    if not all(math.isfinite(n) for n in (width, height, fps, start, end)):
        raise ValueError("Invalid animation metadata")
    if not (1 <= width <= 512 and 1 <= height <= 512 and 1 <= fps <= 60 and 1 <= end - start <= fps * 3.05):
        raise ValueError("Invalid animation dimensions or duration")
    if doc.get("fonts") or any("p" in a or "u" in a for a in doc.get("assets", [])):
        raise ValueError("External resources are not allowed")
    animation = LottieAnimation.from_data(raw.decode("utf-8"), resource_path=str(destination.parent))
    frames = []
    try:
        duration = (end - start) / fps
        frame_count = min(MAX_FRAMES, max(1, math.ceil(duration * FPS)))
        for i in range(frame_count):
            frame_number = min(int(end - start) - 1, int(i * fps / FPS))
            frames.append(opaque_frame(animation.render_pillow_frame(frame_num=frame_number, width=SIDE, height=SIDE)))
        save_gif(frames, destination)
    finally:
        animation.lottie_animation_destroy()


def render_webm(source, destination):
    from PIL import Image
    # Fixed format/protocols prevent remote playlists; fixed output bounds memory.
    command = ["ffmpeg", "-nostdin", "-loglevel", "error", "-threads", "1", "-filter_threads", "1",
               "-protocol_whitelist", "file,pipe", "-f", "webm", "-c:v", "libvpx-vp9", "-i", str(source),
               "-an", "-sn", "-dn", "-t", "3", "-frames:v", str(MAX_FRAMES),
               "-vf", f"fps={FPS},scale={SIDE}:{SIDE}:force_original_aspect_ratio=decrease,pad={SIDE}:{SIDE}:(ow-iw)/2:(oh-ih)/2:color=white",
               "-pix_fmt", "rgba", "-f", "rawvideo", "pipe:1"]
    result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            timeout=30, check=True, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    frame_size = SIDE * SIDE * 4
    if not result.stdout or len(result.stdout) % frame_size or len(result.stdout) > frame_size * MAX_FRAMES:
        raise ValueError("Invalid video frames")
    frames = [opaque_frame(Image.frombytes("RGBA", (SIDE, SIDE), result.stdout[i:i+frame_size]))
              for i in range(0, len(result.stdout), frame_size)]
    save_gif(frames, destination)


def worker(kind, source, destination):
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = 512 * 512
    content = source.read_bytes()
    if not 0 < len(content) <= MAX_STICKER_BYTES:
        raise ValueError("Invalid size")
    if kind == "static":
        with Image.open(BytesIO(content)) as image:
            if image.format not in ("WEBP", "PNG") or max(image.size) > 512:
                raise ValueError("Invalid static sticker")
            opaque_frame(image).save(destination, format="PNG")
    elif kind == "tgs":
        render_tgs(content, destination)
    elif kind == "webm":
        render_webm(source, destination)
    else:
        raise ValueError("Invalid sticker type")


if __name__ == "__main__":
    try:
        worker(sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]))
    except Exception:
        sys.exit(2)
