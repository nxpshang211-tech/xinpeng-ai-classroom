// 全局登录状态校验：所有业务页面加载时调用
// 未登录或 Token 无效/过期时立即跳转登录页；已登录则直接放行并显示欢迎语
function requireLogin() {
    const token = localStorage.getItem("token");
    if (token == null) {
        location.replace("/login");
        return;
    }
    fetch("/checklogin", {
        method: 'POST',
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({"token": token})
    })
    .then(response => response.json())
    .then(data => {
        if (data["message"] !== "Token-OK") {
            localStorage.clear();
            location.replace("/login");
            return;
        }
        const welcome = document.getElementById("welcome");
        if (welcome) {
            welcome.innerHTML = "你好，" + data["realname"] +
                '&nbsp;&nbsp;<a href="#" onclick="doLogout()">退出</a>';
        }
    })
    .catch(err => console.log('登录校验请求失败', err));
}

// 登录处理，只需要将Token保存到localStorage中
function doLogin() {
    const username = document.getElementById("username").value;
    const password = document.getElementById("password").value;
    const errorBox = document.getElementById("login-error");
    fetch("/login", {
        method: 'POST',
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({"username": username, "password": password})
    })
    .then(response => response.json())
    .then(data => {
        if (data["message"] === "login-ok") {
            localStorage.setItem("token", data["token"]);
            localStorage.setItem("realname", data["realname"]);
            location.href = "/";
        }
        else if (data["message"] === "user-not-exist") {
            errorBox.textContent = "用户名不存在，请先注册";
        }
        else {
            errorBox.textContent = "密码错误，请重试";
        }
    })
    .catch(err => {
        errorBox.textContent = "网络异常，请稍后重试";
        console.log('请求失败', err);
    });
}

// 退出登录只需要清空浏览器Token，由于后端也未存储Token，所以不需要处理
function doLogout() {
    localStorage.clear();
    location.replace("/login");
}

// 浏览到视频文件时，将视频文件的文件名填充到视频标题中
function fillTitle() {
    let title = document.getElementById("file").files[0].name;
    title = title.split(".")[0];
    document.getElementById("title").value = title;
}

// 上传视频，如果上传成功则刷新页面，以显示最新的视频信息
function doUpload(obj) {
    const fileInput = document.getElementById("file");
    const statusBox = document.getElementById("upload-status");
    if (!fileInput.files.length) {
        alert("请先选择 MP4 视频");
        return;
    }
    obj.disabled = true;
    obj.textContent = "正在上传...";
    let formData = new FormData();
    formData.append("title", document.getElementById("title").value);
    formData.append("file", fileInput.files[0]);
    fetch('/upload', {
        method: 'POST',
        body: formData
    })
    .then(async response => {
        const data = await response.json();
        if (!response.ok) {
            throw new Error(data.detail || "上传失败");
        }
        return data;
    })
    .then(data => {
        obj.textContent = "已进入处理队列";
        if (statusBox) {
            statusBox.textContent = "视频上传完成，AI 正在后台处理（0%）";
        }
        pollVideoStatus(data.video_id, statusBox, true);
    })
    .catch(err => {
        obj.disabled = false;
        obj.textContent = "上传视频";
        if (statusBox) statusBox.textContent = err.message;
        alert(err.message);
    });
}

const stageNames = {
    uploaded: "等待处理",
    queued: "等待处理",
    extracting_audio: "正在提取音频",
    transcribing: "正在识别语音",
    summarizing: "正在生成课堂笔记",
    generating_mindmap: "正在生成思维导图",
    extracting_frames: "正在提取关键帧",
    searching: "正在搜索扩展资料",
    generating_exam: "正在生成考题",
    completed: "处理完成",
    failed: "处理失败"
};

function pollVideoStatus(videoId, target, reloadWhenDone = false) {
    fetch(`/api/videos/${videoId}/status`)
        .then(async response => {
            const data = await response.json();
            if (!response.ok) throw new Error(data.detail || "无法获取处理进度");
            return data;
        })
        .then(data => {
            const message = stageNames[data.stage] || data.stage;
            if (target) {
                target.innerHTML = `${message}（${data.progress}%）`;
                if (data.warning) target.innerHTML += `<br>提示：${data.warning}`;
                if (data.error) target.innerHTML += `<br>错误：${data.error}`;
            }
            const progress = document.getElementById(`progress-${videoId}`);
            if (progress) progress.value = data.progress;
            const messageBox = document.getElementById(`message-${videoId}`);
            if (messageBox) messageBox.textContent = data.error || data.warning || "";

            if (data.status === "completed") {
                if (reloadWhenDone) location.reload();
                else if (location.pathname.startsWith("/detail/")) location.reload();
                return;
            }
            if (data.status === "failed") return;
            window.setTimeout(() => pollVideoStatus(videoId, target, reloadWhenDone), 1500);
        })
        .catch(err => {
            if (target) target.textContent = err.message;
        });
}

