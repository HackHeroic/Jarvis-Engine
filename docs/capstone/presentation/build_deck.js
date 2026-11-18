// build_deck.js — regenerates Jarvis-Capstone.pptx (16:9) for the capstone evaluation.
//
// Inputs, all in this folder:
//   assets/screens/*.png   cropped live renders of the running app (headless Chrome, 1440 px wide)
//   apply_theme.js         writes the deck's theme colours after pptxgenjs saves the file
//
// Run (dependencies live outside the repo; any folder works):
//   npm install --prefix <dir> pptxgenjs@3.12.0 sharp react@18.3.1 react-dom@18.3.1 lucide-react@0.460.0
//   NODE_PATH=<dir>/node_modules node docs/capstone/presentation/build_deck.js
//
// Palette and type come from jarvis-frontend: app/globals.css ([data-theme="dark"] surfaces and
// text), tailwind.config.ts (terra, gold, dusk, sage; Inter + JetBrains Mono) and
// components/landing/Hero.tsx (the #1C1A17 canvas, aurora glows, the orb). Inter and JetBrains
// Mono are not installed with Office or Keynote, so the deck uses Arial and Courier New, their
// closest standard fallbacks. Icons are Lucide, the frontend's icon set.

const path = require("path");
const fs = require("fs");
const pptxgen = require("pptxgenjs");
const sharp = require("sharp");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
const lucide = require("lucide-react");
const { applyTheme } = require("./apply_theme.js");

const HERE = __dirname;
const OUT = process.env.DECK_OUT || path.join(HERE, "Jarvis-Capstone.pptx");
const SCREENS = path.join(HERE, "assets", "screens");

// ---------------------------------------------------------------- palette (jarvis-frontend)
const HEX = {
  canvas: "1C1A17", // --surface-canvas (dark), Hero background
  card: "252320", // --surface-card (dark)
  subtle: "201E1B", // --surface-subtle (dark)
  border: "3A3632", // --border-default (dark)
  ivory: "FAF8F4", // --text-primary (dark)
  secondary: "9C9488", // --text-secondary (dark)
  terra: "D4775A", // --color-terra
  gold: "E09D5C", // --color-gold
  dusk: "6B7FB5", // --color-dusk
  sage: "4A7B6B", // --color-sage
  sageLight: "7DB3A0", // sage lifted for text on the dark canvas
};

const THEME = {
  name: "Jarvis HUD",
  headFontFace: "Arial",
  bodyFontFace: "Arial",
  colors: {
    dk1: HEX.ivory, // Text 1 is light: this is a dark deck
    lt1: HEX.canvas,
    dk2: HEX.secondary,
    lt2: HEX.card,
    accent1: HEX.terra,
    accent2: HEX.gold,
    accent3: HEX.dusk,
    accent4: HEX.sage,
    accent5: HEX.border,
    accent6: HEX.sageLight,
    hlink: HEX.gold,
    folHlink: HEX.dusk,
  },
};

const MONO = "Courier New"; // stands in for JetBrains Mono
const W = 13.333;
const H = 7.5;
const MX = 0.75;
const CW = W - 2 * MX; // 11.833

let pres;
let C;

// ---------------------------------------------------------------- small helpers
const glow = (color = HEX.terra, opacity = 0.35, blur = 14) => ({
  type: "outer",
  color,
  blur,
  offset: 0,
  angle: 90,
  opacity,
});

function text(slide, value, opts) {
  slide.addText(value, {
    isTextBox: true,
    margin: 0,
    valign: "top",
    color: C.text1,
    fontSize: 16,
    ...opts,
  });
}

const mono = (value, extra = {}) => ({
  text: value,
  options: { fontFace: MONO, bold: true, color: C.accent2, ...extra },
});

function box(slide, x, y, w, h, o = {}) {
  slide.addShape(o.shape || pres.shapes.ROUNDED_RECTANGLE, {
    x,
    y,
    w,
    h,
    rectRadius: o.shape ? undefined : o.r ?? 0.12,
    fill: o.noFill ? { type: "none" } : { color: o.fill ?? C.background2, transparency: o.ft ?? 0 },
    line: o.noLine ? { type: "none" } : { color: o.line ?? C.accent5, width: o.lw ?? 1, transparency: o.lt ?? 0, dashType: o.dash },
    shadow: o.glow ? glow(o.glowColor, o.glowOpacity, o.glowBlur) : undefined,
    objectName: o.name,
  });
}

function circle(slide, cx, cy, d, o = {}) {
  box(slide, cx - d / 2, cy - d / 2, d, d, { ...o, shape: pres.shapes.OVAL });
}

function line(slide, x1, y1, x2, y2, o = {}) {
  slide.addShape(pres.shapes.LINE, {
    x: Math.min(x1, x2),
    y: Math.min(y1, y2),
    w: Math.abs(x2 - x1),
    h: Math.abs(y2 - y1),
    flipH: x2 < x1,
    flipV: y2 < y1,
    line: {
      color: o.color ?? C.text2,
      width: o.w ?? 1.25,
      transparency: o.t ?? 0,
      dashType: o.dash,
      endArrowType: o.end === undefined ? "triangle" : o.end || undefined,
      beginArrowType: o.begin,
    },
    objectName: o.name,
  });
}

const iconCache = new Map();
async function iconData(name, color, stroke = 1.75) {
  const key = `${name}:${color}:${stroke}`;
  if (iconCache.has(key)) return iconCache.get(key);
  const Comp = lucide[name];
  if (!Comp) throw new Error(`no lucide icon ${name}`);
  const svg = renderToStaticMarkup(React.createElement(Comp, { size: 256, color: `#${color}`, strokeWidth: stroke }));
  const buf = await sharp(Buffer.from(svg)).png().toBuffer();
  const data = "image/png;base64," + buf.toString("base64");
  iconCache.set(key, data);
  return data;
}

async function icon(slide, name, x, y, size, color = HEX.terra, alt) {
  slide.addImage({ data: await iconData(name, color), x, y, w: size, h: size, altText: alt || `${name} icon` });
}

// An icon inside a glowing ring: the deck's recurring motif, after the frontend's orb.
async function orb(slide, name, cx, cy, d, o = {}) {
  const accent = o.accent ?? HEX.terra;
  const scheme = o.scheme ?? C.accent1;
  circle(slide, cx, cy, d, {
    fill: o.solid ? C.background1 : scheme,
    ft: o.solid ? 0 : 84,
    line: scheme,
    lw: 1.5,
    glow: true,
    glowColor: accent,
    glowOpacity: o.glowOpacity ?? 0.45,
    glowBlur: 16,
    name: o.name,
  });
  if (name) await icon(slide, name, cx - d * 0.27, cy - d * 0.27, d * 0.54, o.iconColor ?? accent, o.alt);
}

function eyebrowText(slide, value, x, y, w, color) {
  text(slide, value, { x, y, w, h: 0.28, fontSize: 12, bold: true, color: color ?? C.accent1, charSpacing: 3 });
}

function head(slide, eyebrow, title) {
  slide.addText(eyebrow, { placeholder: "eyebrow", charSpacing: 3, bullet: false });
  slide.addText(title, { placeholder: "title", bullet: false });
}

// ---------------------------------------------------------------- generated art (HUD backgrounds)
const PX = 200; // px per inch for the background images
function arcPath(cx, cy, r, a0, a1) {
  const rad = (a) => (a * Math.PI) / 180;
  const x0 = cx + r * Math.cos(rad(a0));
  const y0 = cy + r * Math.sin(rad(a0));
  const x1 = cx + r * Math.cos(rad(a1));
  const y1 = cy + r * Math.sin(rad(a1));
  const large = a1 - a0 > 180 ? 1 : 0;
  return `M ${x0.toFixed(1)} ${y0.toFixed(1)} A ${r} ${r} 0 ${large} 1 ${x1.toFixed(1)} ${y1.toFixed(1)}`;
}
function ticks(cx, cy, r, n, len, longEvery, longLen, stroke, opacity) {
  let s = "";
  for (let i = 0; i < n; i++) {
    const a = (i / n) * Math.PI * 2;
    const l = i % longEvery === 0 ? longLen : len;
    const x0 = cx + r * Math.cos(a);
    const y0 = cy + r * Math.sin(a);
    const x1 = cx + (r + l) * Math.cos(a);
    const y1 = cy + (r + l) * Math.sin(a);
    s += `<line x1="${x0.toFixed(1)}" y1="${y0.toFixed(1)}" x2="${x1.toFixed(1)}" y2="${y1.toFixed(1)}" stroke="${stroke}" stroke-opacity="${opacity}" stroke-width="2"/>`;
  }
  return s;
}
const DEFS = `
  <radialGradient id="base" cx="0.5" cy="0.45" r="0.8">
    <stop offset="0" stop-color="#211E1A"/><stop offset="0.6" stop-color="#1A1815"/><stop offset="1" stop-color="#100F0D"/>
  </radialGradient>
  <radialGradient id="aT"><stop offset="0" stop-color="#D4775A" stop-opacity="1"/><stop offset="1" stop-color="#D4775A" stop-opacity="0"/></radialGradient>
  <radialGradient id="aD"><stop offset="0" stop-color="#6B7FB5" stop-opacity="1"/><stop offset="1" stop-color="#6B7FB5" stop-opacity="0"/></radialGradient>
  <radialGradient id="aS"><stop offset="0" stop-color="#4A7B6B" stop-opacity="1"/><stop offset="1" stop-color="#4A7B6B" stop-opacity="0"/></radialGradient>
  <radialGradient id="core">
    <stop offset="0" stop-color="#FFF4E6" stop-opacity="1"/><stop offset="0.28" stop-color="#E09D5C" stop-opacity="0.75"/>
    <stop offset="0.62" stop-color="#D4775A" stop-opacity="0.22"/><stop offset="1" stop-color="#D4775A" stop-opacity="0"/>
  </radialGradient>
  <filter id="soft" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="12"/></filter>
  <filter id="softer" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="28"/></filter>`;

function reactor(cx, cy, s = 1) {
  const R = (r) => r * s;
  let g = "";
  g += `<circle cx="${cx}" cy="${cy}" r="${R(760)}" fill="url(#aT)" opacity="0.10"/>`;
  g += `<circle cx="${cx}" cy="${cy}" r="${R(650)}" fill="none" stroke="#FAF8F4" stroke-opacity="0.08" stroke-width="2"/>`;
  g += ticks(cx, cy, R(600), 180, R(12), 15, R(30), "#9C9488", 0.45);
  // segmented outer coil, with a blurred copy underneath for the glow
  let seg = "";
  for (let i = 0; i < 12; i++) seg += `<path d="${arcPath(cx, cy, R(545), i * 30 + 3, i * 30 + 26)}"/>`;
  g += `<g fill="none" stroke="#D4775A" stroke-width="${R(16)}" opacity="0.55" filter="url(#soft)">${seg}</g>`;
  g += `<g fill="none" stroke="#D4775A" stroke-width="${R(9)}" opacity="0.9">${seg}</g>`;
  g += `<path d="${arcPath(cx, cy, R(480), -40, 230)}" fill="none" stroke="#E09D5C" stroke-width="${R(10)}" opacity="0.45" filter="url(#soft)"/>`;
  g += `<path d="${arcPath(cx, cy, R(480), -40, 230)}" fill="none" stroke="#E09D5C" stroke-width="${R(3)}" opacity="0.9"/>`;
  g += `<circle cx="${cx}" cy="${cy}" r="${R(430)}" fill="none" stroke="#6B7FB5" stroke-opacity="0.6" stroke-width="2" stroke-dasharray="3 12"/>`;
  g += `<circle cx="${cx}" cy="${cy}" r="${R(360)}" fill="none" stroke="#FAF8F4" stroke-opacity="0.12" stroke-width="1.5"/>`;
  let coils = "";
  for (let i = 0; i < 3; i++) coils += `<path d="${arcPath(cx, cy, R(300), i * 120 + 15, i * 120 + 95)}"/>`;
  g += `<g fill="none" stroke="#D4775A" stroke-width="${R(26)}" opacity="0.35" filter="url(#soft)">${coils}</g>`;
  g += `<g fill="none" stroke="#D4775A" stroke-width="${R(14)}" opacity="0.75">${coils}</g>`;
  g += `<circle cx="${cx}" cy="${cy}" r="${R(225)}" fill="none" stroke="#E09D5C" stroke-opacity="0.4" stroke-width="2"/>`;
  g += `<circle cx="${cx}" cy="${cy}" r="${R(260)}" fill="url(#core)" filter="url(#softer)" opacity="0.9"/>`;
  g += `<circle cx="${cx}" cy="${cy}" r="${R(150)}" fill="url(#core)" opacity="0.9"/>`;
  // the frontend Hero's brain-orb network, scaled into the core
  const n = [
    [-40, -40, "#D4775A"], [40, -40, "#6B7FB5"], [60, 20, "#4A7B6B"], [0, 60, "#D4775A"], [-60, 20, "#6B7FB5"], [0, 0, "#FAF8F4"],
  ];
  const edges = [[0, 1], [1, 2], [2, 3], [3, 4], [4, 0], [0, 5], [1, 5], [5, 3]];
  const k = R(1.35);
  for (const [a, b] of edges)
    g += `<line x1="${cx + n[a][0] * k}" y1="${cy + n[a][1] * k}" x2="${cx + n[b][0] * k}" y2="${cy + n[b][1] * k}" stroke="#1C1A17" stroke-opacity="0.55" stroke-width="${R(3)}"/>`;
  for (const [x, y, c] of n) g += `<circle cx="${cx + x * k}" cy="${cy + y * k}" r="${R(7)}" fill="${c === "#FAF8F4" ? "#1C1A17" : c}" fill-opacity="0.9"/>`;
  return g;
}

