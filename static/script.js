// 全局登录状态校验：所有业务页面加载时调用
// 未登录或 Token 无效/过期时立即跳转登录页；已登录则直接放行并显示欢迎语
function renderAuthBox(html) {
    const welcome = document.getElementById("welcome");
    if (welcome) welcome.innerHTML = html;
}

function requireLogin() {
    const token = localStorage.getItem("token");
    if (token == null) {
        // 先渲染出登录按钮，再跳转，避免出现「页面上完全没有登录入口」的空窗
        renderAuthBox('<button class="auth-btn" onclick="location.href=\'/login\'">登录</button>');
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
            renderAuthBox('<button class="auth-btn" onclick="location.href=\'/login\'">登录</button>');
            location.replace("/login");
            return;
        }
        renderAuthBox(
            '你好，' + data["realname"] +
            '&nbsp;<button class="auth-btn" onclick="doLogout()">退出登录</button>'
        );
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

// 收集多选题已勾选的选项字母，去重升序拼接（如 "ABD"）
function collectMultiAnswer(examid) {
    const boxes = Array.from(
        document.querySelectorAll(`input[name="multi-${examid}"]:checked`)
    );
    return boxes.map(box => box.value.split("-")[2]).sort().join("");
}

// 答案展示转义：判断题 T/F 转「正确/错误」，其余原样
function formatAnswer(type, answer) {
    if (type === "judge") {
        if (String(answer).toUpperCase() === "T") return "正确";
        if (String(answer).toUpperCase() === "F") return "错误";
    }
    return answer;
}

// 统计并展示做题进度：已完成题数 / 总题数（redo 模式下隐藏的题目不计入）
function updateExamStats() {
    const visible = el => el.offsetParent !== null;
    const radios = Array.from(
        document.querySelectorAll('.choice-box input[type="radio"], .judge-box input[type="radio"]')
    ).filter(visible);
    const singleGroups = new Set(radios.map(radio => radio.name));
    const answeredSingles = new Set(
        radios.filter(radio => radio.checked).map(radio => radio.name)
    ).size;
    const checkboxes = Array.from(
        document.querySelectorAll('.multi-box .options input[type="checkbox"]')
    ).filter(visible);
    const multiGroups = new Set(checkboxes.map(box => box.name));
    const answeredMultis = new Set(
        checkboxes.filter(box => box.checked).map(box => box.name)
    ).size;
    const total = singleGroups.size + multiGroups.size;
    const answered = answeredSingles + answeredMultis;
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

// 单题即时判分：三类客观题均为本地规则判分，秒回
function judgeOne(videoid, examid, type) {
    const token = localStorage.getItem("token");
    const resultBox = document.getElementById(`judge-${examid}`);
    let answer = "";
    if (type === "choice" || type === "judge") {
        const radio = document.querySelector(`input[name="${type}-${examid}"]:checked`);
        if (!radio) { resultBox.textContent = "请先选择答案"; return; }
        answer = radio.value.split("-")[2];
    } else if (type === "multi") {
        answer = collectMultiAnswer(examid);
        if (!answer) { resultBox.textContent = "请至少勾选一个答案"; return; }
    }
    resultBox.textContent = "判分中...";
    fetch(`/api/practice/judge/${token}`, {
        method: 'POST',
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({"examid": examid, "answer": answer})
    })
    .then(response => response.json())
    .then(data => {
        const standard = formatAnswer(type, data.answer);
        if (data["correct"]) {
            resultBox.textContent = `回答正确，得 ${data.score} 分`;
        } else if (type === "multi" && data.score > 0) {
            resultBox.textContent =
                `漏选，得 ${data.score} 分，标准答案：${standard}。解析：${data.analysis || "暂无"}`;
        } else {
            resultBox.textContent =
                `回答错误，标准答案：${standard}。解析：${data.analysis || "暂无"}`;
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

// 提交后进入回顾模式：锁定所有输入，展示总分与客观题准确率
function enterReviewMode(data) {
    document.querySelectorAll('.choice-box input[type="radio"], .judge-box input[type="radio"]')
        .forEach(radio => { radio.disabled = true; });
    document.querySelectorAll('.multi-box .options input[type="checkbox"]')
        .forEach(box => { box.disabled = true; });
    document.querySelectorAll(".judge-line").forEach(line => {
        line.style.display = "none";
    });
    const submit = document.getElementById("submit");
    if (submit) submit.style.display = "none";

    // 在统计栏展示总分与客观题准确率（基于判分结果，多选半分不算对）
    const results = data.results || [];
    if (results.length > 0) {
        const correct = results.filter(item => item.correct).length;
        const accuracy = document.getElementById("exam-accuracy");
        if (accuracy) accuracy.textContent = `客观题准确率：${correct}/${results.length}`;
    }
    const saveStatus = document.getElementById("save-status");
    if (saveStatus && data.total != null) {
        saveStatus.textContent = `已提交，总分 ${data.total} 分`;
    }
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
                const answer = String(answers[examid] || "");
                // 单选/判断：答案即选项字母或 T/F
                const radio = document.querySelector(
                    `input[name="choice-${examid}"][value="choice-${examid}-${answer}"],` +
                    `input[name="judge-${examid}"][value="judge-${examid}-${answer}"]`
                );
                if (radio) radio.checked = true;
                // 多选：答案 "ABD" → 勾选对应 checkbox
                if (document.querySelector(`input[name="multi-${examid}"]`)) {
                    answer.split("").forEach(letter => {
                        const box = document.querySelector(
                            `input[name="multi-${examid}"][value="multi-${examid}-${letter}"]`
                        );
                        if (box) box.checked = true;
                    });
                }
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

            // 绑定实时保存：任何选项变更立即保存
            document.querySelectorAll('.choice-box input[type="radio"], .judge-box input[type="radio"]')
                .forEach(radio => {
                    radio.addEventListener("change", () => {
                        const examid = radio.name.split("-")[1];
                        saveDraft(videoid, examid, radio.value.split("-")[2]);
                        updateExamStats();
                    });
                });
            document.querySelectorAll('.multi-box .options input[type="checkbox"]')
                .forEach(box => {
                    box.addEventListener("change", () => {
                        const examid = box.name.split("-")[1];
                        saveDraft(videoid, examid, collectMultiAnswer(examid));
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
            document.querySelectorAll(".choice-box, .multi-box, .judge-box").forEach(box => {
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

    const answers = {"mode": mode};
    const collected = [];

    // 单选与判断：每组必须选中一个
    const radios = Array.from(document.querySelectorAll(
        '.choice-box input[type="radio"], .judge-box input[type="radio"]'
    )).filter(visible);
    const radioGroups = new Set(radios.map(radio => radio.name));
    const checkedRadios = radios.filter(radio => radio.checked);
    if (checkedRadios.length < radioGroups.size) {
        alert("还有单选或判断题未完成");
        return false;
    }
    checkedRadios.forEach(radio => collected.push(radio.value));

    // 多选：每题至少勾选一个选项
    const checkboxGroups = new Set(
        Array.from(document.querySelectorAll('.multi-box .options input[type="checkbox"]'))
            .filter(visible)
            .map(box => box.name)
    );
    for (const name of checkboxGroups) {
        const examid = name.split("-")[1];
        const answer = collectMultiAnswer(examid);
        if (!answer) {
            alert("多选题请至少勾选一个选项");
            return false;
        }
        collected.push(`multi-${examid}-${answer}`);
    }

    answers["choices"] = collected;

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

// ===== 设置页：AI 模型配置 =====
// 供应商预设：选中后自动填 base_url 与推荐模型列表，Key 始终手填；模型可下拉选也可自行输入
const AI_PRESETS = {
    text: {
        dashscope: {base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1",
            models: ["qwen-max", "qwen-plus", "qwen-flash", "qwen-long"]},
        // 火山方舟自 2026-08 起必须填推理接入点 Endpoint ID，直接传模型名会 403/404
        volcengine: {base_url: "https://ark.cn-beijing.volces.com/api/v3",
            models: [], manual: true,
            placeholder: "ep-20260806111300-abcde（接入点 ID，非模型名）"},
        tencent: {base_url: "https://api.hunyuan.cloud.tencent.com/v1",
            models: ["hunyuan-turbos-latest", "hunyuan-t1-latest", "hunyuan-lite"]},
        deepseek: {base_url: "https://api.deepseek.com/v1",
            models: ["deepseek-flash", "deepseek-v4-pro"]},
        kimi: {base_url: "https://api.moonshot.cn/v1",
            models: ["kimi-k3.1-pro", "kimi-k3.1-pro-highspeed", "kimi-k3"]},
        custom: {base_url: "", models: [], manual: true}
    },
    asr: {
        dashscope: {base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1",
            models: ["qwen3-asr-flash", "qwen3-asr-plus", "qwen2-asr-flash"],
            key_tip: "直接填写 DashScope API Key", ready: true},
        volcengine: {base_url: "https://openspeech.bytedance.com/api/v3/auc/bigmodel",
            models: ["bigmodel", "seedasr"],
            key_tip: "填写格式为 AccessKeyID,AccessKeySecret", ready: false},
        tencent: {base_url: "https://asr.tencentcloudapi.com",
            models: ["16k_zh_edu", "16k_zh_video", "16k_zh"],
            key_tip: "填写格式为 SecretId,SecretKey", ready: false},
        custom: {base_url: "", models: [], manual: true,
            key_tip: "填写该服务的 API Key", ready: true}
    }
};

// 读取当前模型值：输入框可见时读 input，否则读下拉
function readModelValue(kind) {
    const select = document.getElementById(`${kind}-model`);
    const input = document.getElementById(`${kind}-model-input`);
    if (input.style.display !== "none") return input.value.trim();
    return select.value === "__custom__" ? "" : select.value.trim();
}

// 下拉选中「使用其他模型…」时切换为输入框，其余选项切回下拉
function onModelSelect(kind) {
    const select = document.getElementById(`${kind}-model`);
    const input = document.getElementById(`${kind}-model-input`);
    if (select.value === "__custom__") {
        input.style.display = "block";
        input.value = "";
        input.focus();
    } else {
        input.style.display = "none";
        input.value = "";
    }
}

// 切换供应商预设：自动填 base_url；重建模型候选且不预选；手填型供应商改用输入框
function applyPreset(kind) {
    const provider = document.getElementById(`${kind}-provider`).value;
    const preset = AI_PRESETS[kind][provider] || AI_PRESETS[kind].custom;
    document.getElementById(`${kind}-base-url`).value = preset.base_url;

    const select = document.getElementById(`${kind}-model`);
    const input = document.getElementById(`${kind}-model-input`);
    let options = preset.models.map(m => `<option value="${m}">${m}</option>`).join("");
    // 预设供应商在下拉末尾追加「使用其他模型…」，允许填列表外的模型
    if (!preset.manual) {
        options += '<option value="__custom__">使用其他模型…</option>';
    }
    select.innerHTML = options;
    // 默认选中第一个模型
    select.value = preset.models[0] || "";
    input.value = "";
    input.placeholder = preset.placeholder || "填写模型 ID";
    select.style.display = preset.manual ? "none" : "block";
    input.style.display = preset.manual ? "block" : "none";

    if (kind === "asr") {
        document.getElementById("asr-transport-field").style.display =
            provider === "dashscope" ? "block" : "none";
        document.getElementById("asr-key-tip").textContent = preset.key_tip || "";
        document.getElementById("asr-unsupported-tip").style.display =
            preset.ready === false ? "block" : "none";
    }
}

// 页面加载：拉取当前配置回显（Key 为脱敏值，留空保存则不修改）
function initSettingsPage() {
    const token = localStorage.getItem("token");
    fetch(`/api/settings/ai/${token}`)
    .then(response => response.json())
    .then(data => {
        if (typeof data !== "object" || data === null) return;
        // 已保存的供应商不在预设列表（如旧版遗留值）时归为「自定义」
        for (const kind of ["text", "asr"]) {
            const select = document.getElementById(`${kind}-provider`);
            const saved = data[`${kind}_provider`] || "dashscope";
            select.value = select.querySelector(`option[value="${saved}"]`) ? saved : "custom";
        }
        document.getElementById("text-api-key").value = data.text_api_key || "";
        document.getElementById("asr-api-key").value = data.asr_api_key || "";
        document.getElementById("asr-transport").value = data.asr_transport || "base64";
        // applyPreset 会按预设覆盖 base_url/model，调用后恢复为已保存的值
        const keep = {
            text: {url: data.text_base_url || "", model: data.text_model || ""},
            asr: {url: data.asr_base_url || "", model: data.asr_model || ""}
        };
        applyPreset("text");
        applyPreset("asr");
        for (const kind of ["text", "asr"]) {
            document.getElementById(`${kind}-base-url`).value = keep[kind].url;
            const select = document.getElementById(`${kind}-model`);
            const input = document.getElementById(`${kind}-model-input`);
            const savedModel = keep[kind].model;
            if (input.style.display !== "none") {
                input.value = savedModel;
            } else if (savedModel) {
                if ([...select.options].some(option => option.value === savedModel)) {
                    select.value = savedModel;
                } else {
                    // 已保存模型不在候选列表：切到「使用其他模型…」并回填
                    select.value = "__custom__";
                    input.value = savedModel;
                    input.style.display = "block";
                }
            }
        }
    })
    .catch(err => console.log("加载配置失败", err));
}

function collectSettings() {
    return {
        text_provider: document.getElementById("text-provider").value,
        text_base_url: document.getElementById("text-base-url").value.trim(),
        text_api_key: document.getElementById("text-api-key").value.trim(),
        text_model: readModelValue("text"),
        asr_provider: document.getElementById("asr-provider").value,
        asr_base_url: document.getElementById("asr-base-url").value.trim(),
        asr_api_key: document.getElementById("asr-api-key").value.trim(),
        asr_model: readModelValue("asr"),
        asr_transport: document.getElementById("asr-transport").value
    };
}

function saveSettings() {
    const token = localStorage.getItem("token");
    const resultBox = document.getElementById("settings-save-result");
    const payload = collectSettings();
    if (!payload.text_base_url || !payload.text_model ||
        !payload.asr_base_url || !payload.asr_model) {
        resultBox.textContent = "接口地址与模型名不能为空";
        resultBox.className = "field-tip tip-err";
        return;
    }
    fetch(`/api/settings/ai/${token}`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload)
    })
    .then(response => response.json())
    .then(data => {
        if (data.message === "ok") {
            resultBox.textContent = "已保存，立即生效";
            resultBox.className = "field-tip tip-ok";
        } else {
            resultBox.textContent = data.detail || "保存失败";
            resultBox.className = "field-tip tip-err";
        }
    })
    .catch(err => { resultBox.textContent = "保存失败，请稍后重试"; console.log(err); });
}

// 用当前表单里的文本模型配置发一条 ping 请求验证连通性
function testTextConnection() {
    const token = localStorage.getItem("token");
    const resultBox = document.getElementById("text-test-result");
    resultBox.textContent = "测试中...";
    resultBox.className = "field-tip";
    fetch(`/api/settings/ai/test/${token}`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
            base_url: document.getElementById("text-base-url").value.trim(),
            api_key: document.getElementById("text-api-key").value.trim(),
            model: readModelValue("text")
        })
    })
    .then(response => response.json())
    .then(data => {
        if (data.message === "ok") {
            resultBox.textContent = "连接成功";
            resultBox.className = "field-tip tip-ok";
        } else {
            resultBox.textContent = data.detail || "连接失败";
            resultBox.className = "field-tip tip-err";
        }
    })
    .catch(err => { resultBox.textContent = "连接失败，请检查网络"; console.log(err); });
}