function startPendingVideoPolling() {
    document.querySelectorAll("[data-video-status]").forEach(element => {
        const status = element.dataset.videoStatus;
        if (status === "queued" || status === "processing") {
            pollVideoStatus(element.dataset.videoId, element, false);
        }
    });
}

function retryVideo(videoId) {
    fetch(`/api/videos/${videoId}/retry`, {method: "POST"})
        .then(async response => {
            const data = await response.json();
            if (!response.ok) throw new Error(data.detail || "重试失败");
            location.reload();
        })
        .catch(err => alert(err.message));
}

function drawMindMap(data) {
    // 配置jsmind的各个选项
    const options = {
        container: 'jsmind_container',  // div名称
        theme: 'primary',      // 外观，使用默认
        editable: false,       // 是否可以编辑Xmind
        mode: 'full',          // 是否支持全屏
        support_html: true,    // 是否支持在节点中渲染HTML
        view: {                // 常用的绘制选择
            hmargin: 100,
            vmargin: 50,
            line_width: 2,
            line_color: '#555'
        }
    }
    // 实例化jsmind并将所有节点展开
    const jm = new jsMind(options);
    jm.show(data);
    jm.expand_all();
}

/* ==================== 做题模块：实时持久化 ==================== */

// 每道题的防抖计时器，输入停止 500ms 后自动保存
const draftTimers = {};

// 将单题答案实时保存到后端，失败时自动重试
function saveDraft(videoid, examid, answer, retries = 3) {
    const token = localStorage.getItem("token");
    const indicator = document.getElementById("save-status");
    if (indicator) indicator.textContent = "保存中...";
    fetch(`/exam/draft/${videoid}/${token}`, {
        method: 'POST',
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({"examid": examid, "answer": answer})
    })
    .then(async response => {
        if (!response.ok) throw new Error("save failed");
        if (indicator) {
            indicator.textContent = "已保存 " + new Date().toLocaleTimeString();
        }
    })
    .catch(() => {
        if (retries > 0) {
            if (indicator) indicator.textContent = "保存失败，正在重试...";
            window.setTimeout(() => saveDraft(videoid, examid, answer, retries - 1), 2000);
        } else if (indicator) {
            indicator.textContent = "保存失败，请检查网络";
        }
    });
}

// 简答题输入防抖：停止输入 500ms 后才发起保存，避免频繁请求
function debouncedSaveDraft(videoid, examid, answer) {
    clearTimeout(draftTimers[examid]);
    draftTimers[examid] = window.setTimeout(
        () => saveDraft(videoid, examid, answer), 500
    );
}

// 统计并展示做题进度：已完成题数 / 总题数（redo 模式下隐藏的题目不计入）
function updateExamStats() {
    const visible = el => el.offsetParent !== null;
    const radios = Array.from(
        document.querySelectorAll('.choice-box input[type="radio"]')
    ).filter(visible);
    const totalChoices = new Set(radios.map(radio => radio.name)).size;
    const answeredChoices = new Set(
        radios.filter(radio => radio.checked).map(radio => radio.name)
    ).size;
    const textareas = Array.from(document.querySelectorAll(".qa-box textarea")).filter(visible);
    const answeredQas = textareas.filter(t => t.value.trim() !== "").length;
    const blanks = Array.from(document.querySelectorAll(".blank-box .blank-input")).filter(visible);
    const answeredBlanks = blanks.filter(t => t.value.trim() !== "").length;
    const total = totalChoices + textareas.length + blanks.length;
    const answered = answeredChoices + answeredQas + answeredBlanks;
    const progress = document.getElementById("exam-progress");
    if (progress) progress.textContent = `进度：${answered}/${total}`;
}

// 当前答题模式：exam=整卷练习 / redo=错题重练
function examMode() {
    return new URLSearchParams(location.search).get("mode") === "redo" ? "redo" : "exam";
}

// 题目标记：勾选/取消后同步到草稿
function toggleMark(videoid, examid, marked) {
    const token = localStorage.getItem("token");
    fetch(`/exam/mark/${videoid}/${token}`, {
        method: 'POST',
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({"examid": examid, "marked": marked})
    }).catch(err => console.log('标记失败', err));
}

