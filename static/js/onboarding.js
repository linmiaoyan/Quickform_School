/* global introJs */

(function () {
  'use strict';

  // 防重复加载：某些页面可能重复引入 onboarding.js，导致弹窗/引导执行两次
  try {
    if (window.__qfOnboardingLoaded) return;
    window.__qfOnboardingLoaded = true;
  } catch (e0) {}

  var STORAGE_KEY = 'quickform_onboarding';
  var COMPLETED_KEY = 'quickform_onboarding_completed_v1';
  var _intentModalInFlight = false;

  function safeJsonParse(s, fallback) {
    try { return JSON.parse(s); } catch (e) { return fallback; }
  }

  function getState() {
    return safeJsonParse(localStorage.getItem(STORAGE_KEY) || '', null) || {};
  }

  function setState(patch) {
    var cur = getState();
    var next = Object.assign({}, cur, patch || {});
    localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
    return next;
  }

  function clearState() {
    try { localStorage.removeItem(STORAGE_KEY); } catch (e) {}
  }

  function markCompleted() {
    try { localStorage.setItem(COMPLETED_KEY, '1'); } catch (e) {}
  }

  function isCompleted() {
    try { return localStorage.getItem(COMPLETED_KEY) === '1'; } catch (e) { return false; }
  }

  function qs(sel) { return document.querySelector(sel); }

  function currentUrl() { return new URL(window.location.href); }

  function hasOnboardQuery() {
    var u = currentUrl();
    return u.searchParams.get('onboard') === '1';
  }

  function ensureOnboardQuery(urlStr) {
    try {
      var u = new URL(urlStr, window.location.origin);
      u.searchParams.set('onboard', '1');
      if (!u.searchParams.get('flow')) u.searchParams.set('flow', 'quickStart_v1');
      return u.toString();
    } catch (e) {
      return urlStr;
    }
  }

  function navigate(urlStr) {
    // 标记为“引导内跳转”，避免 intro.onexit 被误判为用户跳过/退出
    setState({ navigating: true, active: true, flowId: 'quickStart_v1' });
    window.location.assign(ensureOnboardQuery(urlStr));
  }

  function clearNavigatingFlag() {
    try {
      var st = getState();
      if (st && (st.navigating || st.suppressExit)) setState({ navigating: false, suppressExit: false });
    } catch (e) {}
  }

  function copyText(text) {
    if (!text) return Promise.resolve(false);
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text).then(function () { return true; }).catch(function () { return false; });
    }
    try {
      var ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', 'readonly');
      ta.style.position = 'fixed';
      ta.style.left = '-9999px';
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
      return Promise.resolve(true);
    } catch (e) {
      return Promise.resolve(false);
    }
  }

  function startIntro(steps, opts) {
    if (typeof introJs !== 'function') return;
    var intro = introJs();
    try { document.body && document.body.classList && document.body.classList.add('qf-onboarding-active'); } catch (e0) {}
    intro.setOptions(Object.assign({
      steps: steps,
      showBullets: false,
      showProgress: true,
      exitOnEsc: true,
      exitOnOverlayClick: false,
      // 关闭键盘导航：避免输入框/文本域打字时被 Intro.js 抢走按键导致焦点抖动
      keyboardNavigation: false,
      nextLabel: '下一步',
      prevLabel: '上一步',
      skipLabel: '跳过',
      doneLabel: '完成',
      scrollToElement: true,
      scrollPadding: 80,
      disableInteraction: false
    }, (opts || {})));

    intro.onexit(function () {
      // 只有“用户主动退出/跳过”才标记完成；引导内跨页跳转不应触发完成
      try {
        var st = getState();
        if (st && (st.navigating || st.suppressExit)) return;
      } catch (e) {}
      try { document.body && document.body.classList && document.body.classList.remove('qf-onboarding-active'); } catch (e0) {}
      clearState();
      markCompleted();
    });
    intro.oncomplete(function () {
      try { document.body && document.body.classList && document.body.classList.remove('qf-onboarding-active'); } catch (e0) {}
      clearState();
      markCompleted();
    });

    // 容错：元素不存在时，跳过该步，避免卡死
    intro.onbeforechange(function () {
      try {
        var step = intro._introItems && intro._introItems[intro._currentStep];
        var el = step && step.element;
        if (el && el !== document.body && !document.body.contains(el)) {
          setTimeout(function () { try { intro.nextStep(); } catch (e) {} }, 0);
        }
      } catch (e) {}
    });

    intro.start();
    return intro;
  }

  function buildSuggestedPrompt(apiEndpoint, userIntentPrompt) {
    var base = (userIntentPrompt || '').trim();
    var parts = [];
    if (base) parts.push(base);
    parts.push('请生成一个单页 HTML 表单页面（手机端好用、样式简洁）。');
    parts.push('创建完页面后，把表单数据以 POST 方式提交到：' + apiEndpoint);
    return parts.join('\n');
  }

  function startFlowFromAnywhere(entry) {
    setState({
      active: true,
      flowId: 'quickStart_v1',
      entry: entry || '',
      userIntentPrompt: ''
    });
    navigate('/dashboard');
  }

  function bindStartButtons() {
    document.querySelectorAll('[data-onboard-start]').forEach(function (btn) {
      if (btn.dataset.boundOnboard) return;
      btn.dataset.boundOnboard = '1';
      btn.addEventListener('click', function (e) {
        e.preventDefault();
        startFlowFromAnywhere(btn.getAttribute('data-onboard-start') || '');
      });
    });
  }

  function maybeStartDashboardTour() {
    var st = getState();
    if (!st.active && !hasOnboardQuery()) return;
    if (!qs('#qf-onboard-create-task-btn')) return;

    // 只保留“创建任务”步骤：不在一开始询问用户需求；需求提示词在创建任务后（任务详情页）再输入
    var steps = [
      {
        title: '新手引导',
        intro: '接下来我们用几步创建一个数据任务，并把它接到 AI 生成的网页里。你可以随时点“跳过”。',
        element: document.body,
        position: 'floating'
      },
      {
        title: '创建一个新任务',
        intro: '点这里创建任务。创建成功后，我们会在任务详情页再让你输入需求提示词。',
        element: qs('#qf-onboard-create-task-btn'),
        position: 'bottom'
      }
    ];

    var intro = startIntro(steps, { exitOnOverlayClick: false });
    if (!intro) return;

    // 不再在 tooltip 内输入，避免焦点/退出导致引导中断

    // 让用户在第 2 步真的点击按钮时也能继续（不强制）
    var createBtn = qs('#qf-onboard-create-task-btn');
    if (createBtn && !createBtn.dataset.boundNav) {
      createBtn.dataset.boundNav = '1';
      createBtn.addEventListener('click', function (e) {
        // 引导模式：点击“创建任务”直接进入创建页；提示词留到任务详情页再填
        var st2 = getState();
        var inFlow = !!(st2 && (st2.active || hasOnboardQuery()));
        if (!inFlow) return;
        if (e) { e.preventDefault(); }
        try {
          setState({ active: true, flowId: 'quickStart_v1', userIntentPrompt: '' });
          navigate('/create_task');
        } catch (err) {
          // 兜底：直接跳转
          setState({ active: true, flowId: 'quickStart_v1' });
          navigate('/create_task');
        }
      });
    }
  }

  function maybeStartCreateTaskTour() {
    var st = getState();
    if (!st.active && !hasOnboardQuery()) return;
    if (!qs('#qf-create-task-form')) return;

    // 提交创建任务时属于“引导内跳转”：避免 intro.onexit 清空状态
    try {
      var form = qs('#qf-create-task-form');
      if (form && !form.dataset.boundOnboardSubmit) {
        form.dataset.boundOnboardSubmit = '1';
        form.addEventListener('submit', function () {
          try {
            setState({ active: true, flowId: 'quickStart_v1', navigating: true, suppressExit: true });
          } catch (e0) {}
        });
      }
    } catch (e) {}

    var steps = [
      {
        title: '填写任务信息',
        intro: '给这次收集起个名字，便于后续管理。',
        element: qs('#qf-create-task-title'),
        position: 'bottom'
      },
      {
        title: '补充说明（可选）',
        intro: '可以写清楚要收集哪些字段、给谁填、什么时候截止等。',
        element: qs('#qf-create-task-description'),
        position: 'bottom'
      },
      {
        title: '提交创建',
        intro: '创建成功后会进入任务详情页，我们会自动帮你生成“建议提示词”并复制。',
        element: qs('#qf-create-task-submit'),
        position: 'top'
      }
    ];

    // 创建任务页包含输入框：关闭自动滚动，减少重排导致的“抢控制权”
    startIntro(steps, { scrollToElement: false });
  }

  function maybeStartTaskDetailTour() {
    var st = getState();
    if (!st.active && !hasOnboardQuery()) return;
    if (!qs('#qf-onboard-task-api-endpoint')) return;

    var apiEndpoint = (qs('#qf-onboard-task-api-endpoint').textContent || '').trim();
    if (!apiEndpoint) return;

    function _ensureUserIntent(cb){
      var st0 = getState();
      if (st0 && (st0.userIntentPrompt || '').trim()) return cb((st0.userIntentPrompt || '').trim());
      if (_intentModalInFlight) return;
      _intentModalInFlight = true;
      // 弹窗收集：避免在 tooltip 内输入导致闪烁（Bootstrap 不可用时也要能正常工作）
      var existing = qs('#qfOnboardIntentAfterTaskModal');
      if (existing) existing.parentNode.removeChild(existing);
      var wrap = document.createElement('div');
      wrap.innerHTML =
        '<div class="modal fade" id="qfOnboardIntentAfterTaskModal" tabindex="-1" aria-hidden="true">' +
        '  <div class="modal-dialog modal-dialog-centered" style="max-width: 720px;">' +
        '    <div class="modal-content">' +
        '      <div class="modal-header">' +
        '        <h5 class="modal-title">你要生成什么页面？（必填）</h5>' +
        '      </div>' +
        '      <div class="modal-body">' +
        '        <div class="text-muted small mb-2">例如：我要生成一个xx学科的xx任务，用于xx。</div>' +
        '        <textarea class="form-control" id="qfOnboardIntentAfterTaskTextarea" rows="4" placeholder="请输入你的需求…"></textarea>' +
        '        <div class="invalid-feedback d-block mt-2" id="qfOnboardIntentAfterTaskError" style="display:none;">此项必填，请先输入你的需求。</div>' +
        '      </div>' +
        '      <div class="modal-footer">' +
        '        <button type="button" class="btn btn-primary" id="qfOnboardIntentAfterTaskOk">确定</button>' +
        '      </div>' +
        '    </div>' +
        '  </div>' +
        '</div>';
      document.body.appendChild(wrap.firstElementChild);

      var modalEl = qs('#qfOnboardIntentAfterTaskModal');
      var ta = qs('#qfOnboardIntentAfterTaskTextarea');
      var errEl = qs('#qfOnboardIntentAfterTaskError');
      var okBtn = qs('#qfOnboardIntentAfterTaskOk');
      var proceeded = false;

      function _finalize(val) {
        if (proceeded) return;
        proceeded = true;
        _intentModalInFlight = false;
        setState({ userIntentPrompt: val || '', active: true, flowId: 'quickStart_v1' });
        cb(((val || '').trim()));
      }

      // 1) Bootstrap modal 优先
      var canBootstrap = !!(window.bootstrap && bootstrap.Modal);
      if (canBootstrap) {
        try {
          var m = new bootstrap.Modal(modalEl, { backdrop: 'static', keyboard: false });
          m.show();
          // 某些情况下会被页面其他层抢走点击/焦点，这里强制保证可输入
          setTimeout(function(){ try { if (ta){ ta.removeAttribute('disabled'); ta.removeAttribute('readonly'); ta.style.pointerEvents='auto'; ta.focus(); } } catch (e2) {} }, 80);
          setTimeout(function(){ try { if (ta){ ta.focus(); } } catch (e3) {} }, 280);
          if (okBtn && !okBtn.dataset.bound) {
            okBtn.dataset.bound = '1';
            okBtn.addEventListener('click', function () {
              var val = (ta && ta.value) ? (ta.value || '') : '';
              if (!(val || '').trim()) {
                try {
                  if (errEl) errEl.style.display = 'block';
                  ta && ta.classList && ta.classList.add('is-invalid');
                  ta && ta.focus && ta.focus();
                } catch (e0) {}
                return;
              }
              try { if (errEl) errEl.style.display = 'none'; } catch (e1) {}
              try { ta && ta.classList && ta.classList.remove('is-invalid'); } catch (e2) {}
              try { m.hide(); } catch (e3) {}
              _finalize(val);
            });
          }
          return;
        } catch (e) {
          // 如果 Bootstrap 初始化失败，继续走自建弹窗兜底
        }
      }

      // 2) 无 Bootstrap：自建轻量弹窗（不依赖任何库）
      try {
        modalEl.classList.add('show');
        modalEl.style.display = 'block';
        modalEl.style.background = 'rgba(15,23,42,0.55)';
        modalEl.style.position = 'fixed';
        modalEl.style.inset = '0';
        modalEl.style.zIndex = '2000';
        modalEl.style.pointerEvents = 'auto';
        var dialog = modalEl.querySelector('.modal-dialog');
        if (dialog) dialog.style.margin = '1.75rem auto';
        setTimeout(function(){ try { if (ta){ ta.removeAttribute('disabled'); ta.removeAttribute('readonly'); ta.style.pointerEvents='auto'; ta.focus(); } } catch (e6) {} }, 30);
        setTimeout(function(){ try { if (ta){ ta.focus(); } } catch (e7) {} }, 220);

        function _closeLite(val) {
          try { modalEl.style.display = 'none'; } catch (e7) {}
          _finalize(val);
        }

        if (okBtn) {
          okBtn.addEventListener('click', function () {
            var val = (ta && ta.value) ? (ta.value || '') : '';
            if (!(val || '').trim()) {
              try {
                if (errEl) errEl.style.display = 'block';
                ta && ta.classList && ta.classList.add('is-invalid');
                ta && ta.focus && ta.focus();
              } catch (e0) {}
              return;
            }
            try { if (errEl) errEl.style.display = 'none'; } catch (e1) {}
            try { ta && ta.classList && ta.classList.remove('is-invalid'); } catch (e2) {}
            _closeLite(val);
          }, { once: true });
        }
        // 无 Bootstrap 模式：不允许点击遮罩关闭（必填）
      } catch (e2) {
        _intentModalInFlight = false;
        _finalize('');
      }
    }

    _ensureUserIntent(function(userIntent){
      var suggested = buildSuggestedPrompt(apiEndpoint, userIntent || '');

      var steps = [
      {
        title: '复制建议提示词',
        intro:
          '<div class="small text-muted mb-2">我们已把你的需求 + 提交地址拼成建议提示词。</div>' +
          '<textarea class="form-control" id="qf-onboard-suggested" rows="7" readonly></textarea>' +
          '<div class="d-flex justify-content-end gap-2 mt-2">' +
          '<button type="button" class="btn btn-sm btn-primary" id="qf-onboard-copy-suggested">已自动复制，我知道了</button>' +
          '</div>',
        element: qs('#qf-onboard-task-api-endpoint'),
        position: 'bottom'
      },
      {
        title: '选择一个 AI 助手生成网页',
        intro:
          '<div class="small text-muted mb-2">直接选择一个 AI，把刚才的提示词粘贴进去生成 HTML，然后下载 HTML 文件。</div>' +
          '<div class="d-flex flex-wrap gap-2">' +
          '<a class="btn btn-sm btn-outline-primary" target="_blank" rel="noopener noreferrer" href="https://www.doubao.com/">豆包</a>' +
          '<a class="btn btn-sm btn-outline-primary" target="_blank" rel="noopener noreferrer" href="https://tongyi.aliyun.com/">通义</a>' +
          '<a class="btn btn-sm btn-outline-primary" target="_blank" rel="noopener noreferrer" href="https://kimi.moonshot.cn/">Kimi</a>' +
          '<a class="btn btn-sm btn-outline-primary" target="_blank" rel="noopener noreferrer" href="https://yiyan.baidu.com/">文心</a>' +
          '<a class="btn btn-sm btn-outline-primary" target="_blank" rel="noopener noreferrer" href="https://xinghuo.xfyun.cn/">讯飞星火</a>' +
          '<a class="btn btn-sm btn-outline-primary" target="_blank" rel="noopener noreferrer" href="https://yuanbao.tencent.com/">腾讯元宝</a>' +
          '<a class="btn btn-sm btn-outline-primary" target="_blank" rel="noopener noreferrer" href="https://www.coze.cn/">扣子</a>' +
          '</div>',
        element: qs('#qf-onboard-go-ai') || qs('#qf-onboard-task-api-endpoint') || document.body,
        position: 'bottom'
      },
      {
        title: '上传 HTML',
        intro: '下载好 HTML 后，点这里上传到任务里。',
        element: qs('#qf-onboard-upload-html'),
        position: 'bottom'
      },
      {
        title: '完成后会生成二维码',
        intro: '上传通过审核后，你会在“已上传文件”旁看到二维码按钮，扫码即可打开页面。',
        element: qs('#qf-onboard-qrcode-area') || document.body,
        position: 'bottom'
      },
      {
        title: '数据分析与数据大屏',
        intro: '接下来我们进入“数据分析”页面，带你快速生成一个实时数据大屏（数据看板）。',
        element: qs('#qf-onboard-open-smart-analyze') || document.body,
        position: 'bottom'
      }
    ];

      // 如果是从 edit_task 上传保存后回来的，直接续接到“二维码提示”步骤，避免从头再来
      var stNow = getState();
      var resumeAt = (stNow && stNow.resumeAt) ? String(stNow.resumeAt) : '';
      if (resumeAt === 'task_detail_after_upload') {
        try { setState({ resumeAt: '' }); } catch (e0) {}
      }

      var intro = startIntro(steps, { exitOnOverlayClick: false });
      if (!intro) return;

      if (resumeAt === 'task_detail_after_upload') {
        // steps: 0复制 1选AI 2上传 3二维码 4数据分析
        setTimeout(function () {
          try { intro.goToStepNumber(4); } catch (e1) {}
        }, 50);
      }

      intro.onafterchange(function () {
        var ta = qs('#qf-onboard-suggested');
        if (ta && !ta.value) ta.value = suggested;

        var copyBtn = qs('#qf-onboard-copy-suggested');
        if (copyBtn && !copyBtn.dataset.bound) {
          copyBtn.dataset.bound = '1';
          copyBtn.addEventListener('click', function () {
            copyText(suggested).finally(function () {
              try { intro.nextStep(); } catch (e) {}
            });
          });
          // 进入该步时也自动复制一次
          copyText(suggested);
        }

        var upload = qs('#qf-onboard-upload-html');
        if (upload && !upload.dataset.bound) {
          upload.dataset.bound = '1';
          upload.addEventListener('click', function (e) {
            // 进入“上传”环节时，先退出引导再跳转，避免遮罩层与上传控件抢焦点/抢界面
            try { if (e) e.preventDefault(); } catch (e0) {}
            try {
              setState({
                active: true,
                flowId: 'quickStart_v1',
                navigating: true,
                suppressExit: true,
                resumeAt: 'edit_task_upload'
              });
              try { intro.exit(); } catch (e1) {}
              window.location.assign(ensureOnboardQuery(upload.getAttribute('href')));
            } catch (e2) {
              // 兜底：至少保持引导状态
              setState({ active: true, flowId: 'quickStart_v1' });
            }
          });
        }

        var openAnalyze = qs('#qf-onboard-open-smart-analyze');
        if (openAnalyze && !openAnalyze.dataset.bound) {
          openAnalyze.dataset.bound = '1';
          openAnalyze.addEventListener('click', function (e) {
            // 允许正常跳转，同时保持引导继续
            setState({ active: true, flowId: 'quickStart_v1' });
          });
        }
        // “去 AI 生成网页”：不跳转到主页，改为弹出 AI 选择弹窗
        var goAi = qs('#qf-onboard-go-ai');
        if (goAi && !goAi.dataset.boundOnboardModal) {
          goAi.dataset.boundOnboardModal = '1';
          goAi.addEventListener('click', function (e) {
            try { if (e) e.preventDefault(); } catch (e0) {}
            try {
              var existing = qs('#qfOnboardAiPickerModal');
              if (existing) existing.parentNode.removeChild(existing);
              var wrap = document.createElement('div');
              wrap.innerHTML =
                '<div class="modal fade" id="qfOnboardAiPickerModal" tabindex="-1" aria-hidden="true">' +
                '  <div class="modal-dialog modal-dialog-centered" style="max-width: 760px;">' +
                '    <div class="modal-content">' +
                '      <div class="modal-header">' +
                '        <h5 class="modal-title">选择一个 AI 助手</h5>' +
                '        <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>' +
                '      </div>' +
                '      <div class="modal-body">' +
                '        <div class="text-muted small mb-2">把“建议提示词”粘贴进去，生成 HTML 后下载，再回到这里上传。</div>' +
                '        <div class="d-flex flex-wrap gap-2">' +
                '          <a class="btn btn-outline-primary" target="_blank" rel="noopener noreferrer" href="https://www.doubao.com/">豆包</a>' +
                '          <a class="btn btn-outline-primary" target="_blank" rel="noopener noreferrer" href="https://tongyi.aliyun.com/">通义</a>' +
                '          <a class="btn btn-outline-primary" target="_blank" rel="noopener noreferrer" href="https://kimi.moonshot.cn/">Kimi</a>' +
                '          <a class="btn btn-outline-primary" target="_blank" rel="noopener noreferrer" href="https://yiyan.baidu.com/">文心</a>' +
                '          <a class="btn btn-outline-primary" target="_blank" rel="noopener noreferrer" href="https://xinghuo.xfyun.cn/">讯飞星火</a>' +
                '          <a class="btn btn-outline-primary" target="_blank" rel="noopener noreferrer" href="https://yuanbao.tencent.com/">腾讯元宝</a>' +
                '          <a class="btn btn-outline-primary" target="_blank" rel="noopener noreferrer" href="https://www.coze.cn/">扣子</a>' +
                '        </div>' +
                '      </div>' +
                '      <div class="modal-footer">' +
                '        <button type="button" class="btn btn-outline-secondary" data-bs-dismiss="modal">关闭</button>' +
                '      </div>' +
                '    </div>' +
                '  </div>' +
                '</div>';
              document.body.appendChild(wrap.firstElementChild);
              var modalEl = qs('#qfOnboardAiPickerModal');
              var m = (window.bootstrap && bootstrap.Modal) ? new bootstrap.Modal(modalEl, { backdrop: true, keyboard: true }) : null;
              if (m) m.show();
            } catch (err) {}
          });
        }
      });
    });
  }

  function maybeStartEditTaskTour() {
    var st = getState();
    if (!st.active && !hasOnboardQuery()) return;
    if (!qs('#html-upload-area-edit')) return;
    // 只有从任务详情的“上传 HTML”环节续接时才启动
    if ((st.resumeAt || '') !== 'edit_task_upload') return;

    // 这里不用 Intro.js 遮罩引导（会在某些浏览器抢点击，导致文件选择器点不开）
    // 改为：轻量提示 + 高亮上传区域（不影响交互）
    try { setState({ active: true, flowId: 'quickStart_v1', suppressExit: true, resumeAt: '' }); } catch (e0) {}

    // 保存修改（上传完成）后回到任务详情页时，续接到“二维码提示”步骤
    try {
      var form0 = document.getElementById('edit-task-form');
      if (form0 && !form0.dataset.boundOnboardSubmit) {
        form0.dataset.boundOnboardSubmit = '1';
        form0.addEventListener('submit', function () {
          try {
            setState({
              active: true,
              flowId: 'quickStart_v1',
              navigating: true,
              suppressExit: true,
              resumeAt: 'task_detail_after_upload'
            });
          } catch (e1) {}
        });
      }
    } catch (e2) {}

    try {
      if (!document.getElementById('qfOnboardPulseStyle')) {
        var style = document.createElement('style');
        style.id = 'qfOnboardPulseStyle';
        style.textContent =
          '.qf-onboard-pulse{outline:3px solid rgba(37,99,235,.55); outline-offset:4px; box-shadow:0 0 0 6px rgba(37,99,235,.16); animation:qfPulse 1.2s ease-in-out infinite;}@keyframes qfPulse{0%,100%{box-shadow:0 0 0 6px rgba(37,99,235,.14)}50%{box-shadow:0 0 0 10px rgba(37,99,235,.22)}}';
        document.head.appendChild(style);
      }
    } catch (e1) {}

    var area = qs('#html-upload-area-edit');
    if (area) {
      try { area.scrollIntoView({ block: 'center', behavior: 'smooth' }); } catch (e2) {}
      try { area.classList.add('qf-onboard-pulse'); } catch (e3) {}
      setTimeout(function () { try { area.classList.remove('qf-onboard-pulse'); } catch (e4) {} }, 4500);
    }

    // 插入一次性提示条
    try {
      if (!document.getElementById('qfOnboardEditUploadHint')) {
        var hint = document.createElement('div');
        hint.id = 'qfOnboardEditUploadHint';
        hint.className = 'alert alert-info';
        hint.style.position = 'sticky';
        hint.style.top = '0.75rem';
        hint.style.zIndex = '50';
        hint.innerHTML = '<div class="d-flex justify-content-between align-items-center gap-2 flex-wrap"><div><strong>下一步：</strong>请在下方区域选择/拖拽 HTML 文件，选好后点击“保存修改”。</div><button type="button" class="btn btn-sm btn-outline-secondary">知道了</button></div>';
        var btn = hint.querySelector('button');
        if (btn) btn.addEventListener('click', function(){ try{ hint.remove(); }catch(e5){} });
        var container = document.querySelector('.container') || document.body;
        container.insertBefore(hint, container.firstChild);
      }
    } catch (e6) {}
  }

  function maybeStartSmartAnalyzeTour() {
    var st = getState();
    if (!st.active && !hasOnboardQuery()) return;
    // 仅在 smart_analyze 页启动（存在关键元素）
    if (!qs('#dashWizard') && !qs('#dashPromptTemplate')) return;

    var steps = [
      {
        title: '制作实时数据大屏',
        intro: '这里是“数据大屏”向导。你可以手动复制提示词去生成，也可以用平台的自动生成（最多 3 次）。',
        element: document.body,
        position: 'floating'
      },
      {
        title: '展开数据大屏向导',
        intro: '先点这里展开“我要制作数据大屏”。',
        element: qs('[data-bs-target="#dashWizard"]') || document.body,
        position: 'bottom'
      },
      {
        title: '复制提示词模板（手动生成）',
        intro: '这是参考提示词模板（已自动带上 /all 地址与数据格式示例）。你可以复制到 deepseek / 豆包等生成 HTML。',
        element: qs('#dashPromptTemplate') || document.body,
        position: 'bottom'
      },
      {
        title: '一键自动生成（可选）',
        intro: '如果你已在个人设置填了 APIKEY，并且有至少 1 条数据，可以点这里自动生成数据大屏（会消耗 1 次）。',
        element: qs('#qf-onboard-auto-generate-dashboard') || document.body,
        position: 'bottom'
      },
      {
        title: '打开已生成大屏',
        intro: '生成完成后，这里会出现“打开已生成大屏”。建议你先打开看看效果，再决定是否继续修改。',
        element: qs('#qf-onboard-open-dashboard') || document.body,
        position: 'bottom'
      }
    ];

    var intro = startIntro(steps, { exitOnOverlayClick: false });
    if (!intro) return;

    intro.onafterchange(function () {
      // 该步需要点击展开按钮时，Intro 的遮罩/高亮在部分浏览器会“抢点击”
      // 这里改为自动展开并自动进入下一步，避免用户点不到
      try {
        if (intro._currentStep === 1) {
          var openBtn0 = qs('[data-bs-target="#dashWizard"]');
          if (openBtn0 && !openBtn0.dataset.qfOnboardAutoOpened) {
            openBtn0.dataset.qfOnboardAutoOpened = '1';
            try { openBtn0.click(); } catch (e0) {}
            setTimeout(function () { try { intro.nextStep(); } catch (e1) {} }, 350);
            return;
          }
          // 已经展开过了就直接继续
          setTimeout(function () { try { intro.nextStep(); } catch (e2) {} }, 0);
          return;
        }
      } catch (e) {}

      var openBtn = qs('[data-bs-target="#dashWizard"]');
      if (openBtn && !openBtn.dataset.boundOnboardOpen) {
        openBtn.dataset.boundOnboardOpen = '1';
        openBtn.addEventListener('click', function () {
          setState({ active: true, flowId: 'quickStart_v1' });
        });
      }

      var copyBtn = qs('#qf-onboard-copy-dashboard-prompt');
      if (copyBtn && !copyBtn.dataset.boundOnboardCopy) {
        copyBtn.dataset.boundOnboardCopy = '1';
        copyBtn.addEventListener('click', function () {
          // 用户点复制即可，无需强制跳步
        });
      }
    });
  }

  function maybeStartHomeTour() {
    var st = getState();
    if (!st.active && !hasOnboardQuery()) return;
    if (!qs('#quick-start-step-3')) return;

    var steps = [
      {
        title: '选择一个 AI',
        intro: '把刚才复制的提示词粘贴到这里的任意 AI，生成页面后下载 HTML 文件。',
        element: qs('#quick-start-step-3'),
        position: 'top'
      },
      {
        title: '生成后回到任务上传',
        intro: '下载好 HTML 后回到任务详情页/编辑任务上传。若你刚才还没打开任务详情，可以回到“我的数据任务”找到它。',
        element: document.body,
        position: 'floating'
      }
    ];
    startIntro(steps);
  }

  function boot() {
    bindStartButtons();
    clearNavigatingFlag();

    // 不做“自动弹出”，只有带 onboard=1 或 state.active 才启动
    maybeStartDashboardTour();
    maybeStartCreateTaskTour();
    maybeStartTaskDetailTour();
    maybeStartEditTaskTour();
    maybeStartSmartAnalyzeTour();
    maybeStartHomeTour();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();