function svgDoc(body) {
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${W * PX}" height="${H * PX}" viewBox="0 0 ${W * PX} ${H * PX}"><defs>${DEFS}</defs>${body}</svg>`;
}

function titleBackground() {
  const w = W * PX;
  const h = H * PX;
  let b = `<rect width="${w}" height="${h}" fill="url(#base)"/>`;
  b += `<ellipse cx="${0.2 * w}" cy="${0.55 * h}" rx="${0.42 * w}" ry="${0.5 * h}" fill="url(#aT)" opacity="0.13"/>`;
  b += `<ellipse cx="${0.72 * w}" cy="${0.3 * h}" rx="${0.38 * w}" ry="${0.6 * h}" fill="url(#aD)" opacity="0.13"/>`;
  b += `<ellipse cx="${0.6 * w}" cy="${0.85 * h}" rx="${0.35 * w}" ry="${0.3 * h}" fill="url(#aS)" opacity="0.10"/>`;
  b += reactor(10.1 * PX, 3.75 * PX, 1);
  return svgDoc(b);
}

function contentBackground() {
  const w = W * PX;
  const h = H * PX;
  const cx = 2745;
  const cy = -95;
  let b = `<rect width="${w}" height="${h}" fill="url(#base)"/>`;
  b += `<ellipse cx="${0.08 * w}" cy="${0.95 * h}" rx="${0.45 * w}" ry="${0.45 * h}" fill="url(#aT)" opacity="0.07"/>`;
  b += `<ellipse cx="${0.95 * w}" cy="${0.1 * h}" rx="${0.35 * w}" ry="${0.5 * h}" fill="url(#aD)" opacity="0.09"/>`;
  b += `<circle cx="${cx}" cy="${cy}" r="470" fill="none" stroke="#FAF8F4" stroke-opacity="0.07" stroke-width="2"/>`;
  b += ticks(cx, cy, 420, 120, 10, 10, 22, "#9C9488", 0.35);
  let seg = "";
  for (let i = 0; i < 12; i++) seg += `<path d="${arcPath(cx, cy, 360, i * 30 + 4, i * 30 + 25)}"/>`;
  b += `<g fill="none" stroke="#D4775A" stroke-width="12" opacity="0.35" filter="url(#soft)">${seg}</g>`;
  b += `<g fill="none" stroke="#D4775A" stroke-width="5" opacity="0.6">${seg}</g>`;
  b += `<circle cx="${cx}" cy="${cy}" r="300" fill="none" stroke="#6B7FB5" stroke-opacity="0.45" stroke-width="2" stroke-dasharray="3 10"/>`;
  b += `<path d="${arcPath(cx, cy, 250, 90, 200)}" fill="none" stroke="#E09D5C" stroke-opacity="0.55" stroke-width="3"/>`;
  return svgDoc(b);
}

async function png(svg) {
  const buf = await sharp(Buffer.from(svg)).png().toBuffer();
  return "image/png;base64," + buf.toString("base64");
}

// Screenshots: rounded corners, embedded.
async function screen(file, widthPx = 1400, radiusFrac = 0.013) {
  const src = path.join(SCREENS, file);
  const meta = await sharp(src).metadata();
  const w = Math.min(widthPx, meta.width); // never upscale a capture
  const scale = w / meta.width;
  const h = Math.round(meta.height * scale);
  const r = Math.round(radiusFrac * w);
  const mask = Buffer.from(`<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}"><rect width="${w}" height="${h}" rx="${r}" ry="${r}"/></svg>`);
  const buf = await sharp(src).resize({ width: w }).composite([{ input: mask, blend: "dest-in" }]).png().toBuffer();
  return { data: "image/png;base64," + buf.toString("base64"), ratio: meta.width / meta.height };
}

async function framedScreen(slide, file, x, y, h, o = {}) {
  const img = await screen(file);
  const w = h * img.ratio;
  box(slide, x - 0.07, y - 0.07, w + 0.14, h + 0.14, {
    r: 0.14,
    fill: C.background2,
    line: o.accent ?? C.accent1,
    lw: 1,
    lt: 35,
    glow: true,
    glowColor: o.glowHex ?? HEX.terra,
    glowOpacity: 0.3,
    glowBlur: 18,
  });
  slide.addImage({ data: img.data, x, y, w, h, altText: o.alt });
  return w;
}

async function device(slide, file, x, y, h, o = {}) {
  const pad = o.pad ?? 0.09;
  const img = await screen(file, 1400, o.radiusFrac ?? 0.02);
  const w = h * img.ratio;
  box(slide, x - pad, y - pad, w + pad * 2, h + pad * 2, {
    r: o.r ?? 0.16,
    fill: C.background1,
    line: o.accent ?? C.accent5,
    lw: 1.25,
    glow: true,
    glowColor: o.glowHex ?? HEX.terra,
    glowOpacity: 0.28,
    glowBlur: 16,
    name: o.name,
  });
  slide.addImage({ data: img.data, x, y, w, h, altText: o.alt });
  return w;
}

// ---------------------------------------------------------------- slides
async function s01() {
  const s = pres.addSlide({ masterName: "JARVIS_TITLE", sectionTitle: "Opening" });
  s.addText("CAPSTONE  ·  SYSTEM DESIGN & SOFTWARE ENGINEERING", { placeholder: "eyebrow", charSpacing: 1.5, bullet: false });
  s.addText("Jarvis", { placeholder: "title", glow: { size: 14, opacity: 0.3, color: HEX.terra }, bullet: false });
  s.addText("A personal intelligence that has your work ready before you ask.", { placeholder: "subtitle", bullet: false });
  text(s, "Team Jarvis", { x: MX, y: 5.2, w: 5, h: 0.4, fontSize: 18, bold: true });
  text(s, "Murali Madhav C  ·  Amathziah J  ·  Ravi Yadav  ·  Pugazhendhi J", { x: MX, y: 5.58, w: 9, h: 0.32, fontSize: 14, color: C.text1 });
  text(s, "Low-level design evaluation  ·  November 2026", { x: MX, y: 5.95, w: 6, h: 0.3, fontSize: 13, color: C.text2 });
  s.addNotes(
    "Good morning. We are Team Jarvis, and this is our capstone for System Design and Software Engineering. " +
      "Jarvis is a personal intelligence: it builds a model of one person and has their work ready before they ask. " +
      "We will walk the ten low-level-design criteria in order, then show the system running. " +
      "Every number on these slides comes from docs/capstone/01 to 10 and docs/PROJECT_STATUS.md, and every claim names a file you can open."
  );
}

async function s02() {
  const s = pres.addSlide({ masterName: "JARVIS_CONTENT", sectionTitle: "Opening" });
  head(s, "01  ·  THE PROBLEM", "Every tool waits to be asked");
  text(
    s,
    [
      { text: "The person with too much on their mind has to " },
      { text: "remember, ask and report back.", options: { color: C.accent1, bold: true } },
    ],
    { x: MX, y: 2.2, w: 5.9, h: 2.1, fontSize: 26 }
  );
  eyebrowText(s, "OUR RULE", MX, 4.75, 3, C.accent6);
  text(s, "Never ask “did you do it?” Infer, confirm, adapt.", { x: MX, y: 5.08, w: 5.9, h: 0.9, fontSize: 20 });

  const cx = 8.5;
  const cy = 4.35;
  circle(s, cx, cy, 3.0, { noFill: true, line: C.text2, lw: 0.75, lt: 70, dash: "sysDot" });
  circle(s, cx, cy, 2.25, { noFill: true, line: C.accent3, lw: 0.75, lt: 50 });
  circle(s, cx, cy, 1.5, { fill: C.accent3, ft: 82, line: C.accent3, lw: 1.75, glow: true, glowColor: HEX.dusk, glowOpacity: 0.5, glowBlur: 20, name: "You" });
  text(s, "You", { x: cx - 0.75, y: cy - 0.25, w: 1.5, h: 0.5, fontSize: 24, bold: true, align: "center", valign: "middle" });

  const items = [
    ["ListTodo", "What’s due?", 2.85],
    ["MessageSquare", "What’s the plan?", 4.35],
    ["CircleCheck", "Did you do it?", 5.85],
  ];
  for (const [ic, label, yc] of items) {
    box(s, 10.2, yc - 0.42, 2.38, 0.84, { name: label });
    await icon(s, ic, 10.38, yc - 0.17, 0.34, HEX.terra);
    text(s, label, { x: 10.8, y: yc - 0.38, w: 1.72, h: 0.76, fontSize: 15, valign: "middle" });
    const dx = 10.12 - cx;
    const dy = yc - cy;
    const len = Math.hypot(dx, dy);
    const r = 0.86;
    line(s, 10.12, yc, cx + (dx / len) * r, cy + (dy / len) * r, { color: C.accent1, w: 1.5 });
  }
  s.addNotes(
    "Start with the person, not the tool. Planners, to-do apps and chatbots all wait: the user has to remember what is due, ask for a plan and report back what got done. " +
      "That is exactly the load an overloaded person cannot carry, and when the plan slips the tool asks 'did you complete this?', which adds guilt. " +
      "Our design principle, at the top of .claude/CLAUDE.md, is never to ask that question: infer, confirm, adapt. " +
      "The spec's audit (docs/superpowers/specs/2026-11-14-capstone-lld-and-anticipation-design.md, section 2) found the old system had no Clock actor, so nothing could ever be proactive; fixing that drove the design."
  );
}

async function s03() {
  const s = pres.addSlide({ masterName: "JARVIS_CONTENT", sectionTitle: "Opening" });
  head(s, "02  ·  WHAT JARVIS IS", "A model of you that moves first");
  const cx = 3.45;
  const cy = 4.4;
  const R = 1.85;
  circle(s, cx, cy, R * 2, { noFill: true, line: C.text2, lw: 1, lt: 55, dash: "sysDot" });
  circle(s, cx, cy, 1.75, { fill: C.accent1, ft: 84, line: C.accent1, lw: 1.75, glow: true, glowOpacity: 0.55, glowBlur: 22, name: "User model" });
  text(s, "User model", { x: cx - 0.85, y: cy - 0.18, w: 1.7, h: 0.36, fontSize: 18, bold: true, align: "center" });
  const sats = [
    ["Eye", "Observes", -90],
    ["BookOpen", "Remembers", 30],
    ["Sparkles", "Anticipates", 150],
  ];
  for (const [ic, label, deg] of sats) {
    const a = (deg * Math.PI) / 180;
    const x = cx + R * Math.cos(a);
    const y = cy + R * Math.sin(a);
    await orb(s, ic, x, y, 0.82, { solid: true, name: label });
    if (deg === -90) text(s, label, { x: x + 0.55, y: y - 0.17, w: 1.6, h: 0.34, fontSize: 15, bold: true });
    else text(s, label, { x: x - 0.9, y: y + 0.5, w: 1.8, h: 0.32, fontSize: 15, bold: true, align: "center" });
  }

  text(
    s,
    [
      { text: "The scheduler is the muscle.", options: { color: C.text1, breakLine: true } },
      { text: "The user model and the observation loop", options: { color: C.text1, breakLine: true } },
      { text: "are the ", options: { color: C.text1 } },
      { text: "brain.", options: { color: C.accent1, bold: true } },
    ],
    { x: 6.95, y: 2.05, w: 5.63, h: 1.35, fontSize: 20 }
  );
  eyebrowText(s, "IT IS", 6.95, 3.75, 2.6, C.accent6);
  eyebrowText(s, "IT IS NOT", 9.95, 3.75, 2.6, C.text2);
  const is = ["Learns from outcomes", "Forgets on request", "Asks before acting"];
  const isNot = ["A calendar", "A planner you fill in", "A chatbot that waits"];
  for (let i = 0; i < 3; i++) {
    const y = 4.2 + i * 0.62;
    await icon(s, "Check", 6.95, y + 0.03, 0.26, HEX.sageLight);
    text(s, is[i], { x: 7.35, y, w: 2.5, h: 0.34, fontSize: 16 });
    await icon(s, "X", 9.95, y + 0.03, 0.26, HEX.secondary);
    text(s, isNot[i], { x: 10.35, y, w: 2.25, h: 0.34, fontSize: 16, color: C.text2 });
  }
  s.addNotes(
    "Jarvis builds a living model of one person: when they focus, what they set aside, what they have told it. " +
      "It is not a calendar and not a planner you fill in; in the words of the spec (section 1), the scheduler is a muscle, and the user model plus the observation loop is the brain. " +
      "Concretely it observes outcomes in the append-only task_events table, remembers what you say and forgets on request, and anticipates with a morning brief and rule proposals. " +
      "Anything that would change your schedule waits for your consent."
  );
}

