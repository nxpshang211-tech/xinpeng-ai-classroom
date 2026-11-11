from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, desc, select

from config import settings
from model import (
    Answers,
    Exams,
    PracticeSessions,
    Scores,
    Users,
    Videos,
    WrongBook,
    engine,
    init_database,
)
from module import (
    MAX_ANALYSIS_LEN,
    MAX_OPTION_LEN,
    MAX_QUESTION_LEN,
    aes_encrypt,
    check_token,
    gen_wrong_reason,
    get_ai_config,
    mask_api_key,
    process_video,
    retry_incomplete_videos,
    save_ai_config,
    test_text_connection,
)


worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="video-pipeline")


def enqueue_video(videoid: int) -> None:
    worker.submit(process_video, videoid)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_database()
    for videoid in retry_incomplete_videos():
        enqueue_video(videoid)
    yield
    worker.shutdown(wait=False)


app = FastAPI(title="欣鹏AI智慧课堂", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")
templates = Jinja2Templates(directory=str(settings.template_dir))


@app.get("/")
def index(request: Request):
    with Session(engine) as session:
        videos = session.exec(select(Videos).order_by(desc(Videos.videoid))).all()
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"results": videos},
    )


@app.get("/detail/{videoid}")
def detail(request: Request, videoid: int):
    with Session(engine) as session:
        video = session.get(Videos, videoid)
    if video is None:
        raise HTTPException(status_code=404, detail="视频不存在")
    if video.status != "completed":
        return templates.TemplateResponse(
            request=request,
            name="processing.html",
            context={"video": video},
        )

    try:
        references = json.loads(video.reference or "[]")
        xmind_data = json.loads(video.xmindjson or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"视频数据格式错误：{exc}") from exc

    frame_dir = settings.static_dir / "frames" / video.filename
    keyframes = sorted(path.name for path in frame_dir.glob("*.jpg"))
    return templates.TemplateResponse(
        request=request,
        name="detail.html",
        context={
            "video": video,
            "references": references,
            "keyframes": keyframes,
            "xmind_data": xmind_data,
        },
    )


def _make_token(user: Users) -> str:
    expired_time = int(time.time()) + 60 * 60 * 24
    return aes_encrypt(
        f"{user.username}|{user.userid}|{user.realname}|{expired_time}"
    )


@app.get("/login")
def login_page(request: Request):
    return templates.TemplateResponse(request=request, name="login.html")


@app.get("/register")
def register_page(request: Request):
    return templates.TemplateResponse(request=request, name="register.html")


@app.post("/login")
def login(logins: dict = Body()):
    username = str(logins.get("username", "")).strip()
    password = str(logins.get("password", ""))
    with Session(engine) as session:
        user = session.exec(select(Users).where(Users.username == username)).first()
    if user is None:
        return {"message": "user-not-exist"}
    if user.password != password:
        return {"message": "login-fail"}
    return {"message": "login-ok", "token": _make_token(user), "username": user.username}


USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_一-龥]{2,20}$")


@app.post("/api/username-check")
def username_check(data: dict = Body()):
    username = str(data.get("username", "")).strip()
    with Session(engine) as session:
        exists = session.exec(select(Users).where(Users.username == username)).first()
    return {"exists": exists is not None}


@app.post("/register")
def register(data: dict = Body()):
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    if not USERNAME_PATTERN.fullmatch(username):
        return {"message": "invalid-username"}
    if len(password) < 6:
        return {"message": "invalid-password"}
    with Session(engine) as session:
        if session.exec(select(Users).where(Users.username == username)).first():
            return {"message": "user-exists"}
        user = Users(username=username, password=password)
        session.add(user)
        session.commit()
        session.refresh(user)
    return {
        "message": "register-ok",
        "token": _make_token(user),
        "username": user.username,
    }


@app.post("/checklogin")
def checklogin(token_data: dict = Body()):
    return check_token(str(token_data.get("token", "")))


