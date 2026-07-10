import aiohttp
import asyncio
import re
import os
import zipfile
import uuid
import logging
from urllib.parse import urljoin, urlparse, urlunparse
from colorama import init, Fore, Style
from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

init(autoreset=True)

class ColoredFormatter(logging.Formatter):
    COLORS = {
        logging.DEBUG: Fore.CYAN,
        logging.INFO: Fore.GREEN,
        logging.WARNING: Fore.YELLOW,
        logging.ERROR: Fore.RED,
        logging.CRITICAL: Fore.RED + Style.BRIGHT,
    }

    def format(self, record):
        log_color = self.COLORS.get(record.levelno, Fore.WHITE)
        record.msg = f"{log_color}{record.msg}{Style.RESET_ALL}"
        return super().format(record)

logger = logging.getLogger('RobloxAssetAPI')
logger.setLevel(logging.DEBUG)
ch = logging.StreamHandler()
ch.setFormatter(ColoredFormatter('%(asctime)s - %(levelname)s - %(message)s', '%H:%M:%S'))
logger.addHandler(ch)

ROBLOX_COOKIE = os.getenv("ROBLOX_COOKIE")

app = FastAPI(title="RbxDownloader API")
os.makedirs("downloaded_assets", exist_ok=True)
app.mount("/downloads", StaticFiles(directory="downloaded_assets"), name="downloads")

tasks_db = {}

class DownloadRequest(BaseModel):
    asset_id: str
    format: str = "original"
    quality: str = "original"

def update_task(task_id: str, progress: int, message: str, status: str = "processing", result: dict = None):
    if task_id in tasks_db:
        tasks_db[task_id].update({
            "progress": progress,
            "message": message,
            "status": status,
            "result": result
        })

