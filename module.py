from __future__ import annotations

import base64
import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np
import requests
import xmind
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
from dashscope import MultiModalConversation
from sqlmodel import Session, select

from moviepy.editor import AudioFileClip, VideoFileClip

from config import settings
from model import AIConfig, Exams, Videos, WrongBook, engine


class PipelineError(RuntimeError):
    pass


def aes_encrypt(source: str) -> str:
    cipher = AES.new(settings.token_aes_key, AES.MODE_CBC, settings.token_aes_iv)
    return cipher.encrypt(pad(source.encode("utf-8"), AES.block_size)).hex()


def aes_decrypt(encrypted: str) -> str:
    decipher = AES.new(settings.token_aes_key, AES.MODE_CBC, settings.token_aes_iv)
    decrypted = decipher.decrypt(bytes.fromhex(encrypted))
    return unpad(decrypted, AES.block_size).decode("utf-8")


def check_token(token: str) -> dict[str, str]:
    try:
        username, userid, realname, expired_time = aes_decrypt(token).split("|")
        if int(time.time()) > int(expired_time):
            return {"message": "Token-Expired"}
        return {
            "message": "Token-OK",
            "username": username,
            "userid": userid,
            "realname": realname,
        }
    except (ValueError, TypeError, IndexError, UnicodeError):
        return {"message": "Token-Error"}


_ai_config_cache: AIConfig | None = None


def get_ai_config() -> AIConfig:
    """读取全局 AI 配置（单行 id=1）；首次访问用 .env 值播种，带进程内缓存。"""
    global _ai_config_cache
    if _ai_config_cache is not None:
        return _ai_config_cache
    with Session(engine) as session:
        config = session.get(AIConfig, 1)
        if config is None:
            config = AIConfig(
                id=1,
                text_base_url=settings.dashscope_compatible_base_url,
                text_api_key=settings.dashscope_api_key,
                text_model=settings.dashscope_text_model,
                asr_base_url=settings.dashscope_compatible_base_url,
                asr_api_key=settings.dashscope_api_key,
                asr_model=settings.dashscope_asr_model,
                asr_transport=settings.dashscope_asr_transport,
            )
            session.add(config)
            session.commit()
            session.refresh(config)
        # commit 后属性会过期，refresh 加载后脱离 session，供长缓存使用
        session.expunge(config)
        _ai_config_cache = config
    return config


def invalidate_ai_config_cache() -> None:
    global _ai_config_cache
    _ai_config_cache = None


def save_ai_config(**fields: str) -> AIConfig:
    """设置页保存入口：更新单行配置并清缓存。"""
    with Session(engine) as session:
        config = session.get(AIConfig, 1)
        if config is None:
            config = AIConfig(id=1)
            session.add(config)
        for key, value in fields.items():
            setattr(config, key, value)
        config.updated_at = datetime.now(timezone.utc)
        session.commit()
    invalidate_ai_config_cache()
    return get_ai_config()


def mask_api_key(key: str) -> str:
    """设置页回显用脱敏：保留前 3 位与后 4 位。"""
    if not key:
        return ""
    if len(key) <= 7:
        return "****"
    return f"{key[:3]}****{key[-4:]}"


def _require_asr_key() -> str:
    config = get_ai_config()
    if not config.asr_api_key:
        raise PipelineError("未配置语音转写（ASR）API Key，请先在「设置」页填写")
    return config.asr_api_key


def test_text_connection(base_url: str, api_key: str, model: str) -> str | None:
    """连通性测试：发一条最小 chat 请求，成功返回 None，失败返回原因。"""
    try:
        response = requests.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": "ping"}],
                "stream": False,
            },
            timeout=(10, 30),
        )
    except requests.RequestException as exc:
        return f"连接失败：{exc}"
    if response.status_code != 200:
        try:
            error = response.json()
        except ValueError:
            error = response.text
        return f"HTTP {response.status_code}：{error}"
    return None


