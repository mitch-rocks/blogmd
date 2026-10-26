/* Media library page: browse, upload (drag & drop), inspect metadata, edit alt text, delete. */
(function () {
    'use strict';
    const { api, upload, toast, el, mediaTile, fmtSize } = window.Writer;
    const $ = s => document.querySelector(s);
    const grid = $('#lib-grid'), detail = $('#lib-detail'), drop = $('#lib-drop');
    const PAGE = 60;
    let kind = '', offset = 0, scopeAll = false, selected = null;

    async function load(reset) {
        if (reset) { offset = 0; grid.replaceChildren(); }
        try {
            const r = await api(`/write/api/media?limit=${PAGE}&offset=${offset}&kind=${kind}${scopeAll ? '&scope=all' : ''}`);
            r.media.forEach(m => grid.append(mediaTile(m, { onClick: select })));
            offset += r.media.length;
            $('#lib-more').hidden = r.media.length < PAGE;
            if (!grid.children.length) grid.append(el('p', { class: 'hint pad empty-media' }, 'Nothing here yet. Drop files above to upload.'));
        } catch (e) { toast(e.message, 'error'); }
    }

    function row(label, value) {
        return el('div', { class: 'kv' }, el('dt', {}, label), el('dd', {}, value));
    }

    async function select(m, tile) {
        grid.querySelectorAll('.media-tile.is-selected').forEach(t => t.classList.remove('is-selected'));
        if (tile) tile.classList.add('is-selected');
        selected = m;
        let usedBy = [];
        try { usedBy = (await api(`/write/api/media/${m.id}`)).used_by; } catch { /* ignore */ }

        let previewEl;
        if (m.kind === 'image') previewEl = el('img', { src: m.url, alt: m.alt_text || '' });
        else if (m.kind === 'video') previewEl = el('video', { src: m.url, controls: true, preload: 'metadata' });
        else if (m.kind === 'audio') previewEl = el('audio', { src: m.url, controls: true, preload: 'metadata' });
        else previewEl = el('span', { class: 'tile-glyph big' }, '▤');

        const alt = el('input', { class: 'input', value: m.alt_text || '', placeholder: 'Describe the image for screen readers', maxlength: '300' });
        const snippet = el('input', { class: 'input mono', value: m.markdown, readonly: true, onfocus: e => e.target.select() });

        detail.replaceChildren(
            el('div', { class: 'detail-preview' }, previewEl),
            el('div', { class: 'pad' },
                el('h2', { class: 'detail-name' }, m.name),
                el('dl', { class: 'kv-list' },
                    row('Type', m.mime_type),
                    row('Size', fmtSize(m.size_bytes)),
                    m.width ? row('Dimensions', `${m.width} × ${m.height}px`) : null,
                    row('Uploaded', new Date(m.created_at).toLocaleString()),
                    row('SHA-256', el('code', { class: 'hash', title: m.sha256 }, m.sha256.slice(0, 20) + '…')),
                    row('Used in', usedBy.length
                        ? el('span', {}, ...usedBy.map((p, i) => [i ? ', ' : '', el('a', { href: `/write/edit/${p.id}` }, p.title)]))
                        : 'Not used yet'),
                ),
                el('label', { class: 'field' }, el('span', {}, 'Alt text'), alt),
                el('label', { class: 'field' }, el('span', {}, 'Markdown'), snippet),
                el('div', { class: 'btn-row' },
                    el('button', { type: 'button', class: 'btn btn-sm', onclick: async () => {
                        try {
                            const r = await api(`/write/api/media/${m.id}`, { method: 'PATCH', json: { alt_text: alt.value } });
                            Object.assign(m, r.media); snippet.value = m.markdown; toast('Saved', 'ok', 1500);
                        } catch (e) { toast(e.message, 'error'); }
                    } }, 'Save alt text'),
                    el('button', { type: 'button', class: 'btn btn-sm btn-ghost', onclick: async () => {
                        try { await navigator.clipboard.writeText(location.origin + m.url); toast('URL copied', 'ok', 1500); }
                        catch { snippet.value = location.origin + m.url; snippet.select(); }
                    } }, 'Copy URL'),
                    el('a', { class: 'btn btn-sm btn-ghost', href: m.url, target: '_blank', rel: 'noopener' }, 'Open ↗'),
                    el('button', { type: 'button', class: 'btn btn-sm btn-danger', onclick: () => remove(m, usedBy.length) }, 'Delete'),
                ),
            ),
        );
    }

    async function remove(m, uses) {
        const msg = uses ? `"${m.name}" is used in ${uses} post(s); those embeds will break. Delete anyway?` : `Delete "${m.name}"?`;
        if (!confirm(msg)) return;
        try {
            await api(`/write/api/media/${m.id}${uses ? '?force=1' : ''}`, { method: 'DELETE' });
            grid.querySelector(`[data-id="${m.id}"]`)?.remove();
            detail.replaceChildren(el('p', { class: 'dim pad' }, 'Deleted.'));
        } catch (e) { toast(e.message, 'error'); }
    }

    async function handleFiles(files) {
        const list = [...files];
        let done = 0;
        drop.classList.add('busy');
        const status = drop.querySelector('span');
        const original = status.textContent;
        await Promise.all(list.map(async file => {
            try {
                const { media, created } = await upload(file, p => {
                    status.textContent = `Uploading ${file.name} · ${Math.round(p * 100)}%`;
                });
                grid.querySelector('.empty-media')?.remove();
                if (created) grid.prepend(mediaTile(media, { onClick: select }));
                else toast(`${media.name} already existed, reused it.`, 'info');
            } catch (e) { toast(e.message, 'error', 6000); }
            done++;
        }));
        status.textContent = original;
        drop.classList.remove('busy');
        if (done) toast(`Uploaded ${done} file${done > 1 ? 's' : ''}`, 'ok', 2000);
    }

    $('#lib-upload').addEventListener('click', () => $('#file-input').click());
    drop.addEventListener('click', () => $('#file-input').click());
    $('#file-input').addEventListener('change', e => { handleFiles(e.target.files); e.target.value = ''; });
    $('#lib-more').addEventListener('click', () => load(false));
    document.querySelectorAll('.media-filter [data-kind]').forEach(b => b.addEventListener('click', () => {
        document.querySelectorAll('.media-filter [data-kind]').forEach(x => x.classList.toggle('is-on', x === b));
        kind = b.dataset.kind; load(true);
    }));
    const scope = $('#scope-all');
    if (scope) scope.addEventListener('change', () => { scopeAll = scope.checked; load(true); });

    let depth = 0;
    const hasFiles = e => [...(e.dataTransfer?.types || [])].includes('Files');
    document.addEventListener('dragenter', e => { if (hasFiles(e)) { e.preventDefault(); depth++; drop.classList.add('dragging'); } });
    document.addEventListener('dragleave', e => { if (hasFiles(e) && --depth <= 0) { depth = 0; drop.classList.remove('dragging'); } });
    document.addEventListener('dragover', e => { if (hasFiles(e)) e.preventDefault(); });
    document.addEventListener('drop', e => {
        if (!hasFiles(e)) return;
        e.preventDefault(); depth = 0; drop.classList.remove('dragging');
        handleFiles(e.dataTransfer.files);
    });

    load(true);
})();
