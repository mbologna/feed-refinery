// The feeds editor: edit feeds.toml with the build's own checks, preview an
// RSS feed's filters against its live sources, and save through a pull
// request that the config repository's workflow merges once its check passes.
// The config and the published feeds are private: feeds.toml lives in a
// private repository, and the feeds Worker gives the build's health and the
// published feeds (/editor/health, /editor/feed) only to this token.
//
// Python (worker.js, bridge.py) validates, edits and previews; this file is
// the page, the GitHub API and the source relay. No build step, no framework.

const REPO = "mbologna/feeds-config";
const API = "https://api.github.com";
const CONFIG = "feeds.toml";
const TOKEN_KEY = "feeds-editor-token";
const HEALTH = new URL("health", location.href); // the build's health.json, with subscription URLs
const FEED = new URL("feed", location.href); // feed?file=: a published feed
const RELAY = new URL("fetch", location.href); // GET fetch?url=…, served by the feeds Worker
const POLL = 10_000;
const HEADER_LIMIT = 72;
const BODY_LIMIT = 100;
const TYPES = "feat|fix|perf|refactor|docs|style|test|build|ci|chore|revert";

const KIND_NAMES = { feed: "Hacker News feeds", digest: "Hacker News digests", rss: "RSS feeds" };
const LISTS = {
  include_categories: [
    "Include categories",
    "Keep only items with one of these categories (the whole category, any case).",
  ],
  exclude_categories: ["Exclude categories", "Drop items with any of these categories."],
  include_sections: ["Include sections", "Keep only items whose first category, the section, is one of these."],
  exclude_sections: ["Exclude sections", "Drop items whose first category is one of these."],
  include_titles: ["Include titles", "Keep only items whose title matches one of these regular expressions."],
  exclude_titles: ["Exclude titles", "Drop items whose title matches any of these. Start with (?i) to ignore case."],
  include_links: ["Include links", "Keep only items whose link matches one of these regular expressions."],
  exclude_links: ["Exclude links", "Drop items whose link matches any of these."],
  exclude_authors: ["Exclude authors", "Drop items by these authors (the whole name, any case)."],
  exclude_text: [
    "Exclude text",
    "Drop items whose title or description text matches any of these regular expressions, when first seen.",
  ],
};
const PATTERNS = new Set(["include_titles", "exclude_titles", "include_links", "exclude_links", "exclude_text"]);
const EXCLUDE_TITLES = { key: "exclude_titles", type: "list" };
const HN_PAYWALLED = {
  key: "exclude_paywalled",
  label: "Hide stories their page marks as paywalled (checked when the feed builds, not in this preview)",
  type: "flag",
  default: false,
};
const SITE_FIELD = {
  key: "site",
  label: "Website",
  type: "text",
  help: "The site the feed is about: readers show its icon. Default: Hacker News, or the first source's site",
};
const ICON_FIELD = { key: "icon", label: "Icon", type: "text", help: "Default: the website's /favicon.ico" };
const FILE = { key: "file", label: "File", type: "text", help: "Lowercase, ending in .xml" };
const TITLE = { key: "title", label: "Title", type: "text" };
const FIELDS = {
  feed: [
    FILE,
    TITLE,
    {
      key: "tags",
      label: "Algolia tags",
      type: "text",
      help: "front_page, story, show_hn…; comma-separated tags must all match",
    },
    { key: "min_points", label: "Minimum points", type: "number", default: 0 },
    { key: "min_comments", label: "Minimum comments", type: "number", default: 0 },
    { key: "window_hours", label: "Window (hours)", type: "number", default: 48 },
    { key: "max_items", label: "Most items", type: "number", default: 150 },
    { key: "ranked", label: "Only stories in HN's current ranking", type: "flag", default: false },
    HN_PAYWALLED,
    EXCLUDE_TITLES,
    SITE_FIELD,
    ICON_FIELD,
  ],
  digest: [
    FILE,
    TITLE,
    { key: "top", label: "Stories per day", type: "number", default: 20 },
    { key: "days", label: "Days kept", type: "number", default: 7 },
    { key: "publish_hour", label: "Publish hour (UTC)", type: "number", default: 6 },
    HN_PAYWALLED,
    EXCLUDE_TITLES,
    SITE_FIELD,
    ICON_FIELD,
  ],
  rss: [
    FILE,
    TITLE,
    { key: "sources", label: "Sources", type: "sources" },
    {
      key: "json",
      label: "JSON source",
      type: "mapping",
      keys: ["items", "title", "link", "date", "date_format", "summary", "author", "image"],
      help:
        "Only for sources that are JSON, not RSS or Atom: the dotted path to the list of items, then each item's " +
        "fields. items, title and link are required; date_format is a strptime pattern such as %B %d, %Y.",
    },
    ...Object.keys(LISTS).map((key) => ({ key, type: "list" })),
    {
      key: "enrich",
      label: "Fetch each article's image and excerpt when the source lacks them or has only a teaser",
      type: "flag",
      default: true,
    },
    {
      key: "exclude_paywalled",
      label: "Hide articles their page marks as paywalled (checked when the feed builds, not in this preview)",
      type: "flag",
      default: false,
    },
    {
      key: "exclude_on_hacker_news",
      label: "Hide stories the Hacker News feeds already carried (checked when the feed builds, not in this preview)",
      type: "flag",
      default: false,
    },
    { key: "max_items", label: "Most items", type: "number", default: 100 },
    { key: "keep_days", label: "Days an item stays once no source lists it", type: "number", default: 7 },
    {
      key: "dedupe",
      label: "Duplicates",
      type: "choice",
      default: "link",
      choices: { link: "Same link or guid", "link+title": "Same link, guid or title" },
    },
    SITE_FIELD,
    ICON_FIELD,
  ],
};

