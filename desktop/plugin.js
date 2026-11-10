/**
 * GIF Tool for Hermes Desktop.
 *
 * - The GIF card: Desktop draws it for the MEDIA: line of a file one of this plugin's tools made
 *   (gif_open, gif_edit, gif_create, gif_export) through the tool-media renderer seam. It plays the
 *   GIF and edits it live: captions, speed, reverse, boomerang, trim, per-frame timing, crop, and
 *   exports compressed for Discord, Telegram, X, emoji, or a size you choose.
 * - "GIF Tool: open studio" (command palette) opens a tab with every GIF in this profile, where you
 *   can drop in a file or paste a link without asking the agent.
 *
 * Every edit and export goes to this plugin's backend (dashboard/plugin_api.py) and runs on the
 * Hermes host: nothing is generated or uploaded, and the original file is never changed. Older
 * Desktops without the tool-media seam show the GIF in the built-in viewer.
 *
 * Plain ESM with jsx() calls (no build step), importing only the three allowed specifiers.
 */

import * as sdk from '@hermes/plugin-sdk'
import { useCallback, useEffect, useRef, useState } from 'react'
import { jsx, jsxs, Fragment } from 'react/jsx-runtime'

const PLUGIN_ID = 'gif-tool'
const TOOLS = ['gif_open', 'gif_edit', 'gif_create', 'gif_export']
const ID_RE = /^[0-9a-f]{12}$/
const PREVIEW_DEBOUNCE_MS = 350
const MAX_UPLOAD_BYTES = 25 * 1024 * 1024

/** createElement-style helper over the automatic JSX runtime. */
function h(type, props, ...kids) {
  const { key, ...rest } = props || {}
  const children = kids.flat().filter(k => k !== false && k !== null && k !== undefined)
  if (children.length === 1) rest.children = children[0]
  else if (children.length > 1) rest.children = children
  return (children.length > 1 ? jsxs : jsx)(type, rest, key)
}

const T = {
  text: 'var(--ui-text-primary)',
  secondary: 'var(--ui-text-secondary)',
  muted: 'var(--ui-text-tertiary)',
  border: 'var(--ui-stroke-secondary)',
  card: 'var(--ui-bg-card)',
  chip: 'var(--ui-bg-tertiary)',
  input: 'var(--ui-bg-input)',
  accent: 'var(--ui-accent)',
  danger: 'var(--ui-red)',
  ok: 'var(--ui-green, var(--ui-accent))'
}