@app.post("/upload", status_code=202)
async def upload(
    title: str = Form(),
    file: UploadFile = File(),
):
    if Path(file.filename or "").suffix.lower() != ".mp4":
        raise HTTPException(status_code=400, detail="目前只支持 MP4 视频")
    if not title.strip():
        raise HTTPException(status_code=400, detail="视频标题不能为空")

    filename = datetime.now().strftime("%Y%m%d_%H%M%S_%f.mp4")
    output = settings.static_dir / "videos" / filename
    max_bytes = settings.max_upload_mb * 1024 * 1024
    written = 0
    try:
        with output.open("wb") as target:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"视频不能超过 {settings.max_upload_mb} MB",
                    )
                target.write(chunk)
    except Exception:
        output.unlink(missing_ok=True)
        raise
    finally:
        await file.close()

    with Session(engine) as session:
        video = Videos(
            videotitle=title.strip(),
            filename=filename,
            status="queued",
            stage="uploaded",
            progress=0,
        )
        session.add(video)
        session.commit()
        session.refresh(video)
        videoid = video.videoid

    enqueue_video(videoid)
    return JSONResponse(
        status_code=202,
        content={"video_id": videoid, "status": "queued", "progress": 0},
    )


@app.get("/api/videos/{videoid}/status")
def video_status(videoid: int):
    with Session(engine) as session:
        video = session.get(Videos, videoid)
    if video is None:
        raise HTTPException(status_code=404, detail="视频不存在")
    return {
        "video_id": video.videoid,
        "status": video.status,
        "stage": video.stage,
        "progress": video.progress,
        "error": video.error_message,
        "warning": video.warning_message,
    }


@app.post("/api/videos/{videoid}/retry", status_code=202)
def retry_video(videoid: int):
    with Session(engine) as session:
        video = session.get(Videos, videoid)
        if video is None:
            raise HTTPException(status_code=404, detail="视频不存在")
        if video.status not in {"failed", "queued"}:
            raise HTTPException(status_code=409, detail="当前状态不能重试")
        video.status = "queued"
        video.stage = "queued"
        video.error_message = None
        video.updated_at = datetime.now(timezone.utc)
        session.add(video)
        session.commit()
    enqueue_video(videoid)
    return {"video_id": videoid, "status": "queued"}


@app.get("/exam/{videoid}")
def exam(request: Request, videoid: int):
    with Session(engine) as session:
        video = session.get(Videos, videoid)
        if video is None:
            raise HTTPException(status_code=404, detail="视频不存在")
        if video.status != "completed":
            raise HTTPException(status_code=409, detail="视频尚未处理完成")
        exams = session.exec(select(Exams).where(Exams.videoid == videoid)).all()

    rendered_exams = []
    for item in exams:
        rendered_exams.append(
            {
                "examid": item.examid,
                "videoid": item.videoid,
                "question": item.question,
                "type": item.type,
                "score": item.score,
                "options": json.loads(item.options) if item.options else None,
                "knowledge_point": item.knowledge_point or "综合",
                "difficulty": item.difficulty or 2,
            }
        )
    return templates.TemplateResponse(
        request=request,
        name="exam.html",
        context={"exams": rendered_exams, "videoid": videoid},
    )


@app.post("/exam/draft/{videoid}/{token}")
def save_draft(videoid: int, token: str, draft: dict = Body()):
    token_data = check_token(token)
    if token_data["message"] != "Token-OK":
        return "Token-Fail"
    userid = int(token_data["userid"])
    examid = int(draft.get("examid", 0))
    answer = str(draft.get("answer", ""))
    with Session(engine) as session:
        exam = session.get(Exams, examid)
        if exam is None or exam.videoid != videoid:
            raise HTTPException(status_code=404, detail="题目不存在")
        row = session.exec(
            select(Answers).where(
                Answers.userid == userid, Answers.examid == examid
            )
        ).first()
        if row is None:
            session.add(Answers(userid=userid, examid=examid, answer=answer))
        else:
            row.answer = answer
            row.updated_at = datetime.now(timezone.utc)
            session.add(row)
        session.commit()
    return {"message": "saved"}


@app.get("/exam/draft/{videoid}/{token}")
def get_draft(videoid: int, token: str):
    token_data = check_token(token)
    if token_data["message"] != "Token-OK":
        return "Token-Fail"
    userid = int(token_data["userid"])
    with Session(engine) as session:
        examids = [
            exam.examid
            for exam in session.exec(
                select(Exams).where(Exams.videoid == videoid)
            ).all()
        ]
        drafts = session.exec(
            select(Answers).where(
                Answers.userid == userid, Answers.examid.in_(examids)
            )
        ).all()
        score_rows = session.exec(
            select(Scores).where(
                Scores.userid == userid, Scores.videoid == videoid
            )
        ).all()
    scores = {row.examid: row.score for row in score_rows}
    return {
        "answers": {row.examid: row.answer for row in drafts},
        "marked": [row.examid for row in drafts if row.marked],
        "submitted": bool(score_rows),
        "scores": scores,
        "total": sum(scores.values()) if score_rows else None,
    }


