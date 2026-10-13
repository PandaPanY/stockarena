/**
 * 使用手册（docs/USER_GUIDE.md）的渲染与关键字搜索。
 *
 * 手册只用到 Markdown 的常见语法（标题、段落、列表、引用、表格、代码块、加粗、行内代码、链接），
 * 这里用一个小解析器渲染，避免引入依赖；同时把全文按标题切成小节，供搜索定位。
 * 手册是仓库里的文件，不是用户输入，但仍然先转义再拼 HTML。
 */
import { escapeHtml } from './html'

export interface GuideHeading {
  level: number
  text: string
  id: string
}

export interface GuideSection {
  id: string
  level: number
  title: string
  /** 从章到本节的标题链，含本节 */
  path: string[]
  /** 本节正文的纯文本（不含子节） */
  text: string
}

export interface GuideDoc {
  html: string
  headings: GuideHeading[]
  sections: GuideSection[]
}

export interface SnippetPart {
  t: string
  hit: boolean
}

export interface GuideHit {
  section: GuideSection
  /** 命中次数（标题 + 正文） */
  count: number
  titleHit: boolean
  snippet: SnippetPart[]
}

/** 不参与搜索的小节（手册自带的目录，页面左侧已有目录） */
const SKIP_SECTIONS = new Set(['目录'])

/** 与 GitHub 相同的标题锚点规则：小写、去掉标点、空格变连字符；重名加 -1、-2 */
export function slugify(text: string, used: Map<string, number>): string {
  const base = text
    .trim()
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\s_-]/gu, '')
    .replace(/\s/g, '-')
  const n = used.get(base) ?? 0
  used.set(base, n + 1)
  return n ? `${base}-${n}` : base
}

/** 行内语法：`代码`、**加粗**、[文字](链接) */
export function renderInline(text: string): string {
  return text
    .split(/(`[^`]+`)/)
    .map((part) => {
      if (part.length > 2 && part.startsWith('`') && part.endsWith('`'))
        return `<code>${escapeHtml(part.slice(1, -1))}</code>`
      return escapeHtml(part)
        .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_m, label: string, href: string) => {
          if (href.startsWith('#')) return `<a href="${href}" data-anchor="${href.slice(1)}">${label}</a>`
          if (/^https?:\/\//.test(href))
            return `<a href="${href}" target="_blank" rel="noopener noreferrer">${label}</a>`
          // 指向仓库里其他文档的相对链接：页面里打不开，只显示文字
          return `<span class="doc-ref" title="见代码仓库 docs/${href}">${label}</span>`
        })
        .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    })
    .join('')
}

/** 去掉 Markdown 标记，得到用于搜索的纯文本 */
export function plainInline(text: string): string {
  return text
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/\*\*/g, '')
    .replace(/`/g, '')
}

