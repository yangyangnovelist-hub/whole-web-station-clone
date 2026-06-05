#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";

const mirrorDirArg = process.argv[2];

if (!mirrorDirArg) {
  console.error("Usage: node ./scripts/prune-pakfactory-blog.mjs <mirror-dir>");
  process.exit(1);
}

const mirrorDir = path.resolve(mirrorDirArg);
const siteRoot = path.join(mirrorDir, "pakfactory.com");
const blogRoot = path.join(siteRoot, "blog");

function walk(dir, visitor) {
  if (!fs.existsSync(dir)) return;
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const fullPath = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      walk(fullPath, visitor);
    } else {
      visitor(fullPath);
    }
  }
}

function normalizeHref(href) {
  let normalized = href.trim();
  while (normalized.startsWith("./")) normalized = normalized.slice(2);
  while (normalized.startsWith("../")) normalized = normalized.slice(3);
  if (normalized.startsWith("/")) normalized = normalized.slice(1);
  return normalized;
}

function looksLikeHtml(fullPath) {
  try {
    const sample = fs.readFileSync(fullPath, "utf8").slice(0, 2048).toLowerCase();
    return sample.includes("<!doctype html") || sample.includes("<html") || sample.includes("<head") || sample.includes("<body");
  } catch {
    return false;
  }
}

function isBlogHref(href) {
  const normalized = normalizeHref(href);
  if (/^https?:\/\/pakfactory\.com\/blog(?:\/|$)/i.test(href)) return true;
  return /^blog(?:\/|$)/i.test(normalized);
}

function disableBlogHrefAttributes(attrs) {
  let next = attrs
    .replace(/\shref=(["'])([^"']*)\1/i, ' href="#"')
    .replace(/\sonclick=(["'])([^"']*)\1/gi, "");

  if (!/\sdata-clone-pruned=/i.test(next)) {
    next += ' data-clone-pruned="blog"';
  }
  if (!/\saria-disabled=/i.test(next)) {
    next += ' aria-disabled="true"';
  }
  return next;
}

let filesTouched = 0;
let disabledLinks = 0;

walk(mirrorDir, (fullPath) => {
  const ext = path.extname(fullPath).toLowerCase();
  if (!(ext === ".html" || ext === ".htm" || looksLikeHtml(fullPath))) return;

  const original = fs.readFileSync(fullPath, "utf8");
  let changed = false;
  const rewritten = original.replace(/<a\b[^>]*\shref=(["'])([^"']+)\1[^>]*>/gi, (match, _quote, href) => {
    if (!isBlogHref(href)) return match;
    const replacement = disableBlogHrefAttributes(match.slice(2, -1));
    disabledLinks += 1;
    changed = true;
    return `<a${replacement}>`;
  });

  if (changed) {
    fs.writeFileSync(fullPath, rewritten);
    filesTouched += 1;
  }
});

const deletedBlogRoot = fs.existsSync(blogRoot);
if (deletedBlogRoot) {
  fs.rmSync(blogRoot, { recursive: true, force: true });
}

console.log(
  JSON.stringify(
    {
      mirrorDir,
      blogRoot,
      deletedBlogRoot,
      filesTouched,
      disabledLinks,
    },
    null,
    2,
  ),
);
