/* Shared helpers for the writers area: API calls, uploads, toasts, media tiles. */
(function () {
    'use strict';
    const CSRF = (window.BLOGMD || {}).csrf;

    async function api(url, { method = 'GET', json, signal } = {}) {
        const opts = { method, signal, headers: { 'X-CSRF-Token': CSRF, Accept: 'application/json' }, credentials: 'same-origin' };
        if (json !== undefined) {
            opts.headers['Content-Type'] = 'application/json';
            opts.body = JSON.stringify(json);
        }
        const res = await fetch(url, opts);
        let data = {};
        try { data = await res.json(); } catch { /* empty body */ }
        if (res.status === 401) {
            toast('Your session expired. Sign in again in another tab, then retry.', 'error', 8000);
        }
        if (!res.ok) {
            const err = new Error(data.error || `Request failed (${res.status})`);
            err.status = res.status;
            err.data = data;
            throw err;
        }
        return data;
    }

    /** Upload one file with progress. Resolves to {media, created}. */
    function upload(file, onProgress) {
        return new Promise((resolve, reject) => {
            const maxMb = (window.BLOGMD || {}).maxUploadMb || 50;
            if (file.size > maxMb * 1024 * 1024) {
                reject(new Error(`${file.name} is larger than ${maxMb} MB.`));
                return;
            }
            const xhr = new XMLHttpRequest();
            xhr.open('POST', '/write/api/media');
            xhr.setRequestHeader('X-CSRF-Token', CSRF);
            xhr.responseType = 'json';
            xhr.upload.onprogress = e => { if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total); };
            xhr.onload = () => {
                const data = xhr.response || {};
                if (xhr.status >= 200 && xhr.status < 300) resolve(data);
                else reject(new Error(data.error || `Upload failed (${xhr.status})`));
            };
            xhr.onerror = () => reject(new Error('Network error while uploading.'));
            const fd = new FormData();
            fd.append('file', file, file.name || 'pasted-image.png');
            xhr.send(fd);
        });
    }

    function toast(message, kind = 'info', ms = 4000) {
        const host = document.querySelector('.toasts');
        if (!host) return;
        const el = document.createElement('div');
        el.className = `toast toast-${kind}`;
        el.textContent = message;
        host.appendChild(el);
        setTimeout(() => { el.classList.add('out'); setTimeout(() => el.remove(), 250); }, ms);
    }

    function fmtSize(n) {
        if (n < 1024) return `${n} B`;
        if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
        return `${(n / 1024 / 1024).toFixed(1)} MB`;
    }

    function relTime(iso) {
        const s = (Date.now() - new Date(iso).getTime()) / 1000;
        if (s < 45) return 'just now';
        if (s < 3600) return `${Math.round(s / 60)} min ago`;
        if (s < 86400) return `${Math.round(s / 3600)} h ago`;
        if (s < 86400 * 30) return `${Math.round(s / 86400)} d ago`;
        return new Date(iso).toLocaleDateString();
    }

    function el(tag, attrs = {}, ...children) {
        const node = document.createElement(tag);
        for (const [k, v] of Object.entries(attrs)) {
            if (v === undefined || v === null || v === false) continue;
            if (k === 'class') node.className = v;
            else if (k === 'dataset') Object.assign(node.dataset, v);
            else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
            else node.setAttribute(k, v === true ? '' : v);
        }
        for (const c of children.flat()) if (c != null) node.append(c);
        return node;
    }

    const KIND_GLYPH = { video: '▶', audio: '♪', application: '▤', text: '≡' };

    /** A media tile: thumbnail (or glyph) + name + meta. */
    function mediaTile(m, { draggable = false, onClick } = {}) {
        let thumb;
        if (m.kind === 'image') thumb = el('img', { src: m.url, alt: m.alt_text || '', loading: 'lazy', draggable: 'false' });
        else if (m.kind === 'video') thumb = el('video', { src: m.url + '#t=0.5', preload: 'metadata', muted: true });
        else thumb = el('span', { class: 'tile-glyph' }, KIND_GLYPH[m.kind] || '▤');
        const dims = m.width ? ` · ${m.width}×${m.height}` : '';
        const tile = el('button', {
            type: 'button', class: `media-tile kind-${m.kind}`, title: `${m.name}\n${m.mime_type} · ${fmtSize(m.size_bytes)}${dims}`,
            draggable: draggable ? 'true' : null, dataset: { id: m.id },
        },
            el('span', { class: 'tile-thumb' }, thumb),
            el('span', { class: 'tile-name' }, m.name),
            el('span', { class: 'tile-meta' }, `${m.mime_type.split('/')[1]} · ${fmtSize(m.size_bytes)}`),
        );
        if (draggable) {
            tile.addEventListener('dragstart', e => {
                e.dataTransfer.setData('text/plain', m.markdown);
                e.dataTransfer.setData('application/x-blogmd-media', String(m.id));
                e.dataTransfer.effectAllowed = 'copy';
            });
        }
        if (onClick) tile.addEventListener('click', () => onClick(m, tile));
        return tile;
    }

    document.querySelectorAll('time[data-relative]').forEach(t => {
        t.title = t.textContent;
        t.textContent = relTime(t.getAttribute('datetime'));
    });

    window.Writer = { api, upload, toast, fmtSize, relTime, el, mediaTile };
})();
