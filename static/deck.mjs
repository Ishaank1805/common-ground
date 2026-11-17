// The presentations inside the app: the Umeed pitch deck (header button and sign-in screen, from
// /static/deck/slides.html) and each conversation's own slides (the NGO slides tab, built by conversation-deck.mjs).
// A slide is a <section> on a fixed 1920×1080 canvas, scaled to fit; its speaker notes are in its <aside>.
const $ = selector => document.querySelector(selector);
const UMEED = {key: 'umeed', title: 'Common Ground for Umeed for Women'};
let umeedSlides = null;
let slides = null;
let key = null;
let current = 0;
let inlineDeck = null;

async function loadUmeed() {
  if (umeedSlides) return umeedSlides;
  const response = await fetch('/static/deck/slides.html');
  if (!response.ok) throw new Error('The presentation could not be loaded. Please try again.');
  const page = new DOMParser().parseFromString(await response.text(), 'text/html');
  umeedSlides = [...page.querySelectorAll('section.slide')];
  return umeedSlides;
}

function useSlides(list, deckKey, title) {
  if (deckKey !== key) current = 0;
  slides = list;
  key = deckKey;
  $('.deck-name').textContent = title;
  $('#deck-jump').replaceChildren(...slides.map((slide, index) => {
    const option = document.createElement('option');
    option.value = String(index);
    option.textContent = `${index + 1}. ${(slide.querySelector('h1, h2, .big')?.textContent || slide.id).trim()}`;
    return option;
  }));
}

function fit() {
  const frame = $('#deck-frame');
  const scale = Math.min(frame.clientWidth / 1920, frame.clientHeight / 1080);
  $('#deck-stage').style.transform = `scale(${Math.max(scale, 0.05)})`;
}

function show(index) {
  if (!slides?.length) return;
  current = Math.max(0, Math.min(slides.length - 1, index));
  $('#deck-frame').querySelector('.deck-error')?.remove();
  const slide = document.importNode(slides[current], true);
  const notes = slide.querySelector('aside');
  notes?.remove();
  $('#deck-stage').replaceChildren(slide); // Putting the slide in afresh replays its animation.
  $('#deck-notes').textContent = notes?.textContent.trim() || 'No speaker notes for this slide.';
  $('#deck-count').textContent = `${current + 1} / ${slides.length}`;
  $('#deck-jump').value = String(current);
  $('#deck-prev').disabled = current === 0;
  $('#deck-next').disabled = current === slides.length - 1;
  fit();
}

// Show a presentation in a container: the full-window dialog, or the "NGO slides" tab of a conversation.
// deck is {key, title, slides} for a conversation's own slides; without it, the Umeed pitch deck.
export async function showDeckIn(container, deck = null) {
  container.append($('#deck-viewer'));
  $('#deck-close').hidden = container !== $('#deck-dialog');
  if (container === $('#deck-inline')) inlineDeck = deck;
  try {
    useSlides(deck ? deck.slides : await loadUmeed(), deck ? deck.key : UMEED.key, deck ? deck.title : UMEED.title);
    show(current);
  } catch (error) {
    $('#deck-stage').replaceChildren();
    const message = document.createElement('p');
    message.className = 'deck-error';
    message.textContent = error.message;
    $('#deck-frame').prepend(message);
  }
}

export async function openDeck() {
  $('#deck-dialog').showModal();
  await showDeckIn($('#deck-dialog'));
}

for (const button of document.querySelectorAll('[data-open-deck]')) button.addEventListener('click', () => openDeck());
$('#deck-prev').addEventListener('click', () => show(current - 1));
$('#deck-next').addEventListener('click', () => show(current + 1));
$('#deck-jump').addEventListener('change', event => show(Number(event.target.value)));
$('#deck-close').addEventListener('click', () => {
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  $('#deck-dialog').close();
});
$('#deck-fullscreen').addEventListener('click', () => {
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  else $('#deck-viewer').requestFullscreen?.().catch(() => {});
});
$('#deck-notes-toggle').addEventListener('click', event => {
  const open = event.currentTarget.getAttribute('aria-expanded') !== 'true';
  event.currentTarget.setAttribute('aria-expanded', String(open));
  event.currentTarget.textContent = open ? 'Hide speaker notes' : 'Speaker notes';
  $('#deck-notes').hidden = !open;
  fit();
});
$('#deck-viewer').addEventListener('keydown', event => {
  if (event.target.closest('select, input, textarea')) return;
  if (['ArrowRight', 'PageDown'].includes(event.key)) { event.preventDefault(); show(current + 1); }
  if (['ArrowLeft', 'PageUp'].includes(event.key)) { event.preventDefault(); show(current - 1); }
  if (event.key === 'Home') { event.preventDefault(); show(0); }
  if (event.key === 'End' && slides) { event.preventDefault(); show(slides.length - 1); }
});
new ResizeObserver(() => { if (slides && $('#deck-frame').clientWidth) fit(); }).observe($('#deck-frame'));
// Closing the full-window view while the NGO slides tab is open puts that conversation's slides back in the tab.
$('#deck-dialog').addEventListener('close', () => {
  if ($('#slides-stage') && !$('#slides-stage').hidden) showDeckIn($('#deck-inline'), inlineDeck);
});