def _response_error(response: Any) -> str:
    code = getattr(response, "code", "") or getattr(response, "status_code", "")
    message = getattr(response, "message", "")
    return f"DashScope 调用失败（{code}）：{message}".rstrip("：")


def _extract_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        for item in value:
            text = _extract_text(item)
            if text:
                return text
        return ""
    if isinstance(value, dict):
        for key in ("text", "transcript", "content", "message", "choices", "output"):
            if key in value:
                text = _extract_text(value[key])
                if text:
                    return text
        return ""
    if hasattr(value, "to_dict"):
        return _extract_text(value.to_dict())
    for attr in ("text", "content", "message", "choices", "output"):
        if hasattr(value, attr):
            text = _extract_text(getattr(value, attr))
            if text:
                return text
    return ""


def _retry(operation: Callable[[], Any], attempts: int = 3) -> Any:
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return operation()
        except PipelineError:
            raise
        except Exception as exc:  # SDK/network exceptions vary by version.
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(2**attempt)
    raise PipelineError(str(last_error) or "外部服务调用失败")


def _call_text_model(system_prompt: str, user_prompt: str) -> str:
    config = get_ai_config()
    if not config.text_api_key:
        raise PipelineError("未配置文本模型 API Key，请先在「设置」页填写")

    def call():
        response = requests.post(
            f"{config.text_base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {config.text_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": config.text_model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "stream": False,
            },
            timeout=(20, 600),
        )
        if response.status_code != 200:
            try:
                error = response.json()
            except ValueError:
                error = response.text
            raise PipelineError(
                f"文本模型调用失败 HTTP {response.status_code}：{error}"
            )
        text = _extract_text(response.json())
        if not text:
            raise PipelineError("文本模型返回了空文本")
        return text

    return _retry(call)


def _strip_code_fence(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        first_newline = cleaned.find("\n")
        cleaned = cleaned[first_newline + 1 :] if first_newline >= 0 else cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    return cleaned.strip()


def _generate_json(
    system_prompt: str,
    user_prompt: str,
    validator: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    raw = _call_text_model(system_prompt, user_prompt)
    try:
        data = json.loads(_strip_code_fence(raw))
        validator(data)
        return data
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as first_error:
        repair_prompt = (
            "下面的模型输出不是符合要求的 JSON。请修正结构，只返回合法 JSON，"
            "不要输出 Markdown 标记或解释。\n\n"
            f"原要求：\n{user_prompt}\n\n原输出：\n{raw}\n\n错误：{first_error}"
        )
        repaired = _call_text_model(system_prompt, repair_prompt)
        try:
            data = json.loads(_strip_code_fence(repaired))
            validator(data)
            return data
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise PipelineError(f"模型 JSON 经修复后仍不可用：{exc}") from exc


def video_audio(filename: str) -> dict[str, Any]:
    video_path = settings.static_dir / "videos" / filename
    audio_path = video_path.with_suffix(".mp3")
    clip = VideoFileClip(str(video_path))
    try:
        if clip.audio is None:
            raise PipelineError("视频不包含可识别的音轨")
        clip.audio.write_audiofile(
            str(audio_path),
            fps=16000,
            codec="libmp3lame",
            bitrate="48k",
            ffmpeg_params=["-ac", "1"],
            logger=None,
        )
        return {"duration": float(clip.duration)}
    finally:
        clip.close()


def _transcribe_audio_file(audio_path: Path, api_key: str) -> str:
    if audio_path.stat().st_size > settings.dashscope_asr_max_mb * 1024 * 1024:
        raise PipelineError(
            f"音频分片仍超过 {settings.dashscope_asr_max_mb} MB：{audio_path.name}"
        )

    config = get_ai_config()
    provider = config.asr_provider or "dashscope"
    if provider == "dashscope":
        transport = config.asr_transport.lower()
    elif provider == "custom":
        # 自定义：按 OpenAI 兼容音频输入协议调用
        transport = "base64"
    else:
        label = {"volcengine": "火山引擎", "tencent": "腾讯云"}.get(provider, provider)
        raise PipelineError(
            f"语音转写（ASR）供应商「{label}」尚未适配：该厂商使用异步任务协议，"
            "请在「设置」页改用阿里云百炼或自定义 OpenAI 兼容服务"
        )

    def call():
        if transport == "oss":
            response = MultiModalConversation.call(
                api_key=api_key,
                model=config.asr_model,
                messages=[
                    {
                        "role": "user",
                        "content": [{"audio": audio_path.resolve().as_uri()}],
                    }
                ],
                result_format="message",
            )
            if getattr(response, "status_code", 200) != 200:
                raise PipelineError(_response_error(response))
            text = _extract_text(getattr(response, "output", response))
        elif transport == "base64":
            encoded = base64.b64encode(audio_path.read_bytes()).decode("ascii")
            data_uri = f"data:audio/mpeg;base64,{encoded}"
            payload: dict[str, Any] = {
                "model": config.asr_model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_audio",
                                "input_audio": {
                                    "data": data_uri,
                                },
                            }
                        ],
                    }
                ],
                "stream": False,
            }
            if config.asr_provider == "dashscope":
                payload["asr_options"] = {"enable_itn": True}
            response = requests.post(
                f"{config.asr_base_url.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=(20, 600),
            )
            if response.status_code != 200:
                try:
                    error = response.json()
                except ValueError:
                    error = response.text
                raise PipelineError(
                    f"ASR HTTP {response.status_code}：{error}"
                )
            text = _extract_text(response.json())
        else:
            raise PipelineError("ASR 传输方式只支持 base64 或 oss")
        if not text:
            raise PipelineError(f"ASR 返回了空转写结果：{audio_path.name}")
        return text

    return _retry(call, attempts=settings.dashscope_asr_retries)