async function s04() {
  const s = pres.addSlide({ masterName: "JARVIS_CONTENT", sectionTitle: "Opening" });
  head(s, "03  ·  A DAY WITH JARVIS", "From a morning brief to a forget that sticks");
  const x0 = 0.95;
  const aw = 11.45;
  const ay = 3.15;
  const ah = 1.2;
  s.addShape(pres.shapes.CUSTOM_GEOMETRY, {
    x: x0,
    y: ay,
    w: aw,
    h: ah,
    points: [{ x: 0, y: ah }, { x: aw, y: ah, curve: { type: "quadratic", x1: aw / 2, y1: -ah } }],
    line: { color: C.accent1, width: 1.75, transparency: 25 },
    fill: { type: "none" },
    shadow: glow(HEX.terra, 0.5, 10),
    objectName: "Orbit",
  });
  const steps = [
    ["Sunrise", "Brief is ready", "written from facts"],
    ["MessageSquare", "Brain dump", "in your own words"],
    ["CalendarClock", "A draft", "nothing written yet"],
    ["CheckCheck", "You accept", "one transaction"],
    ["Radar", "It notices", "14 of 15 at 9\u00a0AM"],
    ["Eraser", "You forget", "and it stays forgotten"],
  ];
  for (let i = 0; i < steps.length; i++) {
    const [ic, title, desc] = steps[i];
    const x = 1.55 + i * 2.05;
    const t = (x - x0) / aw;
    const y = ay + ah * Math.pow(1 - 2 * t, 2);
    const hot = i === 4;
    await orb(s, ic, x, y, 0.86, {
      solid: true,
      accent: hot ? HEX.gold : HEX.terra,
      scheme: hot ? C.accent2 : C.accent1,
      glowOpacity: hot ? 0.7 : 0.45,
      name: title,
    });
    text(s, String(i + 1).padStart(2, "0"), { x: x - 0.5, y: y - 0.78, w: 1.0, h: 0.26, fontSize: 12, bold: true, fontFace: MONO, color: C.accent2, align: "center" });
    text(s, title, { x: x - 1.0, y: y + 0.56, w: 2.0, h: 0.34, fontSize: 16, bold: true, align: "center" });
    text(s, desc, { x: x - 0.97, y: y + 0.92, w: 1.94, h: 0.62, fontSize: 13, color: C.text2, align: "center" });
  }
  s.addNotes(
    "This is the story the demo follows (10-demo-and-contributions.md, section 2); the wording is the seeded demo user's, as the running app shows it. " +
      "In the morning the brief is already waiting, written from facts with no model call. " +
      "You brain-dump in your own words; Jarvis extracts the goal, decomposes it and lets CP-SAT place it, but it is only a draft until you accept. " +
      "Accepting writes the plan in one database transaction, and a second accept is a 409. " +
      "Later it notices you set work aside around 9 AM in 14 of the last 15 times and offers to keep that hour clear; and when you tell it to forget something, it stays forgotten even if you say it again."
  );
}

async function s05() {
  const s = pres.addSlide({ masterName: "JARVIS_CONTENT", sectionTitle: "Design" });
  head(s, "04  ·  REQUIREMENTS", "Eight use cases, two primary actors");
  const ucs = [
    "Plan a brain dump",
    "Accept atomically",
    "Teach a rule",
    "Complete or skip",
    "Remember or forget",
    "Morning brief",
    "Pattern → rule",
    "Review what it knows",
  ];
  const ux = 3.0;
  const uw = 3.55;
  const uy = (i) => 2.05 + i * 0.58;
  const user = { x: 1.3, y: 3.15 };
  const clock = { x: 1.3, y: 5.45 };
  for (let i = 0; i < 8; i++) line(s, user.x + 0.45, user.y, ux, uy(i) + 0.21, { color: C.text2, w: 0.75, t: 45, end: null });
  for (const i of [5, 6]) line(s, clock.x + 0.45, clock.y, ux, uy(i) + 0.21, { color: C.accent3, w: 1.5, end: null });
  for (let i = 0; i < 8; i++) {
    const clockDriven = i === 5 || i === 6;
    box(s, ux, uy(i), uw, 0.42, { r: 0.21, line: clockDriven ? C.accent3 : C.accent5, lw: clockDriven ? 1.25 : 1, name: `UC${i + 1}` });
    text(s, [mono(`UC${i + 1}`, { fontSize: 12 }), { text: `   ${ucs[i]}`, options: { fontSize: 14 } }], { x: ux + 0.2, y: uy(i), w: uw - 0.3, h: 0.42, valign: "middle" });
  }
  await orb(s, "User", user.x, user.y, 0.85, { solid: true, name: "Actor User" });
  text(s, "User", { x: user.x - 0.6, y: user.y + 0.5, w: 1.2, h: 0.3, fontSize: 14, bold: true, align: "center" });
  await orb(s, "Clock", clock.x, clock.y, 0.85, { solid: true, accent: HEX.dusk, scheme: C.accent3, name: "Actor Clock" });
  text(s, "Clock", { x: clock.x - 0.6, y: clock.y + 0.5, w: 1.2, h: 0.3, fontSize: 14, bold: true, align: "center" });

  text(s, "36", { x: 7.35, y: 1.95, w: 2.2, h: 1.0, fontSize: 60, bold: true, color: C.accent1 });
  text(s, "functional", { x: 7.38, y: 2.95, w: 2.2, h: 0.3, fontSize: 15, color: C.text2 });
  text(s, "15", { x: 9.85, y: 1.95, w: 2.2, h: 1.0, fontSize: 60, bold: true, color: C.accent2 });
  text(s, "non-functional", { x: 9.88, y: 2.95, w: 2.4, h: 0.3, fontSize: 15, color: C.text2 });
  text(s, "Each traced to code and a test.", { x: 7.38, y: 3.55, w: 5.2, h: 0.35, fontSize: 16 });
  box(s, 7.35, 4.5, 5.23, 1.75, { line: C.accent3, lw: 1.25, glow: true, glowColor: HEX.dusk, glowOpacity: 0.35, name: "Clock callout" });
  eyebrowText(s, "THE DESIGN MOVE", 7.65, 4.75, 4, C.accent3);
  text(s, "The Clock is an actor, so Jarvis can move first.", { x: 7.65, y: 5.1, w: 4.7, h: 0.95, fontSize: 20, bold: true });
  s.addNotes(
    "01-requirements.md lists 36 functional and 15 non-functional requirements, and every row names the code that implements it and the test or live step that proves it. " +
      "There are eight use cases. The primary actors are the User and, unusually, the Clock: the AnticipationScheduler ticks every 60 seconds, which is what lets Jarvis act first. " +
      "UC6 and UC7, the morning brief and the pattern-to-rule proposal, are the clock-driven ones, drawn in blue. " +
      "Secondary actors are the local and cloud models, the CP-SAT solver and the database."
  );
}

async function s06() {
  const s = pres.addSlide({ masterName: "JARVIS_CONTENT", sectionTitle: "Design" });
  head(s, "05  ·  ARCHITECTURE", "Dependencies point inward");
  const cx = 3.75;
  const cy = 4.42;
  circle(s, cx, cy, 4.6, { fill: C.background2, ft: 55, line: C.text2, lw: 1, lt: 30, name: "Outer ring" });
  circle(s, cx, cy, 3.2, { fill: C.background2, ft: 10, line: C.accent3, lw: 1.25, name: "Services ring" });
  circle(s, cx, cy, 1.7, { fill: C.accent1, ft: 84, line: C.accent1, lw: 1.75, glow: true, glowOpacity: 0.55, glowBlur: 20, name: "Domain core" });
  text(s, "Domain", { x: cx - 0.8, y: cy - 0.33, w: 1.6, h: 0.36, fontSize: 18, bold: true, align: "center" });
  text(s, "ports · entities", { x: cx - 0.8, y: cy + 0.06, w: 1.6, h: 0.28, fontSize: 12, color: C.text2, align: "center" });
  text(s, "Services & graph", { x: cx - 1.0, y: cy - 1.42, w: 2.0, h: 0.3, fontSize: 13, bold: true, color: C.accent3, align: "center" });
  text(s, "API · routers only", { x: cx - 1.0, y: cy - 2.12, w: 2.0, h: 0.3, fontSize: 13, bold: true, align: "center" });
  text(s, "Infrastructure · adapters", { x: cx - 1.3, y: cy + 1.68, w: 2.6, h: 0.3, fontSize: 13, bold: true, align: "center" });
  for (const deg of [-35, 35, 145, 215]) {
    const a = (deg * Math.PI) / 180;
    line(s, cx + 2.12 * Math.cos(a), cy + 2.12 * Math.sin(a), cx + 0.98 * Math.cos(a), cy + 0.98 * Math.sin(a), { color: C.accent1, w: 1.75 });
  }
  text(
    s,
    [{ text: "Only the " }, { text: "composition root", options: { color: C.accent1, bold: true } }, { text: " names a concrete class." }],
    { x: 7.35, y: 2.1, w: 5.2, h: 1.0, fontSize: 22 }
  );
  const rows = [
    ["tests/test_layering.py", "parses every import; a wrong arrow fails"],
    ["AppContainer.build", "picks every concrete class, once"],
    ["6-entry allow-list", "legacy table calls; it can only shrink"],
  ];
  rows.forEach(([code, desc], i) => {
    const y = 3.45 + i * 1.02;
    box(s, 7.35, y, 5.23, 0.86, { name: code });
    text(s, [mono(code, { fontSize: 14 })], { x: 7.6, y: y + 0.12, w: 4.9, h: 0.3 });
    text(s, desc, { x: 7.6, y: y + 0.45, w: 4.9, h: 0.3, fontSize: 14, color: C.text2 });
  });
  s.addNotes(
    "One FastAPI process built as ports and adapters (02-architecture.md, section 1). The domain at the centre imports only the standard library and Pydantic; services and the LangGraph orchestrator depend on its ports; routers and infrastructure sit outside. " +
      "tests/test_layering.py parses every import with ast and fails the build on an arrow that points the wrong way. " +
      "AppContainer.build in app/container.py is the only place a concrete class is chosen: the Supabase or in-memory repository family, the model chain, every service. " +
      "Six legacy call sites still make raw table calls; they sit on an exact allow-list that a test keeps from growing."
  );
}

async function s07() {
  const s = pres.addSlide({ masterName: "JARVIS_CONTENT", sectionTitle: "Design" });
  head(s, "06  ·  SEQUENCE", "Life of a chat turn");
  const lanes = ["Chat page", "ChatTurnService", "LangGraph turn", "route_llm_call", "CP-SAT solver", "DraftService"];
  const code = [false, true, false, true, false, true];
  const lw = CW / lanes.length;
  const lx = (i) => MX + lw * i + lw / 2;
  for (let i = 0; i < lanes.length; i++) {
    line(s, lx(i), 2.48, lx(i), 6.8, { color: C.text2, w: 1, t: 45, dash: "dash", end: null });
    box(s, lx(i) - 0.9, 1.98, 1.8, 0.5, { line: i === 2 ? C.accent1 : C.accent5, glow: i === 2, glowOpacity: 0.3, name: lanes[i] });
    text(s, code[i] ? [mono(lanes[i], { fontSize: 12 })] : lanes[i], { x: lx(i) - 0.88, y: 1.98, w: 1.76, h: 0.5, fontSize: 13, bold: true, align: "center", valign: "middle" });
  }
  const msgs = [
    [0, 1, "1  POST /chat/stream"],
    [1, 1, "2  lock user:session"],
    [1, 2, "3  run the turn"],
    [2, 3, "4  fast extract"],
    [2, 3, "5  heavy decompose"],
    [2, 4, "6  run_schedule"],
    [2, 5, "7  propose draft"],
    [2, 2, "8  observation loop learns"],
    [1, 0, "9  frames … complete"],
  ];
  msgs.forEach(([a, b, label], i) => {
    const y = 2.82 + i * 0.48;
    if (a === b) {
      const pw = label.length > 20 ? 2.45 : 2.0;
      box(s, lx(a) - pw / 2, y - 0.15, pw, 0.3, { r: 0.16, fill: C.background1, line: C.accent2, lw: 1.25, name: label });
      text(s, label, { x: lx(a) - pw / 2 + 0.05, y: y - 0.15, w: pw - 0.1, h: 0.3, fontSize: 12, align: "center", valign: "middle" });
      return;
    }
    const back = b < a;
    line(s, lx(a), y, lx(b), y, { color: back ? C.accent1 : C.text1, w: back ? 1.5 : 1.25, dash: back ? "dash" : undefined, t: back ? 0 : 15 });
    const left = Math.min(lx(a), lx(b));
    // each label sits inside the first gap after its sender, so no lifeline crosses it
    text(s, label, { x: left + 0.1, y: y - 0.25, w: lw - 0.2, h: 0.22, fontSize: 11, color: back ? C.accent1 : C.text2 });
    if (label.startsWith("7")) {
      text(s, "draft is PENDING", { x: lx(4) + 0.1, y: y - 0.25, w: lw - 0.2, h: 0.22, fontSize: 11, color: C.accent2 });
    }
  });
  s.addNotes(
    "This is S1 in 06-sequence-and-activity.md, simplified. The router only opens a turn; ChatTurnService takes a per-conversation lock, reads the conversation and runs the graph. " +
      "One fast model call (the FAST role) extracts the brain dump, intent is classified by rules with no model call, one heavy call (the PRIMARY role, under the PRIMARY lock) decomposes the goal, and run_schedule asks CP-SAT for the slots. " +
      "DraftService.propose stores a pending draft and writes nothing to user_tasks. " +
      "The observation loop learns inside the turn, so learning frames arrive before the single complete frame, and the lock is released in a shielded finally even if the tab closes."
  );
}

