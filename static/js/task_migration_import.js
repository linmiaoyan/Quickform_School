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
    (rows || []).forEach((t) => {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td>${escapeHtml(t.name || '')}</td>
        <td class="text-muted small">${escapeHtml(t.intro || '')}</td>
        <td><code>${escapeHtml(t.apiid || '')}</code></td>
        <td class="text-end">
          <button class="btn btn-primary btn-sm qf-import-one" data-apiid="${escapeAttr(t.apiid || '')}">导入</button>
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

  async function onFetchAll() {
    const btn = qs('#qf-fetch-all-btn');
    setLoading(btn, true);
    try {
      const username = (qs('#qf-online-username')?.value || '').trim();
      const password = qs('#qf-online-password')?.value || '';
      const baseUrl = (qs('#qf-online-base-url')?.value || '').trim();
      const endpoint = window.QF_TASK_MIGRATION?.endpoints?.listAll || '/task/migration/online/list';
      const data = await postJson(endpoint, { username, password, base_url: baseUrl });
      renderTasks(data.tasks || []);
      qs('#qf-task-list-wrap')?.classList.remove('d-none');
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
      const username = (qs('#qf-online-username')?.value || '').trim();
      const password = qs('#qf-online-password')?.value || '';
      const baseUrl = (qs('#qf-online-base-url')?.value || '').trim();
      const apiUrl = (qs('#qf-online-api-url')?.value || '').trim();
      const endpoint = window.QF_TASK_MIGRATION?.endpoints?.listOne || '/task/migration/online/resolve';
      const data = await postJson(endpoint, { username, password, base_url: baseUrl, api_url: apiUrl });
      if (data.apiid) {
        await importByApiId(data.apiid);
      }
    } catch (e) {
      alert(e.message || '获取失败');
    } finally {
      setLoading(btn, false);
    }
  }

  async function importByApiId(apiid) {
    const username = (qs('#qf-online-username')?.value || '').trim();
    const password = qs('#qf-online-password')?.value || '';
    const baseUrl = (qs('#qf-online-base-url')?.value || '').trim();
    const btn = qs(`#qf-online-task-tbody .qf-import-one[data-apiid="${CSS.escape(apiid)}"]`);
    setLoading(btn, true);
    try {
      const endpoint = window.QF_TASK_MIGRATION?.endpoints?.importOne || '/task/migration/online/import';
      const data = await postJson(endpoint, { username, password, base_url: baseUrl, apiid });
      if (data.redirect) {
        window.location.href = data.redirect;
        return;
      }
      alert('导入成功');
    } catch (e) {
      alert(e.message || '导入失败');
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
      if (!apiid) return;
      importByApiId(apiid);
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bind);
  else bind();
})();

