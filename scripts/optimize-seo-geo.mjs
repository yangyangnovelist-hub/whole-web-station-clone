#!/usr/bin/env node
/**
 * SEO + GEO (generative engine optimization) pass over the mirrored storefront
 * pages. Idempotent: safe to run on every deploy.
 *
 *   node ./scripts/optimize-seo-geo.mjs <public-dir> [options]
 *
 * Options:
 *   --site <url>             canonical origin (default https://packoasis.com)
 *   --backend <url>          Medusa backend origin: pulls /packoasis/catalog for
 *                            live starting prices and embeds the quote widget
 *   --publishable-key <pk>   storefront publishable key for the widget
 *   --catalog <file>         saved /packoasis/catalog JSON instead of --backend
 *   --inventory <file>       route inventory (default docs/pakfactory-route-inventory.json;
 *                            without it, routes are derived from the HTML files)
 *   --logo <url>             PackOasis logo (absolute URL or site path): replaces
 *                            the mirrored header logo, which is still the
 *                            PakFactory wordmark, and is used in JSON-LD
 *   --report <file>          write a JSON report
 *   --check                  audit only, no writes; exit 1 when pages need work
 *
 * Per page: absolute canonical + og:url, meta description when missing,
 * noindex for account/checkout/duplicate pages, Organization/WebPage/FAQPage/
 * Product JSON-LD (with live price ranges), a crawlable "instant pricing"
 * line on product pages, the quote widget, and repair of HTTrack
 * "Page has moved" stubs that refresh to themselves.
 * Site files: robots.txt (search + AI crawlers), sitemap.xml, llms.txt,
 * llms-full.txt.
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

function parseArgs(argv) {
  const args = { _: [] };
  for (let i = 0; i < argv.length; i++) {
    const token = argv[i];
    if (token.startsWith("--")) {
      const key = token.slice(2);
      const next = argv[i + 1];
      if (next === undefined || next.startsWith("--")) {
        args[key] = true;
      } else {
        args[key] = next;
        i++;
      }
    } else {
      args._.push(token);
    }
  }
  return args;
}

const args = parseArgs(process.argv.slice(2));
if (!args._[0]) {
  console.error(
    "Usage: node ./scripts/optimize-seo-geo.mjs <public-dir> [--site url] [--backend url] [--publishable-key pk] [--catalog file] [--report file] [--check]",
  );
  process.exit(1);
}

const publicDir = path.resolve(args._[0]);
const site = String(args.site || "https://packoasis.com").replace(/\/$/, "");
const backend = args.backend ? String(args.backend).replace(/\/$/, "") : null;
const publishableKey = args["publishable-key"] ? String(args["publishable-key"]) : null;
const checkOnly = Boolean(args.check);
const inventoryPath = path.resolve(
  args.inventory || path.join(repoRoot, "docs/pakfactory-route-inventory.json"),
);

function bucketFor(route) {
  if (route === "/" || /^\/index-[0-9a-f]{32}\.html$/.test(route)) return "home";
  if (/^\/customer/.test(route)) return "account";
  if (/^\/checkout/.test(route)) return "checkout";
  if (/^\/(contact|quotation)/.test(route)) return "inquiry";
  if (/^\/inspiration/.test(route)) return "inspiration";
  if (/^\/(catalog|catalogsearch)\//.test(route)) return "catalog";
  if (/^\/(custom-|folding-carton|reverse-tuck|printed-tape)/.test(route)) return "product-detail";
  return "content";
}

/** Without a route inventory (e.g. in the storefront repo), derive routes from the files. */
function inventoryFromFiles(root) {
  const routes = [];
  const visit = (dir) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        if (!/^(media|static)\./.test(entry.name) && entry.name !== "_next") {
          visit(full);
        }
        continue;
      }
      if (!/\.html?$/.test(entry.name)) {
        continue;
      }
      const relativePath = path.relative(root, full).split(path.sep).join("/");
      let route = `/${relativePath}`;
      if (relativePath === "index.html") {
        route = "/";
      } else if (/\.html\/index\.html$/.test(relativePath)) {
        route = `/${relativePath.replace(/\/index\.html$/, "")}`;
      } else if (/(^|\/)[^/.]+\/index\.html$/.test(relativePath) && !relativePath.includes(".html/")) {
        route = `/${relativePath.replace(/\/index\.html$/, "")}`;
      }
      routes.push({ route, relativePath, bucket: bucketFor(route) });
    }
  };
  visit(root);
  return { routes };
}

const inventory = fs.existsSync(inventoryPath)
  ? JSON.parse(fs.readFileSync(inventoryPath, "utf8"))
  : inventoryFromFiles(publicDir);
