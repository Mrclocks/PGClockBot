  (function(){
    function csrfToken() {
      var meta = document.querySelector('meta[name="csrf-token"]');
      if (meta && meta.content) return meta.content;
      var m = document.cookie.match(/(?:^|; )csrf=([^;]*)/);
      return m ? decodeURIComponent(m[1]) : '';
    }
    function ensureCsrfField(form) {
      if (!form || form.tagName !== 'FORM') return;
      if (form.method && form.method.toUpperCase() === 'GET') return;
      var tok = csrfToken();
      if (!tok) return;
      var existing = form.querySelector('input[name="csrf_token"]');
      if (existing) {
        existing.value = tok;
        return;
      }
      var input = document.createElement('input');
      input.type = 'hidden';
      input.name = 'csrf_token';
      input.value = tok;
      form.appendChild(input);
    }
    function collectFormFields(form) {
      var out = [];
      if (!form || !form.elements) return out;
      Array.prototype.forEach.call(form.elements, function (el) {
        if (!el || !el.name || el.disabled) return;
        var tag = (el.tagName || '').toLowerCase();
        if (tag !== 'input' && tag !== 'select' && tag !== 'textarea') return;
        var type = (el.type || 'text').toLowerCase();
        if (type === 'submit' || type === 'button' || type === 'image' || type === 'reset' || type === 'file') return;
        if ((type === 'checkbox' || type === 'radio') && !el.checked) return;
        out.push([el.name, el.value]);
      });
      return out;
    }
    /**
     * Submit via a temporary <form> on document.body.
     *
     * Root cause of «علت حذف الزامی است» even after typing a reason from عملیات:
     * panelConfirm calls closeRowActions(), which un-ports the kebab menu so CSS
     * `.row-actions-menu:not(.is-ported) { display:none !important; width:0; … }`
     * hides the originating delete form. Native form.submit() from that inert
     * menu drops fields on mobile WebKit — reason never reaches the server.
     * Building a body-level form with explicit overrides (reason from JS memory)
     * makes the POST independent of kebab/edit-modal visibility.
     */
    function armPanelNavClock() {
      /* Shared arm for fetch/POST paths that never fire a navigational submit
         event (confirm → submitFormPost, kebab POST). Safe no-op if missing. */
      var clock = document.getElementById('panel-nav-clock');
      if (!clock) return;
      clock.hidden = false;
      clock.setAttribute('aria-hidden', 'false');
      try { void clock.offsetWidth; } catch (_) {}
    }
    function disarmPanelNavClock() {
      var clock = document.getElementById('panel-nav-clock');
      if (!clock) return;
      /* Keep clock if shell-first widgets are still loading. */
      if (document.documentElement.getAttribute('data-panel-widgets-pending') === '1') return;
      var dash = document.getElementById('home-dash') || document.getElementById('pg-dash');
      if (dash && dash.getAttribute('aria-busy') === 'true' && !dash.firstElementChild) return;
      clock.hidden = true;
      clock.setAttribute('aria-hidden', 'true');
    }
    window.panelArmNavClock = armPanelNavClock;
    window.panelDisarmNavClock = disarmPanelNavClock;

    function submitFormPost(form, overrides) {
      if (!form) return;
      overrides = overrides || {};
      /* Real mutation is about to leave the page (fetch → assign/reload). Arm
         here — not on the earlier data-confirm submit that only opens the modal. */
      armPanelNavClock();
      var action = form.getAttribute('action') || form.action || window.location.href;
      var method = (form.getAttribute('method') || form.method || 'post').toLowerCase();
      if (method !== 'get') method = 'post';
      /* Preserve repeated keys (bulk `ids=1&ids=2&…`). A plain object would
         keep only the last value and silently delete a single row. */
      var pairs = collectFormFields(form).slice();
      var overrideKeys = {};
      Object.keys(overrides).forEach(function (k) {
        if (overrides[k] == null) return;
        overrideKeys[k] = true;
      });
      if (Object.keys(overrideKeys).length) {
        pairs = pairs.filter(function (pair) { return !overrideKeys[pair[0]]; });
        Object.keys(overrideKeys).forEach(function (k) {
          var raw = overrides[k];
          if (Array.isArray(raw)) {
            raw.forEach(function (v) {
              pairs.push([k, v == null ? '' : String(v)]);
            });
          } else {
            pairs.push([k, String(raw)]);
          }
        });
      }
      var tok = csrfToken();
      if (tok) {
        pairs = pairs.filter(function (pair) { return pair[0] !== 'csrf_token'; });
        pairs.push(['csrf_token', tok]);
      }

      /* v10.1.16 used a display:none <form>.submit() — on mobile WebKit that
         STILL dropped fields (same class of bug as the kebab menu). Build the
         body as an explicit string and POST via fetch so reason cannot vanish. */
      var body = new URLSearchParams();
      pairs.forEach(function (pair) {
        body.append(pair[0], pair[1] == null ? '' : String(pair[1]));
      });

      function fallbackFormSubmit() {
        var tmp = document.createElement('form');
        tmp.method = method;
        tmp.action = action;
        /* Must NOT use display:none — WebKit may omit fields on submit. */
        tmp.setAttribute('aria-hidden', 'true');
        tmp.style.cssText = 'position:fixed;left:-9999px;top:0;width:1px;height:1px;opacity:0;overflow:hidden;';
        pairs.forEach(function (pair) {
          var input = document.createElement('input');
          input.type = 'hidden';
          input.name = pair[0];
          input.value = pair[1] == null ? '' : String(pair[1]);
          tmp.appendChild(input);
        });
        document.body.appendChild(tmp);
        tmp.submit();
      }

      if (method === 'get' || typeof window.fetch !== 'function') {
        fallbackFormSubmit();
        return;
      }

      window.fetch(action, {
        method: 'POST',
        body: body,
        credentials: 'same-origin',
        redirect: 'follow',
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8',
          'Accept': 'text/html,application/xhtml+xml',
          'X-CSRF-Token': tok || ''
        }
      }).then(function (res) {
        /* Never location.assign() the POST action URL on failure: those routes
           are POST-only (/pg/admins/{u}/delete, /plans/{id}/delete, …). A GET
           navigation there renders the branded 405 «Method Not Allowed» page. */
        if (!res) {
          window.location.reload();
          return;
        }
        if (res.ok || res.redirected) {
          if (res.url) {
            window.location.assign(res.url);
            return;
          }
          window.location.reload();
          return;
        }
        return res.text().then(function (html) {
          if (html && /<html[\s>]/i.test(html)) {
            document.open();
            document.write(html);
            document.close();
            return;
          }
          window.location.reload();
        }).catch(function () {
          window.location.reload();
        });
      }).catch(function () {
        fallbackFormSubmit();
      });
    }
    /* form.submit() skips the submit event — always stamp CSRF before programmatic posts.
       Prefer body-level submitFormPost over in-place form.submit() so kebab/display:none
       forms still deliver reason/csrf (delete-from-ops bug). */
    function submitFormWithCsrf(form) {
      if (!form) return;
      submitFormPost(form, {});
    }
    window.panelEnsureCsrfField = ensureCsrfField;
    window.panelSubmitForm = submitFormWithCsrf;
    window.panelSubmitFormPost = submitFormPost;
    document.addEventListener('submit', function (e) {
      ensureCsrfField(e.target);
    }, true);
    /* Kebab/row-action forms without data-confirm still native-submit. If the
       menu un-ports (display:none) mid-click, WebKit can drop fields — same
       class as delete-reason. Always POST those via panelSubmitFormPost. */
    document.addEventListener('submit', function (e) {
      const form = e.target;
      if (!(form instanceof HTMLFormElement)) return;
      if (form.hasAttribute('data-confirm')) return;
      if (form.id === 'confirm-form' || form.id === 'form-inbox-dismiss') return;
      if (form.dataset.kebabPostSkip === '1') {
        delete form.dataset.kebabPostSkip;
        return;
      }
      if (!form.closest('.row-actions-menu, .row-actions')) return;
      if (typeof window.panelSubmitFormPost !== 'function') return;
      e.preventDefault();
      e.stopPropagation();
      window.panelSubmitFormPost(form, {});
    }, true);
    var _fetch = window.fetch;
    if (typeof _fetch === 'function') {
      window.fetch = function (input, init) {
        init = init || {};
        var method = (init.method || 'GET').toUpperCase();
        if (method !== 'GET' && method !== 'HEAD') {
          var headers = new Headers(init.headers || {});
          if (!headers.has('X-CSRF-Token')) {
            var tok = csrfToken();
            if (tok) headers.set('X-CSRF-Token', tok);
          }
          if (!headers.has('Accept')) headers.set('Accept', 'application/json');
          init.headers = headers;
          if (init.credentials == null) init.credentials = 'same-origin';
        }
        return _fetch.call(this, input, init);
      };
      /** Canonical panel mutation helper — always CSRF + JSON Accept + credentials. */
      window.panelFetch = function (url, init) {
        init = init || {};
        init.credentials = init.credentials || 'same-origin';
        init.headers = new Headers(init.headers || {});
        if (!init.headers.has('Accept')) init.headers.set('Accept', 'application/json');
        return window.fetch(url, init);
      };
      window.panelReadJson = async function (res) {
        var text = await res.text();
        var data = null;
        try { data = text ? JSON.parse(text) : null; } catch (_) { data = null; }
        if (!res.ok) {
          var err = (data && (data.error || data.message)) || (
            res.status === 403
              ? 'نشست امنیتی منقضی شده؛ صفحه را تازه کنید و دوباره تلاش کنید.'
              : ('خطای سرور (' + res.status + ')')
          );
          return { ok: false, error: err, status: res.status };
        }
        if (!data || typeof data !== 'object') {
          return { ok: false, error: 'پاسخ نامعتبر از سرور', status: res.status };
        }
        return data;
      };
    }

    const side = document.getElementById('sidebar');
    const btn = document.getElementById('menu-toggle');
    const back = document.getElementById('side-backdrop');
    function setOpen(v, instant){
      if (!side) return;
      var open = !!v;
      if (instant) {
        side.classList.add('side-nav-closing');
        void side.offsetWidth;
      }
      side.classList.toggle('open', open);
      if (back) {
        /* Class-only (no hidden/display flip) so opacity can fade on the compositor. */
        back.classList.toggle('show', open);
        back.setAttribute('aria-hidden', open ? 'false' : 'true');
      }
      document.body.classList.toggle('nav-open', open);
      if (btn) btn.setAttribute('aria-expanded', open ? 'true' : 'false');
      /* Ported ui-select / kebab menus sit at z-index 5000; drawer is ~50.
         Close them when opening so they cannot paint over the sidebar. */
      if (open) {
        try { if (typeof closeUiSelects === 'function') closeUiSelects(); } catch (_) {}
        try { if (typeof closeRowActions === 'function') closeRowActions(); } catch (_) {}
      }
      if (instant) {
        /* Re-enable transition after paint so the next manual open still animates */
        requestAnimationFrame(function () {
          requestAnimationFrame(function () {
            side.classList.remove('side-nav-closing');
          });
        });
      }
    }
    setOpen(false);
    if (btn) {
      /* click alone waits for iOS momentum scroll to settle on a fixed topbar.
         pointerup opens immediately; click still covers mouse / keyboard. */
      var ignoreClick = false;
      function toggleMenu(e) {
        e.preventDefault();
        e.stopPropagation();
        setOpen(!side.classList.contains('open'));
      }
      btn.addEventListener('pointerup', function (e) {
        if (e.pointerType === 'mouse') return;
        if (e.button != null && e.button !== 0) return;
        ignoreClick = true;
        toggleMenu(e);
        setTimeout(function () { ignoreClick = false; }, 400);
      });
      btn.addEventListener('click', function (e) {
        if (ignoreClick) {
          e.preventDefault();
          e.stopPropagation();
          return;
        }
        toggleMenu(e);
      });
    }
    if (back) back.addEventListener('click', function () { setOpen(false); });
    window.addEventListener('pageshow', function () {
      setOpen(false, true);
    });

    /* Lightweight nav clock — fixed, pointer-events:none, no layout reflow.
       Show IMMEDIATELY on arm (no CSS animation-delay — WebKit freezes it during
       MPA nav; no setTimeout delay — slow navigations often unload before 140ms
       if the click→nav path is busy). Fast-nav flicker is preferable to never
       painting. Matte is CSS on .panel-nav-clock.

       For real internal navigations we preventDefault, close the drawer, arm the
       clock, then location.assign on the next frame. Closing the drawer in the
       same turn as a default click (off-screen slide + pointer-events:none on
       .side) cancelled or skipped the paint on WebKit — Plans and other heavy
       MPA targets often never showed the clock. Explicit assign after one rAF
       keeps closed+loading together for PWA/Safari and guarantees a paint. */
    (function () {
      const clock = document.getElementById('panel-nav-clock');
      if (!clock) return;
      /* Shell-first /home|/pg|/reseller home: defer script may fire
         panel-widgets-loading before this listener exists. Track pending via
         DOM flag + empty aria-busy mount so pageshow cannot hide the clock
         before /body swaps in. */
      function shellWidgetsPending() {
        if (document.documentElement.getAttribute('data-panel-widgets-pending') === '1') {
          return true;
        }
        const dash = document.getElementById('home-dash') || document.getElementById('pg-dash');
        return !!(dash && dash.getAttribute('aria-busy') === 'true' && !dash.firstElementChild);
      }
      let widgetsPending = shellWidgetsPending() ? 1 : 0;
      function arm() {
        clock.hidden = false;
        clock.setAttribute('aria-hidden', 'false');
        void clock.offsetWidth;
      }
      function disarm() {
        if (widgetsPending > 0 || shellWidgetsPending()) return;
        clock.hidden = true;
        clock.setAttribute('aria-hidden', 'true');
      }
      function disarmAfterPaint() {
        requestAnimationFrame(function () {
          requestAnimationFrame(disarm);
        });
      }
      if (widgetsPending > 0) arm();
      function sameDocumentNav(url) {
        return url.origin === location.origin
          && url.pathname === location.pathname
          && url.search === location.search;
      }
      function isRealInternalNavAnchor(a) {
        if (!a || a.target && a.target !== '' && a.target !== '_self') return false;
        if (a.hasAttribute('download') || a.getAttribute('aria-disabled') === 'true') return false;
        const raw = a.getAttribute('href');
        if (!raw || raw.charAt(0) === '#' || raw.indexOf('javascript:') === 0) return false;
        let url;
        try { url = new URL(raw, location.href); } catch (_) { return false; }
        if (url.protocol !== 'http:' && url.protocol !== 'https:') return false;
        if (url.origin !== location.origin) return false;
        if (sameDocumentNav(url) && url.hash) return false;
        return true;
      }
      document.addEventListener('click', function (e) {
        if (e.defaultPrevented || e.button !== 0) return;
        if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
        const a = e.target && e.target.closest ? e.target.closest('a[href]') : null;
        if (!a) return;
        const inSide = side && side.contains(a);
        if (!isRealInternalNavAnchor(a)) {
          if (inSide) setOpen(false, true);
          return;
        }
        e.preventDefault();
        if (inSide) setOpen(false, true);
        arm();
        var href = a.href;
        requestAnimationFrame(function () {
          window.location.assign(href);
        });
      }, true);
      document.addEventListener('submit', function (e) {
        if (e.defaultPrevented) return;
        const form = e.target;
        if (!form || form.tagName !== 'FORM') return;
        if (form.target && form.target !== '' && form.target !== '_self') return;
        if (form.hasAttribute('data-no-nav-clock')) return;
        /* data-confirm forms only open the shared modal — real POST is via
           submitFormPost after OK. Arming here left the clock stuck on cancel. */
        if (form.hasAttribute('data-confirm') || form.id === 'confirm-form') return;
        if (side && side.contains(form)) setOpen(false, true);
        arm();
      }, true);
      window.addEventListener('pageshow', function () {
        if (widgetsPending > 0 || shellWidgetsPending()) {
          widgetsPending = Math.max(widgetsPending, 1);
          arm();
          return;
        }
        disarm();
      });
      window.addEventListener('pagehide', function () {
        widgetsPending = 0;
        document.documentElement.removeAttribute('data-panel-widgets-pending');
        clock.hidden = true;
        clock.setAttribute('aria-hidden', 'true');
      });
      window.addEventListener('popstate', function () {
        if (widgetsPending > 0 || shellWidgetsPending()) {
          arm();
          return;
        }
        disarm();
      });
      /* Shell-first home/PG: keep the clock up until /body swaps widgets in. */
      document.addEventListener('panel-widgets-loading', function () {
        widgetsPending = Math.max(widgetsPending, 1);
        document.documentElement.setAttribute('data-panel-widgets-pending', '1');
        arm();
      });
      document.addEventListener('panel-widgets-ready', function () {
        widgetsPending = 0;
        document.documentElement.removeAttribute('data-panel-widgets-pending');
        disarmAfterPaint();
      });
    })();

    /* Permanent no-zoom: keep focused text controls at ≥16px even if CSS regresses */
    (function () {
      const MIN = 16;
      const SKIP = new Set(['checkbox', 'radio', 'range', 'file', 'hidden', 'submit', 'button', 'reset', 'image']);
      function enforce(el) {
        if (!el || !el.style) return;
        const type = (el.getAttribute('type') || '').toLowerCase();
        if (SKIP.has(type)) return;
        const cs = window.getComputedStyle(el);
        const px = parseFloat(cs.fontSize) || 0;
        if (px > 0 && px < MIN) el.style.fontSize = MIN + 'px';
      }
      /* Enforce on focus only — avoids getComputedStyle scan of every control on load */
      document.addEventListener('focusin', (e) => {
        const t = e.target;
        if (!t) return;
        if (t.matches && t.matches('input, select, textarea, [contenteditable="true"]')) enforce(t);
      }, true);
    })();

    /* Theme: system | light | dark */
    (function(){
      const KEY = 'panel-theme';
      const root = document.documentElement;
      const wrap = document.getElementById('side-theme');
      const toggle = document.getElementById('theme-toggle');
      const menu = document.getElementById('theme-menu');
      const label = document.getElementById('theme-label');
      const ico = document.getElementById('theme-ico');
      const labels = { system: 'تم · سیستم', light: 'تم · روشن', dark: 'تم · تیره' };
      const icons = {
        system: '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>',
        light: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
        dark: '<path d="M21 14.5A8.5 8.5 0 0 1 9.5 3 7 7 0 1 0 21 14.5z"/>'
      };
      function resolve(pref){
        if (pref === 'light' || pref === 'dark') return pref;
        return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
      }
      function apply(pref){
        pref = pref || localStorage.getItem(KEY) || 'system';
        if (pref !== 'light' && pref !== 'dark') pref = 'system';
        localStorage.setItem(KEY, pref);
        root.setAttribute('data-theme', resolve(pref));
        if (label) label.textContent = labels[pref] || labels.system;
        if (ico) ico.innerHTML = icons[pref] || icons.system;
        if (menu) {
          menu.querySelectorAll('[data-theme-pref]').forEach(b => {
            b.classList.toggle('active', b.getAttribute('data-theme-pref') === pref);
          });
        }
        const dark = resolve(pref) === 'dark';
        const metaColor = document.getElementById('meta-theme-color');
        const metaScheme = document.getElementById('meta-color-scheme');
        if (metaColor) metaColor.setAttribute('content', dark ? '#09090b' : '#fafafa');
        if (metaScheme) metaScheme.setAttribute('content', dark ? 'dark' : 'light');
      }
      function setMenu(open){
        if (!menu || !toggle) return;
        menu.hidden = !open;
        toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
        wrap && wrap.classList.toggle('open', open);
      }
      apply();
      if (toggle) {
        toggle.addEventListener('click', (e) => {
          e.stopPropagation();
          setMenu(menu && menu.hidden);
        });
      }
      if (menu) {
        menu.addEventListener('click', (e) => {
          const b = e.target.closest('[data-theme-pref]');
          if (!b) return;
          e.preventDefault();
          apply(b.getAttribute('data-theme-pref'));
          setMenu(false);
        });
      }
      document.addEventListener('click', () => setMenu(false));
      document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') setMenu(false);
      });
      try {
        window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
          if ((localStorage.getItem(KEY) || 'system') === 'system') apply('system');
        });
      } catch (e) {}
    })();

    function markScrollable(el){
      if (!el) return;
      const can = el.scrollWidth > el.clientWidth + 4;
      el.classList.toggle('is-scrollable', can);
    }
    function refreshForceKebab(){
      /* Prefer «عملیات» whenever multiple action controls share a cell —
         prevents buttons stacking/overlapping (e.g. نقش + ویرایش + حذف).
         Default CSS is kebab-stable; only opt into .allow-inline-actions when
         a single short action fits — avoids FOUC / row-height jumps. */
      document.querySelectorAll('.table-wrap').forEach(el => {
        let need = false;
        el.querySelectorAll('.row-actions-menu').forEach(menu => {
          if (need) return;
          const items = [...menu.children].filter((n) => n.nodeType === 1);
          if (items.length > 1) {
            need = true;
            return;
          }
          if (menu.querySelector('select, .ui-select')) {
            need = true;
          }
        });
        if (!need) {
          /* Probe inline without leaving a permanent layout thrash when kebab is required */
          el.classList.add('allow-inline-actions');
          el.classList.remove('force-kebab');
          need = el.scrollWidth > el.clientWidth + 2;
          if (!need) {
            el.querySelectorAll('.row-actions-menu').forEach(menu => {
              if (need) return;
              if (menu.scrollWidth > menu.clientWidth + 2) need = true;
              else if (menu.offsetHeight > 44) need = true;
            });
          }
        }
        el.classList.toggle('force-kebab', need);
        el.classList.toggle('allow-inline-actions', !need);
      });
    }
    function refreshHScrollMarks(){
      document.querySelectorAll('.table-wrap, .section-tabs').forEach(markScrollable);
    }
    function refreshHScroll(){
      if (!document.querySelector('.table-wrap, .section-tabs')) return;
      refreshForceKebab();
      refreshHScrollMarks();
    }
    const _hScrollRoots = document.querySelectorAll('.table-wrap, .section-tabs');
    if (_hScrollRoots.length) {
      refreshHScroll();
      window.addEventListener('resize', refreshHScroll);
      if (window.ResizeObserver) {
        const ro = new ResizeObserver(refreshHScrollMarks);
        _hScrollRoots.forEach(el => ro.observe(el));
      }
    }

    /* Keep active settings/section tab visible in horizontal mobile scroll */
    function scrollActiveTabIntoView(){
      const tabs = document.querySelectorAll('.section-tabs');
      if (!tabs.length) return;
      tabs.forEach(nav => {
        const items = [...nav.querySelectorAll('a, .tab-btn')];
        const active = nav.querySelector('a.active, .tab-btn.active');
        if (!active) return;
        /* First tab active: pin to inline-start. Centering/nearest can leave a
           black gutter beside the first RTL pill on first paint. */
        if (items[0] === active) {
          try { nav.scrollLeft = 0; } catch (e) {}
          return;
        }
        const nr = nav.getBoundingClientRect();
        const ar = active.getBoundingClientRect();
        if (ar.left >= nr.left - 2 && ar.right <= nr.right + 2) return;
        if (typeof active.scrollIntoView !== 'function') return;
        try {
          active.scrollIntoView({ inline: 'nearest', block: 'nearest', behavior: 'instant' in window ? 'instant' : 'auto' });
        } catch (_) {
          try { active.scrollIntoView(false); } catch (e) {}
        }
      });
    }
    if (document.querySelector('.section-tabs')) {
      scrollActiveTabIntoView();
      requestAnimationFrame(scrollActiveTabIntoView);
    }

    /* Mobile row action menus (three-dot) — ported overlay, corner-aligned, inward */
    const rowMenuHomes = new WeakMap();
    function restoreRowMenu(menu){
      if (!menu) return;
      const home = rowMenuHomes.get(menu);
      menu.classList.remove('is-ported');
      /* Clear hidden — desktop inline menus must stay visible; kebab CSS hides in-cell */
      menu.hidden = false;
      menu.removeAttribute('aria-hidden');
      [
        'top', 'left', 'right', 'bottom', 'maxHeight', 'height', 'overflow',
        'visibility', 'position', 'width', 'minWidth', 'zIndex',
      ].forEach((p) => { menu.style[p] = ''; });
      if (home && home.parent) {
        if (home.next && home.next.parentNode === home.parent) {
          home.parent.insertBefore(menu, home.next);
        } else {
          home.parent.appendChild(menu);
        }
      }
    }
    function closeRowActions(){
      document.querySelectorAll('.row-actions.open').forEach(el => {
        el.classList.remove('open');
        el.querySelectorAll('.row-actions-toggle').forEach(btn => {
          btn.setAttribute('aria-expanded', 'false');
        });
      });
      document.querySelectorAll('.row-actions-menu.is-ported').forEach(restoreRowMenu);
    }
    function placeRowMenu(wrap){
      if (!wrap) return;
      /* Prefer the visible toggle (label on desktop force-kebab, icon on mobile) */
      let anchor = null;
      wrap.querySelectorAll('.row-actions-toggle').forEach(t => {
        const st = window.getComputedStyle(t);
        if (st.display !== 'none' && st.visibility !== 'hidden') anchor = t;
      });
      if (!anchor) anchor = wrap.querySelector('.row-actions-toggle');
      let menu = wrap.querySelector('.row-actions-menu');
      if (!menu && wrap.dataset.raId) {
        menu = document.querySelector('.row-actions-menu.is-ported[data-owner="' + wrap.dataset.raId + '"]');
      }
      if (!anchor || !menu) return;
      if (!wrap.dataset.raId) {
        wrap.dataset.raId = 'ra-' + Math.random().toString(36).slice(2, 9);
      }
      if (!rowMenuHomes.has(menu)) {
        rowMenuHomes.set(menu, { parent: menu.parentNode, next: menu.nextSibling });
      }
      menu.dataset.owner = wrap.dataset.raId;
      /* Hide until final coords are set — avoids 0,0 flash and in-cell ghost paint.
         Keep `hidden` until after body-port + is-ported so in-cell never paints chrome. */
      menu.style.visibility = 'hidden';
      menu.hidden = true;
      document.body.appendChild(menu);
      menu.classList.add('is-ported');
      menu.hidden = false;
      menu.setAttribute('aria-hidden', 'false');
      /* Above open modals (4000–4600+) — same floor as ported ui-select menus.
         Without this, kebab actions inside categories/addons modals paint behind
         the dialog and look “broken”. */
      let raZ = 5000;
      document.querySelectorAll('.ui-modal.open').forEach((m) => {
        const z = parseInt(m.style.zIndex || window.getComputedStyle(m).zIndex, 10);
        if (!isNaN(z) && z + 50 > raZ) raZ = z + 50;
      });
      menu.style.setProperty('z-index', String(raZ), 'important');

      const gap = 8; /* --space-1 */
      const pad = 8;
      const rect = anchor.getBoundingClientRect();
      /* Full natural height — never scroll / clamp with max-height */
      menu.style.top = '0px';
      menu.style.left = '0px';
      menu.style.right = 'auto';
      menu.style.bottom = 'auto';
      menu.style.maxHeight = 'none';
      menu.style.height = 'auto';
      menu.style.overflow = 'visible';
      const mw = Math.max(menu.offsetWidth || 168, 168);
      const mh = menu.offsetHeight || 120;
      /* getBoundingClientRect = visual viewport; position:fixed = layout viewport.
         Convert with visualViewport.offset* so the menu stays glued to the toggle
         when the soft keyboard resizes/shifts the visual viewport. */
      const vv = window.visualViewport;
      const vvH = (vv && vv.height) || window.innerHeight;
      const vvW = (vv && vv.width) || window.innerWidth;
      const offTop = (vv && typeof vv.offsetTop === 'number') ? vv.offsetTop : 0;
      const offLeft = (vv && typeof vv.offsetLeft === 'number') ? vv.offsetLeft : 0;

      const spaceBelow = Math.max(0, vvH - rect.bottom - gap - pad);
      const spaceAbove = Math.max(0, rect.top - gap - pad);
      let openDown;
      if (spaceBelow >= mh) openDown = true;
      else if (spaceAbove >= mh) openDown = false;
      else openDown = spaceBelow >= spaceAbove;

      /* Horizontal: prefer toggle's inline-end; keep within visual width */
      let leftVis = rect.left;
      if (leftVis + mw > vvW - pad) leftVis = rect.right - mw;
      if (leftVis < pad) leftVis = pad;
      if (leftVis + mw > vvW - pad) leftVis = Math.max(pad, vvW - pad - mw);

      let topVis;
      if (openDown) {
        topVis = rect.bottom + gap;
      } else {
        topVis = rect.top - gap - mh;
      }
      /* Stay attached to the toggle — never pin to the screen edge away from it */

      menu.style.top = Math.round(topVis + offTop) + 'px';
      menu.style.left = Math.round(leftVis + offLeft) + 'px';
      menu.style.right = 'auto';
      menu.style.bottom = 'auto';
      menu.style.maxHeight = 'none';
      menu.style.overflow = 'visible';
      menu.style.visibility = '';
    }

    /* Custom selects — replace native OS pickers with panel-styled menus.
       Menu is ported to document.body (like kebab) so modal overflow/transform
       cannot pin it flush to the toggle. Gap + smart up/down stay in JS. */
    const uiSelectMenuHomes = new WeakMap();
    const UI_SELECT_GAP = 8; /* --space-1 — small air between toggle and menu */
    const UI_SELECT_PAD = 8;

    function restoreUiSelectMenu(menu){
      if (!menu) return;
      const home = uiSelectMenuHomes.get(menu);
      menu.classList.remove('is-fixed-pos', 'is-ported');
      [
        'position', 'inset', 'inset-inline', 'inset-inline-start', 'inset-inline-end',
        'top', 'left', 'right', 'bottom', 'width', 'min-width', 'max-height', 'z-index',
        'overflow', 'overflow-x', 'overflow-y', '-webkit-overflow-scrolling',
      ].forEach((p) => menu.style.removeProperty(p));
      if (home && home.parent) {
        if (home.next && home.next.parentNode === home.parent) {
          home.parent.insertBefore(menu, home.next);
        } else {
          home.parent.appendChild(menu);
        }
      }
    }

    function placeUiSelectMenu(wrap){
      if (!wrap) return;
      const toggle = wrap.querySelector('.ui-select-toggle');
      let menu = wrap.querySelector('.ui-select-menu') || wrap._portedMenu;
      if (!toggle || !menu || menu.hidden) return;

      const gap = UI_SELECT_GAP;
      const pad = UI_SELECT_PAD;
      const rect = toggle.getBoundingClientRect();
      /* getBoundingClientRect is visual-viewport-relative; position:fixed is
         layout-viewport-relative. Add visualViewport.offset* so the menu stays
         glued to the toggle when the soft keyboard is open. Shrink height to
         fit — never jump the menu to the top/bottom of the screen. */
      const vv = window.visualViewport;
      const vvW = (vv && vv.width) || window.innerWidth || document.documentElement.clientWidth;
      const vvH = (vv && vv.height) || window.innerHeight || document.documentElement.clientHeight;
      const offTop = (vv && typeof vv.offsetTop === 'number') ? vv.offsetTop : 0;
      const offLeft = (vv && typeof vv.offsetLeft === 'number') ? vv.offsetLeft : 0;

      if (!wrap.dataset.uiSelectId) {
        wrap.dataset.uiSelectId = 'us-' + Math.random().toString(36).slice(2, 9);
      }
      if (!uiSelectMenuHomes.has(menu)) {
        uiSelectMenuHomes.set(menu, { parent: menu.parentNode, next: menu.nextSibling });
      }
      wrap._portedMenu = menu;
      menu.dataset.uiSelectOwner = wrap.dataset.uiSelectId;
      if (menu.parentNode !== document.body) {
        document.body.appendChild(menu);
      }
      menu.classList.add('is-fixed-pos', 'is-ported');

      const set = (prop, val) => menu.style.setProperty(prop, val, 'important');
      set('position', 'fixed');
      set('right', 'auto');
      set('bottom', 'auto');
      set('inset-inline', 'auto');
      set('inset-inline-start', 'auto');
      set('inset-inline-end', 'auto');
      set('min-width', '0');
      set('z-index', '5000');
      set('overflow-x', 'hidden');
      set('overflow-y', 'auto');
      set('-webkit-overflow-scrolling', 'touch');

      let width = Math.max(rect.width, 120);
      let leftVis = rect.left;
      const maxW = Math.max(120, vvW - pad * 2);
      if (width > maxW) width = maxW;
      if (leftVis < pad) leftVis = pad;
      if (leftVis + width > vvW - pad) leftVis = Math.max(pad, vvW - pad - width);
      if (rect.width <= maxW && rect.left >= pad - 0.5 && rect.right <= vvW - pad + 0.5) {
        leftVis = rect.left;
        width = rect.width;
      }
      set('left', Math.round(leftVis + offLeft) + 'px');
      set('width', Math.round(width) + 'px');

      const roomBelow = Math.max(0, vvH - rect.bottom - gap - pad);
      const roomAbove = Math.max(0, rect.top - gap - pad);
      set('max-height', Math.min(280, Math.max(48, Math.max(roomBelow, roomAbove, 48))) + 'px');
      let mh = menu.offsetHeight || 120;

      let openUp;
      if (wrap.closest('.ticket-status-form, .ticket-status-actions')) {
        openUp = true;
      } else if (roomBelow >= mh) {
        openUp = false;
      } else if (roomAbove >= mh) {
        openUp = true;
      } else {
        openUp = roomAbove > roomBelow;
      }
      wrap.classList.toggle('drop-up', openUp);
      if (openUp) menu.setAttribute('data-drop-up', '1');
      else menu.removeAttribute('data-drop-up');

      if (openUp) {
        const maxH = Math.min(280, Math.max(48, roomAbove));
        set('max-height', maxH + 'px');
        mh = menu.offsetHeight || Math.min(mh, maxH);
        /* Anchor to toggle bottom-edge going up — do not clamp away from field */
        set('top', Math.round(rect.top - gap - mh + offTop) + 'px');
        set('bottom', 'auto');
      } else {
        const maxH = Math.min(280, Math.max(48, roomBelow));
        set('max-height', maxH + 'px');
        set('top', Math.round(rect.bottom + gap + offTop) + 'px');
        set('bottom', 'auto');
      }
    }

    function clearUiSelectMenuPos(wrap){
      if (!wrap) return;
      const menu = wrap.querySelector('.ui-select-menu') || wrap._portedMenu;
      if (!menu) return;
      restoreUiSelectMenu(menu);
    }
    function closeUiSelects(except){
      document.querySelectorAll('.ui-select.open').forEach(wrap => {
        if (except && wrap === except) return;
        wrap.classList.remove('open');
        wrap.classList.remove('drop-up');
        const btn = wrap.querySelector('.ui-select-toggle');
        if (btn) btn.setAttribute('aria-expanded', 'false');
        const menu = wrap.querySelector('.ui-select-menu') || wrap._portedMenu;
        if (menu) menu.hidden = true;
        clearUiSelectMenuPos(wrap);
      });
      /* Orphan ported menus (owner wrap already closed / detached) */
      document.querySelectorAll('.ui-select-menu.is-ported').forEach((menu) => {
        const id = menu.dataset.uiSelectOwner;
        const wrap = id && document.querySelector('.ui-select[data-ui-select-id="' + id + '"]');
        if (!wrap || !wrap.classList.contains('open')) {
          menu.hidden = true;
          restoreUiSelectMenu(menu);
        }
      });
    }
    function enhanceSelect(sel){
      if (!sel || sel.dataset.uiSelect === '1' || sel.multiple || sel.size > 1) return;
      if (sel.classList.contains('ui-select-native')) return;
      if (sel.closest('.ui-select')) return;
      sel.dataset.uiSelect = '1';

      const wrap = document.createElement('div');
      wrap.className = 'ui-select' + (sel.classList.contains('select-sm') || (sel.closest('.actions') && !sel.classList.contains('select-block')) ? ' ui-select-sm' : '');
      if (sel.classList.contains('users-svc-select')) wrap.classList.add('users-svc-ui');
      if (sel.disabled) wrap.classList.add('is-disabled');

      /* Build custom UI first, then park the native <select> outside any <label>.
         iOS/Android activate native pickers when a labeled <select> is tapped —
         even with opacity:0 / pointer-events:none. display:none + out-of-label
         is required to kill native menus panel-wide. */
      const hostLabel = sel.closest('label');
      const insertParent = sel.parentNode;
      insertParent.insertBefore(wrap, sel);

      const toggle = document.createElement('button');
      toggle.type = 'button';
      toggle.className = 'ui-select-toggle';
      toggle.setAttribute('aria-haspopup', 'listbox');
      toggle.setAttribute('aria-expanded', 'false');
      if (sel.disabled) toggle.disabled = true;
      const label = document.createElement('span');
      label.className = 'ui-select-label';
      toggle.appendChild(label);
      const caret = document.createElement('span');
      caret.innerHTML = '<svg class="ui-select-caret" viewBox="0 0 16 16" aria-hidden="true"><path d="M4.5 6L8 3l3.5 3"/><path d="M4.5 10L8 13l3.5-3"/></svg>';
      toggle.appendChild(caret.firstChild);
      wrap.appendChild(toggle);

      const menu = document.createElement('div');
      menu.className = 'ui-select-menu' + (sel.classList.contains('users-svc-select') ? ' users-svc-boxed' : '');
      menu.setAttribute('role', 'listbox');
      menu.hidden = true;
      wrap.appendChild(menu);

      sel.classList.add('ui-select-native');
      sel.tabIndex = -1;
      sel.setAttribute('aria-hidden', 'true');
      sel.setAttribute('data-ui-select-el', '1');
      /* Keep in form for submit/validation, but never inside <label> */
      if (hostLabel && hostLabel.contains(sel)) {
        if (sel.id && hostLabel.getAttribute('for') === sel.id) {
          hostLabel.removeAttribute('for');
        }
        hostLabel.parentNode.insertBefore(sel, hostLabel.nextSibling);
      } else {
        wrap.appendChild(sel);
      }
      wrap._nativeSelect = sel;
      /* display:none + 0×0 native <select> can be dropped from WebKit form
         payloads (same class as inbox radios / kebab delete). Mirror the
         value into a normal hidden input and strip the select's name so only
         the mirror submits. Custom UI still drives sel.value for JS. */
      if (sel.name) {
        const mirror = document.createElement('input');
        mirror.type = 'hidden';
        mirror.name = sel.name;
        mirror.value = sel.value == null ? '' : String(sel.value);
        mirror.setAttribute('data-ui-select-mirror', '1');
        if (sel.disabled) mirror.disabled = true;
        sel.setAttribute('data-ui-select-name', sel.name);
        sel.removeAttribute('name');
        (sel.parentNode || wrap).insertBefore(mirror, sel);
        wrap._valueMirror = mirror;
      }

      function syncDisabled(){
        const off = !!sel.disabled;
        wrap.classList.toggle('is-disabled', off);
        toggle.disabled = off;
        if (wrap._valueMirror) wrap._valueMirror.disabled = off;
      }
      function syncMirror(){
        if (!wrap._valueMirror) return;
        wrap._valueMirror.value = sel.value == null ? '' : String(sel.value);
      }
      function syncLabel(){
        const opt = sel.options[sel.selectedIndex];
        const ph = sel.getAttribute('placeholder');
        const isEmpty = !sel.value;
        label.textContent = (!isEmpty && opt)
          ? opt.textContent
          : (ph || (opt ? opt.textContent : '—') || '—');
        label.classList.toggle('is-placeholder', isEmpty);
        const tone = (opt && opt.dataset && opt.dataset.tone)
          ? opt.dataset.tone
          : (sel.dataset.tone || '');
        if (tone) wrap.setAttribute('data-tone', tone);
        else wrap.removeAttribute('data-tone');
        /* Color-tag filter: paint the wrap with the selected tag color. */
        const tagHost = wrap.closest('.color-tag-select-wrap');
        if (tagHost) {
          const tagColor = (opt && opt.getAttribute('data-tag-color')) || '';
          if (tagColor) {
            tagHost.classList.add('is-tagged');
            tagHost.style.setProperty('--tag-color', tagColor);
          } else {
            tagHost.classList.remove('is-tagged');
            tagHost.style.removeProperty('--tag-color');
          }
        }
        menu.querySelectorAll('[role="option"]').forEach(btn => {
          btn.classList.toggle('active', btn.dataset.value === sel.value);
          btn.setAttribute('aria-selected', btn.dataset.value === sel.value ? 'true' : 'false');
        });
        syncMirror();
        syncDisabled();
        syncUsersSvcToggleAlert();
      }
      function rebuildOptions(){
        menu.innerHTML = '';
        const isUsersSvc = sel.classList.contains('users-svc-select');
        const isColorTag = sel.classList.contains('color-tag-select');
        Array.from(sel.options).forEach(opt => {
          if (opt.disabled && opt.value === '' && !opt.textContent.trim()) return;
          const btn = document.createElement('button');
          btn.type = 'button';
          btn.setAttribute('role', 'option');
          btn.dataset.value = opt.value;
          if (opt.dataset && opt.dataset.tone) btn.dataset.tone = opt.dataset.tone;
          const tagColor = opt.getAttribute('data-tag-color');
          if (tagColor) {
            btn.setAttribute('data-tag-color', tagColor);
            btn.style.setProperty('--tag-color', tagColor);
            btn.style.color = tagColor;
            btn.style.fontWeight = '600';
          }
          if (isUsersSvc) {
            const optLabel = document.createElement('span');
            optLabel.className = 'users-svc-menu-label';
            optLabel.textContent = opt.textContent;
            btn.appendChild(optLabel);
            if (opt.getAttribute('data-alert') === '1') {
              const dot = document.createElement('span');
              dot.className = 'alert-dot';
              dot.setAttribute('aria-hidden', 'true');
              btn.appendChild(dot);
            }
          } else if (isColorTag && tagColor) {
            const swatch = document.createElement('span');
            swatch.className = 'color-tag-swatch';
            swatch.style.setProperty('--tag-color', tagColor);
            swatch.setAttribute('aria-hidden', 'true');
            const optLabel = document.createElement('span');
            optLabel.className = 'color-tag-menu-label';
            optLabel.textContent = opt.textContent;
            optLabel.style.color = tagColor;
            btn.appendChild(swatch);
            btn.appendChild(optLabel);
            btn.classList.add('color-tag-menu-option');
          } else {
            btn.textContent = opt.textContent;
          }
          if (opt.disabled) btn.disabled = true;
          if (opt.value === sel.value) {
            btn.classList.add('active');
            btn.setAttribute('aria-selected', 'true');
          } else {
            btn.setAttribute('aria-selected', 'false');
          }
          btn.addEventListener('click', (ev) => {
            ev.preventDefault();
            ev.stopPropagation();
            if (opt.disabled) return;
            sel.value = opt.value;
            if (opt.dataset && opt.dataset.tone) sel.dataset.tone = opt.dataset.tone;
            else delete sel.dataset.tone;
            sel.dispatchEvent(new Event('input', { bubbles: true }));
            sel.dispatchEvent(new Event('change', { bubbles: true }));
            syncLabel();
            closeUiSelects();
          });
          menu.appendChild(btn);
        });
        syncLabel();
      }
      function syncUsersSvcToggleAlert(){
        if (!sel.classList.contains('users-svc-select')) return;
        const opt = sel.options[sel.selectedIndex];
        const hasAlert = !!(opt && opt.getAttribute('data-alert') === '1');
        let dot = toggle.querySelector(':scope > .alert-dot');
        if (hasAlert) {
          if (!dot) {
            dot = document.createElement('span');
            dot.className = 'alert-dot';
            dot.setAttribute('aria-hidden', 'true');
            const caretEl = toggle.querySelector('.ui-select-caret');
            if (caretEl) toggle.insertBefore(dot, caretEl);
            else toggle.appendChild(dot);
          }
        } else if (dot) {
          dot.remove();
        }
      }
      rebuildOptions();
      toggle.addEventListener('click', (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        if (sel.disabled) return;
        const open = wrap.classList.contains('open');
        closeUiSelects();
        /* Nested select inside kebab/عملیات must not dismiss the parent menu */
        const nestedInRowMenu = !!wrap.closest('.row-actions-menu, .row-actions');
        if (!nestedInRowMenu) {
          closeRowActions();
        }
        if (!open) {
          wrap.classList.add('open');
          toggle.setAttribute('aria-expanded', 'true');
          menu.hidden = false;
          placeUiSelectMenu(wrap);
          requestAnimationFrame(() => placeUiSelectMenu(wrap));
        }
      });
      /* Stop label-associated native activation (desktop + mobile) */
      if (hostLabel) {
        hostLabel.addEventListener('mousedown', (ev) => {
          if (ev.target.closest('.ui-select') || ev.target === sel) {
            ev.preventDefault();
          }
        }, true);
        hostLabel.addEventListener('click', (ev) => {
          if (ev.target.closest('.ui-select') || ev.target === sel) {
            ev.preventDefault();
          }
        }, true);
      }
      sel.addEventListener('change', syncLabel);
      /* Always rebuild custom menu when <option> list is rewritten (e.g. plans modal
         audience → kind options). Previously only settings forms were watched, so
         dynamic selects kept showing stale labels/options. */
      const mo = new MutationObserver(() => rebuildOptions());
      mo.observe(sel, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ['disabled'] });
    }
    function enhanceAllSelects(root){
      const scope = root && root.querySelectorAll ? root : document;
      if (scope.matches && scope.matches('select')) enhanceSelect(scope);
      scope.querySelectorAll('select').forEach(enhanceSelect);
    }
    /* Reposition open menus when the soft keyboard resizes the visual viewport */
    function repositionOpenUiMenus(){
      document.querySelectorAll('.ui-select.open').forEach((wrap) => placeUiSelectMenu(wrap));
      document.querySelectorAll('.row-actions.open').forEach((wrap) => placeRowMenu(wrap));
    }
    if (window.visualViewport) {
      window.visualViewport.addEventListener('resize', repositionOpenUiMenus);
      window.visualViewport.addEventListener('scroll', repositionOpenUiMenus);
    }
    enhanceAllSelects();
    window.enhanceAllSelects = enhanceAllSelects;
    document.addEventListener('panel:dom-ready', (e) => {
      const root = e && e.detail && e.detail.root;
      enhanceAllSelects(root || document);
    });
    /* New selects injected into the DOM (fragment modals, dynamic forms) */
    try {
      const selectMo = new MutationObserver((mutations) => {
        for (const m of mutations) {
          m.addedNodes && m.addedNodes.forEach((node) => {
            if (!node || node.nodeType !== 1) return;
            if (node.matches && node.matches('select')) enhanceSelect(node);
            if (node.querySelectorAll) node.querySelectorAll('select').forEach(enhanceSelect);
          });
        }
      });
      selectMo.observe(document.documentElement, { childList: true, subtree: true });
    } catch (e) {}
    /* Belt-and-suspenders: never let a labeled enhanced select open natively */
    document.addEventListener('mousedown', (e) => {
      const t = e.target;
      if (!t || !t.closest) return;
      if (t.matches && t.matches('select.ui-select-native')) {
        e.preventDefault();
        e.stopPropagation();
      }
    }, true);
    document.addEventListener('click', (e) => {
      const t = e.target;
      if (!t || !t.closest) return;
      if (t.matches && t.matches('select.ui-select-native')) {
        e.preventDefault();
        e.stopPropagation();
      }
    }, true);
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') closeUiSelects();
    });
    /* Soft keyboard fires window resize — reposition instead of closing so the
       menu stays glued to the field inside modals. */
    window.addEventListener('resize', () => {
      if (document.querySelector('.ui-select.open, .row-actions.open')) {
        repositionOpenUiMenus();
        return;
      }
      closeUiSelects();
    });
    window.addEventListener('scroll', (e) => {
      const openSel = document.querySelector('.ui-select.open');
      const openRow = document.querySelector('.row-actions.open');
      if (!openSel && !openRow) return;
      if (e.target && e.target.closest && e.target.closest('.ui-select-menu, .row-actions-menu')) return;
      const host = openSel || openRow;
      const modal = host && host.closest('.ui-modal, .modal, dialog, [role="dialog"]');
      if (modal && e.target && (e.target === modal || modal.contains(e.target))) {
        repositionOpenUiMenus();
        return;
      }
      closeUiSelects();
      closeRowActions();
    }, true);

    document.addEventListener('click', (e) => {
      const toggle = e.target.closest('.row-actions-toggle');
      if (toggle) {
        e.preventDefault();
        e.stopPropagation();
        const wrap = toggle.closest('.row-actions');
        const open = wrap && wrap.classList.contains('open');
        closeRowActions();
        closeUiSelects();
        if (wrap && !open) {
          wrap.classList.add('open');
          wrap.querySelectorAll('.row-actions-toggle').forEach(btn => {
            btn.setAttribute('aria-expanded', 'true');
          });
          /* Port immediately so fixed menu is not trapped by .table-wrap overflow */
          placeRowMenu(wrap);
          requestAnimationFrame(() => placeRowMenu(wrap));
        }
        return;
      }
      const inRowMenu = !!(e.target.closest('.row-actions')
        || e.target.closest('.row-actions-menu')
        || e.target.closest('.ui-select-menu'));
      if (!inRowMenu) {
        closeRowActions();
      }
      if (!e.target.closest('.ui-select') && !e.target.closest('.ui-select-menu')) {
        closeUiSelects();
      }
    });
    window.addEventListener('resize', closeRowActions);
    window.addEventListener('scroll', (e) => {
      if (!document.querySelector('.row-actions.open')) return;
      /* ignore scrolls inside the open menu or a nested select popup */
      if (e.target && e.target.closest && (
        e.target.closest('.row-actions-menu') || e.target.closest('.ui-select-menu')
      )) return;
      closeRowActions();
    }, true);

    /* Lightweight modals — always render on document.body above sidebar */
    const modalHomes = new WeakMap();
    let modalScrollLockDepth = 0;
    let modalMainScrollTop = 0;
    let modalWheelGuard = null;
    let modalTouchGuard = null;

    function modalScrollRoot(modal){
      if (!modal) return null;
      const panel = modal.querySelector('.ui-modal-panel');
      if (!panel) return null;
      /* Prefer the inner scroll shell (owns rounded chrome after wrap). */
      return panel.querySelector(':scope > .ui-modal-scroll') || panel;
    }

    function ensureModalScrollShell(modal){
      const panel = modal && modal.querySelector('.ui-modal-panel');
      if (!panel || panel.dataset.scrollShell === '1') return;
      let scroll = null;
      try {
        scroll = panel.querySelector(':scope > .ui-modal-scroll');
      } catch (e) {
        scroll = Array.from(panel.children).find((c) => c.classList && c.classList.contains('ui-modal-scroll')) || null;
      }
      /* User-edit only: keep title/close outside the scrollport so the thumb
         never paints over chrome. Every other modal wraps all children (pre-#586). */
      const userEditOnly = modal && modal.id === 'modal-user-edit';
      const keepOutsideScroll = (node) => {
        if (!userEditOnly || !node || !node.classList) return false;
        return node.classList.contains('ui-modal-head');
      };
      if (!scroll) {
        scroll = document.createElement('div');
        scroll.className = 'ui-modal-scroll';
        if (userEditOnly) {
          const toMove = [];
          Array.from(panel.children).forEach((child) => {
            if (!keepOutsideScroll(child)) toMove.push(child);
          });
          toMove.forEach((child) => scroll.appendChild(child));
        } else {
          while (panel.firstChild) scroll.appendChild(panel.firstChild);
        }
        panel.appendChild(scroll);
      }
      /* Marks panel so CSS can put padding/gap on .ui-modal-scroll while the
         outer panel keeps opaque chrome + overflow:hidden corner clip. */
      panel.dataset.scrollShell = '1';
    }

    function canScrollInside(el, deltaY){
      if (!el) return false;
      const top = el.scrollTop;
      const max = el.scrollHeight - el.clientHeight;
      if (max <= 1) return false;
      if (deltaY < 0 && top > 0) return true;
      if (deltaY > 0 && top < max - 1) return true;
      return false;
    }

    function isModalInteriorScroller(modal, scroller){
      if (!modal || !scroller) return false;
      return modal.contains(scroller);
    }

    function tryScrollModalTabs(e, modal){
      /* Vertical-only wheel guard was killing horizontal tab scroll (loyalty etc.). */
      const t = e.target;
      const tabs = t && t.closest ? t.closest('.modal-section-tabs') : null;
      if (!tabs || !modal.contains(tabs)) return false;
      if (tabs.scrollWidth <= tabs.clientWidth + 1) return false;
      const dxRaw = e.deltaX || 0;
      const dyRaw = e.deltaY || 0;
      const dx = Math.abs(dxRaw) > Math.abs(dyRaw) ? dxRaw : dyRaw;
      if (!dx) return false;
      const before = tabs.scrollLeft;
      const max = tabs.scrollWidth - tabs.clientWidth;
      const next = Math.max(0, Math.min(max, before + dx));
      if (next === before) return false;
      tabs.scrollLeft = next;
      e.preventDefault();
      return true;
    }

    function findScrollableAncestor(start, modal){
      let node = start;
      while (node && node !== modal && node !== document.body) {
        if (node.nodeType === 1) {
          const style = window.getComputedStyle(node);
          const oy = style.overflowY;
          if ((oy === 'auto' || oy === 'scroll' || oy === 'overlay')
              && node.scrollHeight > node.clientHeight + 1) {
            return node;
          }
        }
        node = node.parentElement;
      }
      return modalScrollRoot(modal);
    }

    function portedOverlayScroller(target){
      /* ui-select / kebab menus are ported to document.body — outside the modal
         DOM — so modal.contains(target) is false. Without this carve-out the
         capture wheel/touch guards call preventDefault and long lists cannot scroll. */
      if (!target || !target.closest) return null;
      return target.closest('.ui-select-menu') || target.closest('.row-actions-menu');
    }

    function installModalScrollGuards(){
      if (modalWheelGuard) return;
      let touchStartY = 0;
      modalWheelGuard = (e) => {
        if (!document.body.classList.contains('modal-open')) return;
        const modal = topOpenModal();
        if (!modal) return;
        const overlay = portedOverlayScroller(e.target);
        if (overlay) {
          if (canScrollInside(overlay, e.deltaY)) return;
          e.preventDefault();
          return;
        }
        if (!modal.contains(e.target)) {
          e.preventDefault();
          return;
        }
        if (e.target === modal || (e.target.classList && e.target.classList.contains('ui-modal-backdrop'))) {
          e.preventDefault();
          return;
        }
        if (tryScrollModalTabs(e, modal)) return;
        const scroller = findScrollableAncestor(e.target, modal);
        if (canScrollInside(scroller, e.deltaY)) return;
        /* Interior edge: overscroll-behavior:contain on .ui-modal-scroll blocks chain */
        if (isModalInteriorScroller(modal, scroller)) return;
        e.preventDefault();
      };
      const onTouchStart = (e) => {
        if (!e.touches || !e.touches.length) return;
        touchStartY = e.touches[0].clientY;
      };
      modalTouchGuard = (e) => {
        if (!document.body.classList.contains('modal-open')) return;
        const modal = topOpenModal();
        if (!modal) return;
        const overlay = portedOverlayScroller(e.target);
        if (overlay) {
          if (!e.touches || !e.touches.length) return;
          const dyOverlay = touchStartY - e.touches[0].clientY;
          if (Math.abs(dyOverlay) < 1) return;
          if (canScrollInside(overlay, dyOverlay)) return;
          e.preventDefault();
          return;
        }
        if (!modal.contains(e.target)
            || e.target === modal
            || (e.target.classList && e.target.classList.contains('ui-modal-backdrop'))) {
          e.preventDefault();
          return;
        }
        /* Let native horizontal pan work on overflowing modal tab strips. */
        const tabs = e.target.closest && e.target.closest('.modal-section-tabs');
        if (tabs && modal.contains(tabs) && tabs.scrollWidth > tabs.clientWidth + 1) {
          return;
        }
        if (!e.touches || !e.touches.length) return;
        const dy = touchStartY - e.touches[0].clientY;
        if (Math.abs(dy) < 1) return;
        const scroller = findScrollableAncestor(e.target, modal);
        if (canScrollInside(scroller, dy)) return;
        if (isModalInteriorScroller(modal, scroller)) return;
        e.preventDefault();
      };
      document.addEventListener('touchstart', onTouchStart, { passive: true, capture: true });
      document.addEventListener('wheel', modalWheelGuard, { passive: false, capture: true });
      document.addEventListener('touchmove', modalTouchGuard, { passive: false, capture: true });
      modalTouchGuard._onTouchStart = onTouchStart;
    }

    function removeModalScrollGuards(){
      if (modalWheelGuard) {
        document.removeEventListener('wheel', modalWheelGuard, { capture: true });
        modalWheelGuard = null;
      }
      if (modalTouchGuard) {
        if (modalTouchGuard._onTouchStart) {
          document.removeEventListener('touchstart', modalTouchGuard._onTouchStart, { capture: true });
        }
        document.removeEventListener('touchmove', modalTouchGuard, { capture: true });
        modalTouchGuard = null;
      }
    }

    function lockPageScroll(){
      if (modalScrollLockDepth === 0) {
        const main = document.querySelector('.main');
        modalMainScrollTop = main ? main.scrollTop : 0;
        document.body.classList.add('modal-open');
        if (main) main.scrollTop = modalMainScrollTop;
      }
      modalScrollLockDepth += 1;
    }

    function unlockPageScroll(){
      if (modalScrollLockDepth <= 0) return;
      modalScrollLockDepth -= 1;
      if (modalScrollLockDepth > 0) return;
      if (document.querySelector('.ui-modal.open')) {
        modalScrollLockDepth = 1;
        return;
      }
      document.body.classList.remove('modal-open');
      /* Desktop restores .main's scrollTop because body.modal-open froze it with
         overflow:hidden. The document scroll is never locked, so it needs no
         restore — see invariant A in panel.css. */
      const main = document.querySelector('.main');
      if (main) main.scrollTop = modalMainScrollTop;
    }

    function restoreModalHome(el){
      const home = modalHomes.get(el);
      if (!home || !home.parent) return;
      if (home.next && home.next.parentNode === home.parent) {
        home.parent.insertBefore(el, home.next);
      } else {
        home.parent.appendChild(el);
      }
    }
    function ensureModalPorted(el){
      if (!el) return;
      if (!modalHomes.has(el)) {
        modalHomes.set(el, { parent: el.parentNode, next: el.nextSibling });
      }
      if (el.parentNode !== document.body) {
        document.body.appendChild(el);
      }
      ensureModalScrollShell(el);
    }
    /* Among open modals, the topmost is highest z-index, then last in DOM. */
    function topOpenModal(){
      const open = Array.from(document.querySelectorAll('.ui-modal.open'));
      if (!open.length) return null;
      let best = open[0];
      let bestZ = parseInt(best.style.zIndex || window.getComputedStyle(best).zIndex, 10) || 0;
      for (let i = 1; i < open.length; i++) {
        const m = open[i];
        const z = parseInt(m.style.zIndex || window.getComputedStyle(m).zIndex, 10) || 0;
        if (z > bestZ) {
          best = m;
          bestZ = z;
        } else if (z === bestZ && (best.compareDocumentPosition(m) & Node.DOCUMENT_POSITION_FOLLOWING)) {
          best = m;
        }
      }
      return best;
    }
    /**
     * Root cause of confirm-under-edit: edit modal is body-appended AFTER #modal-confirm,
     * both share CSS z-index 4000, so edit paints on top and the reason field is unreachable.
     * Always move the opened modal to the end of <body> and raise its inline z-index.
     */
    function bringModalToFront(el, opts){
      if (!el) return;
      ensureModalPorted(el);
      /* Re-append even when already on body — last child wins same-z paint order. */
      if (el.parentNode === document.body) {
        document.body.appendChild(el);
      }
      document.querySelectorAll('.ui-modal.open.is-front').forEach((m) => {
        if (m !== el) m.classList.remove('is-front');
      });
      let maxZ = 4000;
      document.querySelectorAll('.ui-modal.open').forEach((m) => {
        if (m === el) return;
        const z = parseInt(m.style.zIndex || window.getComputedStyle(m).zIndex, 10);
        if (!isNaN(z) && z > maxZ) maxZ = z;
      });
      /* Confirm / stacked dialogs sit clearly above edit modals (4000). */
      const stack = !!(opts && opts.stack) || el.id === 'modal-confirm';
      const base = stack ? 4600 : 4000;
      el.style.zIndex = String(Math.max(maxZ + 20, base));
      el.classList.add('is-front');
    }
    window.bringModalToFront = bringModalToFront;
    window.topOpenModal = topOpenModal;
    function prefersReducedMotion(){
      try { return window.matchMedia('(prefers-reduced-motion: reduce)').matches; } catch (e) { return false; }
    }

    function finishCloseModal(el){
      if (!el) return;
      const wasOpen = el.classList.contains('open') || el.classList.contains('is-closing');
      /* Blur before hide/restore — focus returning to a kebab/delete control that
         was un-ported (display:none) made mobile browsers scrollIntoView and
         jump the page a few hundred ms after confirm cancel. */
      try {
        const ae = document.activeElement;
        if (ae && el.contains(ae) && typeof ae.blur === 'function') ae.blur();
      } catch (_) {}
      el.hidden = true;
      el.classList.remove('open', 'is-closing', 'is-front', 'is-stack');
      try { el.style.zIndex = ''; } catch (_) {}
      restoreModalHome(el);
      /* After closing a stacked confirm, re-mark the remaining top modal. */
      const still = topOpenModal();
      if (still) still.classList.add('is-front');
      /* Drop sticky SSR form errors when closing (create/edit user modals) */
      el.querySelectorAll('.flash.err[id$="-form-err"]').forEach((err) => {
        err.hidden = true;
        err.textContent = '';
        delete err.dataset.keep;
      });
      if (typeof window.history !== 'undefined' && window.location.search.indexOf('form_err') >= 0) {
        try {
          const u = new URL(window.location.href);
          u.searchParams.delete('form_err');
          u.searchParams.delete('modal');
          u.searchParams.delete('uid');
          const next = u.pathname + (u.searchParams.toString() ? '?' + u.searchParams.toString() : '') + u.hash;
          window.history.replaceState({}, '', next);
        } catch (e) {}
      }
      if (wasOpen) unlockPageScroll();
      else if (!document.querySelector('.ui-modal.open')) {
        modalScrollLockDepth = 0;
        document.body.classList.remove('modal-open');
      }
    }

    function closeModal(el, opts){
      if (!el) return;
      const instant = !!(opts && opts.instant) || prefersReducedMotion();
      if (!el.classList.contains('open')) {
        finishCloseModal(el);
        return;
      }
      if (el.classList.contains('is-closing')) return;
      if (instant) {
        finishCloseModal(el);
        return;
      }
      el.classList.add('is-closing');
      let done = false;
      const finish = () => {
        if (done) return;
        done = true;
        el.removeEventListener('animationend', onEnd);
        finishCloseModal(el);
      };
      const onEnd = (e) => {
        if (!e || !e.target) return;
        if (e.target !== el && !(e.target.classList && e.target.classList.contains('ui-modal-panel'))) return;
        finish();
      };
      el.addEventListener('animationend', onEnd);
      setTimeout(finish, 240);
    }

    function modalEscapeHref(el){
      if (!el) return null;
      const attr = el.getAttribute('data-modal-escape-href');
      if (attr) return attr;
      const nav = el.querySelector('[data-modal-close-nav]');
      if (!nav) return null;
      return nav.getAttribute('data-modal-close-nav') || '/tickets';
    }
    function modalStripKeys(el){
      if (!el) return null;
      /* Prefer dedicated strip-keys attr on the modal root. Legacy:
         data-modal-close-strip on .ui-modal also held keys, but that made
         every inner click (tabs/inputs) soft-close the modal via closest(). */
      const raw = el.getAttribute('data-modal-strip-keys')
        || (el.classList && el.classList.contains('ui-modal')
          ? el.getAttribute('data-modal-close-strip')
          : null);
      if (!raw) return null;
      return raw.split(',').map((s) => s.trim()).filter(Boolean);
    }
    function stripQueryParams(keys){
      try {
        const u = new URL(window.location.href);
        (keys || []).forEach((k) => u.searchParams.delete(k));
        const next = u.pathname + (u.searchParams.toString() ? '?' + u.searchParams.toString() : '') + u.hash;
        window.history.replaceState({}, '', next);
      } catch (e) {}
    }
    function closeModalSoft(el){
      if (!el) return;
      const keys = modalStripKeys(el);
      closeModal(el);
      if (keys && keys.length) stripQueryParams(keys);
    }
    function openModal(id, opts){
      opts = opts || {};
      const el = (typeof id === 'string') ? document.getElementById(id) : id;
      if (!el) return;
      /* opts.stack: open on top without dismissing the caller (confirm over edit). */
      const stack = !!opts.stack;
      if (!stack) {
        document.querySelectorAll('.ui-modal.open').forEach((m) => {
          if (m !== el) closeModal(m, { instant: true });
        });
      } else {
        el.classList.add('is-stack');
      }
      /* Fresh open: clear prior form errors unless SSR keep flag is set */
      el.querySelectorAll('.flash.err[id$="-form-err"]').forEach((err) => {
        if (err.dataset.keep) return;
        err.hidden = true;
        err.textContent = '';
      });
      el.classList.remove('is-closing');
      el.hidden = false;
      el.classList.add('open');
      /* Must run AFTER .open so maxZ scans include siblings; re-appends to body end. */
      bringModalToFront(el, opts);
      lockPageScroll();
      /* Ensure every select in this modal is custom (covers late DOM / fragments) */
      enhanceAllSelects(el);
      /* Never autofocus inputs/buttons — mobile keyboards must only open on user tap. */
    }
    window.openModal = openModal;
    /* Always-on guards: catch wheel/touch even when a page sets body.modal-open itself */
    installModalScrollGuards();
    /* SSR-open modals (e.g. ticket view/create) — portal like button-opened modals */
    document.querySelectorAll('.ui-modal.open').forEach((el) => {
      ensureModalPorted(el);
      el.hidden = false;
      lockPageScroll();
      enhanceAllSelects(el);
      const thread = el.querySelector('.ticket-thread');
      if (thread) thread.scrollTop = thread.scrollHeight;
    });
    /* Pre-wrap every modal so first open already clips the scrollbar to radius. */
    document.querySelectorAll('.ui-modal').forEach((el) => ensureModalScrollShell(el));
    document.addEventListener('click', (e) => {
      const openBtn = e.target.closest('[data-modal-open]');
      if (openBtn) {
        e.preventDefault();
        const modalId = openBtn.getAttribute('data-modal-open');
        const loadUrl = openBtn.getAttribute('data-modal-load') || openBtn.getAttribute('data-edit-url');
        const loadTarget = openBtn.getAttribute('data-modal-load-target');
        const loadTitle = openBtn.getAttribute('data-edit-title') || openBtn.getAttribute('data-modal-load-title');
        try { closeRowActions(); } catch (_) {}
        try { closeUiSelects(); } catch (_) {}
        openModal(modalId);
        if (loadUrl) {
          document.dispatchEvent(new CustomEvent('panel:modal-load', {
            detail: { id: modalId, url: loadUrl, target: loadTarget, title: loadTitle, button: openBtn },
          }));
        }
        return;
      }
      const stripClose = e.target.closest('[data-modal-close-strip]');
      /* Ignore when the only match is the modal root (keys attr legacy).
         Close targets are backdrop / X button (and any explicit strip control). */
      if (stripClose && !(stripClose.classList && stripClose.classList.contains('ui-modal'))) {
        e.preventDefault();
        const modal = stripClose.closest('.ui-modal');
        closeModalSoft(modal);
        return;
      }
      const navClose = e.target.closest('[data-modal-close-nav]');
      if (navClose) {
        e.preventDefault();
        const href = navClose.getAttribute('data-modal-close-nav') || '/tickets';
        window.location.href = href;
        return;
      }
      const closer = e.target.closest('[data-modal-close]');
      if (closer) {
        closeModal(closer.closest('.ui-modal'));
      }
    });
    document.addEventListener('keydown', (e) => {
      if (e.key !== 'Escape') return;
      /* Only the topmost open modal — stacked confirm must not also dismiss edit. */
      const open = topOpenModal();
      if (!open) return;
      if (modalStripKeys(open)) {
        closeModalSoft(open);
        return;
      }
      const href = modalEscapeHref(open);
      if (href) {
        window.location.href = href;
        return;
      }
      closeModal(open);
    });

    /* Domain settings modals: inner tab buttons */
    document.addEventListener('click', (e) => {
      const tabBtn = e.target.closest('[data-modal-tabs] [data-modal-tab]');
      if (!tabBtn) return;
      const nav = tabBtn.closest('[data-modal-tabs]');
      const root = tabBtn.closest('.ui-modal-panel') || tabBtn.closest('.ui-modal');
      if (!nav || !root) return;
      e.preventDefault();
      const id = tabBtn.getAttribute('data-modal-tab');
      nav.querySelectorAll('[data-modal-tab]').forEach((btn) => {
        btn.classList.toggle('active', btn === tabBtn);
      });
      root.querySelectorAll('[data-modal-tab-panel]').forEach((panel) => {
        const match = panel.getAttribute('data-modal-tab-panel') === id;
        panel.hidden = !match;
      });
      try {
        const u = new URL(window.location.href);
        const inDomainModal = !!(
          tabBtn.closest('#modal-loyalty-settings')
          || tabBtn.closest('#modal-finance-settings')
          || tabBtn.closest('#modal-supports-settings')
          || u.searchParams.has('settings')
          || u.searchParams.has('supports')
          || u.searchParams.has('stab')
        );
        if (inDomainModal) {
          if (id === 'payment' || id === 'billing') u.searchParams.set('settings', id);
          else if (id === 'club') u.searchParams.set('settings', '1');
          else if (id === 'referral' || id === 'rules' || id === 'rewards' || id === 'tiers') {
            u.searchParams.set('settings', id);
          }
          else if (id === 'contacts') {
            u.searchParams.set('supports', '1');
            u.searchParams.set('stab', 'contacts');
          } else if (id === 'text') {
            u.searchParams.set('supports', '1');
            u.searchParams.set('stab', 'text');
          }
          const next = u.pathname + (u.searchParams.toString() ? '?' + u.searchParams.toString() : '') + u.hash;
          window.history.replaceState({}, '', next);
        }
      } catch (err) {}
    });

    /* Copy helpers (subscription links, etc.) — data-copy holds the text to copy */
    function markCopied(btn) {
      if (!btn) return;
      const idle = btn.dataset.copyIdle
        || ((btn.getAttribute('data-copy-label') || '').trim() && btn.getAttribute('data-copy-label') !== 'کپی شد'
            ? btn.getAttribute('data-copy-label')
            : null)
        || (((btn.textContent || '').trim() && (btn.textContent || '').trim() !== 'کپی شد')
            ? (btn.textContent || '').trim()
            : 'کپی');
      btn.dataset.copyIdle = idle;
      btn.textContent = 'کپی شد';
      btn.classList.add('btn-ok', 'is-copied');
      if (btn._copyTimer) clearTimeout(btn._copyTimer);
      btn._copyTimer = setTimeout(() => {
        btn.textContent = btn.dataset.copyIdle || 'کپی';
        btn.classList.remove('btn-ok', 'is-copied');
      }, 1400);
    }
    document.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-copy]');
      if (!btn) return;
      // Prefer explicit copy text; optional data-copy-from="#inputId" for input values
      e.preventDefault();
      e.stopPropagation();
      let text = (btn.getAttribute('data-copy') || '').trim();
      const fromSel = btn.getAttribute('data-copy-from');
      if (fromSel) {
        const el = document.querySelector(fromSel);
        if (el) text = (el.value != null ? el.value : (el.textContent || '')).trim();
      }
      if (!text || text === '—') {
        alert('لینکی موجود نیست');
        return;
      }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(() => markCopied(btn)).catch(() => {
          window.prompt('کپی کنید:', text);
        });
      } else {
        window.prompt('کپی کنید:', text);
      }
    });

    /* Upload box file name preview */
    document.addEventListener('change', (e) => {
      const input = e.target;
      if (!input || input.type !== 'file') return;
      const box = input.closest('.upload-box');
      if (!box) return;
      const nameEl = box.querySelector('[data-upload-name]');
      const file = input.files && input.files[0];
      if (nameEl) nameEl.textContent = file ? file.name : 'فایلی انتخاب نشده';
      box.classList.toggle('has-file', !!file);
    });

    /* Multipart forms: progress bar inside upload boxes + success toast */
    (function initUploadProgress(){
      function ensureProgress(box){
        let barWrap = box.querySelector('.upload-box-progress');
        if (!barWrap) {
          barWrap = document.createElement('span');
          barWrap.className = 'upload-box-progress';
          barWrap.hidden = true;
          barWrap.setAttribute('aria-hidden', 'true');
          barWrap.innerHTML = '<i class="upload-box-progress-bar"></i>';
          box.appendChild(barWrap);
        }
        return barWrap;
      }
      function setProgress(box, pct){
        const wrap = ensureProgress(box);
        const bar = wrap.querySelector('.upload-box-progress-bar');
        wrap.hidden = false;
        if (bar) bar.style.width = Math.max(0, Math.min(100, pct)) + '%';
        box.classList.add('is-uploading');
      }
      function clearProgress(box){
        const wrap = box.querySelector('.upload-box-progress');
        const bar = wrap && wrap.querySelector('.upload-box-progress-bar');
        if (wrap) wrap.hidden = true;
        if (bar) bar.style.width = '0%';
        box.classList.remove('is-uploading');
      }
      function showToast(msg){
        document.querySelectorAll('.upload-toast').forEach((el) => el.remove());
        const toast = document.createElement('div');
        toast.className = 'upload-toast';
        toast.setAttribute('role', 'status');
        toast.textContent = msg;
        document.body.appendChild(toast);
        setTimeout(() => toast.remove(), 2800);
      }
      document.addEventListener('submit', (e) => {
        const form = e.target;
        if (!(form instanceof HTMLFormElement)) return;
        if ((form.getAttribute('enctype') || '').toLowerCase() !== 'multipart/form-data') return;
        if (form.dataset.uploadXhr === '1') return;
        const fileInputs = [...form.querySelectorAll('input[type="file"]')];
        const activeBoxes = [];
        fileInputs.forEach((inp) => {
          if (inp.files && inp.files.length) {
            const box = inp.closest('.upload-box');
            if (box) activeBoxes.push(box);
          }
        });
        if (!activeBoxes.length) return;
        e.preventDefault();
        form.dataset.uploadXhr = '1';
        activeBoxes.forEach((box) => setProgress(box, 4));
        ensureCsrfField(form);
        const fd = new FormData(form);
        const submitter = e.submitter;
        if (submitter && submitter.name) {
          fd.append(submitter.name, submitter.value || '1');
        }
        const xhr = new XMLHttpRequest();
        xhr.open((form.method || 'POST').toUpperCase(), form.action || window.location.href, true);
        var tok = csrfToken();
        if (tok) xhr.setRequestHeader('X-CSRF-Token', tok);
        xhr.upload.onprogress = (ev) => {
          if (!ev.lengthComputable) return;
          const pct = Math.round((ev.loaded / ev.total) * 100);
          activeBoxes.forEach((box) => setProgress(box, pct));
        };
        xhr.onload = () => {
          activeBoxes.forEach((box) => setProgress(box, 100));
          const ok = xhr.status >= 200 && xhr.status < 400;
          if (ok) {
            showToast('آپلود با موفقیت انجام شد');
            const next = xhr.responseURL || form.action || window.location.href;
            setTimeout(() => { window.location.href = next; }, 450);
          } else {
            activeBoxes.forEach(clearProgress);
            delete form.dataset.uploadXhr;
            showToast('آپلود ناموفق بود — دوباره تلاش کنید');
          }
        };
        xhr.onerror = () => {
          activeBoxes.forEach(clearProgress);
          delete form.dataset.uploadXhr;
          showToast('خطا در ارسال فایل');
        };
        xhr.send(fd);
      }, true);
    })();

    /* Force-join channels: one row each + optional required toggle */
    (function initForceChannels(){
      function parseEntries(raw){
        const s = (raw || '').trim();
        if (!s) return [];
        if (s.charAt(0) === '[') {
          try {
            const data = JSON.parse(s);
            if (Array.isArray(data)) {
              const out = [];
              const seen = {};
              data.forEach((item) => {
                let id = '';
                let required = true;
                let link = '';
                let title = '';
                if (typeof item === 'string') id = item.trim();
                else if (item && typeof item === 'object') {
                  id = String(item.id || item.channel || '').trim();
                  required = item.required !== false && item.required !== 0 && item.required !== '0';
                  link = String(item.link || item.invite_link || item.url || '').trim();
                  title = String(item.title || item.name || item.label || '').trim();
                }
                if (!id && !link) return;
                if (!id) id = link;
                const key = id.toLowerCase();
                if (seen[key]) return;
                seen[key] = 1;
                out.push({ id, required: !!required, link, title });
              });
              return out;
            }
          } catch (e) {}
        }
        return s.replace(/,/g, '\n').split('\n')
          .map((p) => p.trim())
          .filter(Boolean)
          .filter((ch, i, arr) => arr.findIndex((x) => x.toLowerCase() === ch.toLowerCase()) === i)
          .map((id) => ({ id, required: true, link: '', title: '' }));
      }
      function rowHtml(entry){
        const id = entry && entry.id ? String(entry.id) : '';
        const link = entry && entry.link ? String(entry.link) : '';
        const title = entry && entry.title ? String(entry.title) : '';
        const req = !entry || entry.required !== false;
        function attrEsc(v) {
          return String(v == null ? '' : v)
            .replace(/&/g, '&amp;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;');
        }
        return (
          '<div class="force-channel-row">' +
            '<label class="form-field force-channel-title-wrap">عنوان دکمه اینلاین' +
              '<input type="text" class="force-channel-title" placeholder="مثلاً کانال اخبار" value="' +
                attrEsc(title) + '" autocomplete="off" />' +
              '<small class="muted">متنی که روی دکمه عضویت در ربات دیده می‌شود</small>' +
            '</label>' +
            '<label class="form-field force-channel-id-wrap">شناسه کانال' +
              '<input type="text" class="force-channel-id" dir="ltr" placeholder="@channel یا -100… یا t.me/c/…" value="' +
                attrEsc(id) + '" autocomplete="off" />' +
              '<small class="muted">برای تأیید عضویت: @username یا −100… — ربات باید ادمین کانال باشد</small>' +
            '</label>' +
            '<label class="form-field force-channel-link-wrap">لینک دکمه / دعوت' +
              '<input type="text" class="force-channel-link" dir="ltr" placeholder="https://t.me/+… یا t.me/channel" value="' +
                attrEsc(link) + '" autocomplete="off" />' +
              '<small class="muted">لینک روی دکمه اینلاین؛ کانال خصوصی حتماً لینک دعوت (+…) بگذارید</small>' +
            '</label>' +
            '<div class="force-channel-foot">' +
              '<label class="force-channel-req ui-switch-row">' +
                '<span class="ui-switch-copy"><strong>عضویت الزامی</strong></span>' +
                '<span class="ui-switch">' +
                  '<input type="checkbox" class="force-channel-required" value="1"' + (req ? ' checked' : '') + ' />' +
                  '<span class="ui-switch-track" aria-hidden="true"></span>' +
                '</span>' +
              '</label>' +
              '<div class="force-channel-actions">' +
                '<button type="button" class="btn btn-danger btn-sm force-channel-remove" aria-label="حذف کانال">حذف</button>' +
              '</div>' +
            '</div>' +
          '</div>'
        );
      }
      function addTileHtml(){
        return (
          '<button type="button" class="force-channel-add" data-force-channels-add aria-label="افزودن کانال">' +
            '<span class="force-channel-add-plus" aria-hidden="true">+</span>' +
            '<span>افزودن کانال</span>' +
          '</button>'
        );
      }
      function sync(root){
        const hidden = root.querySelector('[data-force-channels-json]');
        const list = root.querySelector('[data-force-channels-list]');
        if (!hidden || !list) return;
        const entries = [];
        const seen = {};
        list.querySelectorAll('.force-channel-row').forEach((row) => {
          const id = (row.querySelector('.force-channel-id') || {}).value || '';
          const link = String((row.querySelector('.force-channel-link') || {}).value || '').trim();
          const title = String((row.querySelector('.force-channel-title') || {}).value || '').trim();
          let ch = String(id).trim();
          if (!ch && link) ch = link;
          if (!ch) return;
          const key = ch.toLowerCase();
          if (seen[key]) return;
          seen[key] = 1;
          const reqInput = row.querySelector('.force-channel-required');
          const entry = { id: ch, required: !!(reqInput && reqInput.checked) };
          if (link) entry.link = link;
          if (title) entry.title = title;
          entries.push(entry);
        });
        hidden.value = JSON.stringify(entries);
      }
      function blurForceChannelFocus(){
        const el = document.activeElement;
        if (el && el.classList && (
          el.classList.contains('force-channel-id') ||
          el.classList.contains('force-channel-link') ||
          el.classList.contains('force-channel-title')
        )) {
          try { el.blur(); } catch (_) {}
        }
      }
      /* readonly until real user tap — stops post-save / navigation autofocus
         from opening the mobile keyboard on channel name fields. */
      function guardChannelInputs(scope){
        (scope || document).querySelectorAll(
          '.force-channel-id, .force-channel-link, .force-channel-title'
        ).forEach((inp) => {
          if (inp.dataset.focusGuard === '1') return;
          inp.dataset.focusGuard = '1';
          inp.setAttribute('readonly', 'readonly');
          const unlock = () => {
            inp.removeAttribute('readonly');
          };
          inp.addEventListener('touchstart', unlock, { passive: true });
          inp.addEventListener('mousedown', unlock);
          inp.addEventListener('focus', () => {
            /* Programmatic/browser restore: drop focus so keyboard stays closed */
            if (inp.hasAttribute('readonly')) {
              requestAnimationFrame(blurForceChannelFocus);
            }
          });
        });
      }
      function ensureRows(root, entries){
        const list = root.querySelector('[data-force-channels-list]');
        if (!list) return;
        const items = entries && entries.length ? entries : [];
        list.innerHTML = items.map(rowHtml).join('') + addTileHtml();
        sync(root);
        guardChannelInputs(list);
        blurForceChannelFocus();
      }
      document.querySelectorAll('[data-force-channels]').forEach((root) => {
        const hidden = root.querySelector('[data-force-channels-json]');
        ensureRows(root, parseEntries(hidden ? hidden.value : ''));
        root.addEventListener('click', (e) => {
          if (e.target.closest('[data-force-channels-add]')) {
            e.preventDefault();
            const list = root.querySelector('[data-force-channels-list]');
            if (!list) return;
            const add = list.querySelector('[data-force-channels-add]');
            if (add) add.insertAdjacentHTML('beforebegin', rowHtml({ id: '', required: true, link: '', title: '' }));
            else list.insertAdjacentHTML('beforeend', rowHtml({ id: '', required: true, link: '', title: '' }));
            sync(root);
            guardChannelInputs(list);
            return;
          }
          const remove = e.target.closest('.force-channel-remove');
          if (remove) {
            e.preventDefault();
            const row = remove.closest('.force-channel-row');
            const list = root.querySelector('[data-force-channels-list]');
            if (row && list) {
              row.remove();
              if (!list.querySelector('[data-force-channels-add]')) {
                list.insertAdjacentHTML('beforeend', addTileHtml());
              }
              sync(root);
            }
          }
        });
        root.addEventListener('input', () => sync(root));
        root.addEventListener('change', () => sync(root));
        const form = root.closest('form');
        if (form) {
          form.addEventListener('submit', () => {
            sync(root);
            const active = document.activeElement;
            if (active && typeof active.blur === 'function') {
              try { active.blur(); } catch (_) {}
            }
          });
        }
      });
      window.addEventListener('pageshow', () => {
        guardChannelInputs(document);
        blurForceChannelFocus();
      });
    })();

    /* Persian form validation — no English HTML5 tooltips; red borders + FA messages */
    (function setupPanelFormValidation(){
      const MSG = {
        valueMissing: 'پر کردن این فیلد الزامی است.',
        typeMismatch: 'مقدار واردشده معتبر نیست.',
        patternMismatch: 'فرمت واردشده صحیح نیست.',
        tooShort: 'متن واردشده کوتاه‌تر از حد مجاز است.',
        tooLong: 'متن واردشده طولانی‌تر از حد مجاز است.',
        rangeUnderflow: 'مقدار کمتر از حد مجاز است.',
        rangeOverflow: 'مقدار بیشتر از حد مجاز است.',
        stepMismatch: 'مقدار با گام مجاز هم‌خوانی ندارد.',
        badInput: 'مقدار واردشده معتبر نیست.',
        customError: 'مقدار واردشده معتبر نیست.',
      };

      function skipForm(form){
        if (!(form instanceof HTMLFormElement)) return true;
        /* Confirm modal has its own reason/phrase checks; never let the generic
           validator stopImmediatePropagation before panelConfirm finishes. */
        if (form.id === 'confirm-form') return true;
        if (form.getAttribute('data-panel-validate') === '0') return true;
        return false;
      }

      function ensureNovalidate(root){
        (root || document).querySelectorAll('form').forEach((form) => {
          if (skipForm(form)) return;
          form.setAttribute('novalidate', '');
        });
      }

      function fieldWrap(el){
        return (
          el.closest('.form-field, label.form-field, .pw-field, .image-setting, .ui-switch-row') ||
          el.parentElement
        );
      }

      function clearFieldError(el){
        if (!el) return;
        el.removeAttribute('aria-invalid');
        if (typeof el.setCustomValidity === 'function') {
          try { el.setCustomValidity(''); } catch (_) {}
        }
        const wrap = fieldWrap(el);
        if (!wrap) return;
        wrap.classList.remove('is-invalid');
        wrap.querySelectorAll(':scope > .field-error').forEach((n) => n.remove());
      }

      function showFieldError(el, message){
        if (!el) return;
        clearFieldError(el);
        el.setAttribute('aria-invalid', 'true');
        const wrap = fieldWrap(el);
        if (wrap) {
          wrap.classList.add('is-invalid');
          const err = document.createElement('small');
          err.className = 'field-error';
          err.setAttribute('role', 'alert');
          err.textContent = message || MSG.customError;
          wrap.appendChild(err);
        }
      }

      function clearFormErrors(form){
        form.querySelectorAll('[aria-invalid="true"]').forEach(clearFieldError);
        form.querySelectorAll('.is-invalid').forEach((wrap) => {
          wrap.classList.remove('is-invalid');
          wrap.querySelectorAll(':scope > .field-error').forEach((n) => n.remove());
        });
      }

      function isValidateControl(el){
        if (!(el instanceof HTMLElement)) return false;
        if (!(el instanceof HTMLInputElement || el instanceof HTMLSelectElement || el instanceof HTMLTextAreaElement)) {
          return false;
        }
        if (el.disabled) return false;
        const type = (el.getAttribute('type') || '').toLowerCase();
        if (type === 'hidden' || type === 'submit' || type === 'button' || type === 'reset' || type === 'image') {
          return false;
        }
        if (el.closest('[hidden]')) return false;
        const form = el.form;
        if (form && el.closest('form') !== form) return false;
        return true;
      }

      function normalizeDigitsLocal(raw){
        if (typeof window.normalizePanelNumberText === 'function') {
          return window.normalizePanelNumberText(raw);
        }
        return String(raw == null ? '' : raw);
      }

      function messageFromValidity(el){
        const v = el.validity;
        if (!v || v.valid) return null;
        if (v.valueMissing) return MSG.valueMissing;
        if (v.typeMismatch) return MSG.typeMismatch;
        if (v.patternMismatch) return MSG.patternMismatch;
        if (v.tooShort) return MSG.tooShort;
        if (v.tooLong) return MSG.tooLong;
        if (v.rangeUnderflow) return MSG.rangeUnderflow;
        if (v.rangeOverflow) return MSG.rangeOverflow;
        if (v.stepMismatch) return MSG.stepMismatch;
        if (v.badInput) return MSG.badInput;
        if (v.customError) return (el.validationMessage && el.validationMessage.trim()) || MSG.customError;
        return MSG.customError;
      }

      function validateWasNumber(el){
        const raw = String(el.value == null ? '' : el.value).trim();
        const required = el.required || el.hasAttribute('required');
        if (!raw) return required ? MSG.valueMissing : null;
        const n = Number(normalizeDigitsLocal(raw));
        if (!Number.isFinite(n)) return MSG.badInput;
        const minAttr = el.getAttribute('min');
        const maxAttr = el.getAttribute('max');
        if (minAttr != null && minAttr !== '' && n < Number(minAttr)) {
          return MSG.rangeUnderflow + ' (حداقل ' + minAttr + ')';
        }
        if (maxAttr != null && maxAttr !== '' && n > Number(maxAttr)) {
          return MSG.rangeOverflow + ' (حداکثر ' + maxAttr + ')';
        }
        return null;
      }

      function validateControl(el){
        if (!isValidateControl(el)) return null;
        if (el.dataset && el.dataset.wasNumber === '1') {
          return validateWasNumber(el);
        }
        if (typeof el.checkValidity === 'function' && !el.checkValidity()) {
          return messageFromValidity(el);
        }
        return null;
      }

      function validateForm(form){
        if (skipForm(form)) return true;
        ensureNovalidate(form.parentElement || document);
        clearFormErrors(form);
        const controls = Array.from(form.elements || []).filter(isValidateControl);
        let firstInvalid = null;
        controls.forEach((el) => {
          const msg = validateControl(el);
          if (!msg) return;
          showFieldError(el, msg);
          if (!firstInvalid) firstInvalid = el;
        });
        if (firstInvalid) {
          try { firstInvalid.focus({ preventScroll: false }); } catch (_) {
            try { firstInvalid.focus(); } catch (__) {}
          }
          return false;
        }
        return true;
      }

      window.panelValidateForm = validateForm;

      ensureNovalidate(document);
      document.addEventListener('panel:dom-ready', (e) => {
        ensureNovalidate((e.detail && e.detail.root) || document);
      });

      document.addEventListener('input', (e) => {
        const t = e.target;
        if (!isValidateControl(t)) return;
        if (t.getAttribute('aria-invalid') === 'true' || (fieldWrap(t) && fieldWrap(t).classList.contains('is-invalid'))) {
          clearFieldError(t);
        }
      }, true);
      document.addEventListener('change', (e) => {
        const t = e.target;
        if (!isValidateControl(t)) return;
        if (t.getAttribute('aria-invalid') === 'true' || (fieldWrap(t) && fieldWrap(t).classList.contains('is-invalid'))) {
          clearFieldError(t);
        }
      }, true);

      document.addEventListener('submit', (e) => {
        const form = e.target;
        if (!(form instanceof HTMLFormElement)) return;
        if (skipForm(form)) return;
        /* Normalize digits before constraint checks (Persian/Arabic numerals) */
        if (typeof window.normalizePanelNumberText === 'function') {
          form.querySelectorAll('input').forEach((el) => {
            if (!(el instanceof HTMLInputElement)) return;
            if (el.dataset && (el.dataset.wasNumber === '1' || el.dataset.normalizeDigits === '1')) {
              const next = window.normalizePanelNumberText(el.value);
              if (next !== el.value) el.value = next;
            }
          });
        }
        if (validateForm(form)) return;
        e.preventDefault();
        e.stopImmediatePropagation();
      }, true);
    })();

    /* Shared confirm modal — replaces native confirm()/prompt() for panel mutations.
       Reason field is RENDERED only when requireReason=true (delete user/reseller/admin).
       Phrase field is RENDERED when confirmPhrase is set (VPN hard-delete type-to-confirm).
       Never leave a hidden .form-field in the DOM — author CSS display:flex beats [hidden].
       Root-fix (v10.1.13): do not use HTML minlength (Persian IME false "too short");
       commit composition before read; mirror confirm_reason; stack over edit modals. */
    (function setupPanelConfirm(){
      const modal = document.getElementById('modal-confirm');
      if (!modal) return;
      const titleEl = document.getElementById('confirm-title');
      const msgEl = document.getElementById('confirm-message');
      const reasonSlot = document.getElementById('confirm-reason-slot');
      const submitBtn = document.getElementById('confirm-submit');
      const formEl = document.getElementById('confirm-form');
      let resolver = null;
      let activeRequireReason = false;
      let activeReasonMin = 3;
      let activeConfirmPhrase = '';
      let reasonComposing = false;

      if (formEl) formEl.setAttribute('novalidate', '');

      function clearExtraFields(){
        activeRequireReason = false;
        activeConfirmPhrase = '';
        reasonComposing = false;
        if (reasonSlot) {
          reasonSlot.innerHTML = '';
          reasonSlot.hidden = true;
        }
        if (submitBtn) submitBtn.disabled = false;
      }
      function clearReasonField(){
        clearExtraFields();
      }

      function reasonCharLen(s){
        try { return Array.from(String(s || '')).length; }
        catch (_) { return String(s || '').length; }
      }

      function showReasonError(input, message){
        if (!input) return;
        const wrap = document.getElementById('confirm-reason-wrap') || input.closest('.form-field');
        if (wrap) {
          wrap.classList.add('is-invalid');
          let err = wrap.querySelector(':scope > .field-error');
          if (!err) {
            err = document.createElement('small');
            err.className = 'field-error';
            err.setAttribute('role', 'alert');
            wrap.appendChild(err);
          }
          err.textContent = message;
        }
        try { input.focus(); } catch (_) {}
      }

      function clearReasonError(){
        const wrap = document.getElementById('confirm-reason-wrap');
        if (!wrap) return;
        wrap.classList.remove('is-invalid');
        wrap.querySelectorAll(':scope > .field-error').forEach((n) => n.remove());
      }

      function commitReasonInput(input){
        if (!input) return '';
        /* Read BEFORE blur — some Persian IMEs clear or delay flush on blur. */
        let before = '';
        try { before = String(input.value || '').trim(); } catch (_) { before = ''; }
        try {
          if (document.activeElement === input) {
            input.dispatchEvent(new Event('change', { bubbles: true }));
            input.blur();
          }
        } catch (_) {}
        let after = '';
        try { after = String(input.value || '').trim(); } catch (_) { after = ''; }
        if (after && after.length >= before.length) return after;
        return before || after;
      }

      function renderReasonField(opts){
        if (!reasonSlot) return null;
        const label = opts.reasonLabel || 'علت حذف';
        const min = Math.max(1, parseInt(opts.reasonMin, 10) || 3);
        activeRequireReason = true;
        activeReasonMin = min;
        reasonComposing = false;
        reasonSlot.hidden = false;
        /* No HTML required/minlength — Persian IME composition made browsers
           report "too short" even after a valid reason was typed. */
        reasonSlot.innerHTML =
          '<label class="form-field" id="confirm-reason-wrap">' +
            '<span id="confirm-reason-label">' + String(label).replace(/</g, '&lt;') + '</span>' +
            '<textarea id="confirm-reason" name="confirm_reason" rows="3" ' +
              'placeholder="حداقل ' + min + ' کاراکتر" autocomplete="off"></textarea>' +
          '</label>';
        const input = document.getElementById('confirm-reason');
        if (input) {
          input.addEventListener('compositionstart', () => { reasonComposing = true; });
          input.addEventListener('compositionend', () => { reasonComposing = false; clearReasonError(); });
          input.addEventListener('input', () => { if (!reasonComposing) clearReasonError(); });
        }
        return input;
      }

      function renderPhraseField(opts){
        if (!reasonSlot) return null;
        const phrase = String(opts.confirmPhrase || '').trim();
        if (!phrase) return null;
        activeConfirmPhrase = phrase;
        const label = opts.phraseLabel || ('برای تأیید، دقیقاً این عبارت را تایپ کنید: ' + phrase);
        reasonSlot.hidden = false;
        reasonSlot.innerHTML =
          '<label class="form-field" id="confirm-phrase-wrap">' +
            '<span id="confirm-phrase-label">' + label.replace(/</g, '&lt;') + '</span>' +
            '<input id="confirm-phrase" name="confirm_phrase" type="text" autocomplete="off" ' +
              'spellcheck="false" dir="ltr" placeholder="' + phrase.replace(/"/g, '&quot;') + '" />' +
          '</label>';
        const input = document.getElementById('confirm-phrase');
        if (submitBtn) submitBtn.disabled = true;
        if (input) {
          const sync = () => {
            if (submitBtn) submitBtn.disabled = input.value.trim() !== phrase;
          };
          input.addEventListener('input', sync);
          input.addEventListener('change', sync);
          sync();
        }
        return input;
      }

      function finish(result){
        const r = resolver;
        resolver = null;
        clearExtraFields();
        if (modal.classList.contains('open')) closeModal(modal);
        /* Cancel / Escape must clear any stray nav clock (e.g. older builds
           that armed on the intercepted submit). Confirmed path arms in
           submitFormPost instead. */
        if (!result || !result.ok) {
          try { disarmPanelNavClock(); } catch (_) {}
        }
        if (r) r(result || { ok: false });
      }

      window.panelConfirm = function(opts){
        opts = opts || {};
        return new Promise((resolve) => {
          if (resolver) finish({ ok: false });
          resolver = resolve;
          try { closeRowActions(); } catch (_) {}
          try { closeUiSelects(); } catch (_) {}
          if (titleEl) titleEl.textContent = opts.title || 'تأیید';
          if (msgEl) msgEl.textContent = opts.message || 'ادامه می‌دهید؟';
          if (submitBtn) {
            submitBtn.textContent = opts.confirmLabel || 'تأیید';
            submitBtn.className = 'btn' + (opts.danger ? ' btn-danger' : (opts.warn ? ' btn-warn' : ''));
            submitBtn.disabled = false;
          }
          const requireReason = opts.requireReason === true || opts.reason === true;
          const phrase = String(opts.confirmPhrase || '').trim();
          clearExtraFields();
          if (phrase) renderPhraseField(opts);
          else if (requireReason) renderReasonField(opts);
          /* Stack over edit modals so the originating delete form stays mounted. */
          openModal('modal-confirm', { stack: true });
        });
      };

      if (formEl) {
        formEl.addEventListener('submit', (e) => {
          e.preventDefault();
          if (!resolver) return;
          if (activeConfirmPhrase) {
            const phraseInput = document.getElementById('confirm-phrase');
            const v = commitReasonInput(phraseInput);
            if (v !== activeConfirmPhrase) {
              if (phraseInput) phraseInput.focus();
              return;
            }
            finish({ ok: true, phrase: v });
            return;
          }
          if (activeRequireReason) {
            const reasonInput = document.getElementById('confirm-reason');
            if (reasonComposing) {
              const once = () => {
                if (reasonInput) reasonInput.removeEventListener('compositionend', once);
                reasonComposing = false;
                if (typeof formEl.requestSubmit === 'function') formEl.requestSubmit();
                else formEl.dispatchEvent(new Event('submit', { cancelable: true, bubbles: true }));
              };
              if (reasonInput) reasonInput.addEventListener('compositionend', once);
              else reasonComposing = false;
              return;
            }
            const v = commitReasonInput(reasonInput);
            const min = activeReasonMin || 3;
            if (reasonCharLen(v) < min) {
              showReasonError(reasonInput, 'علت حذف باید حداقل ' + min + ' کاراکتر باشد.');
              return;
            }
            clearReasonError();
            finish({ ok: true, reason: v });
            return;
          }
          finish({ ok: true });
        });
      }

      modal.addEventListener('click', (e) => {
        if (!resolver) return;
        if (e.target.closest('[data-modal-close]') || e.target === modal.querySelector('.ui-modal-backdrop')) {
          finish({ ok: false });
        }
      });

      document.addEventListener('keydown', (e) => {
        if (e.key !== 'Escape' || !resolver) return;
        if (!modal.classList.contains('open')) return;
        e.preventDefault();
        e.stopPropagation();
        finish({ ok: false });
      }, true);

      function attrTruthy(el, name){
        if (!el || !el.hasAttribute(name)) return false;
        const v = (el.getAttribute(name) || '').trim().toLowerCase();
        if (!v) return true;
        return !(v === '0' || v === 'false' || v === 'no' || v === 'off');
      }

      function readOpts(el, form){
        const src = el && el.hasAttribute('data-confirm') ? el : form;
        const requireReason = attrTruthy(src, 'data-confirm-reason');
        const reasonName = (src && src.getAttribute('data-confirm-reason-name')) || 'reason';
        const reasonMin = parseInt((src && src.getAttribute('data-confirm-reason-min')) || '3', 10) || 3;
        const confirmPhrase = ((src && src.getAttribute('data-confirm-phrase')) || '').trim();
        const phraseName = (src && src.getAttribute('data-confirm-phrase-name')) || 'confirm_phrase';
        return {
          title: (src && src.getAttribute('data-confirm-title')) || 'تأیید',
          message: (src && src.getAttribute('data-confirm')) || 'ادامه می‌دهید؟',
          danger: !!(src && src.hasAttribute('data-confirm-danger')),
          warn: !!(src && src.hasAttribute('data-confirm-warn')),
          requireReason: requireReason && !confirmPhrase,
          reason: requireReason && !confirmPhrase,
          reasonMin: reasonMin,
          reasonLabel: (src && src.getAttribute('data-confirm-reason-label')) || 'علت حذف',
          reasonName: reasonName,
          confirmPhrase: confirmPhrase,
          phraseName: phraseName,
          phraseLabel: (src && src.getAttribute('data-confirm-phrase-label')) || '',
          confirmLabel: (src && src.getAttribute('data-confirm-label')) || 'تأیید',
        };
      }

      function stripNamedFields(form, name){
        form.querySelectorAll(
          'input[name="' + name + '"], textarea[name="' + name + '"]'
        ).forEach((el) => el.parentNode && el.parentNode.removeChild(el));
        /* Also drop form-associated fields living outside the form tree. */
        try {
          Array.from(form.elements || []).forEach((el) => {
            if (el && el.name === name && el.parentNode) el.parentNode.removeChild(el);
          });
        } catch (_) {}
      }

      function applyReason(form, opts, reason){
        /* v9-compatible: update existing hidden reason in place (templates include
           <input type="hidden" name="reason" value="">). Avoid appending a second
           reason= that Starlette FormData.get can last-win as empty. Still mirror
           confirm_reason as a single backup field for extract_delete_reason. */
        const text = String(reason || '').trim();
        if (!opts || !opts.requireReason || !text) return;
        const name = (opts.reasonName) || 'reason';
        let hidden = form.querySelector(
          'input[name="' + name + '"], textarea[name="' + name + '"]'
        );
        if (!hidden) {
          hidden = document.createElement('input');
          hidden.type = 'hidden';
          hidden.name = name;
          form.appendChild(hidden);
        }
        hidden.value = text;
        /* One confirm_reason backup only */
        let mirror = form.querySelector('input[name="confirm_reason"], textarea[name="confirm_reason"]');
        if (name !== 'confirm_reason') {
          if (!mirror) {
            mirror = document.createElement('input');
            mirror.type = 'hidden';
            mirror.name = 'confirm_reason';
            form.appendChild(mirror);
          }
          mirror.value = text;
        }
      }

      function applyPhrase(form, opts, phrase){
        const text = String(phrase || '').trim();
        if (!opts.confirmPhrase || !text) return;
        const name = (opts && opts.phraseName) || 'confirm_phrase';
        stripNamedFields(form, name);
        const hidden = document.createElement('input');
        hidden.type = 'hidden';
        hidden.name = name;
        hidden.value = text;
        form.appendChild(hidden);
      }

      document.addEventListener('submit', (e) => {
        const form = e.target;
        if (!(form instanceof HTMLFormElement)) return;
        if (form.id === 'confirm-form') return;
        if (form.dataset.confirmSkip === '1') {
          delete form.dataset.confirmSkip;
          return;
        }
        if (!form.hasAttribute('data-confirm')) return;
        e.preventDefault();
        e.stopPropagation();
        const opts = readOpts(form, form);
        window.panelConfirm(opts).then((result) => {
          if (!result || !result.ok) return;
          applyReason(form, opts, result.reason);
          applyPhrase(form, opts, result.phrase);
          /* Body-level POST with reason from JS memory — not form.submit() inside
             a display:none kebab menu (drops fields on mobile WebKit). */
          const overrides = {};
          if (opts.requireReason) {
            const reasonText = String((result && result.reason) || '').trim();
            if (!reasonText) return;
            overrides[opts.reasonName || 'reason'] = reasonText;
            overrides.confirm_reason = reasonText;
          }
          if (opts.confirmPhrase && result.phrase) {
            overrides[opts.phraseName || 'confirm_phrase'] = result.phrase;
          }
          submitFormPost(form, overrides);
        });
      }, true);

      document.addEventListener('click', (e) => {
        const btn = e.target.closest('button[data-confirm], input[type="submit"][data-confirm]');
        if (!btn) return;
        const form = btn.form || btn.closest('form');
        if (form && form.hasAttribute('data-confirm')) return;
        if (btn.dataset.confirmSkip === '1') {
          delete btn.dataset.confirmSkip;
          return;
        }
        e.preventDefault();
        e.stopPropagation();
        const opts = readOpts(btn, form);
        window.panelConfirm(opts).then((result) => {
          if (!result || !result.ok) return;
          if (!form) return;
          applyReason(form, opts, result.reason);
          applyPhrase(form, opts, result.phrase);
          const overrides = {};
          if (opts.requireReason) {
            const reasonText = String((result && result.reason) || '').trim();
            if (!reasonText) return;
            overrides[opts.reasonName || 'reason'] = reasonText;
            overrides.confirm_reason = reasonText;
          }
          if (opts.confirmPhrase && result.phrase) {
            overrides[opts.phraseName || 'confirm_phrase'] = result.phrase;
          }
          if (btn && btn.name) {
            overrides[btn.name] = btn.value || '1';
          }
          submitFormPost(form, overrides);
        });
      }, true);
    })();

    /* Normalize Persian/Arabic digits in numeric fields before submit / blur */
    (function(){
      const FA = {'۰':'0','۱':'1','۲':'2','۳':'3','۴':'4','۵':'5','۶':'6','۷':'7','۸':'8','۹':'9'};
      const AR = {'٠':'0','١':'1','٢':'2','٣':'3','٤':'4','٥':'5','٦':'6','٧':'7','٨':'8','٩':'9'};
      function normalizeNumberText(raw){
        let s = String(raw == null ? '' : raw);
        s = s.replace(/[۰-۹]/g, (ch) => FA[ch] || ch)
             .replace(/[٠-٩]/g, (ch) => AR[ch] || ch)
             .replace(/[٬,\u00a0\s]/g, '')
             .replace(/[٫،]/g, '.');
        if ((s.match(/\./g) || []).length > 1) {
          const parts = s.split('.');
          s = parts.slice(0, -1).join('') + '.' + parts[parts.length - 1];
        }
        return s;
      }
      function isNumericField(el){
        if (!(el instanceof HTMLInputElement)) return false;
        if (el.disabled || el.readOnly) return false;
        if (el.dataset.normalizeDigits === '0') return false;
        const type = (el.getAttribute('type') || 'text').toLowerCase();
        if (type === 'number' || type === 'tel' || el.dataset.wasNumber === '1') return true;
        const mode = (el.getAttribute('inputmode') || '').toLowerCase();
        if (mode === 'numeric' || mode === 'decimal') return true;
        if (el.dataset.normalizeDigits === '1') return true;
        return false;
      }
      function unlockNumberInputs(root){
        (root || document).querySelectorAll('input[type="number"]').forEach((el) => {
          /* Browsers reject Persian/Arabic digits in type=number — use text + inputmode */
          const step = el.getAttribute('step') || '';
          const mode = (step && step !== '1' && step !== 'any') ? 'decimal' : 'numeric';
          if (!el.getAttribute('inputmode')) el.setAttribute('inputmode', mode);
          el.dataset.wasNumber = '1';
          el.dataset.normalizeDigits = '1';
          el.type = 'text';
          el.setAttribute('dir', el.getAttribute('dir') || 'ltr');
          el.setAttribute('autocomplete', el.getAttribute('autocomplete') || 'off');
        });
      }
      function applyNormalize(el){
        if (!isNumericField(el)) return;
        const next = normalizeNumberText(el.value);
        if (next !== el.value) el.value = next;
      }
      window.normalizePanelNumberText = normalizeNumberText;
      unlockNumberInputs(document);
      document.addEventListener('panel:dom-ready', (e) => {
        unlockNumberInputs((e.detail && e.detail.root) || document);
      });
      document.addEventListener('blur', (e) => {
        applyNormalize(e.target);
      }, true);
      document.addEventListener('change', (e) => {
        applyNormalize(e.target);
      }, true);
      document.addEventListener('submit', (e) => {
        const form = e.target;
        if (!(form instanceof HTMLFormElement)) return;
        form.querySelectorAll('input').forEach(applyNormalize);
      }, true);
    })();

    /* Sortable table headers: click th[data-sort-type] on table[data-sortable] */
    (function () {
      function cellValue(td, type) {
        if (!td) return type === 'num' ? Number.NEGATIVE_INFINITY : '';
        const raw = td.getAttribute('data-sort-value');
        const text = raw != null ? raw : (td.textContent || '').trim();
        if (type === 'num') {
          if (text === '' || text === '—' || text === '-') return Number.NEGATIVE_INFINITY;
          const n = Number(String(text).replace(/[^\d.+-eE]/g, ''));
          return Number.isFinite(n) ? n : Number.NEGATIVE_INFINITY;
        }
        return String(text).toLowerCase();
      }
      function sortTable(table, th) {
        const tbody = table.tBodies[0];
        if (!tbody) return;
        const heads = Array.from(th.parentNode.children);
        const col = heads.indexOf(th);
        if (col < 0) return;
        const type = th.getAttribute('data-sort-type') || 'text';
        const cur = th.getAttribute('aria-sort');
        const asc = cur !== 'ascending';
        heads.forEach((h) => {
          if (h !== th) h.removeAttribute('aria-sort');
        });
        th.setAttribute('aria-sort', asc ? 'ascending' : 'descending');
        const rows = Array.from(tbody.querySelectorAll('tr')).filter((tr) =>
          tr.querySelector('td')
        );
        // Keep empty-state single muted row at end
        const dataRows = rows.filter((tr) => !tr.querySelector('td[colspan]'));
        const emptyRows = rows.filter((tr) => tr.querySelector('td[colspan]'));
        dataRows.sort((a, b) => {
          const av = cellValue(a.children[col], type);
          const bv = cellValue(b.children[col], type);
          let cmp = 0;
          if (typeof av === 'number' && typeof bv === 'number') cmp = av - bv;
          else cmp = String(av).localeCompare(String(bv), 'fa', { sensitivity: 'base', numeric: true });
          return asc ? cmp : -cmp;
        });
        dataRows.concat(emptyRows).forEach((tr) => tbody.appendChild(tr));
      }
      function bind(table) {
        if (!(table instanceof HTMLTableElement)) return;
        table.querySelectorAll('thead th[data-sort-type]').forEach((th) => {
          th.setAttribute('role', 'columnheader');
          th.tabIndex = 0;
          th.addEventListener('click', () => sortTable(table, th));
          th.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') {
              e.preventDefault();
              sortTable(table, th);
            }
          });
        });
      }
      document.querySelectorAll('table[data-sortable]').forEach(bind);
    })();

    /* Inbox alert dismiss modal — POST via panelSubmitFormPost so mode/csrf
       cannot vanish the way zero-size native radios did on mobile WebKit. */
    (function () {
      const modal = document.getElementById('modal-inbox-dismiss');
      const form = document.getElementById('form-inbox-dismiss');
      const keyInput = document.getElementById('inbox-dismiss-key');
      const entityInput = document.getElementById('inbox-dismiss-entity');
      const returnInput = document.getElementById('inbox-dismiss-return');
      const modeInput = document.getElementById('inbox-dismiss-mode');
      if (!modal || !keyInput || !form) return;

      function selectedMode() {
        const checked = form.querySelector('input[data-inbox-mode]:checked');
        const v = checked && (checked.getAttribute('data-inbox-mode') || checked.value)
          ? String(checked.getAttribute('data-inbox-mode') || checked.value)
          : '24h';
        return (v === 'forever') ? 'forever' : '24h';
      }

      function syncSelected() {
        const mode = selectedMode();
        if (modeInput) modeInput.value = mode;
        form.querySelectorAll('.inbox-dismiss-option').forEach((opt) => {
          const input = opt.querySelector('input[type="radio"]');
          opt.classList.toggle('is-selected', !!(input && input.checked));
        });
      }

      function openDismiss(alertKey, entityId) {
        keyInput.value = alertKey || '';
        if (entityInput) entityInput.value = entityId || '';
        if (returnInput) returnInput.value = window.location.pathname + window.location.search;
        const first = form.querySelector('input[data-inbox-mode="24h"]');
        if (first) first.checked = true;
        syncSelected();
        if (typeof openModal === 'function') openModal('modal-inbox-dismiss');
        else {
          modal.hidden = false;
          modal.classList.add('open');
        }
      }

      form.addEventListener('change', (e) => {
        if (e.target && e.target.matches('input[type="radio"]')) syncSelected();
      });
      form.querySelectorAll('.inbox-dismiss-option').forEach((opt) => {
        opt.addEventListener('click', () => {
          const input = opt.querySelector('input[type="radio"]');
          if (!input) return;
          input.checked = true;
          syncSelected();
        });
      });

      form.addEventListener('submit', (e) => {
        e.preventDefault();
        syncSelected();
        const mode = selectedMode();
        if (typeof window.panelSubmitFormPost === 'function') {
          window.panelSubmitFormPost(form, { mode: mode });
          return;
        }
        if (modeInput) modeInput.value = mode;
        if (typeof window.panelEnsureCsrfField === 'function') {
          window.panelEnsureCsrfField(form);
        }
        form.submit();
      });

      document.addEventListener('click', (e) => {
        const btn = e.target.closest('[data-inbox-dismiss]');
        if (!btn) return;
        e.preventDefault();
        openDismiss(btn.getAttribute('data-inbox-dismiss'), btn.getAttribute('data-inbox-entity') || '');
      });

      const resetForm = document.getElementById('form-inbox-dismiss-reset');
      if (resetForm) {
        resetForm.addEventListener('submit', (e) => {
          e.preventDefault();
          if (typeof window.panelSubmitFormPost === 'function') {
            window.panelSubmitFormPost(resetForm, {});
            return;
          }
          if (typeof window.panelEnsureCsrfField === 'function') {
            window.panelEnsureCsrfField(resetForm);
          }
          resetForm.submit();
        });
      }
    })();

    /* Payment destination lists (+ / -) */
    (function () {
      function uid() {
        return 'pd' + Math.random().toString(36).slice(2, 10);
      }
      function attrEsc(v) {
        return String(v == null ? '' : v)
          .replace(/&/g, '&amp;')
          .replace(/"/g, '&quot;')
          .replace(/'/g, '&#39;')
          .replace(/</g, '&lt;')
          .replace(/>/g, '&gt;');
      }
      function parseJson(raw) {
        try {
          const data = JSON.parse(raw || '[]');
          return Array.isArray(data) ? data : [];
        } catch (_) {
          return [];
        }
      }
      function storedStyleValue(item) {
        if (!item || item.button_style === undefined || item.button_style === null) return 'inherit';
        if (item.button_style === '') return '';
        return String(item.button_style);
      }
      function styleSelectHtml(cur, inheritLabel) {
        const val = cur == null ? 'inherit' : cur;
        const tone = (!val || val === 'inherit') ? 'default' : val;
        const opts = [
          ['inherit', inheritLabel || 'ارث از پیش‌فرض', 'default'],
          ['', 'سفید', 'default'],
          ['primary', 'آبی', 'primary'],
          ['success', 'سبز', 'success'],
          ['danger', 'قرمز', 'danger'],
        ];
        let html =
          '<div class="plan-color-field item-color-field pay-dest-color">' +
            '<div class="btn-color-card plan-color-card">' +
              '<span class="btn-color-label">رنگ دکمه در ربات</span>' +
              '<select class="btn-color-select pay-dest-style" data-tone="' + tone + '" aria-label="رنگ دکمه در ربات">';
        opts.forEach(function (o) {
          html +=
            '<option value="' + o[0] + '" data-tone="' + o[2] + '"' +
            (val === o[0] ? ' selected' : '') + '>' + o[1] + '</option>';
        });
        html += '</select></div></div>';
        return html;
      }
      function rowStylePatch(row) {
        const styleSel = row.querySelector('.pay-dest-style');
        const styleVal = styleSel ? styleSel.value : 'inherit';
        if (styleVal === 'inherit') return {};
        if (styleVal === '') return { button_style: '' };
        return { button_style: styleVal };
      }
      function rowHtml(kind, item, inheritLabel) {
        const id = (item && item.id) || uid();
        const styleHtml = styleSelectHtml(storedStyleValue(item), inheritLabel);
        if (kind === 'cards') {
          return (
            '<div class="pay-dest-row" data-pay-dest-row data-pay-dest-id="' + attrEsc(id) + '">' +
              '<div class="pay-dest-fields">' +
                '<label>شماره کارت<input type="text" class="pay-dest-card-number" dir="ltr" value="' + attrEsc(item.number) + '" autocomplete="off" /></label>' +
                '<label>صاحب کارت<input type="text" class="pay-dest-card-holder" value="' + attrEsc(item.holder) + '" autocomplete="off" /></label>' +
                styleHtml +
              '</div>' +
              '<button type="button" class="btn btn-danger btn-sm pay-dest-remove pay-dest-remove-btn">حذف</button>' +
            '</div>'
          );
        }
        if (kind === 'gateways') {
          return (
            '<div class="pay-dest-row" data-pay-dest-row data-pay-dest-id="' + attrEsc(id) + '">' +
              '<div class="pay-dest-fields">' +
                '<label>نام درگاه<input type="text" class="pay-dest-gw-name" value="' + attrEsc(item.name) + '" autocomplete="off" /></label>' +
                '<label>لینک<input type="text" class="pay-dest-gw-link" dir="ltr" value="' + attrEsc(item.link) + '" autocomplete="off" /></label>' +
                styleHtml +
              '</div>' +
              '<button type="button" class="btn btn-danger btn-sm pay-dest-remove pay-dest-remove-btn">حذف</button>' +
            '</div>'
          );
        }
        return (
          '<div class="pay-dest-row" data-pay-dest-row data-pay-dest-id="' + attrEsc(id) + '">' +
            '<div class="pay-dest-fields">' +
              '<label>رمزارز<input type="text" class="pay-dest-cr-asset" value="' + attrEsc(item.asset || 'USDT') + '" autocomplete="off" /></label>' +
              '<label>شبکه<input type="text" class="pay-dest-cr-network" value="' + attrEsc(item.network || 'TRC20') + '" autocomplete="off" /></label>' +
              '<label>آدرس<input type="text" class="pay-dest-cr-address" dir="ltr" value="' + attrEsc(item.address) + '" autocomplete="off" /></label>' +
              styleHtml +
            '</div>' +
            '<button type="button" class="btn btn-danger btn-sm pay-dest-remove pay-dest-remove-btn">حذف</button>' +
          '</div>'
        );
      }
      function addBtnHtml() {
        return '<button type="button" class="pay-dest-add-row" data-pay-dest-add><span aria-hidden="true">+</span><span>افزودن</span></button>';
      }
      function sync(root) {
        const hidden = root.querySelector('[data-pay-dest-json]');
        const list = root.querySelector('[data-pay-dest-list]');
        if (!hidden || !list) return;
        const kind = root.getAttribute('data-pay-dest');
        const entries = [];
        list.querySelectorAll('[data-pay-dest-row]').forEach((row, idx) => {
          const rowId = row.getAttribute('data-pay-dest-id') || uid();
          row.setAttribute('data-pay-dest-id', rowId);
          const stylePatch = rowStylePatch(row);
          if (kind === 'cards') {
            const number = String((row.querySelector('.pay-dest-card-number') || {}).value || '').replace(/\D/g, '');
            const holder = String((row.querySelector('.pay-dest-card-holder') || {}).value || '').trim();
            if (!number) return;
            entries.push(Object.assign({ id: rowId, number: number, holder: holder, enabled: true, sort: idx }, stylePatch));
          } else if (kind === 'gateways') {
            const name = String((row.querySelector('.pay-dest-gw-name') || {}).value || '').trim();
            const link = String((row.querySelector('.pay-dest-gw-link') || {}).value || '').trim();
            if (!name && !link) return;
            entries.push(Object.assign({ id: rowId, name: name || 'درگاه پرداخت', link: link, enabled: true, sort: idx }, stylePatch));
          } else {
            const asset = String((row.querySelector('.pay-dest-cr-asset') || {}).value || 'USDT').trim();
            const network = String((row.querySelector('.pay-dest-cr-network') || {}).value || 'TRC20').trim();
            const address = String((row.querySelector('.pay-dest-cr-address') || {}).value || '').trim();
            if (!address) return;
            entries.push(Object.assign({ id: rowId, asset: asset, network: network, address: address, enabled: true, sort: idx }, stylePatch));
          }
        });
        hidden.value = JSON.stringify(entries);
      }
      function render(root, items) {
        const list = root.querySelector('[data-pay-dest-list]');
        const kind = root.getAttribute('data-pay-dest');
        const inheritLabel = root.getAttribute('data-style-inherit-label') || 'ارث از پیش‌فرض';
        if (!list) return;
        const rows = (items && items.length ? items : [{}]).map((item) => rowHtml(kind, item, inheritLabel)).join('');
        list.innerHTML = rows + addBtnHtml();
        sync(root);
      }
      document.querySelectorAll('[data-pay-dest]').forEach((root) => {
        const hidden = root.querySelector('[data-pay-dest-json]');
        const inheritLabel = root.getAttribute('data-style-inherit-label') || 'ارث از پیش‌فرض';
        render(root, parseJson(hidden ? hidden.value : '[]'));
        root.addEventListener('click', (e) => {
          if (e.target.closest('[data-pay-dest-add]')) {
            e.preventDefault();
            const list = root.querySelector('[data-pay-dest-list]');
            const add = list && list.querySelector('[data-pay-dest-add]');
            const kind = root.getAttribute('data-pay-dest');
            const html = rowHtml(kind, {}, inheritLabel);
            if (add) add.insertAdjacentHTML('beforebegin', html);
            else if (list) list.insertAdjacentHTML('afterbegin', html);
            sync(root);
            return;
          }
          if (e.target.closest('.pay-dest-remove')) {
            e.preventDefault();
            const row = e.target.closest('[data-pay-dest-row]');
            const list = root.querySelector('[data-pay-dest-list]');
            if (row && list) {
              row.remove();
              if (!list.querySelector('[data-pay-dest-row]')) {
                const kind = root.getAttribute('data-pay-dest');
                const add = list.querySelector('[data-pay-dest-add]');
                if (add) add.insertAdjacentHTML('beforebegin', rowHtml(kind, {}, inheritLabel));
              }
              sync(root);
            }
          }
        });
        root.addEventListener('input', () => sync(root));
        root.addEventListener('change', (e) => {
          if (e.target && e.target.classList && e.target.classList.contains('pay-dest-style')) sync(root);
        });
      });
    })();

    /* Support contacts editor — same multi-row + final-save pattern as pay-dest */
    (function () {
      function uid() {
        return 's' + Math.random().toString(36).slice(2, 10);
      }
      function parseJson(raw) {
        try {
          const data = JSON.parse(raw || '[]');
          return Array.isArray(data) ? data : [];
        } catch (_) {
          return [];
        }
      }
      function storedStyleValue(item) {
        if (!item || item.button_style === undefined || item.button_style === null) return 'inherit';
        if (item.button_style === '') return '';
        return String(item.button_style);
      }
      function styleSelectHtml(cur) {
        const val = cur == null ? 'inherit' : cur;
        const tone = (!val || val === 'inherit') ? 'default' : val;
        const opts = [
          ['inherit', 'ارث از پشتیبانی', 'default'],
          ['', 'سفید', 'default'],
          ['primary', 'آبی', 'primary'],
          ['success', 'سبز', 'success'],
          ['danger', 'قرمز', 'danger'],
        ];
        let html =
          '<div class="plan-color-field item-color-field pay-dest-color">' +
            '<div class="btn-color-card plan-color-card">' +
              '<span class="btn-color-label">رنگ دکمه در ربات</span>' +
              '<select class="btn-color-select supports-style" data-tone="' + tone + '" aria-label="رنگ دکمه در ربات">';
        opts.forEach(function (o) {
          html +=
            '<option value="' + o[0] + '" data-tone="' + o[2] + '"' +
            (val === o[0] ? ' selected' : '') + '>' + o[1] + '</option>';
        });
        html += '</select></div></div>';
        return html;
      }
      function rowStylePatch(row) {
        const styleSel = row.querySelector('.supports-style');
        const styleVal = styleSel ? styleSel.value : 'inherit';
        if (styleVal === 'inherit') return {};
        if (styleVal === '') return { button_style: '' };
        return { button_style: styleVal };
      }
      function esc(v) {
        return String(v == null ? '' : v)
          .replace(/&/g, '&amp;')
          .replace(/"/g, '&quot;')
          .replace(/'/g, '&#39;')
          .replace(/</g, '&lt;')
          .replace(/>/g, '&gt;');
      }
      function rowHtml(item) {
        const id = (item && item.id) || uid();
        const enabled = !item || item.enabled === undefined || item.enabled === null
          ? true
          : !!item.enabled;
        return (
          '<div class="pay-dest-row" data-supports-row data-supports-id="' + esc(id) + '">' +
            '<div class="pay-dest-fields supports-fields-grid">' +
              '<label>عنوان<input type="text" class="supports-title" maxlength="80" value="' + esc(item && item.title) + '" placeholder="مثلاً پشتیبان ربات" autocomplete="off" /></label>' +
              '<label>آیدی / یوزرنیم تلگرام<input type="text" class="supports-telegram" dir="ltr" value="' + esc(item && item.telegram) + '" placeholder="@username یا 123456789" autocomplete="off" /></label>' +
              '<label>ترتیب<input type="number" class="supports-sort" dir="ltr" value="' + esc(item && item.sort != null ? item.sort : 0) + '" /></label>' +
              '<label class="support-enabled supports-enabled-field">' +
                '<span>فعال</span>' +
                '<span class="ui-switch">' +
                  '<input type="checkbox" class="supports-enabled" value="1"' + (enabled ? ' checked' : '') + ' />' +
                  '<span class="ui-switch-track" aria-hidden="true"></span>' +
                '</span>' +
              '</label>' +
              styleSelectHtml(storedStyleValue(item)) +
            '</div>' +
            '<button type="button" class="btn btn-danger btn-sm supports-remove pay-dest-remove-btn">حذف</button>' +
          '</div>'
        );
      }
      function addBtnHtml() {
        return '<button type="button" class="pay-dest-add-row" data-supports-add><span aria-hidden="true">+</span><span>افزودن</span></button>';
      }
      function sync(root) {
        const hidden = root.querySelector('[data-supports-json]');
        const list = root.querySelector('[data-supports-list]');
        if (!hidden || !list) return;
        const entries = [];
        list.querySelectorAll('[data-supports-row]').forEach((row, idx) => {
          const rowId = row.getAttribute('data-supports-id') || uid();
          row.setAttribute('data-supports-id', rowId);
          const title = String((row.querySelector('.supports-title') || {}).value || '').trim();
          const telegram = String((row.querySelector('.supports-telegram') || {}).value || '').trim();
          if (!title && !telegram) return;
          let sort = idx;
          try {
            sort = parseInt(String((row.querySelector('.supports-sort') || {}).value || idx), 10);
            if (Number.isNaN(sort)) sort = idx;
          } catch (_) { sort = idx; }
          const en = row.querySelector('.supports-enabled');
          const enabled = !!(en && en.checked);
          entries.push(Object.assign({
            id: rowId,
            title: title,
            telegram: telegram,
            sort: sort,
            enabled: enabled,
          }, rowStylePatch(row)));
        });
        hidden.value = JSON.stringify(entries);
      }
      function render(root, items) {
        const list = root.querySelector('[data-supports-list]');
        if (!list) return;
        const rows = (items && items.length ? items : [{}]).map((item) => rowHtml(item)).join('');
        list.innerHTML = rows + addBtnHtml();
        sync(root);
      }
      document.querySelectorAll('[data-supports-editor]').forEach((root) => {
        const hidden = root.querySelector('[data-supports-json]');
        render(root, parseJson(hidden ? hidden.value : '[]'));
        root.addEventListener('click', (e) => {
          if (e.target.closest('[data-supports-add]')) {
            e.preventDefault();
            const list = root.querySelector('[data-supports-list]');
            const add = list && list.querySelector('[data-supports-add]');
            const html = rowHtml({});
            if (add) add.insertAdjacentHTML('beforebegin', html);
            else if (list) list.insertAdjacentHTML('afterbegin', html);
            sync(root);
            return;
          }
          if (e.target.closest('.supports-remove')) {
            e.preventDefault();
            const row = e.target.closest('[data-supports-row]');
            const list = root.querySelector('[data-supports-list]');
            if (row && list) {
              row.remove();
              if (!list.querySelector('[data-supports-row]')) {
                const add = list.querySelector('[data-supports-add]');
                if (add) add.insertAdjacentHTML('beforebegin', rowHtml({}));
              }
              sync(root);
            }
          }
        });
        root.addEventListener('input', () => sync(root));
        root.addEventListener('change', () => sync(root));
      });
    })();

    /* Connection guides editor (تنظیمات ← آموزش اتصال) */
    (function () {
      function uid() {
        return Math.random().toString(16).slice(2, 10) + Math.random().toString(16).slice(2, 6);
      }
      function parseJson(raw) {
        try {
          const data = JSON.parse(raw || '[]');
          return Array.isArray(data) ? data : [];
        } catch (_) {
          return [];
        }
      }
      function esc(v) {
        return String(v == null ? '' : v)
          .replace(/&/g, '&amp;')
          .replace(/"/g, '&quot;')
          .replace(/'/g, '&#39;')
          .replace(/</g, '&lt;')
          .replace(/>/g, '&gt;');
      }
      function styleSelectHtml(cur) {
        const val = cur == null ? 'primary' : String(cur);
        const tone = !val ? 'default' : val;
        const opts = [
          ['', 'سفید', 'default'],
          ['primary', 'آبی', 'primary'],
          ['success', 'سبز', 'success'],
          ['danger', 'قرمز', 'danger'],
        ];
        let html =
          '<div class="plan-color-field item-color-field pay-dest-color">' +
            '<div class="btn-color-card plan-color-card">' +
              '<span class="btn-color-label">رنگ دکمه</span>' +
              '<select class="btn-color-select guides-style" data-tone="' + tone + '" aria-label="رنگ دکمه">' ;
        opts.forEach(function (o) {
          html +=
            '<option value="' + o[0] + '" data-tone="' + o[2] + '"' +
            (val === o[0] ? ' selected' : '') + '>' + o[1] + '</option>';
        });
        html += '</select></div></div>';
        return html;
      }
      function rowHtml(item) {
        const id = (item && item.id) || uid();
        const enabled = !item || item.enabled === undefined || item.enabled === null
          ? true
          : !!item.enabled;
        const audience = (item && item.audience) === 'reseller' ? 'reseller' : 'user';
        const deep = !!(item && item.deep_link);
        const style = item && item.style != null ? item.style : (item && item.button_style != null ? item.button_style : 'primary');
        return (
          '<div class="pay-dest-row" data-guides-row data-guides-id="' + esc(id) + '">' +
            '<div class="pay-dest-fields supports-fields-grid">' +
              '<label>عنوان دکمه<input type="text" class="guides-title" maxlength="64" value="' + esc(item && item.title) + '" placeholder="مثلاً V2Box" autocomplete="off" /></label>' +
              '<label>مخاطب<select class="guides-audience">' +
                '<option value="user"' + (audience === 'user' ? ' selected' : '') + '>کاربران</option>' +
                '<option value="reseller"' + (audience === 'reseller' ? ' selected' : '') + '>نمایندگان</option>' +
              '</select></label>' +
              '<label>ترتیب<input type="number" class="guides-sort" dir="ltr" value="' + esc(item && item.sort != null ? item.sort : 0) + '" /></label>' +
              '<label class="support-enabled supports-enabled-field">' +
                '<span>فعال</span>' +
                '<span class="ui-switch">' +
                  '<input type="checkbox" class="guides-enabled" value="1"' + (enabled ? ' checked' : '') + ' />' +
                  '<span class="ui-switch-track" aria-hidden="true"></span>' +
                '</span>' +
              '</label>' +
              '<label class="support-enabled supports-enabled-field">' +
                '<span>دکمه لینک اشتراک</span>' +
                '<span class="ui-switch">' +
                  '<input type="checkbox" class="guides-deeplink" value="1"' + (deep ? ' checked' : '') + ' />' +
                  '<span class="ui-switch-track" aria-hidden="true"></span>' +
                '</span>' +
              '</label>' +
              styleSelectHtml(style) +
              '<label class="full">متن آموزش<textarea class="guides-body" rows="4" maxlength="4000" placeholder="مراحل اتصال…">' + esc(item && (item.body || item.text)) + '</textarea></label>' +
            '</div>' +
            '<button type="button" class="btn btn-danger btn-sm guides-remove pay-dest-remove-btn">حذف</button>' +
          '</div>'
        );
      }
      function addBtnHtml() {
        return '<button type="button" class="pay-dest-add-row" data-guides-add><span aria-hidden="true">+</span><span>افزودن آموزش</span></button>';
      }
      function sync(root) {
        const hidden = root.querySelector('[data-guides-json]');
        const list = root.querySelector('[data-guides-list]');
        if (!hidden || !list) return;
        const entries = [];
        list.querySelectorAll('[data-guides-row]').forEach((row, idx) => {
          const rowId = row.getAttribute('data-guides-id') || uid();
          row.setAttribute('data-guides-id', rowId);
          const title = String((row.querySelector('.guides-title') || {}).value || '').trim();
          const body = String((row.querySelector('.guides-body') || {}).value || '').trim();
          if (!title && !body) return;
          let sort = idx;
          try {
            sort = parseInt(String((row.querySelector('.guides-sort') || {}).value || idx), 10);
            if (Number.isNaN(sort)) sort = idx;
          } catch (_) { sort = idx; }
          const en = row.querySelector('.guides-enabled');
          const dl = row.querySelector('.guides-deeplink');
          const aud = row.querySelector('.guides-audience');
          const styleSel = row.querySelector('.guides-style');
          entries.push({
            id: rowId,
            title: title,
            body: body,
            audience: aud && aud.value === 'reseller' ? 'reseller' : 'user',
            sort: sort,
            enabled: !!(en && en.checked),
            deep_link: !!(dl && dl.checked),
            style: styleSel ? styleSel.value : 'primary',
          });
        });
        hidden.value = JSON.stringify(entries);
      }
      function render(root, items) {
        const list = root.querySelector('[data-guides-list]');
        if (!list) return;
        const rows = (items && items.length ? items : [{}]).map((item) => rowHtml(item)).join('');
        list.innerHTML = rows + addBtnHtml();
        sync(root);
      }
      document.querySelectorAll('[data-guides-editor]').forEach((root) => {
        const hidden = root.querySelector('[data-guides-json]');
        render(root, parseJson(hidden ? hidden.value : '[]'));
        root.addEventListener('click', (e) => {
          if (e.target.closest('[data-guides-add]')) {
            e.preventDefault();
            const list = root.querySelector('[data-guides-list]');
            const add = list && list.querySelector('[data-guides-add]');
            const html = rowHtml({});
            if (add) add.insertAdjacentHTML('beforebegin', html);
            else if (list) list.insertAdjacentHTML('afterbegin', html);
            sync(root);
            return;
          }
          if (e.target.closest('.guides-remove')) {
            e.preventDefault();
            const row = e.target.closest('[data-guides-row]');
            const list = root.querySelector('[data-guides-list]');
            if (row && list) {
              row.remove();
              if (!list.querySelector('[data-guides-row]')) {
                const add = list.querySelector('[data-guides-add]');
                if (add) add.insertAdjacentHTML('beforebegin', rowHtml({}));
              }
              sync(root);
            }
          }
        });
        root.addEventListener('input', () => sync(root));
        root.addEventListener('change', () => sync(root));
      });
    })();

    /* Colors tab — section sub-tabs */
    (function () {
      const root = document.querySelector('[data-colors-subtabs]');
      if (!root) return;
      const tabs = root.querySelectorAll('.tab-btn');
      const panels = document.querySelectorAll('[data-colors-panel]');
      tabs.forEach((tab) => {
        tab.addEventListener('click', () => {
          const id = tab.getAttribute('data-colors-tab');
          tabs.forEach((t) => {
            const active = t === tab;
            t.classList.toggle('active', active);
            t.setAttribute('aria-selected', active ? 'true' : 'false');
          });
          panels.forEach((panel) => {
            const show = panel.getAttribute('data-colors-panel') === id;
            panel.classList.toggle('is-active', show);
            if (show) panel.removeAttribute('hidden');
            else panel.setAttribute('hidden', 'hidden');
          });
        });
      });
    })();

    /* Table bulk row selection — parallel to per-row actions, eligible-only */
    (function () {
      function faNum(n) {
        try {
          return Number(n).toLocaleString('fa-IR');
        } catch (_) {
          return String(n);
        }
      }
      function rowOps(tr) {
        const raw = (tr && tr.getAttribute('data-bulk-ops')) || '';
        return raw.split(',').map((s) => s.trim()).filter(Boolean);
      }
      function selectedRows(table) {
        return Array.from(table.querySelectorAll('.table-select-input:checked'))
          .map((inp) => inp.closest('tr'))
          .filter(Boolean);
      }
      function eligibleIds(table, op) {
        return selectedRows(table)
          .filter((tr) => rowOps(tr).includes(op))
          .map((tr) => {
            const inp = tr.querySelector('.table-select-input');
            return inp ? inp.value : '';
          })
          .filter(Boolean);
      }
      function findBulkBar(table) {
        const tw = table.closest('.table-wrap');
        let el = tw && tw.previousElementSibling;
        while (el) {
          if (el.hasAttribute && el.hasAttribute('data-table-bulk-bar')) return el;
          if (el.classList && el.classList.contains('table-wrap')) break;
          el = el.previousElementSibling;
        }
        const wrap = table.closest('.card, .ui-modal-panel');
        if (wrap) {
          /* Prefer bar immediately before this table's wrap when multiple tables share a card */
          const bars = wrap.querySelectorAll('[data-table-bulk-bar]');
          if (bars.length === 1) return bars[0];
        }
        return null;
      }
      function updateBar(table) {
        const bar = findBulkBar(table);
        if (!bar) return;
        const rows = selectedRows(table);
        const count = rows.length;
        const label = bar.querySelector('[data-bulk-count-label]');
        if (label) label.textContent = faNum(count) + ' مورد';

        const all = table.querySelector('.table-select-all-input');
        const inputs = table.querySelectorAll('.table-select-input');
        if (all && inputs.length) {
          all.indeterminate = count > 0 && count < inputs.length;
          all.checked = count === inputs.length && count > 0;
        }
        table.querySelectorAll('tbody tr').forEach((tr) => {
          const cb = tr.querySelector('.table-select-input');
          tr.classList.toggle('is-bulk-selected', !!(cb && cb.checked));
        });

        let anyEligible = false;
        bar.querySelectorAll('[data-bulk-op]').forEach((btn) => {
          const op = btn.getAttribute('data-bulk-op');
          const n = eligibleIds(table, op).length;
          const badge = btn.querySelector('[data-bulk-op-count]');
          if (badge) badge.textContent = faNum(n);
          const show = count > 0 && n > 0;
          if (show) anyEligible = true;
          btn.hidden = !show;
          btn.disabled = !show;
          btn.classList.toggle('is-empty', n === 0);
          btn.setAttribute('aria-label', (btn.querySelector('.table-bulk-op-label') || btn).textContent.trim() + ' (' + faNum(n) + ')');
        });
        /* No eligible bulk op for current selection → nothing to do */
        bar.hidden = count === 0 || !anyEligible;
      }
      function cleanReturnTo() {
        try {
          const u = new URL(window.location.href);
          u.searchParams.delete('ok');
          u.searchParams.delete('err');
          u.searchParams.delete('_');
          return u.pathname + (u.search || '');
        } catch (_) {
          return window.location.pathname + window.location.search;
        }
      }
      function submitBulk(table, actionKey, btn) {
        const bar = findBulkBar(table);
        const actionUrl = bar && bar.getAttribute('data-bulk-action');
        if (!actionUrl) return;
        const ids = eligibleIds(table, actionKey);
        if (!ids.length) return;
        const n = ids.length;
        const baseMsg = btn.getAttribute('data-bulk-confirm') || 'ادامه می‌دهید؟';
        const confirmMsg = baseMsg.replace(/\{n\}/g, faNum(n));
        const needsReason = btn.hasAttribute('data-bulk-confirm-reason');
        const confirmPhrase = (btn.getAttribute('data-bulk-confirm-phrase') || '').trim();
        const opts = {
          title: btn.getAttribute('data-bulk-confirm-title') || 'تأیید',
          message: confirmMsg,
          confirmLabel: btn.getAttribute('data-bulk-confirm-label') || 'تأیید',
          danger: btn.hasAttribute('data-bulk-confirm-danger'),
          warn: btn.hasAttribute('data-bulk-confirm-warn'),
          requireReason: needsReason && !confirmPhrase,
          reasonLabel: btn.getAttribute('data-bulk-confirm-reason-label') || 'علت',
          confirmPhrase: confirmPhrase,
          phraseLabel: btn.getAttribute('data-bulk-confirm-phrase-label') || '',
        };
        const run = (reason, phrase) => postBulk(actionUrl, actionKey, ids, reason || '', phrase || '');
        if (typeof window.panelConfirm === 'function') {
          window.panelConfirm(opts).then((result) => {
            if (!result || !result.ok) return;
            run(result.reason || '', result.phrase || '');
          });
          return;
        }
        run('', '');
      }
      function postBulk(url, actionKey, ids, reason, phrase) {
        const form = document.createElement('form');
        form.method = 'post';
        form.action = url;
        form.setAttribute('aria-hidden', 'true');
        form.style.cssText = 'position:fixed;left:-9999px;top:0;width:1px;height:1px;opacity:0;overflow:hidden;';
        const action = document.createElement('input');
        action.type = 'hidden';
        action.name = 'action';
        action.value = actionKey;
        form.appendChild(action);
        const ret = document.createElement('input');
        ret.type = 'hidden';
        ret.name = 'return_to';
        ret.value = cleanReturnTo();
        form.appendChild(ret);
        ids.forEach((id) => {
          const inp = document.createElement('input');
          inp.type = 'hidden';
          inp.name = 'ids';
          inp.value = id;
          form.appendChild(inp);
        });
        document.body.appendChild(form);
        const overrides = {};
        if (reason) {
          overrides.reason = reason;
          overrides.confirm_reason = reason;
        }
        if (phrase) overrides.confirm_phrase = phrase;
        if (typeof window.panelSubmitFormPost === 'function') {
          window.panelSubmitFormPost(form, overrides);
          return;
        }
        if (reason) {
          const r = document.createElement('input');
          r.type = 'hidden';
          r.name = 'reason';
          r.value = reason;
          form.appendChild(r);
        }
        if (phrase) {
          const p = document.createElement('input');
          p.type = 'hidden';
          p.name = 'confirm_phrase';
          p.value = phrase;
          form.appendChild(p);
        }
        ensureCsrfField(form);
        form.submit();
      }
      document.querySelectorAll('table[data-bulk-select]').forEach((table) => {
        table.addEventListener('change', (e) => {
          if (e.target.matches('.table-select-input, .table-select-all-input')) updateBar(table);
        });
        table.addEventListener('click', (e) => {
          const all = e.target.closest('.table-select-all-input');
          if (all) {
            const on = all.checked;
            table.querySelectorAll('.table-select-input').forEach((cb) => { cb.checked = on; });
            updateBar(table);
          }
        });
        const bar = findBulkBar(table);
        if (bar) {
          bar.addEventListener('click', (e) => {
            const btn = e.target.closest('[data-bulk-op]');
            if (!btn || btn.disabled || btn.hidden) return;
            e.preventDefault();
            submitBulk(table, btn.getAttribute('data-bulk-op'), btn);
          });
        }
        updateBar(table);
      });
    })();

    /* Capsule numeric steppers: [data-num-stepper] − / + with editable middle */
    (function initNumSteppers(){
      function parseStepperNumber(raw){
        const text = (typeof window.normalizePanelNumberText === 'function')
          ? window.normalizePanelNumberText(raw)
          : String(raw == null ? '' : raw);
        const n = Number(String(text).trim());
        return Number.isFinite(n) ? n : 0;
      }
      function formatStepperValue(n, step){
        const s = Number(step);
        if (Number.isFinite(s) && s > 0 && s < 1) {
          const digits = Math.min(4, Math.max(1, (String(s).split('.')[1] || '').length));
          const fixed = Number(n).toFixed(digits);
          return fixed.replace(/\.?0+$/, '') || '0';
        }
        if (Number.isFinite(s) && s >= 1 && Number.isInteger(s)) {
          return String(Math.trunc(n));
        }
        return String(n);
      }
      function clampStepper(n, root){
        let out = n;
        const minAttr = root.getAttribute('data-min');
        const maxAttr = root.getAttribute('data-max');
        if (minAttr != null && minAttr !== '' && Number.isFinite(Number(minAttr))) {
          out = Math.max(Number(minAttr), out);
        }
        if (maxAttr != null && maxAttr !== '' && Number.isFinite(Number(maxAttr))) {
          out = Math.min(Number(maxAttr), out);
        }
        return out;
      }
      function applyStep(root, dir){
        const input = root.querySelector('.num-stepper-input');
        if (!input) return;
        const stepRaw = root.getAttribute('data-step') || input.getAttribute('step') || '1';
        const step = Number(stepRaw);
        const delta = (Number.isFinite(step) && step !== 0 ? step : 1) * (dir < 0 ? -1 : 1);
        const next = clampStepper(parseStepperNumber(input.value) + delta, root);
        input.value = formatStepperValue(next, step);
        input.dispatchEvent(new Event('input', { bubbles: true }));
        input.dispatchEvent(new Event('change', { bubbles: true }));
      }
      document.addEventListener('click', (e) => {
        const btn = e.target.closest('[data-num-stepper-dec], [data-num-stepper-inc]');
        if (!btn) return;
        const root = btn.closest('[data-num-stepper]');
        if (!root) return;
        e.preventDefault();
        applyStep(root, btn.hasAttribute('data-num-stepper-dec') ? -1 : 1);
      });
    })();

    /* Clear named fields and submit — CSP-safe replacement for inline onclick. */
    document.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-clear-and-submit]');
      if (!btn) return;
      const form = btn.form || btn.closest('form');
      if (!form) return;
      e.preventDefault();
      const names = String(btn.getAttribute('data-clear-fields') || '')
        .split(',')
        .map((s) => s.trim())
        .filter(Boolean);
      names.forEach((name) => {
        const el = form.elements.namedItem(name);
        if (!el) return;
        if (el instanceof RadioNodeList) {
          Array.from(el).forEach((node) => {
            if ('value' in node) node.value = '';
          });
        } else if ('value' in el) {
          el.value = '';
        }
      });
      if (typeof form.requestSubmit === 'function') form.requestSubmit();
      else {
        ensureCsrfField(form);
        form.submit();
      }
    });
  })();

/* === Panel Update tab (moved out of inline template so CSP nonce always applies via panel.js) === */
(function initPanelUpdateTab() {
  function boot() {
    if (!document.getElementById('upd-root')) return;
const root = document.getElementById('upd-root');
  const startBtn = document.getElementById('upd-start');
  const refreshBtn = document.getElementById('upd-refresh');
  const fill = document.getElementById('upd-fill');
  const percentEl = document.getElementById('upd-percent');
  const stepEl = document.getElementById('upd-step');
  const stateEl = document.getElementById('upd-state-label');
  const toEl = document.getElementById('upd-to');
  const fromEl = document.getElementById('upd-from');
  const ops = document.getElementById('upd-ops');
  const rollbackBtn = document.getElementById('rollback-start');
  const rollbackSelect = document.getElementById('rollback-version');
  const channelSelect = document.getElementById('upd-channel');
  const migrationFlash = document.getElementById('upd-migration-flash');
  const versionFlash = document.getElementById('upd-version-flash');
  const remoteVerEl = document.getElementById('upd-remote-ver');
  const localVerEl = document.getElementById('upd-local-ver');
  const localBox = document.getElementById('upd-local-box');
  const remoteBox = document.getElementById('upd-remote-box');
  const changelogRoot = document.getElementById('upd-changelog');
  const changelogBlocks = document.getElementById('upd-changelog-blocks');
  const changelogTitle = document.getElementById('upd-changelog-title');
  let timer = null;
  let waitingRestart = false;
  let sawDown = false;
  let restartStartedAt = 0;
  let baselineBoot = null;
  let baselinePid = null;
  let expectedVersion = '';
  const RESTART_MSG = 'سرویس در حال راه‌اندازی مجدد است — ممکن است چند دقیقه طول بکشد. از صفحه خارج نشوید و رفرش نکنید.';
  const STAY_WARN = 'لطفاً تا پایان عملیات از این صفحه خارج نشوید و صفحه را رفرش نکنید.';
  let targetRemote = (root && root.dataset.remote) || '';
  let canStart = root && root.dataset.canStart === '1';
  let migrationBlocked = root && root.dataset.migrationBlocked === '1';
  let blockLeave = false;
  let channelBusy = false;

  function csrfToken(){
    const meta = document.querySelector('meta[name="csrf-token"]');
    if (meta && meta.content) return meta.content;
    const m = document.cookie.match(/(?:^|; )csrf=([^;]*)/);
    return m ? decodeURIComponent(m[1]) : '';
  }

  function updateFetchHeaders(){
    const headers = { 'Content-Type': 'application/json', 'Accept': 'application/json' };
    const tok = csrfToken();
    if (tok) headers['X-CSRF-Token'] = tok;
    return headers;
  }

  async function readUpdateResponse(res){
    const text = await res.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch (_) { data = null; }
    if (!res.ok) {
      const err = (data && data.error) || (
        res.status === 403
          ? 'نشست امنیتی منقضی شده؛ صفحه را تازه کنید و دوباره تلاش کنید.'
          : ('خطای سرور (' + res.status + ')')
      );
      return { ok: false, error: err };
    }
    if (!data || typeof data !== 'object') {
      return { ok: false, error: 'پاسخ نامعتبر از سرور' };
    }
    return data;
  }

  function setBlockLeave(on){ blockLeave = !!on; }
  window.addEventListener('beforeunload', function (e) {
    if (!blockLeave) return;
    e.preventDefault();
    e.returnValue = STAY_WARN;
    return STAY_WARN;
  });

  function showOps(on){
    if (ops) ops.hidden = !on;
    if (root) root.dataset.showOps = on ? '1' : '0';
    if (!on) setBlockLeave(false);
  }

  function setBusy(busy, label){
    if (startBtn) {
      if (busy) {
        startBtn.disabled = true;
        startBtn.textContent = label || 'در حال اجرا…';
      } else if (migrationBlocked) {
        startBtn.disabled = true;
        startBtn.textContent = 'آپدیت بلاک شد';
      } else {
        const retry = label === 'تلاش دوباره';
        startBtn.disabled = !canStart && !retry;
        startBtn.textContent = retry ? 'تلاش دوباره' : (canStart ? 'شروع آپدیت' : 'آپدیتی نیست');
      }
    }
    if (rollbackBtn) rollbackBtn.disabled = !!busy;
    if (rollbackSelect) rollbackSelect.disabled = !!busy;
    if (channelSelect) channelSelect.disabled = !!busy || channelBusy;
    if (busy) setBlockLeave(true);
  }

  function escHtml(s){
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function setMetaTone(available){
    if (localBox) {
      localBox.classList.toggle('update-meta-err', !!available);
      localBox.classList.toggle('update-meta-ok', !available);
    }
    if (remoteBox) {
      remoteBox.classList.toggle('update-meta-ok', !!available);
      remoteBox.classList.toggle('update-meta-err', false);
    }
  }

  function renderChangelog(cl){
    if (!changelogRoot || !changelogBlocks) return;
    const has = !!(cl && cl.has_notes && (cl.blocks || []).length);
    changelogRoot.hidden = !has;
    if (changelogTitle && cl && cl.title) changelogTitle.textContent = cl.title;
    if (!has) {
      changelogBlocks.innerHTML = '';
      return;
    }
    changelogBlocks.innerHTML = (cl.blocks || []).map(function (block) {
      const notes = (block.notes || []).map(function (n) {
        return '<li>' + escHtml(n) + '</li>';
      }).join('');
      return (
        '<div class="update-changelog-block">' +
          '<strong class="update-changelog-ver" dir="ltr">v' + escHtml(block.version) + '</strong>' +
          '<ul class="update-changelog-list">' + notes + '</ul>' +
        '</div>'
      );
    }).join('');
  }

  function applyChannelPayload(data){
    const info = (data && data.update_info) || {};
    const pre = (data && data.migration_preflight) || {};
    const chLabel = data.channel_label || info.channel_label || (data.channel === 'dev' ? 'توسعه' : 'پایدار');
    const checkMsg = data.channel_check_message || info.label || '';
    const checkTone = data.channel_check_tone || (info.update_available ? 'warn' : (info.checked ? 'ok' : 'warn'));
    targetRemote = info.remote_version || '';
    if (root) root.dataset.remote = targetRemote;
    migrationBlocked = !!pre.blocked;
    if (root) root.dataset.migrationBlocked = migrationBlocked ? '1' : '0';
    canStart = !!data.can_start_update && !migrationBlocked;
    if (root) root.dataset.canStart = canStart ? '1' : '0';
    if (localVerEl && data.local_version) {
      localVerEl.textContent = 'v' + data.local_version;
    }
    if (remoteVerEl) {
      remoteVerEl.textContent = targetRemote ? ('v' + targetRemote) : '—';
    }
    setMetaTone(!!info.update_available && !migrationBlocked);
    if (migrationFlash) {
      if (pre.blocked) {
        migrationFlash.hidden = false;
        migrationFlash.className = 'flash err';
        migrationFlash.textContent = pre.message || pre.label || '';
      } else if (pre.tone === 'warn' && pre.message) {
        migrationFlash.hidden = false;
        migrationFlash.className = 'flash warn';
        migrationFlash.textContent = pre.message;
      } else {
        migrationFlash.hidden = true;
        migrationFlash.textContent = '';
      }
    }
    if (versionFlash && root.dataset.showOps !== '1') {
      // Migration error already lives in #upd-migration-flash — never mirror it here.
      if (migrationBlocked) {
        versionFlash.hidden = true;
        versionFlash.textContent = '';
      } else {
        versionFlash.hidden = false;
        versionFlash.className = 'flash ' + (checkTone === 'ok' ? 'ok' : (checkTone === 'err' ? 'err' : 'warn'));
        if (checkMsg) {
          versionFlash.textContent = checkMsg;
        } else if (info.update_available) {
          versionFlash.textContent = 'برای کانال ' + chLabel + ' آپدیت جدید هست: ' +
            (info.version || data.local_version || '') + ' → ' + (info.remote_version || '');
        } else if (info.checked && !info.update_available) {
          versionFlash.textContent = 'برای کانال ' + chLabel + ' آپدیت جدیدی نیست — روی آخرین نسخه هستید.';
        } else {
          versionFlash.textContent = info.label || ('بررسی آپدیت کانال ' + chLabel + ' ناموفق');
        }
      }
    }
    if (stateEl && root.dataset.showOps !== '1') {
      if (migrationBlocked) stateEl.textContent = pre.label || 'مایگریشن ناسازگار';
      else if (info.update_available) stateEl.textContent = 'آپدیت کانال ' + chLabel + ' آماده است';
      else if (info.checked) stateEl.textContent = 'آخرین نسخه کانال ' + chLabel;
      else stateEl.textContent = info.label || '';
    }
    renderChangelog(data.changelog);
    // Header badge follows DEPLOYED_CHANNEL only — do not flip it on dropdown change.
    if (root) root.dataset.channel = data.channel || (channelSelect && channelSelect.value) || 'main';
    setBusy(false);
  }

  function markRestartUi(extra){
    showOps(true);
    setBusy(true, 'در حال راه‌اندازی مجدد…');
    if (stateEl) stateEl.textContent = (extra || RESTART_MSG);
    if (stepEl) stepEl.textContent = 'راه‌اندازی مجدد سرویس';
    if (fill) fill.style.width = '98%';
    if (percentEl) percentEl.textContent = '۹۸٪';
  }

  async function probeHealth(){
    // Use authenticated diagnostics — public /health is intentionally minimal ({ok:true})
    // and does not expose version/boot_id/pid needed to confirm a successful restart.
    try {
      const r = await fetch('/health/detail?_=' + Date.now(), { cache: 'no-store', credentials: 'same-origin' });
      if (!r.ok) return null;
      return await r.json();
    } catch (_) { return null; }
  }

  function finishReload(verifiedVersion){
    waitingRestart = false;
    setBlockLeave(false);
    showOps(false);
    if (stateEl) stateEl.textContent = 'آماده';
    clearInterval(timer); timer = null;
    const verNote = verifiedVersion ? (' · نسخه ' + verifiedVersion) : '';
    setTimeout(() => {
      const url = new URL(location.href);
      url.searchParams.set('tab', 'update');
      url.searchParams.set('ok', 'آپدیت با موفقیت اعمال شد' + verNote);
      url.searchParams.set('force', '1');
      location.href = url.pathname + '?' + url.searchParams.toString();
    }, 400);
  }

  function versionMatches(h){
    if (!expectedVersion) return true;
    if (!h || !h.version) return false;
    return String(h.version).trim() === expectedVersion;
  }

  function processChanged(h){
    if (!h) return false;
    if (baselineBoot && h.boot_id && String(h.boot_id) !== baselineBoot) return true;
    if (baselinePid && h.pid && Number(h.pid) !== baselinePid) return true;
    return false;
  }

  async function beginWaitingRestart(st){
    if (waitingRestart) return;
    expectedVersion = String(
      (st && (st.to_version || st.current_version)) ||
      (toEl && toEl.textContent) ||
      targetRemote || ''
    ).trim();
    if (expectedVersion === '—' || expectedVersion === '-') expectedVersion = '';
    waitingRestart = true;
    sawDown = false;
    restartStartedAt = Date.now();
    markRestartUi();
    if (st && st.pre_boot_id) baselineBoot = String(st.pre_boot_id);
    const h0 = await probeHealth();
    if (!baselineBoot && h0 && h0.boot_id) baselineBoot = String(h0.boot_id);
    if (h0 && h0.pid) baselinePid = Number(h0.pid);
    if (st && st.pre_boot_id && h0 && processChanged(h0) && versionMatches(h0)) {
      finishReload(h0.version || expectedVersion);
      return;
    }
    startPolling();
  }

  function applyStatus(st){
    if (!fill) return;
    if (st.to_version && toEl) toEl.textContent = st.to_version;
    if (st.from_version && fromEl) fromEl.textContent = st.from_version;
    if (st.pre_boot_id && !baselineBoot) baselineBoot = String(st.pre_boot_id);

    if (waitingRestart) {
      if (!st.awaiting_restart && st.state !== 'running') {
        if (st.state === 'error' || st.restart_required) {
          waitingRestart = false;
          showOps(true);
          setBusy(false, 'تلاش دوباره');
          if (stateEl) stateEl.textContent = st.message || st.error || 'خطا';
          canStart = true;
          clearInterval(timer); timer = null;
          return;
        }
        if (st.state === 'idle' || st.state === 'done') {
          finishReload(st.current_version || expectedVersion);
          return;
        }
      }
      markRestartUi();
      return;
    }
    if (st.awaiting_restart) {
      beginWaitingRestart(st);
      return;
    }
    if (st.state === 'idle' || (st.state === 'done' && !st.restart_required)) {
      showOps(false);
      setBusy(false);
      if (stateEl) stateEl.textContent = canStart ? 'آپدیت آماده است' : 'آماده';
      return;
    }
    if (st.state === 'running' || st.state === 'error' || st.restart_required) showOps(true);
    const pct = Number(st.percent || 0);
    fill.style.width = pct + '%';
    if (percentEl) percentEl.textContent = pct + '٪';
    if (stepEl) {
      stepEl.textContent = (st.state === 'error' || st.restart_required)
        ? 'خطا — دوباره تلاش کنید'
        : (st.state === 'running' ? 'در حال اعمال…' : 'آماده');
    }
    if (stateEl) stateEl.textContent = st.message || st.state || '';
    const modeLabel = st.mode === 'rollback' ? 'بازگشت' : 'آپدیت';
    if (st.state === 'running') {
      setBusy(true, 'در حال ' + modeLabel + '…');
      showOps(true);
    } else if (st.state === 'error' || st.restart_required) {
      showOps(true);
      canStart = true;
      setBusy(false, 'تلاش دوباره');
    } else {
      setBusy(false);
    }
  }

  async function poll(){
    if (waitingRestart) {
      const elapsed = Date.now() - restartStartedAt;
      const h = await probeHealth();
      if (!h) {
        sawDown = true;
        markRestartUi(elapsed > 180000 ? (RESTART_MSG + ' در صورت نیاز: sudo systemctl restart pgclockbot') : RESTART_MSG);
        return;
      }
      const changed = processChanged(h);
      const verOk = versionMatches(h);
      if (!changed && !sawDown) {
        markRestartUi('منتظر راه‌اندازی مجدد…' + (expectedVersion ? (' نسخه هدف: ' + expectedVersion) : ''));
        try {
          const res = await fetch('/update/status', { credentials: 'same-origin', cache: 'no-store' });
          if (res.ok) {
            const data = await res.json();
            if (!data.awaiting_restart) applyStatus(data);
          }
        } catch (_) {}
        return;
      }
      if (!changed && sawDown) {
        markRestartUi('سرویس برگشت؛ در حال تأیید نسخه…');
        return;
      }
      if (changed && verOk) {
        finishReload(h.version || expectedVersion);
        return;
      }
      if (changed && !verOk) {
        markRestartUi('منتظر نسخه ' + (expectedVersion || 'جدید') + ' (الان: ' + (h.version || '—') + ')…');
        if (!expectedVersion && sawDown) finishReload(h.version || '');
        return;
      }
      markRestartUi();
      return;
    }
    try {
      const res = await fetch('/update/status', { credentials: 'same-origin', cache: 'no-store' });
      if (!res.ok) throw new Error('status ' + res.status);
      const data = await res.json();
      applyStatus(data);
      if (data.state === 'running' || data.awaiting_restart) return;
      clearInterval(timer); timer = null;
    } catch (e) {
      if (stateEl) stateEl.textContent = 'خطا در دریافت وضعیت؛ دوباره تلاش می‌شود…';
    }
  }

  function startPolling(){
    if (timer) return;
    timer = setInterval(function () {
      if (document.hidden) return;
      poll();
    }, 1500);
    poll();
  }
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden && timer) poll();
  });

  async function beginUpdate(isRetry){
    showOps(true);
    setBusy(true, 'شروع…');
    waitingRestart = false;
    sawDown = false;
    baselineBoot = null;
    baselinePid = null;
    expectedVersion = '';
    try {
      const tok = csrfToken();
      const res = await fetch('/update/start', {
        method: 'POST',
        credentials: 'same-origin',
        headers: updateFetchHeaders(),
        body: JSON.stringify({
          target: targetRemote || null,
          force: !!isRetry,
          csrf_token: tok || undefined,
        })
      });
      const data = await readUpdateResponse(res);
      if (!data.ok) {
        if (stateEl) stateEl.textContent = data.error || 'شروع آپدیت ممکن نشد';
        canStart = true;
        setBusy(false, 'تلاش دوباره');
        return;
      }
      if (data.status) applyStatus(data.status);
      startPolling();
    } catch (e) {
      if (stateEl) stateEl.textContent = 'خطا در شروع آپدیت — اتصال یا نشست را بررسی کنید';
      canStart = true;
      setBusy(false, 'تلاش دوباره');
    }
  }

  if (startBtn) startBtn.addEventListener('click', () => {
    if (migrationBlocked) return;
    beginUpdate(/تلاش دوباره/.test(startBtn.textContent || ''));
  });
  if (refreshBtn) refreshBtn.addEventListener('click', () => {
    location.href = '/settings?tab=update&force=1';
  });
  if (channelSelect) {
    channelSelect.addEventListener('change', async () => {
      const next = (channelSelect.value || 'main').trim();
      const prev = (root && root.dataset.channel) || 'main';
      if (!next || next === prev) return;
      const nextLabel = next === 'dev' ? 'توسعه' : 'پایدار';
      channelBusy = true;
      channelSelect.disabled = true;
      if (startBtn) {
        startBtn.disabled = true;
        startBtn.textContent = 'در حال بررسی…';
      }
      if (stateEl) stateEl.textContent = 'در حال بررسی آپدیت کانال ' + nextLabel + '…';
      if (versionFlash) {
        versionFlash.hidden = false;
        versionFlash.className = 'flash warn';
        versionFlash.textContent = 'کانال به «' + nextLabel + '» تغییر کرد — در حال بررسی آپدیت این کانال…';
      }
      try {
        const tok = csrfToken();
        const res = await fetch('/update/channel', {
          method: 'POST',
          credentials: 'same-origin',
          headers: updateFetchHeaders(),
          body: JSON.stringify({ channel: next, csrf_token: tok || undefined }),
        });
        const data = await readUpdateResponse(res);
        if (!data.ok) {
          channelSelect.value = prev;
          if (versionFlash) {
            versionFlash.hidden = false;
            versionFlash.className = 'flash err';
            versionFlash.textContent = data.error || 'تغییر کانال ممکن نشد';
          }
          if (stateEl) stateEl.textContent = data.error || 'تغییر کانال ممکن نشد';
          setBusy(false);
          return;
        }
        applyChannelPayload(data);
      } catch (_) {
        channelSelect.value = prev;
        if (versionFlash) {
          versionFlash.hidden = false;
          versionFlash.className = 'flash err';
          versionFlash.textContent = 'خطا در بررسی آپدیت کانال — اتصال یا نشست را بررسی کنید';
        }
        if (stateEl) stateEl.textContent = 'خطا در تغییر کانال — اتصال یا نشست را بررسی کنید';
        setBusy(false);
      } finally {
        channelBusy = false;
        channelSelect.disabled = false;
      }
    });
  }

  if (rollbackBtn && rollbackSelect) {
    rollbackBtn.addEventListener('click', async () => {
      const ver = (rollbackSelect.value || '').trim();
      if (!ver) return;
      const ask = (window.panelConfirm
        ? window.panelConfirm({
            title: 'بازگشت نسخه',
            message: 'بازگشت به نسخه v' + ver + '؟ سرویس بعد از اعمال دوباره راه‌اندازی می‌شود.',
            warn: true,
            confirmLabel: 'شروع بازگشت',
          })
        : Promise.resolve({ ok: window.confirm('بازگشت به نسخه v' + ver + '؟') }));
      const result = await ask;
      if (!result || !result.ok) return;
      showOps(true);
      setBusy(true, 'شروع بازگشت…');
      waitingRestart = false;
      sawDown = false;
      baselineBoot = null;
      baselinePid = null;
      expectedVersion = ver;
      if (toEl) toEl.textContent = ver;
      try {
        const tok = csrfToken();
        const res = await fetch('/update/rollback', {
          method: 'POST',
          credentials: 'same-origin',
          headers: updateFetchHeaders(),
          body: JSON.stringify({ version: ver, csrf_token: tok || undefined })
        });
        const data = await readUpdateResponse(res);
        if (!data.ok) {
          if (stateEl) stateEl.textContent = data.error || 'بازگشت ممکن نشد';
          setBusy(false);
          return;
        }
        if (data.status) applyStatus(data.status);
        startPolling();
      } catch (e) {
        if (stateEl) stateEl.textContent = 'خطا در شروع بازگشت — اتصال یا نشست را بررسی کنید';
        setBusy(false);
      }
    });
  }

  if (root && root.dataset.running === '1') startPolling();
  const params = new URLSearchParams(location.search);
  if (!params.get('ok') && root && root.dataset.awaitingRestart === '1') {
    beginWaitingRestart({
      to_version: (toEl && toEl.textContent) || targetRemote || '',
      pre_boot_id: root.dataset.preBoot || ''
    });
  }
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();

/* === Sidebar section accordion (no work-mode tabs) ===
   Only the section that owns the current page stays open; others stay collapsed. */
(function initNavWorkModes() {
  function boot() {
    var nav = document.querySelector('[data-side-nav]');
    if (!nav) return;

    function sectionFromPath(pathname, search) {
      var path = pathname || '/';
      var q = search || '';
      if (path.indexOf('/help') === 0) return ''; /* help is a bottom chip, not an accordion */
      if (path.indexOf('/pg') === 0) return 'pg';
      if (path === '/home' || path.indexOf('/inbox') === 0 || path.indexOf('/security') === 0) {
        return 'system';
      }
      if (path.indexOf('/settings') === 0) {
        var tab = 'welcome';
        try { tab = new URLSearchParams(q).get('tab') || 'welcome'; } catch (_) {}
        if (['backup', 'pwa', 'ssl', 'update'].indexOf(tab) >= 0) return 'system';
        return 'shop';
      }
      if (path.indexOf('/shop-settings') === 0) return 'shop';
      var shopPrefixes = [
        '/dashboard', '/users', '/finance', '/orders', '/payments', '/plans',
        '/broadcast', '/loyalty', '/message-variables', '/resellers', '/tickets'
      ];
      for (var i = 0; i < shopPrefixes.length; i++) {
        if (path === shopPrefixes[i] || path.indexOf(shopPrefixes[i] + '/') === 0) return 'shop';
      }
      return 'system';
    }

    function activeSectionKey() {
      var active = nav.querySelector('.nav-item.active');
      if (active) {
        var parent = active.closest('.nav-section[data-nav-mode]');
        if (parent) return parent.getAttribute('data-nav-mode');
      }
      return sectionFromPath(location.pathname, location.search);
    }

    function setOpenSection(openKey) {
      var sections = nav.querySelectorAll('.nav-section[data-nav-mode]');
      var found = false;
      if (openKey) {
        for (var i = 0; i < sections.length; i++) {
          if (sections[i].getAttribute('data-nav-mode') === openKey) found = true;
        }
        if (!found && sections.length) openKey = sections[0].getAttribute('data-nav-mode');
      }
      for (var j = 0; j < sections.length; j++) {
        var section = sections[j];
        var key = section.getAttribute('data-nav-mode');
        var btn = section.querySelector('[data-nav-collapse]');
        var collapsed = !openKey || key !== openKey;
        section.classList.toggle('is-collapsed', collapsed);
        if (btn) btn.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
      }
      /* Only scroll when the active link is in the *open* section — scrolling a
         display:none item in a just-collapsed accordion caused sidebar jump/lag. */
      var active = nav.querySelector('.nav-section[data-nav-mode]:not(.is-collapsed) .nav-item.active');
      if (active && typeof active.scrollIntoView === 'function') {
        try { active.scrollIntoView({ block: 'nearest' }); } catch (_) {}
      }
    }

    setOpenSection(activeSectionKey());

    nav.addEventListener('click', function (e) {
      var btn = e.target.closest('[data-nav-collapse]');
      if (!btn || !nav.contains(btn)) return;
      e.preventDefault();
      var key = btn.getAttribute('data-nav-collapse');
      var section = btn.closest('.nav-section');
      if (!section || !key) return;
      /* Exclusive accordion: opening one closes the rest; collapsing the
         open one is allowed so the user can peek without leaving the page. */
      if (section.classList.contains('is-collapsed')) {
        setOpenSection(key);
      } else {
        section.classList.add('is-collapsed');
        btn.setAttribute('aria-expanded', 'false');
      }
      /* Drop sticky focus chrome after tap (iOS/PWA) */
      if (typeof btn.blur === 'function') btn.blur();
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();


/* Risk-color tag picker — reliable radio selection inside fragment modals
   (WebKit previously dropped zero-size radios so saves looked like no-ops). */
(function () {
  function syncPicker(picker) {
    if (!picker) return;
    picker.querySelectorAll('.color-tag-option').forEach(function (opt) {
      var input = opt.querySelector('input[type="radio"]');
      opt.classList.toggle('is-selected', !!(input && input.checked));
    });
  }
  function bindPicker(picker) {
    if (!picker || picker.dataset.boundColorTag === '1') return;
    picker.dataset.boundColorTag = '1';
    picker.querySelectorAll('.color-tag-option').forEach(function (opt) {
      opt.addEventListener('click', function () {
        var input = opt.querySelector('input[type="radio"]');
        if (!input) return;
        input.checked = true;
        syncPicker(picker);
      });
    });
    picker.addEventListener('change', function (e) {
      if (e.target && e.target.matches('input[type="radio"]')) syncPicker(picker);
    });
    syncPicker(picker);
  }
  function bindAll(root) {
    var scope = root && root.querySelectorAll ? root : document;
    scope.querySelectorAll('[data-color-tag-picker]').forEach(bindPicker);
  }
  function bootPickers() {
    bindAll(document);
    document.addEventListener('panel:dom-ready', function (e) {
      bindAll((e && e.detail && e.detail.root) || document);
    });
    try {
      var mo = new MutationObserver(function (mutations) {
        for (var i = 0; i < mutations.length; i++) {
          var nodes = mutations[i].addedNodes || [];
          for (var j = 0; j < nodes.length; j++) {
            var node = nodes[j];
            if (!node || node.nodeType !== 1) continue;
            if (node.matches && node.matches('[data-color-tag-picker]')) bindPicker(node);
            if (node.querySelectorAll) {
              node.querySelectorAll('[data-color-tag-picker]').forEach(bindPicker);
            }
          }
        }
      });
      mo.observe(document.documentElement, { childList: true, subtree: true });
    } catch (e) {}
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bootPickers);
  } else {
    bootPickers();
  }
})();

/* Tag filter dropdown — navigate on change (keeps search form separate). */
(function () {
  function bindNav(sel) {
    if (!sel || sel.dataset.boundColorTagNav === '1') return;
    sel.dataset.boundColorTagNav = '1';
    sel.addEventListener('change', function () {
      var href = sel.value;
      if (href) window.location.assign(href);
    });
  }
  function bindAll(root) {
    var scope = root && root.querySelectorAll ? root : document;
    scope.querySelectorAll('select[data-color-tag-nav]').forEach(bindNav);
  }
  function boot() {
    bindAll(document);
    document.addEventListener('panel:dom-ready', function (e) {
      bindAll((e && e.detail && e.detail.root) || document);
    });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