const RE_HEADING = /^(#{1,6})\s+(.*)$/
const RE_FENCE = /^\s*```/
const RE_HR = /^\s*(-{3,}|\*{3,})\s*$/
const RE_ITEM = /^(\s*)([-*]|\d+\.)\s+(.*)$/
const RE_TABLE_SEP = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/

function indentOf(line: string): number {
  return line.length - line.trimStart().length
}

function dedent(lines: string[]): string[] {
  const widths = lines.filter((l) => l.trim()).map(indentOf)
  const n = widths.length ? Math.min(...widths) : 0
  return lines.map((l) => l.slice(Math.min(n, indentOf(l))))
}

function splitRow(line: string): string[] {
  let s = line.trim()
  if (s.startsWith('|')) s = s.slice(1)
  if (s.endsWith('|')) s = s.slice(0, -1)
  return s.split('|').map((c) => c.trim())
}

function isBlockStart(line: string, next: string | undefined): boolean {
  return (
    RE_HEADING.test(line) ||
    RE_FENCE.test(line) ||
    RE_HR.test(line) ||
    RE_ITEM.test(line) ||
    line.trimStart().startsWith('>') ||
    (line.trimStart().startsWith('|') && next !== undefined && RE_TABLE_SEP.test(next))
  )
}

interface Collector {
  used: Map<string, number>
  headings: GuideHeading[]
  sections: GuideSection[]
  stack: GuideHeading[]
  current: { section: GuideSection; text: string[] } | null
}

/** 解析一组（已去掉公共缩进的）行；collector 只在最外层传入，用于记录标题和小节 */
function parseBlocks(lines: string[], col: Collector | null): string {
  const out: string[] = []
  const addText = (s: string) => {
    if (col?.current && s.trim()) col.current.text.push(s.trim())
  }
  let i = 0
  while (i < lines.length) {
    const line = lines[i]!
    if (!line.trim()) {
      i++
      continue
    }

    // 代码块
    if (RE_FENCE.test(line)) {
      const body: string[] = []
      i++
      while (i < lines.length && !RE_FENCE.test(lines[i]!)) body.push(lines[i++]!)
      i++ // 结束的 ```
      const code = dedent(body).join('\n')
      out.push(`<pre><code>${escapeHtml(code)}</code></pre>`)
      addText(code)
      continue
    }

    // 标题
    const h = RE_HEADING.exec(line)
    if (h && indentOf(line) === 0) {
      const level = h[1]!.length
      const raw = h[2]!.trim()
      const text = plainInline(raw)
      if (col) {
        const id = slugify(text, col.used)
        const heading = { level, text, id }
        col.headings.push(heading)
        while (col.stack.length && col.stack[col.stack.length - 1]!.level >= level) col.stack.pop()
        col.stack.push(heading)
        const path = col.stack.filter((x) => x.level > 1 || col.stack.length === 1).map((x) => x.text)
        const section = { id, level, title: text, path, text: '' }
        col.sections.push(section)
        flushSection(col)
        col.current = { section, text: [] }
        out.push(`<h${level} id="${escapeHtml(id)}">${renderInline(raw)}</h${level}>`)
      } else {
        out.push(`<h${level}>${renderInline(raw)}</h${level}>`)
      }
      i++
      continue
    }

    if (RE_HR.test(line)) {
      out.push('<hr>')
      i++
      continue
    }

    // 引用：去掉 > 后递归解析
    if (line.trimStart().startsWith('>')) {
      const body: string[] = []
      while (i < lines.length && lines[i]!.trimStart().startsWith('>')) {
        body.push(lines[i]!.trimStart().replace(/^>\s?/, ''))
        i++
      }
      out.push(`<blockquote>${parseBlocks(body, null)}</blockquote>`)
      addText(body.map(plainInline).join(' '))
      continue
    }

    // 表格
    if (line.trimStart().startsWith('|') && i + 1 < lines.length && RE_TABLE_SEP.test(lines[i + 1]!)) {
      const head = splitRow(line)
      i += 2
      const rows: string[][] = []
      while (i < lines.length && lines[i]!.trimStart().startsWith('|')) rows.push(splitRow(lines[i++]!))
      const th = head.map((c) => `<th>${renderInline(c)}</th>`).join('')
      const trs = rows.map((r) => `<tr>${r.map((c) => `<td>${renderInline(c)}</td>`).join('')}</tr>`).join('')
      out.push(`<div class="table-wrap"><table><thead><tr>${th}</tr></thead><tbody>${trs}</tbody></table></div>`)
      for (const r of [head, ...rows]) addText(r.map(plainInline).join(' '))
      continue
    }

    // 列表：同一缩进、同一类型的连续条目；条目下缩进的行（含空行后的缩进行）属于该条目
    const item = RE_ITEM.exec(line)
    if (item) {
      const ordered = /\d/.test(item[2]!)
      const base = indentOf(line)
      const start = ordered ? parseInt(item[2]!, 10) : 1
      const items: string[] = []
      while (i < lines.length) {
        const m = RE_ITEM.exec(lines[i]!)
        if (!m || indentOf(lines[i]!) !== base || /\d/.test(m[2]!) !== ordered) break
        const body = [m[3]!]
        i++
        while (i < lines.length) {
          const l = lines[i]!
          if (l.trim() && indentOf(l) > base) {
            body.push(l)
            i++
            continue
          }
          if (!l.trim()) {
            // 空行之后还有属于本条目的缩进行，才继续
            let j = i
            while (j < lines.length && !lines[j]!.trim()) j++
            if (j < lines.length && indentOf(lines[j]!) > base) {
              while (i < j) body.push(lines[i++]!)
              continue
            }
          }
          break
        }
        const inner = [body[0]!, ...dedent(body.slice(1))]
        items.push(`<li>${parseBlocks(inner, null)}</li>`)
        addText(inner.map((l) => plainInline(l.replace(RE_ITEM, '$3'))).join(' '))
        // 条目之间的空行
        let j = i
        while (j < lines.length && !lines[j]!.trim()) j++
        const nx = j < lines.length ? RE_ITEM.exec(lines[j]!) : null
        if (nx && indentOf(lines[j]!) === base && /\d/.test(nx[2]!) === ordered) i = j
        else break
      }
      const tag = ordered ? 'ol' : 'ul'
      const attr = ordered && start !== 1 ? ` start="${start}"` : ''
      out.push(`<${tag}${attr}>${items.join('')}</${tag}>`)
      continue
    }

    // 段落
    const para: string[] = [line.trim()]
    i++
    while (i < lines.length && lines[i]!.trim() && !isBlockStart(lines[i]!, lines[i + 1])) para.push(lines[i++]!.trim())
    out.push(`<p>${renderInline(para.join('\n'))}</p>`)
    addText(plainInline(para.join(' ')))
  }
  return out.join('\n')
}