def _is_correct(exam: Exams, score: int) -> bool:
    """客观题统一口径：拿满该题分值才算答对（多选漏选半分仍计错题）。"""
    return score == exam.score


def _judge_one(exam: Exams, user_answer: str) -> int:
    if exam.type == "choice":
        return exam.score if exam.answer == str(user_answer).upper()[:1] else 0
    if exam.type == "multi":
        picked = "".join(sorted(set(str(user_answer).upper()) & set("ABCD")))
        standard = "".join(sorted(exam.answer))
        if picked == standard:
            return exam.score
        if settings.multi_partial_credit and picked and set(picked) < set(standard):
            return exam.score // 2  # 漏选半分
        return 0  # 错选/未选零分
    if exam.type == "judge":
        return exam.score if str(user_answer).upper()[:1] == exam.answer else 0
    return 0


@app.post("/exam/submit/{videoid}/{token}")
def exam_submit(videoid: int, token: str, answers: dict = Body()):
    token_data = check_token(token)
    if token_data["message"] != "Token-OK":
        return "Token-Fail"
    userid = int(token_data["userid"])
    mode = str(answers.get("mode", "exam"))
    duration_seconds = int(answers.get("duration_seconds", 0) or 0)

    with Session(engine) as session:
        if mode != "redo" and session.exec(
            select(Scores).where(
                Scores.userid == userid,
                Scores.videoid == videoid,
            )
        ).first():
            return "Already-Exam"
        exams = session.exec(select(Exams).where(Exams.videoid == videoid)).all()

    by_id = {exam.examid: exam for exam in exams}
    parsed: dict[int, str] = {}  # examid -> user_answer

    for encoded in answers.get("choices", []):
        try:
            prefix, examid_text, answer = encoded.split("-", 2)
            examid = int(examid_text)
        except (ValueError, AttributeError):
            raise HTTPException(status_code=400, detail="答案格式错误")
        question = by_id.get(examid)
        if (
            prefix not in ("choice", "multi", "judge")
            or question is None
            or question.type != prefix
        ):
            raise HTTPException(status_code=400, detail=f"题目不存在或题型不符：{examid}")
        parsed[examid] = answer

    if mode != "redo":
        expected_ids = {exam.examid for exam in exams}
        if expected_ids != set(parsed):
            raise HTTPException(status_code=400, detail="请完成全部题目后再提交")
    elif not parsed:
        raise HTTPException(status_code=400, detail="没有需要重练的题目")

    results = []
    wrong_items: list[tuple[Exams, str]] = []
    for examid, user_answer in parsed.items():
        question = by_id[examid]
        score = _judge_one(question, user_answer)
        correct = _is_correct(question, score)
        results.append(
            {
                "examid": examid,
                "type": question.type,
                "score": score,
                "correct": correct,
                "answer": question.answer,
                "user_answer": user_answer,
            }
        )
        if not correct:
            wrong_items.append((question, user_answer))

    total_score = sum(item["score"] for item in results)
    correct_count = sum(1 for item in results if item["correct"])

    with Session(engine) as session:
        for item in results:
            row = session.exec(
                select(Scores).where(
                    Scores.userid == userid,
                    Scores.videoid == videoid,
                    Scores.examid == item["examid"],
                )
            ).first()
            if row is None:
                session.add(
                    Scores(
                        userid=userid,
                        videoid=videoid,
                        examid=item["examid"],
                        answer=item["user_answer"],
                        score=item["score"],
                        mode=mode,
                    )
                )
            else:  # redo 模式：覆盖旧成绩
                row.answer = item["user_answer"]
                row.score = item["score"]
                row.mode = mode
                session.add(row)
            # 最终答案同步落定为草稿，便于提交后回显
            draft = session.exec(
                select(Answers).where(
                    Answers.userid == userid, Answers.examid == item["examid"]
                )
            ).first()
            if draft is None:
                session.add(
                    Answers(userid=userid, examid=item["examid"], answer=item["user_answer"])
                )
            else:
                draft.answer = item["user_answer"]
                draft.updated_at = datetime.now(timezone.utc)
                session.add(draft)

        # 错题收录：答错（含多选漏选半分）即入错题本，错因清空待异步重出
        for question, user_answer in wrong_items:
            row = session.exec(
                select(WrongBook).where(
                    WrongBook.userid == userid, WrongBook.examid == question.examid
                )
            ).first()
            if row is None:
                session.add(
                    WrongBook(
                        userid=userid,
                        examid=question.examid,
                        videoid=videoid,
                        wrong_count=1,
                        mastered=False,
                        last_answer=user_answer,
                        correct_streak=0,
                    )
                )
            else:
                row.wrong_count += 1
                row.mastered = False
                row.correct_streak = 0
                row.last_answer = user_answer
                row.wrong_reason = None
                row.wrong_analysis = None
                row.updated_at = datetime.now(timezone.utc)
                session.add(row)
        if mode == "redo":
            # 重练答对：连续答对计数 +1，达 2 次自动标记已掌握
            correct_examids = {item["examid"] for item in results if item["correct"]}
            if correct_examids:
                for row in session.exec(
                    select(WrongBook).where(
                        WrongBook.userid == userid,
                        WrongBook.examid.in_(correct_examids),
                    )
                ).all():
                    row.correct_streak += 1
                    if row.correct_streak >= 2:
                        row.mastered = True
                    row.updated_at = datetime.now(timezone.utc)
                    session.add(row)

        session.add(
            PracticeSessions(
                userid=userid,
                videoid=videoid,
                mode=mode,
                total_questions=len(results),
                correct_count=correct_count,
                total_score=total_score,
                duration_seconds=duration_seconds,
            )
        )
        session.commit()

    # 错因标签与个性化解析异步生成（写入该生 WrongBook 行），不阻塞响应
    for question, user_answer in wrong_items:
        worker.submit(gen_wrong_reason, question, user_answer, userid)

    return {
        "message": "ok",
        "total": total_score,
        "duration_seconds": duration_seconds,
        "results": results,
        "wrong_examids": [question.examid for question, _ in wrong_items],
        "analysis_pending": bool(wrong_items),
    }


