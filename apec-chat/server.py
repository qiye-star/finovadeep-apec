"""APEC 专题对话服务：接收专题页拼好的上下文，调用大模型，流式返回纯文本。

主模型走 OpenAI 兼容协议（默认 DeepSeek），首个字之前失败则切千问；key 只从环境变量读。
环境变量：
  OPENAI_API_KEY / OPENAI_BASE_URL / APEC_MODEL         主模型（默认 https://api.deepseek.com、deepseek-chat）
  DASHSCOPE_API_KEY / APEC_FALLBACK_MODEL               备用模型（默认 qwen-plus）
  APEC_ALLOW_ORIGINS                                    允许跨域的来源，逗号分隔（默认本机预览端口）
  APEC_RATE_PER_MIN                                     每个 IP 每分钟最多提问次数（默认 10）
  APEC_ADMIN_PASSWORD                                   速览编辑页密码（至少 12 位；不设则编辑功能关闭）
  APEC_DATA_DIR                                         速览数据与历史版本目录（默认本目录下 data/）
运行：uv run uvicorn server:app --port 8840
"""
import hmac
import json
import re
import os
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

load_dotenv(Path(__file__).with_name(".env"))

SYSTEM_PROMPT = Path(__file__).with_name("prompt.md").read_text(encoding="utf-8")
MAX_QUESTION, MAX_CONTEXT, MAX_TURN = 300, 12000, 2000


def providers() -> list[dict]:
    out = []
    if key := os.getenv("OPENAI_API_KEY"):
        out.append({"name": "primary", "base": os.getenv("OPENAI_BASE_URL") or "https://api.deepseek.com",
                    "key": key, "model": os.getenv("APEC_MODEL") or "deepseek-chat"})
    if key := os.getenv("DASHSCOPE_API_KEY"):
        out.append({"name": "fallback", "base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                    "key": key, "model": os.getenv("APEC_FALLBACK_MODEL") or "qwen-plus"})
    return out


class Turn(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(max_length=MAX_TURN)


class ChatIn(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION)
    context: str = Field(default="", max_length=MAX_CONTEXT)
    history: list[Turn] = Field(default_factory=list, max_length=6)


app = FastAPI(title="apec-chat", docs_url=None, redoc_url=None)
origins = [o.strip() for o in (os.getenv("APEC_ALLOW_ORIGINS") or "http://localhost:3100,http://localhost:8830").split(",") if o.strip()]
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["POST", "GET", "PUT"], allow_headers=["Content-Type", "Authorization"])

RATE = int(os.getenv("APEC_RATE_PER_MIN") or 10)
_hits: dict[str, deque[float]] = defaultdict(deque)
_admin_fails: dict[str, deque[float]] = defaultdict(deque)


def _limited(store: dict[str, deque[float]], ip: str, limit: int, window: float = 60) -> bool:
    now, q = time.monotonic(), store[ip]
    while q and now - q[0] > window:
        q.popleft()
    return len(q) >= limit


def check_rate(ip: str) -> None:
    if _limited(_hits, ip, RATE):
        raise HTTPException(429, "提问太频繁，请一分钟后再试")
    _hits[ip].append(time.monotonic())


def client_ip(request: Request) -> str:
    return request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "-")


def build_messages(body: ChatIn) -> list[dict]:
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    msgs += [t.model_dump() for t in body.history]
    msgs.append({"role": "user", "content": f"{body.context}\n\n用户问题：{body.question}"})
    return msgs


async def stream_from(p: dict, messages: list[dict]) -> AsyncIterator[str]:
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
        async with client.stream(
            "POST", f"{p['base'].rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {p['key']}"},
            json={"model": p["model"], "messages": messages, "stream": True, "temperature": 0.3, "max_tokens": 900},
        ) as r:
            if r.status_code != 200:
                raise RuntimeError(f"{p['name']} 返回 {r.status_code}")
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                try:
                    delta = json.loads(data)["choices"][0]["delta"].get("content") or ""
                except (KeyError, IndexError, json.JSONDecodeError):
                    continue
                if delta:
                    yield delta


async def answer(messages: list[dict]) -> AsyncIterator[str]:
    errors = []
    for p in providers():
        started = False
        try:
            async for chunk in stream_from(p, messages):
                started = True
                yield chunk
            if started:
                return
        except (httpx.HTTPError, RuntimeError) as e:
            errors.append(f"{p['name']}: {e}")
            if started:  # 已经输出了一部分，不能再切换模型，补一句说明
                yield "\n\n（回答中断，请再问一次）"
                return
    print("全部模型不可用：", errors or "未配置任何 key", flush=True)
    # 空输出：专题页收到空内容会切换为本地标准回答


@app.get("/health")
def health() -> dict:
    return {"ok": True, "providers": [p["name"] for p in providers()]}


