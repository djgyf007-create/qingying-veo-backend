from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import requests
import sqlite3
from datetime import datetime
from typing import Optional
import traceback

app = FastAPI(title="清影 Veo 后端")

DB_PATH = "tasks.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_type TEXT, prompt TEXT, external_task_id TEXT,
        image_url TEXT, video_url TEXT, status TEXT, created_at TEXT
    )''')
    conn.commit()
    conn.close()

init_db()

class ImageRequest(BaseModel):
    prompt: str
    api_key: str
    size: str = "2K"
    aspect_ratio: str = "16:9"

class VideoRequest(BaseModel):
    prompt: str
    api_key: str
    size: str = "2K"
    aspect_ratio: str = "16:9"
    enhance_prompt: bool = True
    enable_upsample: bool = False
    reference_image: Optional[str] = None

def get_ratio_desc(aspect_ratio: str) -> str:
    if aspect_ratio == "9:16":
        return "竖屏 9:16"
    elif aspect_ratio == "16:9":
        return "横屏 16:9"
    elif aspect_ratio == "1:1":
        return "正方形 1:1"
    else:
        return "横屏 16:9"

@app.post("/api/generate-image")
async def generate_image(req: ImageRequest):
    print("[后端] 收到生图请求")
    try:
        ratio_desc = get_ratio_desc(req.aspect_ratio)
        enhanced_prompt = f"{req.prompt} 。【请严格生成 {ratio_desc} 比例】"

        payload = {
            "contents": [{"parts": [{"text": enhanced_prompt}]}],
            "generationConfig": {"responseModalities": ["IMAGE"]},
            "imageGenerationConfig": {"aspectRatio": req.aspect_ratio}
        }

        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": req.api_key
        }

        resp = requests.post(
            "https://ai.kegeai.top/v1beta/models/gemini-3.1-flash-image-preview:generateContent",
            headers=headers,
            json=payload,
            timeout=120
        )

        print(f"[后端] 图片上游状态码: {resp.status_code}")

        if resp.status_code != 200:
            print(f"[后端] 图片上游错误: {resp.text[:300]}")
            raise HTTPException(500, f"上游错误: {resp.text[:200]}")

        data = resp.json()
        image_url = None

        for candidate in data.get("candidates", []):
            for part in candidate.get("content", {}).get("parts", []):
                if part.get("inlineData", {}).get("data"):
                    image_url = "data:image/png;base64," + part["inlineData"]["data"]
                    break

        if not image_url:
            raise HTTPException(500, "上游未返回图片")

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT INTO tasks (task_type, prompt, image_url, status, created_at) VALUES (?,?,?,?,?)",
                  ("image", req.prompt, image_url, "completed", datetime.now().isoformat()))
        conn.commit()
        conn.close()

        return {"image_url": image_url}

    except Exception as e:
        print("[后端] 生图异常:")
        traceback.print_exc()
        raise HTTPException(500, str(e))


@app.post("/api/generate-video")
async def generate_video(req: VideoRequest):
    print("[后端] 收到生成视频请求")
    if not req.reference_image:
        raise HTTPException(400, "需要参考图片")

    payload = {
        "model": "veo3.1-fast-components",
        "prompt": req.prompt,
        "images": [req.reference_image],
        "enhance_prompt": req.enhance_prompt,
        "enable_upsample": req.enable_upsample,
        "aspect_ratio": req.aspect_ratio
    }
    headers = {"Authorization": f"Bearer {req.api_key}"}

    try:
        r = requests.post(
            "https://ai.kegeai.top/v1/video/create",
            headers=headers, json=payload, timeout=60
        )
        print(f"[后端] 视频创建接口状态码: {r.status_code}")

        if r.status_code != 200:
            print(f"[后端] 视频创建失败内容: {r.text[:400]}")
            raise HTTPException(500, f"创建视频任务失败: {r.text[:200]}")

        task_id = r.json().get("id")
        if not task_id:
            raise HTTPException(500, "创建任务失败，未返回 task_id")

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT INTO tasks (task_type, prompt, external_task_id, status, created_at) VALUES (?,?,?,?,?)",
                  ("video", req.prompt, task_id, "processing", datetime.now().isoformat()))
        conn.commit()
        conn.close()

        return {"task_id": task_id}

    except Exception as e:
        print("[后端] 生成视频异常:")
        traceback.print_exc()
        raise HTTPException(500, f"创建视频任务失败: {str(e)}")


@app.get("/api/video/status/{task_id}")
async def get_video_status(task_id: str, api_key: str = Query(...)):
    headers = {"Authorization": f"Bearer {api_key}"}
    url = f"https://ai.kegeai.top/v1/video/query?id={task_id}"
    r = requests.get(url, headers=headers, timeout=30)
    d = r.json()
    if d.get("video_url"):
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("UPDATE tasks SET status=?, video_url=? WHERE external_task_id=?",
                  ("completed", d["video_url"], task_id))
        conn.commit()
        conn.close()
    return d


@app.get("/api/tasks")
def get_tasks():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM tasks ORDER BY created_at DESC")
    rows = c.fetchall()
    conn.close()
    return [{"id":r[0],"type":r[1],"prompt":r[2],"task_id":r[3],"image":r[4],"video":r[5],"status":r[6],"time":r[7]} for r in rows]


@app.get("/admin", response_class=HTMLResponse)
def admin():
    return HTMLResponse("<h1>清影Veo 任务记录</h1><p>访问 /api/tasks 查看记录</p>")


@app.get("/", response_class=HTMLResponse)
def home():
    try:
        with open("static/index.html", "r", encoding="utf-8") as f:
            return f.read()
    except:
        return HTMLResponse("<h1>请确认 static/index.html 存在</h1>", status_code=404)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)