# ==================== 题库管理 ====================

VALID_TYPES = ("choice", "multi", "judge")
DEFAULT_SCORES = {"choice": 5, "multi": 10, "judge": 5}


def _auth_userid(token: str):
    """统一鉴权：失败返回 None，成功返回 userid。"""
    token_data = check_token(token)
    if token_data["message"] != "Token-OK":
        return None
    return int(token_data["userid"])


def _validate_bank_item(data: dict):
    """校验题库题目字段，返回 (错误信息 or None, 清洗后的字段 dict)。"""
    qtype = str(data.get("type", ""))
    if qtype not in VALID_TYPES:
        return "题型必须是 choice/multi/judge", None
    question = str(data.get("question", "")).strip()
    if not question:
        return "题干不能为空", None
    if len(question) > MAX_QUESTION_LEN:
        return f"题干不能超过 {MAX_QUESTION_LEN} 字", None
    answer = str(data.get("answer", "")).strip()
    if not answer:
        return "答案不能为空", None
    knowledge_point = str(data.get("knowledge_point", "")).strip() or "综合"

    options = None
    if qtype in ("choice", "multi"):
        raw_options = data.get("options")
        if not isinstance(raw_options, dict) or set(raw_options.keys()) != {"A", "B", "C", "D"}:
            return "选择题必须包含 A/B/C/D 四个选项", None
        for text in raw_options.values():
            if len(str(text)) > MAX_OPTION_LEN:
                return f"选项文本不能超过 {MAX_OPTION_LEN} 字", None
        if qtype == "choice":
            if answer.upper() not in ("A", "B", "C", "D") or len(answer) != 1:
                return "单选题答案必须是 A/B/C/D 之一", None
            answer = answer.upper()
        else:
            letters = set(answer.upper())
            if not (2 <= len(letters) <= 4) or not letters <= set("ABCD"):
                return "多选题答案必须是 A-D 中 2-4 个不重复选项", None
            answer = "".join(sorted(letters))
        options = json.dumps(raw_options, ensure_ascii=False)
    if qtype == "judge":
        answer = answer.upper()
        if answer not in ("T", "F"):
            return "判断题答案必须是 T（正确）或 F（错误）", None

    try:
        score = int(data.get("score") or DEFAULT_SCORES[qtype])
    except (TypeError, ValueError):
        return "分值必须是整数", None
    if score <= 0:
        return "分值必须大于 0", None

    try:
        difficulty = int(data.get("difficulty") or 2)
    except (TypeError, ValueError):
        return "难度必须是 1/2/3", None
    if difficulty not in (1, 2, 3):
        return "难度必须是 1（易）/2（中）/3（难）", None

    videoid = data.get("videoid")
    if videoid in ("", None):
        videoid = None
    else:
        try:
            videoid = int(videoid)
        except (TypeError, ValueError):
            return "videoid 必须是整数", None

    analysis = str(data.get("analysis", "")).strip() or None
    if analysis and len(analysis) > MAX_ANALYSIS_LEN:
        return f"解析不能超过 {MAX_ANALYSIS_LEN} 字", None
    return None, {
        "question": question,
        "type": qtype,
        "answer": answer,
        "score": score,
        "options": options,
        "knowledge_point": knowledge_point,
        "analysis": analysis,
        "difficulty": difficulty,
        "videoid": videoid,
    }


