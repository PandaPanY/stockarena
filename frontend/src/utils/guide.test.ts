import { describe, expect, it } from 'vitest'
import guideMd from '../../../docs/USER_GUIDE.md?raw'
import { highlightParts, parseGuide, renderInline, searchGuide, slugify, splitTerms } from './guide'

describe('guide', () => {
  it('slugify 与 GitHub 锚点规则一致', () => {
    const used = new Map<string, number>()
    expect(slugify('10. 现状与局限（请务必读）', used)).toBe('10-现状与局限请务必读')
    expect(slugify('2.4 三段时间：开发期、留出期、实时跟踪', used)).toBe('24-三段时间开发期留出期实时跟踪')
    expect(slugify('Q&A', used)).toBe('qa')
    expect(slugify('Q&A', used)).toBe('qa-1')
  })

  it('行内语法先转义再渲染', () => {
    expect(renderInline('**粗体** 和 `<b>` [锚点](#a-b)')).toBe(
      '<strong>粗体</strong> 和 <code>&lt;b&gt;</code> <a href="#a-b" data-anchor="a-b">锚点</a>',
    )
    expect(renderInline('<script>')).toBe('&lt;script&gt;')
    expect(renderInline('[说明](FEATURES.md)')).toContain('class="doc-ref"')
  })

  it('解析列表、嵌套代码块、表格、引用', () => {
    const md = [
      '## 标题',
      '',
      '- 第一项',
      '- **第二项**：说明',
      '',
      '  ```bash',
      '  echo hi',
      '  ```',
      '',
      '1. 甲',
      '2. 乙',
      '',
      '| 列 | 值 |',
      '|---|---|',
      '| a | 1 |',
      '',
      '> 引用',
      '>',
      '> 第二段',
    ].join('\n')
    const doc = parseGuide(md)
    expect(doc.html).toContain('<h2 id="标题">')
    expect(doc.html).toMatch(/<ul><li>.*第一项.*<\/li><li>.*第二项.*<pre><code>echo hi<\/code><\/pre><\/li><\/ul>/s)
    expect(doc.html).toContain('<ol><li>')
    expect(doc.html).toContain('<td>a</td>')
    expect(doc.html).toMatch(/<blockquote><p>引用<\/p>\s*<p>第二段<\/p><\/blockquote>/)
    expect(doc.sections[0]!.text).toContain('echo hi')
  })

  it('手册里的站内链接都能找到对应标题', () => {
    const doc = parseGuide(guideMd)
    const ids = new Set(doc.headings.map((h) => h.id))
    const anchors = [...doc.html.matchAll(/data-anchor="([^"]+)"/g)].map((m) => m[1]!)
    expect(anchors.length).toBeGreaterThan(5)
    expect(anchors.filter((a) => !ids.has(a))).toEqual([])
    // 每个小节都有标题链，第一章不带手册总标题
    const s = doc.sections.find((x) => x.title.startsWith('5.8.6'))!
    expect(s.path).toEqual(['5. 各页面怎么用', '5.8 配置中心', '5.8.6 技能（扩展 AI 能查询的资料；管理员）'])
  })

  it('搜索：多个关键字须同时出现，标题命中排在前面', () => {
    const doc = parseGuide(guideMd)
    const hits = searchGuide(doc.sections, '留出期')
    expect(hits.length).toBeGreaterThan(2)
    expect(hits[0]!.titleHit).toBe(true)
    expect(hits.every((h) => h.section.title !== '目录')).toBe(true)
    expect(hits[0]!.snippet.some((p) => p.hit)).toBe(true)

    const both = searchGuide(doc.sections, '佣金 最低')
    expect(both.length).toBeGreaterThan(0)
    for (const h of both) {
      const all = (h.section.title + h.section.text).toLowerCase()
      expect(all.includes('佣金') && all.includes('最低')).toBe(true)
    }
    expect(searchGuide(doc.sections, '不存在的词组xyz')).toEqual([])
    expect(searchGuide(doc.sections, '   ')).toEqual([])
  })

  it('关键字切分与高亮片段', () => {
    expect(splitTerms('  AI  ai 复核 ')).toEqual(['ai', '复核'])
    expect(highlightParts('AI 复核：ai', ['ai'])).toEqual([
      { t: 'AI', hit: true },
      { t: ' 复核：', hit: false },
      { t: 'ai', hit: true },
    ])
    expect(highlightParts('a.b', ['.'])).toEqual([
      { t: 'a', hit: false },
      { t: '.', hit: true },
      { t: 'b', hit: false },
    ])
  })
})
