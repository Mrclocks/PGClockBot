/* Connection guides: photos and videos of each guide (upload, order, preview, remove). */
(function (root) {
  'use strict';

  var MAX_MEDIA = 10;

  function esc(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }

  function itemLabel(item, index) {
    var kind = item.kind === 'video' ? 'ویدیو' : 'عکس';
    return (item.kind === 'video' ? '🎬 ' : '🖼 ') + kind + ' ' + (index + 1);
  }

  function itemHtml(item, index, count) {
    var label = esc(itemLabel(item, index));
    var note = item.missing
      ? '<span class="guides-media-missing">فایل ندارد، دوباره آپلود کنید</span>'
      : item.from_bot
        ? '<span class="muted">از ربات</span>'
        : '';
    var name = item.preview
      ? '<a href="' + esc(item.preview) + '" target="_blank" rel="noopener">' + label + '</a>'
      : '<span>' + label + '</span>';
    return (
      '<li class="guides-media-item" data-media-id="' + esc(item.id) + '">' +
        name + note +
        '<span class="guides-media-actions">' +
          '<button type="button" class="btn btn-sm" data-media-move="up" aria-label="جابه‌جایی به قبل"' + (index === 0 ? ' disabled' : '') + '>▲</button>' +
          '<button type="button" class="btn btn-sm" data-media-move="down" aria-label="جابه‌جایی به بعد"' + (index === count - 1 ? ' disabled' : '') + '>▼</button>' +
          '<button type="button" class="btn btn-danger btn-sm" data-media-delete aria-label="حذف ' + label + '">حذف</button>' +
        '</span>' +
      '</li>'
    );
  }

  /** Markup for one guide's media block; ``status`` is a message shown under the list. */
  function mediaBlockHtml(media, status) {
    var list = Array.isArray(media) ? media : [];
    var items = list.map(function (item, index) { return itemHtml(item, index, list.length); }).join('');
    var full = list.length >= MAX_MEDIA;
    return (
      '<fieldset class="guides-media-box"><legend>عکس و ویدیو (حداکثر ' + MAX_MEDIA + ' فایل)</legend>' +
        (items ? '<ul class="guides-media-list">' + items + '</ul>' : '<p class="muted">هنوز فایلی اضافه نشده است.</p>') +
        (full
          ? ''
          : '<label class="guides-media-upload">افزودن عکس یا ویدیو' +
              '<input type="file" accept="image/jpeg,image/png,image/webp,video/mp4" data-media-upload /></label>') +
        '<p class="muted">عکس تا 10 مگابایت، ویدیوی MP4 تا 50 مگابایت. برای آموزش تازه، اول تنظیمات را ذخیره کنید.</p>' +
        (status ? '<p class="guides-media-status" role="status">' + esc(status) + '</p>' : '') +
      '</fieldset>'
    );
  }

  var api = { mediaBlockHtml: mediaBlockHtml, MAX_MEDIA: MAX_MEDIA };
  if (typeof module !== 'undefined' && module.exports) { module.exports = api; return; }

  function parseMap(raw) {
    try {
      var data = JSON.parse(raw || '{}');
      return data && typeof data === 'object' && !Array.isArray(data) ? data : {};
    } catch (_) {
      return {};
    }
  }

  function setup(editor) {
    var base = editor.getAttribute('data-guides-media-base');
    if (!base || editor.__guideMedia) return;
    var state = { media: parseMap(editor.getAttribute('data-guides-media')), status: {} };
    editor.__guideMedia = state;

    function renderSlots() {
      editor.querySelectorAll('[data-guides-row]').forEach(function (row) {
        var slot = row.querySelector('[data-guides-media-slot]');
        var id = row.getAttribute('data-guides-id');
        if (slot) slot.innerHTML = mediaBlockHtml(state.media[id], state.status[id]);
      });
    }

    function send(action, guideId, fields) {
      var body = new FormData();
      body.append('guide_id', guideId);
      Object.keys(fields).forEach(function (key) { body.append(key, fields[key]); });
      state.status[guideId] = action === 'upload' ? 'در حال آپلود…' : '';
      renderSlots();
      return fetch(base + '/' + action, { method: 'POST', body: body })
        .then(function (resp) { return resp.json().catch(function () { return { ok: false }; }); })
        .then(function (data) {
          if (data && data.ok) {
            state.media[guideId] = data.media || [];
            state.status[guideId] = action === 'upload' ? 'فایل اضافه شد.' : '';
          } else {
            state.status[guideId] = (data && data.error) || 'انجام نشد. دوباره تلاش کنید.';
          }
        })
        .catch(function () { state.status[guideId] = 'ارتباط با سرور برقرار نشد.'; })
        .then(renderSlots);
    }

    editor.addEventListener('change', function (event) {
      var input = event.target.closest('[data-media-upload]');
      if (!input || !input.files || !input.files[0]) return;
      event.stopPropagation();
      var row = input.closest('[data-guides-row]');
      send('upload', row.getAttribute('data-guides-id'), { file: input.files[0] });
    });
    editor.addEventListener('click', function (event) {
      var button = event.target.closest('[data-media-move], [data-media-delete]');
      if (!button) return;
      event.preventDefault();
      var row = button.closest('[data-guides-row]');
      var item = button.closest('[data-media-id]');
      var guideId = row.getAttribute('data-guides-id');
      var mediaId = item.getAttribute('data-media-id');
      if (button.hasAttribute('data-media-delete')) {
        if (!window.confirm('این فایل از آموزش حذف شود؟ این کار برگشت ندارد.')) return;
        send('delete', guideId, { media_id: mediaId });
        return;
      }
      send('move', guideId, { media_id: mediaId, direction: button.getAttribute('data-media-move') });
    });
    editor.addEventListener('guides-rendered', renderSlots);
    renderSlots();
  }

  function init() {
    document.querySelectorAll('[data-guides-editor]').forEach(setup);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})(typeof window !== 'undefined' ? window : globalThis);
