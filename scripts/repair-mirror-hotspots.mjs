#!/usr/bin/env node
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { execFileSync } from "node:child_process";

const mirrorDirArg = process.argv[2];
const maxPassesArg = Number.parseInt(process.argv[3] || "", 10);

if (!mirrorDirArg) {
  console.error("Usage: node ./scripts/repair-mirror-hotspots.mjs <mirror-dir> [max-passes]");
  process.exit(1);
}

const mirrorDir = path.resolve(mirrorDirArg);
const maxPasses = Number.isFinite(maxPassesArg) && maxPassesArg >= 1 ? maxPassesArg : 4;
const rewriteScript = path.resolve("scripts/rewrite-mirror-links.mjs");
const pruneScript = path.resolve("scripts/prune-pakfactory-blog.mjs");

function walk(dir, visitor) {
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

function shouldSkip(url) {
  return [
    "https://pakfactory.com/blog",
    "https://pakfactory.com/customer/",
    "https://pakfactory.com/customer/account/login/referer/",
    "https://pakfactory.com/customer/section/load",
    "https://pakfactory.com/catalog",
    "https://pakfactory.com/catalogsearch",
    "https://pakfactory.com/checkout/",
    "https://pakfactory.com/quotation/",
    "https://pakfactory.com/search/",
    "https://pakfactory.com/search/ajax/suggest/",
    "https://pakfactory.com/fbe/Pixel/ProductInfoForAddToCart",
    "/comments/feed/",
    "/feed/",
    "/Firefox/",
    "?options=cart",
    "mailto:",
    "tel:",
  ].some((token) => url.includes(token));
}

function scanUnresolvedUrls() {
  const unresolvedUrls = new Set();
  let htmlFiles = 0;

  walk(mirrorDir, (fullPath) => {
    const ext = path.extname(fullPath).toLowerCase();
    const name = path.basename(fullPath).toLowerCase();
    if (!(ext === ".html" || ext === ".htm" || ext === "" || name.endsWith(".delaye") || name.endsWith(".delay") || name.endsWith(".del") || looksLikeHtml(fullPath))) {
      return;
    }

    htmlFiles += 1;
    const text = fs.readFileSync(fullPath, "utf8");
    for (const match of text.matchAll(/href=["'](https:\/\/pakfactory\.com[^"']+)["']/gi)) {
      const url = match[1];
      if (!shouldSkip(url)) {
        unresolvedUrls.add(url);
      }
    }
  });

  return {
    htmlFiles,
    unresolvedUrls,
  };
}

function fetchUrlBatch(urls) {
  const tmpListPath = path.join(os.tmpdir(), `pakfactory-hotspots-${Date.now()}.txt`);
  fs.writeFileSync(tmpListPath, `${urls.join("\n")}\n`);

  execFileSync(
    "httrack",
    [
      "-i",
      "-C2",
      "-%L",
      tmpListPath,
      "-O",
      mirrorDir,
      "+https://pakfactory.com/inspiration*",
      "+https://static.pakfactory.com/*",
      "+https://media.pakfactory.com/*",
      "-https://pakfactory.com/blog*",
      "-https://pakfactory.com/customer/*",
      "-https://pakfactory.com/catalog*",
      "-https://pakfactory.com/catalogsearch*",
      "-https://pakfactory.com/checkout/*",
      "-https://pakfactory.com/quotation/*",
      "-https://pakfactory.com/search/*",
      "-*",
      "-N",
      "%h%p/%n-%M.%t",
      "-q",
    ],
    { stdio: "inherit" },
  );

  return tmpListPath;
}

const passes = [];

for (let pass = 1; pass <= maxPasses; pass += 1) {
  const before = scanUnresolvedUrls();
  const urls = [...before.unresolvedUrls].sort();
  const summary = {
    pass,
    htmlFilesBefore: before.htmlFiles,
    unresolvedBefore: urls.length,
    fetched: false,
    tmpListPath: null,
  };

  if (urls.length === 0) {
    passes.push(summary);
    break;
  }

  summary.tmpListPath = fetchUrlBatch(urls);
  summary.fetched = true;

  execFileSync("node", [rewriteScript, mirrorDir], { stdio: "inherit" });
  execFileSync("node", [pruneScript, mirrorDir], { stdio: "inherit" });

  const after = scanUnresolvedUrls();
  summary.htmlFilesAfter = after.htmlFiles;
  summary.unresolvedAfter = after.unresolvedUrls.size;
  summary.delta = urls.length - after.unresolvedUrls.size;
  passes.push(summary);

  if (after.unresolvedUrls.size === 0) {
    break;
  }

  if (after.unresolvedUrls.size >= urls.length) {
    summary.stopped = "no_further_convergence";
    break;
  }
}

const finalScan = scanUnresolvedUrls();

console.log(
  JSON.stringify(
    {
      mirrorDir,
      maxPasses,
      passes,
      finalHtmlFiles: finalScan.htmlFiles,
      finalUnresolvedUrls: finalScan.unresolvedUrls.size,
      sampleRemainingUrls: [...finalScan.unresolvedUrls].sort().slice(0, 100),
    },
    null,
    2,
  ),
);
