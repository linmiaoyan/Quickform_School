(function () {
    'use strict';

    var cfg = window.QF_NOTICE_CONFIG || {};
    if (!cfg.listUrl) return;

    var badge = document.getElementById('qf-notice-badge');
    var listEl = document.getElementById('qf-notice-list');
    var emptyEl = document.getElementById('qf-notice-empty');
    var markAllBtn = document.getElementById('qf-notice-mark-all');

    function setBadge(n) {
        if (!badge) return;
        var c = parseInt(n, 10) || 0;
        badge.textContent = c > 99 ? '99+' : String(c);
        badge.style.display = c > 0 ? 'inline-block' : 'none';
    }

    function kindLabel(kind) {
        return kind === 'announcement' ? '公告' : '通知';
    }

    function renderList(items) {
        if (!listEl) return;
        listEl.innerHTML = '';
        if (!items || !items.length) {
            if (emptyEl) emptyEl.style.display = 'block';
            return;
        }
        if (emptyEl) emptyEl.style.display = 'none';
        items.forEach(function (item) {
            var li = document.createElement('li');
            li.className = 'qf-notice-item' + (item.is_read ? '' : ' qf-notice-item--unread');
            li.dataset.id = item.id;
            var meta = document.createElement('div');
            meta.className = 'qf-notice-item-meta';
            meta.innerHTML =
                '<span class="badge ' + (item.kind === 'announcement' ? 'bg-primary' : 'bg-secondary') + ' me-1">' +
                kindLabel(item.kind) + '</span>' +
                '<span class="text-muted small">' + (item.created_at || '') + '</span>';
            var title = document.createElement('div');
            title.className = 'qf-notice-item-title';
            title.textContent = item.title || '';
            var body = document.createElement('div');
            body.className = 'qf-notice-item-body';
            body.textContent = item.body || '';
            li.appendChild(meta);
            li.appendChild(title);
            li.appendChild(body);
            li.addEventListener('click', function () {
                markRead(item.id, li);
            });
            listEl.appendChild(li);
        });
    }

    function fetchNotices() {
        return fetch(cfg.listUrl, {
            credentials: 'same-origin',
            headers: { Accept: 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
        })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (!data || !data.success) return;
                setBadge(data.unread_count);
                renderList(data.notices || []);
            })
            .catch(function () {});
    }

    function markRead(id, li) {
        if (!cfg.readUrlTemplate || !id) return;
        var url = cfg.readUrlTemplate.replace('__ID__', String(id));
        fetch(url, {
            method: 'POST',
            credentials: 'same-origin',
            headers: {
                Accept: 'application/json',
                'X-Requested-With': 'XMLHttpRequest',
                'Content-Type': 'application/json',
            },
            body: '{}',
        })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data && data.success) {
                    if (li) li.classList.remove('qf-notice-item--unread');
                    setBadge(data.unread_count);
                }
            })
            .catch(function () {});
    }

    if (markAllBtn && cfg.readAllUrl) {
        markAllBtn.addEventListener('click', function (e) {
            e.preventDefault();
            e.stopPropagation();
            fetch(cfg.readAllUrl, {
                method: 'POST',
                credentials: 'same-origin',
                headers: {
                    Accept: 'application/json',
                    'X-Requested-With': 'XMLHttpRequest',
                    'Content-Type': 'application/json',
                },
                body: '{}',
            })
                .then(function (r) { return r.json(); })
                .then(function (data) {
                    if (data && data.success) {
                        setBadge(0);
                        document.querySelectorAll('.qf-notice-item--unread').forEach(function (el) {
                            el.classList.remove('qf-notice-item--unread');
                        });
                    }
                })
                .catch(function () {});
        });
    }

    fetchNotices();
    var toggle = document.getElementById('qfNoticeToggle');
    if (toggle) {
        toggle.addEventListener('shown.bs.dropdown', fetchNotices);
    }
})();
