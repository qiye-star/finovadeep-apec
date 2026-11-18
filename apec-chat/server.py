"""APEC 专题对话服务：接收专题页拼好的上下文，调用大模型，流式返回纯文本。

主模型走 OpenAI 兼容协议（默认 DeepSeek），首个字之前失败则切千问；key 只从环境变量读。
环境变量：
  OPENAI_API_KEY / OPENAI_BASE_URL / APEC_MODEL         主模型（默认 https://api.deepseek.com、deepseek-chat）
  DASHSCOPE_API_KEY / APEC_FALLBACK_MODEL               备用模型（默认 qwen-plus）
  APEC_ALLOW_ORIGINS                                    允许跨域的来源，逗号分隔（默认本机预览端口）
  APEC_RATE_PER_MIN                                     每个 IP 每分钟最多提问次数（默认 10）
运行：uv run uvicorn server:app --port 8840
"""
import json
import os
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
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
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["POST", "GET"], allow_headers=["Content-Type"])

RATE = int(os.getenv("APEC_RATE_PER_MIN") or 10)
_hits: dict[str, deque[float]] = defaultdict(deque)


def check_rate(ip: str) -> None:
    now, q = time.monotonic(), _hits[ip]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= RATE:
        raise HTTPException(429, "提问太频繁，请一分钟后再试")
    q.append(now)


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
    check_rate(request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "-"))
    return StreamingResponse(answer(build_messages(body)), media_type="text/plain; charset=utf-8",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
