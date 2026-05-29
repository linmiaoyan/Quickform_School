(() => {
  function qs(sel) {
    return document.querySelector(sel);
  }

  function setLoading(btn, loading) {
    if (!btn) return;
    btn.disabled = !!loading;
    btn.dataset._origText = btn.dataset._origText || btn.innerHTML;
    btn.innerHTML = loading ? '处理中…' : btn.dataset._origText;
  }

  function credentialsPayload() {
    return {
      username: (qs('#qf-online-username')?.value || '').trim(),
      password: qs('#qf-online-password')?.value || '',
      auth_code: (qs('#qf-online-auth-code')?.value || '').trim(),
      base_url: (qs('#qf-online-base-url')?.value || '').trim(),
    };
  }

  async function postJson(url, payload) {
    const r = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify(payload || {}),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok || data.success === false) {
      const msg = data.message || `请求失败（${r.status}）`;
      const err = new Error(msg);
      err.data = data;
      throw err;
    }
    return data;
  }

  function renderTasks(rows) {
    const tbody = qs('#qf-online-task-tbody');
    if (!tbody) return;
    tbody.innerHTML = '';
    if (!rows || !rows.length) {
      tbody.innerHTML = '<tr><td colspan="4" class="text-muted small">未获取到任务。</td></tr>';
      return;
    }
    rows.forEach((t) => {
      const tr = document.createElement('tr');
      const name = t.name || t.title || '';
      tr.innerHTML = `
        <td>${escapeHtml(name)}</td>
        <td class="text-muted small">${escapeHtml(t.intro || t.description || '')}</td>
        <td><code>${escapeHtml(t.apiid || '')}</code></td>
        <td class="text-end">
          <button class="btn btn-primary btn-sm qf-import-one" data-apiid="${escapeAttr(t.apiid || '')}" data-name="${escapeAttr(name)}">导入</button>
        </td>
      `;
      tbody.appendChild(tr);
    });
  }

  function escapeHtml(s) {
    return String(s || '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }
  function escapeAttr(s) {
    return escapeHtml(s).replace(/`/g, '&#96;');
  }

  async function importByApiId(apiid, taskName) {
    const cred = credentialsPayload();
    const btn = qs(`#qf-online-task-tbody .qf-import-one[data-apiid="${CSS.escape(apiid)}"]`);
    setLoading(btn, true);
    try {
      const endpoint = window.QF_TASK_MIGRATION?.endpoints?.importOne || '/task/migration/online/import';
      const data = await postJson(endpoint, { ...cred, apiid });
      const label = taskName || data.new_apiid || apiid;
      const wantData = window.confirm(
        `任务「${label}」已导入成功！是否同时从在线版导入该任务的历史提交数据？`
      );
      if (wantData && data.task_id && (data.original_apiid || data.old_apiid)) {
        const importDataUrl = window.QF_TASK_MIGRATION?.endpoints?.importData || '/api/qf/import_data';
        try {
          const dr = await postJson(importDataUrl, {
            task_id: data.task_id,
            apiid: data.original_apiid || data.old_apiid,
            base_url: cred.base_url,
          });
          alert(dr.message || `已导入 ${dr.count || 0} 条数据`);
        } catch (e) {
          alert('任务已导入，但数据导入失败：' + (e.message || e));
        }
      }
      if (data.redirect) {
        window.location.href = data.redirect;
        return;
      }
      alert(data.message || '导入成功');
    } catch (e) {
      alert(e.message || '导入失败');
    } finally {
      setLoading(btn, false);
    }
  }

  async function onFetchAll() {
    const btn = qs('#qf-fetch-all-btn');
    setLoading(btn, true);
    try {
      const endpoint = window.QF_TASK_MIGRATION?.endpoints?.listAll || '/task/migration/online/list';
      const data = await postJson(endpoint, credentialsPayload());
      renderTasks(data.tasks || []);
    } catch (e) {
      alert(e.message || '获取失败');
    } finally {
      setLoading(btn, false);
    }
  }

  async function onFetchOne() {
    const btn = qs('#qf-fetch-one-btn');
    setLoading(btn, true);
    try {
      const apiUrl = (qs('#qf-online-api-url')?.value || '').trim();
      const endpoint = window.QF_TASK_MIGRATION?.endpoints?.resolveOne || '/task/migration/online/resolve';
      const data = await postJson(endpoint, { ...credentialsPayload(), api_url: apiUrl });
      if (data.apiid) {
        await importByApiId(data.apiid, '');
      }
    } catch (e) {
      alert(e.message || '获取失败');
    } finally {
      setLoading(btn, false);
    }
  }

  function bind() {
    qs('#qf-fetch-all-btn')?.addEventListener('click', onFetchAll);
    qs('#qf-fetch-one-btn')?.addEventListener('click', onFetchOne);
    qs('#qf-online-task-tbody')?.addEventListener('click', (ev) => {
      const btn = ev.target?.closest?.('.qf-import-one');
      if (!btn) return;
      const apiid = btn.getAttribute('data-apiid') || '';
      const name = btn.getAttribute('data-name') || '';
      if (apiid) importByApiId(apiid, name);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bind);
  } else {
    bind();
  }
})();
