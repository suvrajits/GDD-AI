// tiny mock retrieval: match pinned titles or body snippets
import { loadPins } from '../utils/storage.js';

export function queryRag(query){
  const pins = loadPins();
  const hits = pins.filter(p => (p.title||'').toLowerCase().includes(query.toLowerCase()) || (p.text||'').toLowerCase().includes(query.toLowerCase()));
  if(hits.length) {
    return { source: 'local-pins', hits: hits.slice(0,3), text: `Found ${hits.length} pinned items related to "${query}".` };
  }
  return { source: 'knowledge', hits: [], text: `No pinned hits — returning general guidance for "${query}".` };
}