const S = {
  card: {
    border: `1px solid ${T.border}`, borderRadius: 12, background: T.card, color: T.text,
    padding: '12px 14px', margin: '8px 0', maxWidth: 580, display: 'flex', flexDirection: 'column', gap: 10,
    fontSize: 13
  },
  row: { display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' },
  col: { display: 'flex', flexDirection: 'column', gap: 8 },
  chip: {
    borderRadius: 999, padding: '2px 9px', fontSize: 11, lineHeight: '16px', background: T.chip,
    color: T.secondary, whiteSpace: 'nowrap'
  },
  muted: { color: T.muted, fontSize: 12 },
  error: { color: T.danger, fontSize: 12 },
  label: { color: T.secondary, fontSize: 12, minWidth: 56 },
  title: { fontSize: 13.5, fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1, minWidth: 0 },
  input: {
    flex: 1, minWidth: 0, background: T.input, color: T.text, border: `1px solid ${T.border}`,
    borderRadius: 6, padding: '5px 8px', fontSize: 13, outline: 'none'
  },
  num: {
    width: 68, background: T.input, color: T.text, border: `1px solid ${T.border}`, borderRadius: 6,
    padding: '4px 6px', fontSize: 12, textAlign: 'right', outline: 'none'
  },
  stage: {
    position: 'relative', display: 'flex', justifyContent: 'center', alignItems: 'center', minHeight: 120,
    borderRadius: 8, overflow: 'hidden',
    background: 'repeating-conic-gradient(var(--ui-bg-tertiary) 0% 25%, transparent 0% 50%) 50% / 16px 16px'
  },
  img: { maxWidth: '100%', maxHeight: 340, display: 'block' },
  busy: {
    position: 'absolute', top: 6, right: 6, borderRadius: 999, padding: '2px 8px', fontSize: 11,
    background: T.card, border: `1px solid ${T.border}`, color: T.secondary
  },
  tabs: { display: 'flex', gap: 2, borderBottom: `1px solid ${T.border}` },
  tab: on => ({
    appearance: 'none', background: 'none', border: 'none', cursor: 'pointer', padding: '6px 10px', fontSize: 12.5,
    color: on ? T.text : T.muted, fontWeight: on ? 600 : 400,
    borderBottom: `2px solid ${on ? T.accent : 'transparent'}`, marginBottom: -1
  }),
  seg: on => ({
    appearance: 'none', cursor: 'pointer', padding: '4px 10px', fontSize: 12, borderRadius: 6,
    border: `1px solid ${on ? T.accent : T.border}`, background: on ? T.chip : 'transparent',
    color: on ? T.text : T.secondary, fontWeight: on ? 600 : 400
  }),
  range: { flex: 1, minWidth: 80, accentColor: T.accent },
  thumb: (on, edge) => ({
    width: 44, height: 44, objectFit: 'cover', borderRadius: 4, flex: '0 0 auto', cursor: 'pointer',
    opacity: on ? 1 : 0.3, outline: edge ? `2px solid ${T.accent}` : 'none', outlineOffset: -2
  }),
  tile: on => ({
    display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 4, padding: 4, borderRadius: 8,
    border: `1px solid ${on ? T.accent : T.border}`, opacity: on ? 1 : 0.45, width: 78
  }),
  preset: {
    appearance: 'none', cursor: 'pointer', textAlign: 'left', padding: '7px 9px', borderRadius: 8,
    border: `1px solid ${T.border}`, background: 'transparent', color: T.text, display: 'flex',
    flexDirection: 'column', gap: 2, minWidth: 0
  },
  result: { display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, padding: '4px 0' }
}

function Btn(props) {
  const { children, ...rest } = props
  if (typeof sdk.Button === 'function') return h(sdk.Button, { size: 'sm', variant: 'outline', ...rest }, children)
  return h('button', { type: 'button', ...rest }, children)
}

function Icon({ name, spinning }) {
  return typeof sdk.Codicon === 'function' ? h(sdk.Codicon, { name, spinning }) : null
}

const Chip = ({ children, title, style }) => h('span', { style: { ...S.chip, ...style }, title }, children)

function errorText(err) {
  const raw = err instanceof Error ? err.message : String(err)
  const m = /^(\d{3}):\s*(.*)$/s.exec(raw)
  if (m) {
    try {
      const detail = JSON.parse(m[2]).detail
      if (typeof detail === 'string') return detail
    } catch {
      // not JSON
    }
    if (m[1] === '404') return 'The GIF Tool backend is not loaded yet. Enable the plugin, then restart Hermes once.'
  }
  return raw
}

function size(bytes) {
  if (!bytes && bytes !== 0) return '?'
  return bytes >= 1e6 ? `${(bytes / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1000))} KB`
}

const secs = ms => `${(ms / 1000).toFixed(ms < 10000 ? 1 : 0)} s`

function change(bytes, original) {
  if (!original || !bytes) return ''
  const pct = Math.round(((bytes - original) / original) * 100)
  return `${pct > 0 ? '+' : ''}${pct}%`
}

function parseResult(result) {
  let r = result
  if (typeof r === 'string') {
    try {
      r = JSON.parse(r)
    } catch {
      return null
    }
  }
  return r && typeof r === 'object' && typeof r.project_id === 'string' && ID_RE.test(r.project_id) ? r : null
}

async function saveDownload(payload) {
  const blob = await (await fetch(payload.data_url)).blob()
  const url = URL.createObjectURL(blob)
  const link = Object.assign(document.createElement('a'), { href: url, download: payload.file_name })
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 60_000)
}

function readFile(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result))
    reader.onerror = () => reject(new Error('Could not read that file.'))
    reader.readAsDataURL(file)
  })
}