// 单题即时判分：客观题本地判分秒回，简答题需等待 AI 评分
function judgeOne(videoid, examid, type) {
    const token = localStorage.getItem("token");
    const resultBox = document.getElementById(`judge-${examid}`);
    let answer = "";
    if (type === "choice") {
        const radio = document.querySelector(`input[name="choice-${examid}"]:checked`);
        if (!radio) { resultBox.textContent = "请先选择答案"; return; }
        answer = radio.value.split("-")[2];
    } else if (type === "blank") {
        answer = document.getElementById(`blank-${examid}`).value.trim();
        if (!answer) { resultBox.textContent = "请先填写答案"; return; }
    } else {
        answer = document.getElementById(`qa-${examid}`).value.trim();
        if (!answer) { resultBox.textContent = "请先填写答案"; return; }
    }
    resultBox.textContent = "判分中...";
    fetch(`/api/practice/judge/${token}`, {
        method: 'POST',
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({"examid": examid, "answer": answer})
    })
    .then(response => response.json())
    .then(data => {
        if (data["correct"]) {
            resultBox.textContent = `回答正确，得 ${data.score} 分`;
        } else if (type === "qa") {
            resultBox.textContent = `得分 ${data.score}，参考答案：${data.analysis}`;
        } else {
            resultBox.textContent =
                `回答错误，标准答案：${data.answer}。解析：${data.analysis || "暂无"}`;
        }
    })
    .catch(err => { resultBox.textContent = "判分失败，请稍后重试"; console.log(err); });
}

// 答题时长计时：首次进入页面时记录起点，之后每秒刷新
function startExamTimer(videoid) {
    const key = `exam-start-${videoid}`;
    if (localStorage.getItem(key) == null) {
        localStorage.setItem(key, String(Date.now()));
    }
    const start = Number(localStorage.getItem(key));
    const box = document.getElementById("exam-duration");
    if (!box) return;
    const tick = () => {
        const seconds = Math.floor((Date.now() - start) / 1000);
        const mm = String(Math.floor(seconds / 60)).padStart(2, "0");
        const ss = String(seconds % 60).padStart(2, "0");
        box.textContent = `用时：${mm}:${ss}`;
    };
    tick();
    window.setInterval(tick, 1000);
}

// 提交后进入回顾模式：锁定所有输入，展示总分与每题得分
function enterReviewMode(data) {
    document.querySelectorAll('.choice-box input[type="radio"]').forEach(radio => {
        radio.disabled = true;
    });
    document.querySelectorAll(".qa-box textarea").forEach(t => {
        t.disabled = true;
    });
    document.querySelectorAll(".blank-box .blank-input").forEach(t => {
        t.disabled = true;
    });
    document.querySelectorAll(".judge-line").forEach(line => {
        line.style.display = "none";
    });
    const submit = document.getElementById("submit");
    if (submit) submit.style.display = "none";

    // 在统计栏展示总分与选择题准确率
    let choiceTotal = 0, choiceCorrect = 0;
    document.querySelectorAll(".choice-box").forEach(box => {
        const radio = box.querySelector('input[type="radio"]');
        if (!radio) return;
        const examid = radio.name.split("-")[1];
        choiceTotal += 1;
        if ((data.scores || {})[examid] === 5) choiceCorrect += 1;
    });
    const accuracy = document.getElementById("exam-accuracy");
    if (accuracy && choiceTotal > 0) {
        accuracy.textContent = `选择题准确率：${choiceCorrect}/${choiceTotal}`;
    }
    const saveStatus = document.getElementById("save-status");
    if (saveStatus) saveStatus.textContent = `已提交，总分 ${data.total} 分`;
}

// 考试页初始化：恢复草稿、绑定实时保存、启动统计与计时；redo 模式只显示未掌握错题
function initExamPage(videoid) {
    const token = localStorage.getItem("token");
    const mode = examMode();
    fetch(`/exam/draft/${videoid}/${token}`)
        .then(response => response.json())
        .then(data => {
            const answers = data.answers || {};
            Object.keys(answers).forEach(examid => {
                const answer = answers[examid];
                const radio = document.querySelector(
                    `input[name="choice-${examid}"][value="choice-${examid}-${answer}"]`
                );
                if (radio) radio.checked = true;
                const textarea = document.getElementById(`qa-${examid}`);
                if (textarea) textarea.value = answer;
                const blank = document.getElementById(`blank-${examid}`);
                if (blank) blank.value = answer;
            });
            (data.marked || []).forEach(examid => {
                const box = document.getElementById(`mark-${examid}`);
                if (box) box.checked = true;
            });

            if (mode === "redo") {
                setupRedoMode(videoid, token);
            } else if (data.submitted) {
                enterReviewMode(data);
            }

            // 绑定实时保存：选项变更立即保存，文本输入防抖保存
            document.querySelectorAll('.choice-box input[type="radio"]').forEach(radio => {
                radio.addEventListener("change", () => {
                    const examid = radio.name.split("-")[1];
                    saveDraft(videoid, examid, radio.value.split("-")[2]);
                    updateExamStats();
                });
            });
            document.querySelectorAll(".qa-box textarea").forEach(textarea => {
                textarea.addEventListener("input", () => {
                    const examid = textarea.id.split("-")[1];
                    debouncedSaveDraft(videoid, examid, textarea.value);
                    updateExamStats();
                });
            });
            document.querySelectorAll(".blank-box .blank-input").forEach(input => {
                input.addEventListener("input", () => {
                    const examid = input.id.split("-")[1];
                    debouncedSaveDraft(videoid, examid, input.value);
                    updateExamStats();
                });
            });

            updateExamStats();
            startExamTimer(videoid);
        })
        .catch(err => console.log('草稿恢复失败', err));
}

