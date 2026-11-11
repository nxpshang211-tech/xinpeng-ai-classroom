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
            video = Videos(videotitle="模拟课程", filename=filename)
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
                    "knowledge_point": f"知识点{i}",
                    "analysis": "选择题解析",
                }
                for i in range(6)
            ],
            "blanks": [
                {
                    "question": f"填空题{i}",
                    "answer": "DNA",
                    "alt_answers": ["脱氧核糖核酸"],
                    "knowledge_point": f"知识点{i}",
                    "analysis": "填空题解析",
                }
                for i in range(3)
            ],
            "qas": [
                {
                    "question": f"简答题{i}",
                    "answer": "参考答案",
                    "knowledge_point": f"知识点{i}",
                    "analysis": "简答题解析",
                }
                for i in range(2)
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

        # 填空题本地判分：规范化（全角/大小写/空白）与备选答案
        assert service.grade_blank("ＤＮＡ", "DNA")
        assert service.grade_blank(" dna ", "DNA")
        assert service.grade_blank("脱氧核糖核酸", "DNA", ["脱氧核糖核酸"])
        assert not service.grade_blank("RNA", "DNA", ["脱氧核糖核酸"])
        print("grade_blank: OK")

        # 老库迁移：缺列自动补齐 + 知识点回填
        import sqlite3

        from sqlalchemy import create_engine as create_raw_engine

        import model as model_mod

        old_db = temp_dir / "old.db"
        conn = sqlite3.connect(old_db)
        conn.execute(
            "CREATE TABLE exams (examid INTEGER PRIMARY KEY, videoid INTEGER, "
            "question TEXT, type TEXT, answer TEXT, score INTEGER, options TEXT)"
        )
        conn.execute(
            "INSERT INTO exams (videoid, question, type, answer, score) "
            "VALUES (NULL, '老题', 'choice', 'A', 5)"
        )
        conn.execute(
            "CREATE TABLE answers (answerid INTEGER PRIMARY KEY, userid INTEGER, "
            "examid INTEGER, answer TEXT)"
        )
        conn.execute("CREATE TABLE videos (videoid INTEGER PRIMARY KEY, keyword TEXT)")
        conn.commit()
        conn.close()
        old_engine = create_raw_engine(f"sqlite:///{old_db.as_posix()}")
        with patch.object(model_mod, "engine", old_engine):
            model_mod._migrate()
        conn = sqlite3.connect(old_db)
        exam_cols = {row[1] for row in conn.execute("PRAGMA table_info(exams)")}
        answer_cols = {row[1] for row in conn.execute("PRAGMA table_info(answers)")}
        assert {"knowledge_point", "analysis", "alt_answers"} <= exam_cols
        assert "marked" in answer_cols
        assert conn.execute("SELECT knowledge_point FROM exams").fetchone()[0] == "综合"
        conn.close()
        old_engine.dispose()
        print("migration: OK")

        from fastapi.testclient import TestClient
        from main import app

        with TestClient(app) as client, sample.open("rb") as source:
            assert client.get("/").status_code == 200
            assert client.get("/login").status_code == 200
            assert client.get("/register").status_code == 200

            # 注册全流程：查重 -> 注册 -> 重复注册被拒 -> 参数非法被拒 -> 登录
            check = client.post("/api/username-check", json={"username": "newstu"})
            assert check.json()["exists"] is False
            reg = client.post(
                "/register",
                json={"username": "newstu", "realname": "新同学", "password": "pass123"},
            )
            assert reg.json()["message"] == "register-ok", reg.text
            check = client.post("/api/username-check", json={"username": "newstu"})
            assert check.json()["exists"] is True
            dup = client.post(
                "/register",
                json={"username": "newstu", "realname": "x", "password": "pass123"},
            )
            assert dup.json()["message"] == "user-exists"
            bad_name = client.post(
                "/register",
                json={"username": "a", "realname": "x", "password": "pass123"},
            )
            assert bad_name.json()["message"] == "invalid-username"
            bad_pwd = client.post(
                "/register",
                json={"username": "validname", "realname": "x", "password": "123"},
            )
            assert bad_pwd.json()["message"] == "invalid-password"

            login = client.post(
                "/login", json={"username": "newstu", "password": "pass123"}
            )
            assert login.json()["message"] == "login-ok"
            token = login.json()["token"]
            wrong = client.post("/login", json={"username": "newstu", "password": "bad"})
            assert wrong.json()["message"] == "login-fail"
            ghost = client.post("/login", json={"username": "ghost", "password": "x"})
            assert ghost.json()["message"] == "user-not-exist"
            checked = client.post("/checklogin", json={"token": token}).json()
            assert checked["message"] == "Token-OK"
            assert "role" not in checked  # 角色体系已移除
            bad = client.post("/checklogin", json={"token": "not-a-token"}).json()
            assert bad["message"] == "Token-Error"

            assert client.get(f"/detail/{videoid}").status_code == 200
            assert client.get(f"/exam/{videoid}").status_code == 200

            with Session(engine) as session:
                generated = session.exec(select(Exams).where(Exams.videoid == videoid)).all()
            choice_exam = next(item for item in generated if item.type == "choice")
            qa_exam = next(item for item in generated if item.type == "qa")

            # 草稿实时保存：未登录拦截、逐题保存、改答案覆盖、刷新后恢复
            rejected = client.post(
                f"/exam/draft/{videoid}/bad-token",
                json={"examid": choice_exam.examid, "answer": "A"},
            )
            assert rejected.json() == "Token-Fail"
            saved = client.post(
                f"/exam/draft/{videoid}/{token}",
                json={"examid": choice_exam.examid, "answer": "A"},
            )
            assert saved.json()["message"] == "saved"
            saved = client.post(
                f"/exam/draft/{videoid}/{token}",
                json={"examid": qa_exam.examid, "answer": "草稿简答"},
            )
            assert saved.json()["message"] == "saved"
            draft = client.get(f"/exam/draft/{videoid}/{token}").json()
            assert draft["submitted"] is False
            assert draft["answers"][str(choice_exam.examid)] == "A"
            assert draft["answers"][str(qa_exam.examid)] == "草稿简答"
            client.post(
                f"/exam/draft/{videoid}/{token}",
                json={"examid": choice_exam.examid, "answer": "B"},
            )
            restored = client.get(f"/exam/draft/{videoid}/{token}").json()
            assert restored["answers"][str(choice_exam.examid)] == "B"
            client.post(
                f"/exam/draft/{videoid}/{token}",
                json={"examid": choice_exam.examid, "answer": "A"},
            )
            ghost_exam = client.post(
                f"/exam/draft/{videoid}/{token}",
                json={"examid": 999999, "answer": "A"},
            )
            assert ghost_exam.status_code == 404

            # 题目标记：打标后草稿接口返回 marked 集合
            marked = client.post(
                f"/exam/mark/{videoid}/{token}",
                json={"examid": choice_exam.examid, "marked": True},
            )
            assert marked.json()["message"] == "ok"
            draft = client.get(f"/exam/draft/{videoid}/{token}").json()
            assert choice_exam.examid in draft["marked"]

            # 即时判分：客观题本地秒判，简答题走 AI 评分
            judged = client.post(
                f"/api/practice/judge/{token}",
                json={"examid": choice_exam.examid, "answer": "A"},
            ).json()
            assert judged["correct"] is True and judged["score"] == 5
            judged = client.post(
                f"/api/practice/judge/{token}",
                json={"examid": choice_exam.examid, "answer": "C"},
            ).json()
            assert judged["correct"] is False and judged["answer"] == "A"
            blank_exam = next(item for item in generated if item.type == "blank")
            judged = client.post(
                f"/api/practice/judge/{token}",
                json={"examid": blank_exam.examid, "answer": "dna"},
            ).json()
            assert judged["correct"] is True and judged["score"] == 10
            with patch("main.ai_score", return_value=15):
                judged = client.post(
                    f"/api/practice/judge/{token}",
                    json={"examid": qa_exam.examid, "answer": "我的回答"},
                ).json()
            assert judged["score"] == 15 and judged["analysis"] == "参考答案"
            rejected = client.post(
                "/api/practice/judge/bad-token",
                json={"examid": choice_exam.examid, "answer": "A"},
            )
            assert rejected.json() == "Token-Fail"
            print("mark + judge: OK")

            # 提交整卷：5 道选择全对、1 道答错；2 道填空对、1 道答错；简答各 15 分
            with Session(engine) as session:
                generated = session.exec(select(Exams).where(Exams.videoid == videoid)).all()
            choice_exams = [item for item in generated if item.type == "choice"]
            blank_exams = [item for item in generated if item.type == "blank"]
            choices = [
                f"choice-{item.examid}-{item.answer}" for item in choice_exams[:-1]
            ]
            wrong_choice = choice_exams[-1]
            choices.append(f"choice-{wrong_choice.examid}-B")  # 故意答错
            wrong_blank = blank_exams[-1]
            answers = {"choices": choices, "duration_seconds": 100}
            answers.update(
                {
                    f"blank-{item.examid}": item.answer for item in blank_exams[:-1]
                }
            )
            answers[f"blank-{wrong_blank.examid}"] = "错误答案"
            answers.update(
                {
                    f"qa-{item.examid}": "学生简答"
                    for item in generated
                    if item.type == "qa"
                }
            )

            # 未答完必须被拒
            incomplete = client.post(
                f"/exam/submit/{videoid}/{token}", json={"choices": choices}
            )
            assert incomplete.status_code == 400

            with patch("main.ai_score", return_value=15):
                submitted = client.post(
                    f"/exam/submit/{videoid}/{token}", json=answers
                )
            result = submitted.json()
            # 总分 = 5×5(选择) + 2×10(填空) + 2×15(简答) = 75
            assert result["message"] == "ok", submitted.text
            assert result["total"] == 75
            assert result["duration_seconds"] == 100
            assert set(result["wrong_examids"]) == {
                wrong_choice.examid,
                wrong_blank.examid,
            }
            assert len(result["results"]) == 11

            # 提交后草稿落定：标记已提交、总分与每题得分可回显
            final = client.get(f"/exam/draft/{videoid}/{token}").json()
            assert final["submitted"] is True
            assert final["total"] == 75
            assert len(final["answers"]) == 11
            repeated = client.post(f"/exam/submit/{videoid}/{token}", json=answers)
            assert repeated.json() == "Already-Exam", repeated.text

            # 错题本：2 道错题自动收录，解析来自题目缓存
            wb = client.get(f"/api/wrongbook/list/{token}").json()
            assert wb["pending_review"] == 2
            assert wb["mastered_count"] == 0
            assert {item["examid"] for item in wb["items"]} == {
                wrong_choice.examid,
                wrong_blank.examid,
            }
            assert all(item["analysis"] for item in wb["items"])
            first_wrong = wb["items"][0]
            ana = client.get(
                f"/api/wrongbook/analysis/{first_wrong['examid']}/{token}"
            ).json()
            assert ana["analysis"]
            mastered = client.post(
                f"/api/wrongbook/master/{first_wrong['wrongid']}/{token}",
                json={"mastered": True},
            )
            assert mastered.json()["message"] == "ok"
            wb = client.get(f"/api/wrongbook/list/{token}").json()
            assert wb["mastered_count"] == 1
            client.post(
                f"/api/wrongbook/master/{first_wrong['wrongid']}/{token}",
                json={"mastered": False},
            )

            # 错题重练：答对后自动标记掌握，旧成绩被覆盖
            redo_answers = {
                "mode": "redo",
                "duration_seconds": 50,
                "choices": [f"choice-{wrong_choice.examid}-{wrong_choice.answer}"],
                f"blank-{wrong_blank.examid}": wrong_blank.answer,
            }
            redo = client.post(
                f"/exam/submit/{videoid}/{token}", json=redo_answers
            ).json()
            assert redo["total"] == 15, redo
            wb = client.get(f"/api/wrongbook/list/{token}").json()
            assert wb["pending_review"] == 0
            print("submit + wrongbook + redo: OK")

            # 学习分析：聚合正确率、知识点、题型、会话与用时
            stats = client.get(f"/api/stats/overview/{token}").json()
            assert stats["summary"]["total_answers"] == 11
            assert stats["summary"]["accuracy"] == 1.0  # 重练后全部答对
            assert stats["summary"]["total_duration_seconds"] == 150
            assert stats["summary"]["pending_wrong"] == 0
            assert {b["type"] for b in stats["by_type"]} == {"choice", "blank", "qa"}
            assert len(stats["by_knowledge_point"]) > 0
            assert len(stats["recent_sessions"]) == 2
            assert stats["recent_sessions"][0]["mode"] == "redo"
            assert stats["recent_sessions"][1]["total_score"] == 75

            # 题库管理：三题型新增、筛选、编辑、删除与级联清理
            assert client.get("/bank").status_code == 200
            assert client.get("/wrongbook").status_code == 200
            assert client.get("/stats").status_code == 200
            added = client.post(
                f"/api/bank/add/{token}",
                json={
                    "question": "导数的定义",
                    "type": "choice",
                    "options": {"A": "甲", "B": "乙", "C": "丙", "D": "丁"},
                    "answer": "A",
                    "knowledge_point": "导数",
                },
            ).json()
            assert added["message"] == "ok", added
            blank_added = client.post(
                f"/api/bank/add/{token}",
                json={
                    "question": "1+1=____",
                    "type": "blank",
                    "answer": "2",
                    "alt_answers": ["二"],
                    "knowledge_point": "算术",
                },
            ).json()
            assert blank_added["message"] == "ok"
            bad_add = client.post(
                f"/api/bank/add/{token}",
                json={
                    "question": "x",
                    "type": "choice",
                    "answer": "A",
                    "options": {"A": "1", "B": "2"},
                },
            ).json()
            assert bad_add["message"] == "invalid-field"
            rejected = client.post("/api/bank/add/bad-token", json={})
            assert rejected.json() == "Token-Fail"
            listed = client.get("/api/bank/list", params={"type": "blank"}).json()
            assert listed["total"] == 4  # 3 道 AI 生成 + 1 道手动录入
            kps = client.get("/api/bank/knowledge-points").json()["items"]
            assert "导数" in kps
            updated = client.post(
                f"/api/bank/update/{added['examid']}/{token}",
                json={
                    "question": "导数定义（改）",
                    "type": "choice",
                    "options": {"A": "甲", "B": "乙", "C": "丙", "D": "丁"},
                    "answer": "B",
                    "knowledge_point": "导数",
                },
            ).json()
            assert updated["message"] == "ok"
            listed = client.get("/api/bank/list", params={"keyword": "导数定义"}).json()
            assert listed["items"][0]["answer"] == "B"
            deleted = client.post(f"/api/bank/delete/{added['examid']}/{token}").json()
            assert deleted["message"] == "ok"
            listed = client.get("/api/bank/list", params={"keyword": "导数定义"}).json()
            assert listed["total"] == 0
            print("bank CRUD: OK")

            response = client.post(
                "/upload",
                data={"title": "无Key测试"},
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