const $ = (id) => document.getElementById(id);
const state = {
  token: null,
  login: "",
  original: null, // { text, blob, commit }
  base: null, // tables of the original feeds.toml, by kind
  tables: null, // by kind: [{ data, origin }], origin = index in base or null
  edits: [],
  current: null, // { text, diff, error } after the edits
  selected: null, // { kind, entry }
  health: null,
  bodies: new Map(), // source URL -> { data, content_type, url } or { error }
  relay: true,
  saving: false,
};

// Helpers.

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value == null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key === "value" || key === "checked") node[key] = value;
    else node.setAttribute(key, value === true ? "" : value);
  }
  node.append(...children.flat(Infinity).filter((c) => c != null && c !== false));
  return node;
}

const clone = (value) => structuredClone(value);
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const fold = (text) => text.trim().toLocaleLowerCase();
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const sourcesOf = (table) =>
  table ? (Array.isArray(table.sources) ? table.sources : table.source ? [table.source] : []) : [];

function ago(seconds) {
  if (!seconds) return "never";
  const elapsed = Math.max(0, Date.now() / 1000 - seconds);
  if (elapsed < 90) return "just now";
  if (elapsed < 5400) return `${Math.round(elapsed / 60)} min ago`;
  if (elapsed < 129600) return `${Math.round(elapsed / 3600)} h ago`;
  return `${Math.round(elapsed / 86400)} days ago`;
}

function debounce(fn, ms) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

function toBase64(text) {
  const bytes = new TextEncoder().encode(text);
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(binary);
}

function fromBase64(data) {
  const binary = atob(data.replace(/\s/g, ""));
  return new TextDecoder().decode(Uint8Array.from(binary, (c) => c.charCodeAt(0)));
}

function problem(message) {
  $("problem").textContent = message || "";
  $("problem").hidden = !message;
}

// Python, in the worker.

const worker = new Worker(new URL("worker.js", import.meta.url), { type: "module" });
const pending = new Map();
let calls = 0;
worker.onmessage = ({ data }) => {
  if (data.type === "status") $("runtime").textContent = data.text;
  else if (data.type === "ready") $("runtime").textContent = "Python ready";
  else if (data.type === "failed") {
    $("runtime").textContent = "Python failed to load";
    problem(`The editor's Python runtime didn't load: ${data.error}`);
  } else {
    const call = pending.get(data.id);
    pending.delete(data.id);
    if (data.error) call?.reject(new Error(data.error));
    else call?.resolve(data.result);
  }
};

worker.onerror = (event) => {
  $("runtime").textContent = "Python failed to load";
  problem(`The editor's Python worker didn't start: ${event.message || "blocked or missing"}`);
};
// A blocked WebAssembly compile or script leaves Pyodide waiting forever.
setTimeout(() => {
  if ($("runtime").textContent.startsWith("Loading") || $("runtime").textContent.startsWith("Installing")) {
    problem("Python hasn't loaded after two minutes: the browser console says what was blocked or failed.");
  }
}, 120_000);

function py(name, args) {
  const id = ++calls;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    worker.postMessage({ id, name, args });
  });
}

// GitHub.

class GitHubError extends Error {
  constructor(status, message) {
    super(message || `HTTP ${status}`);
    this.status = status;
  }
}

async function gh(path, { method = "GET", body, auth = true } = {}) {
  const headers = { Accept: "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28" };
  if (auth) headers.Authorization = `Bearer ${state.token}`;
  if (body) headers["Content-Type"] = "application/json";
  const response = await fetch(API + path, { method, headers, body: body && JSON.stringify(body), cache: "no-store" });
  const data = response.status === 204 ? null : await response.json().catch(() => null);
  if (!response.ok) throw new GitHubError(response.status, data?.message);
  return data;
}

// The feeds Worker's endpoints for this page, which take the same token.
function fromWorker(url) {
  return fetch(url, { headers: { Authorization: `Bearer ${state.token}` }, cache: "no-store" });
}

async function loadHealth() {
  return fromWorker(HEALTH).then(
    (r) => (r.ok ? r.json() : null),
    () => null,
  );
}

// Signing in and loading.

async function signIn(token) {
  state.token = token;
  const repo = await gh(`/repos/${REPO}`);
  if (!repo.permissions?.push)
    throw new Error(`This token can't write to ${REPO}: give it Contents and Pull requests read and write.`);
  localStorage.setItem(TOKEN_KEY, token);
  state.login = await gh("/user").then(
    (u) => u.login,
    () => "",
  );
  $("who").textContent = state.login ? `Signed in as ${state.login}` : "Signed in";
  $("account").hidden = false;
  $("sign-in").hidden = true;
  await loadConfig();
  $("app").hidden = false;
}

function signOut() {
  localStorage.removeItem(TOKEN_KEY);
  location.reload();
}

async function loadConfig() {
  const branch = await gh(`/repos/${REPO}/branches/main`);
  const commit = branch.commit.sha;
  const file = await gh(`/repos/${REPO}/contents/${CONFIG}?ref=${commit}`);
  const text = fromBase64(file.content);
  state.original = { text, blob: file.sha, commit };
  const loaded = await py("load", { text });
  state.base = loaded.tables;
  state.tables = Object.fromEntries(
    Object.entries(loaded.tables).map(([kind, list]) => [
      kind,
      list.map((data, origin) => ({ data: clone(data), origin })),
    ]),
  );
  state.edits = [];
  state.current = { text, diff: "", error: loaded.error };
  state.selected = null;
  state.health = await loadHealth();
  renderList();
  renderEditor();
  renderStatus();
}

// Edits: each change is one edit, applied by bridge.apply to the original
// text; the tables here mirror them for the forms.

function indexOf(kind, entry) {
  return state.tables[kind].indexOf(entry);
}

