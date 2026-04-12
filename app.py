import os, sys, uuid, subprocess, json, re, time, threading
from flask import Flask, request, jsonify, send_file, send_from_directory
import static_ffmpeg

# ── FFMPEG ──────────────────────────────────────────────────────────────
static_ffmpeg.add_paths()
FFMPEG  = "ffmpeg"
FFPROBE = "ffprobe"

# ── PATHS ───────────────────────────────────────────────────────────────
def get_base():
    if getattr(sys, 'frozen', False):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))

def get_exe_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

BASE     = get_base()
EXE_DIR  = get_exe_dir()
FRONTEND = BASE
UPLOADS  = os.path.join(EXE_DIR, "uploads")

os.makedirs(UPLOADS, exist_ok=True)

AUDIO_EXTS = {"mp3", "wav", "aac", "flac", "ogg", "m4a"}
VIDEO_EXTS = {"mp4", "mkv", "mov", "avi", "webm"}

# ── SILKUT FOLDER ────────────────────────────────────────────────────────
def get_silkut_folder():
    """Always returns ~/Downloads/Silkut, creating it if needed."""
    downloads = os.path.join(os.path.expanduser("~"), "Downloads")
    silkut = os.path.join(downloads, "Silkut")
    os.makedirs(silkut, exist_ok=True)
    return silkut

SILKUT_FOLDER = get_silkut_folder()
SETTINGS_PATH = os.path.join(SILKUT_FOLDER, "silkut_settings.json")