export function parseGuide(md: string): GuideDoc {
  const col: Collector = { used: new Map(), headings: [], sections: [], stack: [], current: null }
  const html = parseBlocks(md.split(/\r?\n/), col)
  flushSection(col)
  return { html, headings: col.headings, sections: col.sections }
}

function flushSection(col: Collector) {
  if (col.current) col.current.section.text = col.current.text.join('\n')
}

/** 关键字：按空白切分，忽略大小写，去重 */
export function splitTerms(query: string): string[] {
  const seen = new Set<string>()
  for (const t of query.trim().toLowerCase().split(/\s+/)) if (t) seen.add(t)
  return [...seen]
}

function escapeRe(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

export function termRegex(terms: string[]): RegExp | null {
  if (!terms.length) return null
  const sorted = [...terms].sort((a, b) => b.length - a.length)
  return new RegExp(sorted.map(escapeRe).join('|'), 'gi')
}

function countOf(hay: string, term: string): number {
  let n = 0
  let at = hay.indexOf(term)
  while (at >= 0) {
    n++
    at = hay.indexOf(term, at + term.length)
  }
  return n
}

/** 把文本切成「命中 / 未命中」片段，供模板高亮（不用 v-html） */
export function highlightParts(text: string, terms: string[]): SnippetPart[] {
  const re = termRegex(terms)
  if (!re) return [{ t: text, hit: false }]
  const parts: SnippetPart[] = []
  let last = 0
  for (const m of text.matchAll(re)) {
    if (m.index! > last) parts.push({ t: text.slice(last, m.index), hit: false })
    parts.push({ t: m[0], hit: true })
    last = m.index! + m[0].length
  }
  if (last < text.length) parts.push({ t: text.slice(last), hit: false })
  return parts
}

function makeSnippet(text: string, terms: string[], before = 24, after = 56): SnippetPart[] {
  const flat = text.replace(/\s+/g, ' ')
  const lower = flat.toLowerCase()
  const at = Math.min(...terms.map((t) => lower.indexOf(t)).filter((x) => x >= 0))
  if (!Number.isFinite(at)) return highlightParts(flat.slice(0, before + after), terms)
  const from = Math.max(0, at - before)
  const to = Math.min(flat.length, at + after)
  const s = (from > 0 ? '…' : '') + flat.slice(from, to) + (to < flat.length ? '…' : '')
  return highlightParts(s, terms)
}

/** 搜索小节：所有关键字都要出现（标题或正文）；标题命中的排前面，其余按命中次数 */
export function searchGuide(sections: GuideSection[], query: string): GuideHit[] {
  const terms = splitTerms(query)
  if (!terms.length) return []
  const hits: GuideHit[] = []
  for (const s of sections) {
    if (SKIP_SECTIONS.has(s.title)) continue
    const title = s.title.toLowerCase()
    const body = s.text.toLowerCase()
    if (!terms.every((t) => title.includes(t) || body.includes(t))) continue
    const titleHit = terms.some((t) => title.includes(t))
    const count = terms.reduce((n, t) => n + countOf(title, t) + countOf(body, t), 0)
    hits.push({ section: s, count, titleHit, snippet: makeSnippet(s.text || s.title, terms) })
  }
  return hits.sort((a, b) => Number(b.titleHit) - Number(a.titleHit) || b.count - a.count)
}
