'use strict';
const assert = require('assert');
const media = require('../app/web/static/guide-media.js');

// Empty guide: an upload control and the empty state.
const empty = media.mediaBlockHtml([], '');
assert.ok(empty.includes('data-media-upload'));
assert.ok(empty.includes('هنوز فایلی اضافه نشده است'));

// Items keep their order, the first cannot move up and the last cannot move down.
const two = media.mediaBlockHtml([
  { id: 'a1', kind: 'photo', preview: '/settings/guides/media/file/x.jpg' },
  { id: 'b2', kind: 'video', missing: true },
], '');
assert.ok(two.indexOf('data-media-id="a1"') < two.indexOf('data-media-id="b2"'));
assert.ok(/data-media-move="up" aria-label="[^"]+" disabled/.test(two));
assert.ok(two.includes('فایل ندارد، دوباره آپلود کنید'));

// A full album hides the upload control.
const full = media.mediaBlockHtml(Array.from({ length: media.MAX_MEDIA }, (_, i) => ({ id: 'm' + i, kind: 'photo' })), '');
assert.ok(!full.includes('data-media-upload'));

// Server text and ids are escaped on insertion.
const hostile = media.mediaBlockHtml([{ id: '"><img src=x onerror=alert(1)>', kind: 'photo', preview: '" onmouseover="x' }], '<script>x</script>');
assert.ok(!hostile.includes('<img src=x'));
assert.ok(!hostile.includes('<script>'));
assert.ok(!hostile.includes('" onmouseover="'));

console.log('guide_media.test.cjs ok');
