# 欣鹏 AI 智慧课堂

基于通义千问（Qwen）大模型的课程视频智能处理与课后练习系统。上传一节课程 MP4，系统自动完成语音转写、课堂笔记、思维导图、关键帧抽取，并生成一套纯客观题课后试卷（单选 / 多选 / 判断），配套题库管理、错题本与学习分析，形成「看视频 → 做练习 → 查漏补缺」的完整闭环。

- 仓库地址：<https://github.com/nxpshang211-tech/xinpeng-ai-classroom>
- 技术栈：FastAPI + SQLModel + SQLite + Jinja2 + 原生 JavaScript，AI 能力由阿里云百炼 DashScope（Qwen）提供

## 功能特性

### 课程视频智能处理流水线

- **语音转写**：Qwen ASR（qwen3-asr-flash）。长视频自动转为 16 kHz 单声道并按 4 分 45 秒切片，Base64 Data URI 直传，每段最多重试 5 次，成功片段暂存，断网/中断后可从失败段续传
- **课堂笔记**：转写文本经 qwen-flash 整理为结构化 HTML 笔记
- **思维导图**：自动生成 XMind 文件，前端 jsMind 渲染展示
- **关键帧**：OpenCV 场景检测抽帧，还原课件画面
- **扩展资料**（可选）：配置阿里云搜索后自动补充延伸阅读；未配置时自动跳过，不影响其他步骤
- **失败可重试**：流水线任一环节失败均记录进度，修复后一键重试，无需重新上传

### 课后习题（纯客观题）

- **AI 自动组卷**：每课 16 题 / 满分 100 = 6 单选×5 分 + 4 多选×10 分 + 6 判断×5 分，每题带知识点与难度（易/中/难）标签
- **多选给分策略可配置**：默认漏选半分、错选零分；置 `MULTI_PARTIAL_CREDIT=false` 可改为全对才得分
- **做题体验**：答题草稿自动保存（刷新/断网不丢）、题目标记、提交后即时判分并展示解析
- **AI 错因分析**：答错题目异步生成错因标签（概念混淆 / 知识盲区 / 粗心失误 / 方法错误）与 150 字内针对性分析

### 题库管理

- 三种题型的手工录入 / 编辑 / 删除，录入即做字段长度与答案格式硬校验
- **实测正确率回填**：按学生首答数据统计每题实测正确率，样本量 < 5 时不显示；实测与标称难度偏差过大自动标记「待复审」

### 错题本

- 答错自动收录（多选漏选半分也计为错题）
- 支持按错因标签筛选，查看 AI 逐题分析
- 重练模式：同一题连对 2 次自动掌握并移出错题本

### 学习分析

- 双口径正确率：首答正确率（真实掌握度）与含重做正确率分离展示
- 错因分布统计，定位薄弱环节

### 用户系统

- 注册 / 登录，AES 加密 token 持久化登录态，未登录自动跳转登录页

## 快速开始

### 环境要求

- Python 3.10+
- FFmpeg（`moviepy` 依赖 `imageio-ffmpeg`，pip 安装时自动附带，无需单独安装）
- 一个阿里云百炼 API Key（<https://bailian.console.aliyun.com/> 开通获取）

### 安装步骤

```powershell
git clone https://github.com/nxpshang211-tech/xinpeng-ai-classroom.git
cd xinpeng-ai-classroom
python -m pip install -r requirements.txt
```

### 配置

```powershell
copy .env.example .env
```

编辑 `.env`，最少只需填写一项：

```dotenv
DASHSCOPE_API_KEY=你的百炼API-Key
```

其余配置项说明（均有默认值，按需修改）：

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `DASHSCOPE_ASR_MODEL` | `qwen3-asr-flash` | 语音转写模型 |
| `DASHSCOPE_TEXT_MODEL` | `qwen-flash` | 笔记 / 出题 / 错因分析模型 |
| `DASHSCOPE_ASR_TRANSPORT` | `base64` | ASR 传输方式，改 `oss` 恢复 SDK 临时 OSS 上传 |
| `DASHSCOPE_ASR_CHUNK_SECONDS` | `285` | ASR 切片时长（秒） |
| `DASHSCOPE_ASR_RETRIES` | `5` | 每段 ASR 最大重试次数 |
| `ALIYUN_SEARCH_API_KEY` / `ALIYUN_SEARCH_ENDPOINT` | 空 | 扩展资料搜索，留空自动跳过 |
| `DATABASE_URL` | `sqlite:///./data/course.db` | 数据库连接 |
| `MAX_UPLOAD_MB` | `500` | 单视频上传大小上限（MB） |
| `MULTI_PARTIAL_CREDIT` | `true` | 多选题漏选半分策略 |
| `TOKEN_AES_KEY` / `TOKEN_AES_IV` | 演示值 | 登录 token 加密材料，**公开部署前必须替换**为 16/24/32 字节随机 key 与 16 字节随机 IV |

