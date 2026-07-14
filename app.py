from flask import Flask, request, send_file, jsonify
import yt_dlp
import tempfile
import os

app = Flask(__name__)

COOKIE_FILE_PATH = "cookies.txt"

if not os.path.exists(COOKIE_FILE_PATH):
    COOKIE_FILE_PATH = None

@app.route("/")
def home():
    return {"status": "online", "cookies_carregados": bool(COOKIE_FILE_PATH)}

@app.route("/mp3")
def mp3():
    url = request.args.get("url")

    if not url:
        return jsonify({"error": "Parâmetro url não informado"}), 400

    temp_dir = tempfile.mkdtemp()
    outtmpl_path = os.path.join(temp_dir, "%(id)s.%(ext)s")

    ydl_opts = {
        "format": "bestaudio[ext=m4a]/bestaudio/bestvideo+bestaudio/best",
        "outtmpl": outtmpl_path,
        "quiet": True,
        "noplaylist": True,
        "js_runtimes": {"deno": {}}, 
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "128"
            }
        ]
    }    

    if COOKIE_FILE_PATH:
        ydl_opts["cookiefile"] = COOKIE_FILE_PATH

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            video_id = info.get("id")
            file_path = os.path.join(temp_dir, f"{video_id}.mp3")

        if not os.path.exists(file_path):
            raise Exception("Falha ao gerar o arquivo MP3.")

        return send_file(
            file_path,
            as_attachment=True,
            download_name=f"{video_id}.mp3",
            mimetype="audio/mpeg"
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/search")
def search():
    q = request.args.get("q")

    if not q:
        return jsonify({"error": "Parâmetro q não informado"}), 400

    ydl_opts = {
        "quiet": True,
        "extract_flat": True,
        "js_runtimes": {"deno": {}} 
    }
    
    if COOKIE_FILE_PATH:
        ydl_opts["cookiefile"] = COOKIE_FILE_PATH

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch10:{q}", download=False)
            
        results = []
        if "entries" in info:
            for entry in info["entries"]:
                thumbnails = entry.get("thumbnails", [])
                thumbnail_url = thumbnails[-1].get("url") if thumbnails else entry.get("thumbnail")

                results.append({
                    "id": entry.get("id"),
                    "title": entry.get("title"),
                    "url": entry.get("url"),
                    "duration": entry.get("duration"),
                    "view_count": entry.get("view_count"),
                    "channel": entry.get("uploader"),
                    "thumbnail": thumbnail_url
                })
                
        return jsonify({"results": results})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/info")
def info():
    url = request.args.get("url")

    if not url:
        return jsonify({"error": "Parâmetro url não informado"}), 400

    ydl_opts = {
        "quiet": True,
        "extract_flat": False,
        "js_runtimes": {"deno": {}} 
    }
    
    if COOKIE_FILE_PATH:
        ydl_opts["cookiefile"] = COOKIE_FILE_PATH

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            entry = ydl.extract_info(url, download=False)
            
            thumbnails = entry.get("thumbnails", [])
            thumbnail_url = thumbnails[-1].get("url") if thumbnails else entry.get("thumbnail")

            video_info = {
                "id": entry.get("id"),
                "title": entry.get("title"),
                "url": entry.get("webpage_url") or url,
                "duration": entry.get("duration"),
                "view_count": entry.get("view_count"),
                "like_count": entry.get("like_count"),
                "channel": entry.get("uploader"),
                "channel_url": entry.get("channel_url"),
                "description": entry.get("description"),
                "upload_date": entry.get("upload_date"),
                "thumbnail": thumbnail_url
            }

        return jsonify(video_info)

    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
