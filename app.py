import os
import re
import uuid
import zipfile
import asyncio
import aiohttp
from urllib.parse import urljoin, urlparse, urlunparse
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel
import uvicorn

ROBLOX_COOKIE = os.getenv("ROBLOX_COOKIE", "")
NO_BINARY_TYPES = [21, 34]

def load_fallback_games():
    place_ids = []
    if not os.path.exists("fallback-games.txt"):
        return place_ids
    with open("fallback-games.txt", "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            place_id = line.split("#", 1)[0].strip()
            if place_id.isdigit():
                place_ids.append(int(place_id))
    return place_ids

FALLBACK_GAMES = load_fallback_games()

app = FastAPI()
tasks = {}

class DownloadRequest(BaseModel):
    asset_ids: str

class OptionsRequest(BaseModel):
    audio_fmt: str = "original"
    audio_qual: str = "original"
    video_fmt: str = "original"
    video_qual: str = "original"

def detect_file_extension(content: bytes, content_type: str, fallback_ext: str) -> str:
    if content.startswith(b'#EXTM3U'): return '.m3u8'
    if content.startswith(b'\x89PNG\r\n\x1a\n'): return '.png'
    if content.startswith(b'OggS'): return '.ogg'
    if content.startswith(b'\x1aE\xdf\xa3'): return '.webm'
    if content.startswith(b'<roblox!'): return '.rbxm'
    if content.startswith(b'<roblox'): return '.rbxmx'
    if content.startswith(b'version '): return '.mesh'
    if content.startswith(b'{"') or content.startswith(b'['): return '.json'
    
    ctype = content_type.lower()
    if 'image/png' in ctype: return '.png'
    if 'audio/ogg' in ctype: return '.ogg'
    if 'video/webm' in ctype: return '.webm'
    if 'application/xml' in ctype: return '.rbxmx'
    if 'application/json' in ctype: return '.json'
    if 'text/plain' in ctype: return '.txt'
    return fallback_ext

async def fetch_creator_games(session: aiohttp.ClientSession, creator_id: int, creator_type: str):
    games_info = []
    url = f"https://games.roproxy.com/v2/groups/{creator_id}/games?accessFilter=2&sortOrder=Asc&limit=50" if creator_type == "Group" else f"https://games.roproxy.com/v2/users/{creator_id}/games?accessFilter=2&sortOrder=Asc&limit=50"
    try:
        async with session.get(url) as response:
            if response.status == 200:
                data = await response.json()
                for game in data.get("data", []):
                    pid = game["rootPlace"]["id"] if "rootPlace" in game and "id" in game["rootPlace"] else None
                    uid = game.get("id")
                    if pid or uid:
                        games_info.append({"place_id": pid, "universe_id": uid})
    except Exception:
        pass
    return games_info

async def fetch_asset_details(session: aiohttp.ClientSession, asset_id: str, cookie=None, max_retries=10):
    url = f"https://economy.roproxy.com/v2/assets/{asset_id}/details"
    headers = {}
    if cookie:
        headers["Cookie"] = f".ROBLOSECURITY={cookie}"
    for attempt in range(max_retries):
        try:
            async with session.get(url, headers=headers) as response:
                if response.status in [200, 400, 403]:
                    return await response.json()
                elif response.status == 429:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                else:
                    break
        except Exception:
            await asyncio.sleep(0.5)
    return None

async def fetch_asset_location(session: aiohttp.ClientSession, asset_id: str, place_id=None, cookie=None, universe_id=None):
    url = 'https://assetdelivery.roproxy.com/v2/assets/batch'
    body_array = [{"assetId": asset_id, "requestId": "0"}]
    headers = {
        "User-Agent": "Roblox/WinInet",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Roblox-Browser-Asset-Request": "false"
    }
    if cookie: headers["Cookie"] = f".ROBLOSECURITY={cookie}"
    if place_id: headers["Roblox-Place-Id"] = str(place_id)
    if universe_id: headers["Roblox-Universe-Id"] = str(universe_id)

    try:
        async with session.post(url, headers=headers, json=body_array) as response:
            if response.status == 200:
                locations = await response.json()
                if locations and len(locations) > 0:
                    obj = locations[0]
                    if obj.get("locations") and obj["locations"][0].get("location"):
                        return obj["locations"][0]["location"]
    except Exception:
        pass
    return None

def sanitize_filename(name: str) -> str:
    sanitized = re.sub(r'[\\/*?"<>|]', '', name)
    return sanitized.replace(" ", "_")

async def convert_media(input_path: str, format: str, quality: str) -> str:
    if not format or (input_path.endswith(format) and quality == 'original'):
        return input_path
    input_dir = os.path.dirname(input_path) or '.'
    input_name = os.path.basename(input_path)
    temp_output_name = input_name.rsplit('.', 1)[0] + "_mod" + format
    temp_output_path = os.path.join(input_dir, temp_output_name)
    cmd = ['ffmpeg', '-y', '-nostdin', '-i', input_name]
    is_audio = format in ['.mp3', '.wav', '.ogg', '.flac']
    if is_audio:
        if format == '.mp3': cmd.extend(['-c:a', 'libmp3lame'])
        elif format == '.wav': cmd.extend(['-c:a', 'pcm_s16le'])
        elif format == '.ogg': cmd.extend(['-c:a', 'libvorbis'])
        elif format == '.flac': cmd.extend(['-c:a', 'flac'])
        if format not in ['.wav', '.flac']:
            if quality == 'high': cmd.extend(['-b:a', '320k'])
            elif quality == 'medium': cmd.extend(['-b:a', '192k'])
            elif quality == 'low': cmd.extend(['-b:a', '128k'])
            elif quality == 'original' and format == '.mp3': cmd.extend(['-q:a', '2'])
    else:
        if format in ['.mp4', '.mov', '.webm']:
            if quality == '1080p': cmd.extend(['-vf', 'scale=-2:1080'])
            elif quality == '720p': cmd.extend(['-vf', 'scale=-2:720'])
            elif quality == '480p': cmd.extend(['-vf', 'scale=-2:480'])
    cmd.append(temp_output_name)
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=os.path.abspath(input_dir)
        )
        try:
            await asyncio.wait_for(process.communicate(), timeout=900)
        except asyncio.TimeoutError:
            try: process.kill()
            except Exception: pass
            return input_path
        if process.returncode != 0:
            return input_path
        if os.path.exists(temp_output_path) and os.path.getsize(temp_output_path) > 0:
            try:
                os.remove(input_path)
                final_output_path = os.path.join(input_dir, input_name.rsplit('.', 1)[0] + format)
                os.rename(temp_output_path, final_output_path)
                return final_output_path
            except Exception:
                return temp_output_path
    except Exception:
        pass
    return input_path

