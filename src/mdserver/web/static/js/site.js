/* Public site behaviour + shared rendering helpers (also used by the editor preview). */
(function () {
    'use strict';

    const store = {
        get(k) { try { return localStorage.getItem(k); } catch { return null; } },
        set(k, v) { try { localStorage.setItem(k, v); } catch { /* storage unavailable */ } },
    };

    // ---- copy-to-clipboard for code blocks
    let liveRegion = null;
    function announce(msg) {
        if (!liveRegion) {
            liveRegion = document.createElement('div');
            liveRegion.className = 'sr-only';
            liveRegion.setAttribute('aria-live', 'polite');
            document.body.appendChild(liveRegion);
        }
        liveRegion.textContent = '';
        setTimeout(() => { liveRegion.textContent = msg; }, 30);
    }

    async function copyText(text) {
        try {
            await navigator.clipboard.writeText(text);
            return true;
        } catch {
            // fallback for plain-http origins where the Clipboard API is unavailable
            const ta = document.createElement('textarea');
            ta.value = text;
            ta.setAttribute('readonly', '');
            ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0;';
            document.body.appendChild(ta);
            ta.select();
            let ok = false;
            try { ok = document.execCommand('copy'); } catch { ok = false; }
            ta.remove();
            return ok;
        }
    }

    function onCopyClick(e) {
        const btn = e.currentTarget;
        const code = btn.closest('.code-block').querySelector('pre > code');
        copyText(code.textContent.replace(/\n$/, '')).then(ok => {
            btn.classList.toggle('is-copied', ok);
            btn.querySelector('.copy-label').textContent = ok ? 'Copied' : 'Press Ctrl+C';
            announce(ok ? 'Code copied to clipboard' : 'Copy failed');
            if (!ok) {   // select the code so the reader can copy it manually
                const range = document.createRange();
                range.selectNodeContents(code);
                const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(range);
            }
            clearTimeout(btn._reset);
            btn._reset = setTimeout(() => {
                btn.classList.remove('is-copied');
                btn.querySelector('.copy-label').textContent = 'Copy';
            }, 1800);
        });
    }

    /** Wrap a <pre> in .code-block with a language label and a copy button (idempotent). */
    function addCopyButton(pre) {
        if (pre.parentElement && pre.parentElement.classList.contains('code-block')) return;
        const code = pre.querySelector(':scope > code');
        const wrap = document.createElement('div');
        wrap.className = 'code-block';
        const lang = code && [...code.classList].find(c => c.startsWith('language-'));
        if (lang) wrap.dataset.lang = lang.slice(9);
        pre.replaceWith(wrap);
        wrap.appendChild(pre);

        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'copy-btn';
        btn.setAttribute('aria-label', lang ? `Copy ${lang.slice(9)} code` : 'Copy code');
        btn.innerHTML = '<svg aria-hidden="true" viewBox="0 0 16 16" width="14" height="14"><rect x="5.5" y="5.5" width="8" height="8" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.4"/><path d="M10.5 3.5v-.5A1.5 1.5 0 0 0 9 1.5H4A1.5 1.5 0 0 0 2.5 3v5A1.5 1.5 0 0 0 4 9.5h.5" fill="none" stroke="currentColor" stroke-width="1.4"/></svg><span class="copy-label">Copy</span>';
        btn.addEventListener('click', onCopyClick);
        wrap.appendChild(btn);
    }

    /** Highlight code, label languages and add copy buttons inside `root`. */
    function enhanceCode(root) {
        root.querySelectorAll('pre > code').forEach(code => {
            if (window.hljs && !code.dataset.highlighted && !code.classList.contains('language-text')) {
                try { window.hljs.highlightElement(code); } catch { /* unknown language */ }
            }
            addCopyButton(code.parentElement);
        });
    }

    /** enhanceCode + typeset maths inside `root`. */
    function enhance(root) {
        enhanceCode(root);
        if (window.MathJax && window.MathJax.typesetPromise) {
            if (window.MathJax.typesetClear) window.MathJax.typesetClear([root]);
            window.MathJax.typesetPromise([root]).catch(() => {});
        }
    }
    window.BlogMD = { enhance, enhanceCode, store };

    // ---- theme: System (follow OS) -> Light -> Dark, saved per browser
    const THEMES = ['system', 'light', 'dark'];
    const THEME_NAMES = { system: 'System', light: 'Light', dark: 'Dark' };
    function currentTheme() {
        const t = document.documentElement.getAttribute('data-theme');
        return t === 'light' || t === 'dark' ? t : 'system';
    }
    function applyTheme(theme) {
        if (theme === 'system') {
            document.documentElement.removeAttribute('data-theme');
            try { localStorage.removeItem('blogmd:theme'); } catch { /* ignore */ }
        } else {
            document.documentElement.setAttribute('data-theme', theme);
            store.set('blogmd:theme', theme);
        }
        syncToggles();
    }
    function syncToggles() {
        const t = currentTheme();
        const next = THEMES[(THEMES.indexOf(t) + 1) % THEMES.length];
        document.querySelectorAll('[data-theme-toggle]').forEach(btn => {
            btn.dataset.state = t;
            const label = `Theme: ${THEME_NAMES[t]}`;
            btn.setAttribute('aria-label', `${label}. Switch to ${THEME_NAMES[next]}`);
            btn.title = `${label} (click for ${THEME_NAMES[next]})`;
            const text = btn.querySelector('.theme-label');
            if (text) text.textContent = label;
        });
    }
    document.querySelectorAll('[data-theme-toggle]').forEach(btn => btn.addEventListener('click', () => {
        applyTheme(THEMES[(THEMES.indexOf(currentTheme()) + 1) % THEMES.length]);
    }));
    // another tab changed the theme
    window.addEventListener('storage', e => {
        if (e.key !== 'blogmd:theme') return;
        if (e.newValue === 'light' || e.newValue === 'dark') document.documentElement.setAttribute('data-theme', e.newValue);
        else document.documentElement.removeAttribute('data-theme');
        syncToggles();
    });
    syncToggles();

    const body = document.getElementById('post-body');
    if (body) {
        enhance(body);
        window.addEventListener('hljs-ready', () => enhance(body), { once: true });
    }

    // ---- sidebar open/close (mobile)
    const menuBtn = document.querySelector('.menu-btn');
    const scrim = document.querySelector('.scrim');
    function setNav(open) {
        document.body.classList.toggle('nav-open', open);
        if (scrim) scrim.hidden = !open;
        if (menuBtn) menuBtn.setAttribute('aria-expanded', String(open));
    }
    document.querySelectorAll('[data-sidebar-toggle]').forEach(el =>
        el.addEventListener('click', () => setNav(!document.body.classList.contains('nav-open'))));
    document.addEventListener('keydown', e => { if (e.key === 'Escape') setNav(false); });

    // ---- remember which folders are expanded
    document.querySelectorAll('.tree details[data-path]').forEach(d => {
        const key = 'nav:' + d.dataset.path;
        const saved = store.get(key);
        if (saved !== null && !d.querySelector('[aria-current]')) d.open = saved === '1';
        d.addEventListener('toggle', () => store.set(key, d.open ? '1' : '0'));
    });
    // clicking the folder name navigates; clicking the twisty/count toggles
    document.querySelectorAll('.tree summary .tree-link').forEach(a =>
        a.addEventListener('click', e => e.stopPropagation()));

    const active = document.querySelector('.nav-scroll [aria-current]');
    if (active) active.scrollIntoView({ block: 'center' });

    // ---- filter
    const filter = document.querySelector('[data-nav-filter]');
    const empty = document.querySelector('.nav-empty');
    if (filter) {
        const items = [...document.querySelectorAll('.tree-item')];
        const snapshot = new Map();
        filter.addEventListener('input', () => {
            const q = filter.value.trim().toLowerCase();
            if (q && !snapshot.size) document.querySelectorAll('.tree details').forEach(d => snapshot.set(d, d.open));
            let shown = 0;
            items.forEach(li => { li.hidden = !!q; });
            if (!q) {
                snapshot.forEach((open, d) => { d.open = open; });
                snapshot.clear();
                if (empty) empty.hidden = true;
                return;
            }
            items.forEach(li => {
                if (!li.dataset.label.includes(q)) return;
                shown++;
                // show the match and every ancestor folder
                for (let el = li; el; el = el.parentElement.closest('.tree-item')) {
                    el.hidden = false;
                    const d = el.querySelector(':scope > details');
                    if (d && el !== li) d.open = true;
                }
                const own = li.querySelector(':scope > details');
                if (own) { own.open = true; own.querySelectorAll('.tree-item').forEach(c => { c.hidden = false; }); }
            });
            if (empty) empty.hidden = shown > 0;
        });
        document.addEventListener('keydown', e => {
            if (e.key === '/' && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) {
                e.preventDefault();
                setNav(true);
                filter.focus();
            }
        });
    }
})();
