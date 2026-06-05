#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";

const baseUrl = process.argv[2];

if (!baseUrl) {
  console.error(
    "Usage: node ./scripts/verify-mirror-release.mjs <base-url>",
  );
  process.exit(1);
}

const targetPages = [
  "/",
  "/contact-us.html",
  "/custom-mailers.html",
  "/packaging-by-industries.html",
  "/inspiration/l/display-style-filter_dump-bin-displays/product-type-filter_displays.1.dela",
  "/custom-bakery-packaging-boxes.html/index.html",
];

const pakfactoryDomainPattern =
  /https?:\/\/(?:www\.)?(?:pakfactory\.com|static\.pakfactory\.com|media\.pakfactory\.com)/i;

const assetPattern =
  /(?:src|href)=["']([^"']+\.(?:css|js|jpg|jpeg|png|gif|svg|webp|woff2?|ttf))(?:[?#][^"']*)?["']/gi;

function absoluteUrl(root, assetPath) {
  return new URL(assetPath, root).toString();
}

async function fetchText(url) {
  const response = await fetch(url, { redirect: "follow" });
  return {
    ok: response.ok,
    status: response.status,
    text: await response.text(),
    finalUrl: response.url,
  };
}

async function fetchHead(url) {
  const response = await fetch(url, {
    method: "HEAD",
    redirect: "follow",
  }).catch(async () => fetch(url, { method: "GET", redirect: "follow" }));

  return {
    ok: response.ok,
    status: response.status,
    finalUrl: response.url,
    contentType: response.headers.get("content-type"),
  };
}

function unique(values) {
  return [...new Set(values)];
}

const summary = {
  baseUrl,
  checkedAt: new Date().toISOString(),
  pages: [],
  totals: {
    pages: 0,
    pageFailures: 0,
    pakfactoryReferences: 0,
    assetChecks: 0,
    assetFailures: 0,
  },
};

for (const route of targetPages) {
  const pageUrl = absoluteUrl(baseUrl, route);
  const pageResult = {
    route,
    pageUrl,
    status: null,
    ok: false,
    finalUrl: null,
    pakfactoryReferences: [],
    assetFailures: [],
    assetChecks: 0,
  };

  try {
    const response = await fetchText(pageUrl);
    pageResult.status = response.status;
    pageResult.ok = response.ok;
    pageResult.finalUrl = response.finalUrl;

    if (!response.ok) {
      summary.totals.pageFailures += 1;
      summary.pages.push(pageResult);
      continue;
    }

    const pakfactoryMatches = response.text.match(
      /https?:\/\/(?:www\.)?(?:pakfactory\.com|static\.pakfactory\.com|media\.pakfactory\.com)[^"'\s<]*/gi,
    );

    pageResult.pakfactoryReferences = unique(pakfactoryMatches || []);
    summary.totals.pakfactoryReferences += pageResult.pakfactoryReferences.length;

    const assetMatches = [];
    let match;
    while ((match = assetPattern.exec(response.text)) !== null) {
      assetMatches.push(match[1]);
    }

    const candidateAssets = unique(
      assetMatches
        .filter((asset) => !asset.startsWith("data:"))
        .filter((asset) => !asset.includes("fonts.googleapis.com"))
        .filter((asset) => !asset.includes("fonts.gstatic.com"))
        .filter((asset) => !asset.includes("cdnjs.cloudflare.com"))
        .map((asset) => absoluteUrl(response.finalUrl || pageUrl, asset)),
    );

    pageResult.assetChecks = candidateAssets.length;
    summary.totals.assetChecks += candidateAssets.length;

    for (const assetUrl of candidateAssets) {
      if (pakfactoryDomainPattern.test(assetUrl)) {
        pageResult.pakfactoryReferences.push(assetUrl);
        continue;
      }

      const assetResponse = await fetchHead(assetUrl);
      if (!assetResponse.ok) {
        pageResult.assetFailures.push({
          assetUrl,
          status: assetResponse.status,
          finalUrl: assetResponse.finalUrl,
        });
      }
    }

    summary.totals.assetFailures += pageResult.assetFailures.length;
  } catch (error) {
    pageResult.ok = false;
    pageResult.error = error instanceof Error ? error.message : String(error);
    summary.totals.pageFailures += 1;
  }

  pageResult.pakfactoryReferences = unique(pageResult.pakfactoryReferences);
  summary.pages.push(pageResult);
}

summary.totals.pages = summary.pages.length;

const outputPath = path.join(
  process.cwd(),
  "docs",
  "mirror-release-verification.json",
);
fs.writeFileSync(outputPath, JSON.stringify(summary, null, 2));

console.log(JSON.stringify(summary, null, 2));