function edit(op) {
  const last = state.edits.at(-1);
  const sameKey = (a, b) =>
    a?.op === "set" && b.op === "set" && a.kind === b.kind && a.index === b.index && a.key === b.key;
  if (sameKey(last, op)) state.edits[state.edits.length - 1] = op;
  else state.edits.push(op);
  const list = state.tables[op.kind];
  if (op.op === "set") {
    if (op.value == null) delete list[op.index].data[op.key];
    else list[op.index].data[op.key] = clone(op.value);
  } else if (op.op === "add") list.push({ data: clone(op.table), origin: null });
  else if (op.op === "delete") list.splice(op.index, 1);
  reapply();
}

function set(kind, entry, key, value) {
  edit({ op: "set", kind, index: indexOf(kind, entry), key, value });
}

let applying = 0;
const reapply = debounce(async () => {
  const run = ++applying;
  const result = await py("apply", { text: state.original.text, edits: state.edits }).catch((e) => ({
    error: e.message,
    diff: "",
  }));
  if (run !== applying) return;
  state.current = result;
  renderStatus();
  renderList();
  if (state.selected?.kind === "rss" && state.bodies.size) refreshPreview();
}, 250);

function renderStatus() {
  const { diff, error } = state.current || {};
  problem(error ? `feeds.toml won't build: ${error}` : "");
  const changed = state.edits.length && diff;
  const lines = (diff || "").split("\n").filter((l) => /^[+-]/.test(l) && !/^(\+\+\+|---) [ab]\//.test(l)).length;
  $("changes").textContent = error
    ? "Fix the problem above to save."
    : changed
      ? `${plural(lines, "line")} changed in feeds.toml.`
      : "No changes.";
  $("review").disabled = !changed || !!error || state.saving;
}

// The feed list.

function healthOf(kind, file) {
  const h = state.health;
  if (!h) return null;
  return (kind === "feed" ? h.feeds : kind === "digest" ? h.digests : h.rss)?.[file] ?? null;
}

function statusBadge(kind, entry) {
  if (entry.origin == null) return el("span", { class: "badge new" }, "new");
  if (!same(state.base[kind][entry.origin], entry.data)) return el("span", { class: "badge changed" }, "changed");
  return null;
}

function healthLine(kind, file) {
  const h = healthOf(kind, file);
  if (!h) return el("span", { class: "health" }, "not published");
  if (kind === "digest") return el("span", { class: "health" }, plural(h.days, "day"));
  const parts = [plural(h.items, "item"), `${h.images} with images`];
  if (kind === "rss") parts.push(`last good fetch ${ago(h.last_ok)}`);
  return el(
    "span",
    { class: "health" },
    parts.join(" · "),
    kind === "rss" && !h.ok ? el("span", { class: "badge bad" }, "stale") : null,
    h.sources_failed?.length
      ? el(
          "span",
          { class: "badge warn", title: h.sources_failed.join("\n") },
          `${plural(h.sources_failed.length, "source")} failing`,
        )
      : null,
  );
}

function renderList() {
  const list = $("feed-list");
  const built = state.health?.built;
  list.replaceChildren(
    el("p", { class: "hint" }, built ? `Last build ${ago(built)}.` : "No build status."),
    ...Object.keys(KIND_NAMES).map((kind) =>
      el(
        "div",
        { class: "group" },
        el("h3", {}, KIND_NAMES[kind]),
        el(
          "ul",
          {},
          state.tables[kind].map((entry) =>
            el(
              "li",
              {},
              el(
                "button",
                {
                  type: "button",
                  class: "feed-button",
                  "aria-current": state.selected?.entry === entry ? "true" : null,
                  onclick: () => select(kind, entry),
                },
                el("span", {}, entry.data.title || "(untitled)", statusBadge(kind, entry)),
                el("span", { class: "file mono" }, entry.data.file || "(no file)"),
                healthLine(kind, entry.data.file),
              ),
            ),
          ),
        ),
      ),
    ),
  );
}

function select(kind, entry) {
  if (state.selected?.entry !== entry) state.bodies.clear();
  state.selected = { kind, entry };
  renderList();
  renderEditor();
  $("editor").querySelector("input, select, button")?.focus();
}

function uniqueFile(stem) {
  const used = new Set(
    Object.values(state.tables)
      .flat()
      .map((e) => e.data.file),
  );
  for (let n = 1; ; n++) {
    const file = n === 1 ? `${stem}.xml` : `${stem}-${n}.xml`;
    if (!used.has(file)) return file;
  }
}

function addTable(kind, table) {
  edit({ op: "add", kind, table });
  select(kind, state.tables[kind].at(-1));
}

const NEW = {
  feed: () => ({ file: uniqueFile("new"), title: "New feed", tags: "story" }),
  digest: () => ({ file: uniqueFile("new-digest"), title: "New digest" }),
  rss: () => ({ file: uniqueFile("new"), title: "New feed", source: "" }),
};

// The forms.

function renderEditor() {
  const section = $("editor");
  $("preview").hidden = true;
  if (!state.selected) {
    section.replaceChildren(el("p", { class: "hint" }, "Choose a feed to edit, or add one."));
    return;
  }
  const { kind, entry } = state.selected;
  const data = entry.data;
  const head = el(
    "div",
    { class: "form-head" },
    el("h2", {}, data.title || "(untitled)"),
    el(
      "button",
      {
        type: "button",
        onclick: () =>
          addTable(kind, {
            ...clone(data),
            file: uniqueFile(data.file.replace(/\.xml$/, "") + "-copy"),
            title: `${data.title} (copy)`,
          }),
      },
      "Duplicate",
    ),
    el(
      "button",
      {
        type: "button",
        onclick: () => {
          if (!confirm(`Delete ${data.file}? It stops being published once saved.`)) return;
          edit({ op: "delete", kind, index: indexOf(kind, entry) });
          state.selected = null;
          renderList();
          renderEditor();
        },
      },
      "Delete",
    ),
  );
  const fields = el(
    "div",
    { class: "fields" },
    FIELDS[kind].map((field) => renderField(kind, entry, field)),
  );
  const url = state.health?.urls?.[data.file];
  const subscribe = url
    ? el(
        "div",
        { class: "field wide" },
        el("label", { for: "subscribe-url" }, "Subscription URL"),
        el("input", {
          id: "subscribe-url",
          type: "text",
          class: "mono",
          readonly: true,
          value: url,
          onfocus: (e) => e.target.select(),
        }),
        el("small", {}, "Private: whoever has it can read the feed."),
      )
    : null;
  section.replaceChildren(head, subscribe, fields, kind === "rss" ? renderTester(entry) : null);
  if (kind === "rss") renderRssPreview(entry);
  else renderPublished(entry);
}

// A flag or choice set back to its default leaves no key behind, unless the
// original table spelled it out.
function setOrDefault(kind, entry, field, value) {
  const original = entry.origin == null ? {} : state.base[kind][entry.origin];
  set(kind, entry, field.key, value === field.default && !(field.key in original) ? null : value);
}

function fieldId(key) {
  return `field-${key}`;
}

function renderField(kind, entry, field) {
  const data = entry.data;
  const id = fieldId(field.key);
  const help = field.help ? el("small", { id: `${id}-help` }, field.help) : null;
  if (field.type === "text") {
    return el(
      "div",
      { class: "field" },
      el("label", { for: id }, field.label),
      el("input", {
        id,
        type: "text",
        value: data[field.key] ?? "",
        "aria-describedby": help ? `${id}-help` : null,
        oninput: (e) => {
          set(kind, entry, field.key, e.target.value);
          if (field.key === "title") $("editor").querySelector("h2").textContent = e.target.value || "(untitled)";
        },
      }),
      help,
    );
  }
  if (field.type === "number") {
    return el(
      "div",
      { class: "field" },
      el("label", { for: id }, field.label),
      el("input", {
        id,
        type: "number",
        inputmode: "numeric",
        placeholder: String(field.default),
        value: data[field.key] ?? "",
        oninput: (e) => set(kind, entry, field.key, e.target.value === "" ? null : Number(e.target.value)),
      }),
      el("small", {}, `Default ${field.default}`),
    );
  }
  if (field.type === "flag") {
    return el(
      "div",
      { class: "field inline wide" },
      el("input", {
        id,
        type: "checkbox",
        checked: data[field.key] ?? field.default,
        onchange: (e) => setOrDefault(kind, entry, field, e.target.checked),
      }),
      el("label", { for: id }, field.label),
    );
  }
  if (field.type === "choice") {
    return el(
      "div",
      { class: "field" },
      el("label", { for: id }, field.label),
      el(
        "select",
        { id, onchange: (e) => setOrDefault(kind, entry, field, e.target.value) },
        Object.entries(field.choices).map(([value, label]) =>
          el("option", { value, selected: (data[field.key] ?? field.default) === value }, label),
        ),
      ),
    );
  }
  if (field.type === "sources") return renderSources(entry);
  if (field.type === "mapping") return renderMapping(kind, entry, field);
  return renderChips(kind, entry, field.key);
}

function renderMapping(kind, entry, field) {
  const values = entry.data[field.key] ?? {};
  const update = (key, text) => {
    const next = { ...(entry.data[field.key] ?? {}) };
    if (text.trim()) next[key] = text.trim();
    else delete next[key];
    set(kind, entry, field.key, Object.keys(next).length ? next : null);
  };
  return el(
    "fieldset",
    { class: "field" },
    el("legend", {}, field.label),
    el("small", {}, field.help),
    field.keys.map((key) => {
      const id = fieldId(`${field.key}-${key}`);
      return el(
        "div",
        { class: "field" },
        el("label", { for: id }, key),
        el("input", {
          id,
          type: "text",
          class: "mono",
          value: values[key] ?? "",
          oninput: (e) => update(key, e.target.value),
        }),
      );
    }),
  );
}

function renderSources(entry) {
  const urls = sourcesOf(entry.data);
  const update = (next) => {
    const data = entry.data;
    if ("source" in data && next.length <= 1) set("rss", entry, "source", next[0] ?? "");
    else if ("source" in data) {
      set("rss", entry, "source", null);
      set("rss", entry, "sources", next);
    } else set("rss", entry, "sources", next);
    renderEditor();
  };
  const input = el("input", {
    id: fieldId("sources"),
    type: "url",
    placeholder: "https://example.org/feed",
    "aria-label": "Source URL to add",
  });
  const add = () => {
    const url = input.value.trim();
    if (url && !urls.includes(url)) update([...urls.filter(Boolean), url]);
  };
  return el(
    "fieldset",
    { class: "field wide" },
    el("legend", {}, "Sources"),
    el("small", {}, "RSS or Atom feed URLs, merged into one feed with duplicates removed."),
    el(
      "ul",
      { class: "sources" },
      urls.map((url) =>
        el(
          "li",
          {},
          el("code", {}, url || "(empty)"),
          el(
            "button",
            {
              type: "button",
              "aria-label": `Remove source ${url}`,
              onclick: () => update(urls.filter((u) => u !== url)),
            },
            "Remove",
          ),
        ),
      ),
    ),
    el("div", { class: "adder" }, input, el("button", { type: "button", onclick: add }, "Add source")),
  );
}

function renderChips(kind, entry, key) {
  const [label, help] = LISTS[key];
  const values = entry.data[key] ?? [];
  const id = fieldId(key);
  const update = (next) => {
    set(kind, entry, key, next.length ? next : null);
    renderEditor();
    $(id)?.focus();
  };
  const input = el("input", {
    id,
    type: "text",
    class: PATTERNS.has(key) ? "mono" : null,
    "aria-label": `${label}: new entry`,
    "aria-describedby": `${id}-help`,
    onkeydown: (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        add();
      }
    },
  });
  const add = () => {
    const value = input.value.trim();
    if (value && !values.includes(value)) update([...values, value]);
    else input.value = "";
  };
  return el(
    "fieldset",
    { class: "field" },
    el("legend", {}, label),
    el("small", { id: `${id}-help` }, help),
    el(
      "ul",
      { class: "chips", "aria-label": label },
      values.map((value) =>
        el(
          "li",
          { class: "chip", "data-key": key, "data-value": value },
          el("span", { class: PATTERNS.has(key) ? "mono" : null }, value),
          el(
            "button",
            {
              type: "button",
              "aria-label": `Remove ${value} from ${label.toLowerCase()}`,
              onclick: () => update(values.filter((v) => v !== value)),
            },
            "×",
          ),
        ),
      ),
    ),
    el(
      "div",
      { class: "adder" },
      input,
      el("button", { type: "button", onclick: add, "aria-label": `Add to ${label.toLowerCase()}` }, "Add"),
    ),
  );
}

