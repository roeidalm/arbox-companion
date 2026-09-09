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
export function filterColor(value, color) {
  if (/^#(?:[0-9a-f]{3}|[0-9a-f]{4}|[0-9a-f]{6}|[0-9a-f]{8})$/i.test(color || '')) return color;
  let hash = 0; for (const c of String(value)) hash = (Math.imul(hash, 31) + c.codePointAt(0)) | 0;
  return `hsl(${Math.abs(hash) % 360} 43% 53%)`;
}
export function filterOptions(values, selected = [], search = '') {
  const picked = new Set(selectedValues(selected));
  const rows = new Map(values.map(v => {const row = typeof v === 'string' ? {value:v,label:v} : {value:v.value ?? v.name, label:v.label ?? v.name ?? v.value, color:v.color}; return [row.value, row];}));
  for (const value of picked) if (!rows.has(value)) rows.set(value, {value,label:value});
  const query = search.trim().toLocaleLowerCase();
  return [...rows.values()].filter(row => picked.has(row.value) || row.label.toLocaleLowerCase().includes(query))
    .sort((a,b) => Number(picked.has(b.value)) - Number(picked.has(a.value)) || a.label.localeCompare(b.label,'he'));
}
export function filterPicker({label, values, selected, change, open = false, toggle, key = label, search = '', onSearch}) {
  const picked = new Set(selectedValues(selected));
  const root = node('details'); root.className = 'filter-picker'; root.open = open;
  root.ontoggle = () => toggle?.(root.open);
  const summary = node('summary');
  const update = () => {summary.textContent = `${label} · ${picked.size === 1 ? [...picked][0] : picked.size ? `${picked.size} נבחרו` : 'הכל'}`;};
  update(); root.append(summary);
  const list = node('div'); list.className = 'filter-picker-options';
  const header=node('div'); header.className='filter-picker-header';
  const input=node('input'); input.type='search';input.placeholder=`חיפוש ${label}…`;input.setAttribute('aria-label',`חיפוש ${label}`);input.value=search;input.dataset.focusKey=`filter-search-${key}`;
  const all = node('button', 'הכל'); all.type = 'button';
  const close=node('button','×');close.type='button';close.setAttribute('aria-label',`סגירת סינון ${label}`);close.onclick=()=>{root.open=false;summary.focus();};
  header.append(input,all,close);list.append(header);
  const options=node('div');options.className='filter-picker-chips';list.append(options);
  const draw=()=>{
    options.replaceChildren();all.setAttribute('aria-pressed',String(!picked.size));
    const rows=filterOptions(values,[...picked],input.value);
    if(picked.size){const caption=node('small',`נבחרו · ${picked.size}`);caption.className='filter-picker-caption';options.append(caption);}
    let separated=false;
    for(const row of rows){
      if(picked.size&&!picked.has(row.value)&&!separated){const divider=node('div');divider.className='filter-picker-divider';options.append(divider);separated=true;}
      const b=node('button');b.type='button';b.className='filter-color-chip';b.style.setProperty('--filter-color',filterColor(row.value,row.color));b.setAttribute('aria-pressed',String(picked.has(row.value)));b.dataset.focusKey=`filter-${key}-${row.value}`;
      b.append(node('span',row.label));if(picked.has(row.value)){const check=node('span','✓');check.setAttribute('aria-hidden','true');b.append(check);}
      b.onclick=()=>{picked.has(row.value)?picked.delete(row.value):picked.add(row.value);update();draw();[...options.querySelectorAll('button')].find(n=>n.dataset.focusKey===b.dataset.focusKey)?.focus();change([...picked]);};options.append(b);
    }
    if(!rows.length)options.append(node('p','לא נמצאו התאמות'));
  };
  input.oninput=()=>{onSearch?.(input.value);draw();};
  all.onclick=()=>{picked.clear();input.value='';onSearch?.('');update();draw();change([]);};
  root.onkeydown=e=>{if(e.key==='Escape'){root.open=false;summary.focus();e.stopPropagation();}};
  draw();
  root.append(list); return root;
}

export function workoutSummary(rows, {filters = {}, change, open = false, toggle} = {}) {
  const root=node('aside');root.className='workout-summary';root.dataset.open=String(open);root.setAttribute('aria-label','סיכום אימונים קרובים');
  const filtered=rows.filter(row=>matchesFilters(row,filters));
  const active=Object.values(filters).some(value=>selectedValues(value).length);
  const text=active?`${filtered.length} מתוך ${rows.length} אימונים · מסונן`:`${rows.length} אימונים קרובים`;
  const heading=node('div');heading.className='workout-summary-heading';heading.append(node('strong',text));
  const expand=node('button');expand.type='button';expand.className='workout-summary-toggle';expand.append(node('strong',text),node('span',open?'צמצום ⌃':'פירוט ⌄'));expand.setAttribute('aria-expanded',String(open));expand.onclick=()=>{open=!open;root.dataset.open=String(open);expand.setAttribute('aria-expanded',String(open));expand.lastChild.textContent=open?'צמצום ⌃':'פירוט ⌄';toggle?.(open);};heading.append(expand);
  const body=node('div');body.className='workout-summary-body';const all=node('button','הכל');all.type='button';all.onclick=()=>change?.({category_name:[],coach_name:[],daypart:''});body.append(all);
  for(const[key,label]of [['category_name','שיעורים'],['coach_name','מאמנים']]){
    const group=node('div');group.className='workout-summary-group';group.append(node('small',label));const chips=node('div');chips.className='workout-summary-chips';
    for(const row of filterOptions([...new Set(rows.map(r=>r[key]).filter(Boolean))],filters[key])){
      const matching=rows.filter(r=>r[key]===row.value), b=node('button');b.type='button';b.className='filter-color-chip';b.style.setProperty('--filter-color',filterColor(row.value,key==='category_name'?matching[0]?.color:null));b.setAttribute('aria-pressed',String(selectedValues(filters[key]).includes(row.value)));b.title=`${row.label} · ${matching.length} אימונים קרובים`;b.append(node('span',row.label),node('b',`×${matching.length}`));b.onclick=()=>{const selected=new Set(selectedValues(filters[key]));selected.has(row.value)?selected.delete(row.value):selected.add(row.value);change?.({[key]:[...selected]});};chips.append(b);
    }
    group.append(chips);body.append(group);
  }
  root.append(heading,body);return root;
}
