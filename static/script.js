// 点击登录时，将登录的用户名和密码输入框显示出来
function clickLogin() {
    document.getElementsByClassName("login")[0].style.display = "block";
}

// 浏览到视频文件时，将视频文件的文件名填充到视频标题中
function fillTitle() {
    let title = document.getElementById("file").files[0].name;
    title = title.split(".")[0];
    document.getElementById("title").value = title;
}

// 登录处理，只需要将Token保存到localStorage中
function doLogin() {
    let username = document.getElementById("username").value;
    let password = document.getElementById("password").value;
    fetch("/login", {
        method: 'POST',
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({"username": username, "password": password})
    })
    .then(response => response.text())
    .then(data => {
        let message = JSON.parse(data)['message'];
        if (message === "login-ok") {
            let token = JSON.parse(data)['token'];
            let realname = JSON.parse(data)['realname'];
            localStorage.setItem("token", token);
            localStorage.setItem("realname", realname);
            location.reload();
        }
        else {
            alert("登录失败或用户名错误.");
        }
    })
    .catch(err => console.log('请求失败', err));
}

// 退出登录只需要清空浏览器Token，由于后端也未存储Token，所以不需要处理
function doLogout() {
    localStorage.clear();
    location.reload();
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
    formData.append("course", document.getElementById("course").value);
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

function doExam(videoid) {
    // 检查用户是否已经登录，未登录的情况下不能提交
    const token = localStorage.getItem("token");
    if (token == null) {
        alert("你还未登录，不能提交试卷");
        return false;
    }

    // 遍历页面中所有的已经选中的单选框
    let radios = document.querySelectorAll('input[type="radio"]:checked');
    if (radios.length < 8) {
        alert("你的选择题似乎未完成.");
        return false;
    }
    let answers = {};   // 定义JS对象用于保存所有答案
    let choices = []   // 定义数组用于保存单选结果
    for (const radio of radios) {
        choices.push(radio.value);
    }
    answers['choices'] = choices;    /// 将单选结果数组赋值给choices这个Key

    // 遍历所有文本域，并获取里面的值
    let textareas = document.getElementsByTagName("textarea");
    for (const textarea of textareas) {
        if (textarea.value.trim() === "") {
            alert("请完成简答题的填写");
            return false;
        }
        if (textarea.id) {
            answers[textarea.id] = textarea.value;
        }
    }

    // 点击提交后，如果满足提交条件，则此时禁用提交按钮，以避免重复提交
    document.getElementById("submit").disabled = true;
    document.getElementById("submit").style.backgroundColor = "gray";
    document.getElementById("submit").textContent = "正在提交...";

    // console.log(answers);
    // return false;

    // 将答案组合为JSON后提交给后端接口，最终计算出总得分
    fetch("/exam/submit/"+videoid + "/" + token, {
        method: 'POST',
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(answers)
    }).then(response => response.text())
    .then(data => {
        if (data.includes("Token-Fail")) {
            alert("Token检验失败，请确认.");
        }
        else if (data.includes("Need-Login")) {
            alert("请先登录学生账号.");
        }
        else if (data.includes("Not-Student")) {
            alert("非学生角色无法提交试卷.")
        }
        else if (data.includes("Already-Exam")) {
            alert("你已经完成过本次考试，请勿重复操作.")
        }
        else {
            alert("你的本次得分为：" + data + " 分.");
        }
    })
    .catch(err => console.log('请求失败', err));
}
