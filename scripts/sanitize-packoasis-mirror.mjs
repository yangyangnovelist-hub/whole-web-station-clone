#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";

const publicDirArg = process.argv[2];

if (!publicDirArg) {
  console.error("Usage: node ./scripts/sanitize-packoasis-mirror.mjs <public-dir>");
  process.exit(1);
}

const publicDir = path.resolve(publicDirArg);

const rawSupportRewrites = new Map([
  [
    "https://support.pakfactory.com/portal/en/kb/articles/what-is-the-minimum-size-of-an-order",
    "/contact-us.html#quantityInput",
  ],
  [
    "https://support.pakfactory.com/portal/en/kb/articles/what-is-the-turnaround-time-on-my-order",
    "/contact-us.html#send-inquiry",
  ],
  [
    "https://support.pakfactory.com/portal/en/kb/articles/where-do-you-ship-to",
    "/contact-us.html#send-inquiry",
  ],
  [
    "https://support.pakfactory.com/portal/en/kb/articles/do-you-have-volume-discounts-or-price-breaks",
    "/contact-us.html#send-inquiry",
  ],
  [
    "https://support.pakfactory.com/portal/en/kb/articles/do-you-offer-rush-orders",
    "/contact-us.html#send-inquiry",
  ],
  [
    "https://support.pakfactory.com/portal/en/kb/articles/how-do-i-place-a-reorder",
    "/contact-us.html#send-inquiry",
  ],
  [
    "https://support.pakfactory.com/portal/en/home",
    "/contact-us.html#send-inquiry",
  ],
]);

const escapedSupportRewrites = new Map(
  [...rawSupportRewrites.entries()].map(([from, to]) => [
    from.replaceAll("/", "\\/"),
    to.replaceAll("/", "\\/"),
  ]),
);

function walk(dir, visitor) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const fullPath = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      walk(fullPath, visitor);
    } else if (entry.isFile()) {
      visitor(fullPath);
    }
  }
}

function looksLikeHtml(text) {
  const sample = text.slice(0, 2048).toLowerCase();
  return (
    sample.includes("<!doctype html") ||
    sample.includes("<html") ||
    sample.includes("<head") ||
    sample.includes("<body")
  );
}

function looksLikeSanitizableText(text) {
  if (!text) {
    return false;
  }

  const normalizedText = text.replace(/\u0000/g, "");
  const sample = normalizedText.slice(0, 8192).toLowerCase();
  return (
    looksLikeHtml(normalizedText) ||
    sample.includes("pakfactory.com") ||
    sample.includes("packoasis.com") ||
    sample.includes("static.pakfactory.com") ||
    sample.includes("media.pakfactory.com") ||
    sample.includes("static.packoasis.com") ||
    sample.includes("media.packoasis.com")
  );
}

function shouldProcess(fullPath) {
  const ext = path.extname(fullPath).toLowerCase();
  const binaryExts = new Set([
    ".avif",
    ".bmp",
    ".css.map",
    ".gif",
    ".gz",
    ".ico",
    ".jpeg",
    ".jpg",
    ".js.map",
    ".mp4",
    ".pdf",
    ".png",
    ".svgz",
    ".tar",
    ".tgz",
    ".ttf",
    ".wav",
    ".webm",
    ".webp",
    ".woff",
    ".woff2",
    ".zip",
  ]);

  if (binaryExts.has(ext)) {
    return false;
  }

  try {
    return looksLikeSanitizableText(fs.readFileSync(fullPath, "utf8"));
  } catch {
    return false;
  }
}

function rewriteRootOriginUrl(urlPath = "") {
  return urlPath || "/";
}

