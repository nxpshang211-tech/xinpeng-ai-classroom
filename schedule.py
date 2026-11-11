"""手动恢复所有排队中或处理中断的视频。

正常启动 main.py 时会自动执行相同的恢复逻辑；本脚本只用于维护。
"""

from model import init_database
from module import process_video, retry_incomplete_videos


def main() -> None:
    init_database()
    videoids = retry_incomplete_videos()
    if not videoids:
        print("没有需要恢复的视频任务。")
        return
    for videoid in videoids:
        print(f"正在恢复视频任务 {videoid} ...")
        process_video(videoid)
    print("恢复任务执行完毕。")


if __name__ == "__main__":
    main()