const SPEEDS = [0.5, 1, 1.5, 2, 3]
const TABS = [['caption', 'Caption'], ['timing', 'Timing'], ['frames', 'Trim'], ['crop', 'Crop'], ['export', 'Export']]
const CROP_LABELS = { none: 'Original', square: 'Square', '16:9': '16:9', '9:16': '9:16', '4:3': '4:3' }

/** The editor: live preview plus tabbed controls. Used by the chat card and the studio. */
function Editor({ ctx, id, onOpenStudio }) {
  const [state, setState] = useState({ kind: 'loading' })
  const [edits, setEditsState] = useState(null)
  const editsRef = useRef(null)
  const setEdits = useCallback(next => {
    editsRef.current = next
    setEditsState(next)
  }, [])
  const [tab, setTab] = useState('caption')
  const [rendering, setRendering] = useState(false)
  const [problem, setProblem] = useState(null)
  const [exporting, setExporting] = useState(null)
  const [results, setResults] = useState([])
  const [custom, setCustom] = useState({ kb: '', px: '', format: 'gif' })
  const [reload, setReload] = useState(0)
  const seq = useRef(0)
  const timer = useRef(null)
  const live = useRef(true)

  useEffect(() => {
    live.current = true
    return () => {
      live.current = false
      clearTimeout(timer.current)
    }
  }, [])

  useEffect(() => {
    let current = true
    setState({ kind: 'loading' })
    setResults([])
    setProblem(null)
    ctx
      .rest(`project?id=${id}`, { timeoutMs: 60_000 })
      .then(data => {
        if (!current) return
        setState({ kind: 'ready', data })
        setEdits(data.project.edits)
      })
      .catch(err => current && setState({ kind: 'error', message: errorText(err) }))
    return () => {
      current = false
      clearTimeout(timer.current)
      seq.current += 1
    }
  }, [ctx, id, reload])

  const send = useCallback(
    body => {
      const mine = ++seq.current
      setRendering(true)
      ctx
        .rest('preview', { method: 'POST', timeoutMs: 60_000, body: { id, ...body } })
        .then(res => {
          if (!live.current || mine !== seq.current) return
          setState(s => (s.kind === 'ready' ? { kind: 'ready', data: { ...s.data, ...res } } : s))
          if (body.reset) setEdits(res.project.edits)
          setProblem(null)
          setRendering(false)
        })
        .catch(err => {
          if (!live.current || mine !== seq.current) return
          setProblem(errorText(err))
          setRendering(false)
        })
    },
    [ctx, id, setEdits]
  )

  // Every change updates the controls at once and re-renders the preview after a short pause.
  const update = useCallback(
    patch => {
      const next = { ...editsRef.current, ...patch }
      setEdits(next)
      setRendering(true)
      seq.current += 1 // a reply to an older request must not overwrite this change
      clearTimeout(timer.current)
      timer.current = setTimeout(() => send({ edits: next }), PREVIEW_DEBOUNCE_MS)
    },
    [send, setEdits]
  )

  if (state.kind === 'error') {
    return h('div', { style: S.col },
      h('span', { style: S.error }, state.message),
      h('div', { style: S.row }, h(Btn, { onClick: () => setReload(n => n + 1) }, 'Retry')))
  }
  if (state.kind === 'loading' || !edits) {
    return h('span', { style: S.muted }, h(Icon, { name: 'loading', spinning: true }), ' Loading GIF…')
  }

  const { data } = state
  const { project, info, limits } = data
  const src = project.source || {}
  const n = edits.delays.length
  const caption = pos => (edits.captions.find(c => c.position === pos) || {}).text || ''
  const setCaption = (pos, text) =>
    update({ captions: ['top', 'bottom'].map(p => ({ position: p, text: p === pos ? text : caption(p) })) })
  const inTrim = i => i >= edits.trim[0] && i <= edits.trim[1]
  const perFrame = n <= 12

  const runExport = async body => {
    const key = body.preset || 'custom'
    if (rendering) return
    setExporting(key)
    setProblem(null)
    try {
      const payload = await ctx.rest('export', { method: 'POST', timeoutMs: 120_000, body: { id, ...body } })
      if (!live.current) return
      await saveDownload(payload)
      setResults(list => [{ key: `${key}-${Date.now()}`, ...payload, data_url: undefined }, ...list].slice(0, 4))
    } catch (err) {
      if (live.current) setProblem(`Export failed: ${errorText(err)}`)
    } finally {
      if (live.current) setExporting(null)
    }
  }

  const panels = {
    caption: () => h('div', { style: S.col },
      ['top', 'bottom'].map(pos =>
        h('label', { key: pos, style: S.row },
          h('span', { style: S.label }, pos === 'top' ? 'Top' : 'Bottom'),
          h('input', {
            style: S.input, value: caption(pos), maxLength: limits.max_caption,
            placeholder: pos === 'top' ? 'Top text' : 'Bottom text',
            onChange: e => setCaption(pos, e.target.value)
          }))),
      h('div', { style: S.row },
        h('span', { style: S.label }, 'Size'),
        data.text_sizes.map(t =>
          h('button', { key: t, type: 'button', style: S.seg(edits.text_size === t), onClick: () => update({ text_size: t }) },
            t[0].toUpperCase() + t.slice(1))),
        h('span', { style: { ...S.muted, marginLeft: 'auto' } }, `Drawn exactly as typed · ${data.font}`))),

    timing: () => h('div', { style: S.col },
      h('div', { style: S.row },
        h('span', { style: S.label }, 'Speed'),
        SPEEDS.map(v =>
          h('button', { key: v, type: 'button', style: S.seg(Math.abs(edits.speed - v) < 0.01), onClick: () => update({ speed: v }) }, `${v}×`)),
        h('input', {
          type: 'range', style: S.range, min: limits.min_speed, max: limits.max_speed, step: 0.05, value: edits.speed,
          'aria-label': 'Speed', onChange: e => update({ speed: Number(e.target.value) })
        }),
        h('span', { style: { ...S.muted, width: 40, textAlign: 'right' } }, `${edits.speed.toFixed(2)}×`)),
      h('div', { style: S.row },
        h('span', { style: S.label }, 'Play'),
        h('button', { type: 'button', style: S.seg(edits.reverse), onClick: () => update({ reverse: !edits.reverse }) }, 'Reverse'),
        h('button', { type: 'button', style: S.seg(edits.boomerang), title: 'Forwards, then backwards', onClick: () => update({ boomerang: !edits.boomerang }) }, 'Boomerang')),
      n > 4 && h('div', { style: S.row },
        h('span', { style: S.label }, 'Frames'),
        [[1, 'All'], [2, 'Every 2nd'], [3, 'Every 3rd'], [4, 'Every 4th']].map(([v, label]) =>
          h('button', { key: v, type: 'button', style: S.seg(edits.step === v), title: 'Fewer frames make a smaller file; timing is kept', onClick: () => update({ step: v }) }, label)))),

    frames: () => perFrame
      ? h('div', { style: S.col },
          h('div', { style: { ...S.row, gap: 8 } },
            data.thumbs.map(t => {
              const i = t.index
              const ok = /^\d+$/.test(String(edits.delays[i])) && edits.delays[i] >= limits.min_delay && edits.delays[i] <= limits.max_delay
              return h('div', { key: i, style: S.tile(edits.enabled[i]) },
                h('img', {
                  src: t.src, alt: `Frame ${i + 1}`, style: { width: 68, height: 68, objectFit: 'cover', borderRadius: 4, cursor: 'pointer' },
                  title: edits.enabled[i] ? 'Click to drop this frame' : 'Click to include this frame',
                  onClick: () => {
                    const next = edits.enabled.map((v, j) => (j === i ? !v : v))
                    if (next.some(Boolean)) update({ enabled: next })
                  }
                }),
                h('label', { style: { ...S.row, gap: 3, ...S.muted }, title: `Frame ${i + 1} delay (${limits.min_delay}–${limits.max_delay} ms)` },
                  h('input', {
                    style: { ...S.num, width: 48, borderColor: ok ? undefined : T.danger }, inputMode: 'numeric', value: edits.delays[i],
                    onChange: e => {
                      const v = Number(e.target.value.replace(/[^0-9]/g, '') || 0)
                      const next = edits.delays.map((d, j) => (j === i ? v : d))
                      if (v >= limits.min_delay && v <= limits.max_delay) update({ delays: next })
                      else setEdits({ ...edits, delays: next })
                    }
                  }), 'ms'))
            })),
          h('span', { style: S.muted }, 'Click a frame to drop it. Each frame has its own delay.'))
      : h('div', { style: S.col },
          h('div', { style: { display: 'flex', gap: 3, overflowX: 'auto', paddingBottom: 2 } },
            data.thumbs.map(t =>
              h('img', {
                key: t.index, src: t.src, alt: `Frame ${t.index + 1}`, style: S.thumb(inTrim(t.index), false),
                title: `Frame ${t.index + 1}: click to start here, shift-click to end here`,
                onClick: e => {
                  if (e.shiftKey) update({ trim: [Math.min(edits.trim[0], t.index), t.index] })
                  else update({ trim: [t.index, Math.max(edits.trim[1], t.index)] })
                }
              }))),
          [['Start', 0], ['End', 1]].map(([label, k]) =>
            h('label', { key: label, style: S.row },
              h('span', { style: S.label }, label),
              h('input', {
                type: 'range', style: S.range, min: 0, max: n - 1, step: 1, value: edits.trim[k],
                onChange: e => {
                  const v = Number(e.target.value)
                  update({ trim: k === 0 ? [Math.min(v, edits.trim[1]), edits.trim[1]] : [edits.trim[0], Math.max(v, edits.trim[0])] })
                }
              }),
              h('span', { style: { ...S.muted, width: 70, textAlign: 'right' } }, `frame ${edits.trim[k] + 1}`))),
          h('span', { style: S.muted }, `Keeping frames ${edits.trim[0] + 1}–${edits.trim[1] + 1} of ${n}.`)),

    crop: () => h('div', { style: S.col },
      h('div', { style: S.row },
        data.crops.map(c =>
          h('button', { key: c, type: 'button', style: S.seg(edits.crop === c), onClick: () => update({ crop: c }) }, CROP_LABELS[c] || c))),
      h('span', { style: S.muted }, 'Crops from the centre. The Discord emoji export is always square.')),

    export: () => h('div', { style: S.col },
      h('div', { style: { display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))', gap: 6 } },
        data.presets.map(p =>
          h('button', {
            key: p.id, type: 'button', style: { ...S.preset, opacity: exporting && exporting !== p.id ? 0.5 : 1 },
            disabled: exporting !== null || rendering, title: `Compress and download for ${p.label}`,
            onClick: () => runExport({ preset: p.id })
          },
            h('span', { style: { fontWeight: 600, fontSize: 12.5 } },
              h(Icon, { name: exporting === p.id ? 'loading' : 'cloud-download', spinning: exporting === p.id }), ` ${p.label}`),
            h('span', { style: { ...S.muted, fontSize: 11 } }, p.note)))),
      h('div', { style: S.row },
        h('span', { style: S.label }, 'Custom'),
        h('input', { style: S.num, inputMode: 'numeric', placeholder: 'max KB', 'aria-label': 'Maximum size in KB', value: custom.kb, onChange: e => setCustom(c => ({ ...c, kb: e.target.value.replace(/[^0-9]/g, '') })) }),
        h('input', { style: S.num, inputMode: 'numeric', placeholder: 'max px', 'aria-label': 'Longest side in pixels', value: custom.px, onChange: e => setCustom(c => ({ ...c, px: e.target.value.replace(/[^0-9]/g, '') })) }),
        ['gif', 'webp'].map(f =>
          h('button', { key: f, type: 'button', style: S.seg(custom.format === f), onClick: () => setCustom(c => ({ ...c, format: f })) }, f.toUpperCase())),
        h(Btn, {
          disabled: exporting !== null || rendering,
          onClick: () => runExport({ max_kb: custom.kb ? Number(custom.kb) : undefined, max_px: custom.px ? Number(custom.px) : undefined, format: custom.format })
        }, h(Icon, { name: exporting === 'custom' ? 'loading' : 'fold-down', spinning: exporting === 'custom' }), ' Compress')),
      results.map(r =>
        h('div', { key: r.key, style: S.result },
          h('span', { style: { color: r.fits ? T.ok : T.danger } }, h(Icon, { name: r.fits ? 'check' : 'warning' })),
          h('span', { style: { fontWeight: 600 } }, r.label),
          h('span', { style: S.muted }, `${r.width}×${r.height} · ${r.frames} frames`),
          h('span', null, size(r.bytes)),
          src.bytes ? h('span', { style: { color: r.bytes < src.bytes ? T.ok : T.muted } }, `${change(r.bytes, src.bytes)} vs original`) : null,
          !r.fits && h('span', { style: S.error }, `over the ${size(r.max_bytes)} limit: trim or drop frames`))),
      !results.length && h('span', { style: S.muted }, 'Exports are compressed on the Hermes host until they fit, then saved to your Downloads.'))
  }

  return h(Fragment, null,
    h('div', { style: { ...S.row, flexWrap: 'nowrap' } },
      h('span', { style: S.title, title: project.title }, h(Icon, { name: 'file-media' }), ' ', project.title),
      onOpenStudio && h(Btn, { variant: 'ghost', title: 'Every GIF in this profile', onClick: onOpenStudio }, h(Icon, { name: 'library' }), ' Studio'),
      h(Btn, { variant: 'ghost', title: 'Undo every edit', disabled: rendering, onClick: () => { clearTimeout(timer.current); send({ reset: true }) } }, h(Icon, { name: 'discard' }), ' Reset')),
    h('div', { style: S.stage },
      h('img', { src: data.gif, alt: project.title, style: S.img }),
      rendering && h('span', { style: S.busy }, h(Icon, { name: 'loading', spinning: true }), ' Updating…')),
    h('div', { style: S.row },
      h(Chip, { title: 'Frames playing and loop length after your edits' }, `${data.timeline} frames · ${secs(info.duration_ms)} loop`),
      h(Chip, { title: 'Source file' }, `Original ${src.width}×${src.height} · ${size(src.bytes)}`),
      h(Chip, { title: 'This preview is a small GIF; exports are rendered separately at full quality' }, `Preview ${size(info.bytes)}`),
      project.kind === 'sheet' && h(Chip, { title: 'gif_create used one image from your ChatGPT plan through your Codex sign-in. Edits and exports are local.' }, '1 plan image')),
    h('div', { style: S.tabs, role: 'tablist' },
      TABS.map(([key, label]) =>
        h('button', { key, type: 'button', role: 'tab', 'aria-selected': tab === key, style: S.tab(tab === key), onClick: () => setTab(key) },
          key === 'frames' && perFrame ? 'Frames' : label))),
    panels[tab](),
    problem && h('span', { style: S.error }, problem))
}

