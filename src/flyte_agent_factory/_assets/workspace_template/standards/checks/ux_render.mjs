// Renders each page at each viewport with Playwright's Chromium and records accessibility and layout findings.
// Run by ux_render.py (and by ux_reading_level.py for the rendered text), which resolves Playwright and passes
// everything in via argv[2] (a JSON file) and the UX_PLAYWRIGHT_DIR environment variable. Writes config.out (JSON)
// and, when inspecting, the screenshots. Exit 0: rendered (findings are in the output); 3: no usable Playwright or
// browser; anything else: a bug here.
//
// config.mode "inspect" (the default): findings per page and viewport. Keyboard reach and visible focus press Tab
// many times, so they run only at the largest viewport. Reflow runs at the smallest viewport, and at any 320 or
// narrower: WCAG 1.4.10 names 320, but a project that declares only wider viewports must still not scroll sideways
// at the narrowest one it targets.
// config.under13: the audience includes children under 13, so every target must be 44x44 CSS px (no spacing
// exception; a link inside a sentence is exempt, as its size is set by the text around it). config.olderAdults:
// the audience includes people 65 or over, so running text must be at least 16px.
// config.mode "text": the visible text of each page at config.viewport, one string per block of text.
//
// Same-origin frames and open shadow roots are inspected as part of the page; cross-origin frames are listed as
// not measured. file:// pages are loaded with --allow-file-access-from-files so a local page's own frames count as
// same-origin, as they would when served.
import { createRequire } from 'node:module';
import fs from 'node:fs';
import path from 'node:path';

const config = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
// Past this, pages not yet rendered are reported as not rendered, so the caller's own timeout is never the
// thing that ends the run and the findings so far are kept.
const deadline = Date.now() + (config.budgetSeconds ?? 180) * 1000;

let chromium;
try {
  ({ chromium } = createRequire(import.meta.url)(process.env.UX_PLAYWRIGHT_DIR));
  if (!chromium) throw new Error('the package has no chromium export');
} catch (error) {
  console.log(`Playwright could not be loaded from ${process.env.UX_PLAYWRIGHT_DIR}: ${String(error.message).split('\n')[0]}`);
  process.exit(3);
}