function renderTester(entry) {
  const result = el("p", { class: "hint", "aria-live": "polite" });
  const run = debounce(async (text) => {
    const chips = [...$("editor").querySelectorAll(".chip[data-key]")].filter((c) => PATTERNS.has(c.dataset.key));
    for (const chip of chips) chip.classList.remove("match", "broken");
    if (!text) {
      result.textContent = "";
      return;
    }
    const matches = [];
    for (const key of PATTERNS) {
      const patterns = entry.data[key] ?? [];
      if (!patterns.length) continue;
      const tested = await py("test", { patterns, text });
      tested.forEach(({ pattern, match, error }) => {
        const chip = chips.find((c) => c.dataset.key === key && c.dataset.value === pattern);
        chip?.classList.toggle("match", match);
        chip?.classList.toggle("broken", !!error);
        if (match) matches.push(`${key} ${pattern}`);
        if (error) matches.push(`${key} ${pattern}: ${error}`);
      });
    }
    result.textContent = matches.length ? `Matches: ${matches.join("; ")}` : "No pattern matches.";
  }, 200);
  return el(
    "div",
    { class: "field tester" },
    el("label", { for: "tester" }, "Try the regular expressions on a title or link"),
    el("input", { id: "tester", type: "text", oninput: (e) => run(e.target.value.trim()) }),
    result,
  );
}

