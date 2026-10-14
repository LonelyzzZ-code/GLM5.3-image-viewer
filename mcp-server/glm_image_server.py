#!/usr/bin/env python3
"""glm-image-viewer MCP server（stdio）。

提供 analyze_image 工具：主模型把图片（本地路径 / URL / base64）和问题交给本工具，
由 GLM-5.3-Flash 视觉模型识图，识别结果以文本返回给主模型。

配置读取优先级：环境变量 > ~/.glm-image-viewer/config.json > 默认值
  - API Key:  环境变量 ZHIPU_API_KEY 或 GLM_API_KEY；配置文件字段 api_key
  - 模型:     环境变量 GLM_VISION_MODEL；配置文件字段 model（默认 glm-5.3-flash）
  - API 地址: 环境变量 ZHIPU_API_BASE；配置文件字段 api_base
  - 单次预算: 环境变量 GLM_VISION_TIMEOUT；配置文件字段 timeout（秒，首次头+body 总额，默认 18）

每次调用写一行结构化日志到 ~/.glm-image-viewer/server.log（自动轮转，1MB 上限），
出问题先看这个文件。仅用标准库，兼容 Python 3.9+。
"""
import base64
import json
import mimetypes
import os
import sys
import threading
import time
import urllib.error
import urllib.request

SERVER_NAME = "glm-image-viewer"
SERVER_VERSION = "0.2.0"
TOOL_NAME = "analyze_image"

DEFAULT_MODEL = "glm-5.3-flash"
DEFAULT_API_BASE = "https://open.bigmodel.cn/api/paas/v4"
CONFIG_DIR = os.path.expanduser("~/.glm-image-viewer")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
LOG_PATH = os.path.join(CONFIG_DIR, "server.log")
LOG_MAX_BYTES = 1024 * 1024  # 超过则轮转为 server.log.1，防无限增长

# 本地图片读入上限（base64 编码前）。开放平台对 data URI 的上限约 10MB，
# 提前拒绝可避免"传一半超时"这种难排查的失败。
MAX_IMAGE_BYTES = 8 * 1024 * 1024

# 两段式超时：urlopen 返回 = 响应头已到；实测挂死形态是"头到、body 永不到"，
# 而正常请求头到后 body 1 秒内到齐。因此头/body 分别计时，挂死可在头到达后
# body_budget 秒内判定（约 10s），不必等满单次总预算。
# 注意：resp.read() 走 socket.makefile() 文件流，不受 socket 超时控制，必须由
# 主线程 Event.wait 兜底。预算：首次 头10s+body8s，重试 头7s+body4s，最坏 29.5s。
HEAD_BUDGET = 10
BODY_BUDGET = 8
RETRY_HEAD_BUDGET = 7
RETRY_BODY_BUDGET = 4
MAX_ATTEMPTS = 2
RETRY_WAIT = 0.5  # 两次尝试之间的间隔秒数
PROGRESS_INTERVAL = 8  # 客户端带 progressToken 时，每 N 秒发一次进度（重置客户端超时）

TOOL_DEF = {
    "name": TOOL_NAME,
    "description": (
        "调用 GLM-5.3-Flash 视觉模型分析一张图片，并把识别结果以文字返回。"
        "当任务需要查看/理解图片内容（截图、照片、图表、UI、题目等）时使用。"
        "image_source 支持：本地文件绝对路径、http(s) 图片 URL、纯 base64 字符串。"
        "prompt 描述希望视觉模型回答的问题。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "image_source": {
                "type": "string",
                "description": "图片来源：本地绝对路径（如 /Users/mac/a.png）、http(s) URL 或 base64 字符串",
            },
            "prompt": {
                "type": "string",
                "description": "要视觉模型执行的任务，如「详细描述图中内容」「提取图中所有文字」「这道题怎么解」",
            },
        },
        "required": ["image_source"],
    },
}

# stdout 可能被主线程（RPC 响应）与进度线程并发写，必须加锁防止 JSON 行交错
_SEND_LOCK = threading.Lock()


def log_event(event, **fields):
    """追加一行结构化日志，任何失败都静默（日志绝不能影响主流程）。"""
    try:
        if (os.path.isfile(LOG_PATH)
                and os.path.getsize(LOG_PATH) > LOG_MAX_BYTES):
            os.replace(LOG_PATH, LOG_PATH + ".1")  # 原子轮转，旧 .1 被覆盖
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event}
        entry.update({k: v for k, v in fields.items() if v is not None})
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def load_config():
    file_cfg = {}
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                file_cfg = json.load(f)
        except (OSError, ValueError):
            file_cfg = {}

    def pick(env_name, file_key, default):
        return os.environ.get(env_name) or file_cfg.get(file_key) or default

    return {
        "api_key": pick("ZHIPU_API_KEY", "api_key", "") or pick("GLM_API_KEY", "api_key", ""),
        "model": pick("GLM_VISION_MODEL", "model", DEFAULT_MODEL),
        "api_base": pick("ZHIPU_API_BASE", "api_base", DEFAULT_API_BASE),
        "timeout": pick("GLM_VISION_TIMEOUT", "timeout", HEAD_BUDGET + BODY_BUDGET),
    }