const today = new Date().toISOString().slice(0, 10);

// Nested mirror pages reach the logo through any number of "../" segments.
const MIRROR_LOGO = /(?:(?:\.\.\/)+|\/)?media\.packoasis\.com\/logo\/websites\/1\/logo1(?:-[0-9a-f]{32})?\.jpg/g;
const logoArg = args.logo ? String(args.logo) : null;
const ORG = {
  name: "PackOasis",
  email: "hello@packoasis.com",
  // The mirrored header logo is PakFactory's wordmark, so only publish a
  // logo in structured data when a real PackOasis logo is supplied.
  logo: logoArg ? (/^https?:/.test(logoArg) ? logoArg : `${site}${logoArg}`) : undefined,
  sameAs: ["https://www.pinterest.ca/packoasis/"],
};

/** HTTrack stubs whose meta refresh points at themselves (infinite reload). */
const STUB_REDIRECTS = {
  "/custom-coffee-packaging.html": "/custom-pouches.html",
  "/custom-ecommerce-packaging-boxes.html": "/custom-printed-corrugated-boxes.html",
  "/custom-pet-packaging-boxes.html": "/custom-pouches.html",
};

const PRIVATE_ROUTE = /^\/(customer|checkout|catalogsearch|quotation|cards\.html)/;
const HASHED_ROUTE = /-[0-9a-f]{32}\.html?$|\/index-[0-9a-f]{32}\.html?$/;

const AI_CRAWLERS = [
  "GPTBot",
  "OAI-SearchBot",
  "ChatGPT-User",
  "ClaudeBot",
  "Claude-SearchBot",
  "Claude-User",
  "anthropic-ai",
  "PerplexityBot",
  "Perplexity-User",
  "Google-Extended",
  "Applebot-Extended",
  "Amazonbot",
  "DuckAssistBot",
  "MistralAI-User",
  "CCBot",
  "meta-externalagent",
];

// ---------------------------------------------------------------- helpers

const ENTITIES = {
  "&amp;": "&",
  "&lt;": "<",
  "&gt;": ">",
  "&quot;": '"',
  "&#39;": "'",
  "&#039;": "'",
  "&apos;": "'",
  "&nbsp;": " ",
  "&rsquo;": "'",
  "&lsquo;": "'",
  "&rdquo;": '"',
  "&ldquo;": '"',
  "&ndash;": "-",
  "&mdash;": "-",
  "&reg;": "®",
  "&trade;": "™",
  "&copy;": "©",
};

