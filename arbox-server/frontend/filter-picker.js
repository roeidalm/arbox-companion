const node = (tag, text) => {const n = document.createElement(tag); if (text != null) n.textContent = text; return n;};
export const selectedValues = value => Array.isArray(value) ? value : value ? [value] : [];
export function matchesFilters(row, filters) {
  return Object.entries(filters || {}).every(([key, value]) => {
    const values = selectedValues(value);
    if (!values.length) return true;
    if (key === 'daypart') {
      const hour = Number((row.start_time || '').slice(0,2));
      const part = hour < 12 ? 'morning' : hour < 16 ? 'noon' : hour < 19 ? 'afternoon' : 'evening';
      return values.includes(part);
    }
    return values.includes(row[key]);
  });
}
export function filterPicker({label, values, selected, change, open = false, toggle, key = label}) {
  const picked = new Set(selectedValues(selected));
  const root = node('details'); root.className = 'filter-picker'; root.open = open;
  root.ontoggle = () => toggle?.(root.open);
  const summary = node('summary');
  const update = () => {summary.textContent = `${label}: ${picked.size === 1 ? [...picked][0] : picked.size ? `${picked.size} נבחרו` : 'הכול'}`;};
  update(); root.append(summary);
  const list = node('div'); list.className = 'filter-picker-options';
  const all = node('button', 'הכול'); all.type = 'button';
  const inputs = [];
  all.onclick = () => {picked.clear(); inputs.forEach(i => {i.checked = false;}); update(); change([]);};
  list.append(all);
  for (const value of [...new Set([...values, ...picked])]) {
    const wrap = node('label'), input = node('input'); input.type = 'checkbox'; input.checked = picked.has(value);
    input.dataset.focusKey = `filter-${key}-${value}`;
    input.onchange = () => {if (input.checked) picked.add(value); else picked.delete(value); update(); change([...picked]);};
    inputs.push(input); wrap.append(input, node('span', value)); list.append(wrap);
  }
  root.append(list); return root;
}