async def process_hls_playlist(session: aiohttp.ClientSession, m3u8_path: str, base_url: str) -> str:
    try:
        with open(m3u8_path, 'r', encoding='utf-8') as f:
            m3u8_content = f.read()
        lines = m3u8_content.splitlines()
        rbx_base_uri = None
        for line in lines:
            match = re.search(r'#EXT-X-DEFINE:NAME="RBX-BASE-URI",VALUE="([^"]+)"', line)
            if match:
                rbx_base_uri = match.group(1)
                if not rbx_base_uri.endswith('/'): rbx_base_uri += '/'
                break
        best_playlist_url = None
        streams = []
        for i, line in enumerate(lines):
            if line.startswith('#EXT-X-STREAM-INF'):
                if i + 1 < len(lines):
                    streams.append((line, lines[i+1]))
        if streams:
            best_stream = None
            max_height = -1
            for info, url in streams:
                res_match = re.search(r'RESOLUTION=\d+x(\d+)', info)
                if res_match:
                    height = int(res_match.group(1))
                    if height > max_height:
                        max_height = height
                        best_stream = (info, url)
            if best_stream:
                best_playlist_url = best_stream[1]
            else:
                best_playlist_url = streams[0][1]
                for info, url in streams:
                    if '720' in info or '720' in url:
                        best_playlist_url = url
                        break
        def get_url_with_auth(base_path, target_path, master_url):
            joined = urljoin(base_path, target_path)
            parsed_joined = urlparse(joined)
            parsed_master = urlparse(master_url)
            if not urlparse(target_path).query:
                if parsed_joined.netloc == parsed_master.netloc:
                    joined = urlunparse(parsed_joined._replace(query=parsed_master.query))
            return joined

        headers = {"User-Agent": "Mozilla/5.0"}
        if not best_playlist_url:
            best_playlist_url = base_url
            internal_m3u8_content = m3u8_content
        else:
            if "{$RBX-BASE-URI}" in best_playlist_url and rbx_base_uri:
                best_playlist_url = best_playlist_url.replace("{$RBX-BASE-URI}", rbx_base_uri.rstrip("/"))
            else:
                best_playlist_url = get_url_with_auth(base_url, best_playlist_url, base_url)
            async with session.get(best_playlist_url, headers=headers) as resp:
                if resp.status != 200: return None
                internal_m3u8_content = await resp.text()

        segments = [line for line in internal_m3u8_content.splitlines() if line and not line.startswith('#')]
        if not segments: return None

        output_dir = os.path.dirname(m3u8_path) or '.'
        base_name = os.path.basename(m3u8_path).rsplit('.', 1)[0]
        segment_files = []
        segments_base_path = best_playlist_url

        for i, seg in enumerate(segments):
            seg_url = get_url_with_auth(segments_base_path, seg, base_url)
            clean_url = seg_url.split('?')[0]
            filename = clean_url.split('/')[-1]
            ext = '.' + filename.split('.')[-1] if '.' in filename else '.webm'
            seg_path = os.path.join(output_dir, f"{base_name}_seg_{i:04d}{ext}")
            async with session.get(seg_url, headers=headers) as resp:
                if resp.status == 200:
                    with open(seg_path, 'wb') as f:
                        async for chunk in resp.content.iter_chunked(65536):
                            f.write(chunk)
                    segment_files.append(seg_path)

        if not segment_files: return None

        list_name = f"{base_name}_list.txt"
        list_path = os.path.join(output_dir, list_name)
        with open(list_path, 'w', encoding='utf-8') as f:
            for sf in segment_files:
                f.write(f"file '{os.path.basename(sf)}'\n")

        webm_name = f"{base_name}.webm"
        webm_output = os.path.join(output_dir, webm_name)
        cmd = ['ffmpeg', '-y', '-nostdin', '-f', 'concat', '-safe', '0', '-i', list_name, '-c', 'copy', webm_name]
        
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, 
            stderr=asyncio.subprocess.PIPE,
            cwd=os.path.abspath(output_dir)
        )
        try:
            await asyncio.wait_for(process.communicate(), timeout=900)
        except asyncio.TimeoutError:
            try: process.kill()
            except Exception: pass
            return None
        if process.returncode != 0:
            return None
        try:
            os.remove(m3u8_path)
            os.remove(list_path)
            for sf in segment_files: os.remove(sf)
        except Exception:
            pass
        return webm_output
    except Exception:
        return None