// Runs inside the page and defines window.__ux. Self-contained: it is serialised and sent to the browser.
function installUx() {
  if (window.__ux) return;
  // A focus indicator drawn this far outside the element (an outline with an offset, a ring on the parent)
  // still counts as the element's.
  const FOCUS_PAD = 8;
  const NEVER_TEXT = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'HEAD', 'TITLE']);
  const NATIVE = 'a[href], area[href], button, input:not([type=hidden]), select, textarea, details > summary:first-of-type, [contenteditable]:not([contenteditable=false])';
  // ARIA roles a person operates. A page that gives a <div> one of these must make it reachable and name it.
  const WIDGET_ROLES = ['button', 'checkbox', 'combobox', 'gridcell', 'link', 'listbox', 'menuitem', 'menuitemcheckbox', 'menuitemradio', 'option', 'radio', 'searchbox', 'slider', 'spinbutton', 'switch', 'tab', 'textbox', 'treeitem'];
  // Items of a composite widget use a roving tabindex: Tab reaches one item and the arrow keys the rest, so the
  // group counts as reached when any item or the container (aria-activedescendant) has focus.
  const ITEM_CONTAINERS = { tab: 'tablist', option: 'listbox', menuitem: 'menu, menubar', menuitemcheckbox: 'menu, menubar', menuitemradio: 'menu, menubar', treeitem: 'tree, treegrid', gridcell: 'grid, treegrid', radio: 'radiogroup' };
  const CONTROL = `${NATIVE}, ${WIDGET_ROLES.map((r) => `[role~=${r}]`).join(', ')}`;
  // Roles whose accessible name may come from their content (WAI-ARIA "name from: contents").
  const NAME_FROM_CONTENT = new Set(['button', 'cell', 'checkbox', 'columnheader', 'gridcell', 'heading', 'link', 'menuitem', 'menuitemcheckbox', 'menuitemradio', 'option', 'radio', 'row', 'rowheader', 'switch', 'tab', 'tooltip', 'treeitem']);
  const TEXT_INPUTS = new Set(['text', 'search', 'email', 'url', 'tel', 'password', 'number', '']);

  const view = (node) => node.ownerDocument.defaultView;
  const css = (el, pseudo) => view(el).getComputedStyle(el, pseudo || null);
  const roleOf = (el) => (el.getAttribute('role') || '').trim().split(/\s+/)[0].toLowerCase();
  // The parent in the flat sense: across a shadow root to its host, and out of a frame to its <iframe>.
  function parentOf(node) {
    if (node.parentElement) return node.parentElement;
    const up = node.parentNode;
    if (up && up.nodeType === 11 && up.host) return up.host;
    if (up && up.nodeType === 9) return up.defaultView?.frameElement ?? null;
    return null;
  }
  const isAncestor = (a, el) => {
    for (let n = parentOf(el); n; n = parentOf(n)) if (n === a) return true;
    return false;
  };
  const closestDeep = (el, sel) => {
    for (let n = el; n; n = parentOf(n)) if (n.nodeType === 1 && n.matches(sel)) return n;
    return null;
  };
  const frameDoc = (el) => {
    try {
      return el.contentDocument;
    } catch {
      return null;
    }
  };

  // Every element of the page, its open shadow roots and its same-origin frames, in document order.
  const crossOrigin = [];
  function allElements() {
    const out = [];
    const visit = (root) => {
      for (const el of root.querySelectorAll('*')) {
        out.push(el);
        if (el.shadowRoot) visit(el.shadowRoot);
        if (el.tagName === 'IFRAME' || el.tagName === 'FRAME') {
          const doc = frameDoc(el);
          if (doc && doc.documentElement) visit(doc);
          else if (!crossOrigin.includes(el)) crossOrigin.push(el);
        }
      }
    };
    visit(document);
    return out;
  }

  function ownSelector(el) {
    const parts = [];
    for (let node = el; node && node.nodeType === 1 && parts.length < 4; node = node.parentElement) {
      const tag = node.tagName.toLowerCase();
      if (node.id) {
        parts.unshift(`${tag}#${node.id}`);
        break;
      }
      const same = node.parentElement ? [...node.parentElement.children].filter((c) => c.tagName === node.tagName) : [];
      parts.unshift(same.length > 1 ? `${tag}:nth-of-type(${same.indexOf(node) + 1})` : tag);
    }
    return parts.join(' > ');
  }
  // `host >>> inner` for an element in a shadow root or a frame.
  function selector(el) {
    const root = el.getRootNode();
    const host = root.host || (root.nodeType === 9 ? root.defaultView?.frameElement : null);
    return host ? `${selector(host)} >>> ${ownSelector(el)}` : ownSelector(el);
  }

  // The element's box in the top page's viewport coordinates.
  function topRect(el) {
    const r = el.getBoundingClientRect();
    let x = r.left;
    let y = r.top;
    for (let win = view(el); win.frameElement; win = win.parent) {
      const f = win.frameElement.getBoundingClientRect();
      x += f.left + win.frameElement.clientLeft;
      y += f.top + win.frameElement.clientTop;
    }
    return { left: x, top: y, right: x + r.width, bottom: y + r.height, width: r.width, height: r.height };
  }
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    // `||`, not `&&`: where no fonts are installed text lays out with zero height, and is still on the page.
    if (!(r.width > 0 || r.height > 0)) return false;
    if (!el.checkVisibility({ visibilityProperty: true, opacityProperty: true })) return false;
    const frame = view(el).frameElement;
    return !frame || visible(frame);
  };

  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = 1;
  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  function parseColor(value) {
    const m = /^rgba?\(([^)]+)\)$/.exec(value.trim());
    if (m) {
      const p = m[1].split(/[\s,/]+/).filter(Boolean).map(Number);
      return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1];
    }
    // Colour spaces other than rgb (lab, oklch, color()): let the browser convert.
    ctx.clearRect(0, 0, 1, 1);
    ctx.fillStyle = '#000';
    ctx.fillStyle = value;
    ctx.fillRect(0, 0, 1, 1);
    const d = ctx.getImageData(0, 0, 1, 1).data;
    return [d[0], d[1], d[2], d[3] / 255];
  }
  const over = (fg, bg) => [0, 1, 2].map((i) => fg[i] * fg[3] + bg[i] * (1 - fg[3]));
  const luminance = (rgb) => {
    const [r, g, b] = rgb.map((c) => {
      const s = c / 255;
      return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const hex = (rgb) => '#' + rgb.map((c) => Math.round(c).toString(16).padStart(2, '0')).join('');
  // What shows through where nothing paints: Chromium's dark canvas when the page opts in to a dark scheme.
  const canvasColor = () =>
    css(document.documentElement).colorScheme.includes('dark') && matchMedia('(prefers-color-scheme: dark)').matches
      ? [18, 18, 18]
      : [255, 255, 255];

  // The colour a box paints behind text, or why it cannot be known.
  function paintOf(node) {
    const s = css(node);
    if (s.backgroundImage !== 'none') return { why: 'text over a background image or gradient' };
    if (node instanceof view(node).SVGGeometryElement) {
      if (s.fill === 'none') return { color: [0, 0, 0, 0] };
      if (!s.fill.startsWith('rgb')) return { why: 'text over an SVG gradient or pattern' };
      const c = parseColor(s.fill);
      c[3] *= Number(s.fillOpacity);
      return { color: c };
    }
    return { color: parseColor(s.backgroundColor) };
  }
  const REPLACED = ['IMG', 'VIDEO', 'CANVAS', 'PICTURE', 'OBJECT', 'EMBED', 'IFRAME'];
  // Backgrounds of the text's ancestors, top first, down to the first opaque one.
  function chainBackdrop(el) {
    const layers = [];
    for (let n = el; n; n = parentOf(n)) {
      const p = paintOf(n);
      if (p.why) return p;
      if (p.color[3] > 0) layers.push(p.color);
      if (p.color[3] >= 1) break;
    }
    return { layers };
  }
  // A box painted under the text that is not its ancestor (a positioned sibling, an SVG shape, an image) is
  // missed by the ancestor walk. elementsFromPoint lists what is painted at a point, top first; use it when such
  // a box comes before the first opaque ancestor. Returns null to fall back to the ancestor walk.
  function stackBackdrop(el, point) {
    const win = view(el);
    if (!point || point.x < 0 || point.y < 0 || point.x >= win.innerWidth || point.y >= win.innerHeight) return null;
    const stack = el.getRootNode().elementsFromPoint(point.x, point.y);
    const i = stack.indexOf(el);
    if (i < 0) return null;
    const layers = [];
    let foreign = false;
    for (const n of stack.slice(i + 1)) {
      const mine = isAncestor(n, el);
      if (!mine && REPLACED.includes(n.tagName)) return { why: `text over an <${n.tagName.toLowerCase()}>` };
      const p = paintOf(n);
      if (!mine && (p.why || p.color[3] > 0)) foreign = true;
      if (p.why) return foreign ? p : null;
      if (p.color[3] > 0) layers.push(p.color);
      if (p.color[3] >= 1) break;
    }
    return foreign ? { layers } : null;
  }
  // Opacity of the text and its ancestors, with filter: opacity(); other filters and blend modes change colours
  // in ways this does not model, so they make the contrast unmeasured. For generated text `pseudo` names the
  // pseudo-element, whose own style counts first.
  function fade(el, pseudo) {
    let alpha = 1;
    for (let n = el; n; n = parentOf(n)) {
      for (const s of n === el && pseudo ? [css(el, pseudo), css(n)] : [css(n)]) {
        alpha *= Number(s.opacity);
        if (s.filter !== 'none') {
          for (const [, fn, arg] of s.filter.matchAll(/([a-z-]+)\(([^)]*)\)/g)) {
            if (fn !== 'opacity') return { why: `CSS filter ${fn}() on the text or an ancestor` };
            alpha *= arg.trim().endsWith('%') ? parseFloat(arg) / 100 : parseFloat(arg);
          }
        }
        if (s.mixBlendMode !== 'normal') return { why: `mix-blend-mode ${s.mixBlendMode} on the text or an ancestor` };
      }
    }
    return { alpha };
  }
  const ownText = (el) => [...el.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent).join('').trim();
  const pseudoText = (el, which) => {
    const s = css(el, which);
    if (s.display === 'none' || !s.content || ['none', 'normal'].includes(s.content)) return '';
    return [...s.content.matchAll(/"((?:[^"\\]|\\.)*)"/g)].map((m) => m[1]).join('');
  };

  // Why the paint behind generated text cannot be established from the element's backdrop, or ''.
  function pseudoBackdropUnknown(p) {
    if (p.position === 'absolute' || p.position === 'fixed') return 'generated text positioned out of flow';
    if (p.backgroundImage !== 'none') return 'text over a background image or gradient';
    return '';
  }

  // The texts a reader sees on each element: its own text nodes, a form field's value or placeholder, a select's
  // chosen option, SVG text (painted with fill, not color), and ::before/::after content.
  function textItems(el) {
    const items = [];
    const s = css(el);
    const tag = el.tagName;
    const isSvg = el instanceof view(el).SVGElement;
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (isSvg) {
      const text = ownText(el);
      if (text && el instanceof view(el).SVGTextContentElement && s.fill !== 'none') {
        if (!s.fill.startsWith('rgb')) items.push({ text, why: 'SVG text painted with a gradient or pattern' });
        else items.push({ text, fg: s.fill, alpha: Number(s.fillOpacity), style: s });
      }
      return items;
    }
    if ((tag === 'INPUT' && TEXT_INPUTS.has(type)) || tag === 'TEXTAREA') {
      if (el.value) items.push({ text: el.value, fg: s.webkitTextFillColor, style: s });
      else if (el.placeholder) {
        const p = css(el, '::placeholder');
        items.push({ text: el.placeholder, fg: p.webkitTextFillColor, style: p, label: 'placeholder' });
      }
    } else if (tag === 'INPUT' && ['button', 'submit', 'reset'].includes(type) && el.value) {
      items.push({ text: el.value, fg: s.webkitTextFillColor, style: s });
    } else if (tag === 'SELECT') {
      const chosen = el.selectedOptions?.[0]?.text?.trim();
      if (chosen) items.push({ text: chosen, fg: s.webkitTextFillColor, style: s });
    } else if (tag !== 'OPTION') {
      const text = ownText(el);
      if (text) items.push({ text, fg: s.webkitTextFillColor, style: s });
    }
    // Generated text is painted in the flow of its element, so it is measured like the element's own text over the
    // element's backdrop and the pseudo-element's own background. Out of flow it can sit over boxes that
    // neither the element's ancestors nor the element show, and then its backdrop is not known.
    for (const which of ['::before', '::after']) {
      const text = pseudoText(el, which).trim();
      const p = css(el, which);
      if (!/[\p{L}\p{N}]/u.test(text) || p.visibility !== 'visible') continue;
      const item = { text, pseudo: which, fg: p.webkitTextFillColor, style: p };
      const unknown = pseudoBackdropUnknown(p);
      if (unknown) item.why = unknown;
      items.push(item);
    }
    return items;
  }
  // Where to look for what is painted under the text: the middle of its first line, else of its box.
  function pointOf(el, item) {
    if (!item.label && !item.pseudo) {
      const node = [...el.childNodes].find((n) => n.nodeType === 3 && n.textContent.trim());
      if (node) {
        const range = el.ownerDocument.createRange();
        range.selectNodeContents(node);
        const r = range.getClientRects()[0];
        if (r && r.width > 0) return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
      }
    }
    const r = el.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + Math.min(r.height, 40) / 2 };
  }

  // Contrast of every visible text. The page is scrolled a screen at a time so elementsFromPoint can see what is
  // painted under each text; the scroll position is restored afterwards.
  function contrast(scheme) {
    const findings = [];
    const unmeasured = [];
    const pending = [];
    for (const el of allElements()) {
      if (NEVER_TEXT.has(el.tagName) || !visible(el) || el.closest(':disabled')) continue;
      for (const item of textItems(el)) pending.push({ el, item });
    }
    const step = Math.max(innerHeight, 1);
    const screens = new Map();
    for (const p of pending) {
      const r = topRect(p.el);
      const screen = Math.max(0, Math.floor((r.top + scrollY + Math.min(r.height, 40) / 2) / step));
      if (!screens.has(screen)) screens.set(screen, []);
      screens.get(screen).push(p);
    }
    const start = [scrollX, scrollY];
    for (const [screen, list] of [...screens].sort((a, b) => a[0] - b[0])) {
      // Beyond 40 screens only the ancestor walk is used, so an endless page cannot keep this busy.
      if (screen <= 40) window.scrollTo(start[0], screen * step);
      for (const { el, item } of list) {
        const where = { selector: selector(el) + (item.pseudo ?? ''), text: item.text.slice(0, 40) };
        // A reason for generated text says so, so the reviewer looks at the right text in the screenshot.
        const skip = (why) => {
          const reason = item.pseudo ? `${why}, in ${item.pseudo} content (generated text)` : why;
          unmeasured.push({ ...where, what: 'contrast', reason });
        };
        if (item.why) {
          skip(item.why);
          continue;
        }
        const faded = fade(el, item.pseudo);
        if (faded.why) {
          skip(faded.why);
          continue;
        }
        const back = (screen <= 40 && stackBackdrop(el, pointOf(el, item))) || chainBackdrop(el);
        if (back.why) {
          skip(back.why);
          continue;
        }
        let bg = canvasColor();
        for (const c of [...back.layers].reverse()) bg = over(c, bg);
        if (item.pseudo) bg = over(parseColor(item.style.backgroundColor), bg);
        const fgColor = parseColor(item.fg);
        fgColor[3] *= faded.alpha * (item.alpha ?? 1);
        const fg = over(fgColor, bg);
        const [hi, lo] = [luminance(fg), luminance(bg)].sort((a, b) => b - a);
        const ratio = (hi + 0.05) / (lo + 0.05);
        const size = parseFloat(item.style.fontSize);
        const large = size >= 24 || (size >= 18.66 && parseInt(item.style.fontWeight, 10) >= 700);
        const need = large ? 3 : 4.5;
        if (ratio < need) {
          const what = [item.label, scheme === 'dark' ? 'dark colour scheme' : ''].filter(Boolean).join(', ');
          findings.push({
            kind: 'contrast',
            selector: where.selector,
            message: `${ratio.toFixed(2)}:1 < ${need}:1 (${hex(fg)} on ${hex(bg)}, ${size}px${large ? ' large' : ''}${what ? `, ${what}` : ''}) "${item.text.slice(0, 30)}"`,
            ratio,
          });
        }
      }
    }
    window.scrollTo(start[0], start[1]);
    return { findings, unmeasured };
  }

  // Whether any stylesheet styles a dark colour scheme. A sheet that cannot be read (cross-origin) might, so it
  // counts as yes: the dark pass then only costs time.
  function stylesDarkScheme() {
    const mentions = (rules) =>
      [...rules].some(
        (r) =>
          (r.media && r.media.mediaText.includes('prefers-color-scheme')) ||
          (r.conditionText || '').includes('prefers-color-scheme') ||
          (r.cssRules && mentions(r.cssRules)) ||
          (r.styleSheet && sheetMentions(r.styleSheet))
      );
    const sheetMentions = (sheet) => {
      try {
        return sheet.media.mediaText.includes('prefers-color-scheme') || mentions(sheet.cssRules);
      } catch {
        return true;
      }
    };
    const roots = [document, ...allElements().filter((e) => e.shadowRoot).map((e) => e.shadowRoot)];
    for (const el of allElements()) if (el.tagName === 'IFRAME' && frameDoc(el)) roots.push(frameDoc(el));
    for (const root of roots) {
      const sheets = [...(root.styleSheets || []), ...(root.adoptedStyleSheets || [])];
      if (sheets.some(sheetMentions)) return true;
      const top = root.documentElement;
      if (top && css(top).colorScheme.includes('dark')) return true;
    }
    return false;
  }

  // Text of a subtree as it counts towards an accessible name: hidden parts (display:none, visibility:hidden,
  // aria-hidden) contribute nothing unless `includeHidden` (for a node named by aria-labelledby).
  const SVG_NS = 'http://www.w3.org/2000/svg';
  const kids = (node) => {
    if (node.shadowRoot) return [...node.shadowRoot.childNodes];
    if (node.tagName === 'SLOT') {
      const assigned = node.assignedNodes({ flatten: true });
      return assigned.length ? assigned : [...node.childNodes];
    }
    return [...node.childNodes];
  };
  function nameText(node, includeHidden) {
    if (node.nodeType === 3) return node.textContent;
    if (node.nodeType !== 1) return '';
    if (NEVER_TEXT.has(node.tagName)) return '';
    const s = css(node);
    if (!includeHidden && (s.display === 'none' || node.getAttribute('aria-hidden') === 'true')) return '';
    const label = node.getAttribute('aria-label');
    if (label && label.trim()) return ` ${label} `;
    if (node.tagName === 'IMG' || node.tagName === 'AREA') return ` ${node.getAttribute('alt') || ''} `;
    if (['INPUT', 'SELECT', 'TEXTAREA'].includes(node.tagName)) return '';
    if (node.namespaceURI === SVG_NS && node.localName === 'svg') {
      const title = [...node.children].find((c) => c.localName === 'title');
      if (title) return ` ${title.textContent} `;
    }
    const shown = includeHidden || s.visibility === 'visible';
    const inner = kids(node)
      .map((k) => (k.nodeType === 3 ? (shown ? k.textContent : '') : nameText(k, includeHidden)))
      .join('');
    const pseudo = (which) => (shown ? pseudoText(node, which) : '');
    const gap = s.display.startsWith('inline') ? '' : ' ';
    return gap + pseudo('::before') + inner + pseudo('::after') + gap;
  }
  // The essentials of the accessible name computation (WAI-ARIA accname and HTML-AAM), in their order:
  // aria-labelledby, aria-label, a native label or value, alt, content for roles named by content, title,
  // placeholder. Checked against Chromium's own accessibility tree on the cases the tests cover.
  function accessibleName(el) {
    const clean = (s) => (s || '').replace(/\s+/g, ' ').trim();
    const ids = clean(el.getAttribute('aria-labelledby'));
    if (ids) {
      const root = el.getRootNode();
      const refs = ids.split(' ').map((id) => root.getElementById(id)).filter(Boolean);
      const name = clean(refs.map((n) => nameText(n, !n.checkVisibility({ visibilityProperty: true }))).join(' '));
      if (name) return name;
    }
    const label = clean(el.getAttribute('aria-label'));
    if (label) return label;
    const tag = el.tagName;
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (tag === 'INPUT' && ['button', 'submit', 'reset'].includes(type)) {
      return clean(el.value) || { submit: 'Submit', reset: 'Reset' }[type] || clean(el.title);
    }
    if (tag === 'INPUT' && type === 'image') return clean(el.alt) || clean(el.title);
    if (el.labels && el.labels.length) {
      const name = clean([...el.labels].map((l) => nameText(l, false)).join(' '));
      if (name) return name;
    }
    if (tag === 'IMG' || tag === 'AREA') {
      const alt = clean(el.getAttribute('alt'));
      if (alt) return alt;
    }
    const role = roleOf(el);
    const fromContent = role ? NAME_FROM_CONTENT.has(role) : !['INPUT', 'SELECT', 'TEXTAREA'].includes(tag);
    if (fromContent) {
      const name = clean(nameText(el, false));
      if (name) return name;
    }
    const title = clean(el.getAttribute('title'));
    if (title) return title;
    if (tag === 'INPUT' || tag === 'TEXTAREA') return clean(el.getAttribute('placeholder'));
    return '';
  }

  // The size Chromium draws an unstyled checkbox and radio: WCAG 2.5.8 exempts a target whose size the user
  // agent sets. Measured in a shadow root, where the page's styles do not reach.
  function userAgentToggleSizes() {
    const host = document.createElement('div');
    (document.body || document.documentElement).append(host);
    const root = host.attachShadow({ mode: 'open' });
    root.innerHTML = '<input type=checkbox><input type=radio>';
    const sizes = Object.fromEntries([...root.children].map((i) => [i.type, i.getBoundingClientRect()]));
    host.remove();
    return sizes;
  }
  // WCAG 2.5.8 inline exception: a link in a sentence or block of text. Its block must hold real words beyond
  // its links (at least two, and at least one per link), so a row of crowded links is not exempt.
  function inSentence(el) {
    if (el.tagName !== 'A' || css(el).display !== 'inline') return false;
    let block = parentOf(el);
    while (block && /^(inline|contents)/.test(css(block).display)) block = parentOf(block);
    if (!block) return false;
    let words = 0;
    let links = 0;
    const walk = (node) => {
      for (const child of node.childNodes) {
        if (child.nodeType === 3) words += (child.textContent.match(/[\p{L}\p{N}]+/gu) || []).length;
        else if (child.nodeType === 1 && child.matches(CONTROL)) links += 1;
        else if (child.nodeType === 1) walk(child);
      }
    };
    walk(block);
    return words >= Math.max(2, links);
  }
  const undersized = (r) => r.width < 24 || r.height < 24;
  // Rounded, so a 43.6px box laid out from a 44px design is not flagged for sub-pixel rounding.
  const CHILD_TARGET = 44;
  const childSized = (r) => Math.round(r.width) >= CHILD_TARGET && Math.round(r.height) >= CHILD_TARGET;
  // Running text: an element holding a sentence of its own (five words or more), not a label or a control.
  const MIN_SENTENCE_WORDS = 5;
  const OLDER_ADULT_TEXT_PX = 16;
  const sentenceWords = (el) => (ownText(el).match(/[\p{L}\p{N}]+/gu) || []).length;
  const centre = (r) => [r.left + r.width / 2, r.top + r.height / 2];
  // WCAG 2.5.8 spacing exception: a 24px circle centred on the target intersects no other target, and no
  // other undersized target's circle. Two such circles intersect when their centres are under 24px apart.
  function crowded(el, r, others) {
    const [cx, cy] = centre(r);
    return others.some(([other, o]) => {
      if (other === el || other.contains(el) || el.contains(other)) return false;
      const dx = Math.max(o.left - cx, 0, cx - o.right);
      const dy = Math.max(o.top - cy, 0, cy - o.bottom);
      if (Math.hypot(dx, dy) < 12) return true;
      if (!undersized(o)) return false;
      const [ox, oy] = centre(o);
      return Math.hypot(ox - cx, oy - cy) < 24;
    });
  }
  // Visually hidden until focused (a skip link): judged by its size when it has focus.
  const hiddenUntilFocus = (r) => r.width <= 1 || r.height <= 1 || r.right <= 0 || r.bottom <= 0;

  // Text cut off at the right by overflow hidden or clip (on the page or a box) or by a fixed box wider than the
  // viewport. Ellipsis truncation is deliberate, and a visually hidden (1px, clipped) box is for screen readers.
  function clippedText(width, elements) {
    const cut = [];
    for (const el of elements) {
      if (view(el) !== window || NEVER_TEXT.has(el.tagName) || !visible(el) || !ownText(el)) continue;
      let bound = width;
      let skip = false;
      for (let n = el; n && n !== document.documentElement && n !== document.body; n = parentOf(n)) {
        const s = css(n);
        if (s.textOverflow === 'ellipsis' || s.clip !== 'auto' || s.clipPath !== 'none') skip = true;
        if (['hidden', 'clip'].includes(s.overflowX)) {
          const r = n.getBoundingClientRect();
          if (r.width <= 2 || r.height <= 2) skip = true;
          bound = Math.min(bound, r.left + n.clientLeft + n.clientWidth);
        }
      }
      if (skip) continue;
      for (const node of el.childNodes) {
        if (node.nodeType !== 3 || !node.textContent.trim()) continue;
        const range = document.createRange();
        range.selectNodeContents(node);
        if ([...range.getClientRects()].some((r) => r.left < bound - 1 && r.right > bound + 1)) {
          cut.push(el);
          break;
        }
      }
    }
    return cut;
  }

  function deepActive() {
    let el = document.activeElement;
    for (;;) {
      if (el && el.shadowRoot && el.shadowRoot.activeElement) {
        el = el.shadowRoot.activeElement;
        continue;
      }
      if (el && (el.tagName === 'IFRAME' || el.tagName === 'FRAME')) {
        const inner = frameDoc(el)?.activeElement;
        if (inner && inner !== inner.ownerDocument.body) {
          el = inner;
          continue;
        }
      }
      return el;
    }
  }

  const state = { reachable: [], controls: [], hidden: [], targets: [], focused: [], under13: false };

  window.__ux = {
    inspect({ width, checkReflow, under13, olderAdults }) {
      state.under13 = !!under13;
      const findings = [];
      const add = (kind, el, message, extra = {}) => findings.push({ kind, selector: el ? selector(el) : '', message, ...extra });
      const elements = allElements();

      // (c) horizontal overflow, WCAG 1.4.10 reflow.
      if (checkReflow) {
        const scroll = document.scrollingElement.scrollWidth;
        if (scroll > width + 1) {
          const wide = elements
            .filter((el) => view(el) === window && el.getBoundingClientRect().right > width + 1 && visible(el))
            .slice(0, 3);
          add('overflow', wide[0] || null, `page is ${scroll}px wide in a ${width}px viewport (WCAG 1.4.10)`, { wide: wide.map(selector) });
        } else {
          const cut = clippedText(width, elements);
          if (cut.length) {
            add('overflow', cut[0], `text is cut off at the right edge in a ${width}px viewport (${cut.length} elements; overflow hidden or a fixed box wider than the screen; WCAG 1.4.10)`, { wide: cut.slice(0, 3).map(selector) });
          }
        }
      }

      // (d) contrast
      const measured = contrast('light');
      findings.push(...measured.findings);
      const unmeasured = measured.unmeasured;
      for (const frame of crossOrigin) {
        unmeasured.push({ selector: selector(frame), reason: 'a cross-origin frame: its content is not inspected', text: frame.getAttribute('src') || '' });
      }

      // (e) images without a text alternative (alt="" is decorative and fine)
      for (const img of elements) {
        const role = roleOf(img);
        if (img.tagName === 'IMG' && !img.hasAttribute('alt')) {
          if (['presentation', 'none'].includes(role) || img.getAttribute('aria-label') || img.getAttribute('aria-labelledby')) continue;
          add('img-alt', img, `<img> has no alt attribute (${img.getAttribute('src') || 'no src'}); use alt="" if decorative`);
        } else if (role === 'img' && img.tagName !== 'IMG' && !closestDeep(img, '[aria-hidden="true"]') && !accessibleName(img)) {
          add('img-alt', img, `<${img.tagName.toLowerCase()} role="img"> has no accessible name (aria-label or aria-labelledby)`);
        }
      }

      // (f) accessible names, (h) target size, and the controls the keyboard pass must reach
      const controls = elements.filter(
        (el) => el.matches(CONTROL) || (el.hasAttribute('tabindex') && el.tabIndex >= 0 && !NEVER_TEXT.has(el.tagName))
      );
      const shown = controls.filter(visible);
      const live = (el) =>
        !el.matches(':disabled') &&
        el.getAttribute('aria-disabled') !== 'true' &&
        !closestDeep(el, '[inert]') &&
        !closestDeep(el, '[aria-hidden="true"]');
      state.controls = controls;
      state.reachable = shown.filter(live);
      for (const el of shown) {
        if (closestDeep(el, '[aria-hidden="true"]') || el.isContentEditable && !roleOf(el)) continue;
        if (!accessibleName(el)) {
          add('no-name', el, `<${el.tagName.toLowerCase()}> has no accessible name (visible text, aria-label, aria-labelledby, a shown label, alt or title)`);
        }
      }
      const toggles = userAgentToggleSizes();
      // A disabled or inert control is not a target: nothing happens when it is pressed.
      const rects = state.reachable.map((el) => [el, topRect(el)]);
      state.targets = state.reachable;
      state.hidden = [];
      for (const [el, r] of rects) {
        if (hiddenUntilFocus(r)) {
          state.hidden.push(el);
          continue;
        }
        if (under13 && !childSized(r) && !inSentence(el)) {
          add('target-size-child', el, `${Math.round(r.width)}x${Math.round(r.height)}px, under 44x44, the smallest target for an audience that includes children under 13`);
        }
        if (!undersized(r) || inSentence(el)) continue;
        const ua = el.tagName === 'INPUT' ? toggles[el.type] : null;
        if (ua && css(el).appearance !== 'none' && Math.abs(r.width - ua.width) < 0.5 && Math.abs(r.height - ua.height) < 0.5) continue;
        if (!crowded(el, r, rects)) continue;
        add('target-size', el, `${Math.round(r.width)}x${Math.round(r.height)}px, under 24x24, and its 24px circle meets another target or another small target's circle (WCAG 2.5.8)`);
      }

      // (i) body text for older adults
      if (olderAdults) {
        for (const el of elements) {
          if (NEVER_TEXT.has(el.tagName) || sentenceWords(el) < MIN_SENTENCE_WORDS || !visible(el)) continue;
          if (closestDeep(el, 'button, select, textarea, [role~=button]')) continue;
          const px = parseFloat(css(el).fontSize);
          if (px < OLDER_ADULT_TEXT_PX) add('body-text-size', el, `body text is ${px}px, under ${OLDER_ADULT_TEXT_PX}px, the smallest for an audience that includes people aged 65 or over`);
        }
      }

      // (g) title and language
      if (!document.title.trim()) add('title', null, 'document has no <title> text');
      if (!(document.documentElement.getAttribute('lang') || '').trim()) add('lang', null, '<html> has no lang attribute');
      return { findings, unmeasured };
    },

    contrast,
    stylesDarkScheme,

    // One Tab press per control, plus spare for the page's own stops and for what cross-origin frames hold.
    tabBudget: () => state.controls.length + 5 + 10 * crossOrigin.length,

    // After a Tab press: remember the newly focused element and where to look for its focus indicator.
    record() {
      const el = deepActive();
      if (!el || ['BODY', 'HTML'].includes(el.tagName) || state.focused.some((f) => f.el === el)) return null;
      // A frame takes focus on its way in; what counts is the control inside it, and a cross-origin frame's
      // controls cannot be seen from here (they are listed as not measured).
      if (el.tagName === 'IFRAME' || el.tagName === 'FRAME') return null;
      const scrolls = [];
      for (let n = el; n; n = parentOf(n)) scrolls.push([n, n.scrollLeft, n.scrollTop]);
      // An image map area has no box of its own; its focus ring is drawn on the image.
      const map = el.tagName === 'AREA' ? el.closest('map') : null;
      const image = map && map.name ? el.getRootNode().querySelector(`img[usemap="#${CSS.escape(map.name)}"]`) : null;
      const r = topRect(image || el);
      const entry = { el, scrolls, offscreen: false, small: null, smallForChild: null };
      state.focused.push(entry);
      if (state.hidden.includes(el)) {
        const others = state.targets.filter((t) => t !== el).map((t) => [t, topRect(t)]);
        if (state.under13 && !childSized(r)) entry.smallForChild = `${Math.round(r.width)}x${Math.round(r.height)}px when it has focus`;
        if (!undersized(r)) {
          // Big enough once focused; if it is still off the screen, the focus check says so.
        } else if (hiddenUntilFocus(r)) entry.small = `${Math.round(r.width)}x${Math.round(r.height)}px even with focus`;
        else if (crowded(el, r, others)) entry.small = `${Math.round(r.width)}x${Math.round(r.height)}px when it has focus`;
      }
      const left = Math.max(0, r.left - FOCUS_PAD);
      const top = Math.max(0, r.top - FOCUS_PAD);
      const right = Math.min(innerWidth, r.right + FOCUS_PAD);
      const bottom = Math.min(innerHeight, r.bottom + FOCUS_PAD);
      // Off the screen even after the browser scrolled it into view: there is nothing to see.
      if (right - left < 1 || bottom - top < 1 || r.right <= 0 || r.bottom <= 0 || r.left >= innerWidth || r.top >= innerHeight) {
        entry.offscreen = true;
        return { index: state.focused.length - 1, clip: null };
      }
      return { index: state.focused.length - 1, clip: { x: left, y: top, width: right - left, height: bottom - top } };
    },
    blurAll() {
      for (let el = deepActive(); el && el.blur; el = el === document.activeElement ? null : document.activeElement) el.blur();
      if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
    },
    // Put back the scroll positions the element had when it was focused, so its region shows the same content.
    restore(index) {
      for (const [n, left, top] of state.focused[index].scrolls) {
        n.scrollLeft = left;
        n.scrollTop = top;
      }
    },
    finish(changed) {
      const findings = [];
      const focused = state.focused.map((f) => f.el);
      const reached = (el) => focused.some((f) => f === el || el.contains(f));
      for (const el of state.reachable) {
        let group = [el];
        // Tab visits one radio button per group, so a group is reached when any member is.
        if (el.tagName === 'INPUT' && el.type === 'radio' && el.name) {
          group = [...el.getRootNode().querySelectorAll('input[type=radio]')].filter((r) => r.name === el.name && r.form === el.form);
        }
        const container = ITEM_CONTAINERS[roleOf(el)];
        const box = container ? closestDeep(parentOf(el), container.split(', ').map((r) => `[role~=${r}]`).join(', ')) : null;
        if (box) group = [box];
        if (group.some(reached)) continue;
        findings.push({
          kind: 'not-keyboard-reachable',
          selector: selector(el),
          message: `<${el.tagName.toLowerCase()}${roleOf(el) ? ` role="${roleOf(el)}"` : ''}> never received focus from Tab (WCAG 2.1.1); use a native control or add tabindex="0"`,
        });
      }
      state.focused.forEach((entry, i) => {
        const tag = `<${entry.el.tagName.toLowerCase()}>`;
        if (entry.small) {
          findings.push({ kind: 'target-size', selector: selector(entry.el), message: `${tag} is hidden until focused and ${entry.small}, under 24x24 (WCAG 2.5.8)` });
        }
        if (entry.smallForChild) {
          findings.push({ kind: 'target-size-child', selector: selector(entry.el), message: `${tag} is hidden until focused and ${entry.smallForChild}, under 44x44, the smallest target for an audience that includes children under 13` });
        }
        if (changed[i]) return;
        findings.push({
          kind: 'no-visible-focus',
          selector: selector(entry.el),
          message: entry.offscreen
            ? `${tag} is off the screen when it has focus, so its focus cannot be seen (WCAG 2.4.7)`
            : `${tag} looks the same focused and not: nothing within ${FOCUS_PAD}px of it changes (WCAG 2.4.7)`,
        });
      });
      return findings;
    },

    // The visible text, one string per block (an element that is not laid out inline), in document order.
    text() {
      const blocks = new Map();
      const shown = (el) => {
        let n = el;
        while (n && css(n).display === 'contents') n = parentOf(n);
        return !n || n.checkVisibility({ visibilityProperty: true, opacityProperty: true });
      };
      const blockOf = (el) => {
        let n = el;
        while (parentOf(n) && /^(inline|contents)/.test(css(n).display)) n = parentOf(n);
        return n;
      };
      const walk = (node) => {
        const children = node.shadowRoot ? [...node.shadowRoot.childNodes, ...node.childNodes] : [...node.childNodes];
        for (const child of children) {
          if (child.nodeType === 3) {
            const parent = parentOf(child);
            const text = child.textContent.replace(/\s+/g, ' ').trim();
            if (!text || !parent || NEVER_TEXT.has(parent.tagName) || !shown(parent)) continue;
            const block = blockOf(parent);
            if (!blocks.has(block)) blocks.set(block, []);
            blocks.get(block).push(text);
          } else if (child.nodeType === 1) {
            // aria-hidden marks decoration and duplicates, which a reader skips too.
            if (NEVER_TEXT.has(child.tagName) || child.getAttribute('aria-hidden') === 'true') continue;
            const doc = child.tagName === 'IFRAME' ? frameDoc(child) : null;
            walk(doc && doc.body ? doc.body : child);
          }
        }
      };
      walk(document.body || document.documentElement);
      return [...blocks.values()].map((parts) => parts.join(' '));
    },
  };
}