function sanitizeFileContents(text) {
  let output = text;

  const removalPatterns = [
    /<script\b[^>]*>[\s\S]*?posthog\.init\([\s\S]*?<\/script>/gi,
    /<!-- Google Tag Manager -->[\s\S]*?<!-- End Google Tag Manager -->/gi,
    /<!-- Google Tag Manager \(noscript\) -->[\s\S]*?<!-- End Google Tag Manager \(noscript\) -->/gi,
    /<!-- Facebook Business Extension for Magento 2 -->[\s\S]*?<!-- End Facebook Pixel Code -->/gi,
    /<script[^>]+addthis_widget\.js[^>]*><\/script>/gi,
    /<!--\s*zoho\s*-->[\s\S]*?<!--Zoho Campaigns Web-Optin Form Ends Here-->/gi,
    /<!--Zoho Campaigns Web-Optin Form's Header Code Starts Here-->[\s\S]*?<!--Zoho Campaigns Web-Optin Form Ends Here-->/gi,
    /<script[^>]+salesiq[^>]*><\/script>/gi,
    /<script[^>]+zoho[^>]*><\/script>/gi,
  ];

  for (const pattern of removalPatterns) {
    output = output.replace(pattern, "");
  }

  for (const [from, to] of rawSupportRewrites.entries()) {
    output = output.split(from).join(to);
  }

  for (const [from, to] of escapedSupportRewrites.entries()) {
    output = output.split(from).join(to);
  }

  output = output.replace(
    /https?:\/\/static\.(?:pakfactory|packoasis)\.com(\/[^"')\s<]*)/gi,
    (_, urlPath) => `/static.pakfactory.com${urlPath}`,
  );

  output = output.replace(
    /https?:\/\/media\.(?:pakfactory|packoasis)\.com(\/[^"')\s<]*)/gi,
    (_, urlPath) => `/media.pakfactory.com${urlPath}`,
  );

  output = output.replace(
    /(^|[^:])\/\/static\.(?:pakfactory|packoasis)\.com(\/[^"')\s<]*)/gi,
    (_, prefix, urlPath) => `${prefix}/static.pakfactory.com${urlPath}`,
  );

  output = output.replace(
    /(^|[^:])\/\/media\.(?:pakfactory|packoasis)\.com(\/[^"')\s<]*)/gi,
    (_, prefix, urlPath) => `${prefix}/media.pakfactory.com${urlPath}`,
  );

  output = output.replace(
    /https:\\\/\\\/static\.(?:pakfactory|packoasis)\.com\\\/([^"'\\<\s]*)/gi,
    (_, urlPath) => `\\/static.pakfactory.com\\/${urlPath}`,
  );

  output = output.replace(
    /https:\\\/\\\/media\.(?:pakfactory|packoasis)\.com\\\/([^"'\\<\s]*)/gi,
    (_, urlPath) => `\\/media.pakfactory.com\\/${urlPath}`,
  );

  output = output.replace(
    /\b(href|action)=("|')https?:\/\/(?:www\.)?pakfactory\.com([^"'#?]*?(?:\?[^"'#]*)?(?:#[^"']*)?)\2/gi,
    (_, attr, quote, urlPath) =>
      `${attr}=${quote}${rewriteRootOriginUrl(urlPath)}${quote}`,
  );

  output = output.replace(
    /\b(content)=("|')https?:\/\/(?:www\.)?pakfactory\.com([^"']*)\2/gi,
    (_, attr, quote, urlPath) =>
      `${attr}=${quote}https://packoasis.com${urlPath || "/"}${quote}`,
  );

  output = output.replace(
    /https:\\\/\\\/(?:www\.)?pakfactory\.com\\\/([^"'\\<\s]*)/gi,
    (_, urlPath) => `\\/${urlPath}`,
  );

  output = output
    .replace(/https?:\/\/(?:www\.)?pakfactory\.com\//gi, "/")
    .replace(/https?:\/\/(?:www\.)?pakfactory\.com\b/gi, "/");

  output = output.replace(
    /\b(href)=("|')(?:https?:\/\/packoasis\.com\/)?customer\/account\/(?:login|create)[^"']*\2/gi,
    '$1=$2/us/account$2',
  );

  output = output.replace(
    /\b(href)=("|')(?:https?:\/\/packoasis\.com\/)?quotation\/quote\/?[^"']*\2/gi,
    '$1=$2/contact-us.html$2',
  );

  output = output.replace(
    /\b(action)=("|')(?:https?:\/\/packoasis\.com\/)?catalogsearch\/result\/?[^"']*\2/gi,
    '$1=$2#$2 data-clone-pruned="search-action"',
  );

  output = output.replace(
    /\b(href)=("|')(?:https?:\/\/packoasis\.com\/)?catalogsearch\/advanced\/?[^"']*\2/gi,
    '$1=$2#$2 data-clone-pruned="advanced-search"',
  );

  output = output.replace(
    /(["'])https?:\/\/packoasis\.com\/search\/ajax\/suggest\/\1/gi,
    '"\/search-disabled.json"',
  );

  output = output.replace(
    /(["'])\/search\/ajax\/suggest\/\1/gi,
    '"\/search-disabled.json"',
  );

  output = output.replace(
    /\bhref=(["'])(https?:\/\/(?:www\.)?(?:linkedin\.com\/company\/[^"']+|facebook\.com\/[^"']+|instagram\.com\/[^"']+|twitter\.com\/[^"']+|x\.com\/[^"']+|pinterest\.com\/[^"']+|youtube\.com\/[^"']+|tiktok\.com\/[^"']+))\1/gi,
    (_match, quote) => `href=${quote}#${quote} data-clone-pruned="social-link"`,
  );

  output = output
    .replace(/quote@pakfactory\.com/gi, "hello@packoasis.com")
    .replace(/PakFactory®/g, "PackOasis")
    .replace(/PakFactory/g, "PackOasis");

  return output;
}

let changedFiles = 0;

walk(publicDir, (fullPath) => {
  if (!shouldProcess(fullPath)) {
    return;
  }

  const original = fs.readFileSync(fullPath, "utf8");
  const normalizedOriginal = original.replace(/\u0000/g, "");
  const sanitized = sanitizeFileContents(normalizedOriginal);

  if (sanitized !== original) {
    fs.writeFileSync(fullPath, sanitized);
    changedFiles += 1;
  }
});

console.log(
  JSON.stringify(
    {
      publicDir,
      changedFiles,
    },
    null,
    2,
  ),
);