@app.post("/chat")
async def chat(body: ChatIn, request: Request) -> StreamingResponse:
    check_rate(client_ip(request))
    # 逐字输出不依赖服务器 Nginx 配置：
    # - Content-Encoding: identity：Nginx 见到已有编码就不再 gzip（gzip 会把整段攒齐再发）
    # - text/event-stream：常见反向代理和面板默认不压缩、不缓冲这种类型；页面按纯文本读取，不受影响
    # - X-Accel-Buffering: no：要求 Nginx 关闭这次响应的代理缓冲
    return StreamingResponse(answer(build_messages(body)), media_type="text/event-stream; charset=utf-8",
                             headers={"Cache-Control": "no-store, no-transform", "X-Accel-Buffering": "no",
                                      "Content-Encoding": "identity"})


# ---------- 今日速览：线上读取与编辑 ----------
# 速览存在 data/brief.json；每次保存前把旧版本存进 data/history/，可以一键恢复。

DATA_DIR = Path(os.getenv("APEC_DATA_DIR") or Path(__file__).with_name("data"))
BRIEF_FILE = DATA_DIR / "brief.json"
HISTORY_DIR = DATA_DIR / "history"
HISTORY_KEEP = 50
BJ = timezone(timedelta(hours=8), "北京时间")  # 中国无夏令时，固定东八区，不依赖服务器时区数据
NO_STORE = {"Cache-Control": "no-store"}


class BriefItem(BaseModel):
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    text: str = Field(min_length=5, max_length=300)
    source: str = Field(min_length=1, max_length=30)
    url: str = Field(pattern=r"^https?://\S+$", max_length=500)
    topics: list[str] = Field(default_factory=list, max_length=10)


class BriefIn(BaseModel):
    items: list[BriefItem] = Field(min_length=1, max_length=20)


def read_brief() -> dict | None:
    try:
        return json.loads(BRIEF_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def require_admin(request: Request, authorization: str | None) -> None:
    password = os.getenv("APEC_ADMIN_PASSWORD") or ""
    if len(password) < 12:
        raise HTTPException(503, "编辑功能未开启：服务器未设置 APEC_ADMIN_PASSWORD（至少 12 位）")
    ip = client_ip(request)
    if _limited(_admin_fails, ip, 5, 600):
        raise HTTPException(429, "密码错误次数过多，请 10 分钟后再试")
    given = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(given.encode(), password.encode()):
        _admin_fails[ip].append(time.monotonic())
        raise HTTPException(401, "密码不对")


@app.get("/brief")
def get_brief():
    data = read_brief()
    if data is None:
        raise HTTPException(404, "线上还没有保存过速览", headers=NO_STORE)
    return JSONResponse(data, headers=NO_STORE)


@app.get("/admin/check")
def admin_check(request: Request, authorization: str | None = Header(default=None)):
    require_admin(request, authorization)
    return {"ok": True}


@app.put("/admin/brief")
def admin_save(body: BriefIn, request: Request, authorization: str | None = Header(default=None)):
    require_admin(request, authorization)
    for it in body.items:
        bad = [t for t in it.topics if not re.fullmatch(r"A\d{2}", t)]
        if bad:
            raise HTTPException(422, f"议题编号不对：{'、'.join(bad)}")
    now = datetime.now(BJ)
    old = read_brief()
    if old is not None:
        write_json_atomic(HISTORY_DIR / f"brief-{now:%Y%m%d-%H%M%S}.json", old)
        for f in sorted(HISTORY_DIR.glob("brief-*.json"))[:-HISTORY_KEEP]:
            f.unlink(missing_ok=True)
    data = {"updatedAt": now.strftime("%Y-%m-%d %H:%M"), "items": [it.model_dump() for it in body.items]}
    write_json_atomic(BRIEF_FILE, data)
    return data


@app.get("/admin/history")
def admin_history(request: Request, authorization: str | None = Header(default=None)):
    require_admin(request, authorization)
    out = []
    for f in sorted(HISTORY_DIR.glob("brief-*.json"), reverse=True)[:HISTORY_KEEP]:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            out.append({"name": f.stem, "updatedAt": d.get("updatedAt"), "count": len(d.get("items", [])),
                        "first": (d.get("items") or [{}])[0].get("text", "")[:40]})
        except (OSError, json.JSONDecodeError):
            continue
    return JSONResponse(out, headers=NO_STORE)


@app.post("/admin/restore/{name}")
def admin_restore(name: str, request: Request, authorization: str | None = Header(default=None)):
    require_admin(request, authorization)
    if not re.fullmatch(r"brief-\d{8}-\d{6}", name):
        raise HTTPException(400, "版本名不对")
    src = HISTORY_DIR / f"{name}.json"
    if not src.exists():
        raise HTTPException(404, "找不到这个版本")
    restored = json.loads(src.read_text(encoding="utf-8"))
    return admin_save(BriefIn(items=restored["items"]), request, authorization)
