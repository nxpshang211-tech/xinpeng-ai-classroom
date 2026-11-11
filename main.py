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
from model import Answers, Exams, Scores, Users, Videos, engine, init_database
from module import (
    aes_encrypt,
    ai_score,
    check_token,
    process_video,
    retry_incomplete_videos,
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
    return {"message": "login-ok", "token": _make_token(user), "realname": user.realname}


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
    realname = str(data.get("realname", "")).strip()
    if not USERNAME_PATTERN.fullmatch(username):
        return {"message": "invalid-username"}
    if len(password) < 6:
        return {"message": "invalid-password"}
    if not realname:
        return {"message": "invalid-realname"}
    with Session(engine) as session:
        if session.exec(select(Users).where(Users.username == username)).first():
            return {"message": "user-exists"}
        user = Users(username=username, password=password, realname=realname)
        session.add(user)
        session.commit()
        session.refresh(user)
    return {
        "message": "register-ok",
        "token": _make_token(user),
        "realname": user.realname,
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
                "options": json.loads(item.options) if item.options else None,
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
        "submitted": bool(score_rows),
        "scores": scores,
        "total": sum(scores.values()) if score_rows else None,
    }


@app.post("/exam/submit/{videoid}/{token}")
def exam_submit(videoid: int, token: str, answers: dict = Body()):
    token_data = check_token(token)
    if token_data["message"] != "Token-OK":
        return "Token-Fail"
    userid = int(token_data["userid"])

    with Session(engine) as session:
        already_submitted = session.exec(
            select(Scores).where(
                Scores.userid == userid,
                Scores.videoid == videoid,
            )
        ).first()
        if already_submitted:
            return "Already-Exam"
        exams = session.exec(select(Exams).where(Exams.videoid == videoid)).all()

    by_id = {exam.examid: exam for exam in exams}
    pending_scores: list[Scores] = []

    for encoded in answers.get("choices", []):
        try:
            _, examid_text, answer = encoded.split("-", 2)
            examid = int(examid_text)
        except (ValueError, AttributeError):
            raise HTTPException(status_code=400, detail="选择题答案格式错误")
        question = by_id.get(examid)
        if question is None or question.type != "choice":
            raise HTTPException(status_code=400, detail=f"选择题不存在：{examid}")
        pending_scores.append(
            Scores(
                userid=userid,
                videoid=videoid,
                examid=examid,
                answer=answer,
                score=5 if question.answer == answer else 0,
            )
        )

    for key, answer in answers.items():
        if key == "choices":
            continue
        try:
            prefix, examid_text = key.split("-", 1)
            examid = int(examid_text)
        except (ValueError, AttributeError):
            raise HTTPException(status_code=400, detail="简答题答案格式错误")
        question = by_id.get(examid)
        if prefix != "qa" or question is None or question.type != "qa":
            raise HTTPException(status_code=400, detail=f"简答题不存在：{examid}")
        score = ai_score(str(answer), question.question, question.answer)
        pending_scores.append(
            Scores(
                userid=userid,
                videoid=videoid,
                examid=examid,
                answer=str(answer),
                score=score,
            )
        )

    expected_ids = {exam.examid for exam in exams}
    submitted_ids = {score.examid for score in pending_scores}
    if expected_ids != submitted_ids:
        raise HTTPException(status_code=400, detail="请完成全部题目后再提交")

    total_score = sum(item.score for item in pending_scores)
    with Session(engine) as session:
        session.add_all(pending_scores)
        # 最终答案同步落定为草稿，便于提交后回显
        for item in pending_scores:
            row = session.exec(
                select(Answers).where(
                    Answers.userid == userid, Answers.examid == item.examid
                )
            ).first()
            if row is None:
                session.add(
                    Answers(userid=userid, examid=item.examid, answer=item.answer)
                )
            else:
                row.answer = item.answer
                row.updated_at = datetime.now(timezone.utc)
                session.add(row)
        session.commit()
    return total_score


if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=False)
