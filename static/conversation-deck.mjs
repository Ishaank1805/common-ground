// Draws the NGO slides Gemini wrote for a conversation (see gemini.generate_slides), in the presentation's styles
// (static/deck/deck.css). Every value is inserted as text, never as markup, and the content is all Gemini's, as
// checked by the server: quotes are her own words and table values are the app's own counts.
function el(tag, className = '', text = '') {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

const KINDS = {cover: 'slide dark', closing: 'slide dark', statement: 'slide accent', quote: 'slide sage', cards: 'slide', table: 'slide', list: 'slide sage'};

function draw(spec, index) {
  const section = el('section', KINDS[spec.layout] || 'slide');
  section.id = `slide-${index + 1}`;
  if (spec.eyebrow) section.append(el('p', 'eyebrow', spec.eyebrow));
  if (spec.layout === 'cover' || spec.layout === 'closing') {
    section.append(el('h1', 'title', spec.heading));
    if (spec.subtitle) section.append(el('p', 'lead', spec.subtitle));
  } else if (spec.layout === 'statement') {
    section.append(el('p', 'big', spec.heading));
    if (spec.subtitle) section.append(el('p', 'lead', spec.subtitle));
  } else {
    section.append(el('h2', 'heading', spec.heading));
    if (spec.subtitle) section.append(el('p', 'lead', spec.subtitle));
  }
  if (spec.layout === 'cards' && spec.items.length) {
    const row = el('div', spec.items.length > 3 ? 'cards grid' : 'cards');
    for (const item of spec.items) {
      const card = el('div', 'card');
      if (item.label) card.append(el('p', 'card-step', item.label));
      if (item.title) card.append(el('h3', '', item.title));
      if (item.text) card.append(el('p', 'card-text', item.text));
      row.append(card);
    }
    section.append(row);
  }
  if (spec.layout === 'table' && spec.rows.length) {
    const table = el('table', 'results');
    for (const entry of spec.rows) {
      const line = el('tr');
      line.append(el('td', '', entry.label), el('td', 'value', entry.value));
      table.append(line);
    }
    section.append(table);
  }
  if (spec.layout === 'list' && spec.items.length) {
    const list = el('ol', 'asks');
    for (const item of spec.items) {
      const line = el('li');
      if (item.title || item.label) line.append(el('b', '', `${item.title || item.label}: `));
      line.append(document.createTextNode(item.text));
      list.append(line);
    }
    section.append(list);
  }
  if (spec.quote) section.append(el('p', spec.layout === 'quote' ? 'big' : 'lead', `“${spec.quote}”`));
  if (spec.note) section.append(el('p', 'note', spec.note));
  if (spec.notes) section.append(el('aside', '', spec.notes));
  return section;
}

export function renderSlides(slides) {
  return slides.map(draw);
}