// Tab from the page start. For each newly focused element, its region (padded) is captured focused, then again
// with nothing focused and the same scroll positions; a focus indicator is visible when the two differ. That
// counts any visible change, an outline, an underline, a ring on the parent, a colour, and nothing else.
async function keyboardFindings(tab) {
  // Without this a focus style that transitions in would be read half way.
  await tab.addStyleTag({ content: '*, *::before, *::after { transition: none !important; animation: none !important; }' });
  const presses = await tab.evaluate(() => window.__ux.tabBudget());
  const shots = [];
  for (let press = 0; press < presses; press++) {
    await tab.keyboard.press('Tab');
    const got = await tab.evaluate(() => window.__ux.record());
    if (got) shots[got.index] = { clip: got.clip, focused: got.clip ? await tab.screenshot({ clip: got.clip, animations: 'disabled' }) : null };
  }
  await tab.evaluate(() => window.__ux.blurAll());
  const changed = [];
  for (let i = 0; i < shots.length; i++) {
    if (!shots[i]?.focused) {
      changed.push(false);
      continue;
    }
    await tab.evaluate((index) => window.__ux.restore(index), i);
    const plain = await tab.screenshot({ clip: shots[i].clip, animations: 'disabled' });
    changed.push(!plain.equals(shots[i].focused));
  }
  return tab.evaluate((flags) => window.__ux.finish(flags), changed);
}

