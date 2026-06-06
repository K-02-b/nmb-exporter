// ========== utils ==========
const $ = (s, r=document) => r.querySelector(s);
const $$ = (s, r=document) => Array.from(r.querySelectorAll(s));

function escapeHTML(s) {
  return String(s ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function toast(msg, type='info') {
  const c = $('#toastContainer');
  const cls = type==='error' ? 'danger'
            : type==='warning' ? 'warning'
            : type==='success' ? 'success' : 'primary';

  const el = document.createElement('div');
  el.className = `toast align-items-center text-bg-${cls} border-0 show mb-2`;
  el.style.minWidth = '0';
  el.innerHTML = `<div class="d-flex">
    <div class="toast-body">${escapeHTML(msg)}</div>
    <button type="button" class="btn-close btn-close-white me-2 m-auto"></button>
  </div>`;

  el.querySelector('.btn-close').onclick = () => el.remove();
  c.appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

// ========== 共享 ext 状态 ==========
const ext = {
  show_header: true,
  show_meta: true,
  show_divider: true,
  jpg_width: 1080,
  export_pages: '',
  save_images: true,
  image_quality: 'thumb',
};

function bindExtPanels() {
  $$('.ext-panel').forEach(panel => {
    $$('.ext-toggle', panel).forEach(el => {
      const k = el.dataset.ext;
      el.checked = !!ext[k];

      el.addEventListener('change', () => {
        ext[k] = el.checked;
        syncExtPanels();
        onExtChange();
      });
    });

    $$('.ext-input', panel).forEach(el => {
      const k = el.dataset.ext;
      el.value = ext[k] ?? '';

      el.addEventListener('change', () => {
        ext[k] = el.value;
        syncExtPanels();
        onExtChange();
      });

      el.addEventListener('input', () => {
        ext[k] = el.value;
      });
    });
  });
}

function syncExtPanels() {
  $$('.ext-panel').forEach(panel => {
    $$('.ext-toggle', panel).forEach(el => {
      const k = el.dataset.ext;
      el.checked = !!ext[k];
    });

    $$('.ext-input', panel).forEach(el => {
      const k = el.dataset.ext;
      el.value = ext[k] ?? '';
    });
  });
}

let extChangeTimer = null;

function onExtChange() {
  clearTimeout(extChangeTimer);

  extChangeTimer = setTimeout(() => {
    if (state.currentTab === 'download' && state.downloadThread) {
      refreshDownloadPreview();
    } else if (state.currentTab === 'library' && state.libraryDetail) {
      reloadLibraryDetail();
    }
  }, 250);
}

// ========== state ==========
const state = {
  currentTab: 'download',
  downloadThread: null,
  libraryList: [],
  libraryDetail: null,
  sse: null,
};

// ========== tabs ==========
$$('.nav-tab').forEach(t => {
  t.addEventListener('click', () => {
    state.currentTab = t.dataset.tab;

    $$('.nav-tab').forEach(x => {
      x.classList.toggle('active', x === t);
    });

    $$('.tab-pane').forEach(p => {
      p.classList.toggle('active', p.id === `tab-${t.dataset.tab}`);
    });

    if (t.dataset.tab === 'library') refreshLibrary();
    if (t.dataset.tab === 'settings') loadConfig();
  });
});

// ========== 下载 ==========
$('#downloadForm').addEventListener('submit', e => {
  e.preventDefault();

  const tid = parseInt($('#threadId').value, 10);
  if (!tid) {
    toast('请输入串号', 'warning');
    return;
  }

  startDownload({
    tid,
    only_po: $('#onlyPo').checked ? 1 : 0,
    pages: $('#pages').value.trim(),
    threads: parseInt($('#threads').value, 10) || 5,
    resume: $('#resume').checked ? 1 : 0,
    show_header: ext.show_header ? 1 : 0,
    show_meta: ext.show_meta ? 1 : 0,
    show_divider: ext.show_divider ? 1 : 0,
    save_images: ext.save_images ? 1 : 0,
    image_quality: ext.image_quality || 'thumb',
  });
});

let totalPages = 0;
let donePages = 0;
let accumulatedHTML = '';

function startDownload(params) {
  if (state.sse) state.sse.close();

  state.downloadThread = {
    tid: params.tid,
    only_po: !!params.only_po,
  };

  totalPages = 0;
  donePages = 0;
  accumulatedHTML = '';

  $('#progressBox').classList.remove('d-none');
  $('#stageLabel').textContent = '准备中...';
  $('#statusLine').textContent = '';
  $('#failedLine').textContent = '';
  $('#progressBar').style.width = '0%';
  $('#previewTitle').textContent = `No.${params.tid}${params.only_po ? '（仅Po）' : ''}`;
  $('#previewMeta').textContent = '';
  $('#downloadPreview').srcdoc = '';
  $('#exportButtons').style.display = '';

  const qs = new URLSearchParams(params).toString();
  const sse = new EventSource(`/api/download_stream?${qs}`);
  state.sse = sse;

  sse.onmessage = ev => {
    try {
      handleSSE(JSON.parse(ev.data));
    } catch (e) {
      console.error(e);
    }
  };

  sse.onerror = () => {
    sse.close();
    state.sse = null;
  };
}

function handleSSE(d) {
  switch (d.event) {
    case 'open':
      accumulatedHTML = '';
      totalPages = 0;
      donePages = 0;
      break;

    case 'stage':
      if (d.stage === 'fetch_total') {
        $('#stageLabel').textContent = '正在获取总页数...';
      } else if (d.stage === 'fetch_images') {
        $('#stageLabel').textContent = `正在下载图片（${d.quality}）...`;
      } else {
        $('#stageLabel').textContent = d.stage || '';
      }
      break;

    case 'total_pages':
      $('#stageLabel').textContent = `共 ${d.total || 0} 个回复页`;
      break;

    case 'plan':
      totalPages = Array.isArray(d.pages) ? d.pages.length : 0;
      donePages = 0;
      $('#stageLabel').textContent = `计划下载 ${totalPages} 页`;
      $('#statusLine').textContent = `已完成 0/${totalPages || '?'} 页`;
      $('#progressBar').style.width = '0%';
      break;

    case 'page_done':
      donePages++;

      if (totalPages > 0) {
        $('#progressBar').style.width =
          `${Math.min(100, donePages * 100 / totalPages)}%`;
      }

      $('#statusLine').textContent =
        `已完成 ${donePages}/${totalPages || '?'} 页`;

      if (d.html) {
        accumulatedHTML += d.html;
        renderAccumulated();
      }
      break;

    case 'page_failed':
      donePages++;

      if (totalPages > 0) {
        $('#progressBar').style.width =
          `${Math.min(100, donePages * 100 / totalPages)}%`;
      }

      $('#statusLine').textContent =
        `已完成 ${donePages}/${totalPages || '?'} 页`;

      $('#failedLine').textContent =
        `失败页：${d.page}`;
      break;

    case 'rate_limit':
      $('#stageLabel').textContent =
        `限流暂停 ${Number(d.wait || 0).toFixed(1)} 秒`;
      break;

    case 'image_progress':
      $('#statusLine').textContent =
        `[图片] ${d.done}/${d.total}（成功 ${d.ok}，失败 ${d.fail}）`;
      break;

    case 'images_done':
      $('#statusLine').textContent =
        `[图片] 完成：成功 ${d.ok}，失败 ${d.fail}，合计 ${d.total}`;
      break;

    case 'image_error':
      $('#failedLine').textContent = `图片下载异常：${d.error}`;
      break;

    case 'all_done':
      $('#stageLabel').textContent = '完成';
      $('#progressBar').style.width = '100%';
      toast('下载完成', 'success');
      refreshDownloadPreview();
      break;

    case 'error':
      $('#failedLine').textContent = d.error || '错误';
      toast(d.error || '错误', 'error');
      break;

    case 'close':
      if (state.sse) {
        state.sse.close();
        state.sse = null;
      }
      break;
  }
}

function renderAccumulated() {
  const html = `<!DOCTYPE html><html><head><meta charset="utf-8">
    <style>
      body{margin:0;padding:20px;background:#f0f2f5;
            font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;}
      .thread{max-width:860px;margin:0 auto;}
    </style></head><body>
    <div class="thread">${accumulatedHTML}</div></body></html>`;

  $('#downloadPreview').srcdoc = html;
}

function refreshDownloadPreview() {
  if (!state.downloadThread) return;

  const params = new URLSearchParams({
    tid: state.downloadThread.tid,
    po: state.downloadThread.only_po ? 1 : 0,
    show_header: ext.show_header ? 1 : 0,
    show_meta: ext.show_meta ? 1 : 0,
    show_divider: ext.show_divider ? 1 : 0,
    image_quality: ext.save_images ? (ext.image_quality || 'thumb') : '',
  });

  fetch(`/api/preview?${params}`)
    .then(r => r.json())
    .then(d => {
      if (!d.ok) {
        toast(d.error, 'error');
        return;
      }

      $('#downloadPreview').srcdoc = d.html;
      $('#previewMeta').textContent =
        `共 ${d.count} 楼，已渲染页：${d.rendered_pages.join(',')}`;
    });
}

$$('#exportButtons button').forEach(btn => {
  btn.addEventListener('click', () => {
    if (!state.downloadThread) return;
    doExport(btn.dataset.fmt, state.downloadThread);
  });
});

// ========== 本地库 ==========
function refreshLibrary() {
  fetch('/api/threads')
    .then(r => r.json())
    .then(d => {
      state.libraryList = d.threads || [];
      renderLibraryList();
    });
}

function renderLibraryList() {
  const c = $('#libraryList');
  c.innerHTML = '';

  if (!state.libraryList.length) {
    $('#libraryEmpty').classList.remove('d-none');
    return;
  }

  $('#libraryEmpty').classList.add('d-none');

  state.libraryList.forEach(t => {
    const item = document.createElement('div');
    item.className = 'lib-item';

    if (
      state.libraryDetail &&
      state.libraryDetail.tid === t.thread_id &&
      state.libraryDetail.only_po === t.only_po
    ) {
      item.classList.add('active');
    }

    item.innerHTML = `
      <div>
        <b>No.${escapeHTML(t.thread_id)}</b>
        ${t.only_po ? ' <span class="badge bg-info">仅Po</span>' : ''}
      </div>
      <div class="meta">已存：${escapeHTML(t.saved_pages_text || '无')} / 共 ${escapeHTML(t.total_pages_known || '?')} 页</div>
      <div class="meta">${escapeHTML(t.updated_at || '')}</div>`;

    item.addEventListener('click', () => {
      state.libraryDetail = {
        tid: t.thread_id,
        only_po: t.only_po,
      };

      renderLibraryList();
      reloadLibraryDetail();
    });

    c.appendChild(item);
  });
}

function reloadLibraryDetail() {
  if (!state.libraryDetail) return;

  const t = state.libraryDetail;

  $('#libTitle').textContent = `No.${t.tid}${t.only_po ? '（仅Po）' : ''}`;
  $('#libExportButtons').style.display = '';
  $('#libExtBox').style.display = '';
  $('#libDelete').style.display = '';

  const params = new URLSearchParams({
    tid: t.tid,
    po: t.only_po ? 1 : 0,
    show_header: ext.show_header ? 1 : 0,
    show_meta: ext.show_meta ? 1 : 0,
    show_divider: ext.show_divider ? 1 : 0,
    image_quality: ext.save_images ? (ext.image_quality || 'thumb') : '',
    pages: ext.export_pages || '',
  });

  fetch(`/api/preview?${params}`)
    .then(r => r.json())
    .then(d => {
      if (!d.ok) {
        toast(d.error, 'error');
        return;
      }

      $('#libPreview').srcdoc = d.html;
      $('#libMeta').textContent =
        `共 ${d.count} 楼，已渲染页：${d.rendered_pages.join(',')}`;
    });
}

$$('#libExportButtons button').forEach(btn => {
  btn.addEventListener('click', () => {
    if (!state.libraryDetail) return;
    doExport(btn.dataset.fmt, state.libraryDetail);
  });
});

$('#libDelete').addEventListener('click', () => {
  if (!state.libraryDetail) return;

  if (!confirm(`确认删除 No.${state.libraryDetail.tid} 的本地数据？`)) {
    return;
  }

  const t = state.libraryDetail;

  fetch(`/api/thread/${t.tid}?po=${t.only_po ? 1 : 0}`, {
    method:'DELETE',
  })
    .then(r => r.json())
    .then(d => {
      if (d.ok) {
        toast('已删除', 'success');

        state.libraryDetail = null;

        $('#libPreview').srcdoc = '';
        $('#libTitle').textContent = '请选择串';
        $('#libMeta').textContent = '';
        $('#libExportButtons').style.display = 'none';
        $('#libExtBox').style.display = 'none';
        $('#libDelete').style.display = 'none';

        refreshLibrary();
      }
    });
});

// ========== 导出 ==========
function doExport(fmt, ctx) {
  const params = new URLSearchParams({
    tid: ctx.tid,
    po: ctx.only_po ? 1 : 0,
    show_header: ext.show_header ? 1 : 0,
    show_meta: ext.show_meta ? 1 : 0,
    show_divider: ext.show_divider ? 1 : 0,
    width: parseInt(ext.jpg_width, 10) || 1080,
    image_quality: ext.save_images ? (ext.image_quality || 'thumb') : '',
    pages: ext.export_pages || '',
  });

  window.open(`/api/export/${fmt}?${params}`, '_blank');
}

// ========== 设置 ==========
function loadConfig() {
  fetch('/api/config')
    .then(r => r.json())
    .then(d => {
      if (d.ok) {
        $('#cookieInput').value = (d.config && d.config.cookie) || '';
      }
    });
}

$('#saveConfig').addEventListener('click', () => {
  fetch('/api/config', {
    method:'POST',
    headers:{
      'Content-Type':'application/json',
    },
    body:JSON.stringify({
      cookie: $('#cookieInput').value.trim(),
    }),
  })
    .then(r => r.json())
    .then(d => {
      if (d.ok) toast('已保存', 'success');
    });
});

// ========== 二维码导入 userhash ==========
$('#parseQrcode').addEventListener('click', () => {
  const fileInput = $('#qrcodeFile');
  const file = fileInput.files && fileInput.files[0];

  if (!file) {
    toast('请选择二维码图片', 'warning');
    return;
  }

  const btn = $('#parseQrcode');
  const oldText = btn.innerHTML;

  btn.disabled = true;
  btn.innerHTML =
    '<span class="spinner-border spinner-border-sm me-1"></span>读取中...';

  const fd = new FormData();
  fd.append('image', file);

  fetch('/api/config/qrcode', {
    method:'POST',
    body:fd,
  })
    .then(r => r.json())
    .then(d => {
      if (!d.ok) {
        toast(d.error || '二维码读取失败', 'error');
        return;
      }

      $('#cookieInput').value = d.cookie || '';

      if (d.name) {
        toast(`已读取并保存：${d.name}`, 'success');
      } else {
        toast('已读取并保存 userhash', 'success');
      }
    })
    .catch(e => {
      toast(`二维码读取失败：${e}`, 'error');
    })
    .finally(() => {
      btn.disabled = false;
      btn.innerHTML = oldText;
    });
});

// ========== init ==========
bindExtPanels();
loadConfig();