def _audio_subclip(clip: AudioFileClip, start: float, end: float):
    if hasattr(clip, "subclipped"):
        return clip.subclipped(start, end)
    return clip.subclip(start, end)


def _audio_duration_seconds(audio_path: Path) -> float:
    """Read the real duration because ASR limits apply to both size and time."""
    clip = AudioFileClip(str(audio_path))
    try:
        return float(clip.duration)
    finally:
        clip.close()


def _split_audio_for_asr(audio_path: Path) -> tuple[Path, list[Path]]:
    chunk_dir = settings.data_dir / "asr_chunks" / audio_path.stem
    split_marker = chunk_dir / ".split_complete"
    if chunk_dir.exists():
        existing = sorted(chunk_dir.glob("chunk_*.mp3"))
        max_bytes = settings.dashscope_asr_max_mb * 1024 * 1024
        if (
            split_marker.exists()
            and existing
            and all(0 < path.stat().st_size <= max_bytes for path in existing)
        ):
            return chunk_dir, existing
        shutil.rmtree(chunk_dir)
    chunk_dir.mkdir(parents=True, exist_ok=True)

    clip = AudioFileClip(str(audio_path))
    chunks: list[Path] = []
    try:
        chunk_seconds = settings.dashscope_asr_chunk_seconds
        if chunk_seconds <= 0:
            raise PipelineError("DASHSCOPE_ASR_CHUNK_SECONDS 必须大于 0")
        total = max(1, int(np.ceil(float(clip.duration) / chunk_seconds)))
        for index in range(total):
            start = index * chunk_seconds
            end = min(float(clip.duration), (index + 1) * chunk_seconds)
            output = chunk_dir / f"chunk_{index + 1:03d}.mp3"
            segment = _audio_subclip(clip, start, end)
            # MoviePy 1.x subclips share the parent reader. Closing each segment
            # also closes the parent and breaks the next chunk, so close once below.
            segment.write_audiofile(
                str(output),
                fps=16000,
                codec="libmp3lame",
                bitrate="48k",
                ffmpeg_params=["-ac", "1"],
                logger=None,
            )
            if output.stat().st_size > settings.dashscope_asr_max_mb * 1024 * 1024:
                raise PipelineError(
                    f"第 {index + 1} 段仍超过大小限制，请调小 "
                    "DASHSCOPE_ASR_CHUNK_SECONDS"
                )
            chunks.append(output)
        split_marker.write_text(str(len(chunks)), encoding="ascii")
    except Exception:
        shutil.rmtree(chunk_dir, ignore_errors=True)
        raise
    finally:
        clip.close()
    return chunk_dir, chunks