// Console errors are caught up to a second after the network goes quiet (at most 5 s after load), not later.
async function settle(tab) {
  await tab.waitForLoadState('networkidle', { timeout: 5000 }).catch(() => {});
  await tab.waitForTimeout(1000);
}

const slug = (s) => s.replace(/^[a-z]+:\/\//, '').replace(/[^A-Za-z0-9._-]+/g, '-').replace(/^-+|-+$/g, '');
// Screenshot names come from the page's path; two pages whose paths slug alike (a/index.html, a-index.html) get
// a suffix rather than overwriting each other's screenshots.
const usedNames = new Set();
function shotName(name) {
  let candidate = slug(name) || 'page';
  for (let n = 2; usedNames.has(candidate); n++) candidate = `${slug(name)}~${n}`;
  usedNames.add(candidate);
  return candidate;
}

let browser;
try {
  browser = await chromium.launch({ headless: true, args: ['--allow-file-access-from-files'] });
} catch (error) {
  console.log(`browser could not start: ${String(error.message).split('\n')[0]}`);
  process.exit(3);
}

const area = (v) => v.split('x').map(Number);

async function textMode() {
  const [width, height] = area(config.viewport);
  const pages = [];
  for (const page of config.pages) {
    if (Date.now() > deadline) {
      pages.push({ name: page.name, error: 'not rendered: the time budget ran out' });
      continue;
    }
    const context = await browser.newContext({ viewport: { width, height } });
    const tab = await context.newPage();
    try {
      await tab.goto(page.url, { waitUntil: 'load', timeout: 20000 });
      await settle(tab);
      await tab.evaluate(installUx);
      pages.push({ name: page.name, blocks: await tab.evaluate(() => window.__ux.text()) });
    } catch (error) {
      pages.push({ name: page.name, error: String(error.message).split('\n')[0] });
    }
    await context.close();
  }
  return { pages };
}

async function inspectMode() {
  fs.mkdirSync(config.outDir, { recursive: true });
  const results = [];
  const screenshots = [];
  const darkPages = [];
  const largest = [...config.viewports].sort((a, b) => area(a)[0] - area(b)[0] || area(a)[1] - area(b)[1]).at(-1);
  const smallestWidth = Math.min(...config.viewports.map((v) => area(v)[0]));
  const reflowWidths = [...new Set(config.viewports.map((v) => area(v)[0]))]
    .filter((w) => w === smallestWidth || w <= 320)
    .sort((a, b) => a - b);
  for (const page of config.pages) {
    const base = shotName(page.name);
    for (const viewport of config.viewports) {
      const [width, height] = area(viewport);
      const label = `${page.name}@${viewport}`;
      const result = { page: page.name, viewport, findings: [], unmeasured: [] };
      results.push(result);
      const add = (kind, message) => result.findings.push({ kind, selector: '', message });
      if (Date.now() > deadline) {
        add('render', `${label} was not rendered: the ${config.budgetSeconds}s time budget ran out; declare fewer pages or viewports`);
        continue;
      }
      const context = await browser.newContext({ viewport: { width, height }, colorScheme: 'light' });
      const tab = await context.newPage();
      tab.on('console', (m) => {
        // A failed load is also logged to the console; the request check reports it once, with the URL.
        if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) add('console', m.text());
      });
      tab.on('pageerror', (e) => add('console', `uncaught: ${e.message}`));
      tab.on('requestfailed', (r) => {
        const why = r.failure()?.errorText || 'failed';
        if (!r.url().startsWith('data:') && why !== 'net::ERR_ABORTED') add('request', `${r.url()}: ${why}`);
      });
      tab.on('response', (r) => {
        if (r.status() >= 400) add('request', `${r.url()}: HTTP ${r.status()}`);
      });
      try {
        await tab.goto(page.url, { waitUntil: 'load', timeout: 20000 });
        await settle(tab);
        await tab.evaluate(installUx);
        const inspected = await tab.evaluate((args) => window.__ux.inspect(args), { width, checkReflow: reflowWidths.includes(width), under13: !!config.under13, olderAdults: !!config.olderAdults });
        result.findings.push(...inspected.findings);
        result.unmeasured = inspected.unmeasured;
        const file = path.join(config.outDir, `${base}@${viewport}.png`);
        await tab.screenshot({ path: file, fullPage: true });
        screenshots.push(file);
        if (await tab.evaluate(() => window.__ux.stylesDarkScheme())) {
          await tab.emulateMedia({ colorScheme: 'dark' });
          const dark = await tab.evaluate(() => window.__ux.contrast('dark'));
          // Only what the dark scheme changes: text that fails the same way in both is reported once.
          const light = new Set(inspected.findings.filter((f) => f.kind === 'contrast').map((f) => `${f.selector}|${f.ratio.toFixed(2)}`));
          result.findings.push(...dark.findings.filter((f) => !light.has(`${f.selector}|${f.ratio.toFixed(2)}`)));
          const darkFile = path.join(config.outDir, `${base}@${viewport}-dark.png`);
          await tab.screenshot({ path: darkFile, fullPage: true });
          screenshots.push(darkFile);
          if (!darkPages.includes(page.name)) darkPages.push(page.name);
          await tab.emulateMedia({ colorScheme: 'light' });
        }
        if (viewport === largest) result.findings.push(...(await keyboardFindings(tab)));
      } catch (error) {
        add('render', `${label} could not be rendered: ${String(error.message).split('\n')[0]}`);
      }
      await context.close();
    }
  }
  return { results, screenshots, keyboardViewport: largest, reflowWidths, darkPages };
}

const output = config.mode === 'text' ? await textMode() : await inspectMode();
await browser.close();
fs.writeFileSync(config.out, JSON.stringify(output, null, 2));