async def fetch_version_fallback(session: aiohttp.ClientSession, asset_id: str, cookie: str = None, max_versions=10):
    for version in range(1, max_versions + 1):
        url = f"https://assetdelivery.roproxy.com/v1/asset/?id={asset_id}&version={version}"
        headers = {"User-Agent": "Roblox/WinInet", "Roblox-Browser-Asset-Request": "false"}
        if cookie: headers["Cookie"] = f".ROBLOSECURITY={cookie}"
        try:
            async with session.get(url, headers=headers, allow_redirects=True) as response:
                if response.status == 200:
                    content_type = response.headers.get('Content-Type', '')
                    if 'text/html' not in content_type.lower() and 'application/json' not in content_type.lower():
                        return url
        except Exception:
            pass
        await asyncio.sleep(0.5)
    return None

async def download_public_video(session: aiohttp.ClientSession, asset_id: str, cookie: str, sanitized_name: str):
    url = "https://assetdelivery.roproxy.com/v2/asset"
    params = {"Id": asset_id, "ContentRepresentationPriorityList": "W3siZm9ybWF0IjoiaGxzIiwibWFqb3JWZXJzaW9uIjoiMSIsImZpZGVsaXR5IjoibWFpbiJ9XQ=="}
    headers = {"User-Agent": "Mozilla/5.0"}
    if cookie: headers["Cookie"] = f".ROBLOSECURITY={cookie}"
    async with session.get(url, params=params, headers=headers) as resp:
        if resp.status != 200: return None, f"HTTP Failure {resp.status}"
        try: data = await resp.json()
        except Exception: return None, "Invalid JSON response"
        if not data.get("locations") or not data["locations"][0].get("location"):
            return None, "Empty manifest"
        manifest_url = data["locations"][0]["location"]

    parts = manifest_url.split("/manifest.m3u8")
    base_url = parts[0]
    query = parts[1] if len(parts) > 1 else ""
    os.makedirs("downloaded_assets", exist_ok=True)
    base_name = f"{asset_id}_{sanitized_name}"
    
    i = 0
    downloaded_parts = []
    while True:
        part_url = f"{base_url}/720/{i:04d}.webm{query}"
        async with session.get(part_url, headers=headers) as r:
            if r.status != 200: break
            part_filename = os.path.join("downloaded_assets", f"{base_name}_part_{i:04d}.webm")
            with open(part_filename, "wb") as f:
                async for chunk in r.content.iter_chunked(65536): f.write(chunk)
            downloaded_parts.append(part_filename)
        i += 1
    if not downloaded_parts: return None, "No parts found"

    list_filename = os.path.join("downloaded_assets", f"{base_name}_list.txt")
    with open(list_filename, "w", encoding='utf-8') as f:
        for p in downloaded_parts: f.write(f"file '{os.path.basename(p)}'\n")

    output_filename = os.path.join("downloaded_assets", f"{base_name}.webm")
    cmd = ["ffmpeg", "-y", "-nostdin", "-f", "concat", "-safe", "0", "-i", os.path.basename(list_filename), "-c", "copy", os.path.basename(output_filename)]
    process = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=os.path.abspath("downloaded_assets"))
    try:
        await asyncio.wait_for(process.communicate(), timeout=900)
    except asyncio.TimeoutError:
        try: process.kill()
        except Exception: pass
        return None, "FFmpeg Timeout"

    try:
        os.remove(list_filename)
        for p in downloaded_parts: os.remove(p)
    except Exception: pass
    if process.returncode == 0 and os.path.exists(output_filename): return output_filename, None
    return None, f"FFmpeg failure (code {process.returncode})"

