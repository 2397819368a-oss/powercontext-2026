// 静态站点检索逻辑

// 全局数据
let allChunks = [];
let meta = null;

// HTML 转义函数（用于文本节点）
function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

// 属性值转义函数（用于 attr="..." 内）
function escapeAttr(text) {
  return escapeHtml(text).replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// 高亮关键词
function highlightKeyword(text, keyword) {
  if (!keyword) return escapeHtml(text);
  const escaped = escapeHtml(text);
  const regex = new RegExp(`(${keyword.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')})`, 'gi');
  return escaped.replace(regex, '<mark>$1</mark>');
}

// 提取文件名（从完整路径）
function getFileName(filePath) {
  return filePath.split(/[\\/]/).pop();
}

// dir_type 到中文标签的映射
function getTypeTag(dirType) {
  const map = {
    '10-MOC与索引': 'MOC',
    '11-赛题归档': '赛题',
    '12-原子卡片': '卡片',
    '13-代码片段': '代码',
    '14-文献卡片': '文献',
  };
  return map[dirType] || dirType;
}

// Markdown 清洗（逐行处理，保留行结构）
function cleanMarkdownLines(text) {
  const lines = text.split('\n');
  const cleaned = [];
  for (let line of lines) {
    // 丢弃代码围栏
    if (line.trim().startsWith('```')) continue;
    // 丢弃表格分隔行
    if (/^\s*\|?[\s:\-|]+\|?\s*$/.test(line)) continue;
    // 去掉行首的 # + 空格
    line = line.replace(/^#+\s*/, '').trimStart();
    // 去掉行首的 > + 空格
    line = line.replace(/^>\s*/, '').trimStart();
    // 去掉行首的列表符（- / * / +）+ 空格
    if (/^[-*+]\s/.test(line)) line = line.substring(2);
    // 去掉行首的数字编号（如 "1. "）
    line = line.replace(/^\d+\.\s+/, '');
    // 把 | 替换成空格
    line = line.replace(/\|/g, ' ');
    cleaned.push(line);
  }
  // 去掉 ** __ ` 装饰符（保留内部文字）
  return cleaned.map(line => line.replace(/\*\*/g, '').replace(/__/g, '').replace(/`/g, ''));
}

// 折叠为单行文本
function collapseToSingleLine(lines) {
  return lines.map(l => l.trim()).filter(l => l).join(' ').replace(/\s+/g, ' ').trim();
}

// 生成摘要（以关键词命中点为中心）
function makeSnippet(content, keyword, heading, width = 120) {
  if (!content) return '';

  // 步骤 1：在原始 content 上找关键词位置
  const originalPos = keyword ? content.toLowerCase().indexOf(keyword.toLowerCase()) : -1;

  // 步骤 2：逐行清洗 Markdown
  const cleanedLines = cleanMarkdownLines(content);

  // 步骤 3：如果首行清洗后等于 heading 或为空，丢弃首行
  const cleanedHeading = heading ? collapseToSingleLine(cleanMarkdownLines(heading)) : '';
  if (cleanedLines.length > 0) {
    const firstLine = cleanedLines[0].trim();
    if (firstLine === cleanedHeading || !firstLine) {
      cleanedLines.shift();
    }
  }

  // 步骤 4：折叠为单行文本
  const cleaned = collapseToSingleLine(cleanedLines);
  if (!cleaned) return '';

  // 步骤 5：在清洗后文本上重新找关键词位置
  const pos = keyword ? cleaned.toLowerCase().indexOf(keyword.toLowerCase()) : -1;

  // 步骤 6：截取窗口
  if (pos < 0) {
    const snippet = cleaned.substring(0, width);
    return snippet + (cleaned.length > width ? '...' : '');
  }

  let start = Math.max(0, pos - Math.floor(width / 3));
  let end = Math.min(cleaned.length, start + width);
  if (end - start < width) {
    start = Math.max(0, end - width);
  }

  const snippet = cleaned.substring(start, end);
  const prefix = start > 0 ? '...' : '';
  const suffix = end < cleaned.length ? '...' : '';
  return prefix + snippet + suffix;
}

// 打分公式（必须与 check_static_parity.py 保持一致）
// score = 2.0 × 关键词出现次数 + 3.0 × (标题命中 ? 1 : 0) − 0.001 × content 长度
function calculateScore(chunk, keyword) {
  if (!keyword) return 0;

  const keywordLower = keyword.toLowerCase();
  const contentLower = chunk.content.toLowerCase();
  const headingLower = chunk.heading.toLowerCase();

  // 关键词在 content 中出现的次数
  let count = 0;
  let pos = 0;
  while ((pos = contentLower.indexOf(keywordLower, pos)) !== -1) {
    count++;
    pos += keywordLower.length;
  }

  // 标题是否命中
  const headingHit = headingLower.includes(keywordLower) ? 1 : 0;

  // content 长度惩罚
  const lengthPenalty = chunk.content.length * 0.001;

  return 2.0 * count + 3.0 * headingHit - lengthPenalty;
}

// 搜索函数
function searchChunks(keyword, filters, topK) {
  if (!keyword || !keyword.trim()) return [];

  const keywordLower = keyword.toLowerCase();
  let candidates = allChunks;

  // 过滤：目录类型
  if (filters.dirType) {
    candidates = candidates.filter(c => c.dir_type === filters.dirType);
  }

  // 过滤：年份
  if (filters.year) {
    candidates = candidates.filter(c => c.year === filters.year);
  }

  // 关键词匹配（子串包含）
  const matched = candidates.filter(c =>
    c.content.toLowerCase().includes(keywordLower) ||
    c.heading.toLowerCase().includes(keywordLower)
  );

  // 打分
  const scored = matched.map(c => ({
    ...c,
    score: calculateScore(c, keyword),
  }));

  // 排序：score 降序，score 相同时 id 升序
  scored.sort((a, b) => {
    if (b.score !== a.score) return b.score - a.score;
    return a.id - b.id;
  });

  // 取 Top K
  return scored.slice(0, topK);
}

// 加载数据
async function loadData() {
  try {
    const [chunksRes, metaRes] = await Promise.all([
      fetch('data/chunks.json'),
      fetch('data/meta.json'),
    ]);
    allChunks = await chunksRes.json();
    meta = await metaRes.json();
    document.getElementById('stats').textContent =
      `库内统计：${meta.notes_count} 篇 / ${meta.chunks_count} 块`;
  } catch (e) {
    document.getElementById('stats').textContent = '数据加载失败';
    console.error('Failed to load data:', e);
  }
}

// 更新检索条件面板（搜索成功后自动展开）
function updateConditionsPanel(keyword, filters, topK, matchedCount, totalCount) {
  const panel = document.getElementById('conditionsPanel');
  const summary = document.getElementById('conditionsSummary');
  const body = document.getElementById('conditionsBody');
  const toggle = document.getElementById('conditionsToggle');

  const dirTypeText = filters.dirType || '不限';
  const yearText = filters.year || '不限';

  summary.textContent = '检索条件';
  body.innerHTML = `
    <div><strong>关键词</strong> = ${escapeHtml(keyword)}</div>
    <div><strong>目录</strong> = ${escapeHtml(dirTypeText)}</div>
    <div><strong>年份</strong> = ${escapeHtml(yearText)}</div>
    <div><strong>Top K</strong> = ${topK}</div>
    <div><strong>候选池</strong> ${totalCount} 块（按目录/年份过滤后）→ 命中 ${matchedCount} 块</div>
    <div class="note">静态版无 SQL 执行，以上为等价检索条件；本地版对应语句见仓库 docs/样例.md</div>
  `;
  panel.classList.add('active');
  body.classList.add('active');
  toggle.textContent = '[收起]';
}

// 搜索
async function doSearch() {
  const q = document.getElementById('searchInput').value.trim();
  if (!q) {
    document.getElementById('results').innerHTML = '<div class="empty">请输入关键词</div>';
    document.getElementById('conditionsPanel').classList.remove('active');
    return;
  }

  const type = document.getElementById('typeFilter').value;
  const year = document.getElementById('yearFilter').value;
  const topK = parseInt(document.getElementById('topKInput').value) || 10;

  // 更新 URL 参数
  const params = new URLSearchParams();
  params.set('q', q);
  if (type) params.set('type', type);
  if (year) params.set('year', year);
  params.set('top_k', topK);
  history.replaceState(null, '', '?' + params.toString());

  document.getElementById('results').innerHTML = '<div class="loading">搜索中...</div>';

  // 等待一帧，让 loading 显示出来
  await new Promise(resolve => setTimeout(resolve, 0));

  const filters = {};
  if (type) filters.dirType = type;
  if (year) filters.year = parseInt(year);

  const results = searchChunks(q, filters, topK);

  // 更新检索条件面板（候选池按目录/年份过滤后计算）
  const pool = allChunks.filter(c =>
    (!filters.dirType || c.dir_type === filters.dirType) &&
    (!filters.year || c.year === filters.year)
  );
  const matchedCount = pool.filter(c =>
    c.content.toLowerCase().includes(q.toLowerCase()) ||
    c.heading.toLowerCase().includes(q.toLowerCase())
  ).length;
  updateConditionsPanel(q, filters, topK, matchedCount, pool.length);

  if (results.length === 0) {
    document.getElementById('results').innerHTML = '<div class="empty">未找到结果</div>';
    return;
  }

  const html = results.map(r => `
    <div class="result-card" data-chunk-id="${r.id}" data-keyword="${escapeAttr(q)}">
      <div class="result-header">
        <div class="result-meta">
          <span class="result-score">score: ${r.score.toFixed(4)}</span>
          <span class="result-type-tag">${escapeHtml(getTypeTag(r.dir_type))}</span>
        </div>
        <span class="result-lines">行 ${r.line_start}-${r.line_end}</span>
      </div>
      <div class="result-file" title="${escapeAttr(r.file_path)}">${escapeHtml(r.file_name)}</div>
      <div class="result-heading">${highlightKeyword(r.heading || '(前言)', q)}</div>
      <div class="result-snippet">${highlightKeyword(makeSnippet(r.content, q, r.heading), q)}</div>
      <div class="detail-panel" id="detail-${r.id}"></div>
    </div>
  `).join('');

  document.getElementById('results').innerHTML = html;
}

// 展开详情
async function toggleDetail(chunkId, keyword) {
  const panel = document.getElementById(`detail-${chunkId}`);
  if (panel.classList.contains('active')) {
    panel.classList.remove('active');
    return;
  }

  // 从 allChunks 中找到对应的 chunk
  const chunk = allChunks.find(c => c.id === parseInt(chunkId));
  if (!chunk) {
    panel.innerHTML = '<div class="empty">未找到 chunk</div>';
    panel.classList.add('active');
    return;
  }

  panel.innerHTML = '<div class="loading">加载中...</div>';
  panel.classList.add('active');

  const lines = [];
  const contentLines = chunk.content.split('\n');

  // 主内容（从 line_start 到 line_end）
  for (let i = 0; i < contentLines.length; i++) {
    const lineNum = chunk.line_start + i;
    if (lineNum > chunk.line_end) break;
    lines.push(`<div class="detail-line chunk-line"><span class="line-num">${lineNum}</span><span class="line-text">${highlightKeyword(contentLines[i], keyword)}</span></div>`);
  }

  panel.innerHTML = `<div class="detail-content">${lines.join('')}</div>`;
}

// 检索条件面板折叠/展开
document.getElementById('conditionsHeader').addEventListener('click', () => {
  const body = document.getElementById('conditionsBody');
  const toggle = document.getElementById('conditionsToggle');
  if (body.classList.contains('active')) {
    body.classList.remove('active');
    toggle.textContent = '[展开]';
  } else {
    body.classList.add('active');
    toggle.textContent = '[收起]';
  }
});

// 事件委托：处理卡片点击
document.getElementById('results').addEventListener('click', (e) => {
  const card = e.target.closest('.result-card');
  if (!card) return;
  const chunkId = card.dataset.chunkId;
  const keyword = card.dataset.keyword;
  if (chunkId && keyword !== undefined) {
    toggleDetail(parseInt(chunkId), keyword);
  }
});

// 示例按钮点击
document.querySelectorAll('.example-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.getElementById('searchInput').value = btn.dataset.q;
    doSearch();
  });
});

// 搜索按钮和回车
document.getElementById('searchBtn').addEventListener('click', doSearch);
document.getElementById('searchInput').addEventListener('keypress', (e) => {
  if (e.key === 'Enter') doSearch();
});

// 从 URL 参数恢复搜索
function loadFromUrl() {
  const params = new URLSearchParams(window.location.search);
  const q = params.get('q');
  if (q) {
    document.getElementById('searchInput').value = q;
    if (params.get('type')) document.getElementById('typeFilter').value = params.get('type');
    if (params.get('year')) document.getElementById('yearFilter').value = params.get('year');
    if (params.get('top_k')) document.getElementById('topKInput').value = params.get('top_k');
    doSearch();
  }
}

// 初始化
loadData().then(() => {
  loadFromUrl();
});
