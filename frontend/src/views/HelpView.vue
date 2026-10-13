<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import guideMd from '../../../docs/USER_GUIDE.md?raw'
import { parseGuide, searchGuide, splitTerms, termRegex, type GuideHit } from '@/utils/guide'

const doc = parseGuide(guideMd)
const toc = doc.headings.filter((h) => h.level >= 2 && h.level <= 3 && h.text !== '目录')

const route = useRoute()
const router = useRouter()
const body = ref<HTMLElement>()
const tocEl = ref<HTMLElement>()

const query = ref(typeof route.query.q === 'string' ? route.query.q : '')
const terms = ref<string[]>(splitTerms(query.value))
const hits = computed<GuideHit[]>(() => searchGuide(doc.sections, terms.value.join(' ')))
const activeHit = ref('')
const activeId = ref('')

// ---- 正文高亮：在渲染好的 DOM 上给命中的文字套 <mark>，并支持上一处 / 下一处 ----
const marks = ref<HTMLElement[]>([])
const markIndex = ref(-1)

function clearMarks(root: HTMLElement) {
  for (const m of Array.from(root.querySelectorAll('mark.guide-hit'))) {
    m.replaceWith(document.createTextNode(m.textContent ?? ''))
  }
  root.normalize()
}

function applyMarks() {
  const root = body.value
  if (!root) return
  clearMarks(root)
  marks.value = []
  markIndex.value = -1
  const re = termRegex(terms.value)
  if (!re) return
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  const nodes: Text[] = []
  while (walker.nextNode()) nodes.push(walker.currentNode as Text)
  for (const node of nodes) {
    const text = node.data
    const found = [...text.matchAll(re)]
    if (!found.length) continue
    const frag = document.createDocumentFragment()
    let last = 0
    for (const m of found) {
      if (m.index! > last) frag.append(text.slice(last, m.index))
      const mark = document.createElement('mark')
      mark.className = 'guide-hit'
      mark.textContent = m[0]
      frag.append(mark)
      last = m.index! + m[0].length
    }
    if (last < text.length) frag.append(text.slice(last))
    node.replaceWith(frag)
  }
  marks.value = Array.from(root.querySelectorAll<HTMLElement>('mark.guide-hit'))
}

function setCurrent(index: number) {
  const n = marks.value.length
  markIndex.value = ((index % n) + n) % n
  marks.value.forEach((m, i) => m.classList.toggle('current', i === markIndex.value))
}

function focusMark(index: number) {
  if (!marks.value.length) return
  setCurrent(index)
  marks.value[markIndex.value]!.scrollIntoView({ block: 'center' })
}

function nextMark(step: number) {
  focusMark(markIndex.value < 0 && step < 0 ? -1 : markIndex.value + step)
}

function urlQuery() {
  const q = query.value.trim()
  return q ? { q } : {}
}

// 输入停顿后再搜索，并把关键字写进地址栏，方便分享 / 刷新后保留
let timer: ReturnType<typeof setTimeout> | undefined
watch(query, (q) => {
  clearTimeout(timer)
  timer = setTimeout(() => {
    search(q)
    void router.replace({ query: urlQuery(), hash: route.hash })
  }, 400)
})

/** 关键字变了：重新高亮，并跳到排在最前面的小节 */
function search(q: string) {
  const now = splitTerms(q)
  if (now.join(' ') === terms.value.join(' ')) return false
  terms.value = now
  activeHit.value = ''
  void nextTick(() => {
    applyMarks()
    if (hits.value[0]) openHit(hits.value[0])
  })
  return true
}

function headingEl(id: string): HTMLElement | null {
  return body.value?.querySelector<HTMLElement>(`[id="${CSS.escape(id)}"]`) ?? null
}

function scrollToId(id: string) {
  const el = headingEl(id)
  if (!el) return
  el.scrollIntoView({ block: 'start' })
  activeId.value = id
  if (route.hash !== `#${id}`) void router.replace({ query: urlQuery(), hash: `#${id}` })
}

/** 打开搜索结果：跳到该小节里第一处命中（标题命中则停在标题） */
function openHit(hit: GuideHit) {
  activeHit.value = hit.section.id
  const head = headingEl(hit.section.id)
  if (!head) return
  const inTitle = head.querySelector<HTMLElement>('mark.guide-hit')
  if (inTitle) {
    setCurrent(marks.value.indexOf(inTitle))
    scrollToId(hit.section.id)
    return
  }
  // 本节正文：从标题之后到下一个标题之前
  for (let el = head.nextElementSibling; el && !/^H[1-6]$/.test(el.tagName); el = el.nextElementSibling) {
    const m = el.querySelector<HTMLElement>('mark.guide-hit')
    if (m) return focusMark(marks.value.indexOf(m))
  }
  scrollToId(hit.section.id)
}

