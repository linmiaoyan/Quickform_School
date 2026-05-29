/**
 * 任务 HTML 在线编辑：查找/替换、智能替换 API 地址、保存到服务端。
 */
(function () {
    'use strict';

    var cfg = window.QF_HTML_EDITOR || {};
    var modalEl = document.getElementById('qfHtmlEditorModal');
    if (!modalEl || !cfg.contentUrl) return;

    var textarea = document.getElementById('qfHtmlEditorTextarea');
    var fileLabel = document.getElementById('qfHtmlEditorFileLabel');
    var apiUrlEl = document.getElementById('qfHtmlEditorApiUrl');
    var statusEl = document.getElementById('qfHtmlEditorStatus');
    var findInput = document.getElementById('qfHtmlFindInput');
    var replaceInput = document.getElementById('qfHtmlReplaceInput');
    var currentSavedName = '';
    var taskApiId = '';
    var apiSubmitUrl = '';
    var lastFindIndex = 0;
    var modal = window.bootstrap && bootstrap.Modal ? bootstrap.Modal.getOrCreateInstance(modalEl) : null;

    function setStatus(msg, isError) {
        if (!statusEl) return;
        statusEl.textContent = msg || '';
        statusEl.className = 'small me-auto ' + (isError ? 'text-danger' : 'text-muted');
    }

    function openEditor(savedName, originalName) {
        currentSavedName = savedName || '';
        lastFindIndex = 0;
        if (fileLabel) {
            fileLabel.textContent = '正在加载：' + (originalName || savedName) + ' …';
        }
        if (textarea) textarea.value = '';
        setStatus('');
        if (modal) modal.show();

        var url = cfg.contentUrl + (cfg.contentUrl.indexOf('?') >= 0 ? '&' : '?') + 'saved_name=' + encodeURIComponent(savedName);
        fetch(url, { credentials: 'same-origin', headers: { Accept: 'application/json' } })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (!data.success) {
                    setStatus(data.message || '加载失败', true);
                    return;
                }
                taskApiId = data.task_api_id || '';
                apiSubmitUrl = data.api_submit_url || '';
                if (textarea) textarea.value = data.content || '';
                if (fileLabel) {
                    fileLabel.textContent = '编辑文件：' + (data.original_name || savedName);
                }
                if (apiUrlEl) apiUrlEl.textContent = apiSubmitUrl;
            })
            .catch(function (e) {
                setStatus('加载失败：' + e.message, true);
            });
    }

    document.querySelectorAll('.qf-html-edit-btn').forEach(function (btn) {
        btn.addEventListener('click', function () {
            openEditor(btn.getAttribute('data-saved-name'), btn.getAttribute('data-original-name'));
        });
    });

    function findNext() {
        if (!textarea || !findInput) return;
        var needle = findInput.value;
        if (!needle) {
            setStatus('请输入要查找的内容', true);
            return;
        }
        var hay = textarea.value;
        var idx = hay.indexOf(needle, lastFindIndex);
        if (idx < 0) {
            idx = hay.indexOf(needle, 0);
            lastFindIndex = 0;
        }
        if (idx < 0) {
            setStatus('未找到', true);
            return;
        }
        textarea.focus();
        textarea.setSelectionRange(idx, idx + needle.length);
        lastFindIndex = idx + needle.length;
        setStatus('已定位');
    }

    function replaceAll() {
        if (!textarea || !findInput) return;
        var needle = findInput.value;
        if (!needle) {
            setStatus('请输入要查找的内容', true);
            return;
        }
        var rep = replaceInput ? replaceInput.value : '';
        var parts = textarea.value.split(needle);
        var count = parts.length - 1;
        if (count <= 0) {
            setStatus('未找到可替换内容', true);
            return;
        }
        textarea.value = parts.join(rep);
        setStatus('已替换 ' + count + ' 处');
    }

    function smartReplaceApi() {
        if (!textarea) return;
        var text = textarea.value;
        var base = apiSubmitUrl || '';
        var apiId = taskApiId || '';
        if (!apiId) {
            setStatus('缺少任务 API ID', true);
            return;
        }
        var n = 0;
        var reFull = /https?:\/\/[^/\s"'<>]+?\/api\/[0-9a-zA-Z]{6,64}/gi;
        text = text.replace(reFull, function () {
            n++;
            return base;
        });
        var rePath = /\/api\/[0-9a-zA-Z]{6,64}/g;
        text = text.replace(rePath, function (m) {
            if (m === '/api/' + apiId) return m;
            n++;
            return '/api/' + apiId;
        });
        textarea.value = text;
        setStatus(n > 0 ? ('已智能替换 ' + n + ' 处 API 地址') : '未发现可替换的 API 地址');
    }

    function saveContent() {
        if (!textarea || !currentSavedName) return;
        var btn = document.getElementById('qfHtmlEditorSaveBtn');
        if (btn) btn.disabled = true;
        setStatus('保存中…');
        fetch(cfg.saveUrl, {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
            body: JSON.stringify({ saved_name: currentSavedName, content: textarea.value }),
        })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data.success) {
                    setStatus('已保存');
                    if (modal) modal.hide();
                    if (window.showToast) window.showToast('success', 'HTML 已保存');
                } else {
                    setStatus(data.message || '保存失败', true);
                }
            })
            .catch(function (e) {
                setStatus('保存失败：' + e.message, true);
            })
            .finally(function () {
                if (btn) btn.disabled = false;
            });
    }

    var findBtn = document.getElementById('qfHtmlFindNextBtn');
    var replaceBtn = document.getElementById('qfHtmlReplaceAllBtn');
    var smartBtn = document.getElementById('qfHtmlSmartApiBtn');
    var saveBtn = document.getElementById('qfHtmlEditorSaveBtn');
    if (findBtn) findBtn.addEventListener('click', findNext);
    if (replaceBtn) replaceBtn.addEventListener('click', replaceAll);
    if (smartBtn) smartBtn.addEventListener('click', smartReplaceApi);
    if (saveBtn) saveBtn.addEventListener('click', saveContent);
})();