def to_image_url(source):
    """把输入统一成 API 需要的 image_url：URL 原样，本地文件转 data URI，其余当裸 base64。

    抛 ValueError 表示输入本身有问题（文件不存在/过大/不像 base64）。
    """
    if source.startswith(("http://", "https://")):
        return source
    if os.path.exists(source):
        if not os.path.isfile(source):
            raise ValueError("路径不是常规文件（可能是目录）：%s" % source)
        size = os.path.getsize(source)
        if size > MAX_IMAGE_BYTES:
            raise ValueError("图片 %.1fMB 超过 %dMB 上限，请先压缩" % (
                size / 1048576, MAX_IMAGE_BYTES // 1048576))
        mime = mimetypes.guess_type(source)[0] or "image/png"
        with open(source, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        return "data:%s;base64,%s" % (mime, b64)
    # 既不是 URL 也不是存在的文件：只有像 base64（长且全是 base64 字符）才当 base64 用
    stripped = source.strip()
    if len(stripped) >= 100 and all(c.isalnum() or c in "+/=" for c in stripped):
        return "data:image/png;base64," + stripped
    raise ValueError("图片不存在（且不像 base64）：%s" % source[:100])


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # 拒绝跟随重定向；识图端点不应 30x，返回 None 会让 urllib 抛 HTTPError


_OPENER = urllib.request.build_opener(_NoRedirect)


def get_attempt_timeout(cfg):
    """config 的 timeout 字段：首次尝试的总预算（头+body），默认 18 秒。"""
    try:
        return max(8, int(cfg["timeout"]))
    except (TypeError, ValueError):
        return HEAD_BUDGET + BODY_BUDGET


def call_vision_api_monitored(cfg, prompt, image_url, head_budget, body_budget):
    """两段式监控的 API 调用，返回 (状态, 值)。

    状态: "ok"（值=识别文本）| "error"（值=异常对象，含 HTTPError）
        | "head_timeout" | "body_timeout"（值=None，线程残留为 daemon）。

    头阶段：_OPENER.open 返回即响应头已到（连接挂死/头挂死在此判出）。
    body 阶段：正常请求头到后 body 1 秒内到齐；body_budget 内未到即判挂死。
    resp.read() 走 makefile 文件流不受 socket 超时控制，全靠 Event.wait 兜底。
    """
    body = {
        "model": cfg["model"],
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            }
        ],
        "stream": False,
    }
    api_url = cfg["api_base"].rstrip("/") + "/chat/completions"
    # urllib 支持 file:// 等协议，api_base 仅允许 http/https 出站；不做私网/环回 IP 阻断，
    # 因为 one-api 等自建网关合法运行在 localhost/内网
    if not api_url.startswith(("http://", "https://")):
        raise ValueError("api_base 仅支持 http/https，当前值：%r" % cfg["api_base"][:50])
    req = urllib.request.Request(
        api_url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + cfg["api_key"],
        },
        method="POST",
    )
    result = {}
    headers_done = threading.Event()
    all_done = threading.Event()

    def worker():
        try:
            resp = _OPENER.open(req, timeout=max(head_budget, 1))
            result["data"] = resp.read()
        except Exception as e:
            result["error"] = e
            headers_done.set()  # 出错也要放行主线程的等待
        finally:
            headers_done.set()
            all_done.set()

    threading.Thread(target=worker, daemon=True).start()
    if not headers_done.wait(head_budget):
        return "head_timeout", None
    if not all_done.wait(body_budget):
        return "body_timeout", None
    if "error" in result:
        return "error", result["error"]
    data = json.loads(result["data"].decode("utf-8"))
    choices = data.get("choices") or []
    if not choices or not isinstance(choices[0], dict) or "message" not in choices[0]:
        return "error", ValueError("API 返回结构异常：%s" %
                                   json.dumps(data, ensure_ascii=False)[:200])
    content = choices[0]["message"].get("content")
    if content is None:  # 触发内容审核等情况 content 会是 None
        return "error", ValueError("API 未返回内容（finish_reason=%s）" %
                                   choices[0].get("finish_reason"))
    if isinstance(content, list):  # 部分返回格式是多段内容，拼接文本部分
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return "ok", content


