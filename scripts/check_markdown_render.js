// Regression check for the board's hand-rolled Markdown renderer.
//
// Extracts ``renderBriefMarkdown`` / ``inlineMarkdown`` / ``escapeHtml`` from the
// real ``static/app.js`` (so the check cannot drift from the shipped code) and
// asserts the structures the weekly brief and publishable article actually use.
// Run with: ``node scripts/check_markdown_render.js``

'use strict';

const fs = require('fs');
const path = require('path');

const appJs = fs.readFileSync(
  path.join(__dirname, '..', 'src', 'nutrition_signal_radar', 'static', 'app.js'),
  'utf8',
);

function extract(name) {
  const start = appJs.indexOf(`function ${name}`);
  if (start < 0) throw new Error(`function ${name} not found in app.js`);
  const end = appJs.indexOf('\n}', start);
  if (end < 0) throw new Error(`end of function ${name} not found`);
  return appJs.slice(start, end + 2);
}

/* eslint-disable no-eval */
const escapeHtml = eval(`(${extract('escapeHtml')})`);
const inlineMarkdown = eval(`(${extract('inlineMarkdown')})`);
const renderBriefMarkdown = eval(`(${extract('renderBriefMarkdown')})`);

let failures = 0;
function check(label, actual, expected) {
  if (actual === expected) {
    console.log(`ok   ${label}`);
  } else {
    failures += 1;
    console.error(`FAIL ${label}\n  expected: ${expected}\n  actual:   ${actual}`);
  }
}

// 1. Real weekly-article "来源" section: numbered markdown links.
const sources = renderBriefMarkdown(
  '## 来源\n1. [国务院任免（新华网）](https://www.news.cn/a)\n2. [专项整治通知（市场监管总局）](https://www.samr.gov.cn/b)\n',
);
check(
  'numbered links render as an ordered list',
  sources,
  '<h3>来源</h3><ol><li><a href="https://www.news.cn/a" target="_blank" rel="noreferrer">国务院任免（新华网）</a></li>' +
    '<li><a href="https://www.samr.gov.cn/b" target="_blank" rel="noreferrer">专项整治通知（市场监管总局）</a></li></ol>',
);

// 2. Chinese enumerators (no trailing space) and 「N) 」 join the same ordered list.
check(
  '「1、」and「2)」prefixes join the same ordered list',
  renderBriefMarkdown('1、甲\n2) 乙\n'),
  '<ol><li>甲</li><li>乙</li></ol>',
);

// 2b. Decimal-leading sentences must not become list items.
check(
  '「1.5亿元」 is not a list item',
  renderBriefMarkdown('1.5亿元规模的市场。'),
  '<p>1.5亿元规模的市场。</p>',
);

// 2c. Link text may contain one nested bracket level, as in real source titles.
check(
  'nested brackets inside link text still render as a link',
  renderBriefMarkdown(
    '3. [团标通知（中营学发[2026]40号）（食品伙伴网）](https://down.foodmate.net/info/sort/1/43798.html)\n',
  ),
  '<ol><li><a href="https://down.foodmate.net/info/sort/1/43798.html" target="_blank" rel="noreferrer">团标通知（中营学发[2026]40号）（食品伙伴网）</a></li></ol>',
);

// 3. Bullets keep working; switching list kinds closes the previous one.
check(
  'bullet and ordered lists split into separate elements',
  renderBriefMarkdown('- 要点\n1. 编号\n- 又一个要点\n'),
  '<ul><li>要点</li></ul><ol><li>编号</li></ol><ul><li>又一个要点</li></ul>',
);

// 4. Numbered lines without a space after the marker stay paragraphs.
check(
  'bare numbering like 「2026年」 is not a list item',
  renderBriefMarkdown('2026年8月发布。'),
  '<p>2026年8月发布。</p>',
);

// 5. Untrusted text stays escaped inside list items and paragraphs.
check(
  'html in list items is escaped',
  renderBriefMarkdown('1. <img src=x>文本\n'),
  '<ol><li>&lt;img src=x&gt;文本</li></ol>',
);

// 6. Headings, quotes, rules and bold still render as before.
check(
  'existing block structures unchanged',
  renderBriefMarkdown('# 标题\n> 引用\n---\n正文**加粗**\n'),
  '<h2>标题</h2><blockquote>引用</blockquote><hr><p>正文<strong>加粗</strong></p>',
);

if (failures) {
  console.error(`\n${failures} check(s) failed`);
  process.exit(1);
}
console.log('\nall renderer checks passed');