function onEnter(e: KeyboardEvent) {
  clearTimeout(timer)
  if (search(query.value)) {
    void router.replace({ query: urlQuery(), hash: route.hash })
    return
  }
  nextMark(e.shiftKey ? -1 : 1)
}

// 正文里的站内链接（如「见第 9 章」）在页面内跳转
function onBodyClick(e: MouseEvent) {
  const a = (e.target as HTMLElement).closest<HTMLAnchorElement>('a[data-anchor]')
  if (!a) return
  e.preventDefault()
  scrollToId(a.dataset.anchor!)
}

// ---- 左侧目录跟随滚动高亮 ----
let scroller: HTMLElement | null = null
let ticking = false

function updateActive() {
  ticking = false
  if (!body.value || !scroller) return
  const top = scroller.getBoundingClientRect().top + 80
  let current = ''
  for (const h of toc) {
    const el = headingEl(h.id)
    if (!el) continue
    if (el.getBoundingClientRect().top <= top) current = h.id
    else break
  }
  if (current && current !== activeId.value) {
    activeId.value = current
    tocEl.value?.querySelector<HTMLElement>(`[data-id="${CSS.escape(current)}"]`)?.scrollIntoView({ block: 'nearest' })
  }
}

function onScroll() {
  if (ticking) return
  ticking = true
  requestAnimationFrame(updateActive)
}

onMounted(() => {
  scroller = body.value?.closest<HTMLElement>('.el-main') ?? null
  scroller?.addEventListener('scroll', onScroll, { passive: true })
  applyMarks()
  // 等首屏布局稳定（如弹窗锁定滚动条会改变宽度）后再定位
  const hash = decodeURIComponent(route.hash.replace(/^#/, ''))
  requestAnimationFrame(() =>
    requestAnimationFrame(() => {
      if (hash) scrollToId(hash)
      else if (hits.value[0]) openHit(hits.value[0])
      else updateActive()
    }),
  )
})

onBeforeUnmount(() => {
  clearTimeout(timer)
  scroller?.removeEventListener('scroll', onScroll)
})
</script>

<template>
  <div class="help">
    <aside class="side">
      <el-input
        v-model="query"
        clearable
        prefix-icon="Search"
        placeholder="搜索手册，如：留出期、佣金、AI 复核"
        @keydown.enter.prevent="onEnter"
      />
      <div v-if="terms.length" class="hit-bar">
        <span v-if="marks.length">
          {{ hits.length }} 个小节，共 {{ marks.length }} 处<template v-if="markIndex >= 0"
            >（第 {{ markIndex + 1 }} 处）</template
          >
        </span>
        <span v-else>没有找到</span>
        <span class="hit-nav">
          <el-button
            text
            size="small"
            icon="ArrowUp"
            title="上一处（Shift + 回车）"
            :disabled="!marks.length"
            @click="nextMark(-1)"
          />
          <el-button
            text
            size="small"
            icon="ArrowDown"
            title="下一处（回车）"
            :disabled="!marks.length"
            @click="nextMark(1)"
          />
        </span>
      </div>

      <div v-if="terms.length" class="list">
        <div
          v-for="hit in hits"
          :key="hit.section.id"
          class="hit"
          :class="{ active: activeHit === hit.section.id }"
          @click="openHit(hit)"
        >
          <div class="hit-title">{{ hit.section.path.join(' › ') }}</div>
          <div class="hit-snippet">
            <template v-for="(p, i) in hit.snippet" :key="i">
              <mark v-if="p.hit">{{ p.t }}</mark>
              <template v-else>{{ p.t }}</template>
            </template>
          </div>
        </div>
        <el-empty
          v-if="!hits.length"
          :image-size="64"
          description="换个说法试试；多个关键字用空格分开，需要同时出现在同一小节里"
        />
      </div>

      <nav v-else ref="tocEl" class="list toc">
        <a
          v-for="h in toc"
          :key="h.id"
          :data-id="h.id"
          :class="['toc-item', `lv${h.level}`, { active: activeId === h.id }]"
          @click="scrollToId(h.id)"
        >
          {{ h.text }}
        </a>
      </nav>
    </aside>

    <!-- eslint-disable-next-line vue/no-v-html 手册来自仓库文件，解析时已转义 -->
    <article ref="body" class="guide-body" @click="onBodyClick" v-html="doc.html" />
  </div>
</template>

<style scoped>
.help {
  display: flex;
  gap: 24px;
  align-items: flex-start;
}

.side {
  position: sticky;
  top: 0;
  flex: 0 0 280px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: calc(100vh - 60px - 36px - 40px);
}

.hit-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.hit-nav {
  display: flex;
}

.list {
  overflow-y: auto;
  min-height: 0;
  flex: 1;
}

.hit {
  padding: 8px 10px;
  border-radius: 6px;
  cursor: pointer;
  border: 1px solid transparent;
}

.hit:hover {
  background: var(--el-fill-color-light);
}

.hit.active {
  border-color: var(--el-color-primary-light-5);
  background: var(--el-color-primary-light-9);
}

.hit-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--el-text-color-primary);
}

