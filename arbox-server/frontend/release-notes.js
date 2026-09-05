/* Curated, image-local notes. The installed version comes only from /api/health. */
(function () {
  "use strict";
  const STORAGE_KEY = "arbox_release_notes_v1";
  function versionParts(value) {
    const match = /^v?(\d+)\.(\d+)\.(\d+)$/.exec(value || "");
    return match ? match.slice(1).map(Number) : null;
  }
  function compareVersions(a, b) {
    const aa = versionParts(a), bb = versionParts(b);
    if (!aa || !bb) return null;
    for (let i = 0; i < 3; i++) if (aa[i] !== bb[i]) return aa[i] > bb[i] ? 1 : -1;
    return 0;
  }
  function selectNotes(data, version, previousVersion) {
    const development = ["dev", "local-demo"].includes(version);
    const current = data.current;
    const available = (data.releases || []).filter(r =>
      development || (compareVersions(r.version, version) !== null && compareVersions(r.version, version) <= 0));
    if (current && (development || compareVersions(version, current.after) === 1)) {
      available.unshift({ version, notes: current.notes });
    }
    available.sort((a, b) => a.version === version ? -1 : b.version === version ? 1 : (compareVersions(b.version, a.version) || 0));
    const latest = available.find(r => compareVersions(r.version, version) === 0 || r.version === version);
    // An unrecognized build never borrows another version's announcement.
    if (!latest) return null;
    const updates = available.filter(r => r === latest ||
      (previousVersion && compareVersions(r.version, previousVersion) === 1));
    const priority = { action: 0, removed: 1, changed: 2, added: 2 };
    const notes = updates.flatMap(r => r.notes).map((note, index) => ({ note, index }));
    // Breaking/action-required changes must remain visible in the short card.
    notes.sort((a, b) => (priority[a.note.kind] ?? 3) - (priority[b.note.kind] ?? 3) || a.index - b.index);
    const summary = notes.slice(0, 3).map(x => x.note);
    const additional = available.map(release => ({
      ...release, notes: release.notes.filter(note => !summary.includes(note)),
    })).filter(release => release.notes.length);
    return {
      version, development, available, additional, notes: summary,
      identity: development ? `${version}:${current?.id || "preview"}` : version.replace(/^v/, ""),
    };
  }
  function readSeen(storage) {
    try {
      const value = JSON.parse(storage.getItem(STORAGE_KEY));
      return value && Array.isArray(value.seen) ? value : { seen: [] };
    } catch (_) { return { seen: [] }; }
  }
  function shouldShow(model, seen) { return !!model && !seen.seen.includes(model.identity); }
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { selectNotes, compareVersions, readSeen, shouldShow, STORAGE_KEY };
  }
  if (typeof document === "undefined") return;

  let model = null, pending = false, restoreFocus = null;
  // Storage can be disabled in embedded/private browsers. Keep this visit usable.
  let storage;
  try { storage = window.localStorage; } catch (_) { storage = null; }
  let seen = readSeen(storage);
  const button = document.querySelector("#releaseNotesOpen");
  const dialog = document.createElement("dialog");
  dialog.id = "releaseNotesDialog";
  dialog.dir = "rtl";
  dialog.setAttribute("aria-labelledby", "releaseNotesTitle");
  document.body.append(dialog);
  function element(tag, text, className) {
    const el = document.createElement(tag);
    if (text) el.textContent = text;
    if (className) el.className = className;
    return el;
  }
  function noteList(notes) {
    const list = element("ul");
    const icons = { added: "✨", changed: "🔄", removed: "➖", action: "⚠️" };
    for (const note of notes) {
      const li = element("li"), icon = element("span", icons[note.kind] || "✨", "release-icon");
      icon.setAttribute("aria-hidden", "true");
      const body = element("div");
      const prefix = note.kind === "removed" ? "הוסר: " : note.kind === "action" ? "נדרשת פעולה: " : "";
      body.append(element("h3", prefix + note.title), element("p", note.text));
      if (["/mine", "/journal", "/settings", "/automations", "/system", "/schedule"].includes(note.href)) {
        const link = element("a", note.link_label || "לפתיחה");
        link.href = note.href;
        link.addEventListener("click", () => acknowledge());
        body.append(link);
      }
      li.append(icon, body); list.append(li);
    }
    return list;
  }
  function render() {
    dialog.replaceChildren();
    const content = element("div", "", "release-content");
    const title = element("h2", "מה חדש ב־Arbox"); title.id = "releaseNotesTitle";
    content.append(title, element("p", model.development ? "תצוגה מקומית · השינויים שבפיתוח" : `גרסה ${model.version.replace(/^v/, "")}`, "release-subtitle"), noteList(model.notes));
    const details = element("details"), summary = element("summary", "שינויים נוספים וגרסאות קודמות");
    const history = element("div", "", "release-history");
    for (const release of model.additional) {
      history.append(element("h3", model.development && release.version === model.version ? "בפיתוח" : `גרסה ${release.version.replace(/^v/, "")}`), noteList(release.notes));
    }
    const allLink = element("a", "לרשימת הגרסאות המלאה ב־GitHub", "release-all-link");
    allLink.href = "https://github.com/roeidalm/arbox-companion/releases";
    allLink.target = "_blank"; allLink.rel = "noopener noreferrer";
    history.append(allLink); details.append(summary, history); if (model.additional.length) content.append(details);
    else content.append(allLink);
    const footer = element("div", "", "release-footer");
    const done = element("button", "הבנתי", "primary"); done.type = "button"; done.autofocus = true;
    done.addEventListener("click", () => dialog.close());
    footer.append(element("small", "מוצג פעם אחת לגרסה בדפדפן הזה. תמיד זמין שוב בהגדרות."), done);
    dialog.append(content, footer);
  }
  function acknowledge() {
    if (!model) return;
    seen = { seen: [...new Set([...seen.seen, model.identity])].slice(-100), lastVersion: model.version };
    try { storage?.setItem(STORAGE_KEY, JSON.stringify(seen)); } catch (_) {}
    pending = false;
  }
  function open() {
    if (!model || dialog.open) return;
    restoreFocus = document.activeElement;
    render(); dialog.showModal();
  }
  function maybeShow() {
    if (pending && !document.querySelector("dialog[open]")) { pending = false; open(); }
  }
  dialog.addEventListener("close", () => {
    acknowledge();
    if (restoreFocus?.isConnected) restoreFocus.focus();
  });
  // Native Escape and the acknowledgement button both dismiss permanently.
  document.addEventListener("close", () => queueMicrotask(maybeShow), true);
  button?.addEventListener("click", open);
  window.addEventListener("storage", event => {
    if (event.key !== STORAGE_KEY) return;
    seen = readSeen(storage);
    if (!shouldShow(model, seen)) pending = false;
  });
  window.addEventListener("arbox:ready", async event => {
    try {
      const response = await fetch("/static/release-notes.json", { cache: "no-store" });
      if (!response.ok) return;
      model = selectNotes(await response.json(), event.detail.version, seen.lastVersion);
      if (!model) return;
      if (button) button.hidden = false;
      pending = shouldShow(model, seen);
      maybeShow();
    } catch (_) { /* Release notes must never block the application. */ }
  });
})();