// Hacker News feeds: the published items. A live preview is left for later.

async function renderPublished(entry) {
  const body = $("preview-body");
  $("preview").hidden = false;
  $("preview-title").textContent = "Published now";
  const file = entry.data.file;
  body.replaceChildren(el("p", { class: "hint" }, "Loading…"));
  const response = await fromWorker(`${FEED}?file=${encodeURIComponent(file)}`).catch(() => null);
  if (state.selected?.entry !== entry) return;
  if (!response?.ok) {
    body.replaceChildren(el("p", { class: "hint" }, "Not published yet."));
    return;
  }
  const doc = new DOMParser().parseFromString(await response.text(), "application/xml");
  const items = [...doc.querySelectorAll("item")];
  body.replaceChildren(
    el("p", {}, `${plural(items.length, "item")} in ${file}. Changes show here after the next build.`),
    el(
      "ol",
      {},
      items.slice(0, 30).map((item) => {
        const date = item.querySelector("pubDate")?.textContent;
        return el(
          "li",
          {},
          el(
            "a",
            { href: item.querySelector("link")?.textContent || null },
            item.querySelector("title")?.textContent || "(untitled)",
          ),
          date ? el("small", { class: "hint" }, ` · ${new Date(date).toLocaleString()}`) : null,
        );
      }),
    ),
  );
}

// RSS feeds: the live preview, through the relay.

class RelayUnavailable extends Error {}

async function fetchSource(url) {
  if (state.bodies.has(url)) return;
  let got;
  try {
    const response = await fetch(`${RELAY}?url=${encodeURIComponent(url)}`, {
      headers: { Authorization: `Bearer ${state.token}` },
      cache: "no-store",
    });
    if (response.status === 404) throw new RelayUnavailable();
    if (response.ok) {
      got = {
        data: await response.arrayBuffer(),
        content_type: response.headers.get("Content-Type") || "",
        url: response.headers.get("X-Final-URL") || url,
      };
    } else if (response.status === 401 || response.status === 403) {
      got = {
        error: `the relay refused the token (HTTP ${response.status}): ${(await response.text()).slice(0, 200)}`,
      };
    } else {
      got = { error: (await response.text()).slice(0, 300) || `HTTP ${response.status}` };
    }
  } catch (error) {
    if (error instanceof RelayUnavailable) throw error;
    got = { error: `the relay didn't answer: ${error.message}` };
  }
  state.bodies.set(url, got);
}

function bodiesFor(table) {
  const bodies = {};
  for (const url of sourcesOf(table)) {
    const got = state.bodies.get(url);
    if (got)
      bodies[url] = got.error
        ? { error: got.error }
        : { data: got.data.slice(0), content_type: got.content_type, url: got.url };
  }
  return bodies;
}

function renderRssPreview(entry) {
  $("preview").hidden = false;
  $("preview-title").textContent = "Preview";
  const button = el(
    "button",
    { type: "button", class: "primary", onclick: () => loadPreview(entry, button) },
    state.bodies.size ? "Fetch the sources again" : "Fetch the sources",
  );
  $("preview-body").replaceChildren(
    el(
      "p",
      { class: "hint" },
      state.relay ? "Fetch the sources to see what the filters keep, drop and merge." : "Preview relay unavailable.",
    ),
    button,
    el("div", { id: "preview-result" }),
  );
  if (state.bodies.size) refreshPreview();
}