function classBox(s, x, y, w, h, o) {
  box(s, x, y, w, h, { r: 0.08, line: o.accent ?? C.accent5, lw: o.accent ? 1.5 : 1, glow: !!o.glow, glowOpacity: 0.35, name: o.name });
  let ty = y + 0.1;
  if (o.stereo) {
    text(s, o.stereo, { x: x + 0.1, y: ty, w: w - 0.2, h: 0.22, fontSize: 11, color: C.text2, align: "center" });
    ty += 0.22;
  }
  if (!o.members) {
    text(s, [mono(o.name, { fontSize: o.nameSize ?? 13, color: C.text1 })], { x: x + 0.06, y, w: w - 0.12, h, align: "center", valign: "middle" });
    return;
  }
  text(s, [mono(o.name, { fontSize: o.nameSize ?? 13, color: C.text1 })], { x: x + 0.08, y: ty, w: w - 0.16, h: 0.28, align: "center" });
  ty += 0.36;
  line(s, x, ty, x + w, ty, { color: C.accent5, w: 1, end: null });
  if (o.members) {
    text(s, o.members.map((m, i) => ({ text: m, options: { breakLine: i < o.members.length - 1 } })), {
      x: x + 0.16,
      y: ty + 0.08,
      w: w - 0.3,
      h: h - (ty - y) - 0.12,
      fontSize: 12,
      fontFace: MONO,
      color: C.text2,
      paraSpaceAfter: 2,
    });
  }
}

async function s08() {
  const s = pres.addSlide({ masterName: "JARVIS_CONTENT", sectionTitle: "Design" });
  head(s, "07  ·  CLASS DIAGRAMS", "Ports in the domain, adapters at the edge");
  classBox(s, 0.85, 2.05, 2.75, 1.55, { stereo: "«entity»", name: "DraftSchedule", members: ["status: DraftStatus", "version", "transition(to)"] });
  classBox(s, 0.85, 4.35, 2.75, 1.75, { stereo: "«enumeration»", name: "DraftStatus", members: ["pending · modified", "accepted · rejected", "expired · superseded"] });
  classBox(s, 4.25, 2.05, 2.2, 1.3, { name: "DraftService", members: ["propose()", "accept()"] });
  classBox(s, 6.95, 2.05, 2.55, 1.75, { stereo: "«interface»", name: "DraftRepository", accent: C.accent1, glow: true, members: ["get()", "accept_atomic()", "set_status()"] });
  classBox(s, 4.05, 4.85, 2.6, 0.62, { name: "SupabaseDraftRepository", nameSize: 12 });
  classBox(s, 6.95, 4.85, 2.6, 0.62, { name: "InMemoryDraftRepository", nameSize: 12 });
  // associations
  line(s, 6.45, 2.62, 6.95, 2.62, { color: C.text1, w: 1.25, end: "arrow" });
  line(s, 4.25, 2.62, 3.6, 2.62, { color: C.text2, w: 1.25, dash: "dash", end: "arrow" });
  line(s, 2.22, 3.6, 2.22, 4.35, { color: C.text2, w: 1.25, end: "arrow" });
  // realizations
  line(s, 5.35, 4.85, 7.7, 3.8, { color: C.accent1, w: 1.25, dash: "dash", end: "triangle" });
  line(s, 8.25, 4.85, 8.25, 3.8, { color: C.accent1, w: 1.25, dash: "dash", end: "triangle" });
  text(s, "uses", { x: 6.4, y: 2.3, w: 0.6, h: 0.24, fontSize: 11, color: C.text2, align: "center" });
  text(s, "implements", { x: 8.35, y: 4.2, w: 1.1, h: 0.24, fontSize: 11, color: C.accent1 });

  text(s, "15", { x: 9.95, y: 1.95, w: 2.6, h: 1.05, fontSize: 66, bold: true, color: C.accent1, glow: { size: 6, opacity: 0.25, color: HEX.terra } });
  text(s, "class diagrams in doc 03", { x: 9.98, y: 3.0, w: 2.6, h: 0.3, fontSize: 15, color: C.text2 });
  text(s, [mono("test_design_conformance.py", { fontSize: 11 })], { x: 9.98, y: 3.6, w: 2.6, h: 0.26 });
  text(s, "checks each one against the code.", { x: 9.98, y: 3.92, w: 2.6, h: 0.7, fontSize: 15 });
  text(s, "A diagram that lies fails the build.", { x: 9.98, y: 4.8, w: 2.6, h: 0.8, fontSize: 17, bold: true, color: C.accent1 });
  s.addNotes(
    "03-class-diagrams.md has fifteen diagrams: one overview and fourteen subsystem views. This is the draft slice. " +
      "DraftService depends on the DraftRepository port, an abc.ABC in app/domain/ports.py, and two families implement it: SupabaseDraftRepository, whose accept_atomic calls the accept_draft SQL function, and InMemoryDraftRepository. " +
      "DraftSchedule owns its status and only changes it through transition(), which checks the DraftStatus transition table. " +
      "tests/test_design_conformance.py parses every diagram in doc 03 and checks that each class imports, each interface is an ABC, each arrow is a real subclass or attribute and each listed method exists. A diagram that lies fails the build."
  );
}

async function s09() {
  const s = pres.addSlide({ masterName: "JARVIS_CONTENT", sectionTitle: "Design" });
  head(s, "08  ·  OBJECT-ORIENTED DESIGN", "Four pillars, four real classes");
  const cards = [
    ["Lock", "Encapsulation", "DraftSchedule", "Status changes only through its transition table"],
    ["Layers", "Abstraction", "LLMProvider", "Three methods; no transport, retry or vendor"],
    ["GitBranch", "Inheritance", "JarvisError", "Each subclass carries its code and HTTP status"],
    ["Shapes", "Polymorphism", "FallbackChainProvider", "One call; local, Claude or Gemini answers"],
  ];
  const gap = 0.3;
  const w = (CW - gap * 3) / 4;
  for (let i = 0; i < 4; i++) {
    const [ic, name, cls, desc] = cards[i];
    const x = MX + i * (w + gap);
    box(s, x, 2.1, w, 4.15, { name });
    await orb(s, ic, x + 0.75, 2.95, 0.9, { solid: true, name: `${name} icon` });
    text(s, name, { x: x + 0.3, y: 3.75, w: w - 0.5, h: 0.42, fontSize: 21, bold: true });
    text(s, [mono(cls, { fontSize: 12 })], { x: x + 0.3, y: 4.27, w: w - 0.4, h: 0.3 });
    text(s, desc, { x: x + 0.3, y: 4.75, w: w - 0.55, h: 1.3, fontSize: 16, color: C.text2 });
  }
  s.addNotes(
    "One real class for each pillar, from 04-oop-and-solid.md sections 1 to 4. " +
      "Encapsulation: DraftSchedule.transition checks the transition table and raises InvalidStateTransition, a 409, on an illegal move. " +
      "Abstraction: the LLMProvider port has three methods and hides transport, retries and the vendor. " +
      "Inheritance: every JarvisError subclass sets its own code and HTTP status, and one handler renders them all as the envelope. " +
      "Polymorphism: FallbackChainProvider calls complete() on whatever providers it holds, local, Claude or Gemini. Doc 04 also says where each one falls short."
  );
}

async function s10() {
  const s = pres.addSlide({ masterName: "JARVIS_CONTENT", sectionTitle: "Design" });
  head(s, "09  ·  SOLID", "SOLID, as the tests keep it");
  const cols = [
    ["S", "Single responsibility", "The chat router only routes"],
    ["O", "Open / closed", "Add a detector; edit nothing"],
    ["L", "Liskov substitution", "Two repository families, one contract"],
    ["I", "Interface segregation", "Planning sees seven fields, not the container"],
    ["D", "Dependency inversion", "Services take ports, never classes"],
  ];
  const gap = 0.25;
  const w = (CW - gap * 4) / 5;
  cols.forEach(([letter, name, ex], i) => {
    const x = MX + i * (w + gap);
    box(s, x, 2.05, w, 3.9, { name: `SOLID ${letter}` });
    text(s, letter, { x: x + 0.25, y: 2.2, w: 1.2, h: 1.2, fontSize: 64, bold: true, color: i % 2 ? C.accent2 : C.accent1, glow: { size: 6, opacity: 0.3, color: i % 2 ? HEX.gold : HEX.terra } });
    text(s, name, { x: x + 0.25, y: 3.55, w: w - 0.45, h: 0.62, fontSize: 15, bold: true });
    text(s, ex, { x: x + 0.25, y: 4.35, w: w - 0.45, h: 1.6, fontSize: 16, color: C.text2 });
  });
  text(s, "Each one is pinned by a test that would break (doc 04).", { x: MX, y: 6.15, w: CW, h: 0.3, fontSize: 13, color: C.text2 });
  s.addNotes(
    "From 04-oop-and-solid.md sections 5 to 9, each with a test that would break. " +
      "S: the chat router only routes; test_chat_turn.py caps it at 80 lines and forbids imports of the turn's internals. " +
      "O: a new PatternDetector or Trigger is registered in the container and nothing else is edited. L: tests/test_repository_contracts.py runs one contract against both repository families, and I: the planning sub-graph receives PlanningServices, a seven-field record, not the whole container. D: services take ports in their constructors and only AppContainer picks implementations. " +
      "Be ready to say where we fall short: the turn state is one wide dict, and adding a cognitive module still means editing the routing table."
  );
}

async function s11() {
  const s = pres.addSlide({ masterName: "JARVIS_CONTENT", sectionTitle: "Design" });
  head(s, "10  ·  DESIGN PATTERNS", "Eleven patterns, each with a job");
  const clusters = [
    ["Database", "Persistence", [["Repository", "11 ports, 2 families"], ["Adapter", "Supabase*Repository"], ["Factory", "RepositoryFactory"]]],
    ["Cpu", "Model chain", [["Strategy", "LLMProvider"], ["Decorator", "Redact → Retry → Meter → Timeout"], ["Chain of Responsibility", "FallbackChainProvider"]]],
    [
      "Sparkles",
      "Anticipation",
      [["Template Method", "Trigger.run"], ["Chain of Responsibility", "ConsentPolicy"], ["State", "SUGGESTION_TRANSITIONS"], ["Command", "ProactiveAction"], ["Observer", "EventBus"]],
    ],
  ];
  const gap = 0.3;
  const w = (CW - gap * 2) / 3;
  const top = 1.98;
  const ph = 3.85;
  for (let c = 0; c < 3; c++) {
    const [ic, name, pats] = clusters[c];
    const x = MX + c * (w + gap);
    box(s, x, top, w, ph, { fill: C.background2, ft: 30, name });
    await icon(s, ic, x + 0.22, top + 0.2, 0.3, HEX.terra);
    text(s, name, { x: x + 0.62, y: top + 0.18, w: w - 0.8, h: 0.34, fontSize: 16, bold: true });
    pats.forEach(([p, where], j) => {
      const y = top + 0.68 + j * 0.64;
      box(s, x + 0.18, y, w - 0.36, 0.56, { fill: C.background1, r: 0.08, name: p });
      text(s, p, { x: x + 0.34, y: y + 0.05, w: w - 0.6, h: 0.24, fontSize: 13, bold: true });
      text(s, [mono(where, { fontSize: 11 })], { x: x + 0.34, y: y + 0.29, w: w - 0.6, h: 0.22 });
    });
    line(s, x + w / 2, 6.1, x + w / 2, top + ph, { color: C.accent1, w: 1.25, t: 20 });
  }
  box(s, MX, 6.1, CW, 0.58, { line: C.accent1, lw: 1.25, glow: true, glowOpacity: 0.35, name: "Composition root" });
  text(
    s,
    [
      { text: "Composition root  ", options: { bold: true } },
      mono("AppContainer.build", { fontSize: 13 }),
      { text: "      Facade  ", options: { bold: true } },
      mono("UserModelService.snapshot", { fontSize: 13 }),
    ],
    { x: MX + 0.3, y: 6.1, w: CW - 0.6, h: 0.58, fontSize: 14, valign: "middle" }
  );
  s.addNotes(
    "05-design-patterns.md lists only patterns that exist in the code, each with the test that pins it. " +
      "Persistence uses Repository, with an in-memory and a Supabase family, Adapter and a factory. The model chain uses Strategy, the four Decorators and a Chain of Responsibility for fallback. " +
      "Anticipation uses Template Method in Trigger.run, the consent chain, State transition tables, the Command-like ProactiveAction and the Observer EventBus. " +
      "AppContainer.build is the composition root that wires them all, and UserModelService.snapshot is the facade the You page reads. Section 13 of that doc lists the patterns the spec named that we did not build."
  );
}