def load_fallback_games():
    place_ids = []
    if not os.path.exists("fallback-games.txt"):
        return place_ids
    with open("fallback-games.txt", "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            place_id = line.split("#", 1)[0].strip()
            if place_id.isdigit():
                place_ids.append(int(place_id))
    return place_ids

FALLBACK_GAMES = load_fallback_games()
NO_BINARY_TYPES = [21, 34]

def detect_file_extension(content: bytes, content_type: str, fallback_ext: str) -> str:
    if content.startswith(b'#EXTM3U'): return '.m3u8'
    if content.startswith(b'\x89PNG\r\n\x1a\n'): return '.png'
    if content.startswith(b'OggS'): return '.ogg'
    if content.startswith(b'\x1aE\xdf\xa3'): return '.webm'
    if content.startswith(b'<roblox!'): return '.rbxm'
    if content.startswith(b'<roblox'): return '.rbxmx'
    if content.startswith(b'version '): return '.mesh'
    if content.startswith(b'{"') or content.startswith(b'['): return '.json'
    return fallback_ext

async def fetch_asset_details(session: aiohttp.ClientSession, asset_id: str, cookie=None):
    url = f"https://economy.roproxy.com/v2/assets/{asset_id}/details"
    headers = {"Cookie": f".ROBLOSECURITY={cookie}"} if cookie else {}
    try:
        async with session.get(url, headers=headers) as response:
            if response.status == 200:
                return await response.json()
    except Exception:
        pass
    return None

async def fetch_asset_location(session: aiohttp.ClientSession, asset_id: str, place_id=None, cookie=None, universe_id=None):
    url = 'https://assetdelivery.roproxy.com/v2/assets/batch'
    body_array = [{"assetId": asset_id, "requestId": "0"}]
    headers = {
        "User-Agent": "Roblox/WinInet",
        "Content-Type": "application/json",
        "Roblox-Browser-Asset-Request": "false"
    }
    if cookie: headers["Cookie"] = f".ROBLOSECURITY={cookie}"
    if place_id: headers["Roblox-Place-Id"] = str(place_id)
    if universe_id: headers["Roblox-Universe-Id"] = str(universe_id)

    try:
        async with session.post(url, headers=headers, json=body_array) as response:
            if response.status == 200:
                locations = await response.json()
                if locations and len(locations) > 0 and locations[0].get("locations"):
                    return locations[0]["locations"][0]["location"]
    except Exception:
        pass
    return None

def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/*?"<>|]', '', name).replace(" ", "_")

async def convert_media(input_path: str, format_target: str, quality: str) -> str:
    if format_target == 'original' or not format_target:
        return input_path
        
    ext = format_target if format_target.startswith('.') else f".{format_target}"
    input_dir = os.path.dirname(input_path) or '.'
    input_name = os.path.basename(input_path)
    output_name = input_name.rsplit('.', 1)[0] + ext
    output_path = os.path.join(input_dir, output_name)

    cmd = ['ffmpeg', '-y', '-nostdin', '-i', input_name]
    
    if ext in ['.mp3', '.wav', '.ogg']:
        if ext == '.mp3': cmd.extend(['-c:a', 'libmp3lame'])
        elif ext == '.wav': cmd.extend(['-c:a', 'pcm_s16le'])
        if quality == 'high': cmd.extend(['-b:a', '320k'])
    elif ext in ['.mp4', '.webm']:
        if quality == '1080p': cmd.extend(['-vf', 'scale=-2:1080'])
        elif quality == '720p': cmd.extend(['-vf', 'scale=-2:720'])

    cmd.append(output_name)

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=os.path.abspath(input_dir)
        )
        await asyncio.wait_for(process.communicate(), timeout=300)
        
        if process.returncode == 0 and os.path.exists(output_path):
            os.remove(input_path)
            return output_path
    except Exception as e:
        logger.error(f"FFmpeg error: {e}")
        
    return input_path

async def download_core(session: aiohttp.ClientSession, asset_id: str, task_id: str):
    update_task(task_id, 10, "Buscando detalhes do Asset...")
    details = await fetch_asset_details(session, asset_id, ROBLOX_COOKIE)
    
    asset_name = str(asset_id)
    asset_type_id = None
    if details and "errors" not in details:
        asset_name = details.get("Name", str(asset_id))
        asset_type_id = details.get("AssetTypeId")

    sanitized_name = sanitize_filename(asset_name)
    if asset_type_id in NO_BINARY_TYPES:
        return None, "Este tipo de asset nao possui arquivo baixavel."

    update_task(task_id, 30, "Resolvendo URL de download (Bypass)...")
    asset_url = await fetch_asset_location(session, asset_id)
    
    if not asset_url:
        for place_id in FALLBACK_GAMES:
            asset_url = await fetch_asset_location(session, asset_id, place_id, ROBLOX_COOKIE)
            if asset_url: break

    if not asset_url:
        return None, "Asset inacessivel ou excluido."

    update_task(task_id, 50, "Baixando arquivo binario...")
    try:
        async with session.get(asset_url) as response:
            if response.status != 200:
                return None, f"Falha HTTP {response.status}"

            content_type = response.headers.get('Content-Type', '')
            first_chunk = await response.content.read(1024)
            final_ext = detect_file_extension(first_chunk, content_type, '.bin')

            file_path = os.path.join("downloaded_assets", f"{asset_id}_{sanitized_name}{final_ext}")
            
            with open(file_path, "wb") as f:
                f.write(first_chunk)
                async for chunk in response.content.iter_chunked(65536):
                    f.write(chunk)

            return file_path, None
            
    except Exception as e:
        return None, f"Erro na conexao: {str(e)}"

async def process_download_task(task_id: str, req: DownloadRequest):
    try:
        async with aiohttp.ClientSession() as session:
            file_path, error = await download_core(session, req.asset_id, task_id)
            
            if error:
                update_task(task_id, 100, error, status="error")
                return

            update_task(task_id, 70, "Aplicando conversoes e finalizando...")
            final_path = await convert_media(file_path, req.format, req.quality)
            
            filename = os.path.basename(final_path)
            
            update_task(task_id, 100, "Pronto! Download concluido.", status="completed", result={
                "url": f"/downloads/{filename}",
                "filename": filename
            })

    except Exception as e:
        logger.error(f"Task Error: {e}")
        update_task(task_id, 100, "Erro interno do servidor.", status="error")

@app.get("/")
async def read_index():
    return FileResponse("index.html")

@app.post("/api/download")
async def start_download(req: DownloadRequest, background_tasks: BackgroundTasks):
    clean_id = req.asset_id.strip()
    if not clean_id.isdigit():
        return JSONResponse(status_code=400, content={"error": "ID de Asset invalido. Use apenas numeros."})
        
    task_id = str(uuid.uuid4())
    tasks_db[task_id] = {
        "status": "processing",
        "progress": 0,
        "message": "Iniciando processo...",
        "result": None
    }
    
    background_tasks.add_task(process_download_task, task_id, req)
    return {"task_id": task_id}

@app.get("/api/status/{task_id}")
async def get_task_status(task_id: str):
    task = tasks_db.get(task_id)
    if not task:
        return JSONResponse(status_code=404, content={"error": "Tarefa nao encontrada"})
    return task

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
