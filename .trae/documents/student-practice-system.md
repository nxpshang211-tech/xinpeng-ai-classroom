# 学生专属做题系统 — 后端优先的设计与实现方案

## 一、方案概述

在现有「欣鹏AI智慧课堂」（FastAPI + SQLModel + SQLite + 原生JS）架构上，将单一考试页升级为完整的做题系统。**本轮以服务端业务为核心交付：完整的接口设计与实现；前端只做能跑通业务流程的粗略展示页面，不做美化与响应式，后续再重构。**

五大模块：

| 模块 | 核心能力 | 主要载体 |
|------|---------|---------|
| 题库管理 | AI 视频出题（扩展填空题/知识点标签）+ 手动增删改查 + 筛选 | `/api/bank/*` |
| 在线答题 | 每个视频一套练习题：实时草稿（已有）+ 题目标记 + 用时记录 | `/exam/draft`、`/exam/submit`、`/exam/mark` |
| 自动批改 | 选择/填空本地即时判分；简答 AI 评分 | `/api/practice/judge`、`/exam/submit` |
| 错题本 | 交卷自动收录 + AI 解析异步生成缓存 + 掌握标记 | `/api/wrongbook/*` |
| 学习分析 | 正确率/知识点掌握/用时统计聚合 | `/api/stats/overview` |

已确认决策：题库 = AI 生成 + 手动管理；错题解析 = 提交后 AI 生成并缓存于题目记录；前端粗略实现，不引入任何外部图表库。

## 二、现状分析（探索结论）

