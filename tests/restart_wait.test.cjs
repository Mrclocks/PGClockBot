// Logic of the restart wait on «نگهداری سرور»: finished only when the panel answers from a new boot.
const assert = require('node:assert/strict');
const { restartFinished, nextUrl } = require('../app/web/static/restart-wait.js');

assert.equal(restartFinished(null, 'old'), false, 'no answer yet');
assert.equal(restartFinished({ boot_id: 'old' }, 'old'), false, 'the old process still answers');
assert.equal(restartFinished({}, 'old'), false, 'an answer without a boot id');
assert.equal(restartFinished({ boot_id: 'new' }, ''), false, 'nothing to compare with');
assert.equal(restartFinished({ boot_id: 'new' }, 'old'), true, 'a new process answers');

const done = new URL(nextUrl('https://panel.test/tools/maintenance?restarting=old', true));
assert.equal(done.searchParams.get('restarting'), null);
assert.equal(done.searchParams.get('ok'), 'پنل دوباره روشن شد');
const late = new URL(nextUrl('https://panel.test/tools/maintenance?restarting=old', false));
assert.ok(late.searchParams.get('err'));
assert.equal(late.searchParams.get('restarting'), null);
console.log('restart_wait: ok');
