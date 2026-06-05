/**
 * QuickForm 校园版：学生端收集页提交增强
 * - 含 <input type="file"> 时优先 multipart（json 字段 + file），避免把大文件 Base64 打进 JSON
 * - 解析 API 错误 JSON，展示 message（而非原始 \uXXXX 字符串）
 */
(function () {
    'use strict';

    var limits = {
        maxFileMb: 1,
        maxBodyMb: 10,
        maxJsonKb: 60,
        allowedExtensions: [],
    };

    function loadLimits() {
        var meta = document.querySelector('meta[name="qf-api-limits"]');
        if (!meta || !meta.content) return;
        try {
            var parsed = JSON.parse(meta.content);
            if (parsed && typeof parsed === 'object') {
                limits = Object.assign(limits, parsed);
            }
        } catch (e) {
            console.warn('[qf-api-submit] 无法解析 qf-api-limits', e);
        }
    }

    function apiPathMatch(url) {
        if (!url) return null;
        var s = String(url);
        try {
            var u = new URL(s, window.location.href);
            s = u.pathname || s;
        } catch (e) { /* keep s */ }
        var m = s.match(/\/api\/([A-Za-z0-9_-]+)\/?$/);
        return m ? m[1] : null;
    }

    function parseApiErrorMessage(text, status) {
        var fallback = '提交失败（HTTP ' + status + '）';
        if (!text || !String(text).trim()) return fallback;
        try {
            var data = JSON.parse(text);
            if (data && data.message) return String(data.message);
            if (data && data.error) return String(data.error);
        } catch (e) {
            if (text.length < 500) return text;
        }
        return fallback;
    }

    function showSubmitError(msg) {
        var box = document.getElementById('qf-submit-error');
        if (!box) {
            box = document.createElement('div');
            box.id = 'qf-submit-error';
            box.setAttribute('role', 'alert');
            box.style.cssText =
                'margin:12px 0;padding:12px 14px;border-radius:8px;background:#fef2f2;color:#991b1b;border:1px solid #fecaca;font-size:14px;line-height:1.5;';
            var anchor = document.querySelector('form') || document.body;
            if (anchor && anchor.parentNode) {
                anchor.parentNode.insertBefore(box, anchor);
            } else {
                document.body.appendChild(box);
            }
        }
        box.textContent = msg;
        box.style.display = 'block';
    }

    function hideSubmitError() {
        var box = document.getElementById('qf-submit-error');
        if (box) box.style.display = 'none';
    }

    function fileTooLarge(file) {
        var max = (limits.maxFileMb || 20) * 1024 * 1024;
        return file && file.size > max;
    }

    function collectFormFields(form, skipFileInputs) {
        var data = {};
        var els = form.querySelectorAll('input, select, textarea');
        els.forEach(function (el) {
            if (!el.name || el.disabled) return;
            if (skipFileInputs && el.type === 'file') return;
            if (el.type === 'file') return;
            if (el.type === 'checkbox') {
                if (el.checked) data[el.name] = el.value || true;
                return;
            }
            if (el.type === 'radio') {
                if (el.checked) data[el.name] = el.value;
                return;
            }
            data[el.name] = el.value;
        });
        return data;
    }

    function submitFormMultipart(form, actionUrl) {
        var files = form.querySelectorAll('input[type="file"]');
        var picked = [];
        files.forEach(function (inp) {
            if (!inp.files) return;
            for (var i = 0; i < inp.files.length; i++) {
                var f = inp.files[i];
                if (!f || !f.name) continue;
                if (fileTooLarge(f)) {
                    showSubmitError(
                        '文件「' +
                            f.name +
                            '」超过 ' +
                            (limits.maxFileMb || 20) +
                            'MB 限制，请压缩后重试或拆分为多次提交。'
                    );
                    return Promise.reject(new Error('file_too_large'));
                }
                picked.push(f);
            }
        });
        hideSubmitError();
        var fields = collectFormFields(form, true);
        // 附件路径由服务端写入，避免客户端重复拼接 /static/uploads/...
        delete fields.attachment;
        delete fields.attachments;
        var fd = new FormData();
        fd.append('json', JSON.stringify(fields));
        picked.forEach(function (f) {
            fd.append('file', f, f.name);
        });
        return fetch(actionUrl, { method: 'POST', body: fd, credentials: 'same-origin' })
            .then(function (res) {
                return res.text().then(function (text) {
                    if (!res.ok) {
                        throw new Error(parseApiErrorMessage(text, res.status));
                    }
                    try {
                        return text ? JSON.parse(text) : {};
                    } catch (e) {
                        return { raw: text };
                    }
                });
            });
    }

    function patchFetch() {
        if (!window.fetch || window.__qfFetchPatched) return;
        window.__qfFetchPatched = true;
        var orig = window.fetch.bind(window);
        window.fetch = function (input, init) {
            init = init || {};
            var url = typeof input === 'string' ? input : (input && input.url) || '';
            var method = ((init.method || 'GET') + '').toUpperCase();
            var taskId = apiPathMatch(url);
            if (!taskId || method !== 'POST') {
                return orig(input, init);
            }
            var body = init.body;
            if (typeof body === 'string' && body.length > (limits.maxJsonKb || 60) * 1024) {
                console.warn(
                    '[qf-api-submit] JSON 请求体约 ' +
                        Math.round(body.length / 1024) +
                        'KB，超过建议上限 ' +
                        (limits.maxJsonKb || 60) +
                        'KB；大文件请用 multipart 的 file 字段上传。'
                );
            }
            return orig(input, init).then(function (res) {
                if (res.ok) return res;
                return res.clone().text().then(function (text) {
                    var err = new Error(parseApiErrorMessage(text, res.status));
                    err.qfStatus = res.status;
                    err.qfRaw = text;
                    throw err;
                });
            });
        };
    }

    function bindForms() {
        document.addEventListener(
            'submit',
            function (e) {
                var form = e.target;
                if (!form || form.tagName !== 'FORM') return;
                if (form.dataset && form.dataset.qfSkipEnhance === '1') return;
                var action = form.getAttribute('action') || '';
                if (!apiPathMatch(action)) return;
                var fileInputs = form.querySelectorAll('input[type="file"]');
                var hasFile = false;
                fileInputs.forEach(function (inp) {
                    if (inp.files && inp.files.length) hasFile = true;
                });
                if (!hasFile) return;

                e.preventDefault();
                e.stopPropagation();
                var url = action;
                try {
                    url = new URL(action, window.location.href).href;
                } catch (err) { /* keep */ }

                submitFormMultipart(form, url)
                    .then(function () {
                        hideSubmitError();
                        var ok = document.getElementById('qf-submit-success');
                        if (ok) {
                            ok.style.display = 'block';
                        } else {
                            alert('提交成功');
                        }
                        if (form.dataset.qfResetOnSuccess === '1') {
                            form.reset();
                        }
                    })
                    .catch(function (err) {
                        if (err && err.message !== 'file_too_large') {
                            showSubmitError(err.message || '提交失败');
                        }
                    });
            },
            true
        );
    }

    function init() {
        loadLimits();
        patchFetch();
        bindForms();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
