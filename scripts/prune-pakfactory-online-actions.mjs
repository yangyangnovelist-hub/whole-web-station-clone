#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";

const mirrorDirArg = process.argv[2];

if (!mirrorDirArg) {
  console.error("Usage: node ./scripts/prune-pakfactory-online-actions.mjs <mirror-dir>");
  process.exit(1);
}

const mirrorDir = path.resolve(mirrorDirArg);

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

function looksLikeHtml(fullPath) {
  try {
    const sample = fs.readFileSync(fullPath, "utf8").slice(0, 2048).toLowerCase();
    return sample.includes("<!doctype html") || sample.includes("<html") || sample.includes("<head") || sample.includes("<body");
  } catch {
    return false;
  }
}

function shouldDisableAction(url) {
  return [
    "?options=cart",
    "https://pakfactory.com/customer",
    "https://pakfactory.com/checkout",
    "https://pakfactory.com/quotation",
    "https://pakfactory.com/search",
    "https://pakfactory.com/catalogsearch",
  ].some((token) => url.includes(token));
}

function disableFormAttrs(attrs) {
  let next = attrs
    .replace(/\saction=(["'])([^"']*)\1/i, ' action="#"')
    .replace(/\sonsumbit=(["'])([^"']*)\1/gi, "")
    .replace(/\starget=(["'])([^"']*)\1/gi, "");

  if (!/\sdata-clone-pruned=/i.test(next)) {
    next += ' data-clone-pruned="online-action"';
  }
  return next;
}

let filesTouched = 0;
let disabledForms = 0;

walk(mirrorDir, (fullPath) => {
  const ext = path.extname(fullPath).toLowerCase();
  if (!(ext === ".html" || ext === ".htm" || looksLikeHtml(fullPath))) return;

  const original = fs.readFileSync(fullPath, "utf8");
  let changed = false;

  const rewritten = original.replace(/<form\b([^>]*?)\saction=(["'])(https:\/\/pakfactory\.com[^"']+)\2([^>]*)>/gi, (match, before, _quote, url, after) => {
    if (!shouldDisableAction(url)) return match;
    const replacement = disableFormAttrs(`${before} action="${url}"${after}`);
    disabledForms += 1;
    changed = true;
    return `<form${replacement}>`;
  });

  if (changed) {
    fs.writeFileSync(fullPath, rewritten);
    filesTouched += 1;
  }
});

console.log(
  JSON.stringify(
    {
      mirrorDir,
      filesTouched,
      disabledForms,
    },
    null,
    2,
  ),
);