.hit-snippet {
  margin-top: 2px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--el-text-color-secondary);
  word-break: break-all;
}

.hit-snippet mark {
  background: var(--el-color-warning-light-7);
  color: inherit;
  border-radius: 2px;
}

.toc {
  display: flex;
  flex-direction: column;
  border-left: 1px solid var(--el-border-color-lighter);
}

.toc-item {
  padding: 4px 10px;
  font-size: 13px;
  line-height: 1.5;
  color: var(--el-text-color-regular);
  cursor: pointer;
  border-left: 2px solid transparent;
  margin-left: -1px;
}

.toc-item.lv2 {
  font-weight: 600;
  margin-top: 6px;
}

.toc-item.lv3 {
  padding-left: 22px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.toc-item:hover {
  color: var(--el-color-primary);
}

.toc-item.active {
  color: var(--el-color-primary);
  border-left-color: var(--el-color-primary);
}

.guide-body {
  flex: 1;
  min-width: 0;
  max-width: 920px;
  padding-bottom: 40vh;
  font-size: 14px;
  line-height: 1.8;
  color: var(--el-text-color-primary);
}

.guide-body :deep(h1) {
  font-size: 26px;
  margin: 0 0 16px;
}

.guide-body :deep(h2) {
  font-size: 21px;
  margin: 36px 0 12px;
  padding-bottom: 6px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.guide-body :deep(h3) {
  font-size: 17px;
  margin: 28px 0 10px;
}

.guide-body :deep(h4) {
  font-size: 15px;
  margin: 22px 0 8px;
}

.guide-body :deep(h1),
.guide-body :deep(h2),
.guide-body :deep(h3),
.guide-body :deep(h4) {
  scroll-margin-top: 12px;
}

.guide-body :deep(p) {
  margin: 8px 0;
}

.guide-body :deep(ul),
.guide-body :deep(ol) {
  padding-left: 24px;
  margin: 8px 0;
}

.guide-body :deep(li > p) {
  margin: 2px 0;
}

.guide-body :deep(blockquote) {
  margin: 12px 0;
  padding: 4px 14px;
  border-left: 4px solid var(--el-color-primary-light-5);
  background: var(--el-fill-color-lighter);
  color: var(--el-text-color-regular);
}

.guide-body :deep(code) {
  font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
  font-size: 12.5px;
  padding: 1px 5px;
  border-radius: 4px;
  background: var(--el-fill-color);
}

.guide-body :deep(pre) {
  margin: 10px 0;
  padding: 12px 14px;
  overflow-x: auto;
  border-radius: 6px;
  background: var(--el-fill-color-light);
  line-height: 1.55;
}

.guide-body :deep(pre code) {
  padding: 0;
  background: none;
}

.guide-body :deep(.table-wrap) {
  overflow-x: auto;
  margin: 10px 0;
}

.guide-body :deep(table) {
  border-collapse: collapse;
  font-size: 13px;
}

.guide-body :deep(th),
.guide-body :deep(td) {
  border: 1px solid var(--el-border-color-light);
  padding: 6px 10px;
  text-align: left;
  vertical-align: top;
}

.guide-body :deep(th) {
  background: var(--el-fill-color-light);
  white-space: nowrap;
}

.guide-body :deep(hr) {
  border: none;
  border-top: 1px solid var(--el-border-color-lighter);
  margin: 24px 0;
}

.guide-body :deep(a) {
  color: var(--el-color-primary);
  text-decoration: none;
}

.guide-body :deep(a:hover) {
  text-decoration: underline;
}

.guide-body :deep(.doc-ref) {
  border-bottom: 1px dashed var(--el-text-color-placeholder);
  cursor: help;
}

.guide-body :deep(mark.guide-hit) {
  background: var(--el-color-warning-light-7);
  color: inherit;
  border-radius: 2px;
}

.guide-body :deep(mark.guide-hit.current) {
  background: var(--el-color-warning);
  color: #fff;
}

@media (max-width: 900px) {
  .help {
    flex-direction: column;
  }

  .side {
    position: static;
    flex: none;
    width: 100%;
    max-height: none;
  }

  .list {
    max-height: 240px;
  }
}
</style>
