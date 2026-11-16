#!/usr/bin/env node
/* Panel UI audit (DESIGN_SYSTEM.md §11): opens every page, its tabs and settings sections, and every modal a button
   opens, then reports layout defects that the design rules forbid:
     TOUCH / TOUCH-H  two boxes touching (gap under 6px)
     GAP / PAD        box-to-box gap or box padding that is not 16
     OFFCENTRE        a single-line control whose text is off its vertical centre
     LATIN-HIGH       a Latin label in a capsule that is not cap-trimmed (sits ~2px high in Vazirmatn)
     PAIR             side-by-side fields whose labels line up but whose boxes do not
     ROWH             controls in one row with different heights
     OVERFLOW         text spilling out of a control
   Usage: node scripts/ui_audit.cjs --base http://127.0.0.1:9000 --user admin --pass '...' [--width 390] [--no-modals]
   Exits 1 when it finds anything. Run it at 390, 768 and 1440 before a pull request that changes the UI. */
'use strict';
const args = Object.fromEntries(process.argv.slice(2).reduce((acc, a, i, all) => {
  if (a.startsWith('--')) acc.push([a.slice(2), all[i + 1] && !all[i + 1].startsWith('--') ? all[i + 1] : '1']);
  return acc; }, []));
const { chromium } = require('playwright');
const BASE = (args.base || 'http://127.0.0.1:9000').replace(/\/$/, '');
const SEED = ['/home','/dashboard','/users','/users/12/edit','/plans','/plans/1/edit','/resellers/plans','/finance','/finance/payg','/orders','/payments','/tickets','/inbox','/notifications','/resellers','/resellers/11/edit','/resellers/applications','/resellers/children-pricing','/principals','/marketing','/broadcast','/shop-broadcast','/campaigns','/cashback','/catalog-discounts','/random-discounts','/gift-codes','/loyalty','/funnel','/service-cleanup','/service-cancellations','/service-import/requests','/service-shop-review','/staff-actions','/trial-users','/pg','/pg/admins','/pg/groups','/pg/hosts','/pg/inbounds','/pg/nodes','/pg/templates','/pg/users','/connection-status','/settings','/shop-settings','/security','/update','/help','/message-variables','/menu-layout','/my-subscription','/payg/charges/review',
 '/finance?settings=billing','/finance?settings=cleanup','/finance?settings=payment','/loyalty?settings=club','/loyalty?settings=referral','/loyalty?settings=rewards','/loyalty?settings=rules','/loyalty?settings=tiers','/loyalty?settings=wheel'];
