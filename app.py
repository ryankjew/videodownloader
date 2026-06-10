import os
import uuid
import threading
import subprocess
import shutil
import zipfile
import io
import json
import time
from pathlib import Path
from flask import Flask, request, jsonify, render_template, send_file, after_this_request

app = Flask(__name__)

WORK_DIR = Path("/tmp/videodownloader")
WORK_DIR.mkdir(exist_ok=True)

SUPPORTED_SITES = [
    "TikTok", "YouTube", "Instagram", "Twitter/X", "Facebook",
    "Reddit", "Vimeo", "Twitch", "Pinterest", "Dailymotion", "+1000 sites"
]

def job_path(job_id):
    return WORK_DIR / job_id / "job.json"

def read_job(job_id):
    p = job_path(job_id)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except:
            pass
    return None

def write_job(job):
    p = job_path(job["id"])
    p.parent.mkdir(exist_ok=True)
    p.write_text(json.dumps(job))

def session_path(session_id):
    return WORK_DIR / f"session_{session_id}.json"

def read_session(session_id):
    p = session_path(session_id)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except:
            pass
    return None

def write_session(sess):
    session_path(sess["id"]).write_text(json.dumps(sess))

def download_video(job_id, url, quality, audio_only):
    job_dir = WORK_DIR / job_id
    job_dir.mkdir(exist_ok=True)

    job = read_job(job_id) or {"id": job_id, "url": url}
    job.update({"status": "downloading", "progress": 5, "title": url[:60]})
    write_job(job)

    try:
        out_template = str(job_dir / "video.%(ext)s")
        cmd = ["yt-dlp", "--no-playlist", "--no-warnings", "--no-part"]

        if audio_only:
            cmd += ["-x", "--audio-format", "mp3", "--audio-quality", "0"]
        else:
            if quality == "720":
                cmd += ["-f", "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]"]
            elif quality == "480":
                cmd += ["-f", "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480]"]
            elif quality == "360":
                cmd += ["-f", "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/best[height<=360]"]
            else:
                cmd += ["-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"]
            cmd += ["--merge-output-format", "mp4"]

        cmd += ["--no-mtime", "-o", out_template, "--newline", url]

        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1
        )

        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            if "[download]" in line and "%" in line:
                try:
                    pct = float(line.split("%")[0].split()[-1])
                    job["progress"] = max(5, min(88, int(pct * 0.85)))
                    write_job(job)
                except:
                    pass
            if "[Merger]" in line or "[ExtractAudio]" in line:
                job["status"] = "processing"
                job["progress"] = 90
                write_job(job)

        proc.wait()

        if proc.returncode != 0:
            raise RuntimeError("Falha ao baixar. Verifique se o link é válido e público.")

        files = [f for f in job_dir.glob("*") if f.is_file() and f.name != "job.json"]
        if not files:
            raise RuntimeError("Arquivo não encontrado após download")

        output_file = max(files, key=lambda f: f.stat().st_size)
        ext = output_file.suffix.lower()
        title = url.split("/")[-1][:60] or "video"
        final_name = f"video_{job_id}{ext}"

        job.update({
            "status": "done",
            "progress": 100,
            "output_path": str(output_file),
            "filename": final_name,
            "filesize": output_file.stat().st_size,
            "title": title,
        })
        write_job(job)

        # Auto cleanup after 10 min
        def cleanup():
            time.sleep(600)
            shutil.rmtree(str(job_dir), ignore_errors=True)
        threading.Thread(target=cleanup, daemon=True).start()

    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        job["progress"] = 0
        write_job(job)


