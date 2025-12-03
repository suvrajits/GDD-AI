export function $(sel, root=document) { return root.querySelector(sel); }
export function $all(sel, root=document) { return Array.from(root.querySelectorAll(sel)); }
export function ce(tag, cls=''){ const e = document.createElement(tag); if(cls) e.className = cls; return e; }