const width = +(args.width || 1440), withModals = !args['no-modals'];
const results = [];
async function scan(p, where) {
  const found = await p.evaluate(() => {
    const out = [];
    const vis = e => { const r=e.getBoundingClientRect(); const s=getComputedStyle(e); return r.width>0 && r.height>0 && s.visibility!=='hidden' && s.display!=='none' && +s.opacity>0.05; };
    const name = e => { const t=(e.getAttribute('aria-label')||e.textContent||'').trim().replace(/\s+/g,' ').slice(0,22); return `${e.tagName.toLowerCase()}.${(e.className.baseVal??e.className).toString().split(' ').filter(Boolean).slice(0,2).join('.')}«${t}»`; };
    // modal open? limit scope
    const modal=[...document.querySelectorAll('.ui-modal')].find(m=>!m.hidden && (m.classList.contains('open')||getComputedStyle(m).display!=='none') && vis(m));
    const root = modal || document.querySelector('#main') || document.body;
    // (a) touching boxes
    const boxes=[...root.querySelectorAll('*')].filter(e=>{ if(!vis(e)) return false; const s=getComputedStyle(e); const r=e.getBoundingClientRect(); return parseFloat(s.borderTopWidth)>0 && parseFloat(s.borderBottomWidth)>0 && parseFloat(s.borderTopLeftRadius)>=8 && r.height>48 && r.width>140 && s.position!=='fixed' && s.position!=='absolute'; });
    for (const a of boxes) { const ra=a.getBoundingClientRect();
      for (const b of boxes) { if (a===b || a.contains(b) || b.contains(a)) continue; const rb=b.getBoundingClientRect();
        const gap=rb.top-ra.bottom; const ov=Math.min(ra.right,rb.right)-Math.max(ra.left,rb.left);
        if (gap>=-1 && gap<6 && ov>0.5*Math.min(ra.width,rb.width)) out.push(`TOUCH ${gap.toFixed(0)}px ${name(a)} ↕ ${name(b)}`);
        const hgap=rb.left-ra.right; const vov=Math.min(ra.bottom,rb.bottom)-Math.max(ra.top,rb.top);
        if (hgap>=-1 && hgap<6 && vov>0.5*Math.min(ra.height,rb.height)) out.push(`TOUCH-H ${hgap.toFixed(0)}px ${name(a)} ↔ ${name(b)}`);
      } }
    // (b) text centring in single-line controls
    const ctl=[...root.querySelectorAll('button, .btn, a.btn, .badge, .ds-badge, .ds-tag, .ds-mini, .chip, [class*="-chip"], [class*="chip-"], .section-tabs a, .ui-select-toggle, label.ds-check, .ds-choice label, .ds-pill, .pill, [class*="pill"], [role="tab"], .list-editor-row, .icon-btn, .ui-switch-chip, .sales-seg > *, .color-tag-option, .wallet-mode-btn')].filter(vis);
    for (const e of ctl) { const r=e.getBoundingClientRect(); if (r.height<18 || r.height>72) continue;
      const tw=document.createTreeWalker(e, NodeFilter.SHOW_TEXT); const rects=[]; let n;
      while ((n=tw.nextNode())) { if (!n.textContent.trim()) continue; const pe=n.parentElement; if (!vis(pe)) continue; const pr=pe.getBoundingClientRect(); const ps=getComputedStyle(pe); if (pr.width<=2 || ps.clipPath.includes('inset(50%') || (ps.textBoxTrim && ps.textBoxTrim!=='none')) continue; const rg=document.createRange(); rg.selectNodeContents(n); for (const q of rg.getClientRects()) if (q.height>0) rects.push(q); }
      if (!rects.length) continue; const top=Math.min(...rects.map(q=>q.top)), bot=Math.max(...rects.map(q=>q.bottom));
      const lh=parseFloat(getComputedStyle(e).fontSize)*1.9; if (bot-top>lh) continue; // multi-line
      const s=getComputedStyle(e); const ct=r.top+parseFloat(s.borderTopWidth), cb=r.bottom-parseFloat(s.borderBottomWidth);
      const d=((top+bot)/2)-((ct+cb)/2); if (Math.abs(d)>1.6) out.push(`OFFCENTRE ${d.toFixed(1)}px ${name(e)} h=${r.height.toFixed(0)}`);
      // Latin-only label in a capsule: the font puts it ~2px high unless it is cap-trimmed
      const tw2=document.createTreeWalker(e, NodeFilter.SHOW_TEXT); let m; while ((m=tw2.nextNode())) { const t=m.textContent.trim(); if (!t || !/[A-Za-z]/.test(t) || /[\u0600-\u06FF]/.test(t)) continue; const pe=m.parentElement; if (!vis(pe)) continue; const tb=getComputedStyle(pe).textBoxTrim; if ((!tb || tb==='none') && r.height<=60) { out.push(`LATIN-HIGH ${name(e)} «${t.slice(0,16)}»`); break; } } }
    // (c) field pairs
    const ctrlOf=l=>l.querySelector(':scope > input:not([type=hidden]):not([type=checkbox]):not([type=radio]), :scope > select:not([hidden]), :scope > .ui-select, :scope > textarea, :scope > .pw-field, :scope > .num-stepper, :scope > div > input:not([type=hidden])');
    const labels=[...root.querySelectorAll('label, .form-field')].filter(l=>vis(l)&&ctrlOf(l)&&vis(ctrlOf(l)));
    for (const a of labels) for (const c of labels) { if (a===c) continue; const ra=a.getBoundingClientRect(), rc=c.getBoundingClientRect();
      if (Math.abs(ra.top-rc.top)>1 || !(ra.right<=rc.left)) continue; const d=Math.round(ctrlOf(a).getBoundingClientRect().top-ctrlOf(c).getBoundingClientRect().top);
      if (Math.abs(d)>1) out.push(`PAIR ${d}px ${name(a)} vs ${name(c)}`); }

    // (d) controls side by side in one row must share a height
    const isCtl = e => e.matches('input:not([type=checkbox]):not([type=radio]):not([type=hidden]):not([type=range]), select, .ui-select, .btn, a.btn, .icon-btn, .num-stepper, .pw-field, .list-editor-toggle');
    for (const row of root.querySelectorAll('*')) { if (!vis(row)) continue; const st=getComputedStyle(row); if (!/flex|grid/.test(st.display)) continue;
      const kids=[...row.children].filter(k=>vis(k)).map(k=> isCtl(k)? k : (k.matches('label, .form-field') ? null : (k.querySelector(':scope > .btn, :scope > input')||null))).filter(Boolean).filter(isCtl);
      if (kids.length<2) continue; const rs=kids.map(k=>k.getBoundingClientRect());
      for (let i=1;i<rs.length;i++) { const a=rs[0], b=rs[i]; const sameRow=Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top) > 0.5*Math.min(a.height,b.height); if (!sameRow) continue;
        if (Math.abs(a.height-b.height)>2) { out.push(`ROWH ${Math.round(a.height)}/${Math.round(b.height)} ${name(kids[0])} + ${name(kids[i])}`); break; } } }
    // (e) text spilling out of a control
    for (const e of root.querySelectorAll('.btn, a.btn, .badge, .ds-tag, .ui-switch-chip, .list-editor-chip, .section-tabs a')) { if (!vis(e)) continue; const s2=getComputedStyle(e); if (s2.overflow==='hidden' || s2.textOverflow==='ellipsis') continue;
      if (e.scrollWidth > e.clientWidth + 2) out.push(`OVERFLOW ${e.scrollWidth}>${e.clientWidth} ${name(e)}`); }
    // (f) box padding and box-to-box gap: 16 (DESIGN_SYSTEM.md §1)
    const cards=[...root.querySelectorAll('.card, .settings-card, .ds-card, .edit-section, .stat, .home-panel')].filter(c=>vis(c) && !c.closest('.ui-modal-head'));
    for (const c of cards) { const s3=getComputedStyle(c); if (parseFloat(s3.borderTopWidth)===0) continue; if (c.matches('[class*="flush"], details')) continue;
      const pads=[s3.paddingTop,s3.paddingRight,s3.paddingBottom,s3.paddingLeft].map(parseFloat); if (pads.some(v=>Math.abs(v-16)>0.5)) out.push(`PAD ${pads.join('/')} ${name(c)}`); }
    for (const a of cards) { const ra=a.getBoundingClientRect(); if (parseFloat(getComputedStyle(a).borderTopWidth)===0) continue;
      let best=null; for (const b of cards) { if (a===b||a.contains(b)||b.contains(a)) continue; const rb=b.getBoundingClientRect(); const ov=Math.min(ra.right,rb.right)-Math.max(ra.left,rb.left); const g=rb.top-ra.bottom; if (ov>0.5*Math.min(ra.width,rb.width) && g>=-1 && g<60 && (!best||g<best[0])) best=[g,b]; }
      if (best && Math.abs(best[0]-16)>1 && a.parentElement===best[1].parentElement) out.push(`GAP ${best[0].toFixed(0)} ${name(a)} ↕ ${name(best[1])}`); }
    return [...new Set(out)];
  });
  for (const f of found) results.push(`${where} | ${f}`);
}
(async()=>{ const b=await chromium.launch(); const mob=width<900;
 const ctx=await b.newContext({viewport:{width, height:mob?844:1000}, isMobile:mob, hasTouch:mob}); const p=await ctx.newPage();
 await p.goto(BASE+'/login'); await p.fill('input[name=username]', args.user || 'admin'); await p.fill('input[name=password]', args.pass || ''); await p.press('input[name=password]','Enter'); await p.waitForTimeout(1500);
 const queue=[...SEED], seen=new Set();
 while (queue.length) { const u=queue.shift(); if (seen.has(u)) continue; seen.add(u);
   const resp=await p.goto(BASE+u).catch(()=>null); if (!resp || resp.status()>=400) continue; await p.waitForTimeout(450);
   const here=new URL(p.url()); if (here.pathname==='/login') continue;
   // sub-views: same-path links with a query (tabs, settings sections)
   const subs=await p.evaluate(()=>[...document.querySelectorAll('#main a[href]')].map(a=>a.getAttribute('href')).filter(h=>h && /^\/[^#]*\?/.test(h) && !/export|csv|download|delete|logout/.test(h)));
   for (const s of subs) { const su=new URL(s, BASE); if (su.pathname===here.pathname && !seen.has(su.pathname+su.search) && seen.size<260) queue.push(su.pathname+su.search); }
   await scan(p, u);
   if (withModals) { const openers=await p.$$('[data-modal-open]'); const done=new Set();
     for (const o of openers) { try { const id=await o.getAttribute('data-modal-open'); if (done.has(id) || !(await o.isVisible())) continue; done.add(id);
       await o.click({timeout:1500}); await p.waitForTimeout(450); await scan(p, `${u} #${id}`); await p.keyboard.press('Escape'); await p.waitForTimeout(250);
       await p.evaluate(()=>document.querySelectorAll('.ui-modal.open').forEach(m=>m.classList.remove('open'))); } catch(e){} } }
 }
 for (const r of results) console.log(r);
 console.log(`ui-audit ${width}px: ${seen.size} pages, ${results.length} findings`);
 process.exitCode = results.length ? 1 : 0;
 await b.close(); })();