/** Card for a MEDIA: file one of this plugin's tools made. Anything it can't draw keeps the built-in viewer. */
function GifCard({ ctx, media, openStudio }) {
  const result = parseResult(media.result)
  const isExport = media.toolName === 'gif_export'
  const [editing, setEditing] = useState(!isExport)
  const [url, setUrl] = useState(null)

  useEffect(() => {
    if (!isExport || !result || !media.file || typeof media.file.resolveUrl !== 'function') return
    let current = true
    media.file.resolveUrl().then(u => current && setUrl(u)).catch(() => {})
    return () => {
      current = false
    }
  }, [isExport, media.file, result && result.project_id])

  if (!result) return h(Fragment, null, media.fallback)
  if (isExport && !editing) {
    return h('div', { style: S.card, 'data-gif-tool-card': 'export' },
      url ? h('div', { style: S.stage }, h('img', { src: url, alt: 'Exported GIF', style: S.img })) : media.fallback,
      h('div', { style: S.row },
        h(Chip, null, `${String(result.preset || 'export').replace('_', ' ')} · ${String(result.format || '').toUpperCase()}`),
        h(Chip, null, `${result.width}×${result.height} · ${result.frames} frames`),
        h(Chip, { style: { color: T.text, fontWeight: 600 } }, result.size),
        result.change && h(Chip, { title: `Original: ${result.original_size}` }, result.change),
        result.fits === false && h(Chip, { style: { color: T.danger } }, `over ${result.limit}`),
        h('span', { style: { flex: 1 } }),
        url && h(Btn, { onClick: () => saveDownload({ data_url: url, file_name: (media.file && media.file.name) || 'export.gif' }) }, h(Icon, { name: 'cloud-download' }), ' Download'),
        h(Btn, { onClick: () => setEditing(true) }, h(Icon, { name: 'edit' }), ' Edit')))
  }
  return h('div', { style: S.card, 'data-gif-tool-card': result.project_id },
    h(Editor, { ctx, id: result.project_id, onOpenStudio: openStudio }))
}