async function s12() {
  const s = pres.addSlide({ masterName: "JARVIS_CONTENT", sectionTitle: "Design" });
  head(s, "11  ·  ANTICIPATION", "The first move, under consent");
  const nodes = [
    ["Timer", "Clock", "a tick every 60 s"],
    ["RefreshCw", "Sweeps first", "slots → missed"],
    ["Workflow", "Triggers", "Trigger.run"],
    ["ShieldCheck", "Consent", "ConsentPolicy"],
  ];
  const gap = 0.45;
  const w = (CW - gap * 3) / 4;
  for (let i = 0; i < 4; i++) {
    const [ic, name, sub] = nodes[i];
    const x = MX + i * (w + gap);
    box(s, x, 2.0, w, 0.98, { line: i === 3 ? C.accent1 : C.accent5, glow: i === 3, glowOpacity: 0.35, name });
    await icon(s, ic, x + 0.22, 2.29, 0.4, i === 3 ? HEX.terra : HEX.gold);
    text(s, name, { x: x + 0.8, y: 2.14, w: w - 0.95, h: 0.32, fontSize: 16, bold: true });
    const isCode = sub === "Trigger.run" || sub === "ConsentPolicy";
    text(s, isCode ? [mono(sub, { fontSize: 13 })] : sub, {
      x: x + 0.8,
      y: 2.5,
      w: w - 0.9,
      h: 0.3,
      fontSize: 13,
      color: C.text2,
    });
    if (i < 3) line(s, x + w + 0.06, 2.49, x + w + gap - 0.06, 2.49, { color: C.accent1, w: 1.5 });
  }
  // consent chain
  text(s, "Consent: the first rule that decides wins", { x: MX, y: 3.45, w: 5.6, h: 0.32, fontSize: 15, bold: true });
  const rules = [
    ["Dismissed before", "DENY", C.accent1],
    ["Read-only", "ALLOW", C.accent6],
    ["Your stated rule covers it", "DENY", C.accent1],
    ["Your autonomy mode", "DECIDES", C.accent2],
  ];
  rules.forEach(([r, verdict, col], i) => {
    const y = 3.95 + i * 0.66;
    box(s, MX, y, 5.6, 0.52, { name: r });
    text(s, [mono(String(i + 1), { fontSize: 13 }), { text: "   " + r, options: { fontSize: 15 } }], { x: MX + 0.2, y, w: 4.0, h: 0.52, valign: "middle" });
    box(s, MX + 4.35, y + 0.1, 1.05, 0.32, { r: 0.16, fill: col, ft: 80, line: col, lw: 1 });
    text(s, [mono(verdict, { fontSize: 12, color: col })], { x: MX + 4.35, y: y + 0.1, w: 1.05, h: 0.32, align: "center", valign: "middle" });
    if (i < 3) line(s, MX + 0.27, y + 0.52, MX + 0.27, y + 0.66, { color: C.text2, w: 1, end: null });
  });
  // suggestion state machine
  text(s, "A suggestion’s life", { x: 7.0, y: 3.45, w: 5.5, h: 0.32, fontSize: 15, bold: true });
  const st = (label, x, y, w, accent) => {
    box(s, x, y, w, 0.46, { r: 0.23, fill: C.background1, line: accent ?? C.accent3, lw: 1.25, name: label });
    text(s, [mono(label, { fontSize: 12, color: C.text1 })], { x, y, w, h: 0.46, align: "center", valign: "middle" });
  };
  st("pending", 7.0, 4.15, 1.45, C.accent2);
  st("accepted", 9.05, 4.15, 1.5);
  st("auto_applied", 11.08, 4.15, 1.5);
  st("dismissed", 7.0, 5.35, 1.45);
  st("expired", 9.05, 5.35, 1.5);
  line(s, 8.45, 4.38, 9.05, 4.38, { color: C.text1, w: 1.25 });
  line(s, 10.55, 4.38, 11.08, 4.38, { color: C.text1, w: 1.25 });
  line(s, 7.72, 4.61, 7.72, 5.35, { color: C.text2, w: 1.25 });
  line(s, 9.8, 4.61, 9.8, 5.35, { color: C.text2, w: 1.25 });
  text(s, "A schedule change always asks.", { x: 7.0, y: 6.1, w: 5.5, h: 0.36, fontSize: 17, bold: true, color: C.accent1 });
  s.addNotes(
    "AnticipationScheduler ticks every 60 seconds (app/services/anticipation/scheduler.py). Each tick runs the due sweeps first: passed slots become missed after a 30-minute grace, memories decay, sessions get summaries. Then it runs each user's triggers, the MorningBriefTrigger and the PatternRuleTrigger. " +
      "Trigger.run is a Template Method in app/domain/anticipation.py: should_fire, gather, build_suggestion, then consent. " +
      "ConsentPolicy is a chain of responsibility: dismissed before means deny, read-only means allow, an hour you already said to keep free means deny, and otherwise your autonomy mode decides; each decision carries a typed reason the card shows. " +
      "Suggestions move through SUGGESTION_TRANSITIONS with compare-and-set writes, and a schedule change always asks first."
  );
}

async function s13() {
  const s = pres.addSlide({ masterName: "JARVIS_CONTENT", sectionTitle: "Design" });
  head(s, "12  ·  MEMORY", "Remembers what you say. Forgets for good.");
  const st = (label, x, y, w, o = {}) => {
    box(s, x, y, w, 0.6, { r: 0.3, fill: o.fill ?? C.background1, ft: o.ft ?? 0, line: o.line ?? C.accent3, lw: 1.5, glow: !!o.glow, glowColor: o.glowHex, glowOpacity: 0.55, glowBlur: 18, name: label });
    text(s, [mono(label, { fontSize: 15, color: C.text1 })], { x, y, w, h: 0.6, align: "center", valign: "middle" });
  };
  st("active", 0.95, 2.6, 1.8, { line: C.accent1, glow: true, glowHex: HEX.terra });
  st("faded", 4.55, 2.6, 1.8);
  st("dismissed", 0.95, 5.0, 1.8);
  st("forgotten", 4.55, 5.0, 1.8, { line: C.accent2, glow: true, glowHex: HEX.gold });
  line(s, 2.75, 2.78, 4.55, 2.78, { color: C.text1, w: 1.25 });
  line(s, 4.55, 3.03, 2.75, 3.03, { color: C.text2, w: 1.25 });
  text(s, "daily decay", { x: 2.8, y: 2.45, w: 1.7, h: 0.26, fontSize: 12, color: C.text2, align: "center" });
  text(s, "said again", { x: 2.8, y: 3.1, w: 1.7, h: 0.26, fontSize: 12, color: C.text2, align: "center" });
  line(s, 1.85, 3.2, 1.85, 5.0, { color: C.text2, w: 1.25 });
  text(s, "“not right”", { x: 0.75, y: 3.95, w: 1.0, h: 0.26, fontSize: 12, color: C.text2 });
  line(s, 5.45, 3.2, 5.45, 5.0, { color: C.accent2, w: 1.25 });
  line(s, 2.55, 3.2, 4.75, 5.0, { color: C.accent2, w: 1.25 });
  line(s, 2.75, 5.3, 4.55, 5.3, { color: C.accent2, w: 1.25 });
  text(s, "forget", { x: 5.55, y: 3.95, w: 0.9, h: 0.26, fontSize: 12, color: C.accent2 });
  text(s, [{ text: "Tombstone", options: { bold: true, color: C.accent2, breakLine: true } }, { text: "words erased, embedding kept" }], { x: 3.95, y: 5.72, w: 3.0, h: 0.6, fontSize: 13, color: C.text2, align: "center" });

  const rows = [
    ["Repeat", "Said twice: reinforced, not duplicated"],
    ["Search", "Recall by pgvector, within a token budget"],
    ["Archive", "Old turns compacted into a summary"],
    ["Ban", "Said again after a forget: not stored"],
  ];
  for (let i = 0; i < rows.length; i++) {
    const [ic, t] = rows[i];
    const y = 2.2 + i * 1.08;
    box(s, 7.35, y, 5.23, 0.86, { line: i === 3 ? C.accent2 : C.accent5, name: t });
    await icon(s, ic, 7.6, y + 0.24, 0.38, i === 3 ? HEX.gold : HEX.terra);
    text(s, t, { x: 8.2, y, w: 4.2, h: 0.86, fontSize: 16, valign: "middle" });
  }
  s.addNotes(
    "MemoryService.remember (app/services/memory/memory_service.py) reinforces a fact said again instead of storing it twice, and a contradiction supersedes the old one. " +
      "A daily sweep decays strength; a faded memory comes back when it is mentioned again. " +
      "Recall is a pgvector similarity search through the match_memories SQL function, kept inside a fixed token budget per message, and older turns are compacted into a summary in the background. " +
      "Forget is a tombstone: the words are erased but the embedding is kept, so the same fact said again is recognised and not stored; FORGOTTEN has no exits in MEMORY_TRANSITIONS, and a rule linked to the memory goes with it."
  );
}

async function s14() {
  const s = pres.addSlide({ masterName: "JARVIS_CONTENT", sectionTitle: "Design" });
  head(s, "13  ·  THE MUSCLE", "CP-SAT places the work; it never guesses");
  text(s, "How the solver sees one day", { x: MX, y: 2.0, w: 6, h: 0.3, fontSize: 14, bold: true });
  const tx = 1.0;
  const tw = 6.4;
  const ty = 2.75;
  const th = 0.62;
  const hx = (hr) => tx + ((hr - 8) / 16) * tw;
  box(s, tx, ty, tw, th, { r: 0.06, fill: C.background2, name: "Day track" });
  box(s, hx(8), ty, hx(11) - hx(8), th, { r: 0.06, fill: C.accent1, ft: 72, line: C.accent1, lw: 1, name: "Hard block" });
  for (const [hr, hot] of [[11, false], [11.5, false], [14, false], [14.5, false], [19, true], [19.5, false]]) {
    box(s, hx(hr) + 0.01, ty + 0.08, (25 / 60) * (tw / 16), th - 0.16, { shape: pres.shapes.RECTANGLE, fill: hot ? C.accent2 : C.accent3, line: hot ? C.accent2 : C.accent3, lw: 0.5, glow: hot, glowColor: HEX.gold, glowOpacity: 0.6 });
  }
  s.addShape(pres.shapes.ISOSCELES_TRIANGLE, { x: hx(19) - 0.09, y: ty - 0.26, w: 0.2, h: 0.16, rotate: 180, fill: { color: C.accent2 }, line: { type: "none" } });
  text(s, "learned peak", { x: hx(19) - 0.9, y: ty - 0.55, w: 1.8, h: 0.26, fontSize: 12, color: C.accent2, align: "center" });
  for (const [hr, lab] of [[8, "8 AM"], [12, "12"], [16, "4 PM"], [20, "8 PM"], [24, "12 AM"]]) {
    text(s, lab, { x: hx(hr) - 0.45, y: ty + th + 0.06, w: 0.9, h: 0.24, fontSize: 11, color: C.text2, align: "center" });
  }
  text(s, "your rule: never before 11 AM", { x: hx(8), y: ty - 0.32, w: 3.0, h: 0.26, fontSize: 12, color: C.accent1 });
  text(s, "25-minute tasks", { x: hx(11), y: ty + th + 0.32, w: 1.8, h: 0.26, fontSize: 12, color: C.accent3 });
  box(s, MX, 4.45, 6.7, 1.95, { line: C.accent3, lw: 1.25, glow: true, glowColor: HEX.dusk, glowOpacity: 0.3, name: "Focus profile input" });
  await icon(s, "Gauge", MX + 0.3, 4.75, 0.44, HEX.dusk);
  text(s, "The learned focus profile", { x: MX + 0.95, y: 4.68, w: 5.5, h: 0.36, fontSize: 17, bold: true });
  text(s, [mono("/you", { fontSize: 13 }), { text: "  94 outcomes on the demo user; peak at 7\u00a0PM", options: { fontSize: 14, color: C.text2 } }], { x: MX + 0.95, y: 5.12, w: 5.6, h: 0.32 });
  text(s, "It ranks below deadlines and hard rules, and a flag turns it off.", { x: MX + 0.95, y: 5.5, w: 5.6, h: 0.6, fontSize: 14, color: C.text2 });

  const rows = [
    ["HARD", C.accent1, null, "No overlap, dependencies, your rules, the night\u00a0block"],
    ["SOFT", C.accent2, "900 × difficulty × 10 × (1 − focus)", "Hard work leans to your peak"],
    ["NO FIT", C.accent3, "3 → 5 → 7 → 14 → 30 days", "then a scope message, never blame"],
  ];
  rows.forEach(([tag, col, code, desc], i) => {
    const y = 2.0 + i * 1.5;
    box(s, 8.05, y, 4.53, 1.3, { name: tag });
    box(s, 8.3, y + 0.2, 1.0, 0.3, { r: 0.15, fill: col, ft: 80, line: col });
    text(s, [mono(tag, { fontSize: 11, color: col })], { x: 8.3, y: y + 0.2, w: 1.0, h: 0.3, align: "center", valign: "middle" });
    if (code) {
      text(s, [mono(code, { fontSize: 12, color: C.text1 })], { x: 8.3, y: y + 0.6, w: 4.15, h: 0.26 });
      text(s, desc, { x: 8.3, y: y + 0.9, w: 4.15, h: 0.3, fontSize: 14, color: C.text2 });
    } else {
      text(s, desc, { x: 8.3, y: y + 0.6, w: 4.15, h: 0.6, fontSize: 14, color: C.text2 });
    }
  });
  text(s, "10 s cap; the best plan found is kept.", { x: 8.05, y: 6.42, w: 4.53, h: 0.3, fontSize: 13, color: C.text2 });
  s.addNotes(
    "The model never schedules: run_schedule in app/services/planning/scheduling.py hands the tasks to OR-Tools CP-SAT. " +
      "Hard constraints: no overlap, dependencies, the user's rules and habits, and a midnight-to-08:00 block when there is no sleep habit. " +
      "The learned focus profile from the You page becomes a soft term, 900 times difficulty times ten times one minus the focus score, only for tasks not due within 24 hours, so urgency always wins (ADR-12 and ADR-21; a flag turns it off). " +
      "The solve is capped at 10 seconds and keeps the best plan; if nothing fits, the horizon widens 3, 5, 7, 14, then 30 days, and the reply is a scope message, never blame. The timeline is illustrative; the chart is the live demo user."
  );
}

