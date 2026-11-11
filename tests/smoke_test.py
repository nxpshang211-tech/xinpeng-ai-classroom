"""无需真实 API Key 的本地冒烟测试。

运行：python tests/smoke_test.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))


def main() -> None:
    with tempfile.TemporaryDirectory(
        prefix="chapter07-smoke-", ignore_cleanup_errors=True
    ) as temp_name:
        temp_dir = Path(temp_name)
        os.environ["DATABASE_URL"] = f"sqlite:///{(temp_dir / 'test.db').as_posix()}"
        # Force the no-key path even when the developer has filled the local .env.
        os.environ["DASHSCOPE_API_KEY"] = ""

        from config import settings

        object.__setattr__(settings, "static_dir", temp_dir / "static")
        for name in ("videos", "frames", "xminds"):
            (settings.static_dir / name).mkdir(parents=True, exist_ok=True)

        import module as service
        from model import Exams, Videos, engine, init_database
        from sqlmodel import Session, select

        init_database()
        sample = next((PROJECT_DIR / "ori_videos").glob("*.mp4"))

        filename = "mock.mp4"
        (settings.static_dir / "videos" / filename).write_bytes(b"mock")
        with Session(engine) as session:
            video = Videos(videotitle="模拟课程", filename=filename, coursename="测试")
            session.add(video)
            session.commit()
            session.refresh(video)
            videoid = video.videoid

        mindmap = {
            "meta": {"name": "测试课程", "author": "AI生成", "version": "1.0"},
            "format": "node_tree",
            "data": {"id": "1", "topic": "测试课程", "children": []},
        }
        exam = {
            "choices": [
                {
                    "question": f"选择题{i}",
                    "options": {"A": "甲", "B": "乙", "C": "丙", "D": "丁"},
                    "answer": "A",
                }
                for i in range(8)
            ],
            "qas": [
                {"question": f"简答题{i}", "answer": "参考答案"}
                for i in range(3)
            ],
        }

        xmind_output = service.write_xmind_file(mindmap, "artifact.mp4")
        assert xmind_output.is_file()
        service.gen_key_frames(sample)
        assert (settings.static_dir / "frames" / sample.name / ".complete").is_file()
        print("xmind + key frames: OK")

        def fake_video_audio(_filename: str):
            (settings.static_dir / "videos" / "mock.mp3").write_bytes(b"audio")
            return {"audio_name": "mock.mp3", "duration": 12.5}

        with (
            patch.object(service, "video_audio", side_effect=fake_video_audio),
            patch.object(service, "audio_text", return_value="课堂转写文本"),
            patch.object(service, "summarize", return_value="<p>课堂笔记</p>"),
            patch.object(
                service,
                "gen_xmind_json",
                return_value=json.dumps(mindmap, ensure_ascii=False),
            ),
            patch.object(service, "write_xmind_file"),
            patch.object(service, "gen_key_frames", return_value=[]),
            patch.object(
                service,
                "gen_reference",
                side_effect=service.PipelineError("未配置搜索，已跳过"),
            ),
            patch.object(
                service,
                "gen_exam",
                return_value=json.dumps(exam, ensure_ascii=False),
            ),
        ):
            service.process_video(videoid)

        with Session(engine) as session:
            completed = session.get(Videos, videoid)
            assert completed is not None and completed.status == "completed"
            assert completed.progress == 100
            assert "已跳过" in (completed.warning_message or "")
            questions = session.exec(select(Exams).where(Exams.videoid == videoid)).all()
            assert len(questions) == 11
        print("mock pipeline: OK")

        from fastapi.testclient import TestClient
        from main import app

        with TestClient(app) as client, sample.open("rb") as source:
            assert client.get("/").status_code == 200
            login = client.post(
                "/login", json={"username": "student", "password": "student123"}
            )
            assert login.json()["message"] == "login-ok"
            token = login.json()["token"]
            assert client.post("/checklogin", json={"token": token}).json()["message"] == "Token-OK"
            assert client.get(f"/detail/{videoid}").status_code == 200
            assert client.get(f"/exam/{videoid}").status_code == 200

            with Session(engine) as session:
                generated = session.exec(select(Exams).where(Exams.videoid == videoid)).all()
            choices = [
                f"choice-{item.examid}-{item.answer}"
                for item in generated
                if item.type == "choice"
            ]
            answers = {"choices": choices}
            answers.update(
                {
                    f"qa-{item.examid}": "学生简答"
                    for item in generated
                    if item.type == "qa"
                }
            )
            with patch("main.ai_score", return_value=15):
                submitted = client.post(
                    f"/exam/submit/{videoid}/{token}", json=answers
                )
            assert submitted.json() == 85, submitted.text
            repeated = client.post(f"/exam/submit/{videoid}/{token}", json=answers)
            assert repeated.json() == "Already-Exam", repeated.text

            response = client.post(
                "/upload",
                data={"title": "无Key测试", "course": "测试课程"},
                files={"file": (sample.name, source, "video/mp4")},
            )
            assert response.status_code == 202, response.text
            uploaded_id = response.json()["video_id"]

            status = None
            for _ in range(120):
                status = client.get(f"/api/videos/{uploaded_id}/status").json()
                if status["status"] in {"completed", "failed"}:
                    break
                time.sleep(0.5)
            assert status is not None and status["status"] == "failed", status
            assert status["stage"] == "transcribing", status
            assert "DASHSCOPE_API_KEY" in status["error"], status

            retried = client.post(f"/api/videos/{uploaded_id}/retry")
            assert retried.status_code == 202, retried.text
            for _ in range(30):
                status = client.get(f"/api/videos/{uploaded_id}/status").json()
                if status["status"] == "failed":
                    break
                time.sleep(0.2)
            assert status["status"] == "failed", status
        print("http + missing-key path: OK")
        engine.dispose()


if __name__ == "__main__":
    main()