/** Studio tab: the profile's GIF library, import by file / drop / link, and the editor. */
function Studio({ ctx }) {
  const [list, setList] = useState(null)
  const [selected, setSelected] = useState(null)
  const [busy, setBusy] = useState(null)
  const [message, setMessage] = useState(null)
  const [link, setLink] = useState('')
  const [confirm, setConfirm] = useState(null)
  const [over, setOver] = useState(false)
  const fileInput = useRef(null)

  const refresh = useCallback(
    pick =>
      ctx
        .rest('projects', { timeoutMs: 60_000 })
        .then(res => {
          setList(res.projects)
          setSelected(cur => pick || (cur && res.projects.some(p => p.id === cur) ? cur : (res.projects[0] || {}).id || null))
        })
        .catch(err => setMessage(errorText(err))),
    [ctx]
  )

  useEffect(() => {
    void refresh()
  }, [refresh])

  const importBody = async (body, what) => {
    setBusy(what)
    setMessage(null)
    try {
      const res = await ctx.rest('import', { method: 'POST', timeoutMs: 120_000, body })
      await refresh(res.id)
    } catch (err) {
      setMessage(errorText(err))
    } finally {
      setBusy(null)
    }
  }

  const importFile = async file => {
    if (!file) return
    if (file.size > MAX_UPLOAD_BYTES) {
      setMessage(`That file is ${size(file.size)}. Files over ${size(MAX_UPLOAD_BYTES)} open faster through the agent: ask it to "open <path> with GIF Tool".`)
      return
    }
    try {
      await importBody({ name: file.name, data: await readFile(file) }, 'file')
    } catch (err) {
      setMessage(errorText(err))
    }
  }

  const remove = async id => {
    setConfirm(null)
    try {
      await ctx.rest('delete', { method: 'POST', body: { id } })
      if (selected === id) setSelected(null)
      await refresh()
    } catch (err) {
      setMessage(errorText(err))
    }
  }

  return h('div', {
    style: { display: 'flex', flexDirection: 'column', gap: 10, padding: 14, height: '100%', boxSizing: 'border-box', color: T.text, fontSize: 13, outline: over ? `2px dashed ${T.accent}` : 'none', outlineOffset: -6 },
    onDragOver: e => { e.preventDefault(); setOver(true) },
    onDragLeave: () => setOver(false),
    onDrop: e => { e.preventDefault(); setOver(false); void importFile(e.dataTransfer.files && e.dataTransfer.files[0]) }
  },
    h('div', { style: S.row },
      h('input', { ref: fileInput, type: 'file', accept: 'image/gif,image/webp,image/png,image/apng,image/jpeg', style: { display: 'none' }, onChange: e => { void importFile(e.target.files && e.target.files[0]); e.target.value = '' } }),
      h(Btn, { variant: 'default', disabled: busy !== null, onClick: () => fileInput.current && fileInput.current.click() },
        h(Icon, { name: busy === 'file' ? 'loading' : 'add', spinning: busy === 'file' }), ' Add a GIF'),
      h('input', {
        style: { ...S.input, minWidth: 160 }, placeholder: 'or paste an https:// link to a GIF', value: link,
        onChange: e => setLink(e.target.value),
        onKeyDown: e => { if (e.key === 'Enter' && link.trim() && !busy) { void importBody({ url: link.trim() }, 'link').then(() => setLink('')) } }
      }),
      h(Btn, { disabled: busy !== null || !link.trim(), onClick: () => void importBody({ url: link.trim() }, 'link').then(() => setLink('')) },
        h(Icon, { name: busy === 'link' ? 'loading' : 'link', spinning: busy === 'link' }), ' Open link')),
    message && h('span', { style: S.error }, message),
    h('div', { style: { display: 'flex', gap: 14, flex: 1, minHeight: 0, flexWrap: 'wrap' } },
      h('div', { style: { flex: '0 0 190px', display: 'flex', flexDirection: 'column', gap: 6, overflowY: 'auto', maxHeight: '100%' } },
        list === null && h('span', { style: S.muted }, h(Icon, { name: 'loading', spinning: true }), ' Loading…'),
        list && !list.length && h('span', { style: S.muted }, 'No GIFs yet. Add a file, drop one here, paste a link, or ask the agent to open or create one.'),
        (list || []).map(p =>
          h('div', {
            key: p.id, role: 'button', tabIndex: 0,
            style: { display: 'flex', gap: 8, alignItems: 'center', padding: 5, borderRadius: 8, cursor: 'pointer', border: `1px solid ${selected === p.id ? T.accent : T.border}`, background: selected === p.id ? T.chip : 'transparent' },
            onClick: () => setSelected(p.id),
            onKeyDown: e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setSelected(p.id) } }
          },
            p.thumb ? h('img', { src: p.thumb, alt: '', style: { width: 40, height: 40, objectFit: 'cover', borderRadius: 4, flex: '0 0 auto' } }) : h('span', { style: { width: 40 } }),
            h('div', { style: { minWidth: 0, flex: 1 } },
              h('div', { style: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontSize: 12.5 } }, p.title || 'GIF'),
              h('div', { style: S.muted }, `${p.frames} frames · ${size(p.bytes)}`)),
            confirm === p.id
              ? h(Btn, { variant: 'ghost', title: 'Delete this project and its source from plugin storage', onClick: e => { e.stopPropagation(); void remove(p.id) } }, 'Delete?')
              : h(Btn, { variant: 'ghost', 'aria-label': 'Delete', title: 'Delete', onClick: e => { e.stopPropagation(); setConfirm(p.id) } }, h(Icon, { name: 'trash' }))))),
      h('div', { style: { flex: '1 1 320px', minWidth: 0, display: 'flex', flexDirection: 'column', gap: 10, overflowY: 'auto', maxHeight: '100%' } },
        selected ? h(Editor, { key: selected, ctx, id: selected }) : list && list.length ? null : h('span', { style: S.muted }, 'Your GIF will appear here.'))))
}