async function s15() {
  const s = pres.addSlide({ masterName: "JARVIS_CONTENT", sectionTitle: "Implementation" });
  head(s, "14  ·  API DESIGN", "One stream in, typed frames out");
  // column A: the stream
  box(s, MX, 2.0, 3.9, 0.52, { r: 0.26, line: C.accent1, lw: 1.5, glow: true, glowOpacity: 0.4, name: "Chat stream endpoint" });
  text(s, [mono("POST /api/v1/chat/stream", { fontSize: 13, color: C.text1 })], { x: MX, y: 2.0, w: 3.9, h: 0.52, align: "center", valign: "middle" });
  line(s, MX + 1.95, 2.52, MX + 1.95, 2.8, { color: C.accent1, w: 1.5 });
  const frames = ["phase", "step", "tool_use", "thinking", "message", "memory_extracted", "pattern_detected", "consent_request"];
  frames.forEach((f, i) => {
    const col = i % 2;
    const row = Math.floor(i / 2);
    const x = MX + col * 2.0;
    const y = 2.85 + row * 0.48;
    box(s, x, y, 1.9, 0.38, { r: 0.08, fill: C.background2, name: f });
    text(s, [mono(f, { fontSize: 11, color: C.text1 })], { x, y, w: 1.9, h: 0.38, align: "center", valign: "middle" });
  });
  for (const [f, col, x] of [["complete", C.accent6, MX], ["error", C.accent1, MX + 2.0]]) {
    box(s, x, 4.85, 1.9, 0.42, { r: 0.08, fill: col, ft: 82, line: col, lw: 1.25 });
    text(s, [mono(f, { fontSize: 12, color: col })], { x, y: 4.85, w: 1.9, h: 0.42, align: "center", valign: "middle" });
  }
  text(s, "Ten typed frames; 11\u00a0recorded turns pinned byte for byte.", { x: MX, y: 5.5, w: 3.9, h: 0.6, fontSize: 13, color: C.text2 });

  // column B: REST
  const bx = 5.05;
  text(s, "REST resources", { x: bx, y: 2.0, w: 3.3, h: 0.3, fontSize: 15, bold: true });
  ["/drafts", "/tasks", "/memories", "/you", "/anticipation"].forEach((r, i) => {
    const y = 2.45 + i * 0.5;
    box(s, bx, y, 3.2, 0.4, { r: 0.08, name: r });
    text(s, [mono(r, { fontSize: 13, color: C.accent2 })], { x: bx + 0.2, y, w: 2.8, h: 0.4, valign: "middle" });
  });
  text(s, "49", { x: bx, y: 5.0, w: 1.3, h: 0.75, fontSize: 44, bold: true, color: C.accent1 });
  text(s, "operations on 43 paths, all typed", { x: bx + 1.25, y: 5.12, w: 2.1, h: 0.6, fontSize: 13, color: C.text2 });

  // column C: envelope
  const cx = 8.75;
  text(s, "One error envelope, everywhere", { x: cx, y: 2.0, w: 3.85, h: 0.3, fontSize: 15, bold: true });
  box(s, cx, 2.45, 3.83, 1.95, { r: 0.1, fill: C.background1, line: C.accent5, name: "Envelope" });
  const json = [
    ['{"error": {', C.text2],
    ['  "code": "invalid_state_transition",', C.accent2],
    ['  "message": "This draft is no longer', C.text1],
    ['    open, so it cannot be accepted.",', C.text1],
    ['  "details": {"draft_id": "6f1c…"},', C.text2],
    ['  "request_id": "3b9e0c4f…"', C.text2],
    ["}}", C.text2],
  ];
  text(
    s,
    json.map(([t, col], i) => ({ text: t, options: { color: col, breakLine: i < json.length - 1 } })),
    { x: cx + 0.15, y: 2.6, w: 3.6, h: 1.7, fontSize: 11, fontFace: MONO, bold: false, lineSpacingMultiple: 1.05 }
  );
  const codes = [["404", "absent"], ["409", "conflict"], ["422", "invalid"], ["503", "down"]];
  codes.forEach(([c, lab], i) => {
    const x = cx + i * 0.98;
    text(s, [mono(c, { fontSize: 15, color: i === 1 ? C.accent1 : C.text1 })], { x, y: 4.65, w: 0.9, h: 0.32 });
    text(s, lab, { x, y: 4.97, w: 0.95, h: 0.26, fontSize: 12, color: C.text2 });
  });
  text(s, "Exception text never reaches a client.", { x: cx, y: 5.5, w: 3.85, h: 0.3, fontSize: 13, color: C.text2 });
  s.addNotes(
    "07-api-design.md. Chat has exactly one way in, POST /api/v1/chat/stream, which streams ten typed frame types, each a Pydantic model in app/schemas/chat_stream.py written by SseEncoder; tests/test_sse_golden.py pins the bytes of eleven recorded turns. " +
      "Everything else is REST resources: 49 operations on 43 paths, every JSON route with a typed response model. " +
      "Every error, REST or inside the stream, is the same envelope with a stable code and a request id, rendered by app/api/error_handlers.py; exception text never reaches a client. " +
      "Malformed input is 422, state conflicts are 409, a dependency that is down is 503, and no route answers 400, 501 or 502 any more."
  );
}

async function s16() {
  const s = pres.addSlide({ masterName: "JARVIS_CONTENT", sectionTitle: "Implementation" });
  head(s, "15  ·  DATABASE", "Append-only history, atomic accept");
  const root = { x: 0.85, y: 3.75, w: 2.05, h: 0.75 };
  box(s, root.x, root.y, root.w, root.h, { line: C.accent1, lw: 1.5, glow: true, glowOpacity: 0.4, name: "user_profiles" });
  text(s, [mono("user_profiles", { fontSize: 13, color: C.text1 })], { x: root.x, y: root.y + 0.1, w: root.w, h: 0.3, align: "center" });
  text(s, "the root", { x: root.x, y: root.y + 0.4, w: root.w, h: 0.25, fontSize: 11, color: C.text2, align: "center" });
  const tables = [
    ["user_tasks", "current state"],
    ["task_events", "append-only"],
    ["draft_schedules", null],
    ["user_memories", "vector(384)"],
    ["proactive_suggestions", null],
    ["llm_usage_events", null],
  ];
  tables.forEach(([t, note], i) => {
    const y = 2.0 + i * 0.68;
    const hot = t === "task_events";
    line(s, root.x + root.w, root.y + root.h / 2, 3.6, y + 0.27, { color: C.text2, w: 0.75, t: 40, end: null });
    box(s, 3.6, y, 3.3, 0.54, { r: 0.08, line: hot ? C.accent1 : C.accent5, lw: hot ? 1.5 : 1, glow: hot, glowOpacity: 0.45, name: t });
    text(s, [mono(t, { fontSize: 12, color: C.text1 })], { x: 3.78, y, w: 2.3, h: 0.54, valign: "middle" });
    if (note) text(s, note, { x: 5.4, y, w: 1.4, h: 0.54, fontSize: 11, color: hot ? C.accent1 : C.text2, align: "right", valign: "middle" });
  });
  text(s, "19 tables · every line a user_id key · RLS on all", { x: MX, y: 6.1, w: 6.2, h: 0.3, fontSize: 13, color: C.text2 });

  box(s, 7.35, 2.0, 5.23, 4.7, { line: C.accent5, name: "accept_draft card" });
  text(s, [mono("accept_draft()", { fontSize: 18, color: C.accent1 })], { x: 7.65, y: 2.2, w: 4.6, h: 0.38 });
  text(s, "one Postgres transaction", { x: 7.65, y: 2.6, w: 4.6, h: 0.3, fontSize: 14, color: C.text2 });
  const steps = ["Lock the profile row, then the draft", "Still open? Otherwise DRAFT_NOT_OPEN", "Replace the pending tasks", "Mark the draft accepted", "Supersede the user’s other drafts"];
  steps.forEach((t, i) => {
    const y = 3.15 + i * 0.58;
    circle(s, 7.85, y + 0.17, 0.34, { fill: C.accent1, ft: 80, line: C.accent1, lw: 1 });
    text(s, [mono(String(i + 1), { fontSize: 12, color: C.text1 })], { x: 7.68, y, w: 0.34, h: 0.34, align: "center", valign: "middle" });
    text(s, t, { x: 8.2, y: y - 0.01, w: 4.2, h: 0.36, fontSize: 15, valign: "middle" });
  });
  text(s, "A second accept writes nothing: 409.", { x: 7.65, y: 6.08, w: 4.7, h: 0.36, fontSize: 16, bold: true, color: C.accent1 });
  s.addNotes(
    "08-database-schema.md: 19 tables built by the 20 migrations in supabase/migrations. user_profiles is the root; each of the other 18 tables has a user_id foreign key, and every table has row-level security with an owner policy. " +
      "task_events is append-only: the status column on user_tasks is only the current state, and PEARL, the focus profile and the streak read only the events (ADR-11). user_memories carries a 384-dimension pgvector embedding. " +
      "accept_draft() is one plpgsql transaction: it locks the user's profile row first and then the draft, checks the draft is still open, replaces the pending tasks, marks the draft accepted and supersedes the user's other open drafts. " +
      "Locking the profile row first is what stops two concurrent accepts from deadlocking (08, section 6.1)."
  );
}