// 错题重练：拉取本视频下未掌握的错题，隐藏其余题目
function setupRedoMode(videoid, token) {
    fetch(`/api/wrongbook/list/${token}?mastered=false`)
        .then(response => response.json())
        .then(data => {
            const wrongIds = new Set(
                (data.items || [])
                    .filter(item => String(item.videoid) === String(videoid))
                    .map(item => item.examid)
            );
            if (wrongIds.size === 0) {
                alert("本课程没有待复习的错题");
                const submit = document.getElementById("submit");
                if (submit) submit.style.display = "none";
                return;
            }
            document.querySelectorAll(".choice-box, .qa-box, .blank-box").forEach(box => {
                const examid = Number(box.id.split("-")[1]);
                if (!wrongIds.has(examid)) box.style.display = "none";
            });
            updateExamStats();
        })
        .catch(err => console.log('错题加载失败', err));
}

// 提交试卷：先检查完整性，提交成功后进入回顾模式（redo 模式提交后回到错题本）
function doExam(videoid) {
    const token = localStorage.getItem("token");
    if (token == null) {
        location.replace("/login");
        return false;
    }
    const mode = examMode();
    const visible = el => el.offsetParent !== null;

    const radios = Array.from(
        document.querySelectorAll('.choice-box input[type="radio"]')
    ).filter(visible);
    const totalChoices = new Set(radios.map(radio => radio.name)).size;
    const checkedRadios = radios.filter(radio => radio.checked);
    if (checkedRadios.length < totalChoices) {
        alert("你的选择题似乎未完成.");
        return false;
    }

    let answers = {"mode": mode};
    answers['choices'] = checkedRadios.map(radio => radio.value);

    for (const input of document.querySelectorAll(".blank-box .blank-input")) {
        if (!visible(input)) continue;
        if (input.value.trim() === "") {
            alert("请完成填空题的填写");
            return false;
        }
        answers[input.id] = input.value;
    }

    for (const textarea of document.getElementsByTagName("textarea")) {
        if (!visible(textarea)) continue;
        if (textarea.value.trim() === "") {
            alert("请完成简答题的填写");
            return false;
        }
        if (textarea.id) {
            answers[textarea.id] = textarea.value;
        }
    }

    // 答题用时（秒），随提交上报用于学习统计
    const start = Number(localStorage.getItem(`exam-start-${videoid}`) || Date.now());
    answers["duration_seconds"] = Math.floor((Date.now() - start) / 1000);

    // 点击提交后，如果满足提交条件，则此时禁用提交按钮，以避免重复提交
    document.getElementById("submit").disabled = true;
    document.getElementById("submit").style.backgroundColor = "gray";
    document.getElementById("submit").textContent = "正在提交...";

    fetch("/exam/submit/" + videoid + "/" + token, {
        method: 'POST',
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(answers)
    }).then(async response => {
        const text = await response.text();
        if (text.includes("Token-Fail")) {
            localStorage.clear();
            location.replace("/login");
            return;
        }
        if (text.includes("Already-Exam")) {
            alert("你已经完成过本次考试，请勿重复操作.");
            location.reload();
            return;
        }
        if (!response.ok) {
            let detail = "提交失败";
            try { detail = JSON.parse(text).detail || detail; } catch (e) {}
            alert(detail);
            location.reload();
            return;
        }
        const data = JSON.parse(text);
        const wrongCount = (data.wrong_examids || []).length;
        let message = `你的本次得分为：${data.total} 分，答错 ${wrongCount} 题`;
        if (wrongCount > 0) message += "，错题已收录到错题本";
        if (data.analysis_pending) message += "，错题解析正在生成，稍后可在错题本查看";
        alert(message);
        if (mode === "redo") location.href = "/wrongbook";
        else location.reload();
    })
    .catch(err => console.log('请求失败', err));
}