async def download_core(session: aiohttp.ClientSession, asset_id: str):
    details = await fetch_asset_details(session, asset_id, ROBLOX_COOKIE)
    asset_name = str(asset_id)
    asset_type_id = None
    creator_id = None
    creator_type = None
    is_public = False
    if details and "errors" not in details:
        asset_name = details.get("Name", str(asset_id))
        asset_type_id = details.get("AssetTypeId")
        creator = details.get("Creator", {})
        creator_id = creator.get("CreatorTargetId")
        creator_type = creator.get("CreatorType")
        is_public = details.get("IsPublicDomain", False)

    sanitized_name = sanitize_filename(asset_name)
    if asset_type_id in NO_BINARY_TYPES:
        return None, "No binary file type."

    if is_public and asset_type_id == 62:
        return await download_public_video(session, asset_id, ROBLOX_COOKIE, sanitized_name)

    asset_url = None
    if asset_type_id:
        asset_url = await fetch_asset_location(session, asset_id)
        if not asset_url:
            if creator_id:
                games_info = await fetch_creator_games(session, creator_id, creator_type)
                if games_info:
                    for g in games_info:
                        if g.get("place_id"):
                            asset_url = await fetch_asset_location(session, asset_id, g["place_id"], ROBLOX_COOKIE)
                            if asset_url: break
                        if g.get("universe_id"):
                            asset_url = await fetch_asset_location(session, asset_id, None, ROBLOX_COOKIE, g["universe_id"])
                            if asset_url: break

    if not asset_url:
        asset_url = await fetch_version_fallback(session, asset_id, ROBLOX_COOKIE)
        if not asset_url:
            for place_id in FALLBACK_GAMES:
                test_url = await fetch_asset_location(session, asset_id, place_id, ROBLOX_COOKIE)
                if test_url:
                    asset_url = test_url
                    break

    if not asset_url: return None, "Asset inaccessible or deleted."

    try:
        async with session.get(asset_url) as response:
            if response.status != 200: return None, f"HTTP {response.status}."
            content_type = response.headers.get('Content-Type', '')
            if 'text/html' in content_type.lower() or 'application/json' in content_type.lower(): return None, "Invalid file returned."
            first_chunk = await response.content.read(1024)
            if not first_chunk: return None, "Empty file."
            final_ext = detect_file_extension(first_chunk, content_type, '.bin')
            os.makedirs("downloaded_assets", exist_ok=True)
            file_path = os.path.join("downloaded_assets", f"{asset_id}_{sanitized_name}{final_ext}")
            with open(file_path, "wb") as f:
                f.write(first_chunk)
                async for chunk in response.content.iter_chunked(65536): f.write(chunk)
            if final_ext == '.m3u8':
                hls_webm_path = await process_hls_playlist(session, file_path, asset_url)
                if not hls_webm_path: return None, "Failed to rebuild HLS."
                file_path = hls_webm_path
            return file_path, None
    except Exception as e:
        return None, str(e)