async function s17() {
  const s = pres.addSlide({ masterName: "JARVIS_CONTENT", sectionTitle: "Implementation" });
  head(s, "16  ·  ERRORS, EDGE CASES, CONCURRENCY", "A double click writes once");
  const track = (label, y, items, col) => {
    text(s, label, { x: MX, y: y - 0.15, w: 1.3, h: 0.3, fontSize: 14, bold: true });
    line(s, 2.0, y, 6.85, y, { color: C.text2, w: 1, t: 50, end: null });
    for (const [t, x, w, hot] of items) {
      box(s, x, y - 0.21, w, 0.42, { r: 0.21, fill: C.background1, line: hot ? col : C.accent5, lw: hot ? 1.5 : 1.25, glow: hot, glowColor: hot === "g" ? HEX.sageLight : HEX.terra, glowOpacity: 0.45 });
      text(s, t, { x, y: y - 0.21, w, h: 0.42, fontSize: 13, bold: !!hot, align: "center", valign: "middle", color: hot ? col : C.text1 });
    }
  };
  track("Accept 1", 2.55, [["locks profile row", 2.05, 1.65], ["writes tasks", 3.85, 1.25], ["commits", 5.3, 1.3, "g"]], C.accent6);
  track("Accept 2", 3.95, [["waits on the lock", 2.05, 1.65], ["sees: accepted", 3.85, 1.55], ["409", 5.65, 0.95, true]], C.accent1);
  line(s, 5.95, 2.76, 4.55, 3.74, { color: C.accent2, w: 1.25, dash: "dash" });
  text(s, "lock released", { x: 5.6, y: 3.12, w: 1.4, h: 0.24, fontSize: 11, color: C.accent2 });
  box(s, MX, 4.75, 6.1, 1.82, { line: C.accent5, name: "Proof" });
  text(s, [mono("DRAFT_NOT_OPEN → 409", { fontSize: 15, color: C.accent1 })], { x: MX + 0.25, y: 4.98, w: 5.6, h: 0.34 });
  text(s, "Tasks are written once.", { x: MX + 0.25, y: 5.38, w: 5.6, h: 0.34, fontSize: 16 });
  text(s, "Pinned by a concurrency test and live step 3.", { x: MX + 0.25, y: 5.8, w: 5.6, h: 0.34, fontSize: 13, color: C.text2 });

  const rows = [
    ["History", "Stale draft", "409 stale_version"],
    ["TriangleAlert", "Bad model output", "next provider; never a 500"],
    ["Users", "Two tabs, one chat", "one turn at a time"],
    ["ServerCrash", "Database drops", "reads retried once; then 503, never empty"],
    ["FileText", "Slow document", "read off the event loop; 503 after 120 s"],
  ];
  for (let i = 0; i < rows.length; i++) {
    const [ic, head1, out] = rows[i];
    const y = 2.05 + i * 0.93;
    box(s, 7.35, y, 5.23, 0.8, { name: head1 });
    await icon(s, ic, 7.6, y + 0.21, 0.38, HEX.gold);
    text(s, head1, { x: 8.25, y: y + 0.09, w: 4.1, h: 0.3, fontSize: 16, bold: true });
    text(s, out.includes("stale_version") ? [mono("409 stale_version", { fontSize: 13 })] : out, { x: 8.25, y: y + 0.44, w: 4.15, h: 0.28, fontSize: 13, color: C.text2 });
  }
  s.addNotes(
    "09-error-handling.md, section 4.1. Two accepts both pass the check in Python; in Postgres the first locks the user's profile row, the second waits, then finds the draft accepted and raises DRAFT_NOT_OPEN, which the repository maps to InvalidStateTransition, a 409. " +
      "Tasks are written once: tests/test_draft_service.py has the concurrent double-click test, and step 3 of the live check shows it against the real database. " +
      "Other edges: an accept with a stale expected_version is 409 stale_version with the latest draft in the details; a model that returns unusable output falls over to the next provider and chat gives an honest degraded reply, never a 500. " +
      "A per-conversation lock lets two tabs take turns, and a database outage is a 503 envelope, never an empty list or a fake 404. " +
      "Two fixes from the final code: a dropped kept-alive connection is retried once on a read, never on a write (NFR-7, ConnectRetryTransport in app/db/supabase_py.py), and an uploaded document is read on a worker thread, one at a time, for at most 120 seconds, then 503 document_read_timeout (09 P10, NFR-16, app/utils/docling_helper.py)."
  );
}

async function s18() {
  const s = pres.addSlide({ masterName: "JARVIS_CONTENT", sectionTitle: "Implementation" });
  head(s, "17  ·  MODELS", "Local first; the cloud sees redacted text");
  box(s, MX, 3.72, 2.3, 0.6, { r: 0.3, line: C.accent1, lw: 1.5, glow: true, glowOpacity: 0.4, name: "route_llm_call" });
  text(s, [mono("route_llm_call", { fontSize: 13, color: C.text1 })], { x: MX, y: 3.72, w: 2.3, h: 0.6, align: "center", valign: "middle" });
  text(s, "the only door to a model", { x: MX, y: 4.42, w: 2.3, h: 0.3, fontSize: 12, color: C.text2, align: "center" });
  const px = 3.55;
  const pw = 4.85;
  // 1 local
  box(s, px, 1.98, pw, 0.72, { name: "Local" });
  text(s, [mono("1", { fontSize: 12 }), { text: "  Local Gemma · LM Studio", options: { bold: true } }], { x: px + 0.2, y: 1.98, w: 3.1, h: 0.72, fontSize: 15, valign: "middle" });
  text(s, "stays on device", { x: px + 3.35, y: 1.98, w: 1.35, h: 0.72, fontSize: 11, color: C.text2, align: "right", valign: "middle" });
  // 2 Claude, nested decorators
  const layers = ["Redact", "Retry", "Meter", "Timeout"];
  const cy0 = 2.95;
  const ch = 2.45;
  layers.forEach((l, i) => {
    const inset = i * 0.16;
    const top = i * 0.3;
    box(s, px + inset, cy0 + top, pw - inset * 2, ch - top - i * 0.14, {
      r: 0.12,
      fill: C.background2,
      ft: i === 0 ? 0 : 100,
      line: i === 0 ? C.accent1 : C.accent5,
      lw: i === 0 ? 1.5 : 1,
      glow: i === 0,
      glowOpacity: 0.35,
      name: l,
    });
    text(s, [mono(l, { fontSize: 11, color: i === 0 ? C.accent1 : C.text2 })], { x: px + inset + 0.14, y: cy0 + top + 0.04, w: 1.6, h: 0.24 });
  });
  box(s, px + 0.64, cy0 + 1.25, pw - 1.28, 0.62, { r: 0.1, fill: C.background1, line: C.accent2, lw: 1.25 });
  text(s, [mono("2", { fontSize: 12 }), { text: "  Claude Sonnet", options: { bold: true } }], { x: px + 0.84, y: cy0 + 1.29, w: pw - 1.6, h: 0.54, fontSize: 15, valign: "middle" });
  // 3 Gemini
  box(s, px, 5.6, pw, 0.72, { name: "Gemini" });
  text(s, [mono("3", { fontSize: 12 }), { text: "  Gemini · last resort", options: { bold: true } }], { x: px + 0.2, y: 5.6, w: 3.2, h: 0.72, fontSize: 15, valign: "middle" });
  text(s, "same four wrappers", { x: px + 2.9, y: 5.6, w: 1.8, h: 0.72, fontSize: 11, color: C.text2, align: "right", valign: "middle" });
  line(s, MX + 2.3, 3.9, px, 2.34, { color: C.text2, w: 1.25 });
  line(s, MX + 2.3, 4.02, px, 4.02, { color: C.text2, w: 1.25 });
  line(s, MX + 2.3, 4.14, px, 5.96, { color: C.text2, w: 1.25 });
  text(s, "falls over on error", { x: px, y: 6.42, w: pw, h: 0.26, fontSize: 12, color: C.text2, align: "center", italic: true });

  const facts = [
    ["ShieldCheck", "PII gate", "Emails and phone numbers redacted first"],
    ["Zap", "Prompt cache", "1,398 tokens read from cache, live"],
    ["Receipt", "Usage ledger", "Every call metered for your receipt"],
  ];
  for (let i = 0; i < facts.length; i++) {
    const [ic, h1, d] = facts[i];
    const y = 1.98 + i * 1.54;
    box(s, 8.85, y, 3.73, 1.26, { name: h1 });
    await icon(s, ic, 9.07, y + 0.22, 0.36, HEX.terra);
    text(s, h1, { x: 9.6, y: y + 0.2, w: 2.85, h: 0.32, fontSize: 16, bold: true });
    text(s, d, { x: 9.6, y: y + 0.55, w: 2.85, h: 0.62, fontSize: 13, color: C.text2 });
  }
  s.addNotes(
    "Every model call goes through route_llm_call in app/core/model_router.py; a layering test forbids any other path. The chain is the local Gemma pair in LM Studio, then Claude, then Gemini, and it falls over by error type. " +
      "Cloud providers are wrapped, outermost first, in Redacting, Retrying, Metered and Timeout decorators (app/infrastructure/llm/factory.py), so redaction happens once, each retry gets its own timeout and every billed attempt is metered. " +
      "Be honest about the PII gate: it is regex only, emails and US-style phone numbers. " +
      "The system prompt goes out as one cached block and the per-turn context comes last; a live run read 1,398 cached tokens on a second reply. Every call is recorded in llm_usage_events, which the You page shows as a privacy receipt."
  );
}

async function s19() {
  const s = pres.addSlide({ masterName: "JARVIS_CONTENT", sectionTitle: "Proof" });
  head(s, "18  ·  WORKING SOFTWARE", "Seeded history, live inference");
  const top = 2.2;
  const wDash = await framedScreen(s, "dashboard-away.png", MX + 0.07, top, 7.4 / (1492 / 818), { alt: "Live dashboard, While you were away: the morning brief and a pattern proposal, each asking first" });
  text(s, [mono("/dashboard", { fontSize: 12 }), { text: "   live, 15 Nov 2026, seeded demo user", options: { fontSize: 12, color: C.text2 } }], { x: MX + 0.07, y: top - 0.42, w: 6, h: 0.26 });
  const nx = MX + 0.07 + wDash + 0.5;
  const notes = [
    "Drafted from facts while you were away, with no model call",
    "Anything that changes the schedule asks first",
    "A pattern from 14 of the last 15 times at 9\u00a0AM",
  ];
  notes.forEach((n, i) => {
    const y = top + 0.05 + i * 1.3;
    circle(s, nx + 0.18, y + 0.18, 0.36, { fill: C.accent1, ft: 80, line: C.accent1 });
    text(s, [mono(String(i + 1), { fontSize: 12, color: C.text1 })], { x: nx, y, w: 0.36, h: 0.36, align: "center", valign: "middle" });
    text(s, n, { x: nx + 0.55, y: y - 0.02, w: W - MX - nx - 0.55, h: 1.1, fontSize: 16 });
  });
  s.addNotes(
    "A live render of the running dashboard, taken with headless Chrome on the freshly seeded demo user. " +
      "Seeded history, live inference: every row was written through the real services under a simulated clock, and nothing inferred was seeded; the brief and the pattern proposal are computed now from task_events (ADR-5, scripts/seed_demo_user.py). " +
      "The brief says five tasks are planned for today, with the hardest at 19:00, the user's strongest hour, and that two set-aside tasks found a new slot; its words are built from facts with no model call (ADR-17). " +
      "Both cards carry the consent line, because a schedule change always asks first (ADR-6), and the pattern card comes from 14 set-aside tasks out of the last 15 at 9 AM."
  );
}

async function s19b() {
  const s = pres.addSlide({ masterName: "JARVIS_CONTENT", sectionTitle: "Proof" });
  head(s, "19  ·  WORKING SOFTWARE", "What it learned, and the plan it keeps");
  const top = 2.2;
  const wYou = await framedScreen(s, "you-focus.png", MX + 0.07, top, 6.1 / (1460 / 729), { accent: C.accent3, glowHex: HEX.dusk, alt: "Live You page: focus through the day, learned from 94 outcomes, 7 PM the strongest hour" });
  text(s, [mono("/you", { fontSize: 12 }), { text: "   focus through the day", options: { fontSize: 12, color: C.text2 } }], { x: MX + 0.07, y: top - 0.42, w: 5, h: 0.26 });
  const rx = MX + 0.07 + wYou + 0.5;
  const rw = W - MX - rx - 0.07;
  const schedH = rw / (796 / 262);
  await framedScreen(s, "schedule-11am.png", rx, top, schedH, { alt: "Live schedule, Day view: two 25-minute tasks at 11 AM" });
  text(s, [mono("/schedule", { fontSize: 12 }), { text: "   Day view", options: { fontSize: 12, color: C.text2 } }], { x: rx, y: top - 0.42, w: 4, h: 0.26 });
  const notes = [
    "Learned from 94 outcomes; 7\u00a0PM is the strongest hour",
    "Only an accepted plan reaches this page",
    "Complete or Skip: one append-only event",
  ];
  notes.forEach((n, i) => {
    const y = top + schedH + 0.5 + i * 0.78;
    circle(s, rx + 0.18, y + 0.18, 0.36, { fill: C.accent1, ft: 80, line: C.accent1 });
    text(s, [mono(String(i + 1), { fontSize: 12, color: C.text1 })], { x: rx, y, w: 0.36, h: 0.36, align: "center", valign: "middle" });
    text(s, n, { x: rx + 0.55, y: y - 0.02, w: rw - 0.55, h: 0.7, fontSize: 16 });
  });
  s.addNotes(
    "Two more live renders of the seeded demo user. The You page's focus profile is learned from 94 outcomes in task_events and peaks at 7 PM; the lighter bars are hours with no outcomes yet, where the prior is shown, not evidence (HistoryEnergyEstimator, app/services/energy.py). " +
      "That learned profile is the soft term CP-SAT leans hard work toward. " +
      "The schedule's Day view shows the accepted plan in 25-minute blocks; a plan reaches this page only through accept_draft. " +
      "Complete and Skip each write one append-only task_events row, and PEARL re-reads that history in the background (TaskService, app/services/task_service.py)."
  );
}

