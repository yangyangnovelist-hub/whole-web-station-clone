#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";

const targetDir = process.argv[2];

if (!targetDir) {
  console.error("Usage: node ./scripts/generate-mirror-manifest.mjs <mirror-dir>");
  process.exit(1);
}

const absoluteTargetDir = path.resolve(targetDir);

if (!fs.existsSync(absoluteTargetDir)) {
  console.error(`Mirror directory not found: ${absoluteTargetDir}`);
  process.exit(1);
}

const buckets = {
  html: [],
  css: [],
  js: [],
  images: [],
  fonts: [],
  media: [],
  other: [],
};

const htmlLinkSummary = [];

const imageExtensions = new Set([".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif", ".ico"]);
const fontExtensions = new Set([".woff", ".woff2", ".ttf", ".otf", ".eot"]);
const mediaExtensions = new Set([".mp4", ".webm", ".mp3", ".wav", ".ogg", ".m4a"]);

function walk(dir) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const fullPath = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      walk(fullPath);
      continue;
    }

    const relativePath = path.relative(absoluteTargetDir, fullPath);
    const ext = path.extname(entry.name).toLowerCase();
    const stat = fs.statSync(fullPath);
    const fileRecord = {
      path: relativePath,
      bytes: stat.size,
    };

    if (ext === ".html" || ext === ".htm") {
      buckets.html.push(fileRecord);
      summarizeHtml(fullPath, relativePath);
    } else if (ext === ".css") {
      buckets.css.push(fileRecord);
    } else if (ext === ".js" || ext === ".mjs" || ext === ".cjs") {
      buckets.js.push(fileRecord);
    } else if (imageExtensions.has(ext)) {
      buckets.images.push(fileRecord);
    } else if (fontExtensions.has(ext)) {
      buckets.fonts.push(fileRecord);
    } else if (mediaExtensions.has(ext)) {
      buckets.media.push(fileRecord);
    } else {
      buckets.other.push(fileRecord);
    }
  }
}

function summarizeHtml(fullPath, relativePath) {
  const html = fs.readFileSync(fullPath, "utf8");
  const hrefs = [...html.matchAll(/href\s*=\s*["']([^"']+)["']/gi)].map((match) => match[1]);
  const srcs = [...html.matchAll(/src\s*=\s*["']([^"']+)["']/gi)].map((match) => match[1]);
  const forms = [...html.matchAll(/<form\b/gi)].length;
  const imageMaps = [...html.matchAll(/<map\b/gi)].length;
  htmlLinkSummary.push({
    path: relativePath,
    hrefCount: hrefs.length,
    srcCount: srcs.length,
    formCount: forms,
    imageMapCount: imageMaps,
    sampleHrefs: hrefs.slice(0, 10),
    sampleSrcs: srcs.slice(0, 10),
  });
}

walk(absoluteTargetDir);

const totalBytes = Object.values(buckets)
  .flat()
  .reduce((sum, file) => sum + file.bytes, 0);

const manifest = {
  generatedAt: new Date().toISOString(),
  mirrorDir: absoluteTargetDir,
  counts: Object.fromEntries(
    Object.entries(buckets).map(([bucket, files]) => [bucket, files.length]),
  ),
  totalBytes,
  htmlLinkSummary,
  files: buckets,
};

process.stdout.write(`${JSON.stringify(manifest, null, 2)}\n`);