def _exam_to_dict(exam: Exams, videotitle: str | None) -> dict:
    return {
        "examid": exam.examid,
        "videoid": exam.videoid,
        "videotitle": videotitle,
        "question": exam.question,
        "type": exam.type,
        "score": exam.score,
        "options": json.loads(exam.options) if exam.options else None,
        "answer": exam.answer,
        "knowledge_point": exam.knowledge_point or "综合",
        "analysis": exam.analysis,
        "difficulty": exam.difficulty or 2,
    }


@app.get("/api/bank/list")
def bank_list(type: str = "", knowledge_point: str = "", keyword: str = ""):
    with Session(engine) as session:
        statement = select(Exams)
        if type:
            statement = statement.where(Exams.type == type)
        if knowledge_point:
            statement = statement.where(Exams.knowledge_point == knowledge_point)
        if keyword:
            statement = statement.where(Exams.question.contains(keyword))
        exams = session.exec(statement.order_by(desc(Exams.examid))).all()
        video_ids = {exam.videoid for exam in exams if exam.videoid is not None}
        titles = {
            video.videoid: video.videotitle
            for video in session.exec(select(Videos).where(Videos.videoid.in_(video_ids))).all()
        } if video_ids else {}
    items = [
        _exam_to_dict(exam, titles.get(exam.videoid) if exam.videoid else "公共题库")
        for exam in exams
    ]
    return {"items": items, "total": len(items)}


@app.get("/api/bank/knowledge-points")
def bank_knowledge_points():
    with Session(engine) as session:
        rows = session.exec(select(Exams.knowledge_point).distinct()).all()
    return {"items": sorted(point for point in rows if point)}


@app.get("/api/bank/difficulty-stats")
def bank_difficulty_stats():
    """实测难度回填：按首答记录（Scores.mode='exam'）聚合每题实测正确率。"""
    with Session(engine) as session:
        score_rows = session.exec(select(Scores).where(Scores.mode == "exam")).all()
        exam_map = {
            exam.examid: exam
            for exam in session.exec(
                select(Exams).where(Exams.examid.in_([r.examid for r in score_rows]))
            ).all()
        } if score_rows else {}
    stats: dict[int, dict] = {}
    for row in score_rows:
        exam = exam_map.get(row.examid)
        if exam is None:
            continue
        bucket = stats.setdefault(row.examid, {"attempts": 0, "correct_count": 0})
        bucket["attempts"] += 1
        if _is_correct(exam, row.score):
            bucket["correct_count"] += 1
    items = [
        {
            "examid": examid,
            "attempts": bucket["attempts"],
            "correct_count": bucket["correct_count"],
            # 样本量不足 5 次首答时不给出实测正确率
            "measured_accuracy": round(bucket["correct_count"] / bucket["attempts"], 4)
            if bucket["attempts"] >= 5
            else None,
        }
        for examid, bucket in sorted(stats.items())
    ]
    return {"items": items}


@app.post("/api/bank/add/{token}")
def bank_add(token: str, data: dict = Body()):
    if _auth_userid(token) is None:
        return "Token-Fail"
    error, fields = _validate_bank_item(data)
    if error:
        return {"message": "invalid-field", "detail": error}
    if fields["videoid"] is not None:
        with Session(engine) as session:
            if session.get(Videos, fields["videoid"]) is None:
                return {"message": "invalid-field", "detail": "所属视频不存在"}
    with Session(engine) as session:
        exam = Exams(**fields)
        session.add(exam)
        session.commit()
        session.refresh(exam)
    return {"message": "ok", "examid": exam.examid}