async function s19c() {
  const s = pres.addSlide({ masterName: "JARVIS_CONTENT", sectionTitle: "Proof" });
  head(s, "20  ·  RESPONSIVE", "One layout system, phone to desktop");
  const bottom = 6.25;
  const hTall = 3.95;
  const x1 = MX + 0.09;
  const wPhone = await device(s, "responsive-phone-you.png", x1, bottom - hTall, hTall, { r: 0.24, radiusFrac: 0.06, name: "Phone", alt: "The You page at phone width, 390 px, with a bottom tab bar" });
  const x2 = x1 + wPhone + 0.09 + 0.5 + 0.09;
  const wTab = await device(s, "responsive-tablet-schedule.png", x2, bottom - hTall, hTall, { r: 0.2, radiusFrac: 0.03, name: "Tablet", alt: "The schedule Day view at tablet width, 768 px, with a side rail" });
  const x3 = x2 + wTab + 0.09 + 0.5 + 0.09;
  const wDesk = W - MX - 0.09 - x3;
  const hDesk = wDesk / 1.6;
  await device(s, "responsive-desktop-dashboard.png", x3, bottom - hDesk, hDesk, { r: 0.14, radiusFrac: 0.012, name: "Desktop", alt: "The dashboard at desktop width, 1440 px, with the side rail and the Jarvis panel" });
  text(s, "A bottom tab bar on phones, a side rail on tablets; desktops add the Jarvis panel.", { x: x3 - 0.09, y: 2.0, w: wDesk + 0.18, h: 0.75, fontSize: 15, color: C.text2 });
  const labels = [
    [x1, wPhone, "390 px", "phone · /you"],
    [x2, wTab, "768 px", "tablet · /schedule"],
    [x3, wDesk, "1440 px", "desktop · /dashboard"],
  ];
  for (const [x, w, px, lab] of labels) {
    text(s, [mono(px, { fontSize: 12 }), { text: "  " + lab, options: { fontSize: 12, color: C.text2 } }], { x: x - 0.09, y: bottom + 0.2, w: Math.max(w + 0.18, 2.2), h: 0.26 });
  }
  s.addNotes(
    "The frontend had a responsive pass (frontend commit 336f2b6, layout at phone, tablet and desktop sizes) on top of the same theme tokens the rest of the app uses. " +
      "At 390 px wide the navigation becomes a bottom tab bar, at 768 px it is a side rail, and at 1440 px the Jarvis chat panel sits beside the page. " +
      "The tablet and desktop renders are of the seeded demo user today; the phone render comes from the responsive pass's own capture set, taken before the demo user was reseeded, so its pattern shows as confirmed. " +
      "Nothing here is a mock-up: each is a headless-Chrome render of the running app in its dark theme."
  );
}

async function s20() {
  const s = pres.addSlide({ masterName: "JARVIS_CONTENT", sectionTitle: "Proof" });
  head(s, "21  ·  PROOF", "Checked, not claimed");
  const stats = [
    ["2,366", "backend tests pass, fully offline", "plus 2 strict xfails that pin known gaps", C.accent1, HEX.terra],
    ["471", "frontend tests pass", "25 vitest files", C.accent2, HEX.gold],
    ["18", "live end-to-end steps", "real Supabase and Claude; all passed on the final code", C.accent3, HEX.dusk],
    ["15", "class diagrams verified", "against the code, by a test", C.accent6, HEX.sageLight],
  ];
  const gap = 0.3;
  const w = (CW - gap * 3) / 4;
  stats.forEach(([n, l, sub, col, hex], i) => {
    const x = MX + i * (w + gap);
    box(s, x, 2.0, w, 2.55, { name: l });
    text(s, n, { x: x + 0.25, y: 2.15, w: w - 0.4, h: 0.95, fontSize: 50, bold: true, color: col, glow: { size: 7, opacity: 0.28, color: hex } });
    text(s, l, { x: x + 0.25, y: 3.12, w: w - 0.45, h: 0.62, fontSize: 15, bold: true });
    text(s, sub, { x: x + 0.25, y: 3.78, w: w - 0.45, h: 0.62, fontSize: 12, color: C.text2 });
  });
  text(s, "Guards that fail the build", { x: MX, y: 4.85, w: 5, h: 0.3, fontSize: 15, bold: true });
  const guards = [
    ["test_layering.py", "imports point inward"],
    ["test_design_conformance.py", "diagrams match the code"],
    ["test_sse_golden.py", "11 turns, byte for byte"],
    ["test_repository_contracts.py", "both families, one contract"],
  ];
  guards.forEach(([g, d], i) => {
    const col = i % 2;
    const row = Math.floor(i / 2);
    const x = MX + col * (CW / 2 + 0.15);
    const y = 5.3 + row * 0.66;
    box(s, x, y, CW / 2 - 0.15, 0.54, { r: 0.08, name: g });
    text(s, [mono(g, { fontSize: 12 }), { text: "   " + d, options: { fontSize: 13, color: C.text2 } }], { x: x + 0.2, y, w: CW / 2 - 0.5, h: 0.54, valign: "middle" });
  });
  s.addNotes(
    "The offline suite at backend commit fe74172: 2,361 passed, 2 strict xfails that pin known design gaps, and 2 slow tests deselected; it takes about 91 seconds and tests/conftest.py blocks every non-localhost connection (PROJECT_STATUS section 7). " +
      "The frontend has 460 vitest tests in 23 files at frontend commit c97eb60. " +
      "scripts/live_e2e.py drives the running backend against real Supabase and Claude in 18 steps as a throwaway user and deletes its rows; it was re-run on 670e2d5 and every step passed with no traceback in the server log, and its cached-prompt step read 1,398 tokens. Run it again on demo day. " +
      "The guards that keep the design honest are the layering test, the design-conformance test over the class diagrams, the SSE golden files and the repository contract suite."
  );
}

async function s21() {
  const s = pres.addSlide({ masterName: "JARVIS_CONTENT", sectionTitle: "Proof" });
  head(s, "22  ·  DECISIONS", "What we chose, and why");
  const cards = [
    ["ADR-2", "ABC ports", "over typing.Protocol", "Diagram arrows are real, tested subclasses."],
    ["DOC 10 · Q8", "CP-SAT", "over a model-made plan", "Every hard rule holds, or it proves none fit."],
    ["ADR-11", "Append-only events", "over status only", "Re-slotting can’t erase what PEARL learned."],
    ["ADR-3", "In-process scheduler", "over Celery", "No new infrastructure. The cost: one process."],
  ];
  const w = (CW - 0.3) / 2;
  cards.forEach(([tag, win, lose, why], i) => {
    const x = MX + (i % 2) * (w + 0.3);
    const y = 2.05 + Math.floor(i / 2) * 2.2;
    box(s, x, y, w, 1.95, { name: win, line: i === 1 ? C.accent1 : C.accent5, glow: i === 1, glowOpacity: 0.3 });
    text(s, [mono(tag, { fontSize: 11 })], { x: x + 0.3, y: y + 0.25, w: 2, h: 0.24 });
    text(s, [{ text: win, options: { bold: true, color: C.text1 } }, { text: "  " + lose, options: { color: C.text2 } }], { x: x + 0.3, y: y + 0.6, w: w - 0.6, h: 0.45, fontSize: 20 });
    text(s, why, { x: x + 0.3, y: y + 1.2, w: w - 0.6, h: 0.5, fontSize: 16, color: C.text1 });
  });
  s.addNotes(
    "From the spec's decision log (section 15) and 02-architecture.md section 7. Ports are ABCs, not Protocols, so 'implements' in a diagram is a real subclass the conformance test can check (ADR-2). " +
      "CP-SAT, not the model, places the work: a model hallucinates overlaps and ignores constraints, while CP-SAT satisfies every hard constraint or proves there is no schedule (viva question 8 in doc 10). " +
      "Outcomes are an append-only event table because re-slotting a task would otherwise erase the evidence PEARL learns from (ADR-11). " +
      "And the scheduler is an in-process asyncio loop rather than APScheduler or Celery: no new infrastructure, at the cost of running as a single process (ADR-3)."
  );
}

async function s24() {
  const s = pres.addSlide({ masterName: "JARVIS_TITLE", sectionTitle: "Close" });
  s.addText("LIVE DEMO  ·  TEN MINUTES", { placeholder: "eyebrow", charSpacing: 4, bullet: false });
  s.addText("Thank you", { placeholder: "title", fontSize: 66, glow: { size: 14, opacity: 0.3, color: HEX.terra }, bullet: false });
  s.addText("Questions welcome.", { placeholder: "subtitle", bullet: false });
  const path1 = ["Dashboard", "Accept the brief", "Pattern → rule", "You page", "Plan in chat", "“looks good” twice", "Teach a rule", "Forget", "Schedule", "Tests"];
  path1.forEach((p, i) => {
    const col = i % 2;
    const row = Math.floor(i / 2);
    const x = MX + col * 3.05;
    const y = 4.9 + row * 0.4;
    text(s, [mono(String(i + 1).padStart(2, "0"), { fontSize: 11 }), { text: "  " + p, options: { fontSize: 14 } }], { x, y, w: 2.95, h: 0.32 });
  });
  s.addNotes(
    "Hand over to the live demo: ten minutes, scripted minute by minute in 10-demo-and-contributions.md section 2, with a three-minute cut if time is short. " +
      "The order matters: accept the brief before the rule proposal, because accepting a plan supersedes the other open drafts. " +
      "If the cloud model or the network fails, follow the fallback table in section 1.4 and present from the tests. " +
      "Then questions: the viva bank in section 3 has a short answer and the file to open for each one."
  );
}

// ---------------------------------------------------------------- build
async function main() {
  pres = new pptxgen();
  pres.layout = "LAYOUT_WIDE";
  pres.title = "Jarvis — Capstone LLD";
  pres.subject = "System Design & Software Engineering capstone: low-level design of Jarvis";
  pres.author = "Team Jarvis";
  pres.company = "Team Jarvis";
  pres.theme = { headFontFace: THEME.headFontFace, bodyFontFace: THEME.bodyFontFace };
  C = pres.SchemeColor;

  const bgTitle = await png(titleBackground());
  const bgContent = await png(contentBackground());

  pres.defineSlideMaster({
    title: "JARVIS_TITLE",
    background: { data: bgTitle },
    objects: [
      { placeholder: { options: { name: "eyebrow", type: "body", x: MX, y: 2.0, w: 7.0, h: 0.34, fontSize: 12, bold: true, color: C.accent1, margin: 0, valign: "top", align: "left" }, text: "" } },
      { placeholder: { options: { name: "title", type: "title", x: MX, y: 2.4, w: 6.6, h: 1.45, fontSize: 80, bold: true, color: C.text1, margin: 0, valign: "top", align: "left" }, text: "" } },
      { placeholder: { options: { name: "subtitle", type: "body", x: MX, y: 3.95, w: 5.9, h: 1.1, fontSize: 22, color: C.text1, margin: 0, valign: "top", align: "left" }, text: "" } },
    ],
  });
  pres.defineSlideMaster({
    title: "JARVIS_CONTENT",
    background: { data: bgContent },
    objects: [
      { placeholder: { options: { name: "eyebrow", type: "body", x: MX, y: 0.52, w: 10.6, h: 0.3, fontSize: 12, bold: true, color: C.accent1, margin: 0, valign: "top", align: "left" }, text: "" } },
      { placeholder: { options: { name: "title", type: "title", x: MX, y: 0.86, w: 10.6, h: 0.75, fontSize: 34, bold: true, color: C.text1, margin: 0, valign: "top", align: "left" }, text: "" } },
      { text: { text: "JARVIS  ·  CAPSTONE LLD  ·  TEAM JARVIS", options: { x: MX, y: 7.0, w: 6, h: 0.26, fontSize: 10, color: C.text2, charSpacing: 2, margin: 0 } } },
    ],
    slideNumber: { x: 11.83, y: 7.0, w: 0.75, h: 0.26, fontSize: 10, color: HEX.secondary, align: "right", margin: 0 },
  });

  const sections = [
    ["Opening", [s01, s02, s03, s04]],
    ["Design", [s05, s06, s07, s08, s09, s10, s11, s12, s13, s14]],
    ["Implementation", [s15, s16, s17, s18]],
    ["Proof", [s19, s19b, s19c, s20, s21]],
    ["Close", [s24]],
  ];
  for (const [title, fns] of sections) {
    pres.addSection({ title });
    for (const fn of fns) await fn();
  }

  await pres.writeFile({ fileName: OUT });
  await applyTheme(OUT, THEME);
  console.log("wrote", OUT);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