async def process_task(task_id: str, ids_list: list):
    task = tasks[task_id]
    downloaded_files = []
    errors = []
    task["status"] = "processing"
    
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=600)) as session:
        for idx, aid in enumerate(ids_list):
            if task.get("canceled"): break
            task["message"] = f"Downloading {idx+1}/{len(ids_list)}..."
            task["progress"] = int(((idx) / len(ids_list)) * 40)
            res, err = await download_core(session, aid)
            if res: downloaded_files.append(res)
            else: errors.append(err)
            
    if task.get("canceled"):
        for f in downloaded_files:
            if os.path.exists(f): os.remove(f)
        return

    if not downloaded_files:
        task["status"] = "error"
        task["message"] = f"Failed all downloads. {errors[0] if errors else ''}"
        return

    task["files"] = downloaded_files
    has_a = any(f.endswith('.ogg') for f in downloaded_files)
    has_v = any(f.endswith('.webm') for f in downloaded_files)
    
    if has_a or has_v:
        task["status"] = "needs_input"
        task["has_audio"] = has_a
        task["has_video"] = has_v
        task["message"] = "Waiting for conversion choices..."
        task["progress"] = 50
        try:
            await asyncio.wait_for(task["event"].wait(), timeout=300)
        except asyncio.TimeoutError:
            task["status"] = "error"
            task["message"] = "Session expired."
            for f in downloaded_files:
                if os.path.exists(f): os.remove(f)
            return

    if task.get("canceled"):
        for f in downloaded_files:
            if os.path.exists(f): os.remove(f)
        return

    task["status"] = "processing"
    task["message"] = "Converting files..."
    new_files = []
    total = len(downloaded_files)
    for idx, f in enumerate(downloaded_files):
        if task.get("canceled"): break
        task["progress"] = 50 + int((idx / total) * 30)
        if f.endswith('.ogg') and task.get("audio_fmt"):
            f = await convert_media(f, task["audio_fmt"], task["audio_qual"])
        elif f.endswith('.webm') and task.get("video_fmt"):
            f = await convert_media(f, task["video_fmt"], task["video_qual"])
        new_files.append(f)
        
    downloaded_files = new_files
    task["files"] = downloaded_files

    if task.get("canceled"):
        for f in downloaded_files:
            if os.path.exists(f): os.remove(f)
        return

    task["message"] = "Finalizing..."
    task["progress"] = 90

    if len(ids_list) > 1 or len(downloaded_files) > 1:
        zip_filename = os.path.join("downloaded_assets", f"batch_{uuid.uuid4().hex[:8]}.zip")
        def create_zip():
            with zipfile.ZipFile(zip_filename, 'w', zipfile.ZIP_DEFLATED) as zipf:
                for file in downloaded_files:
                    if os.path.exists(file):
                        zipf.write(file, os.path.basename(file))
        await asyncio.to_thread(create_zip)
        for f in downloaded_files:
            if os.path.exists(f): os.remove(f)
        task["final_file"] = zip_filename
        task["filename"] = os.path.basename(zip_filename)
    else:
        task["final_file"] = downloaded_files[0]
        task["filename"] = os.path.basename(downloaded_files[0])

    task["progress"] = 100
    task["status"] = "completed"
    task["result"] = {"url": f"/api/download/{task_id}", "filename": task["filename"]}