@app.post("/api/bank/update/{examid}/{token}")
def bank_update(examid: int, token: str, data: dict = Body()):
    if _auth_userid(token) is None:
        return "Token-Fail"
    error, fields = _validate_bank_item(data)
    if error:
        return {"message": "invalid-field", "detail": error}
    with Session(engine) as session:
        exam = session.get(Exams, examid)
        if exam is None:
            raise HTTPException(status_code=404, detail="题目不存在")
        if fields["videoid"] is not None and session.get(Videos, fields["videoid"]) is None:
            return {"message": "invalid-field", "detail": "所属视频不存在"}
        for name, value in fields.items():
            setattr(exam, name, value)
        session.add(exam)
        session.commit()
    return {"message": "ok"}


@app.post("/api/bank/delete/{examid}/{token}")
def bank_delete(examid: int, token: str):
    if _auth_userid(token) is None:
        return "Token-Fail"
    with Session(engine) as session:
        exam = session.get(Exams, examid)
        if exam is None:
            raise HTTPException(status_code=404, detail="题目不存在")
        # 级联清理关联行，防外键残留
        for model in (Answers, Scores, WrongBook):
            for row in session.exec(select(model).where(model.examid == examid)).all():
                session.delete(row)
        session.delete(exam)
        session.commit()
    return {"message": "ok"}


# ==================== 在线答题：即时判分与标记 ====================


@app.post("/api/practice/judge/{token}")
def practice_judge(token: str, data: dict = Body()):
    if _auth_userid(token) is None:
        return "Token-Fail"
    examid = int(data.get("examid", 0))
    answer = str(data.get("answer", ""))
    with Session(engine) as session:
        exam = session.get(Exams, examid)
    if exam is None:
        raise HTTPException(status_code=404, detail="题目不存在")
    score = _judge_one(exam, answer)
    return {
        "score": score,
        "correct": _is_correct(exam, score),
        "answer": exam.answer,
        "analysis": exam.analysis or "",
        "difficulty": exam.difficulty or 2,
    }


@app.post("/exam/mark/{videoid}/{token}")
def exam_mark(videoid: int, token: str, data: dict = Body()):
    userid = _auth_userid(token)
    if userid is None:
        return "Token-Fail"
    examid = int(data.get("examid", 0))
    marked = bool(data.get("marked", False))
    with Session(engine) as session:
        exam = session.get(Exams, examid)
        if exam is None or exam.videoid != videoid:
            raise HTTPException(status_code=404, detail="题目不存在")
        row = session.exec(
            select(Answers).where(
                Answers.userid == userid, Answers.examid == examid
            )
        ).first()
        if row is None:
            session.add(Answers(userid=userid, examid=examid, answer="", marked=marked))
        else:
            row.marked = marked
            row.updated_at = datetime.now(timezone.utc)
            session.add(row)
        session.commit()
    return {"message": "ok"}


# ==================== 错题本 ====================


def _wrongbook_item(row: WrongBook, exam: Exams, videotitle: str | None) -> dict:
    item = _exam_to_dict(exam, videotitle)
    item.update(
        {
            "wrongid": row.wrongid,
            "wrong_count": row.wrong_count,
            "mastered": row.mastered,
            "last_answer": row.last_answer,
            "standard_answer": exam.answer,
            "wrong_reason": row.wrong_reason,
            "wrong_analysis": row.wrong_analysis,
            "correct_streak": row.correct_streak,
        }
    )
    return item


@app.get("/api/wrongbook/list/{token}")
def wrongbook_list(token: str, mastered: str = "", reason: str = ""):
    userid = _auth_userid(token)
    if userid is None:
        return "Token-Fail"
    with Session(engine) as session:
        statement = select(WrongBook).where(WrongBook.userid == userid)
        if mastered in ("true", "false"):
            statement = statement.where(WrongBook.mastered == (mastered == "true"))
        if reason == "pending":
            statement = statement.where(WrongBook.wrong_reason.is_(None))  # type: ignore[union-attr]
        elif reason:
            statement = statement.where(WrongBook.wrong_reason == reason)
        rows = session.exec(
            statement.order_by(WrongBook.mastered, desc(WrongBook.updated_at))
        ).all()
        exam_map = {
            exam.examid: exam
            for exam in session.exec(
                select(Exams).where(Exams.examid.in_([r.examid for r in rows]))
            ).all()
        } if rows else {}
        video_ids = {row.videoid for row in rows if row.videoid is not None}
        titles = {
            video.videoid: video.videotitle
            for video in session.exec(select(Videos).where(Videos.videoid.in_(video_ids))).all()
        } if video_ids else {}
    items = [
        _wrongbook_item(row, exam_map[row.examid], titles.get(row.videoid))
        for row in rows
        if row.examid in exam_map
    ]
    return {
        "items": items,
        "pending_review": sum(1 for item in items if not item["mastered"]),
        "mastered_count": sum(1 for item in items if item["mastered"]),
    }