### 启动

```powershell
python main.py
```

浏览器访问 <http://127.0.0.1:8000>。首次启动自动创建 `data/`、`static/videos/` 等目录并初始化 SQLite 数据库，无需手动建库。

## 使用示例

1. **注册账号**：访问 <http://127.0.0.1:8000/register>（系统不预置账号、不区分角色）
2. **上传课程**：首页选择 MP4、填写标题后上传，页面实时显示后台处理进度（转写 → 笔记 → 导图 → 关键帧 → 出题）
3. **查看课程**：处理完成后进入详情页，查看课堂笔记、思维导图、关键帧与扩展资料
4. **进入考试**：详情页入口进入 `/exam/{videoid}`，作答 16 道客观题，可中途标记、离开后续答
5. **查看解析**：提交后即时出分，逐题展示对错与解析；错题自动进入错题本并生成 AI 错因分析
6. **查漏补缺**：错题本按错因筛选、逐题重练，连对 2 次自动移出；学习分析页查看首答正确率与错因分布
7. **维护题库**：题库管理页手工补题、修订难度，参考「实测正确率」列识别过难/过易的题

主要页面路由：

| 路由 | 说明 |
| --- | --- |
| `/` | 视频列表与上传 |
| `/detail/{videoid}` | 课程详情（笔记/导图/关键帧/资料） |
| `/exam/{videoid}` | 课后练习 |
| `/bank` | 题库管理 |
| `/wrongbook` | 错题本 |
| `/stats` | 学习分析 |

## 目录结构

```text
xinpeng-ai-classroom/
├── main.py              # FastAPI 入口：页面路由、考试提交/判分、题库/错题本/统计 API
├── module.py            # 视频处理流水线：ASR 转写、笔记、导图、关键帧、出题、错因分析
├── model.py             # SQLModel 数据模型与 SQLite 幂等迁移
├── config.py            # 配置加载与运行目录自动创建
├── schedule.py          # 定时任务脚本
├── requirements.txt
├── .env.example         # 配置模板
├── templates/           # Jinja2 页面模板（首页/详情/考试/题库/错题本/统计等）
├── static/              # 前端 JS/CSS 与运行时产物目录（videos/frames/xminds 自动生成）
├── tests/
│   ├── smoke_test.py    # 无 Key 冒烟测试（模拟完整流水线）
│   └── asr_*_probe.py   # ASR 传输/时长/续传探针脚本
├── data/                # SQLite 数据库（首次启动自动创建，已 gitignore）
└── ori_videos/          # 冒烟测试用视频素材（自备，已 gitignore）
```

## 测试

不填真实 API Key 即可验证数据库、页面、模拟流水线、出题校验、判分、错题本与统计全链路：

```powershell
python tests/smoke_test.py
```

注意：测试会从 `ori_videos/*.mp4` 取一个真实视频验证关键帧抽取，请先在该目录放入任意 MP4 文件。

## 常见问题

**Q：没有百炼 API Key 能运行吗？**
A：可以启动并浏览页面；视频处理会在 ASR 步骤提示缺 Key 失败，配置后点「重试」即可继续。冒烟测试完全不依赖 Key。

**Q：视频处理中断或失败怎么办？**
A：流水线记录每一步进度。ASR 支持断点续传，已转写的片段不会重复请求；修复问题后在详情页点「重试」，无需重新上传。

**Q：上传报错或超限？**
A：默认单视频上限 500 MB，调大 `MAX_UPLOAD_MB` 即可。目前仅支持 MP4 格式。

**Q：旧版本数据库里有填空题/简答题怎么办？**
A：启动时的自动迁移会补全新列并清除 blank/qa 旧题及其关联记录，无需手工干预。

**Q：公开部署要注意什么？**
A：必须替换 `.env` 中的 `TOKEN_AES_KEY` / `TOKEN_AES_IV`；建议置于反向代理（HTTPS）之后；当前为演示级实现，密码明文存储、单 worker 处理视频，不适合直接承载真实多用户业务。

## 贡献指南

1. Fork 本仓库并创建特性分支：`git checkout -b feat/你的特性`
2. 提交遵循 Conventional Commits 风格：`feat:` / `fix:` / `chore:` / `docs:` 开头，简要说明动机
3. 提交前请运行 `python tests/smoke_test.py` 确认全链路通过
4. 发起 Pull Request，描述变更内容与验证方式；涉及数据库结构变更时，请在 `model.py` 的 `_migrate()` 中补充幂等迁移逻辑

## 开源协议

本仓库暂未附带 LICENSE 文件，保留所有权利。如需在您的项目中使用代码，请先联系作者取得许可；作者后续计划补充开源协议（建议 MIT）。