- [model.py](file:///c:/Users/31586/Desktop/chapter07/chapter07/model.py)：现有 `Users / Videos / Exams / Scores / Answers` 五表。`Exams` 仅 choice/qa 题型，无知识点/解析字段；`Answers`（userid+examid 唯一）为草稿表。
- [main.py](file:///c:/Users/31586/Desktop/chapter07/chapter07/main.py)：已有 `/exam/{videoid}`、`/exam/draft` 存取、`/exam/submit`（选择题 5 分硬编码、qa 走 `ai_score`、要求全答完、纯文本返回总分）。
- [module.py](file:///c:/Users/31586/Desktop/chapter07/chapter07/module.py)：`gen_exam` 固定 8 选择 + 3 简答；`_generate_json` 带修复重试；`ai_score` 简答评分。
- [static/script.js](file:///c:/Users/31586/Desktop/chapter07/chapter07/static/script.js)：草稿保存/防抖/统计/计时/回顾模式已就绪。
- 约束：SQLite 用 `ALTER TABLE ADD COLUMN` 做平滑迁移；`Exams.score` 列保留；非 AI 接口目标 <200ms（AI 解析走后台线程池 `worker`）。

## 三、数据模型变更（model.py）

### 3.1 Exams 表新增列
```python
knowledge_point: Optional[str] = Field(default=None, index=True)  # 知识点标签
analysis: Optional[str] = None        # 题目解析（AI 生成后缓存，全用户共享）
alt_answers: Optional[str] = None     # 填空题备选答案，JSON 数组字符串
```
`type` 扩展为 `choice / blank / qa`；手动录入题目允许 `videoid=NULL`（公共题库）。

### 3.2 新增 WrongBook 表
```python
class WrongBook(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("userid", "examid", name="uq_wrong_item"),)
    wrongid: Optional[int] = Field(default=None, primary_key=True)
    userid: Optional[int] = Field(default=None, foreign_key="users.userid", index=True)
    examid: Optional[int] = Field(default=None, foreign_key="exams.examid", index=True)
    videoid: Optional[int] = Field(default=None, index=True)
    wrong_count: int = Field(default=1)
    mastered: bool = Field(default=False)
    last_answer: str = ""
    createtime / updated_at: datetime
```

### 3.3 新增 PracticeSessions 表
```python
class PracticeSessions(SQLModel, table=True):
    sessionid: Optional[int] = Field(default=None, primary_key=True)
    userid: Optional[int] = Field(default=None, foreign_key="users.userid", index=True)
    videoid: Optional[int] = Field(default=None, index=True)
    mode: str = Field(default="exam")   # exam=正常练习 / redo=错题重练
    total_questions: int
    correct_count: int                  # 客观题答对数
    total_score: int
    duration_seconds: int = Field(default=0)
    createtime: datetime
```

### 3.4 Answers 表新增列
```python
marked: bool = Field(default=False)   # 题目标记（疑难题复查）
```

### 3.5 迁移（init_database 内，幂等）
`create_all` 之后执行：
- `PRAGMA table_info(exams)` 检查，缺列则 `ALTER TABLE exams ADD COLUMN ...`（answers 表同理加 `marked`）
- 回填知识点：`UPDATE exams SET knowledge_point=(SELECT keyword FROM videos WHERE videos.videoid=exams.videoid) WHERE knowledge_point IS NULL`；仍 NULL 的填 `'综合'`

## 四、接口设计（本轮核心交付）

所有写接口带 `{token}` 路径参数，`check_token` 未通过返回 `"Token-Fail"`。响应均为 JSON。

### 4.1 题库管理

| 方法 | 路径 | 请求体/参数 | 响应 |
|------|------|------------|------|
| GET | `/api/bank/list` | query: `type`(choice/blank/qa，可空)、`knowledge_point`(可空)、`keyword`(题干模糊，可空) | `{items:[{examid,videoid,videotitle,question,type,score,options,knowledge_point,analysis,alt_answers}], total}` |
| GET | `/api/bank/knowledge-points` | 无 | `{items:["知识点1",...]}`（去重列表，供筛选下拉） |
| POST | `/api/bank/add/{token}` | `{question,type,answer,score?,options?(dict),alt_answers?(list),knowledge_point,videoid?(可空)}` | 成功 `{message:"ok", examid}`；字段不合法返回 `{message:"invalid-field", detail}` |
| POST | `/api/bank/update/{examid}/{token}` | 同 add（全量覆盖可改字段） | `{message:"ok"}` / 404 |
| POST | `/api/bank/delete/{examid}/{token}` | 无 | `{message:"ok"}`；级联删除关联 Answers/Scores/WrongBook 行 |

校验规则：choice 必须有 4 个选项且 answer ∈ ABCD；blank 必须有非空 answer；qa 必须有非空 answer；默认分值 choice=5、blank=10、qa=20（可覆盖）。

### 4.2 在线答题与批改

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/practice/judge/{token}` | 练习模式单题即时判分。体：`{examid, answer}`。choice/blank 本地判分返回 `{score, correct, answer(标准答案), analysis}`；qa 调 `ai_score` 返回 `{score, analysis(参考答案)}` |
| POST | `/exam/mark/{videoid}/{token}` | 体：`{examid, marked:bool}`；upsert Answers.marked，返回 `{message:"ok"}` |
| GET | `/exam/draft/{videoid}/{token}` | **改造**：返回增加 `marked:[examid,...]` 集合 |
| POST | `/exam/submit/{videoid}/{token}` | **改造**：体新增 `duration_seconds:int=0`（前端正计时上报，仅用于统计）；blank 题参与判分；选择题分值改用 `exam.score`；保持「全部作答才能提交」的现有规则，不做强制交卷 |

**submit 改造后响应（JSON，替代原纯文本总分）：**
```json
{
  "message": "ok",
  "total": 85,
  "duration_seconds": 620,
  "results": [{"examid":1,"type":"choice","score":5,"correct":true,"answer":"A","user_answer":"A"}],
  "wrong_examids": [3, 7],
  "analysis_pending": true
}
```

> 无倒计时、无强制交卷：页面只保留正计时显示用时，提交时机完全由学生决定，且必须答完全部题目（沿用现有校验）。
保留 `"Token-Fail"` / `"Already-Exam"` 纯文本响应以兼容旧前端判断。

**判分与错题落库流程（submit 内部）：**
1. 逐题判分：choice 对答案；blank 走 `grade_blank`（规范化：去空白、全角转半角、大小写折叠，命中 answer 或 alt_answers 满分否则 0）；qa 走 `ai_score`
2. 写 Scores（原有唯一约束防重）+ 最终答案同步 Answers（原有逻辑）
3. 错题 upsert WrongBook：新错题 `wrong_count=1`；已存在则 `wrong_count+1`、`mastered=False`、更新 `last_answer`
4. 写 PracticeSessions（mode/total/correct/score/duration）
5. 错题的 AI 解析提交到 `worker` 线程池异步执行 `gen_wrong_analysis`（结果缓存到 `exams.analysis`），接口立即返回，保证 <200ms

### 4.3 错题本

| 方法 | 路径 | 响应 |
|------|------|------|
| GET | `/api/wrongbook/list/{token}` | `{items:[{wrongid,examid,videoid,videotitle,question,type,score,options,knowledge_point,wrong_count,mastered,last_answer,standard_answer,analysis}], pending_review, mastered_count}`；query `mastered=true/false` 筛选，默认未掌握在前 |
| POST | `/api/wrongbook/master/{wrongid}/{token}` | 体：`{mastered:bool}` → `{message:"ok"}` |
| GET | `/api/wrongbook/analysis/{examid}/{token}` | 单题解析查询：`{analysis}`；解析异步未就绪时 `{analysis:null}`，前端轮询此接口 |

**错题重练**：v1 按单视频进行 — `GET /exam/{videoid}?mode=redo` 时服务端只渲染该用户在 WrongBook 中未掌握且属于该 videoid 的题目；交卷仍走 `/exam/submit`（同一视频内）。跨视频混合组卷不做。

### 4.4 学习分析

| 方法 | 路径 | 响应 |
|------|------|------|
| GET | `/api/stats/overview/{token}` | 见下方结构 |

```json
{
  "summary": {"total_answers": 42, "accuracy": 0.71, "total_duration_seconds": 3600, "pending_wrong": 5},
  "by_knowledge_point": [{"knowledge_point":"导数","total":10,"correct":7,"accuracy":0.7}],
  "by_type": [{"type":"choice","total":20,"correct":15,"accuracy":0.75}],
  "recent_sessions": [{"sessionid":3,"videoid":1,"videotitle":"...","mode":"exam","total_score":85,"duration_seconds":620,"createtime":"..."}]
}
```
- `total_answers/accuracy`：来自 Scores 关联 Exams（choice/blank 以 score>0 判对；qa 以得分率≥60% 判对）
- `recent_sessions`：最近 10 条 PracticeSessions
- 纯 SQL 聚合，无 AI 调用

### 4.5 module.py 配套改造
- `gen_exam`：新结构 6 选择 + 3 填空 + 2 简答（满分 100 = 6×5+3×10+2×20），每题带 `knowledge_point` 与 `analysis`；`_validate_exam` 同步验证三题型
- `insert_exam`：按新结构入库（blank 存 alt_answers、写 knowledge_point/analysis）
- 新增 `grade_blank(user_answer, standard, alt_answers) -> bool`
- 新增 `gen_wrong_analysis(exam, student_answer) -> str|None`：优先返回 `exams.analysis` 缓存；无缓存则 AI 生成（错因+正确思路，150 字内）并写回缓存；失败返回 None 不阻塞

## 五、前端（粗略展示，不美化）

仅保证能操作/能看到结果，沿用现有 style.css，少量补充基础样式（表格、卡片边框即可），**不做响应式、不做 SVG 图表**：

1. **exam.html / script.js 最小改造**：
   - 渲染 blank 题（文本输入框 `id="blank-{examid}"`），纳入草稿保存/恢复/提交收集
   - 题头显示知识点标签文本 + 标记复选框（调 `/exam/mark`）
   - 保留现有正计时「用时 mm:ss」，提交时上报 `duration_seconds`；**不加倒计时**
   - 每题作答后可点「立即判分」按钮调 `/api/practice/judge`，在该题下方直接显示对错/解析文本（客观题秒回；简答题判分耗时长，按钮加 loading）
   - `doExam` 适配 JSON 响应：alert 总分 + 错题题号；解析 pending 时提示稍后查看错题本
2. **templates/bank.html**：筛选下拉+搜索框+题目表格+一个新增/编辑表单（题型切换显示对应字段），按钮直调接口，alert 结果后刷新
3. **templates/wrongbook.html**：错题列表（题干、知识点、错误次数、我的答案、标准答案、解析文本，解析为空时 3s 轮询），「已掌握」按钮、「去重练」链接（按视频分组）
4. **templates/stats.html**：概览数字 + 知识点/题型正确率用 HTML 表格 + 文本进度条（`█░` 字符或 div 宽度）粗略展示；近 10 次成绩表格
5. **index.html 菜单**：加 `题库 / 错题本 / 学习报告` 文本链接

## 六、测试（tests/smoke_test.py 扩展）

沿用 TestClient + mock AI 风格：
1. **迁移**：无新列的老库启动后新列齐全、知识点回填正确
2. **题库 CRUD**：三题型新增 → 筛选命中 → 编辑 → 删除后关联行清理；非法字段被拒
3. **填空判分**：大小写/全角/空格规范化、备选答案命中、错误 0 分
4. **practice/judge**：choice 对/错分支、返回标准答案与解析
5. **submit 改造**：含 blank 的整卷判分正确；错题自动入 WrongBook；未答完仍被拒（400）；返回结构含 wrong_examids 与 duration_seconds；Already-Exam 兼容
6. **错题本**：list/master 切换/analysis 轮询接口（mock gen_wrong_analysis）
7. **stats/overview**：知识点分组、题型分组、会话列表、用时聚合正确
8. 更新既有断言：AI 出题 mock 改 6+3+2 结构、总分期望值重算

验证：`D:\anaconda3\python.exe -m pytest tests\smoke_test.py -q` 全绿 + 真实库启动 uvicorn 手动过一遍主流程。

## 七、实施顺序

1. model.py：新表/新列 + 迁移逻辑
2. module.py：gen_exam/_validate_exam/insert_exam、grade_blank、gen_wrong_analysis
3. main.py：4.1–4.4 全部接口（含 submit 改造）
4. smoke_test.py 扩展并运行通过
5. 前端粗略页面（bank/wrongbook/stats/exam 改造 + 菜单入口）
6. 真实库启动验证主流程
7. git 提交（建议：`feat: 学生做题系统后端（题库/批改/错题本/学习分析）`）

## 八、假设与决策记录

- 无倒计时、无强制交卷：前端正计时仅用于统计用时；必须答完全部题目才可提交（沿用现有规则）
- 每个视频固定一套练习题（6选择+3填空+2简答），即时判分通过「立即判分」按钮逐题触发
- 错题重练 v1 限单视频内；AI 解析缓存于 `exams.analysis` 全用户共享，异步生成
- 填空题纯本地判分（含备选答案），不走 AI，保证即时性
- 满分 100 制（30+30+40），旧测试断言同步更新
- 前端本轮不做美化/响应式/图表库，后续单独重构
- 不改 AES token、登录注册、视频处理管线的现有行为
