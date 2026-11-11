from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import UniqueConstraint, event
from sqlmodel import Field, SQLModel, Session, create_engine, select

from config import settings


class Users(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("username", name="uq_users_username"),)

    userid: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(index=True)
    password: str
    realname: str
    role: str
    createtime: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Videos(SQLModel, table=True):
    videoid: Optional[int] = Field(default=None, primary_key=True)
    videotitle: str
    filename: str
    duration: Optional[float] = None
    content: Optional[str] = None
    summary: Optional[str] = None
    keyword: Optional[str] = None
    reference: Optional[str] = None
    xmindjson: Optional[str] = None
    examjson: Optional[str] = None
    status: str = Field(default="queued", index=True)
    stage: str = Field(default="uploaded")
    progress: int = Field(default=0)
    error_message: Optional[str] = None
    warning_message: Optional[str] = None
    createtime: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Exams(SQLModel, table=True):
    examid: Optional[int] = Field(default=None, primary_key=True)
    videoid: Optional[int] = Field(default=None, foreign_key="videos.videoid", index=True)
    question: str
    type: str
    answer: str
    score: int
    options: Optional[str] = None
    createtime: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Scores(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint("userid", "videoid", "examid", name="uq_score_submission"),
    )

    scoreid: Optional[int] = Field(default=None, primary_key=True)
    userid: Optional[int] = Field(default=None, foreign_key="users.userid", index=True)
    videoid: Optional[int] = Field(default=None, foreign_key="videos.videoid", index=True)
    examid: Optional[int] = Field(default=None, foreign_key="exams.examid", index=True)
    answer: str
    score: int
    createtime: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


DEMO_USERS = (
    ("admin", "admin123", "管理员", "admin"),
    ("teacher", "teacher123", "演示教师", "teacher"),
    ("student", "student123", "演示学生", "student"),
)


def init_database() -> None:
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        for username, password, realname, role in DEMO_USERS:
            exists = session.exec(select(Users).where(Users.username == username)).first()
            if exists is None:
                session.add(
                    Users(
                        username=username,
                        password=password,
                        realname=realname,
                        role=role,
                    )
                )
        session.commit()