async function loadPreview(entry, button) {
  button.disabled = true;
  const result = $("preview-result");
  result.replaceChildren(el("p", { class: "hint" }, "Fetching…"));
  state.bodies.clear();
  const base = entry.origin == null ? null : state.base.rss[entry.origin];
  const urls = [...new Set([...sourcesOf(entry.data), ...sourcesOf(base)].filter(Boolean))];
  try {
    await Promise.all(urls.map(fetchSource));
    state.relay = true;
    await refreshPreview();
  } catch (error) {
    state.bodies.clear();
    const unavailable = error instanceof RelayUnavailable;
    state.relay = !unavailable;
    result.replaceChildren(
      el(
        "p",
        { class: "error" },
        unavailable
          ? "Preview relay unavailable: this site doesn't serve /editor/fetch yet, so the sources can't be fetched. Editing and saving still work."
          : `The preview failed: ${error.message}`,
      ),
    );
  } finally {
    button.disabled = false;
  }
}

let previews = 0;
async function refreshPreview() {
  const selected = state.selected;
  if (selected?.kind !== "rss" || !state.bodies.size) return;
  const entry = selected.entry;
  const run = ++previews;
  const base = entry.origin == null ? null : state.base.rss[entry.origin];
  try {
    const missing = sourcesOf(entry.data).filter((url) => url && !state.bodies.has(url));
    await Promise.all(missing.map(fetchSource));
    const [after, before] = await Promise.all([
      py("preview", { table: entry.data, bodies: bodiesFor(entry.data) }),
      base ? py("preview", { table: base, bodies: bodiesFor(base) }) : null,
    ]);
    if (run !== previews || state.selected?.entry !== entry) return;
    $("preview-result")?.replaceChildren(...renderResult(entry, after, before));
  } catch (error) {
    if (run === previews)
      $("preview-result")?.replaceChildren(el("p", { class: "error" }, `The preview failed: ${error.message}`));
  }
}

function renderResult(entry, after, before) {
  if (after.error) return [el("p", { class: "error" }, `This feed won't build: ${after.error}`)];
  const was = (key) =>
    before && !before.error && before.counts[key] !== after.counts[key] ? ` (was ${before.counts[key]})` : "";
  const counts = el(
    "ul",
    { class: "counts", "aria-label": "Counts" },
    el("li", {}, el("b", {}, String(after.counts.listed)), " listed"),
    el(
      "li",
      { class: "dropped" },
      el("b", { "data-count": "dropped" }, String(after.counts.dropped)),
      ` dropped${was("dropped")}`,
    ),
    el(
      "li",
      { class: "merged" },
      el("b", { "data-count": "merged" }, String(after.counts.merged)),
      ` merged copies${was("merged")}`,
    ),
    el(
      "li",
      { class: "kept" },
      el("b", { "data-count": "published" }, String(after.counts.published)),
      ` published${was("published")}`,
    ),
  );
  const sources = el(
    "ul",
    {},
    Object.entries(after.sources).map(([url, s]) =>
      el(
        "li",
        {},
        el("code", {}, url),
        s.error ? el("span", { class: "error" }, ` ${s.error}`) : ` · ${plural(s.items, "item")}`,
      ),
    ),
  );
  const titles = Object.fromEntries(after.items.filter((i) => i.key && !i.merged).map((i) => [i.key, i.title]));
  const published = new Set(after.stories.map((s) => s.key));
  const verdict = (item) => {
    if (item.drop)
      return el(
        "span",
        { class: "dropped" },
        `dropped: ${item.drop.key}${item.drop.entry ? ` ${item.drop.entry}` : ""}`,
      );
    if (item.merged) return el("span", { class: "merged" }, `merged into “${titles[item.key] ?? item.key}”`);
    return published.has(item.key)
      ? el("span", { class: "kept" }, "published")
      : el("span", { class: "merged" }, "kept, beyond max_items");
  };
  const show = el(
    "select",
    { id: "show", onchange: (e) => filterRows(e.target.value) },
    ["all", "published", "dropped", "merged"].map((v) => el("option", { value: v }, v)),
  );
  const rows = after.items.map((item) =>
    el(
      "tr",
      { "data-state": item.drop ? "dropped" : item.merged ? "merged" : "published" },
      el(
        "td",
        {},
        item.link ? el("a", { href: item.link, target: "_blank", rel: "noopener" }, item.title) : item.title,
      ),
      el("td", {}, item.categories[0] ?? ""),
      el("td", { class: "verdict" }, verdict(item)),
      el("td", {}, el("small", {}, item.categories.slice(1).join(", "))),
    ),
  );
  const filterRows = (value) => rows.forEach((row) => (row.hidden = value !== "all" && row.dataset.state !== value));
  const groups = new Map();
  after.items.filter((i) => i.key).forEach((i) => groups.set(i.key, [...(groups.get(i.key) ?? []), i]));
  const mergedStories = [...groups.values()].filter((copies) => copies.length > 1);
  return [
    counts,
    sources,
    el(
      "div",
      { class: "facets" },
      renderFacets(entry, "sections", "exclude_sections", after),
      renderFacets(entry, "categories", "exclude_categories", after),
    ),
    el("div", { class: "toolbar" }, el("label", { for: "show" }, "Show"), show),
    el(
      "div",
      { class: "table-wrap" },
      el(
        "table",
        {},
        el(
          "thead",
          {},
          el(
            "tr",
            {},
            el("th", {}, "Item"),
            el("th", {}, "Section"),
            el("th", {}, "Verdict"),
            el("th", {}, "Other categories"),
          ),
        ),
        el("tbody", {}, rows),
      ),
    ),
    el(
      "details",
      {},
      el("summary", {}, `Merged stories (${mergedStories.length})`),
      el(
        "ul",
        {},
        mergedStories.map((copies) =>
          el(
            "li",
            {},
            copies[0].title,
            el(
              "small",
              { class: "hint" },
              ` · ${copies.length} copies from ${[...new Set(copies.map((c) => new URL(c.source).pathname))].join(", ")}`,
            ),
          ),
        ),
      ),
    ),
  ];
}

