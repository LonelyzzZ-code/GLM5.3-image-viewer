# glm-image-viewer

让没有视觉能力的模型"长出眼睛"的 [ZCode](https://z.ai) 插件：主模型遇到图片时，通过 MCP 工具 `analyze_image` 调用智谱 **GLM-5.3-Flash** 视觉模型识图，识别结果以文本返回给主模型继续推理。

适合读截图、图表、UI 界面、题目、OCR 等一切"模型看不了图"的场景。

## 特性

- **三种图片输入**：本地文件绝对路径、http(s) URL、裸 base64 字符串
- **零依赖**：纯 Python 标准库实现 MCP stdio 协议，兼容 Python 3.9+，无需 pip 安装
- **输入预检**：路径不存在 / 超 8MB / 非法 base64 直接给出明确报错
- **安全**：API Key 只存本机配置文件，协议白名单（仅 http/https 出站），拒绝重定向

## 安装

### 方式一：作为 ZCode 插件市场源

1. 打开 ZCode → Settings → Plugins → Add marketplace，填入本仓库地址：
   `https://github.com/LonelyzzZ-code/GLM5.3-image-viewer`
2. 安装 `glm-image-viewer` 插件并启用。

### 方式二：手动注册 MCP server

任何支持 stdio MCP 的客户端（ZCode / Claude Code 等）均可，以 `~/.zcode/cli/config.json` 为例：

```json
{
  "mcp": {
    "servers": {
      "glm-image-viewer": {
        "command": "/usr/bin/python3",
        "args": ["/path/to/glm-image-viewer/mcp-server/glm_image_server.py"],
        "timeoutMs": 120000
      }
    }
  }
}
```

## 配置

创建 `~/.glm-image-viewer/config.json`：

```json
{
  "api_key": "你的智谱API Key",
  "model": "glm-5.3-flash",
  "api_base": "https://open.bigmodel.cn/api/paas/v4",
  "timeout": 18,
  "fallback_model": "glm-4v-flash"
}
```

| 字段 | 环境变量 | 默认值 | 说明 |
|---|---|---|---|
| `api_key` | `ZHIPU_API_KEY` / `GLM_API_KEY` | 无（必填） | [智谱开放平台](https://open.bigmodel.cn) API Key，按量计费 |
| `model` | `GLM_VISION_MODEL` | `glm-5.3-flash` | 视觉模型名 |
| `api_base` | `ZHIPU_API_BASE` | `https://open.bigmodel.cn/api/paas/v4` | 兼容 one-api 等自建网关（支持 localhost/内网地址） |
| `timeout` | `GLM_VISION_TIMEOUT` | `18` | 首次尝试总预算（秒），按 10:8 拆分给响应头/响应体 |
| `fallback_model` | `GLM_VISION_FALLBACK_MODEL` | `glm-4v-flash` | 主模型重试用尽后的备用视觉模型，置空字符串禁用 |

环境变量优先级高于配置文件。

## 使用

```
analyze_image(
  image_source="/Users/you/screenshot.png",
  prompt="提取图中所有文字"
)
```

正常耗时 2-8 秒（取决于图片大小与提问复杂度）。

## 排障

看日志最后几行：

```bash
tail ~/.glm-image-viewer/server.log
```

每次调用记录 `attempt`（每次尝试的状态/耗时/错误）和 `call_done`（整体结果），1MB 自动轮转。

## License

[MIT](LICENSE)
