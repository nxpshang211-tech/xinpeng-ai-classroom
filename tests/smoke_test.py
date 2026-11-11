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
        from model import Exams, Scores, Videos, WrongBook, engine, init_database
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
            "singles": [
                {
                    "question": f"单选题{i}",
                    "options": {"A": "甲", "B": "乙", "C": "丙", "D": "丁"},
                    "answer": "A",
                    "knowledge_point": f"知识点{i}",
                    "difficulty": 1 if i < 2 else (2 if i < 5 else 3),
                    "analysis": "单选题解析",
                }
                for i in range(6)
            ],
            "multis": [
                {
                    "question": f"多选题{i}",
                    "options": {"A": "甲", "B": "乙", "C": "丙", "D": "丁"},
                    "answer": "ABD",
                    "knowledge_point": f"知识点{i}",
                    "difficulty": 2,
                    "analysis": "多选题解析",
                }
                for i in range(4)
            ],
            "judges": [
                {
                    "question": f"判断题{i}",
                    "answer": "T" if i % 2 == 0 else "F",
                    "knowledge_point": f"知识点{i}",
                    "difficulty": 2,
                    "analysis": "判断题解析",
                }
                for i in range(6)
            ],
        }

        xmind_output = service.write_xmind_file(mindmap, "artifact.mp4")
        assert xmind_output.is_file()
        service.gen_key_frames(sample)
        assert (settings.static_dir / "frames" / sample.name / ".complete").is_file()
        print("xmind + key frames: OK")

        # 组卷蓝图校验：题量/选项/答案格式的硬校验
        assert service._validate_exam(exam) is None
        with_bad_count = {**exam, "singles": exam["singles"][:5]}
        try:
            service._validate_exam(with_bad_count)
            raise AssertionError("题量不足应校验失败")
        except ValueError:
            pass
        with_bad_multi = {
            **exam,
            "multis": [{**exam["multis"][0], "answer": "A"}] + exam["multis"][1:],
        }
        try:
            service._validate_exam(with_bad_multi)
            raise AssertionError("多选单答案应校验失败")
        except ValueError:
            pass
        with_long_option = {
            **exam,
            "judges": [{**exam["judges"][0], "question": "长" * 501}] + exam["judges"][1:],
        }
        try:
            service._validate_exam(with_long_option)
            raise AssertionError("超长题干应校验失败")
        except ValueError:
            pass
        # 旧格式试卷拒绝入库
        try:
            service.insert_exam(json.dumps({"choices": [], "blanks": [], "qas": []}), videoid)
            raise AssertionError("旧格式应被拒绝")
        except ValueError:
            pass
        print("exam validation: OK")

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
            assert len(questions) == 16
            by_type = {}
            for item in questions:
                by_type[item.type] = by_type.get(item.type, 0) + 1
                assert item.difficulty in (1, 2, 3)
            assert by_type == {"choice": 6, "multi": 4, "judge": 6}
        print("mock pipeline: OK")

        # 老库迁移：缺列自动补齐 + 知识点/难度回填 + blank/qa 数据清理
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
            "INSERT INTO exams (examid, videoid, question, type, answer, score) "
            "VALUES (1, NULL, '老题', 'choice', 'A', 5)"
        )
        conn.execute(
            "INSERT INTO exams (examid, videoid, question, type, answer, score) "
            "VALUES (2, NULL, '老填空', 'blank', 'DNA', 10)"
        )
        conn.execute(
            "INSERT INTO exams (examid, videoid, question, type, answer, score) "
            "VALUES (3, NULL, '老简答', 'qa', '参考答案', 20)"
        )
        conn.execute(
            "CREATE TABLE answers (answerid INTEGER PRIMARY KEY, userid INTEGER, "
            "examid INTEGER, answer TEXT)"
        )
        conn.execute("INSERT INTO answers (userid, examid, answer) VALUES (1, 2, 'DNA')")
        conn.execute(
            "CREATE TABLE scores (scoreid INTEGER PRIMARY KEY, userid INTEGER, "
            "videoid INTEGER, examid INTEGER, answer TEXT, score INTEGER)"
        )
        conn.execute("INSERT INTO scores (userid, examid, answer, score) VALUES (1, 2, 'DNA', 10)")
        conn.execute(
            "CREATE TABLE wrongbook (wrongid INTEGER PRIMARY KEY, userid INTEGER, "
            "examid INTEGER, videoid INTEGER, wrong_count INTEGER, "
            "mastered BOOLEAN, last_answer TEXT)"
        )
        conn.execute(
            "INSERT INTO wrongbook (userid, examid, wrong_count, mastered, last_answer) "
            "VALUES (1, 3, 1, 0, 'x')"
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
        score_cols = {row[1] for row in conn.execute("PRAGMA table_info(scores)")}
        wrong_cols = {row[1] for row in conn.execute("PRAGMA table_info(wrongbook)")}
        assert {"knowledge_point", "analysis", "alt_answers", "difficulty"} <= exam_cols
        assert "marked" in answer_cols
        assert "mode" in score_cols
        assert {"wrong_reason", "wrong_analysis", "correct_streak"} <= wrong_cols
        row = conn.execute("SELECT knowledge_point, difficulty FROM exams").fetchone()
        assert row == ("综合", 2), row
        # blank/qa 题目及关联记录被级联清除，仅保留单选题
        assert conn.execute("SELECT COUNT(*) FROM exams").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM answers").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM wrongbook").fetchone()[0] == 0
        conn.close()
        old_engine.dispose()
        print("migration: OK")

        from fastapi.testclient import TestClient
        from main import app

        # 错因异步生成的本地替身：同步写回该生 WrongBook 行
        def fake_reason(exam, student_answer, userid):
            with Session(engine) as session:
                row = session.exec(
                    select(WrongBook).where(
                        WrongBook.userid == userid, WrongBook.examid == exam.examid
                    )
                ).first()
                if row is not None and not row.wrong_reason:
                    row.wrong_reason = "概念混淆"
                    row.wrong_analysis = f"错因分析：{student_answer}"
                    session.add(row)
                    session.commit()
            return ("概念混淆", f"错因分析：{student_answer}")

        def wait_reasons(client, token, expected, timeout=10):
            deadline = time.time() + timeout
            while time.time() < deadline:
                wb = client.get(f"/api/wrongbook/list/{token}").json()
                if sum(1 for item in wb["items"] if item["wrong_reason"]) >= expected:
                    return wb
                time.sleep(0.1)
            raise AssertionError("错因标签未在超时前生成")

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
            exam_page = client.get(f"/exam/{videoid}")
            assert exam_page.status_code == 200
            assert 'class="multi-box"' in exam_page.text
            assert 'class="judge-box"' in exam_page.text
            assert "blank-box" not in exam_page.text and "qa-box" not in exam_page.text

            with Session(engine) as session:
                generated = session.exec(select(Exams).where(Exams.videoid == videoid)).all()
            choice_exam = next(item for item in generated if item.type == "choice")
            multi_exam = next(item for item in generated if item.type == "multi")
            judge_exam = next(item for item in generated if item.type == "judge")

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
                json={"examid": multi_exam.examid, "answer": "ABD"},
            )
            assert saved.json()["message"] == "saved"
            draft = client.get(f"/exam/draft/{videoid}/{token}").json()
            assert draft["submitted"] is False
            assert draft["answers"][str(choice_exam.examid)] == "A"
            assert draft["answers"][str(multi_exam.examid)] == "ABD"
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

            # 即时判分：三类客观题本地秒判
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

            # 多选判分：全对满分 / 乱序归一 / 漏选半分 / 错选零分
            judged = client.post(
                f"/api/practice/judge/{token}",
                json={"examid": multi_exam.examid, "answer": "DBA"},
            ).json()
            assert judged["correct"] is True and judged["score"] == 10
            judged = client.post(
                f"/api/practice/judge/{token}",
                json={"examid": multi_exam.examid, "answer": "AB"},
            ).json()
            assert judged["correct"] is False and judged["score"] == 5
            judged = client.post(
                f"/api/practice/judge/{token}",
                json={"examid": multi_exam.examid, "answer": "ABC"},
            ).json()
            assert judged["correct"] is False and judged["score"] == 0
            # 关闭漏选半分后：漏选也得 0 分
            object.__setattr__(settings, "multi_partial_credit", False)
            try:
                judged = client.post(
                    f"/api/practice/judge/{token}",
                    json={"examid": multi_exam.examid, "answer": "AB"},
                ).json()
                assert judged["score"] == 0
            finally:
                object.__setattr__(settings, "multi_partial_credit", True)

            # 判断判分：T/F 与小写容错
            judge_answer = judge_exam.answer
            judged = client.post(
                f"/api/practice/judge/{token}",
                json={"examid": judge_exam.examid, "answer": judge_answer.lower()},
            ).json()
            assert judged["correct"] is True and judged["score"] == 5
            judged = client.post(
                f"/api/practice/judge/{token}",
                json={"examid": judge_exam.examid, "answer": "F" if judge_answer == "T" else "T"},
            ).json()
            assert judged["correct"] is False and judged["score"] == 0

            rejected = client.post(
                "/api/practice/judge/bad-token",
                json={"examid": choice_exam.examid, "answer": "A"},
            )
            assert rejected.json() == "Token-Fail"
            print("mark + judge: OK")

            # 提交整卷：1 道单选答错、1 道多选漏选（半分仍计错题）、其余全对
            with Session(engine) as session:
                generated = session.exec(select(Exams).where(Exams.videoid == videoid)).all()
            choice_exams = [item for item in generated if item.type == "choice"]
            multi_exams = [item for item in generated if item.type == "multi"]
            judge_exams = [item for item in generated if item.type == "judge"]
            choices = [f"choice-{item.examid}-{item.answer}" for item in choice_exams[:-1]]
            wrong_choice = choice_exams[-1]
            choices.append(f"choice-{wrong_choice.examid}-B")  # 故意答错
            partial_multi = multi_exams[-1]
            choices.append(f"multi-{partial_multi.examid}-AB")  # 漏选 D，半分
            choices += [f"multi-{item.examid}-{item.answer}" for item in multi_exams[:-1]]
            choices += [f"judge-{item.examid}-{item.answer}" for item in judge_exams]
            answers = {"choices": choices, "duration_seconds": 100}

            # 未答完必须被拒
            incomplete = client.post(
                f"/exam/submit/{videoid}/{token}", json={"choices": choices[:-3]}
            )
            assert incomplete.status_code == 400

            # 题型前缀与库中题目不符必须被拒
            mismatch = dict(answers)
            mismatch["choices"] = [
                f"choice-{item.examid}-{item.answer}" for item in generated
            ]
            assert client.post(
                f"/exam/submit/{videoid}/{token}", json=mismatch
            ).status_code == 400

            with patch("main.gen_wrong_reason", side_effect=fake_reason):
                submitted = client.post(
                    f"/exam/submit/{videoid}/{token}", json=answers
                )
            result = submitted.json()
            # 总分 = 100 - 5(单选答错) - 5(多选漏选半分) = 90
            assert result["message"] == "ok", submitted.text
            assert result["total"] == 90
            assert result["duration_seconds"] == 100
            assert set(result["wrong_examids"]) == {
                wrong_choice.examid,
                partial_multi.examid,
            }
            assert len(result["results"]) == 16
            assert result["analysis_pending"] is True

            # 提交后草稿落定：标记已提交、总分与答案可回显
            final = client.get(f"/exam/draft/{videoid}/{token}").json()
            assert final["submitted"] is True
            assert final["total"] == 90
            assert len(final["answers"]) == 16
            repeated = client.post(f"/exam/submit/{videoid}/{token}", json=answers)
            assert repeated.json() == "Already-Exam", repeated.text

            # Scores.mode 首答口径
            with Session(engine) as session:
                rows = session.exec(select(Scores).where(Scores.userid == 1)).all()
                assert all(row.mode == "exam" for row in rows)

            # 错题本：2 道错题自动收录，错因标签异步生成
            wb = wait_reasons(client, token, expected=2)
            assert wb["pending_review"] == 2
            assert wb["mastered_count"] == 0
            assert {item["examid"] for item in wb["items"]} == {
                wrong_choice.examid,
                partial_multi.examid,
            }
            assert all(item["wrong_reason"] == "概念混淆" for item in wb["items"])
            assert all(item["correct_streak"] == 0 for item in wb["items"])
            first_wrong = wb["items"][0]
            ana = client.get(
                f"/api/wrongbook/analysis/{first_wrong['examid']}/{token}"
            ).json()
            assert ana["wrong_reason"] == "概念混淆" and ana["wrong_analysis"]
            # 错因筛选
            filtered = client.get(
                f"/api/wrongbook/list/{token}", params={"reason": "概念混淆"}
            ).json()
            assert len(filtered["items"]) == 2
            filtered = client.get(
                f"/api/wrongbook/list/{token}", params={"reason": "粗心失误"}
            ).json()
            assert len(filtered["items"]) == 0
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

            # 学习分析（首答后）：双口径正确率 + 错因分布
            stats = client.get(f"/api/stats/overview/{token}").json()
            assert stats["summary"]["total_answers"] == 16
            assert stats["summary"]["first_answers"] == 16
            assert stats["summary"]["first_accuracy"] == 0.875  # 14/16
            assert stats["summary"]["overall_accuracy"] == 0.875
            assert stats["summary"]["pending_wrong"] == 2
            assert stats["by_wrong_reason"] == [{"reason": "概念混淆", "count": 2}]
            assert {b["type"] for b in stats["by_type"]} == {"choice", "multi", "judge"}

            # 错题重练第 1 次答对：连对 1/2，尚未自动移出
            redo_answers = {
                "mode": "redo",
                "duration_seconds": 50,
                "choices": [
                    f"choice-{wrong_choice.examid}-{wrong_choice.answer}",
                    f"multi-{partial_multi.examid}-{partial_multi.answer}",
                ],
            }
            redo = client.post(
                f"/exam/submit/{videoid}/{token}", json=redo_answers
            ).json()
            assert redo["total"] == 15, redo
            wb = client.get(f"/api/wrongbook/list/{token}").json()
            assert wb["pending_review"] == 2
            assert all(item["correct_streak"] == 1 for item in wb["items"])

            # 错题重练第 2 次答对：连对 2/2，自动标记已掌握
            redo2 = client.post(
                f"/exam/submit/{videoid}/{token}", json=redo_answers
            ).json()
            assert redo2["total"] == 15, redo2
            wb = client.get(f"/api/wrongbook/list/{token}").json()
            assert wb["pending_review"] == 0
            assert wb["mastered_count"] == 2
            print("submit + wrongbook + redo: OK")

            # 学习分析（重练后）：首答/含重做口径分离
            stats = client.get(f"/api/stats/overview/{token}").json()
            assert stats["summary"]["total_answers"] == 16
            # 重练覆盖的 2 行 mode 变为 redo，首答口径只剩 14 行且全对
            assert stats["summary"]["first_answers"] == 14
            assert stats["summary"]["first_accuracy"] == 1.0
            assert stats["summary"]["overall_accuracy"] == 1.0
            assert stats["summary"]["total_duration_seconds"] == 200
            assert stats["summary"]["pending_wrong"] == 0
            assert stats["by_wrong_reason"] == []
            assert len(stats["by_knowledge_point"]) > 0
            assert len(stats["recent_sessions"]) == 3
            assert stats["recent_sessions"][0]["mode"] == "redo"
            assert stats["recent_sessions"][2]["total_score"] == 90

            # 实测难度回填：只统计首答记录，样本不足时不给出实测正确率
            diff_stats = client.get("/api/bank/difficulty-stats").json()["items"]
            by_id = {item["examid"]: item for item in diff_stats}
            # 被重练覆盖的 2 题已转为 redo 口径，不计入首答统计
            assert wrong_choice.examid not in by_id
            assert partial_multi.examid not in by_id
            sample_stat = by_id[choice_exam.examid]
            assert sample_stat["attempts"] == 1 and sample_stat["correct_count"] == 1
            assert sample_stat["measured_accuracy"] is None  # attempts < 5

            # 题库管理：三类客观题新增、筛选、编辑、删除与级联清理
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
                    "difficulty": 1,
                },
            ).json()
            assert added["message"] == "ok", added
            multi_added = client.post(
                f"/api/bank/add/{token}",
                json={
                    "question": "下列属于导数应用的有",
                    "type": "multi",
                    "options": {"A": "甲", "B": "乙", "C": "丙", "D": "丁"},
                    "answer": "dba",  # 小写乱序，应自动归一为 ABD
                    "knowledge_point": "导数",
                },
            ).json()
            assert multi_added["message"] == "ok"
            judge_added = client.post(
                f"/api/bank/add/{token}",
                json={
                    "question": "导数反映函数的瞬时变化率",
                    "type": "judge",
                    "answer": "T",
                    "knowledge_point": "导数",
                },
            ).json()
            assert judge_added["message"] == "ok"
            # 非法录入：多选单答案 / 判断非 T/F / 旧题型 / 超长字段
            bad_multi = client.post(
                f"/api/bank/add/{token}",
                json={
                    "question": "x",
                    "type": "multi",
                    "options": {"A": "1", "B": "2", "C": "3", "D": "4"},
                    "answer": "A",
                },
            ).json()
            assert bad_multi["message"] == "invalid-field"
            bad_judge = client.post(
                f"/api/bank/add/{token}",
                json={"question": "x", "type": "judge", "answer": "对"},
            ).json()
            assert bad_judge["message"] == "invalid-field"
            legacy = client.post(
                f"/api/bank/add/{token}",
                json={"question": "1+1=____", "type": "blank", "answer": "2"},
            ).json()
            assert legacy["message"] == "invalid-field"
            too_long = client.post(
                f"/api/bank/add/{token}",
                json={
                    "question": "长" * 501,
                    "type": "judge",
                    "answer": "T",
                },
            ).json()
            assert too_long["message"] == "invalid-field"
            long_option = client.post(
                f"/api/bank/add/{token}",
                json={
                    "question": "x",
                    "type": "choice",
                    "options": {"A": "甲" * 201, "B": "乙", "C": "丙", "D": "丁"},
                    "answer": "A",
                },
            ).json()
            assert long_option["message"] == "invalid-field"
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

            listed = client.get("/api/bank/list", params={"type": "multi"}).json()
            assert listed["total"] == 5  # 4 道 AI 生成 + 1 道手动录入
            normalized = next(
                item for item in listed["items"] if item["examid"] == multi_added["examid"]
            )
            assert normalized["answer"] == "ABD"
            assert normalized["difficulty"] == 2  # 缺省难度为中
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
                    "difficulty": 3,
                },
            ).json()
            assert updated["message"] == "ok"
            listed = client.get("/api/bank/list", params={"keyword": "导数定义"}).json()
            assert listed["items"][0]["answer"] == "B"
            assert listed["items"][0]["difficulty"] == 3
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