function renderFacets(entry, facet, key, after) {
  const excluded = new Set((entry.data[key] ?? []).map(fold));
  const toggle = (name) => {
    const values = entry.data[key] ?? [];
    const next = excluded.has(fold(name)) ? values.filter((v) => fold(v) !== fold(name)) : [...values, name];
    set("rss", entry, key, next.length ? next : null);
    renderEditor();
  };
  const what = facet === "sections" ? "section" : "category";
  return el(
    "div",
    {},
    el("h3", {}, facet === "sections" ? "Sections (first category)" : "Categories"),
    el("small", { class: "hint" }, `Click to add to ${key}, or take it out again.`),
    el(
      "ul",
      { class: "chips" },
      after.facets[facet].slice(0, 60).map((f) =>
        el(
          "li",
          {},
          el(
            "button",
            {
              type: "button",
              class: `facet${excluded.has(fold(f.name)) ? " dropped" : ""}`,
              "aria-pressed": excluded.has(fold(f.name)) ? "true" : "false",
              "aria-label": `${excluded.has(fold(f.name)) ? "Stop excluding" : "Exclude"} ${what} ${f.name}: ${plural(f.count, "item")}, ${f.passed} pass`,
              onclick: () => toggle(f.name),
            },
            f.name,
            el("span", { class: "n" }, ` ${f.count}/${f.passed}`),
          ),
        ),
      ),
    ),
  );
}

// Saving: a branch, a commit, a pull request that the config repository's
// workflow merges, then the build that merge dispatches.

function changes() {
  const parts = [];
  const used = new Set();
  for (const [kind, list] of Object.entries(state.tables)) {
    for (const entry of list) {
      if (entry.origin == null) {
        parts.push(`add ${entry.data.file}`);
        continue;
      }
      used.add(`${kind}:${entry.origin}`);
      const old = state.base[kind][entry.origin];
      const keys = [...new Set([...Object.keys(old), ...Object.keys(entry.data)])].filter(
        (k) => !same(old[k], entry.data[k]),
      );
      if (!keys.length) continue;
      const file = old.file;
      if (same(keys, ["file"])) parts.push(`rename ${file} to ${entry.data.file}`);
      else if (keys.length === 1) parts.push(describeKey(file, keys[0], old[keys[0]], entry.data[keys[0]]));
      else if (keys.length === 2) parts.push(`change ${file} ${keys[0]} and ${keys[1]}`);
      else parts.push(`change ${keys.length} settings of ${file}`);
    }
    state.base[kind].forEach((table, n) => used.has(`${kind}:${n}`) || parts.push(`remove ${table.file}`));
  }
  return parts;
}

function describeKey(file, key, old, value) {
  const names = (list) => (list.length > 2 ? `${list.length} entries` : list.map((v) => `"${v}"`).join(" and "));
  if (value === undefined) return `reset ${file} ${key} to its default`;
  if (Array.isArray(value) && (Array.isArray(old) || old === undefined)) {
    const added = value.filter((v) => !(old ?? []).includes(v));
    const removed = (old ?? []).filter((v) => !value.includes(v));
    if (added.length && !removed.length) return `add ${names(added)} to ${file} ${key}`;
    if (removed.length && !added.length) return `remove ${names(removed)} from ${file} ${key}`;
    return `change ${file} ${key}`;
  }
  return `set ${file} ${key} to ${typeof value === "string" ? `"${value}"` : value}`;
}

function suggestMessage() {
  const parts = changes();
  const files = [...new Set(parts.map((p) => p.match(/[a-z0-9][a-z0-9._-]*\.xml/)?.[0]).filter(Boolean))];
  const options = [
    parts.join(", "),
    files.length === 1 ? `change ${files[0]}` : `update ${files.join(", ")}`,
    `update ${plural(files.length, "feed")}`,
  ];
  return (
    options.map((d) => `chore(config): ${d}`).find((m) => m.length <= HEADER_LIMIT) ?? "chore(config): update the feeds"
  );
}

function checkMessage(message) {
  const [header, ...rest] = message.replace(/\s+$/, "").split("\n");
  const problems = [];
  const match = header.match(new RegExp(`^(${TYPES})(\\([a-z0-9-]+\\))?!?: (.*)$`));
  if (header.length > HEADER_LIMIT)
    problems.push(`The first line has ${header.length} characters; the limit is ${HEADER_LIMIT}.`);
  if (!match) problems.push("Start with a type and scope, like chore(config): …");
  else if (!match[3].trim()) problems.push("Say what changed after the colon.");
  else if (/^[A-Z]/.test(match[3])) problems.push("Start the description with a lowercase word.");
  if (header.endsWith(".")) problems.push("No full stop at the end of the first line.");
  if (rest.length && rest[0].trim() !== "") problems.push("Leave a blank line after the first line.");
  if (rest.some((line) => line.length > BODY_LIMIT)) problems.push(`Wrap the body at ${BODY_LIMIT} characters.`);
  return problems;
}

function renderDiff(diff) {
  return diff
    .split("\n")
    .filter((line, n, all) => n < all.length - 1 || line)
    .map((line) =>
      el(
        "span",
        {
          class: line.startsWith("@@")
            ? "hunk"
            : line.startsWith("+") && !line.startsWith("+++")
              ? "add"
              : line.startsWith("-") && !line.startsWith("---")
                ? "del"
                : null,
        },
        `${line}\n`,
      ),
    );
}

