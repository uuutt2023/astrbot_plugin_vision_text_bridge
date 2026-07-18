"""独立 OpenAI 兼容 HTTP server on 127.0.0.1:<port> — bypass framework legacy_router JWT.

Why: framework /api/plug/<plugin>/* 路径 require_dashboard_user (JWT 必需).
openai SDK 发 'Authorization: Bearer placeholder' → 不匹配 JWT → framework 401 'Token 无效'.

解决: 我方 start 独立 HTTP server on 127.0.0.1:<port>. zero deps (Python stdlib + asyncio).
框架 ProviderOpenAIOfficial uses openai SDK → POST 我方独立 server (no JWT) → 200.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid

try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger("astrbot_plugin_vision_text_bridge")

_server: "asyncio.AbstractServer | None" = None
_port: int = 2023
_MAX_BODY_BYTES = 50 * 1024 * 1024


async def _wrap_sync_result(value):
    """Wrap a sync return in a coroutine for asyncio.gather compatibility."""
    return value


def _build_response(body: dict, status: int = 200) -> tuple[bytes, str]:
    """构造 HTTP/1.1 响应."""
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    reason = "OK" if status == 200 else ("Bad Request" if status == 400 else "Internal Server Error")
    headers = (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Type: application/json; charset=utf-8\r\n"
        f"Content-Length: {len(payload)}\r\n"
        f"Access-Control-Allow-Origin: *\r\n"
        f"Connection: close\r\n"
        "\r\n"
    ).encode("utf-8")
    return headers + payload


async def _handle_request(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, plugin) -> None:
    """处理一个 HTTP request — 解析 + 调 plugin._describe_one."""
    peer = writer.get_extra_info("peername")
    peer_str = f"{peer[0]}:{peer[1]}" if peer else "?"
    t_start = time.perf_counter()
    try:
        # 读 request line
        request_line = await reader.readline()
        if not request_line:
            writer.close()
            return
        try:
            method, path, _ = request_line.decode("utf-8", errors="replace").split(" ", 2)
        except ValueError:
            response_bytes = _build_response({"status": "error", "message": "bad request"}, 400); writer.write(response_bytes)
            await writer.drain()
            writer.close()
            return

        # 读 headers
        headers = {}
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"", b"\n"):
                break
            try:
                k, _, v = line.decode("utf-8", errors="replace").rstrip("\r\n").partition(":")
                headers[k.strip().lower()] = v.strip()
            except Exception:
                pass

        # 读 body (if Content-Length)
        content_length = int(headers.get("content-length", "0") or "0")
        if content_length > _MAX_BODY_BYTES:
            response_bytes = _build_response(
                {"status": "error", "message": f"request body too large (max {_MAX_BODY_BYTES} bytes)"}, 413,
            )
            writer.write(response_bytes)
            await writer.drain()
            writer.close()
            return
        body_bytes = b""
        if content_length > 0:
            body_bytes = await reader.readexactly(content_length)

        logger.debug(
            "[vision_text_bridge] ⇠ 请求: %s %s from %s | content-length=%d | ua=%s",
            method, path, peer_str, content_length,
            headers.get("user-agent", "-"),
        )

        # 路由
        if method.upper() == "POST" and path in ("/v1/chat/completions", "/v1/chat/completions/chat/completions"):
            try:
                body = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
            except Exception as e:
                response_bytes = _build_response({"status": "error", "message": f"无法解析请求体: {e}"}, 400)
                writer.write(response_bytes)
                await writer.drain()
                writer.close()
                logger.warning("[vision_text_bridge] /v1/chat/completions 请求体解析失败: %s", e)
                return
            response_body, status = await _handle_chat_completions(body, plugin)
            response_bytes = _build_response(response_body, status)
            writer.write(response_bytes)
            await writer.drain()
            elapsed_ms = (time.perf_counter() - t_start) * 1000
            content_len = response_body.get("choices", [{}])[0].get("message", {}).get("content", "")
            logger.debug(
                "[vision_text_bridge] ⇠ 响应: POST /v1/chat/completions status=%d 耗时=%.1fms 内容预览=%s",
                status, elapsed_ms, repr((content_len or "")[:120]),
            )
        elif method.upper() == "GET" and path == "/health":
            response_bytes = _build_response({"status": "ok", "server": "vision_text_bridge_solo"}, 200)
            writer.write(response_bytes)
            await writer.drain()
            logger.debug("[vision_text_bridge] ⇠ GET /health 200")
        else:
            response_bytes = _build_response({"status": "error", "message": f"未找到路由: {method} {path}"}, 400)
            writer.write(response_bytes)
            await writer.drain()
            logger.debug("[vision_text_bridge] ⇠ 404: %s %s", method, path)
    except Exception as e:
        logger.warning("[vision_text_bridge] solo server handler 异常: %s", e)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def _handle_chat_completions(body: dict, plugin) -> tuple[dict, int]:
    """OpenAI compatible /v1/chat/completions handler."""
    model_in = body.get("model") or "vision_text_bridge"
    messages = body.get("messages") or []
    logger.debug(
        "[vision_text_bridge] OpenAI 请求解析: model=%s messages=%d",
        model_in, len(messages),
    )
    if not messages:
        return {"status": "error", "message": "messages 不能为空"}, 400
    image_urls: list[str] = []
    prompt_parts: list[str] = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type")
                if ptype == "image_url":
                    url = (part.get("image_url") or {}).get("url")
                    if isinstance(url, str) and url:
                        image_urls.append(url)
                elif ptype == "text":
                    t = part.get("text")
                    if t:
                        prompt_parts.append(t)
        elif isinstance(content, str):
            prompt_parts.append(content)
    logger.debug(
        "[vision_text_bridge] 提取结果: image_urls=%d, prompt_chars=%d",
        len(image_urls), sum(len(p) for p in prompt_parts),
    )
    if not image_urls:
        return {"status": "error", "message": "未提供 image_url"}, 400
    # Call plugin._describe_one (mmx-via)
    captions: list[str] = []
    # : 将调用方传入的文本部分拼接为自定义 vision_prompt
    caller_prompt = "\n".join(prompt_parts).strip() if prompt_parts else ""
    logger.info(
        "[vision_text_bridge] 收到 /v1/chat/completions 请求: images=%d "
        "prompt_len=%d caller_prompt=%s",
        len(image_urls), len(caller_prompt),
        repr(caller_prompt[:120]),
    )
    try:
        if not image_urls:
            return {"status": "error", "message": "no image urls provided"}, 400
        coros = []
        for u in image_urls:
            cap = plugin._describe_one(u, "main_server", vision_prompt=caller_prompt)
            coros.append(cap if asyncio.iscoroutine(cap) else _wrap_sync_result(cap))
        results = await asyncio.gather(*coros, return_exceptions=True)
        for idx, cap in enumerate(results, 1):
            if isinstance(cap, Exception):
                logger.warning(
                    "[vision_text_bridge] mmx 调 #%d 异常: %s",
                    idx, cap,
                )
                continue
            u = image_urls[idx - 1]
            url_preview = (u[:80] + "...") if len(u) > 80 else u
            if cap:
                captions.append(cap)
                logger.debug(
                    "[vision_text_bridge] mmx 调 #%d 结果长度=%d 预览=%s",
                    idx, len(cap), repr(cap[:80]),
                )
            else:
                logger.warning(
                    "[vision_text_bridge] mmx 调 #%d 返回空 url=%s",
                    idx, url_preview,
                )
    except Exception as e:
        logger.exception("chat_completions 调 mmx 失败: %s", e)
        return {"status": "error", "message": f"mmx 描述失败: {e}"}, 500
    if not captions:
        return {"status": "error", "message": "mmx 描述返回空"}, 500
    caption_text = "\n".join(f"[图片{i+1}] {c}" for i, c in enumerate(captions))
    logger.debug(
        "[vision_text_bridge] 描述合并完成: 共 %d 张, 总长 %d 字符",
        len(captions), len(caption_text),
    )
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_in,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": caption_text},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }, 200


async def start_solo_server(plugin, port: int = 2023) -> int | None:
    """启动 asyncio TCP server on 127.0.0.1:<port>。

    Args:
        port: 绑定端口，默认 2023

    Returns:
        实际绑定的端口 (int)，失败返 None。
    """
    global _server, _port
    if _server is not None:
        logger.info("[vision_text_bridge] solo server 已在跑, port=%d — 跳过启动", _port)
        return _port
    logger.info("[vision_text_bridge] start_solo_server 调用: port=%d", port)
    try:
        _server = await asyncio.start_server(
            lambda r, w: _handle_request(r, w, plugin),
            host="127.0.0.1",
            port=port,
        )
        # 获取实际绑定的端口（port=0 时 OS 自动分配）
        sockets = _server.sockets or []
        actual_port = sockets[0].getsockname()[1] if sockets else port
        _port = actual_port
        logger.info(
            "[vision_text_bridge] solo openai-compat server 启动: "
            "http://127.0.0.1:%d/v1/chat/completions (bypass framework JWT, zero deps)",
            actual_port,
        )
        return actual_port
    except OSError as e:
        if "Address already in use" in str(e) or "in use" in str(e):
            logger.warning("[vision_text_bridge] port %d 已被占用 — solo server 未启动.", port)
        else:
            logger.exception("[vision_text_bridge] solo server 启动失败: %s", e)
        return None
    except Exception as e:
        logger.exception("[vision_text_bridge] solo server 启动异常: %s", e)
        return None


async def stop_solo_server():
    """停止 solo server."""
    global _server
    if _server is not None:
        _server.close()
        await _server.wait_closed()
        _server = None
        logger.debug("[vision_text_bridge] solo server stopped")