@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    with open("index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

@app.post("/api/download")
async def start_download(req: DownloadRequest, background_tasks: BackgroundTasks):
    raw_ids = [x.strip() for x in req.asset_ids.split(',') if x.strip()]
    ids_list = []
    for x in raw_ids:
        if x.isdigit() and x not in ids_list: ids_list.append(x)
    if not ids_list:
        return {"error": "No valid IDs provided."}
    if len(ids_list) > 20:
        return {"error": "Maximum limit of 20 assets per batch."}
    task_id = str(uuid.uuid4())
    tasks[task_id] = {
        "status": "starting",
        "progress": 0,
        "message": "Initializing...",
        "files": [],
        "event": asyncio.Event(),
        "has_audio": False,
        "has_video": False,
        "canceled": False
    }
    background_tasks.add_task(process_task, task_id, ids_list)
    return {"task_id": task_id}

@app.get("/api/status/{task_id}")
async def get_status(task_id: str):
    if task_id not in tasks:
        return {"status": "error", "message": "Task not found."}
    t = tasks[task_id]
    return {
        "status": t["status"],
        "progress": t["progress"],
        "message": t["message"],
        "has_audio": t.get("has_audio", False),
        "has_video": t.get("has_video", False),
        "result": t.get("result")
    }

@app.post("/api/options/{task_id}")
async def set_options(task_id: str, opts: OptionsRequest):
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    t = tasks[task_id]
    t["audio_fmt"] = opts.audio_fmt
    t["audio_qual"] = opts.audio_qual
    t["video_fmt"] = opts.video_fmt
    t["video_qual"] = opts.video_qual
    if "event" in t:
        t["event"].set()
    return {"success": True}

@app.post("/api/cancel/{task_id}")
async def cancel_task(task_id: str):
    if task_id in tasks:
        tasks[task_id]["canceled"] = True
        tasks[task_id]["status"] = "canceled"
        tasks[task_id]["message"] = "Task canceled."
        if "event" in tasks[task_id]:
            tasks[task_id]["event"].set()
        return {"success": True}
    return {"error": "Task not found."}

@app.get("/api/download/{task_id}")
async def serve_file(task_id: str):
    if task_id not in tasks or tasks[task_id]["status"] != "completed":
        raise HTTPException(status_code=404, detail="File not ready")
    file_path = tasks[task_id]["final_file"]
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found on disk")
    return FileResponse(path=file_path, filename=tasks[task_id]["filename"])

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
