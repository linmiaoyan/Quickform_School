(function (global) {
  'use strict';

  function escapeHtml(s) {
    return String(s || '').replace(/[&<>"']/g, function (c) {
      return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c];
    });
  }

  function isCertificationRequired(data) {
    if (!data || typeof data !== 'object') return false;
    return data.code === 'certification_required' || data.is_certified === false;
  }

  function formatOnlineCliErrorHtml(data) {
    if (!data) return escapeHtml('请求失败');
    if (isCertificationRequired(data)) {
      var url = data.certification_url || 'https://quickform.cn/certification/request';
      var html = '<div class="fw-semibold mb-1">需完成教师认证</div>';
      html += '<div class="mb-2">' + escapeHtml(data.message || '该账号尚未完成教师认证，无法连接在线版。') + '</div>';
      if (data.hint) {
        html += '<div class="text-muted small mb-2">' + escapeHtml(data.hint) + '</div>';
      }
      html += '<a class="btn btn-sm btn-warning" href="' + escapeHtml(url) + '" target="_blank" rel="noopener">前往 quickform.cn 提交教师认证</a>';
      return html;
    }
    var msg = data.message || '请求失败';
    if (data.hint) {
      msg += '\n' + data.hint;
    }
    return escapeHtml(msg).replace(/\n/g, '<br>');
  }

  function formatOnlineCliErrorText(data) {
    if (!data) return '请求失败';
    var parts = [data.message || '请求失败'];
    if (data.hint) parts.push(data.hint);
    if (isCertificationRequired(data)) {
      parts.push('认证入口：' + (data.certification_url || 'https://quickform.cn/certification/request'));
    }
    return parts.filter(Boolean).join('\n');
  }

  function showOnlineCliErrorIn(el, data, options) {
    if (!el) return;
    options = options || {};
    el.innerHTML = formatOnlineCliErrorHtml(data);
    el.classList.remove('d-none');
    el.style.display = 'block';
    if (isCertificationRequired(data)) {
      el.classList.remove('alert-danger');
      el.classList.add('alert-warning');
    } else if (options.danger !== false) {
      el.classList.remove('alert-warning');
      el.classList.add('alert-danger');
    }
  }

  function alertOnlineCliError(data) {
    if (isCertificationRequired(data)) {
      var wrap = document.createElement('div');
      wrap.innerHTML = formatOnlineCliErrorHtml(data);
      var modal = document.createElement('div');
      modal.className = 'modal fade show';
      modal.style.display = 'block';
      modal.style.backgroundColor = 'rgba(0,0,0,0.5)';
      modal.innerHTML =
        '<div class="modal-dialog modal-dialog-centered">' +
        '<div class="modal-content">' +
        '<div class="modal-header"><h5 class="modal-title">需完成教师认证</h5>' +
        '<button type="button" class="btn-close" onclick="this.closest(\'.modal\').remove()"></button></div>' +
        '<div class="modal-body">' + wrap.innerHTML + '</div>' +
        '<div class="modal-footer"><button type="button" class="btn btn-secondary" onclick="this.closest(\'.modal\').remove()">关闭</button></div>' +
        '</div></div>';
      document.body.appendChild(modal);
      return;
    }
    alert(formatOnlineCliErrorText(data));
  }

  global.OnlineCliErrors = {
    isCertificationRequired: isCertificationRequired,
    formatOnlineCliErrorHtml: formatOnlineCliErrorHtml,
    formatOnlineCliErrorText: formatOnlineCliErrorText,
    showOnlineCliErrorIn: showOnlineCliErrorIn,
    alertOnlineCliError: alertOnlineCliError,
  };
})(window);