@app.post("/api/wrongbook/master/{wrongid}/{token}")
def wrongbook_master(wrongid: int, token: str, data: dict = Body()):
    userid = _auth_userid(token)
    if userid is None:
        return "Token-Fail"
    with Session(engine) as session:
        row = session.get(WrongBook, wrongid)
        if row is None or row.userid != userid:
            raise HTTPException(status_code=404, detail="错题记录不存在")
        row.mastered = bool(data.get("mastered", False))
        row.updated_at = datetime.now(timezone.utc)
        session.add(row)
        session.commit()
    return {"message": "ok"}


@app.get("/api/wrongbook/analysis/{examid}/{token}")
def wrongbook_analysis(examid: int, token: str):
    userid = _auth_userid(token)
    if userid is None:
        return "Token-Fail"
    with Session(engine) as session:
        exam = session.get(Exams, examid)
        if exam is None:
            raise HTTPException(status_code=404, detail="题目不存在")
        row = session.exec(
            select(WrongBook).where(
                WrongBook.userid == userid, WrongBook.examid == examid
            )
        ).first()
    return {
        "wrong_reason": row.wrong_reason if row else None,
        "wrong_analysis": row.wrong_analysis if row else None,
        "analysis": exam.analysis,
    }


# ==================== 学习数据分析 ====================


@app.get("/api/stats/overview/{token}")
def stats_overview(token: str):
    userid = _auth_userid(token)
    if userid is None:
        return "Token-Fail"
    with Session(engine) as session:
        score_rows = session.exec(select(Scores).where(Scores.userid == userid)).all()
        exam_map = {
            exam.examid: exam
            for exam in session.exec(
                select(Exams).where(Exams.examid.in_([r.examid for r in score_rows]))
            ).all()
        } if score_rows else {}
        sessions = session.exec(
            select(PracticeSessions)
            .where(PracticeSessions.userid == userid)
            .order_by(desc(PracticeSessions.sessionid))
            .limit(10)
        ).all()
        total_duration = sum(
            s.duration_seconds
            for s in session.exec(
                select(PracticeSessions).where(PracticeSessions.userid == userid)
            ).all()
        )
        pending_rows = session.exec(
            select(WrongBook).where(
                WrongBook.userid == userid, WrongBook.mastered == False  # noqa: E712
            )
        ).all()
        pending_wrong = len(pending_rows)
        reason_counts: dict[str, int] = {}
        for row in pending_rows:
            key = row.wrong_reason or "分析中"
            reason_counts[key] = reason_counts.get(key, 0) + 1
        video_ids = {s.videoid for s in sessions if s.videoid is not None}
        titles = {
            video.videoid: video.videotitle
            for video in session.exec(select(Videos).where(Videos.videoid.in_(video_ids))).all()
        } if video_ids else {}

    def bucket(rows, key_of):
        groups: dict[str, list[bool]] = {}
        for row in rows:
            exam = exam_map.get(row.examid)
            if exam is None:
                continue
            groups.setdefault(key_of(exam), []).append(_is_correct(exam, row.score))
        return [
            {
                "name": name,
                "total": len(flags),
                "correct": sum(flags),
                "accuracy": round(sum(flags) / len(flags), 4) if flags else 0,
            }
            for name, flags in sorted(groups.items())
        ]

    total = len(score_rows)
    correct_total = sum(
        1
        for row in score_rows
        if row.examid in exam_map and _is_correct(exam_map[row.examid], row.score)
    )
    # 正确率口径分离：首答（mode='exam'）与含重做（全部记录）
    first_rows = [row for row in score_rows if row.mode == "exam"]
    first_correct = sum(
        1
        for row in first_rows
        if row.examid in exam_map and _is_correct(exam_map[row.examid], row.score)
    )
    return {
        "summary": {
            "total_answers": total,
            "first_answers": len(first_rows),
            "first_accuracy": round(first_correct / len(first_rows), 4) if first_rows else 0,
            "overall_accuracy": round(correct_total / total, 4) if total else 0,
            "total_duration_seconds": total_duration,
            "pending_wrong": pending_wrong,
        },
        "by_wrong_reason": [
            {"reason": reason, "count": count}
            for reason, count in sorted(reason_counts.items())
        ],
        "by_knowledge_point": [
            {"knowledge_point": b["name"], "total": b["total"],
             "correct": b["correct"], "accuracy": b["accuracy"]}
            for b in bucket(score_rows, lambda e: e.knowledge_point or "综合")
        ],
        "by_type": [
            {"type": b["name"], "total": b["total"],
             "correct": b["correct"], "accuracy": b["accuracy"]}
            for b in bucket(score_rows, lambda e: e.type)
        ],
        "recent_sessions": [
            {
                "sessionid": s.sessionid,
                "videoid": s.videoid,
                "videotitle": titles.get(s.videoid),
                "mode": s.mode,
                "total_questions": s.total_questions,
                "correct_count": s.correct_count,
                "total_score": s.total_score,
                "duration_seconds": s.duration_seconds,
                "createtime": s.createtime.isoformat(),
            }
            for s in sessions
        ],
    }