async function openSave() {
  // The edits of the last moments may not have been applied yet.
  state.current = await py("apply", { text: state.original.text, edits: state.edits });
  renderStatus();
  if (state.current.error || !state.current.diff) return;
  $("diff").replaceChildren(...renderDiff(state.current.diff));
  $("message").value = suggestMessage();
  $("steps").replaceChildren();
  $("submit").disabled = false;
  $("submit").hidden = false;
  validateMessage();
  $("save").showModal();
  $("message").focus();
}

function validateMessage() {
  const problems = checkMessage($("message").value);
  const header = $("message").value.split("\n")[0];
  const check = $("message-check");
  check.className = `check ${problems.length ? "bad" : "ok"}`;
  check.textContent = problems.length
    ? problems.join(" ")
    : `First line: ${header.length} of ${HEADER_LIMIT} characters.`;
  $("submit").disabled = !!problems.length || state.saving;
  return !problems.length;
}

function step(text) {
  const item = el("li", { class: "active" }, text);
  $("steps").append(item);
  return {
    done(note) {
      item.className = "done";
      item.textContent = `${text}: ${note ?? "done"}`;
    },
    failed(note) {
      item.className = "failed";
      item.textContent = `${text}: ${note}`;
    },
    note(note) {
      item.textContent = `${text}: ${note}`;
    },
    link(href, label) {
      item.append(" ", el("a", { href, target: "_blank", rel: "noopener" }, label));
    },
  };
}

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function slug(message) {
  const words = message
    .split("\n")[0]
    .replace(/^[^:]*:\s*/, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");
  const cut = words.length > 40 ? words.slice(0, 41).replace(/-[^-]*$/, "") : words;
  return cut || "config";
}

async function save() {
  if (!validateMessage()) return;
  const message = $("message").value.replace(/\s+$/, "") + "\n";
  const subject = message.split("\n")[0];
  state.saving = true;
  $("submit").disabled = true;
  renderStatus();
  let current;
  try {
    const stamp = new Date().toISOString().replace(/[-:T]/g, "").slice(0, 12);
    const branch = `editor/${slug(message)}-${stamp}`;
    current = step("Branch");
    await gh(`/repos/${REPO}/git/refs`, {
      method: "POST",
      body: { ref: `refs/heads/${branch}`, sha: state.original.commit },
    });
    current.done(branch);
    current = step("Commit");
    const put = await gh(`/repos/${REPO}/contents/${CONFIG}`, {
      method: "PUT",
      body: { message, content: toBase64(state.current.text), sha: state.original.blob, branch },
    });
    current.done(put.commit.sha.slice(0, 7));
    current = step("Pull request");
    const body = `Changed with the [feeds editor](${location.href}).\n\n\`\`\`diff\n${state.current.diff.trimEnd()}\n\`\`\`\n`;
    const pr = await gh(`/repos/${REPO}/pulls`, {
      method: "POST",
      body: { title: subject, head: branch, base: "main", body },
    });
    current.done(`#${pr.number}`);
    current.link(pr.html_url, "open on GitHub");
    await follow(pr);
  } catch (error) {
    current?.failed(error.message);
  } finally {
    state.saving = false;
    renderStatus();
  }
}

async function follow(pr) {
  let current = step("Checks and merge");
  let merged;
  for (;;) {
    const now = await gh(`/repos/${REPO}/pulls/${pr.number}`);
    if (now.merged) {
      merged = now;
      break;
    }
    if (now.state === "closed") {
      current.failed("closed without merging");
      return;
    }
    const runs = await gh(`/repos/${REPO}/commits/${now.head.sha}/check-runs`).catch(() => null);
    if (runs) {
      const done = runs.check_runs.filter((r) => r.status === "completed");
      const failed = done.filter((r) => !["success", "skipped", "neutral"].includes(r.conclusion));
      if (failed.length) {
        current.failed(`${failed.map((r) => r.name).join(", ")} failed; fix it on the pull request`);
        return;
      }
      current.note(`${done.length} of ${runs.check_runs.length} checks done`);
    }
    await wait(POLL);
  }
  current.done(`merged ${new Date(merged.merged_at).toLocaleTimeString()}`);
  // The merge dispatches the build; it's live once health.json says so.
  current = step("Live");
  const since = Date.parse(merged.merged_at) / 1000;
  for (;;) {
    const health = await loadHealth();
    if (health?.built >= since) break;
    current.note("waiting for the build the merge started (a minute or two)");
    await wait(POLL);
  }
  current.done("the published feeds use the new feeds.toml");
  $("submit").hidden = true;
  const reload = el("button", { type: "button", onclick: () => location.reload() }, "Reload the editor");
  $("steps").append(el("li", {}, reload));
}

// Start.

function init() {
  $("sign-out").addEventListener("click", signOut);
  $("token-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    $("sign-in-error").textContent = "";
    try {
      await signIn($("token").value.trim());
    } catch (error) {
      state.token = null;
      $("sign-in-error").textContent =
        error instanceof GitHubError && error.status === 401 ? "GitHub doesn't accept this token." : error.message;
    }
  });
  $("add").addEventListener("click", () => {
    const kind = $("add-kind").value;
    addTable(kind, NEW[kind]());
  });
  $("review").addEventListener("click", openSave);
  $("message").addEventListener("input", validateMessage);
  $("cancel").addEventListener("click", () => $("save").close());
  $("submit").addEventListener("click", save);
  const token = localStorage.getItem(TOKEN_KEY);
  if (!token) {
    $("sign-in").hidden = false;
    return;
  }
  signIn(token).catch((error) => {
    state.token = null;
    $("sign-in").hidden = false;
    $("sign-in-error").textContent = `Signing in with the saved token failed: ${error.message}`;
  });
}

init();