export default {
  id: PLUGIN_ID,
  name: 'GIF Tool',
  description: 'Play, caption, trim, speed up, crop and compress GIFs in chat, with exports sized for Discord, Telegram and X.',
  register(ctx) {
    let closeStudio = null
    const canStudio = sdk.host && typeof sdk.host.openWorkspace === 'function'
    const openStudio = () => {
      if (!canStudio) return
      closeStudio = sdk.host.openWorkspace('gif-tool-studio', {
        title: 'GIF Studio',
        minWidth: '34rem',
        onClose: () => {
          closeStudio = null
        },
        render: () => h(Studio, { ctx })
      })
    }
    ctx.onDispose(() => {
      if (closeStudio) closeStudio()
    })

    const contributions = []
    // Desktops without the tool-media seam keep the built-in image viewer for the MEDIA: line.
    if (typeof sdk.TOOL_MEDIA_RENDERER_AREA === 'string') {
      contributions.push({
        id: 'gif-card',
        area: sdk.TOOL_MEDIA_RENDERER_AREA,
        data: { tools: TOOLS, render: props => h(GifCard, { ctx, media: props, openStudio: canStudio ? openStudio : null }) }
      })
    }
    if (canStudio && typeof sdk.PALETTE_AREA === 'string') {
      contributions.push({
        id: 'open-studio',
        area: sdk.PALETTE_AREA,
        data: { id: 'gif-tool.studio', label: 'GIF Tool: open studio', keywords: ['gif', 'meme', 'compress', 'caption', 'studio', 'library'], run: openStudio }
      })
    }
    if (contributions.length) ctx.registerMany(contributions)
  }
}