function decode(text) {
  return text
    .replace(/&#(\d+);/g, (_, code) => String.fromCodePoint(Number(code)))
    .replace(/&#x([0-9a-f]+);/gi, (_, code) => String.fromCodePoint(parseInt(code, 16)))
    .replace(/&[a-z]+;|&#0?39;/gi, (entity) => ENTITIES[entity.toLowerCase()] ?? entity);
}

function textOf(fragment) {
  return decode(fragment.replace(/<[^>]*>/g, " ")).replace(/\s+/g, " ").trim();
}

function escapeAttr(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/"/g, "&quot;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function escapeHtml(value) {
  return String(value).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function truncate(text, max) {
  if (text.length <= max) {
    return text;
  }
  const cut = text.slice(0, max - 1);
  return `${cut.slice(0, cut.lastIndexOf(" ") > max * 0.6 ? cut.lastIndexOf(" ") : cut.length)}…`;
}

function usd(amount) {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: amount < 1 ? 3 : 2,
  }).format(amount);
}

function getTitle(html) {
  const match = html.match(/<title[^>]*>([\s\S]*?)<\/title>/i);
  return match ? textOf(match[1]) : "";
}

function metaRegex(attr, key) {
  return new RegExp(
    `<meta\\b[^>]*\\b${attr}\\s*=\\s*["']${key}["'][^>]*>`,
    "gi",
  );
}

function getMeta(html, attr, key) {
  const tag = html.match(metaRegex(attr, key))?.[0];
  if (!tag) {
    return "";
  }
  const content = tag.match(/\bcontent\s*=\s*"([^"]*)"|\bcontent\s*=\s*'([^']*)'/i);
  return content ? decode(content[1] ?? content[2] ?? "").trim() : "";
}

// Replacer functions keep "$" in the tags (e.g. a --logo URL) literal.
function insertIntoHead(html, tags) {
  if (/<\/head>/i.test(html)) {
    return html.replace(/<\/head>/i, () => `${tags}\n</head>`);
  }
  return html.replace(/<body/i, () => `${tags}\n<body`);
}

function setMeta(html, attr, key, content) {
  const tag = `<meta ${attr}="${key}" content="${escapeAttr(content)}" />`;
  if (metaRegex(attr, key).test(html)) {
    let replaced = false;
    return html.replace(metaRegex(attr, key), () => {
      if (replaced) {
        return "";
      }
      replaced = true;
      return tag;
    });
  }
  return insertIntoHead(html, tag);
}

function setCanonical(html, url) {
  const tag = `<link rel="canonical" href="${escapeAttr(url)}" />`;
  const pattern = /<link\b[^>]*\brel\s*=\s*["']canonical["'][^>]*>/gi;
  if (pattern.test(html)) {
    let replaced = false;
    return html.replace(pattern, () => {
      if (replaced) {
        return "";
      }
      replaced = true;
      return tag;
    });
  }
  return insertIntoHead(html, tag);
}

// Every marked element is inserted followed by exactly one "\n", and removal
// takes that newline with it, so re-running restores the original byte for
// byte before re-inserting (idempotent even when </head> is indented).
function removeMarked(html, marker) {
  return html
    .replace(new RegExp(`<script[^>]*${marker}[^>]*>[\\s\\S]*?<\\/script>\\n?`, "gi"), "")
    .replace(new RegExp(`<style[^>]*${marker}[^>]*>[\\s\\S]*?<\\/style>\\n?`, "gi"), "")
    .replace(new RegExp(`<div[^>]*${marker}[^>]*>[\\s\\S]*?<!--/${marker}-->\\n?`, "gi"), "");
}

/** Returns the inner HTML of the element whose opening tag starts at `start`. */
function balancedInner(html, start, tag = "div") {
  const open = new RegExp(`<${tag}\\b`, "gi");
  const close = new RegExp(`</${tag}>`, "gi");
  const contentStart = html.indexOf(">", start) + 1;
  let depth = 1;
  let cursor = contentStart;
  while (depth > 0 && cursor < html.length) {
    open.lastIndex = cursor;
    close.lastIndex = cursor;
    const nextOpen = open.exec(html);
    const nextClose = close.exec(html);
    if (!nextClose) {
      return html.slice(contentStart);
    }
    if (nextOpen && nextOpen.index < nextClose.index) {
      depth++;
      cursor = nextOpen.index + 1;
    } else {
      depth--;
      cursor = nextClose.index + nextClose[0].length;
      if (depth === 0) {
        return html.slice(contentStart, nextClose.index);
      }
    }
  }
  return html.slice(contentStart);
}

function extractFaqs(html) {
  const faqs = [];
  const itemPattern = /<div\b[^>]*class\s*=\s*["'][^"']*\bfaq-item\b[^"']*["'][^>]*>/gi;
  let match;
  while ((match = itemPattern.exec(html))) {
    const inner = balancedInner(html, match.index);
    const question = inner.match(/<h[2-5][^>]*>([\s\S]*?)<\/h[2-5]>/i);
    const contentOpen = inner.search(/<div\b[^>]*class\s*=\s*["'][^"']*\bfaq-content\b/i);
    if (!question || contentOpen < 0) {
      continue;
    }
    const answer = textOf(balancedInner(inner, contentOpen));
    const q = textOf(question[1]);
    if (q && answer) {
      faqs.push({ question: q, answer: truncate(answer, 900) });
    }
  }

  for (const details of html.matchAll(/<details\b[^>]*>([\s\S]*?)<\/details>/gi)) {
    const summary = details[1].match(/<summary[^>]*>([\s\S]*?)<\/summary>/i);
    if (!summary) {
      continue;
    }
    const q = textOf(summary[1]);
    const answer = textOf(details[1].replace(summary[0], ""));
    if (q && answer) {
      faqs.push({ question: q, answer: truncate(answer, 900) });
    }
  }

  const seen = new Set();
  return faqs.filter((faq) => {
    const key = faq.question.toLowerCase();
    if (seen.has(key)) {
      return false;
    }
    seen.add(key);
    return true;
  });
}

function bodyText(html) {
  const body = html.match(/<body[^>]*>([\s\S]*)<\/body>/i)?.[1] ?? html;
  return textOf(
    body
      .replace(/<script[\s\S]*?<\/script>/gi, " ")
      .replace(/<style[\s\S]*?<\/style>/gi, " ")
      .replace(/<noscript[\s\S]*?<\/noscript>/gi, " ")
      .replace(/<header\b[\s\S]*?<\/header>/gi, " ")
      .replace(/<footer\b[\s\S]*?<\/footer>/gi, " ")
      .replace(/<nav\b[\s\S]*?<\/nav>/gi, " ")
      .replace(/<div\b[^>]*class="[^"]*(?:drop-menu|mega-menu|nav-sections|block-search|minicart)[^"]*"[\s\S]*?<\/div>/gi, " "),
  );
}

function mainText(html) {
  const main = html.match(/<main\b[\s\S]*<\/main>/i)?.[0];
  return main ? bodyText(`<body>${main}</body>`) : bodyText(html);
}

function firstH1(html) {
  const match = html.match(/<h1\b[^>]*>([\s\S]*?)<\/h1>/i);
  return match ? textOf(match[1]) : "";
}

function autoDescription(html, title) {
  const main = html.match(/<main\b[\s\S]*?<\/main>/i)?.[0] ?? html;
  for (const paragraph of main.matchAll(/<p\b[^>]*>([\s\S]*?)<\/p>/gi)) {
    const text = textOf(paragraph[1]);
    if (text.length >= 60 && !/cookie|javascript/i.test(text)) {
      return truncate(text, 158);
    }
  }
  return truncate(
    `${title.replace(/\s*\|.*$/, "")}: custom printed packaging from PackOasis with instant online pricing, low minimums and fast production.`,
    158,
  );
}

function visiblePakfactoryMentions(html) {
  const text = bodyText(html).replace(/https?:\/\/\S+/g, " ");
  const mentions = [];
  for (const match of text.matchAll(/.{0,60}pak ?factory.{0,60}/gi)) {
    mentions.push(match[0].trim());
  }
  const titleAndMeta = [getTitle(html), getMeta(html, "name", "description")].join(" ");
  if (/pak ?factory/i.test(titleAndMeta)) {
    mentions.push(`<head>: ${truncate(titleAndMeta, 120)}`);
  }
  return mentions;
}

function productTypeForRoute(route, catalogData) {
  if (!catalogData) {
    return null;
  }
  const lower = route.toLowerCase();
  for (const hint of catalogData.page_hints ?? []) {
    if (new RegExp(hint.pattern).test(lower)) {
      return catalogData.product_types.find((type) => type.id === hint.product_type) ?? null;
    }
  }
  return null;
}

function priceLine(type) {
  const days = type.production_days;
  return `Instant pricing: from ${usd(type.price_from.unit_price)} per piece at ${type.price_from.quantity.toLocaleString("en-US")} pcs (${usd(type.price_at_moq.total)} at the ${type.moq.toLocaleString("en-US")} piece minimum). Production ${days[0]}-${days[1]} business days, freight to the contiguous US included.`;
}

function upsertTeaser(html, type) {
  const cleaned = removeMarked(html, "data-packoasis-teaser");
  const inBanner = /class="white-banner-title"/.test(cleaned);
  // A solid pill stays readable whether or not the hero image loads.
  const style = inBanner
    ? "display:inline-block;background:rgba(17,24,39,.78);color:#fff;font-weight:600;margin:0 0 16px;padding:8px 12px;border-radius:8px;line-height:1.5"
    : "display:inline-block;background:#e8f6ee;color:#12542e;font-weight:600;margin:12px 0 16px;padding:8px 12px;border-radius:8px;line-height:1.5";
  const teaser = `<div data-packoasis-teaser data-product-type="${escapeAttr(type.id)}" style="${style}">${escapeHtml(
    priceLine(type),
  )}</div><!--/data-packoasis-teaser-->`;

  const h1 = cleaned.match(/<h1\b[^>]*>[\s\S]*?<\/h1>/i);
  if (!h1) {
    return cleaned;
  }
  let insertAt = h1.index + h1[0].length;
  const after = cleaned.slice(insertAt, insertAt + 1200);
  const paragraph = after.match(/^\s*<p\b[^>]*>[\s\S]*?<\/p>/i);
  if (paragraph) {
    insertAt += paragraph[0].length;
  }
  return `${cleaned.slice(0, insertAt)}${teaser}\n${cleaned.slice(insertAt)}`;
}

function upsertWidget(html) {
  const cleaned = removeMarked(html, "data-packoasis-widget");
  const tag = `<script src="${escapeAttr(backend)}/packoasis/widget.js" data-publishable-key="${escapeAttr(
    publishableKey,
  )}" data-packoasis-widget defer></script>`;
  if (/<\/body>/i.test(cleaned)) {
    return cleaned.replace(/<\/body>(?![\s\S]*<\/body>)/i, `${tag}\n</body>`);
  }
  // Truncated mirror files have no </body>: append at the end.
  return `${cleaned.replace(/\s+$/, "")}\n${tag}\n`;
}

function upsertJsonLd(html, graph) {
  const cleaned = removeMarked(html, "data-packoasis-seo");
  const json = JSON.stringify({ "@context": "https://schema.org", "@graph": graph }).replace(
    /</g,
    "\\u003c",
  );
  return insertIntoHead(
    cleaned,
    `<script type="application/ld+json" data-packoasis-seo>${json}</script>`,
  );
}

function setRobots(html, value) {
  return setMeta(html, "name", "robots", value);
}

function routeUrl(route) {
  return route === "/" ? `${site}/` : `${site}${encodeURI(route)}`;
}

function stubRedirectHtml(target) {
  const url = routeUrl(target);
  return `<!DOCTYPE html>
<!-- packoasis-redirect -->
<html lang="en"><head><meta charset="utf-8" />
<title>Moved to ${escapeHtml(target)} | PackOasis</title>
<meta name="robots" content="noindex, follow" />
<link rel="canonical" href="${escapeAttr(url)}" />
<meta http-equiv="refresh" content="0; url=${escapeAttr(target)}" />
</head><body><p>This page moved to <a href="${escapeAttr(target)}">${escapeHtml(target)}</a>.</p>
<script>location.replace(${JSON.stringify(target)})</script></body></html>
`;
}

// ------------------------------------------------------------- catalog

async function loadCatalog() {
  if (args.catalog) {
    const data = JSON.parse(fs.readFileSync(path.resolve(String(args.catalog)), "utf8"));
    return data.catalog ?? data;
  }
  if (!backend) {
    return null;
  }
  const response = await fetch(`${backend}/packoasis/catalog`);
  if (!response.ok) {
    throw new Error(`Could not load ${backend}/packoasis/catalog (${response.status})`);
  }
  return (await response.json()).catalog;
}

// ----------------------------------------------------------------- run

const catalog = await loadCatalog();

// Without a catalog the teasers and Product JSON-LD cannot be rebuilt, so the
// run would strip them. Refuse rather than silently wipe published pricing.
if (!catalog) {
  const PRICED = /data-packoasis-teaser|<script[^>]*data-packoasis-seo[^>]*>[^<]*"@type":"Product"/;
  const priced = inventory.routes.filter((record) => {
    const file = path.join(publicDir, record.relativePath);
    return fs.existsSync(file) && fs.statSync(file).isFile() && PRICED.test(fs.readFileSync(file, "utf8"));
  });
  if (priced.length) {
    console.error(
      `${priced.length} pages carry instant pricing from an earlier run (e.g. ${priced[0].route}); pass --backend or --catalog so it can be refreshed instead of removed.`,
    );
    process.exit(1);
  }
}

const knownRoutes = new Set(inventory.routes.map((route) => route.route));
const report = {
  generatedAt: new Date().toISOString(),
  site,
  pages: 0,
  changed: 0,
  canonicalFixed: 0,
  ogUrlFixed: 0,
  descriptionsAdded: 0,
  noindexed: 0,
  jsonLd: 0,
  faqPages: 0,
  productOffers: 0,
  teasers: 0,
  widgets: 0,
  stubsRepaired: [],
  stubsLeft: [],
  pakfactoryMentions: [],
  duplicateTitles: [],
  missingH1: [],
  pakfactoryLogoPages: 0,
  pakfactoryPhonePages: 0,
  missingBackgroundImages: [],
  changedRoutes: [],
};
const missingBackgrounds = new Map();
const sitemap = [];
const llmsPages = [];
const titles = new Map();

for (const record of inventory.routes) {
  const file = path.join(publicDir, record.relativePath);
  if (!fs.existsSync(file) || fs.statSync(file).isDirectory()) {
    continue;
  }
  report.pages++;
  const original = fs.readFileSync(file, "utf8");
  let html = original;
  const route = record.route;

  if (html.includes("<!-- packoasis-redirect -->")) {
    const target = html.match(/url=([^"]+)"/i)?.[1];
    report.stubsRepaired.push({ route, target });
    continue;
  }

  if (/<title>\s*Page has moved/i.test(html)) {
    const refresh = html.match(/URL=([^"'>]+)/i)?.[1];
    const resolved = refresh ? new URL(refresh, `${site}${route}`).pathname : null;
    const target = STUB_REDIRECTS[route];
    if (target && knownRoutes.has(target)) {
      html = stubRedirectHtml(target);
      report.stubsRepaired.push({ route, target });
    } else {
      html = setRobots(html, "noindex, follow");
      if (resolved === route) {
        report.stubsLeft.push({ route, problem: "refreshes to itself; add it to STUB_REDIRECTS" });
      }
    }
    if (html !== original) {
      report.changed++;
      report.changedRoutes.push(route);
      if (!checkOnly) {
        fs.writeFileSync(file, html);
      }
    }
    continue;
  }

  const hashed = HASHED_ROUTE.test(route);
  const cleanRoute = hashed
    ? route.replace(/-[0-9a-f]{32}(\.html?)$/, "$1").replace(/\/index-[0-9a-f]{32}\.html?$/, "")
    : route;
  const canonicalRoute = hashed && knownRoutes.has(cleanRoute) ? cleanRoute : route;
  const canonical = routeUrl(canonicalRoute);
  const indexable =
    !hashed && !PRIVATE_ROUTE.test(route) && !["account", "checkout"].includes(record.bucket);

  const currentCanonical = html.match(/<link\b[^>]*\brel\s*=\s*["']canonical["'][^>]*href\s*=\s*["']([^"']*)["']/i)?.[1];
  if (currentCanonical !== canonical) {
    report.canonicalFixed++;
  }
  html = setCanonical(html, canonical);

  if (getMeta(html, "property", "og:url") !== canonical) {
    report.ogUrlFixed++;
  }
  html = setMeta(html, "property", "og:url", canonical);
  if (!getMeta(html, "property", "og:site_name")) {
    html = setMeta(html, "property", "og:site_name", "PackOasis");
  }
  if (!getMeta(html, "property", "og:type")) {
    html = setMeta(html, "property", "og:type", "website");
  }

  const title = getTitle(html);
  let description = getMeta(html, "name", "description");
  if (!description) {
    description = autoDescription(html, title);
    html = setMeta(html, "name", "description", description);
    report.descriptionsAdded++;
  }
  if (!getMeta(html, "property", "og:title") && title) {
    html = setMeta(html, "property", "og:title", title);
  }
  if (!getMeta(html, "property", "og:description")) {
    html = setMeta(html, "property", "og:description", description);
  }

  if (!indexable) {
    html = setRobots(html, "noindex, follow");
    report.noindexed++;
  }

  if (!/<html\b[^>]*\blang=/i.test(html)) {
    html = html.replace(/<html\b/i, '<html lang="en"');
  }

  // Hero banners use CSS background images that the mirror never captured;
  // give them a dark fallback so the white headline stays readable.
  html = removeMarked(html, "data-packoasis-fixes");
  if (/landing-page-banner/.test(html)) {
    html = insertIntoHead(
      html,
      '<style data-packoasis-fixes>.landing-page-banner{background-color:#1f2d3a !important}</style>',
    );
  }
  for (const match of html.matchAll(/url\(\s*["']?(\/media\.packoasis\.com\/[^)"']+)["']?\s*\)/g)) {
    const assetPath = match[1].replace(/\/{2,}/g, "/");
    if (!fs.existsSync(path.join(publicDir, decodeURI(assetPath)))) {
      missingBackgrounds.set(assetPath, (missingBackgrounds.get(assetPath) ?? 0) + 1);
    }
  }
  if (MIRROR_LOGO.test(html)) {
    report.pakfactoryLogoPages++;
    if (ORG.logo) {
      // A replacer function inserts "$" in the value literally.
      html = html.replace(MIRROR_LOGO, () => escapeAttr(logoArg));
    }
  }
  MIRROR_LOGO.lastIndex = 0;
  if (/888[-. )]*622[-. ]*2819/.test(html)) {
    report.pakfactoryPhonePages++;
  }

  const faqs = extractFaqs(html);
  const productType =
    ["product-detail", "catalog"].includes(record.bucket) || route === "/printed-tape.html"
      ? productTypeForRoute(route, catalog)
      : null;
  const mainHtml = html.match(/<main\b[\s\S]*<\/main>/i)?.[0] ?? html;
  const image =
    getMeta(html, "property", "og:image") ||
    (productType?.image ? `${site}${productType.image}` : undefined) ||
    (() => {
      const src = mainHtml.match(
        /<img\b[^>]*\bsrc="(\/media\.packoasis\.com\/(?![^"]*(?:logo|icon))[^"]+\.(?:jpe?g|png|webp))"/i,
      )?.[1];
      return src ? `${site}${src}` : undefined;
    })();

  const graph = [
    {
      "@type": "Organization",
      "@id": `${site}/#organization`,
      name: ORG.name,
      url: `${site}/`,
      logo: ORG.logo,
      email: ORG.email,
      sameAs: ORG.sameAs,
      contactPoint: {
        "@type": "ContactPoint",
        contactType: "sales",
        email: ORG.email,
        availableLanguage: ["English"],
      },
    },
    {
      "@type": "WebPage",
      "@id": `${canonical}#webpage`,
      url: canonical,
      name: title,
      description,
      isPartOf: { "@id": `${site}/#website` },
      publisher: { "@id": `${site}/#organization` },
      inLanguage: "en",
    },
    {
      "@type": "WebSite",
      "@id": `${site}/#website`,
      url: `${site}/`,
      name: ORG.name,
      publisher: { "@id": `${site}/#organization` },
    },
  ];
  if (faqs.length) {
    graph.push({
      "@type": "FAQPage",
      "@id": `${canonical}#faq`,
      mainEntity: faqs.map((faq) => ({
        "@type": "Question",
        name: faq.question,
        acceptedAnswer: { "@type": "Answer", text: faq.answer },
      })),
    });
    report.faqPages++;
  }
  // The teaser and the Product JSON-LD come and go together: strip the teaser
  // on every page, so a page that no longer gets an offer keeps no stale price.
  html = removeMarked(html, "data-packoasis-teaser");
  if (productType && indexable) {
    graph.push({
      "@type": "Product",
      "@id": `${canonical}#product`,
      name: title.replace(/\s*\|.*$/, "") || firstH1(html),
      description,
      image,
      category: productType.category,
      brand: { "@id": `${site}/#organization` },
      manufacturer: { "@id": `${site}/#organization` },
      additionalProperty: [
        { "@type": "PropertyValue", name: "Minimum order quantity", value: productType.moq },
        {
          "@type": "PropertyValue",
          name: "Production lead time (business days)",
          value: `${productType.production_days[0]}-${productType.production_days[1]}`,
        },
      ],
      offers: {
        "@type": "AggregateOffer",
        priceCurrency: "USD",
        lowPrice: productType.price_from.unit_price,
        highPrice: Number((productType.price_at_moq.total / productType.moq).toFixed(4)),
        availability: "https://schema.org/InStock",
        url: `${canonical}#instant-quote`,
        seller: { "@id": `${site}/#organization` },
      },
    });
    report.productOffers++;
    html = upsertTeaser(html, productType);
    report.teasers++;
  }
  html = upsertJsonLd(html, graph);
  report.jsonLd++;

  if (backend && publishableKey) {
    html = upsertWidget(html);
    report.widgets++;
  }

  const mentions = visiblePakfactoryMentions(html);
  if (mentions.length) {
    report.pakfactoryMentions.push({ route, mentions: mentions.slice(0, 5), count: mentions.length });
  }
  if (!firstH1(html) && indexable) {
    report.missingH1.push(route);
  }

  if (indexable) {
    const list = titles.get(title) ?? [];
    list.push(route);
    titles.set(title, list);
    const priority =
      route === "/"
        ? "1.0"
        : record.bucket === "product-detail"
          ? "0.9"
          : ["inquiry", "catalog"].includes(record.bucket)
            ? "0.8"
            : record.bucket === "content"
              ? "0.6"
              : "0.5";
    sitemap.push({ loc: canonical, priority });
    llmsPages.push({
      route,
      url: canonical,
      bucket: record.bucket,
      title: title.replace(/\s*\|\s*PackOasis.*$/i, "").replace(/\s*\|\s*$/, ""),
      description,
      productType,
      excerpt: truncate(mainText(html), 900),
    });
  }

  if (html !== original) {
    report.changed++;
    report.changedRoutes.push(route);
    if (!checkOnly) {
      fs.writeFileSync(file, html);
    }
  }
}

report.missingBackgroundImages = [...missingBackgrounds.entries()]
  .sort((a, b) => b[1] - a[1])
  .map(([asset, pages]) => ({ asset, pages }));

report.duplicateTitles = [...titles.entries()]
  .filter(([, routes]) => routes.length > 1)
  .map(([title, routes]) => ({ title, routes }));

// ------------------------------------------------------------ site files

const disallow = ["/checkout", "/customer", "/catalogsearch", "/quotation", "/api/", "/*/checkout", "/*/account", "/*/cart", "/*?*po_quote="];
const robots = [
  "# Generated by scripts/optimize-seo-geo.mjs",
  "User-agent: *",
  "Allow: /",
  ...disallow.map((rule) => `Disallow: ${rule}`),
  "",
  "# AI assistants and answer engines are welcome to read and cite PackOasis.",
  ...AI_CRAWLERS.map((bot) => `User-agent: ${bot}`),
  "Allow: /",
  ...disallow.map((rule) => `Disallow: ${rule}`),
  "",
  `Sitemap: ${site}/sitemap.xml`,
  "",
].join("\n");

const sitemapXml = `<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
${[...new Map(sitemap.map((entry) => [entry.loc, entry])).values()]
  .map(
    (entry) =>
      `  <url><loc>${escapeHtml(entry.loc)}</loc><lastmod>${today}</lastmod><priority>${entry.priority}</priority></url>`,
  )
  .join("\n")}
</urlset>
`;

const products = llmsPages.filter((page) => page.bucket === "product-detail");
const productLines = products.map(
  (page) =>
    `- [${page.title}](${page.url}): ${page.description}${page.productType ? ` ${priceLine(page.productType)}` : ""}`,
);
const companyLines = llmsPages
  .filter((page) => ["content", "inquiry", "home"].includes(page.bucket))
  .map((page) => `- [${page.title || page.route}](${page.url}): ${page.description}`);
const catalogLines = catalog
  ? catalog.product_types.map(
      (type) =>
        `- ${type.label} (${type.category}): MOQ ${type.moq.toLocaleString("en-US")}; from ${usd(type.price_from.unit_price)}/pc at ${type.price_from.quantity.toLocaleString("en-US")} pcs; ${usd(type.price_at_moq.total)} at MOQ; production ${type.production_days[0]}-${type.production_days[1]} business days${type.instant ? "; instant online ordering" : "; reviewed quote within 1 business day"}.`,
    )
  : [];

const llms = `# PackOasis

> PackOasis manufactures custom printed packaging (mailer boxes, folding cartons, rigid boxes, shipping boxes, pouches, paper bags, labels, tissue paper, inserts and displays) for brands of every size. Buyers get an instant online price and can order in minutes; standard production is 8-22 business days after a free digital proof, with freight to the contiguous US included.

Key facts:
- Instant quotes: every product page has an "Instant quote" button; prices update live for size, quantity, print, finish and rush options.
- Minimums start at ${catalog ? Math.min(...catalog.product_types.filter((type) => type.instant).map((type) => type.moq)).toLocaleString("en-US") : "500"} pieces; volume discounts apply automatically.
- Free digital proof before production; payment only when the buyer confirms at checkout.
- Contact: ${ORG.email}

## Instant pricing for assistants and agents
${
  backend
    ? `- [Quote API](${backend}/packoasis/quote?product_type=mailer-box&dimensions=10x8x4&quantity=1000&print=cmyk_outside): GET, no key needed; returns unit price, total, lead time, volume tiers and an order_url that opens the pre-filled quote on packoasis.com.
- [Catalog API](${backend}/packoasis/catalog): product types, options, minimums and starting prices.
- [OpenAPI description](${backend}/packoasis/openapi.json)`
    : "- Use the Instant quote button on any product page."
}

## Price guide
${catalogLines.join("\n") || "- See product pages."}

## Products
${productLines.join("\n")}

## Company
${companyLines.join("\n")}

## Optional
- [Inspiration gallery](${site}/inspiration.html): finished packaging projects by industry and style.
- [Sitemap](${site}/sitemap.xml)
`;

const llmsFull = `${llms}

## Page details
${llmsPages
  .filter((page) => page.bucket !== "inspiration")
  .map((page) => `### ${page.title || page.route}\nURL: ${page.url}\n${page.description}\n\n${page.excerpt}\n`)
  .join("\n")}`;

if (!checkOnly) {
  fs.writeFileSync(path.join(publicDir, "robots.txt"), robots);
  fs.writeFileSync(path.join(publicDir, "sitemap.xml"), sitemapXml);
  fs.writeFileSync(path.join(publicDir, "llms.txt"), llms);
  fs.writeFileSync(path.join(publicDir, "llms-full.txt"), llmsFull);
}

if (args.report) {
  fs.writeFileSync(path.resolve(String(args.report)), `${JSON.stringify(report, null, 2)}\n`);
}

console.log(
  JSON.stringify(
    {
      pages: report.pages,
      changed: report.changed,
      canonicalFixed: report.canonicalFixed,
      ogUrlFixed: report.ogUrlFixed,
      descriptionsAdded: report.descriptionsAdded,
      noindexed: report.noindexed,
      faqPages: report.faqPages,
      productOffers: report.productOffers,
      widgets: report.widgets,
      stubsRepaired: report.stubsRepaired.length,
      stubsLeft: report.stubsLeft.length,
      pagesWithPakfactoryText: report.pakfactoryMentions.length,
      pagesWithPakfactoryLogo: report.pakfactoryLogoPages,
      pagesWithPakfactoryPhone: report.pakfactoryPhonePages,
      missingBackgroundImages: report.missingBackgroundImages.length,
      duplicateTitles: report.duplicateTitles.length,
      sitemapUrls: sitemap.length,
    },
    null,
    2,
  ),
);

if (checkOnly && report.changed > 0) {
  console.error(`${report.changed} pages need optimization; run without --check.`);
  process.exit(1);
}