def audio_text(
    filename: str,
    progress_callback: Callable[[int, int], None] | None = None,
) -> str:
    api_key = _require_asr_key()
    audio_path = (settings.static_dir / "videos" / filename).resolve()
    if not audio_path.exists():
        raise PipelineError(f"找不到待识别音频：{audio_path.name}")
    max_bytes = settings.dashscope_asr_max_mb * 1024 * 1024
    duration = _audio_duration_seconds(audio_path)
    needs_split = (
        audio_path.stat().st_size > max_bytes
        or duration > settings.dashscope_asr_chunk_seconds
    )
    if not needs_split:
        text = _transcribe_audio_file(audio_path, api_key)
        if progress_callback:
            progress_callback(1, 1)
        return text

    chunk_dir, chunks = _split_audio_for_asr(audio_path)
    cache_path = chunk_dir / "transcripts.json"
    transcript_cache: dict[str, str] = {}
    if cache_path.exists():
        try:
            loaded = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                transcript_cache = {
                    str(name): str(text)
                    for name, text in loaded.items()
                    if isinstance(text, str) and text.strip()
                }
        except (OSError, json.JSONDecodeError):
            transcript_cache = {}

    transcripts: list[str] = []
    completed = False
    try:
        for index, chunk in enumerate(chunks, start=1):
            text = transcript_cache.get(chunk.name)
            if not text:
                try:
                    text = _transcribe_audio_file(chunk, api_key)
                except Exception as exc:
                    raise PipelineError(
                        f"第 {index}/{len(chunks)} 段识别失败：{exc}"
                    ) from exc
                transcript_cache[chunk.name] = text
                cache_path.write_text(
                    json.dumps(transcript_cache, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            transcripts.append(text)
            if progress_callback:
                progress_callback(index, len(chunks))
        completed = True
    finally:
        if completed:
            shutil.rmtree(chunk_dir, ignore_errors=True)
    return "\n".join(transcripts)


def summarize(text: str) -> str:
    prompt = f"""
#背景#
以下文本来自课堂录音的语音识别，可能存在口语化表达和识别错误。
#任务#
理解教师实际讲授内容，纠正明显识别错误，整理成连贯、准确、简洁的课程笔记。
#输出#
只输出可直接嵌入网页正文区域的 HTML；不要输出 html、head、body 标签，不要评论任务本身。
#输入#
{text}
""".strip()
    return _call_text_model("你是一名擅长整理课堂内容的教师。", prompt)


def _validate_mindmap(data: dict[str, Any]) -> None:
    if data.get("format") != "node_tree":
        raise ValueError("format 必须是 node_tree")
    if not isinstance(data.get("meta"), dict) or not data["meta"].get("name"):
        raise ValueError("meta.name 缺失")

    def validate_node(node: Any) -> None:
        if not isinstance(node, dict) or not node.get("id") or not node.get("topic"):
            raise ValueError("导图节点必须包含 id 和 topic")
        children = node.get("children", [])
        if not isinstance(children, list):
            raise ValueError("children 必须是数组")
        for child in children:
            validate_node(child)

    validate_node(data.get("data"))


def gen_xmind_json(text: str) -> str:
    prompt = f"""
根据课堂文本生成 node_tree 思维导图并提取一个课程关键词。只返回以下结构的合法 JSON：
{{
  "meta": {{"name": "课程关键词", "author": "AI生成", "version": "1.0"}},
  "format": "node_tree",
  "data": {{"id": "1", "topic": "根主题", "children": []}}
}}
每个节点必须包含唯一 id、简洁 topic 和 children 数组。课堂文本如下：
{text}
""".strip()
    data = _generate_json(
        "你是一名擅长把非结构化课程内容整理为层级导图的教师。",
        prompt,
        _validate_mindmap,
    )
    return json.dumps(data, ensure_ascii=False)


def write_xmind_file(xmind_json: dict[str, Any], filename: str) -> Path:
    output = settings.static_dir / "xminds" / f"{Path(filename).stem}.xmind"

    def add_node(parent, node: dict[str, Any]) -> None:
        parent.setTitle(node["topic"])
        for child in node.get("children", []):
            add_node(parent.addSubTopic(), child)

    workbook = xmind.load(str(output))
    sheet = workbook.getPrimarySheet()
    sheet.setTitle(xmind_json["meta"]["name"])
    add_node(sheet.getRootTopic(), xmind_json["data"])
    xmind.save(workbook, str(output))
    return output


MAX_QUESTION_LEN = 500
MAX_OPTION_LEN = 200
MAX_ANALYSIS_LEN = 500


def _validate_choice_item(item: Any, allow_multi: bool) -> None:
    if not isinstance(item, dict):
        raise ValueError("题目必须是对象")
    question = str(item.get("question") or "")
    if not question:
        raise ValueError("题干不能为空")
    if len(question) > MAX_QUESTION_LEN:
        raise ValueError(f"题干不能超过 {MAX_QUESTION_LEN} 字")
    options = item.get("options")
    if not isinstance(options, dict) or set(options.keys()) != {"A", "B", "C", "D"}:
        raise ValueError("每道选择题必须包含 A/B/C/D 四个选项")
    for text in options.values():
        if len(str(text)) > MAX_OPTION_LEN:
            raise ValueError(f"选项文本不能超过 {MAX_OPTION_LEN} 字")
    answer = str(item.get("answer") or "")
    letters = set(answer)
    if not allow_multi:
        if answer not in {"A", "B", "C", "D"}:
            raise ValueError("单选题答案必须是 A/B/C/D 之一")
    else:
        if not (2 <= len(letters) <= 4) or not letters <= {"A", "B", "C", "D"} \
                or len(letters) != len(answer):
            raise ValueError("多选题答案必须是 A-D 中 2-4 个不重复字母")
    if not item.get("knowledge_point"):
        raise ValueError("每道题必须标注知识点")
    if len(str(item.get("analysis") or "")) > MAX_ANALYSIS_LEN:
        raise ValueError(f"解析不能超过 {MAX_ANALYSIS_LEN} 字")


def _validate_exam(data: dict[str, Any]) -> None:
    singles = data.get("singles")
    multis = data.get("multis")
    judges = data.get("judges")
    if not isinstance(singles, list) or len(singles) != 6:
        raise ValueError("必须生成 6 道单选题")
    if not isinstance(multis, list) or len(multis) != 4:
        raise ValueError("必须生成 4 道多选题")
    if not isinstance(judges, list) or len(judges) != 6:
        raise ValueError("必须生成 6 道判断题")
    for item in singles:
        _validate_choice_item(item, allow_multi=False)
    for item in multis:
        _validate_choice_item(item, allow_multi=True)
    for item in judges:
        if not isinstance(item, dict):
            raise ValueError("判断题必须是对象")
        question = str(item.get("question") or "")
        if not question:
            raise ValueError("判断题题干不能为空")
        if len(question) > MAX_QUESTION_LEN:
            raise ValueError(f"题干不能超过 {MAX_QUESTION_LEN} 字")
        if str(item.get("answer") or "") not in {"T", "F"}:
            raise ValueError("判断题答案必须是 T 或 F")
        if not item.get("knowledge_point"):
            raise ValueError("判断题必须标注知识点")
        if len(str(item.get("analysis") or "")) > MAX_ANALYSIS_LEN:
            raise ValueError(f"解析不能超过 {MAX_ANALYSIS_LEN} 字")


def _normalize_exam_item(item: dict[str, Any]) -> dict[str, Any]:
    difficulty = item.get("difficulty")
    if difficulty not in (1, 2, 3):
        difficulty = 2
    return {**item, "difficulty": difficulty}


def gen_exam(text: str) -> str:
    prompt = f"""
根据课堂文本按组卷蓝图生成一套纯客观题试卷：6 道单选题、4 道多选题、6 道判断题，覆盖核心知识点。
要求：
1. 多选题正确答案为 2-4 个选项；判断题只考课堂中明确讲过的事实性结论，答案 T 表示正确、F 表示错误。
2. 难度配比约为 易(1) 6 道 / 中(2) 7 道 / 难(3) 3 道，difficulty 只能取 1、2、3。
3. 题干不超过 500 字，每个选项不超过 200 字。
4. 每道题标注考察的知识点（knowledge_point，2-8 个字）并给出简短解析（analysis，100 字内）。
5. 多选题的 answer 用大写字母升序拼接，如 "ABD"。
只返回合法 JSON：
{{
  "singles": [{{"question": "题目", "options": {{"A": "", "B": "", "C": "", "D": ""}}, "answer": "A", "knowledge_point": "知识点", "difficulty": 2, "analysis": "解析"}}],
  "multis": [{{"question": "题目", "options": {{"A": "", "B": "", "C": "", "D": ""}}, "answer": "ABD", "knowledge_point": "知识点", "difficulty": 2, "analysis": "解析"}}],
  "judges": [{{"question": "题目", "answer": "T", "knowledge_point": "知识点", "difficulty": 2, "analysis": "解析"}}]
}}
课堂文本如下：
{text}
""".strip()
    data = _generate_json(
        "你是一名擅长根据课程知识点设计考试题目的教师。",
        prompt,
        _validate_exam,
    )
    return json.dumps(data, ensure_ascii=False)


def insert_exam(examtext: str, videoid: int) -> None:
    exam_json = json.loads(examtext)
    if "blanks" in exam_json or "qas" in exam_json:
        raise ValueError("检测到旧版试卷格式，需要重新生成")
    _validate_exam(exam_json)
    with Session(engine) as session:
        if session.exec(select(Exams).where(Exams.videoid == videoid)).first():
            return
        for choice in exam_json["singles"]:
            item = _normalize_exam_item(choice)
            session.add(
                Exams(
                    videoid=videoid,
                    question=item["question"],
                    answer=item["answer"],
                    type="choice",
                    score=5,
                    options=json.dumps(item["options"], ensure_ascii=False),
                    knowledge_point=item["knowledge_point"],
                    analysis=item.get("analysis"),
                    difficulty=item["difficulty"],
                )
            )
        for multi in exam_json["multis"]:
            item = _normalize_exam_item(multi)
            answer = "".join(sorted(set(str(item["answer"]).upper())))
            session.add(
                Exams(
                    videoid=videoid,
                    question=item["question"],
                    answer=answer,
                    type="multi",
                    score=10,
                    options=json.dumps(item["options"], ensure_ascii=False),
                    knowledge_point=item["knowledge_point"],
                    analysis=item.get("analysis"),
                    difficulty=item["difficulty"],
                )
            )
        for judge in exam_json["judges"]:
            item = _normalize_exam_item(judge)
            session.add(
                Exams(
                    videoid=videoid,
                    question=item["question"],
                    answer=item["answer"],
                    type="judge",
                    score=5,
                    options=None,
                    knowledge_point=item["knowledge_point"],
                    analysis=item.get("analysis"),
                    difficulty=item["difficulty"],
                )
            )
        session.commit()


def gen_reference(keyword: str) -> list[dict[str, Any]]:
    if not settings.aliyun_search_api_key or not settings.aliyun_search_endpoint:
        raise PipelineError("未配置阿里云搜索 Key 或 Endpoint，已跳过扩展资料")
    response = requests.post(
        settings.aliyun_search_endpoint,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings.aliyun_search_api_key}",
        },
        json={"query": keyword, "top_k": 4},
        timeout=30,
    )
    response.raise_for_status()
    result = response.json().get("result", {}).get("search_result", [])
    if not isinstance(result, list):
        raise PipelineError("搜索接口返回结构不正确")
    return result


