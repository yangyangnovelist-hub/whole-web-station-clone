#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";

const mirrorDirArg = process.argv[2];

if (!mirrorDirArg) {
  console.error("Usage: node ./scripts/normalize-pakfactory-lazy-images.mjs <mirror-dir>");
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

function hasRealAttr(tag, attrName) {
  const masked = tag
    .replace(/\bdata-srcset=/gi, "data_srcset=")
    .replace(/\bdata-src=/gi, "data_src=");
  return new RegExp(`\\s${attrName}=`, "i").test(masked);
}

let filesTouched = 0;
let imagesFixed = 0;

walk(mirrorDir, (fullPath) => {
  const ext = path.extname(fullPath).toLowerCase();
  if (!(ext === ".html" || ext === ".htm" || looksLikeHtml(fullPath))) return;

  const original = fs.readFileSync(fullPath, "utf8");
  let changed = false;

  const rewritten = original.replace(/<img\b[^>]*>/gi, (tag) => {
    const dataSrc = tag.match(/\bdata-src=(["'])([^"']+)\1/i)?.[2];
    const dataSrcset = tag.match(/\bdata-srcset=(["'])([^"']+)\1/i)?.[2];
    let next = tag;

    if (dataSrc && !hasRealAttr(tag, "src")) {
      next = next.replace(/<img\b/i, `<img src="${dataSrc}"`);
    }

    if (dataSrcset && !hasRealAttr(next, "srcset")) {
      next = next.replace(/<img\b/i, `<img srcset="${dataSrcset}"`);
    }

    if (next !== tag) {
      imagesFixed += 1;
      changed = true;
    }

    return next;
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
      imagesFixed,
    },
    null,
    2,
  ),
);