def analyze_image(args):
    t_start = time.time()
    source = str(args.get("image_source", "")).strip()
    prompt = str(args.get("prompt") or "详细描述这张图片的内容。")
    if not source:
        return True, "参数错误：image_source 不能为空。"

    cfg = load_config()
    if not cfg["api_key"]:
        return True, (
            "未配置智谱 API Key。请设置环境变量 ZHIPU_API_KEY，"
            '或创建 %s 内容如 {"api_key": "你的Key"}' % CONFIG_PATH
        )

    try:
        image_url = to_image_url(source)
    except OSError as e:
        return True, "读取图片失败：%s" % e
    except ValueError as e:
        return True, "图片输入有误：%s" % e

    # 首次预算按 config.timeout 总额的 10:8 拆头/body，重试用固定短预算
    total = get_attempt_timeout(cfg)
    head1 = max(3, total * HEAD_BUDGET // (HEAD_BUDGET + BODY_BUDGET))
    body1 = max(2, total - head1)
    budgets = [(head1, body1), (RETRY_HEAD_BUDGET, RETRY_BODY_BUDGET)]

    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        hb, bb = budgets[attempt - 1]
        t0 = time.time()
        try:
            status, value = call_vision_api_monitored(cfg, prompt, image_url, hb, bb)
        except Exception as e:  # 理论上不抛，防御保留
            status, value = "error", e
        log_event("attempt", attempt=attempt, status=status,
                  cost=round(time.time() - t0, 1),
                  err=None if (status == "ok" or value is None) else str(value)[:150])
        if status == "ok":
            if attempt > 1:
                sys.stderr.write("attempt %d ok\n" % attempt)
            log_event("call_done", ok=True, attempts=attempt,
                      total=round(time.time() - t_start, 1))
            return False, value
        if status == "error" and isinstance(value, urllib.error.HTTPError):
            # 5xx 是服务端瞬时故障，值得吃掉重试名额；4xx 是配置/输入问题，重试无意义
            if 500 <= value.code < 600 and attempt < MAX_ATTEMPTS:
                last_error = "API HTTP %d（服务端瞬时故障）" % value.code
            else:
                detail = value.read().decode("utf-8", "replace")[:500]
                log_event("call_done", ok=False, http=value.code,
                          total=round(time.time() - t_start, 1))
                return True, "API 请求失败 HTTP %d：%s（当前模型 %s，如模型名不对可在 %s 的 model 字段修改）" % (
                    value.code, detail, cfg["model"], CONFIG_PATH)
        elif status == "head_timeout":
            last_error = "API 连接/响应头 %d 秒未返回" % hb
        elif status == "body_timeout":
            last_error = "API 响应体 %d 秒未到达（服务端偶发无响应）" % bb
        else:
            last_error = "API 请求异常：%r" % value
        if attempt < MAX_ATTEMPTS:
            time.sleep(RETRY_WAIT)
    log_event("call_done", ok=False, total=round(time.time() - t_start, 1))
    return True, "%s；已自动重试 %d 次仍失败。请稍后再试，或在 %s 中增大 timeout 字段（单次总预算秒数）。历史调用记录见 %s" % (
        last_error, MAX_ATTEMPTS - 1, CONFIG_PATH, LOG_PATH)


def send(obj):
    line = json.dumps(obj, ensure_ascii=False) + "\n"
    with _SEND_LOCK:
        sys.stdout.write(line)
        sys.stdout.flush()


def start_progress_reporter(progress_token):
    """客户端传了 progressToken 时，定期发进度通知。

    MCP 客户端（如 ZCode）开启 resetTimeoutOnProgress 时，每条进度都会重置
    工具调用的客户端超时，长耗时调用不再被 30s 窗口掐断。返回停止函数。
    """
    stop = threading.Event()

    def reporter():
        elapsed = 0
        while not stop.wait(PROGRESS_INTERVAL):
            elapsed += PROGRESS_INTERVAL
            try:
                send({"jsonrpc": "2.0", "method": "notifications/progress",
                      "params": {"progressToken": progress_token,
                                 "progress": elapsed, "message": "识别中 %ds" % elapsed}})
            except Exception:
                break  # stdout 已坏，停止上报

    threading.Thread(target=reporter, daemon=True).start()
    return stop.set


def handle(msg):
    method = msg.get("method", "")
    msg_id = msg.get("id")
    if msg_id is None:  # notification，无需应答
        return

    if method == "initialize":
        client_ver = (msg.get("params") or {}).get("protocolVersion") or "2024-11-05"
        send({"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": client_ver,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }})
    elif method == "ping":
        send({"jsonrpc": "2.0", "id": msg_id, "result": {}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": [TOOL_DEF]}})
    elif method == "tools/call":
        params = msg.get("params") or {}
        token = (params.get("_meta") or {}).get("progressToken")
        stop_progress = start_progress_reporter(token) if token is not None else (lambda: None)
        try:
            if params.get("name") != TOOL_NAME:
                raise ValueError("未知工具: %s" % params.get("name"))
            is_error, text = analyze_image(params.get("arguments") or {})
        except Exception as e:
            is_error, text = True, "工具执行出错：%r" % e
        finally:
            stop_progress()
        send({"jsonrpc": "2.0", "id": msg_id, "result": {
            "content": [{"type": "text", "text": text}], "isError": is_error}})
    else:
        send({"jsonrpc": "2.0", "id": msg_id,
              "error": {"code": -32601, "message": "method not found: %s" % method}})


def main():
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            sys.stderr.write("invalid JSON line: %r\n" % line[:100])
            continue
        try:
            handle(msg)
        except Exception as e:  # 单条消息处理异常不退出进程
            sys.stderr.write("handle error: %r\n" % e)
            log_event("handle_error", err=repr(e)[:200])


if __name__ == "__main__":
    main()