def gen_key_frames(filepath: str | Path) -> list[Path]:
    filepath = Path(filepath)
    target = settings.static_dir / "frames" / filepath.name
    marker = target / ".complete"
    if marker.exists():
        return sorted(target.glob("*.jpg"))

    staging = target.with_name(f"{target.name}.building")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)

    sampled_frames: list[np.ndarray] = []
    cap = cv2.VideoCapture(str(filepath))
    try:
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        for index in range(frame_count):
            ok, frame = cap.read()
            if not ok:
                break
            if index % 200 == 0:
                sampled_frames.append(frame)
    finally:
        cap.release()

    written: list[Path] = []
    for index in range(max(0, len(sampled_frames) - 1)):
        previous = cv2.cvtColor(sampled_frames[index], cv2.COLOR_BGR2GRAY)
        following = cv2.cvtColor(sampled_frames[index + 1], cv2.COLOR_BGR2GRAY)
        if float(np.average(cv2.absdiff(previous, following))) > 0.8:
            output = staging / f"{index}.jpg"
            cv2.imwrite(str(output), sampled_frames[index + 1])
            written.append(output)

    marker_in_staging = staging / ".complete"
    marker_in_staging.touch()
    if target.exists():
        shutil.rmtree(target)
    staging.rename(target)
    return [target / path.name for path in written]