# ==================== 页面路由 ====================


@app.get("/bank")
def bank_page(request: Request):
    return templates.TemplateResponse(request=request, name="bank.html")


@app.get("/wrongbook")
def wrongbook_page(request: Request):
    return templates.TemplateResponse(request=request, name="wrongbook.html")


@app.get("/stats")
def stats_page(request: Request):
    return templates.TemplateResponse(request=request, name="stats.html")


@app.get("/settings")
def settings_page(request: Request):
    return templates.TemplateResponse(request=request, name="settings.html")


@app.get("/api/settings/ai/{token}")
def get_ai_settings(token: str):
    if _auth_userid(token) is None:
        return "Token-Fail"
    config = get_ai_config()
    return {
        "text_provider": config.text_provider,
        "text_base_url": config.text_base_url,
        "text_api_key": mask_api_key(config.text_api_key),
        "text_model": config.text_model,
        "asr_provider": config.asr_provider,
        "asr_base_url": config.asr_base_url,
        "asr_api_key": mask_api_key(config.asr_api_key),
        "asr_model": config.asr_model,
        "asr_transport": config.asr_transport,
    }


@app.post("/api/settings/ai/{token}")
def save_ai_settings(token: str, data: dict = Body()):
    if _auth_userid(token) is None:
        return "Token-Fail"
    fields: dict[str, str] = {}
    for name in ("text_base_url", "text_model", "asr_base_url", "asr_model"):
        value = str(data.get(name, "")).strip()
        if not value:
            return {"message": "invalid-field", "detail": f"{name} 不能为空"}
        fields[name] = value
    fields["text_provider"] = str(data.get("text_provider", "")).strip() or "custom"
    fields["asr_provider"] = str(data.get("asr_provider", "")).strip() or "custom"
    fields["asr_transport"] = (
        str(data.get("asr_transport", "")).strip().lower() or "base64"
    )
    if fields["asr_transport"] not in ("base64", "oss"):
        return {"message": "invalid-field", "detail": "ASR 传输方式只支持 base64 或 oss"}
    # Key 留空或原样传回脱敏值 = 不修改已保存的 Key
    current = get_ai_config()
    for name, old in (
        ("text_api_key", current.text_api_key),
        ("asr_api_key", current.asr_api_key),
    ):
        value = str(data.get(name, "")).strip()
        fields[name] = old if not value or value == mask_api_key(old) else value
    save_ai_config(**fields)
    return {"message": "ok"}


@app.post("/api/settings/ai/test/{token}")
def test_ai_settings(token: str, data: dict = Body()):
    """用提交的值发一条最小文本请求验证连通性（仅测文本模型）。"""
    if _auth_userid(token) is None:
        return "Token-Fail"
    base_url = str(data.get("base_url", "")).strip()
    model = str(data.get("model", "")).strip()
    api_key = str(data.get("api_key", "")).strip()
    if not base_url or not model:
        return {"message": "invalid-field", "detail": "base_url 与 model 不能为空"}
    if not api_key:
        api_key = get_ai_config().text_api_key
    error = test_text_connection(base_url, api_key, model)
    if error:
        return {"message": "fail", "detail": error}
    return {"message": "ok"}


if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=False)
