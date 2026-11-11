# AI 模型配置页（设置页）方案

## 背景

当前所有 AI 配置（API Key / base_url / 模型名）写死在 `.env` + [config.py](file:///c:/Users/31586/Desktop/欣鹏ai智慧课堂/欣鹏ai智慧课堂程序/config.py) 的 frozen `Settings` 中，运行中不可修改。用户要求参照 Trae「添加模型」的逻辑：前端设置页可选供应商预设（默认阿里云百炼）或自定义填写 base_url + key + model，**文本模型与 ASR 都可换**，全局一份配置，任何登录用户可修改（本系统不分角色，每人自己部署自己改）。

### 现状关键点（已核实）

- 全部文本调用（笔记/导图/出题/错因）收敛于 [module.py:111](file:///c:/Users/31586/Desktop/欣鹏ai智慧课堂/欣鹏ai智慧课堂程序/module.py#L111) `_call_text_model()`，走 `dashscope.Generation.call` SDK
- ASR 在 `_transcribe_audio_file()`：两条路径——`oss`（DashScope SDK）与 `base64`（OpenAI 兼容 POST `{base_url}/chat/completions`，已用 requests）
- 导航在 index/exam/bank/wrongbook/stats 五个模板中结构一致（首页/题库/错题本/学习报告）
- 阿里云搜索配置保留在 `.env`，本次不动

## 改动设计

### 1. 存储：新增 `AIConfig` 表（[model.py](file:///c:/Users/31586/Desktop/欣鹏ai智慧课堂/欣鹏ai智慧课堂程序/model.py)）

单行全局配置（id 固定为 1），字段：

```
text_provider, text_base_url, text_api_key, text_model
asr_provider, asr_base_url, asr_api_key, asr_model, asr_transport
updated_at
```

`SQLModel.metadata.create_all` 自动建表，无需迁移代码。

### 2. 配置读取：[module.py](file:///c:/Users/31586/Desktop/欣鹏ai智慧课堂/欣鹏ai智慧课堂程序/module.py) 新增 `get_ai_config()`

- 读库；无记录时用现有 `.env` 值**播种**（老用户零感知升级）
- 进程内缓存；保存接口写入后调用 `invalidate_ai_config_cache()`

### 3. 文本调用改造（`_call_text_model`）

改为 OpenAI 兼容 HTTP：`POST {text_base_url}/chat/completions`，Bearer key，body `{model, messages:[system,user]}`，取 `choices[0].message.content`。保留 `_retry` 重试与 `PipelineError` 语义。requests 已在依赖中，dashscope SDK 仅保留给 ASR-oss 路径。

### 4. ASR 改造（`_transcribe_audio_file`）

- `base64` 路径的 base_url/key/model 改读 `get_ai_config()` 的 asr_* 字段
- `oss` 路径仅当 `asr_provider == "dashscope"` 时可用，否则强制走 base64 路径
- `_require_dashscope_key()` 改为 `_require_asr_key()`（从配置读，缺省时报「请在设置页配置 ASR Key」）

### 5. 后端接口（[main.py](file:///c:/Users/31586/Desktop/欣鹏ai智慧课堂/欣鹏ai智慧课堂程序/main.py)）

| 接口 | 说明 |
| --- | --- |
| `GET /settings` | 设置页模板 |
| `GET /api/settings/ai/{token}` | 返回配置；api_key 脱敏为 `sk-****尾4位` |
| `POST /api/settings/ai/{token}` | 保存；key 留空或原样传回脱敏值 = 不修改；base_url/model 必填校验 |
| `POST /api/settings/ai/test/{token}` | 用提交的值发一条最小 chat 请求（"ping"），返回成功/失败原因（仅测文本模型） |

### 6. 前端

- 新建 [templates/settings.html](file:///c:/Users/31586/Desktop/欣鹏ai智慧课堂/欣鹏ai智慧课堂程序/templates/settings.html)：两个卡片（文本模型 / 语音转写 ASR），每卡含供应商下拉 + base_url + api_key（type=password，placeholder「已配置，留空不修改」）+ model 输入 + 保存按钮；文本卡附「测试连接」按钮
- 预设**硬编码在前端 JS**（script.js）：
  - 文本：阿里云百炼（默认，`dashscope.../compatible-mode/v1` + qwen-flash）/ DeepSeek（`api.deepseek.com/v1` + deepseek-chat）/ OpenAI（`api.openai.com/v1` + gpt-4o-mini）/ 自定义
  - ASR：阿里云百炼（默认，qwen3-asr-flash）/ 自定义；选预设自动填 base_url 与 model，key 手填；ASR 卡加提示「换用非阿里云供应商时，模型需支持音频输入」
- [style.css](file:///c:/Users/31586/Desktop/欣鹏ai智慧课堂/欣鹏ai智慧课堂程序/static/style.css) 复用 panel/auth-field 风格补充设置页样式
- 五个模板（index/exam/bank/wrongbook/stats）导航加 `<a href="/settings">设置</a>`

### 7. 测试（[tests/smoke_test.py](file:///c:/Users/31586/Desktop/欣鹏ai智慧课堂/欣鹏ai智慧课堂程序/tests/smoke_test.py)）

新增用例：配置播种（默认走 .env 值）、GET 脱敏、POST 保存与「留空不改 key」、mock requests 断言 `_call_text_model` 命中自定义 base_url 且响应解析正确。

### 8. 文档

`.env.example` 与 README 补一句：`.env` 为初始默认值，首次启动后以前端设置页配置为准。

## 不做的事

- 不做每用户独立配置、不做权限分级
- 阿里云搜索配置不挪到设置页
- ASR 不做在线测试按钮（需真实音频，成本高）

## 验证

1. `python tests/smoke_test.py` 全绿（含新增配置用例）
2. 启动 uvicorn，真实库自动播种配置；打开 `/settings`：切换预设自动填值、保存、刷新确认持久化、测试连接返回成功；key 脱敏显示
3. 上传/重试一个视频确认流水线各环节走新配置