def run_session(session_id, max_workers=3):
    sess = read_session(session_id)
    if not sess:
        return
    sess["status"] = "running"
    write_session(sess)

    sem = threading.Semaphore(max_workers)
    threads = []

    def worker(item):
        with sem:
            download_video(item["job_id"], item["url"], sess["quality"], sess["audio_only"])

    for item in sess["items"]:
        t = threading.Thread(target=worker, args=(item,), daemon=True)
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    sess["status"] = "done"
    write_session(sess)

    def cleanup():
        time.sleep(600)
        p = session_path(session_id)
        if p.exists():
            p.unlink()
    threading.Thread(target=cleanup, daemon=True).start()


@app.route("/")
def index():
    return render_template("index.html", sites=SUPPORTED_SITES)

@app.route("/api/start", methods=["POST"])
def api_start():
    data = request.get_json()
    urls = data.get("urls", [])
    quality = data.get("quality", "best")
    audio_only = data.get("audio_only", False)

    urls = [u.strip() for u in urls if u.strip().startswith("http")]
    if not urls:
        return jsonify({"error": "Nenhuma URL válida"}), 400
    if len(urls) > 20:
        return jsonify({"error": "Máximo 20 vídeos por vez"}), 400

    session_id = str(uuid.uuid4())[:8]
    items = []
    for url in urls:
        job_id = str(uuid.uuid4())[:8]
        job = {"id": job_id, "url": url, "status": "queued", "progress": 0, "title": url[:60], "error": None}
        write_job(job)
        items.append({"job_id": job_id, "url": url})

    sess = {"id": session_id, "status": "pending", "items": items, "quality": quality, "audio_only": audio_only}
    write_session(sess)

    threading.Thread(target=run_session, args=(session_id,), daemon=True).start()
    return jsonify({"session_id": session_id, "job_ids": [i["job_id"] for i in items]})

@app.route("/api/session/<session_id>")
def api_session(session_id):
    sess = read_session(session_id)
    if not sess:
        return jsonify({"error": "Sessão não encontrada"}), 404
    items_out = []
    for item in sess["items"]:
        j = read_job(item["job_id"]) or {}
        items_out.append({
            "job_id": item["job_id"],
            "url": item["url"],
            "status": j.get("status", "queued"),
            "progress": j.get("progress", 0),
            "title": j.get("title", item["url"][:50]),
            "filename": j.get("filename"),
            "filesize": j.get("filesize"),
            "error": j.get("error"),
        })
    return jsonify({"id": session_id, "status": sess["status"], "items": items_out})

@app.route("/api/get/<job_id>")
def api_get(job_id):
    j = read_job(job_id)
    if not j or j.get("status") != "done":
        return jsonify({"error": "Arquivo não disponível"}), 404
    output_path = Path(j["output_path"])
    if not output_path.exists():
        return jsonify({"error": "Arquivo não encontrado no disco"}), 404
    filename = j.get("filename", "video.mp4")
    ext = output_path.suffix.lower()
    mime = "audio/mpeg" if ext == ".mp3" else "video/mp4"

    @after_this_request
    def delete_after(response):
        def remove():
            time.sleep(3)
            try: output_path.unlink()
            except: pass
        threading.Thread(target=remove, daemon=True).start()
        return response

    return send_file(str(output_path), as_attachment=True, download_name=filename, mimetype=mime)

@app.route("/api/download-all/<session_id>")
def api_download_all(session_id):
    sess = read_session(session_id)
    if not sess:
        return jsonify({"error": "Sessão não encontrada"}), 404
    buf = io.BytesIO()
    count = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for item in sess["items"]:
            j = read_job(item["job_id"]) or {}
            if j.get("status") == "done":
                op = Path(j.get("output_path", ""))
                if op.exists():
                    zf.write(str(op), j.get("filename", op.name))
                    count += 1
    if count == 0:
        return jsonify({"error": "Nenhum arquivo pronto"}), 404
    buf.seek(0)
    return send_file(buf, as_attachment=True,
                     download_name=f"clipador_{session_id}.zip",
                     mimetype="application/zip")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n🎬 Clipador — porta {port}\n")
    app.run(host="0.0.0.0", port=port, threaded=True)
