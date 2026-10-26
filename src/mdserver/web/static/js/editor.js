/* Writers area editor: live preview, saving/autosave, drag & drop media, formatting. */
(function () {
    'use strict';
    const { api, upload, toast, el, mediaTile, fmtSize } = window.Writer;
    const { enhance, store } = window.BlogMD;
    const $ = sel => document.querySelector(sel);

    const editor = $('#editor');
    const f = {
        title: $('#f-title'), path: $('#f-path'), description: $('#f-description'),
        order: $('#f-order'), status: $('#f-status'), content: $('#f-content'),
    };
    const ta = f.content;
    const preview = $('#preview');
    const previewScroll = $('#preview-scroll');
    const saveState = $('#save-state');
    const pathMsg = $('#path-msg');

    let post = window.EDITOR_POST;          // null until first save
    let saving = false, saveQueued = false;
    let savedSnapshot = post ? JSON.stringify(payload()) : null;
    const folderPrefix = (f.path.value || '').replace(/[^/]*$/, '');   // "weekly/" from ?folder=weekly

    // ================================================================ utilities
    const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
    const slugify = s => (s || '').toLowerCase().normalize('NFKD').replace(/[̀-ͯ]/g, '')
        .replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 80);

    function payload() {
        return {
            title: f.title.value.trim(),
            path: f.path.value.trim().replace(/^\/+|\/+$/g, ''),
            description: f.description.value.trim(),
            nav_order: parseInt(f.order.value, 10) || 0,
            status: f.status.value,
            content: ta.value,
        };
    }
    const isDirty = () => JSON.stringify(payload()) !== savedSnapshot;

    function setState(state, text) {
        saveState.dataset.state = state;
        saveState.textContent = text || {
            new: 'Not saved yet', dirty: 'Unsaved changes', saving: 'Saving…', saved: 'Saved',
            error: 'Save failed',
        }[state];
        if (state === 'saved') saveState.title = 'Saved at ' + new Date().toLocaleTimeString();
    }

    // ================================================================ text editing
    /** Replace the selection, keeping the browser's undo stack where possible. */
    function replaceSelection(text, selectInner) {
        ta.focus();
        const start = ta.selectionStart;
        if (!text && start === ta.selectionEnd) return;
        const ok = document.execCommand && (text
            ? document.execCommand('insertText', false, text)
            : document.execCommand('delete'));
        if (!ok) {
            ta.setRangeText(text, ta.selectionStart, ta.selectionEnd, 'end');
            ta.dispatchEvent(new Event('input', { bubbles: true }));
        }
        if (selectInner) ta.setSelectionRange(start + selectInner[0], start + selectInner[1]);
        else ta.setSelectionRange(start + text.length, start + text.length);
    }

    function wrap(before, after, placeholder) {
        const { selectionStart: s, selectionEnd: e, value } = ta;
        const sel = value.slice(s, e) || placeholder;
        replaceSelection(before + sel + after, [before.length, before.length + sel.length]);
    }

    /** Prefix every selected line (toggle off if all lines already have it). */
    function prefixLines(prefixFn) {
        const v = ta.value;
        const s = v.lastIndexOf('\n', ta.selectionStart - 1) + 1;
        let e = v.indexOf('\n', ta.selectionEnd);
        if (e === -1) e = v.length;
        const lines = v.slice(s, e).split('\n');
        const all = lines.every((l, i) => l.startsWith(prefixFn(i)));
        const out = lines.map((l, i) => all ? l.slice(prefixFn(i).length) : prefixFn(i) + l).join('\n');
        ta.setSelectionRange(s, e);
        replaceSelection(out, [0, out.length]);
    }

    /** Insert text as its own block (blank line before and after). */
    function insertBlock(text) {
        const v = ta.value, s = ta.selectionStart, e = ta.selectionEnd;
        const before = v.slice(0, s), after = v.slice(e);
        const pre = !before ? '' : before.endsWith('\n\n') ? '' : before.endsWith('\n') ? '\n' : '\n\n';
        const post_ = after.startsWith('\n\n') ? '' : after.startsWith('\n') ? '\n' : '\n\n';
        replaceSelection(pre + text + post_);
        const caret = s + pre.length + text.length;
        ta.setSelectionRange(caret, caret);
    }

    const actions = {
        bold: () => wrap('**', '**', 'bold text'),
        italic: () => wrap('*', '*', 'italic text'),
        strike: () => wrap('~~', '~~', 'struck text'),
        mark: () => wrap('==', '==', 'highlighted'),
        code: () => wrap('`', '`', 'code'),
        h2: () => prefixLines(() => '## '),
        h3: () => prefixLines(() => '### '),
        quote: () => prefixLines(() => '> '),
        ul: () => prefixLines(() => '- '),
        ol: () => prefixLines(i => `${i + 1}. `),
        task: () => prefixLines(() => '- [ ] '),
        link: () => {
            const sel = ta.value.slice(ta.selectionStart, ta.selectionEnd);
            if (/^https?:\/\//.test(sel)) replaceSelection(`[link text](${sel})`, [1, 10]);
            else wrap('[', '](https://)', 'link text');
        },
        codeblock: () => {
            const sel = ta.value.slice(ta.selectionStart, ta.selectionEnd);
            insertBlock('```python\n' + (sel || '') + '\n```');
            if (!sel) { const p = ta.selectionStart - 4; ta.setSelectionRange(p, p); }
        },
        table: () => insertBlock('| Column | Column |\n| ------ | ------ |\n| Cell   | Cell   |'),
        hr: () => insertBlock('---'),
        upload: () => $('#file-input').click(),
    };
    document.querySelectorAll('[data-md]').forEach(b =>
        b.addEventListener('click', () => actions[b.dataset.md]()));

    // ================================================================ live preview
    let renderSeq = 0, renderCtl = null;
    const sources = new WeakMap();     // top-level preview node -> html it was rendered from

    function keyOf(node) {
        return node.nodeType === 1 ? node.outerHTML : '#' + node.nodeType + ':' + node.textContent;
    }

    /** Patch only changed top-level blocks, so images/maths don't flicker while typing. */
    function patchPreview(html) {
        const tmp = document.createElement('div');
        tmp.innerHTML = html || '<p class="dim">Nothing to preview yet.</p>';
        const incoming = [...tmp.childNodes];
        const current = [...preview.childNodes];
        const changed = [], removed = [];
        incoming.forEach((raw, i) => {
            const key = keyOf(raw);
            const old = current[i];
            if (old && sources.get(old) === key) return;
            // enhance before inserting: a top-level <pre> gets wrapped in .code-block,
            // and the wrapper is what must be tracked as this block's node
            let node = raw;
            if (raw.nodeType === 1) {
                const holder = document.createElement('div');
                holder.appendChild(raw);
                enhanceCode(holder);
                node = holder.firstChild;
            }
            sources.set(node, key);
            if (old) { removed.push(old); preview.replaceChild(node, old); } else preview.appendChild(node);
            if (node.nodeType === 1) changed.push(node);
        });
        current.slice(incoming.length).forEach(n => { removed.push(n); n.remove(); });

        changed.forEach(n => {
            // show in-flight uploads as a box instead of a broken image
            n.querySelectorAll('img[src^="uploading:"], img[alt^="Uploading "]:not([src])').forEach(img => {
                img.replaceWith(el('span', { class: 'uploading-chip' }, img.alt || 'Uploading…'));
            });
        });
        if (window.MathJax && window.MathJax.typesetPromise) {
            const els = removed.filter(n => n.nodeType === 1);
            if (els.length && window.MathJax.typesetClear) window.MathJax.typesetClear(els);
            if (changed.length) window.MathJax.typesetPromise(changed).catch(() => {});
        }
    }

    const enhanceCode = root => window.BlogMD.enhanceCode(root);   // highlight + copy buttons (site.js)

    async function render() {
        const seq = ++renderSeq;
        if (renderCtl) renderCtl.abort();
        renderCtl = new AbortController();
        const t0 = performance.now();
        try {
            const data = await api('/write/api/render', {
                method: 'POST', json: { content: ta.value, post_id: post && post.id }, signal: renderCtl.signal,
            });
            if (seq !== renderSeq) return;
            patchPreview(data.html);
            $('#render-ms').textContent = `${Math.round(performance.now() - t0)} ms`;
            syncScroll();
        } catch (e) {
            if (e.name !== 'AbortError') $('#render-ms').textContent = 'preview error';
        }
    }
    const scheduleRender = debounce(render, 110);
    window.addEventListener('hljs-ready', () => enhanceCode(preview), { once: true });

    function renderHeader() {
        document.querySelector('[data-preview="title"]').textContent = f.title.value || 'Untitled';
        const d = document.querySelector('[data-preview="description"]');
        d.textContent = f.description.value;
        d.hidden = !f.description.value;
        const a = window.EDITOR_AUTHOR;
        if (a) document.querySelector('[data-preview="author"]').textContent = a.display_name;
    }

    // scroll sync: editor drives preview
    let syncing = false;
    function syncScroll() {
        if (editor.dataset.view !== 'split') return;
        const max = ta.scrollHeight - ta.clientHeight;
        const ratio = max > 0 ? ta.scrollTop / max : 0;
        syncing = true;
        previewScroll.scrollTop = ratio * (previewScroll.scrollHeight - previewScroll.clientHeight);
        requestAnimationFrame(() => { syncing = false; });
    }
    ta.addEventListener('scroll', () => { if (!syncing) syncScroll(); });

    // ================================================================ status bar
    function updateStats() {
        const text = ta.value;
        const words = (text.replace(/```[\s\S]*?```/g, ' ').match(/[\p{L}\p{N}'’-]+/gu) || []).length;
        $('#st-words').textContent = `${words.toLocaleString()} word${words === 1 ? '' : 's'}`;
        $('#st-read').textContent = `${Math.max(1, Math.round(words / 220))} min read`;
        updateCaret();
    }
    function updateCaret() {
        const before = ta.value.slice(0, ta.selectionStart);
        const line = before.split('\n').length;
        const col = ta.selectionStart - before.lastIndexOf('\n');
        $('#st-pos').textContent = `Ln ${line}, Col ${col}`;
    }
    ta.addEventListener('keyup', updateCaret);
    ta.addEventListener('click', updateCaret);

    // ================================================================ path handling
    async function checkPath() {
        const p = payload().path;
        if (!p && !post) { pathMsg.textContent = 'Filled in from the title.'; pathMsg.className = 'hint'; return; }
        try {
            const r = await api(`/write/api/check-path?path=${encodeURIComponent(p)}&id=${post ? post.id : ''}`);
            pathMsg.textContent = r.ok ? `Will be published at /blog/${r.path}${r.path ? '/' : ''}` : r.error;
            pathMsg.className = 'hint ' + (r.ok ? 'ok' : 'bad');
        } catch { /* ignore */ }
    }
    const scheduleCheckPath = debounce(checkPath, 350);

    f.title.addEventListener('input', () => {
        if (f.path.dataset.auto) { f.path.value = folderPrefix + slugify(f.title.value); scheduleCheckPath(); }
        renderHeader();
        changed();
    });
    f.path.addEventListener('input', () => { delete f.path.dataset.auto; scheduleCheckPath(); changed(); });
    f.description.addEventListener('input', () => { renderHeader(); changed(); });
    f.order.addEventListener('input', changed);
    f.status.addEventListener('change', changed);

    // ================================================================ saving
    const backupKey = () => 'blogmd:backup:' + (post ? post.id : 'new');
    const backupLocally = debounce(() => {
        if (isDirty()) store.set(backupKey(), JSON.stringify({ at: Date.now(), data: payload() }));
    }, 800);

    const scheduleAutosave = debounce(() => {
        const p = payload();
        if (p.status === 'draft' && p.title && (post || p.content.trim()) && isDirty()) save({ auto: true });
    }, 2500);

    function changed() {
        if (isDirty()) setState('dirty'); else if (post) setState('saved');
        backupLocally();
        scheduleAutosave();
    }

    async function save({ auto = false, force = false } = {}) {
        if (saving) { saveQueued = true; return; }
        const p = payload();
        if (!p.title) {
            if (!auto) { toast('Give the post a title first.', 'error'); f.title.focus(); }
            return;
        }
        saving = true;
        setState('saving');
        try {
            let data;
            if (!post) {
                data = await api('/write/api/posts', { method: 'POST', json: p });
                store.set(backupKey(), '');
                post = data.post;
                history.replaceState(null, '', `/write/edit/${post.id}`);
            } else {
                data = await api(`/write/api/posts/${post.id}`, {
                    method: 'PUT', json: { ...p, base_updated_at: post.updated_at, force },
                });
                post = data.post;
            }
            // server may normalise the path
            if (f.path.value !== post.path) f.path.value = post.path;
            delete f.path.dataset.auto;
            savedSnapshot = JSON.stringify({ ...p, path: post.path });
            try { localStorage.removeItem(backupKey()); } catch { /* ignore */ }
            const view = $('#view-link');
            view.href = post.url; view.hidden = false;
                    setState(isDirty() ? 'dirty' : 'saved');
            if (!auto) toast(p.status === 'draft' ? 'Draft saved' : `Saved · live at ${post.url}`, 'ok', 2500);
        } catch (e) {
            setState('error', e.message.length < 40 ? e.message : 'Save failed');
            if (e.status === 409 && e.data && e.data.conflict) {
                saving = false;
                if (confirm('This post was saved somewhere else since you opened it.\n\nOverwrite it with your version?')) {
                    return save({ force: true });
                }
            } else {
                if (e.data && e.data.field === 'path') { $('#ed-details').hidden = false; f.path.focus(); }
                toast(e.message, 'error', 6000);
            }
        } finally {
            saving = false;
            if (saveQueued) { saveQueued = false; if (isDirty()) save({ auto: true }); }
        }
    }

    $('#save-btn').addEventListener('click', () => save());
    window.addEventListener('beforeunload', e => { if (isDirty()) { e.preventDefault(); e.returnValue = ''; } });

    const del = $('#delete-btn');
    if (del) del.addEventListener('click', async () => {
        if (!post || !confirm(`Delete "${post.title}"? This can't be undone.`)) return;
        try {
            await api(`/write/api/posts/${post.id}`, { method: 'DELETE' });
            savedSnapshot = JSON.stringify(payload());
            location.href = '/write/';
        } catch (e) { toast(e.message, 'error'); }
    });

    // offer to restore a local backup newer than the saved version
    (function restoreBackup() {
        let b;
        try { b = JSON.parse(store.get(backupKey()) || 'null'); } catch { b = null; }
        if (!b || !b.data) return;
        const savedAt = post ? new Date(post.updated_at).getTime() : 0;
        if (b.at <= savedAt || JSON.stringify(b.data) === savedSnapshot) return;
        const bar = el('div', { class: 'notice notice-warn restore-bar' },
            el('strong', {}, 'Recovered'),
            `Unsaved changes from ${new Date(b.at).toLocaleString()} were found on this device. `,
            el('button', { type: 'button', class: 'btn btn-sm', onclick: () => {
                f.title.value = b.data.title; f.path.value = b.data.path; f.description.value = b.data.description;
                f.order.value = b.data.nav_order; f.status.value = b.data.status; ta.value = b.data.content;
                delete f.path.dataset.auto;
                bar.remove(); renderHeader(); render(); updateStats(); changed();
            } }, 'Restore'),
            el('button', { type: 'button', class: 'btn btn-sm btn-ghost', onclick: () => { store.set(backupKey(), ''); bar.remove(); } }, 'Discard'),
        );
        editor.prepend(bar);
    })();

    // ================================================================ uploads + drag & drop
    let uploadId = 0;
    const active = new Map();   // id -> progress 0..1
    const queue = [];
    let running = 0;

    function updateUploadStatus() {
        const st = $('#st-uploads');
        if (!active.size) { st.textContent = ''; return; }
        const pct = [...active.values()].reduce((a, b) => a + b, 0) / active.size;
        st.textContent = `Uploading ${active.size} file${active.size > 1 ? 's' : ''} · ${Math.round(pct * 100)}%`;
    }

    function placeholderFor(file, id) {
        const name = (file.name || 'file').replace(/[[\]]/g, '');
        const isImg = (file.type || '').startsWith('image/');
        return `${isImg ? '!' : ''}[Uploading ${name}…](uploading:${id})`;
    }

    function replacePlaceholder(id, text) {
        const v = ta.value;
        const m = v.match(new RegExp(`!?\\[Uploading [^\\]]*…\\]\\(uploading:${id}\\)`));
        if (!m) return false;
        const s = m.index, e = s + m[0].length;
        const selS = ta.selectionStart, selE = ta.selectionEnd;
        ta.setRangeText(text, s, e, 'preserve');
        // keep the caret where the writer left it
        const shift = text.length - m[0].length;
        if (selS > e) ta.setSelectionRange(selS + shift, selE + shift);
        ta.dispatchEvent(new Event('input', { bubbles: true }));
        return true;
    }

    function handleFiles(fileList) {
        const files = [...fileList].filter(Boolean);
        if (!files.length) return;
        const snippets = files.map(file => {
            const id = ++uploadId;
            queue.push({ file, id });
            return placeholderFor(file, id);
        });
        insertBlock(snippets.join('\n\n'));
        pump();
    }

    function pump() {
        while (running < 3 && queue.length) {
            const { file, id } = queue.shift();
            running++;
            active.set(id, 0);
            updateUploadStatus();
            upload(file, p => { active.set(id, p); updateUploadStatus(); })
                .then(({ media, created }) => {
                    replacePlaceholder(id, media.markdown);
                    addTileToPanel(media);
                    if (!created) toast(`${media.name} was already in your library, reused it.`, 'info', 2500);
                })
                .catch(err => {
                    replacePlaceholder(id, '');
                    toast(err.message, 'error', 6000);
                })
                .finally(() => { running--; active.delete(id); updateUploadStatus(); pump(); });
        }
    }

    $('#file-input').addEventListener('change', e => { handleFiles(e.target.files); e.target.value = ''; });

    const body = document.querySelector('.ed-body');
    const dropTarget = $('#drop-target');
    let dragDepth = 0;
    const hasFiles = e => e.dataTransfer && [...e.dataTransfer.types].includes('Files');

    body.addEventListener('dragenter', e => {
        if (!hasFiles(e)) return;
        e.preventDefault();
        dragDepth++;
        dropTarget.classList.add('dragging');
    });
    body.addEventListener('dragover', e => {
        if (!hasFiles(e)) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = 'copy';
    });
    body.addEventListener('dragleave', e => {
        if (!hasFiles(e)) return;
        if (--dragDepth <= 0) { dragDepth = 0; dropTarget.classList.remove('dragging'); }
    });
    body.addEventListener('drop', e => {
        if (!hasFiles(e)) return;    // media-tile drags fall through to the textarea's native text drop
        e.preventDefault();
        dragDepth = 0;
        dropTarget.classList.remove('dragging');
        handleFiles(e.dataTransfer.files);
    });
    // stop the browser opening files dropped just outside the editor
    window.addEventListener('dragover', e => { if (hasFiles(e)) e.preventDefault(); });
    window.addEventListener('drop', e => { if (hasFiles(e)) e.preventDefault(); });

    ta.addEventListener('paste', e => {
        const files = [...(e.clipboardData ? e.clipboardData.files : [])];
        if (!files.length) return;
        e.preventDefault();
        const stamp = new Date().toISOString().slice(0, 19).replace(/[-:T]/g, '');
        handleFiles(files.map((file, i) => {
            if (file.name && file.name !== 'image.png') return file;
            const ext = (file.type.split('/')[1] || 'png').replace('jpeg', 'jpg');
            return new File([file], `pasted-${stamp}${i ? '-' + i : ''}.${ext}`, { type: file.type });
        }));
    });

    // ================================================================ media panel
    const mediaPanel = $('#ed-media');
    const grid = $('#media-grid');
    let mediaKind = '', mediaOffset = 0, mediaLoaded = false;
    const PAGE = 40;

    function insertMedia(m) {
        if (m.kind === 'image' || m.kind === 'video' || m.kind === 'audio') insertBlock(m.markdown);
        else replaceSelection(m.markdown);
        toast(`Inserted ${m.name}`, 'ok', 1500);
    }

    async function loadMedia(reset) {
        if (reset) { mediaOffset = 0; grid.replaceChildren(); }
        try {
            const r = await api(`/write/api/media?limit=${PAGE}&offset=${mediaOffset}&kind=${mediaKind}`);
            r.media.forEach(m => grid.append(mediaTile(m, { draggable: true, onClick: insertMedia })));
            mediaOffset += r.media.length;
            $('#media-more').hidden = r.media.length < PAGE;
            if (!grid.children.length) grid.append(el('p', { class: 'hint empty-media' }, 'No files yet. Drop some on the editor.'));
            mediaLoaded = true;
        } catch (e) { toast(e.message, 'error'); }
    }

    function addTileToPanel(m) {
        if (!mediaLoaded || (mediaKind && m.kind !== mediaKind)) return;
        grid.querySelector('.empty-media')?.remove();
        if (grid.querySelector(`[data-id="${m.id}"]`)) return;
        grid.prepend(mediaTile(m, { draggable: true, onClick: insertMedia }));
    }

    function toggleMedia(open) {
        mediaPanel.hidden = !open;
        editor.classList.toggle('media-open', open);
        $('#toggle-media').setAttribute('aria-expanded', String(open));
        if (open && !mediaLoaded) loadMedia(true);
        store.set('blogmd:media-open', open ? '1' : '0');
    }
    $('#toggle-media').addEventListener('click', () => toggleMedia(mediaPanel.hidden));
    $('#close-media').addEventListener('click', () => toggleMedia(false));
    $('#media-more').addEventListener('click', () => loadMedia(false));
    mediaPanel.querySelectorAll('[data-kind]').forEach(b => b.addEventListener('click', () => {
        mediaPanel.querySelectorAll('[data-kind]').forEach(x => x.classList.toggle('is-on', x === b));
        mediaKind = b.dataset.kind;
        loadMedia(true);
    }));
    if (store.get('blogmd:media-open') === '1') toggleMedia(true);

    // ================================================================ details + layout
    $('#toggle-details').addEventListener('click', () => {
        const d = $('#ed-details');
        d.hidden = !d.hidden;
        $('#toggle-details').setAttribute('aria-expanded', String(!d.hidden));
    });

    function setView(v) {
        editor.dataset.view = v;
        document.querySelectorAll('[data-view-btn]').forEach(b => b.classList.toggle('is-on', b.dataset.viewBtn === v));
        store.set('blogmd:view', v);
        if (v !== 'write') render();
    }
    document.querySelectorAll('[data-view-btn]').forEach(b => b.addEventListener('click', () => setView(b.dataset.viewBtn)));
    setView(store.get('blogmd:view') || (window.innerWidth < 900 ? 'write' : 'split'));

    // ================================================================ keyboard
    let escPressed = false;
    ta.addEventListener('keydown', e => {
        if (e.key === 'Escape') { escPressed = true; return; }
        if (e.key === 'Tab' && !escPressed && !e.ctrlKey && !e.metaKey && !e.altKey) {
            e.preventDefault();
            if (e.shiftKey) {
                const v = ta.value, s = v.lastIndexOf('\n', ta.selectionStart - 1) + 1;
                const n = (v.slice(s).match(/^ {1,4}/) || [''])[0].length;
                if (n) { const c = ta.selectionStart; ta.setSelectionRange(s, s + n); replaceSelection(''); ta.setSelectionRange(c - n, c - n); }
            } else replaceSelection('    ');
        }
        escPressed = false;
        // continue lists on Enter
        if (e.key === 'Enter' && !e.shiftKey && !e.ctrlKey && !e.metaKey && ta.selectionStart === ta.selectionEnd) {
            const v = ta.value, s = v.lastIndexOf('\n', ta.selectionStart - 1) + 1;
            const line = v.slice(s, ta.selectionStart);
            const m = line.match(/^(\s*)([-*+] \[[ x]\] |[-*+] |(\d+)\. |> )/);
            if (m) {
                e.preventDefault();
                if (line.trim() === m[2].trim()) {          // empty item: end the list
                    ta.setSelectionRange(s, ta.selectionStart); replaceSelection('');
                } else {
                    const next = m[3] ? `${+m[3] + 1}. ` : m[2].replace('[x]', '[ ]');
                    replaceSelection('\n' + m[1] + next);
                }
            }
        }
    });

    document.addEventListener('keydown', e => {
        const mod = e.ctrlKey || e.metaKey;
        if (!mod) return;
        const k = e.key.toLowerCase();
        if (k === 's') { e.preventDefault(); save(); }
        else if (document.activeElement === ta && k === 'b') { e.preventDefault(); actions.bold(); }
        else if (document.activeElement === ta && k === 'i') { e.preventDefault(); actions.italic(); }
        else if (document.activeElement === ta && k === 'k') { e.preventDefault(); actions.link(); }
        else if (['1', '2', '3'].includes(k)) { e.preventDefault(); setView(['write', 'split', 'preview'][+k - 1]); }
    });

    // ================================================================ boot
    ta.addEventListener('input', () => { scheduleRender(); updateStats(); changed(); });
    renderHeader();
    updateStats();
    render();
    if (!post) { checkPath(); if (!f.title.value) f.title.focus(); }
    else if (post) ta.focus();
    window.BlogMDEditor = { save, handleFiles, payload, isDirty };
})();
