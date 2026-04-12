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

# ── SETTINGS FILE (persists output folder choice) ───────────────────────
SETTINGS_PATH = os.path.join(EXE_DIR, "silkut_settings.json")

def load_settings():
    try:
        with open(SETTINGS_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_settings(data):
    try:
        with open(SETTINGS_PATH, "w") as f:
            json.dump(data, f)
    except Exception:
        pass

def get_default_output_dir():
    if sys.platform == "win32":
        desktop = os.path.join(os.path.expanduser("~"), "Desktop")
        if not os.path.isdir(desktop):
            desktop = os.path.join(os.path.expanduser("~"), "OneDrive", "Desktop")
        if not os.path.isdir(desktop):
            desktop = os.path.expanduser("~")
    else:
        desktop = os.path.join(os.path.expanduser("~"), "Desktop")
        if not os.path.isdir(desktop):
            desktop = os.path.expanduser("~")
    return os.path.join(desktop, "Silkut")

def get_output_dir():
    settings = load_settings()
    folder = settings.get("output_folder", "")
    if folder and os.path.isdir(folder):
        return folder
    return get_default_output_dir()

os.makedirs(UPLOADS, exist_ok=True)

AUDIO_EXTS = {"mp3", "wav", "aac", "flac", "ogg", "m4a"}
VIDEO_EXTS = {"mp4", "mkv", "mov", "avi", "webm"}

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
    settings = load_settings()
    output_folder = settings.get("output_folder", "")
    if not output_folder or not os.path.isdir(output_folder):
        output_folder = get_default_output_dir()
    return jsonify({"output_folder": output_folder})

@app.route("/settings/folder", methods=["POST"])
def set_folder():
    """Open a native folder picker and save the chosen path."""
    try:
        import webview
        windows = webview.windows
        if windows:
            result = windows[0].create_file_dialog(webview.FOLDER_DIALOG)
            if result and len(result) > 0:
                folder = result[0]
                settings = load_settings()
                settings["output_folder"] = folder
                save_settings(settings)
                os.makedirs(folder, exist_ok=True)
                return jsonify({"ok": True, "folder": folder})
        return jsonify({"ok": False, "error": "No window"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/settings/folder/manual", methods=["POST"])
def set_folder_manual():
    """Set folder from a manually typed path."""
    data = request.get_json(silent=True) or {}
    folder = data.get("folder", "").strip()
    if not folder:
        return jsonify({"ok": False, "error": "Empty path"})
    try:
        os.makedirs(folder, exist_ok=True)
        settings = load_settings()
        settings["output_folder"] = folder
        save_settings(settings)
        return jsonify({"ok": True, "folder": folder})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# ── UPLOAD ──────────────────────────────────────────────────────────────
@app.route("/upload", methods=["POST"])
def upload():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file received. Please try again."}), 400

    ext   = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
    ftype = "video" if ext in VIDEO_EXTS else "audio" if ext in AUDIO_EXTS else None
    if not ftype:
        return jsonify({"error": f"The format .{ext} is not supported."}), 400

    f.seek(0, 2)
    mb = f.tell() / 1048576
    f.seek(0)

    OUTPUTS = get_output_dir()
    os.makedirs(OUTPUTS, exist_ok=True)

    job      = str(uuid.uuid4())[:8]
    in_path  = os.path.join(UPLOADS, f"{job}.{ext}")
    out_ext  = "mp4" if ftype == "video" else ext
    out_name = f"{job}-silkut.{out_ext}"
    out_path = os.path.join(OUTPUTS, out_name)

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
        return jsonify({"error": "Processing failed. The file may be corrupted or in an unsupported encoding."}), 500

    _rm(in_path)
    elapsed = round(time.time() - t0, 1)

    return jsonify({
        "download_url":      f"/download/{out_name}",
        "stream_url":        f"/stream/{out_name}",
        "file_type":         ftype,
        "original_duration": fmt_sec(res["orig"]),
        "output_duration":   fmt_sec(res["out"]),
        "removed_duration":  fmt_sec(res["removed"]),
        "saved_percent":     f"{res['pct']}%",
        "segments_removed":  res["n"],
        "process_time":      f"{elapsed}s",
        "file_size_mb":      round(mb, 1),
        "denoise_used":      denoise,
        "output_path":       out_path,
        "output_folder":     OUTPUTS,
    })


# ── DOWNLOAD / STREAM ───────────────────────────────────────────────────
@app.route("/download/<n>")
def download(n):
    if ".." in n or "/" in n or "\\" in n:
        return "bad request", 400
    OUTPUTS = get_output_dir()
    path = os.path.join(OUTPUTS, n)
    if not os.path.exists(path):
        return jsonify({"error": "File not found"}), 404
    return send_file(path, as_attachment=True, download_name=n)

@app.route("/stream/<n>", methods=["GET", "HEAD"])
def stream(n):
    if ".." in n or "/" in n or "\\" in n:
        return "bad request", 400
    OUTPUTS = get_output_dir()
    path = os.path.join(OUTPUTS, n)
    if not os.path.exists(path):
        return jsonify({"error": "File not found"}), 404
    if request.method == "HEAD":
        return "", 200
    return send_file(path, as_attachment=False)


# ── CORE PROCESSING ─────────────────────────────────────────────────────
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
    # Hide console window on Windows
    kwargs = {}
    if sys.platform == "win32":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
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


# ── LAUNCH ──────────────────────────────────────────────────────────────
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
