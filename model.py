from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import UniqueConstraint, event
from sqlmodel import Field, SQLModel, create_engine

from config import settings


class Users(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("username", name="uq_users_username"),)

    userid: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(index=True)
    password: str
    realname: str
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
    type: str  # choice=单选 / multi=多选 / judge=判断
    answer: str
    score: int
    options: Optional[str] = None
    knowledge_point: Optional[str] = Field(default=None, index=True)
    analysis: Optional[str] = None
    alt_answers: Optional[str] = None  # 已停用（历史填空题遗留列）
    difficulty: Optional[int] = Field(default=None, index=True)  # 1 易 / 2 中 / 3 难
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
    mode: str = Field(default="exam")  # exam=首答 / redo=重练覆盖
    createtime: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Answers(SQLModel, table=True):
    """做题过程的实时草稿：每次答案变更都会 upsert 到这里。"""

    __table_args__ = (
        UniqueConstraint("userid", "examid", name="uq_answer_draft"),
    )

    answerid: Optional[int] = Field(default=None, primary_key=True)
    userid: Optional[int] = Field(default=None, foreign_key="users.userid", index=True)
    examid: Optional[int] = Field(default=None, foreign_key="exams.examid", index=True)
    answer: str
    marked: bool = Field(default=False)  # 题目标记（疑难题复查）
    createtime: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class WrongBook(SQLModel, table=True):
    """错题本：交卷后自动收录答错的题目。"""

    __table_args__ = (
        UniqueConstraint("userid", "examid", name="uq_wrong_item"),
    )

    wrongid: Optional[int] = Field(default=None, primary_key=True)
    userid: Optional[int] = Field(default=None, foreign_key="users.userid", index=True)
    examid: Optional[int] = Field(default=None, foreign_key="exams.examid", index=True)
    videoid: Optional[int] = Field(default=None, index=True)
    wrong_count: int = Field(default=1)
    mastered: bool = Field(default=False)
    last_answer: str = ""
    wrong_reason: Optional[str] = None  # AI 错因标签：概念混淆/知识盲区/粗心失误/方法错误
    wrong_analysis: Optional[str] = None  # 针对本次错误答案的个性化解析
    correct_streak: int = Field(default=0)  # 重练连续答对次数，达 2 自动标记掌握
    createtime: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AIConfig(SQLModel, table=True):
    """全局 AI 模型配置（单行，id 固定为 1），由设置页维护。"""

    __tablename__ = "aiconfig"

    id: Optional[int] = Field(default=None, primary_key=True)
    # 文本模型（笔记/导图/出题/错因），走 OpenAI 兼容接口
    text_provider: str = Field(default="dashscope")
    text_base_url: str = ""
    text_api_key: str = ""
    text_model: str = ""
    # 语音转写模型（ASR）
    asr_provider: str = Field(default="dashscope")
    asr_base_url: str = ""
    asr_api_key: str = ""
    asr_model: str = ""
    asr_transport: str = Field(default="base64")  # dashscope 专属：base64 / oss
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class PracticeSessions(SQLModel, table=True):
    """练习/考试会话：每次交卷产生一条，用于成绩与用时统计。"""

    sessionid: Optional[int] = Field(default=None, primary_key=True)
    userid: Optional[int] = Field(default=None, foreign_key="users.userid", index=True)
    videoid: Optional[int] = Field(default=None, index=True)
    mode: str = Field(default="exam")  # exam=正常练习 / redo=错题重练
    total_questions: int
    correct_count: int  # 客观题答对数
    total_score: int
    duration_seconds: int = Field(default=0)
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


def _table_columns(table: str) -> set[str]:
    with engine.connect() as connection:
        rows = connection.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


def _migrate() -> None:
    """对老库做平滑迁移：缺列则 ADD COLUMN，幂等可重复执行。"""
    with engine.begin() as connection:
        exam_columns = _table_columns("exams")
        if "knowledge_point" not in exam_columns:
            connection.exec_driver_sql("ALTER TABLE exams ADD COLUMN knowledge_point TEXT")
        if "analysis" not in exam_columns:
            connection.exec_driver_sql("ALTER TABLE exams ADD COLUMN analysis TEXT")
        if "alt_answers" not in exam_columns:
            connection.exec_driver_sql("ALTER TABLE exams ADD COLUMN alt_answers TEXT")
        if "difficulty" not in exam_columns:
            connection.exec_driver_sql("ALTER TABLE exams ADD COLUMN difficulty INTEGER")

        answer_columns = _table_columns("answers")
        if "marked" not in answer_columns:
            connection.exec_driver_sql(
                "ALTER TABLE answers ADD COLUMN marked BOOLEAN DEFAULT 0"
            )

        score_columns = _table_columns("scores")
        if "mode" not in score_columns:
            connection.exec_driver_sql(
                "ALTER TABLE scores ADD COLUMN mode TEXT DEFAULT 'exam'"
            )

        wrong_columns = _table_columns("wrongbook")
        if "wrong_reason" not in wrong_columns:
            connection.exec_driver_sql("ALTER TABLE wrongbook ADD COLUMN wrong_reason TEXT")
        if "wrong_analysis" not in wrong_columns:
            connection.exec_driver_sql("ALTER TABLE wrongbook ADD COLUMN wrong_analysis TEXT")
        if "correct_streak" not in wrong_columns:
            connection.exec_driver_sql(
                "ALTER TABLE wrongbook ADD COLUMN correct_streak INTEGER DEFAULT 0"
            )

        # 回填老题目的知识点：优先取所属视频关键词，兜底为「综合」
        connection.exec_driver_sql(
            "UPDATE exams SET knowledge_point = "
            "(SELECT keyword FROM videos WHERE videos.videoid = exams.videoid) "
            "WHERE knowledge_point IS NULL"
        )
        connection.exec_driver_sql(
            "UPDATE exams SET knowledge_point = '综合' WHERE knowledge_point IS NULL"
        )
        # 存量题难度回填为「中」
        connection.exec_driver_sql(
            "UPDATE exams SET difficulty = 2 WHERE difficulty IS NULL"
        )

        # 纯客观题改造：清除历史填空/简答题及其关联作答记录
        legacy_rows = connection.exec_driver_sql(
            "SELECT examid FROM exams WHERE type IN ('blank', 'qa')"
        ).fetchall()
        legacy_ids = [row[0] for row in legacy_rows]
        if legacy_ids:
            placeholders = ", ".join("?" for _ in legacy_ids)
            params = tuple(legacy_ids)
            for table in ("answers", "scores", "wrongbook"):
                connection.exec_driver_sql(
                    f"DELETE FROM {table} WHERE examid IN ({placeholders})", params
                )
            connection.exec_driver_sql(
                f"DELETE FROM exams WHERE examid IN ({placeholders})", params
            )


def init_database() -> None:
    SQLModel.metadata.create_all(engine)
    _migrate()