def gen_wrong_reason(exam: Exams, student_answer: str, userid: int) -> tuple[str, str] | None:
    """为错题生成结构化错因标签与个性化解析，写入该生的 WrongBook 行。

    返回 (reason, analysis)；AI 失败时返回 None，不阻塞主流程。
    """
    options_text = ""
    if exam.options:
        try:
            options = json.loads(exam.options)
            options_text = "；".join(f"{k}.{v}" for k, v in sorted(options.items()))
        except json.JSONDecodeError:
            options_text = ""
    type_names = {"choice": "单选题", "multi": "多选题", "judge": "判断题"}
    prompt = f"""
{type_names.get(exam.type, "客观题")}：{exam.question}
{f"选项：{options_text}" if options_text else ""}
标准答案：{exam.answer}
学生的错误答案：{student_answer or "（未作答）"}
请分析学生出错的原因。只返回合法 JSON：
{{"reason": "从 概念混淆/知识盲区/粗心失误/方法错误 四者中选最贴切的一个", "analysis": "150 字以内，说明出错原因并给出正确解题思路"}}
""".strip()

    def validate(data: dict[str, Any]) -> None:
        if data.get("reason") not in ("概念混淆", "知识盲区", "粗心失误", "方法错误"):
            raise ValueError("reason 必须是四类错因之一")
        if not str(data.get("analysis") or "").strip():
            raise ValueError("analysis 不能为空")

    try:
        data = _generate_json("你是一名耐心细致的辅导教师。", prompt, validate)
    except PipelineError:
        return None
    reason = data["reason"]
    analysis = str(data["analysis"]).strip()
    with Session(engine) as session:
        row = session.exec(
            select(WrongBook).where(
                WrongBook.userid == userid, WrongBook.examid == exam.examid
            )
        ).first()
        if row is not None and not row.wrong_reason:
            row.wrong_reason = reason
            row.wrong_analysis = analysis
            row.updated_at = datetime.now(timezone.utc)
            session.add(row)
            session.commit()
    return reason, analysis


