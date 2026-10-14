---
name: analyze-image
description: 当任务需要查看、识别或分析图片（本地文件、URL、base64），或需要把图片内容转成文字（读截图、图表、UI、题目、OCR）时，调用 glm-image-viewer 插件的 analyze_image 工具，由 GLM-5.3-Flash 视觉模型识图并把结果返回给当前模型使用。
---

# 图片分析（GLM-5.3-Flash 识图）

当前模型需要"看图"时，不要凭文件名猜测图片内容，调用 `analyze_image` 工具让 GLM-5.3-Flash 识别：

- `image_source`：本地绝对路径 / http(s) URL / base64 字符串
- `prompt`：想让视觉模型回答的问题

示例：用户给出 `~/Desktop/error.png` 并问"这报错怎么修"，调用：

```
analyze_image(image_source="/Users/you/Desktop/error.png", prompt="这是终端报错截图，请完整转写报错文字并说明原因")
```

返回的文本就是视觉模型的识别结果，据此继续回答用户。

- 若返回「未配置智谱 API Key」，提示用户在 `~/.glm-image-viewer/config.json` 写入 `{"api_key": "..."}` 或设置环境变量 `ZHIPU_API_KEY`。
- 若返回模型名错误，提示用户在同一配置文件中修改 `model` 字段。
