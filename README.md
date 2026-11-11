# 蜗牛 AI 智慧课堂

## 运行步骤

1. 安装依赖：

   ```powershell
   python -m pip install -r requirements.txt
   ```

2. 打开 `.env`，填写：

   ```dotenv
   DASHSCOPE_API_KEY=你的百炼API-Key
   ```

   阿里云搜索配置是可选项；不填写时只跳过扩展资料，其他步骤照常执行。

3. 启动：

   ```powershell
   python main.py
   ```

4. 浏览器访问 <http://127.0.0.1:8000>，上传 MP4。上传完成后，页面会自动显示后台处理进度。

较长视频会自动把音频转换为 16 kHz 单声道并按 4分45秒切片，通过兼容接口以 Base64 Data URI 直接发送给 Qwen ASR，避免大文件限制和临时 OSS 上传连接中断。每段最多重试5次，成功转写会暂存，连接中断后可从失败段继续。已有失败任务在更新代码并重启后可直接点击“重试”，不需要重新上传视频。若需要恢复SDK临时OSS上传方式，可把 `DASHSCOPE_ASR_TRANSPORT` 改成 `oss`。

## 演示账号

| 角色 | 用户名 | 密码 |
|---|---|---|
| 管理员 | `admin` | `admin123` |
| 教师 | `teacher` | `teacher123` |
| 学生 | `student` | `student123` |

SQLite 数据库会在首次启动时自动创建为 `data/course.db`。旧的 `course.sql` 仅保留为历史样例，不参与运行。

## 无 Key 冒烟测试

不填写真实 API Key 也可以验证 SQLite、页面、模拟完整流水线、XMind、关键帧、考试提交和缺 Key 错误处理：

```powershell
python tests/smoke_test.py
```