def _update_video(videoid: int, **values: Any) -> None:
    with Session(engine) as session:
        video = session.get(Videos, videoid)
        if video is None:
            raise PipelineError(f"视频记录不存在：{videoid}")
        for name, value in values.items():
            setattr(video, name, value)
        video.updated_at = datetime.now(timezone.utc)
        session.add(video)
        session.commit()


def process_video(videoid: int) -> None:
    stage = "extracting_audio"
    try:
        _update_video(
            videoid,
            status="processing",
            stage="extracting_audio",
            progress=5,
            error_message=None,
        )
        with Session(engine) as session:
            video = session.get(Videos, videoid)
            if video is None:
                raise PipelineError(f"视频记录不存在：{videoid}")
            filename = video.filename

        video_path = settings.static_dir / "videos" / filename
        audio_path = video_path.with_suffix(".mp3")
        if not audio_path.exists():
            media = video_audio(filename)
            _update_video(videoid, duration=media["duration"])

        with Session(engine) as session:
            video = session.get(Videos, videoid)
            content = video.content if video else None
        if not content:
            stage = "transcribing"
            _update_video(videoid, stage=stage, progress=20)
            content = audio_text(
                audio_path.name,
                progress_callback=lambda current, total: _update_video(
                    videoid,
                    stage="transcribing",
                    progress=20 + int(20 * current / total),
                ),
            )
            _update_video(videoid, content=content, progress=40)

        with Session(engine) as session:
            video = session.get(Videos, videoid)
            summary = video.summary if video else None
        if not summary:
            stage = "summarizing"
            _update_video(videoid, stage=stage, progress=45)
            summary = summarize(content)
            _update_video(videoid, summary=summary, progress=55)

        with Session(engine) as session:
            video = session.get(Videos, videoid)
            xmind_text = video.xmindjson if video else None
        stage = "generating_mindmap"
        _update_video(videoid, stage=stage, progress=60)
        if not xmind_text:
            xmind_text = gen_xmind_json(content)
        xmind_data = json.loads(xmind_text)
        _validate_mindmap(xmind_data)
        write_xmind_file(xmind_data, filename)
        keyword = xmind_data["meta"]["name"]
        _update_video(
            videoid,
            xmindjson=json.dumps(xmind_data, ensure_ascii=False),
            keyword=keyword,
            progress=70,
        )

        stage = "extracting_frames"
        _update_video(videoid, stage=stage, progress=72)
        gen_key_frames(video_path)
        _update_video(videoid, progress=80)

        with Session(engine) as session:
            video = session.get(Videos, videoid)
            reference_text = video.reference if video else None
        if reference_text is None:
            stage = "searching"
            _update_video(videoid, stage=stage, progress=82)
            try:
                references = _retry(lambda: gen_reference(keyword))
                _update_video(
                    videoid,
                    reference=json.dumps(references, ensure_ascii=False),
                    progress=88,
                )
            except Exception as exc:
                _update_video(
                    videoid,
                    reference="[]",
                    warning_message=str(exc),
                    progress=88,
                )

        with Session(engine) as session:
            video = session.get(Videos, videoid)
            exam_text = video.examjson if video else None
        stage = "generating_exam"
        _update_video(videoid, stage=stage, progress=90)
        # 旧版试卷缓存（含填空/简答）视为无效，重新生成纯客观题试卷
        if exam_text:
            try:
                cached = json.loads(exam_text)
            except json.JSONDecodeError:
                cached = {}
            if not isinstance(cached, dict) or "blanks" in cached or "qas" in cached \
                    or "singles" not in cached:
                exam_text = None
        if not exam_text:
            exam_text = gen_exam(content)
        insert_exam(exam_text, videoid)
        _update_video(
            videoid,
            examjson=exam_text,
            status="completed",
            stage="completed",
            progress=100,
            error_message=None,
        )
        audio_path.unlink(missing_ok=True)
    except Exception as exc:
        _update_video(
            videoid,
            status="failed",
            stage=stage,
            error_message=f"{stage}: {exc}",
        )


def retry_incomplete_videos() -> list[int]:
    with Session(engine) as session:
        videos = session.exec(
            select(Videos).where(Videos.status.in_(["queued", "processing"]))
        ).all()
        ids = [video.videoid for video in videos if video.videoid is not None]
        for video in videos:
            video.status = "queued"
            video.stage = "queued"
            video.updated_at = datetime.now(timezone.utc)
            session.add(video)
        session.commit()
    return ids
