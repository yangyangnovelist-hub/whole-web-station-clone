#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";

const mirrorDirArg = process.argv[2];

if (!mirrorDirArg) {
  console.error("Usage: node ./scripts/fix-httrack-html-dir-indexes.mjs <mirror-dir>");
  process.exit(1);
}

const mirrorDir = path.resolve(mirrorDirArg);
const siteRoot = path.join(mirrorDir, "pakfactory.com");

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

function extractRedirectTarget(html) {
  const metaMatch = html.match(/URL=\.\.\/([^"]+)/i);
  if (metaMatch) return metaMatch[1];
  const anchorMatch = html.match(/<A HREF="\.\.\/([^"]+)"/i);
  if (anchorMatch) return anchorMatch[1];
  return null;
}

function extractHtmlBuffer(filePath) {
  const buf = fs.readFileSync(filePath);
  const markers = [
    Buffer.from("<!doctype html", "utf8"),
    Buffer.from("<html", "utf8"),
    Buffer.from("<HTML", "utf8"),
  ];

  let start = -1;
  for (const marker of markers) {
    const idx = buf.indexOf(marker);
    if (idx !== -1 && (start === -1 || idx < start)) {
      start = idx;
    }
  }

  if (start === -1) return null;
  return buf.subarray(start);
}

const filesTouched = [];

walk(siteRoot, (fullPath) => {
  const dir = path.dirname(fullPath);
  if (!dir.endsWith(".html")) return;

  const base = path.basename(fullPath).toLowerCase();
  const isCandidate =
    base === "index.html" ||
    /^index-.*\.html(?:\.tmp)?$/i.test(base) ||
    /^index\.[a-z0-9]+(?:\.html)?(?:\.tmp)?$/i.test(base);

  if (!isCandidate) return;

  let html;
  try {
    html = fs.readFileSync(fullPath, "utf8");
  } catch {
    return;
  }

  if (!/Page has moved/i.test(html) && !/META HTTP-EQUIV="Refresh"/i.test(html)) return;

  const redirectTarget = extractRedirectTarget(html);
  const parentDir = path.resolve(dir, "..");
  const dirBase = path.basename(dir, ".html");

  const candidatePaths = [];
  if (redirectTarget) {
    candidatePaths.push(path.resolve(parentDir, redirectTarget));
  }

  if (fs.existsSync(parentDir)) {
    const siblingCandidates = fs
      .readdirSync(parentDir)
      .filter((name) => {
        if (name === path.basename(dir)) return false;
        return (
          name.startsWith(`${dirBase}-`) &&
          (name.endsWith(".html") || name.endsWith(".html.tmp") || name.endsWith(".htm") || name.endsWith(".htm.tmp"))
        );
      })
      .map((name) => path.join(parentDir, name));
    candidatePaths.push(...siblingCandidates);
  }

  let targetBuffer = null;
  for (const candidatePath of candidatePaths) {
    if (!fs.existsSync(candidatePath) || !fs.statSync(candidatePath).isFile()) continue;
    targetBuffer = extractHtmlBuffer(candidatePath);
    if (targetBuffer) break;
  }

  if (!targetBuffer) return;

  const currentBuffer = fs.readFileSync(fullPath);
  if (currentBuffer.equals(targetBuffer)) return;

  fs.writeFileSync(fullPath, targetBuffer);
  filesTouched.push(path.relative(mirrorDir, fullPath));
});

console.log(
  JSON.stringify(
    {
      mirrorDir,
      filesTouched: filesTouched.length,
      sample: filesTouched.slice(0, 50),
    },
    null,
    2,
  ),
);