# ── SETTINGS ────────────────────────────────────────────────────────────
def load_settings():
    try:
        with open(SETTINGS_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_settings(data):
    try:
        os.makedirs(SILKUT_FOLDER, exist_ok=True)
        with open(SETTINGS_PATH, "w") as f:
            json.dump(data, f)
    except Exception:
        pass

def get_default_output_dir():
    return SILKUT_FOLDER

def get_output_dir():
    """Get output dir, always ensure it exists, fallback to default if saved one is gone."""
    settings = load_settings()
    folder = settings.get("output_folder", "").strip()
    if folder:
        try:
            os.makedirs(folder, exist_ok=True)
            return folder
        except Exception:
            pass
    default = get_default_output_dir()
    os.makedirs(default, exist_ok=True)
    return default

# ── FILE REGISTRY — tracks output files by job ID ───────────────────────
_file_registry = {}  # job_id -> absolute out_path

def register_file(job_id, path):
    _file_registry[job_id] = path

def resolve_file(job_id):
    return _file_registry.get(job_id)

# ── FLASK APP ────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder=FRONTEND)

# ── FRONTEND ────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory(FRONTEND, "app.html")

@app.route("/<path:filename>")
def static_files(filename):
    return send_from_directory(FRONTEND, filename)

# ── SETTINGS API ────────────────────────────────────────────────────────
@app.route("/settings", methods=["GET"])
def get_settings_api():
    folder = get_output_dir()
    return jsonify({"output_folder": folder})

@app.route("/settings/folder", methods=["POST"])
def set_folder():
    try:
        import webview
        windows = webview.windows
        if windows:
            result = windows[0].create_file_dialog(webview.FOLDER_DIALOG)
            if result and len(result) > 0:
                folder = result[0].strip()
                os.makedirs(folder, exist_ok=True)
                settings = load_settings()
                settings["output_folder"] = folder
                save_settings(settings)
                return jsonify({"ok": True, "folder": folder})
        return jsonify({"ok": False, "error": "No window available"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/settings/folder/reset", methods=["POST"])
def reset_folder():
    settings = load_settings()
    settings.pop("output_folder", None)
    save_settings(settings)
    folder = get_output_dir()
    return jsonify({"ok": True, "folder": folder})

# ── UPLOAD ──────────────────────────────────────────────────────────────
@app.route("/upload", methods=["POST"])
def upload():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file received. Please try again."}), 400

    ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
    ftype = "video" if ext in VIDEO_EXTS else "audio" if ext in AUDIO_EXTS else None
    if not ftype:
        return jsonify({"error": f"The format .{ext} is not supported."}), 400

    f.seek(0, 2)
    mb = f.tell() / 1048576
    f.seek(0)

    # Resolve output dir ONCE per job
    output_dir = get_output_dir()

    job_id   = str(uuid.uuid4())[:8]
    in_path  = os.path.join(UPLOADS, f"{job_id}.{ext}")
    out_ext  = "mp4" if ftype == "video" else ext
    out_name = f"{job_id}-silkut.{out_ext}"
    out_path = os.path.join(output_dir, out_name)

    f.save(in_path)

    try:
        thresh  = float(request.form.get("threshold",   -35))
        min_sil = float(request.form.get("min_silence",  0.3))
        padding = float(request.form.get("padding",      0.1))
    except (ValueError, TypeError):
        thresh, min_sil, padding = -35.0, 0.3, 0.1

    denoise = request.form.get("denoise", "0") == "1"

    t0 = time.time()
    try:
        res = process(in_path, out_path, ext, ftype, thresh, min_sil, padding, denoise)
    except Exception:
        _rm(in_path)
        return jsonify({"error": "Processing failed. The file may be corrupted or unsupported."}), 500

    _rm(in_path)
    elapsed = round(time.time() - t0, 1)

    # Register exact path — download/stream use this, never recalculate
    register_file(job_id, out_path)

    return jsonify({
        "job_id":            job_id,
        "download_url":      f"/download/{job_id}",
        "stream_url":        f"/stream/{job_id}",
        "file_type":         ftype,
        "original_duration": fmt_sec(res["orig"]),
        "output_duration":   fmt_sec(res["out"]),
        "removed_duration":  fmt_sec(res["removed"]),
        "saved_percent":     f"{res['pct']}%",
        "segments_removed":  res["n"],
        "process_time":      f"{elapsed}s",
        "file_size_mb":      round(mb, 1),
        "denoise_used":      denoise,
        "output_folder":     output_dir,
        "out_name":          out_name,
    })

# ── DOWNLOAD ────────────────────────────────────────────────────────────
@app.route("/download/<job_id>")
def download(job_id):
    path = resolve_file(job_id)
    if not path:
        return jsonify({"error": "File not found. It may have been moved or deleted."}), 404
    if not os.path.exists(path):
        return jsonify({"error": "Output file was not found on disk."}), 404
    return send_file(path, as_attachment=True, download_name=os.path.basename(path))

# ── STREAM ──────────────────────────────────────────────────────────────
@app.route("/stream/<job_id>", methods=["GET", "HEAD"])
def stream(job_id):
    path = resolve_file(job_id)
    if not path:
        return jsonify({"error": "File not found."}), 404
    if not os.path.exists(path):
        return jsonify({"error": "Output file was not found on disk."}), 404
    if request.method == "HEAD":
        return "", 200
    return send_file(path, as_attachment=False)

# ── CORE PROCESSING ──────────────────────────────────────────────────────
def process(in_path, out_path, ext, ftype, thresh, min_sil, padding, denoise):
    orig     = get_dur(in_path)
    silences = detect_silences(in_path, thresh, min_sil, orig, denoise)

    if not silences:
        run(["-i", in_path, "-c", "copy", out_path])
        return {"orig": orig, "out": get_dur(out_path), "removed": 0.0, "pct": 0.0, "n": 0}

    keep = build_keep(silences, orig, padding)

    if not keep:
        run(["-i", in_path, "-c", "copy", out_path])
        return {"orig": orig, "out": orig, "removed": 0.0, "pct": 0.0, "n": 0}

    if ftype == "video":
        cut_video(in_path, out_path, keep)
    else:
        cut_audio(in_path, out_path, ext, keep)

    out     = get_dur(out_path) if os.path.exists(out_path) else orig
    removed = max(0.0, orig - out)
    pct     = round(removed / orig * 100, 1) if orig > 0 else 0.0

    return {"orig": orig, "out": out, "removed": removed, "pct": pct, "n": len(silences)}

def detect_silences(in_path, thresh, min_sil, total, denoise=False):
    af = (
        f"highpass=f=150,afftdn=nf=-20,silencedetect=noise={thresh}dB:d={min_sil}"
        if denoise else
        f"silencedetect=noise={thresh}dB:d={min_sil}"
    )
    r = subprocess.run(
        [FFMPEG, "-i", in_path, "-af", af, "-f", "null", "-"],
        capture_output=True, text=True
    )
    starts = [float(x) for x in re.findall(r"silence_start:\s*([\d.eE+\-]+)", r.stderr)]
    ends   = [float(x) for x in re.findall(r"silence_end:\s*([\d.eE+\-]+)",   r.stderr)]
    silences = []
    for i, s in enumerate(starts):
        s = max(0.0, min(float(s), total))
        e = max(s,   min(float(ends[i]) if i < len(ends) else total, total))
        if e - s >= min_sil * 0.5:
            silences.append((s, e))
    return silences

def build_keep(silences, total, padding):
    remove = []
    for (s, e) in silences:
        rs, re_ = s + padding, e - padding
        if re_ - rs >= 0.02:
            remove.append([rs, re_])

    if not remove:
        return [(0.0, total)]

    remove.sort()
    merged = [remove[0]]
    for seg in remove[1:]:
        if seg[0] <= merged[-1][1] + 0.01:
            merged[-1][1] = max(merged[-1][1], seg[1])
        else:
            merged.append(seg)

    keep, cursor = [], 0.0
    for (rs, re_) in merged:
        if rs - cursor >= 0.02:
            keep.append((max(0.0, cursor), min(total, rs)))
        cursor = re_
    if total - cursor >= 0.02:
        keep.append((max(0.0, cursor), total))
    return keep

def cut_audio(in_path, out_path, ext, keep):
    n     = len(keep)
    parts = [f"[0:a]atrim=start={s:.6f}:end={e:.6f},asetpts=PTS-STARTPTS[a{i}]" for i, (s, e) in enumerate(keep)]
    ins   = "".join(f"[a{i}]" for i in range(n))
    parts.append(f"{ins}concat=n={n}:v=0:a=1[outa]")
    codec = {
        "mp3":  ["-c:a", "libmp3lame", "-q:a", "2"],
        "wav":  ["-c:a", "pcm_s16le"],
        "flac": ["-c:a", "flac"],
    }.get(ext, ["-c:a", "aac", "-b:a", "192k"])
    run(["-i", in_path, "-filter_complex", ";".join(parts), "-map", "[outa]", *codec, out_path])

def cut_video(in_path, out_path, keep):
    n, parts = len(keep), []
    for i, (s, e) in enumerate(keep):
        parts += [
            f"[0:v]trim=start={s:.6f}:end={e:.6f},setpts=PTS-STARTPTS[v{i}]",
            f"[0:a]atrim=start={s:.6f}:end={e:.6f},asetpts=PTS-STARTPTS[a{i}]",
        ]
    vi = "".join(f"[v{i}]" for i in range(n))
    ai = "".join(f"[a{i}]" for i in range(n))
    parts += [f"{vi}concat=n={n}:v=1:a=0[outv]", f"{ai}concat=n={n}:v=0:a=1[outa]"]
    run([
        "-i", in_path,
        "-filter_complex", ";".join(parts),
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        out_path,
    ])

def get_dur(path):
    r = subprocess.run(
        [FFPROBE, "-v", "quiet", "-print_format", "json", "-show_format", path],
        capture_output=True, text=True
    )
    return float(json.loads(r.stdout)["format"]["duration"])

def run(args):
    kwargs = {}
    if sys.platform == "win32":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        kwargs["startupinfo"] = si
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    r = subprocess.run([FFMPEG, "-y"] + args, capture_output=True, text=True, **kwargs)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-800:])

def _rm(p):
    try:
        if p and os.path.exists(p):
            os.remove(p)
    except Exception:
        pass

def fmt_sec(s):
    s = int(s)
    return f"{s // 60}m {s % 60}s" if s >= 60 else f"{s}s"

# ── LAUNCH ───────────────────────────────────────────────────────────────
def start_flask():
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    app.run(debug=False, port=8080, use_reloader=False, threaded=True)

if __name__ == "__main__":
    threading.Thread(target=start_flask, daemon=True).start()
    time.sleep(1.5)

    try:
        import webview
        window = webview.create_window(
            title     = "Silkut",
            url       = "http://127.0.0.1:8080",
            width     = 1100,
            height    = 720,
            min_size  = (800, 600),
            resizable = True,
        )
        webview.start()
    except Exception:
        import webbrowser
        webbrowser.open("http://127.0.0.1:8080")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
