#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";

const defaultMirrorRoot =
  "captures/pakfactory.com/20260417-132610-stable/mirror/pakfactory.com";
const defaultOutputPath = "docs/pakfactory-route-inventory.json";

const mirrorRoot = path.resolve(process.argv[2] || defaultMirrorRoot);
const outputPath = path.resolve(process.argv[3] || defaultOutputPath);

if (!fs.existsSync(mirrorRoot)) {
  console.error(`Mirror root not found: ${mirrorRoot}`);
  process.exit(1);
}

function walk(dir, files = []) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const fullPath = path.join(dir, entry.name);

    if (entry.isDirectory()) {
      walk(fullPath, files);
      continue;
    }

    if (!/\.(html?|xht)$/i.test(entry.name)) continue;
    files.push(fullPath);
  }

  return files;
}

function normalizeRoute(relativePath) {
  let route = relativePath.replace(/\\/g, "/");
  route = route.replace(/\/index\.html?$/i, "/");
  route = route.replace(/^index\.html?$/i, "/");
  route = route.replace(/\/+/g, "/");
  route = route.replace(/\/$/, route === "/" ? "/" : "");

  if (!route.startsWith("/")) route = `/${route}`;
  return route;
}

function classifyRoute(route) {
  if (route === "/" || /^\/index-[^/]+\.html$/i.test(route)) {
    return {
      bucket: "home",
      migrationPhase: "phase-1-core",
      dynamic: false,
    };
  }

  if (
    route.startsWith("/contact") ||
    route.startsWith("/contact-us") ||
    route.startsWith("/quotation")
  ) {
    return {
      bucket: "inquiry",
      migrationPhase: "phase-1-core",
      dynamic: route.startsWith("/quotation"),
    };
  }

  if (route.startsWith("/customer/")) {
    return {
      bucket: "account",
      migrationPhase: "phase-2-auth",
      dynamic: true,
    };
  }

  if (route.startsWith("/checkout/")) {
    return {
      bucket: "checkout",
      migrationPhase: "phase-3-commerce",
      dynamic: true,
    };
  }

  if (route.startsWith("/catalog") || route.startsWith("/catalogsearch")) {
    return {
      bucket: "catalog",
      migrationPhase: "phase-2-listing",
      dynamic: route.startsWith("/catalogsearch"),
    };
  }

  if (route.startsWith("/inspiration")) {
    return {
      bucket: "inspiration",
      migrationPhase: "phase-3-long-tail",
      dynamic: false,
    };
  }

  if (
    route.startsWith("/custom-") ||
    route.startsWith("/folding-") ||
    route.startsWith("/reverse-") ||
    route.startsWith("/straight-") ||
    route.startsWith("/roll-") ||
    route.startsWith("/tuck-") ||
    route.startsWith("/paper-") ||
    route.startsWith("/printed-") ||
    route.startsWith("/hexagon") ||
    route.startsWith("/gable") ||
    route.startsWith("/cube-") ||
    route.startsWith("/bookend") ||
    route.startsWith("/auto-") ||
    route.startsWith("/4-") ||
    route.startsWith("/6-") ||
    route.startsWith("/flip-") ||
    route.startsWith("/full-") ||
    route.startsWith("/header-") ||
    route.startsWith("/prism-") ||
    route.startsWith("/side-") ||
    route.startsWith("/tab-") ||
    route.startsWith("/gusset-")
  ) {
    return {
      bucket: "product-detail",
      migrationPhase: "phase-2-listing",
      dynamic: false,
    };
  }

  if (
    route.startsWith("/about-us") ||
    route.startsWith("/careers") ||
    route.startsWith("/sustainability") ||
    route.startsWith("/why-") ||
    route.startsWith("/privacy-policy") ||
    route.startsWith("/terms-") ||
    route.startsWith("/sitemap") ||
    route.startsWith("/guide-") ||
    route.startsWith("/navigate-") ||
    route.startsWith("/packaging-") ||
    route.startsWith("/option-library")
  ) {
    return {
      bucket: "content",
      migrationPhase: "phase-3-long-tail",
      dynamic: false,
    };
  }

  return {
    bucket: "other",
    migrationPhase: "phase-4-legacy-fallback",
    dynamic: false,
  };
}

const routeEntries = walk(mirrorRoot)
  .map((fullPath) => {
    const relativePath = path.relative(mirrorRoot, fullPath);
    const route = normalizeRoute(relativePath);
    const { bucket, migrationPhase, dynamic } = classifyRoute(route);

    return {
      route,
      relativePath: relativePath.replace(/\\/g, "/"),
      bucket,
      migrationPhase,
      dynamic,
    };
  })
  .sort((left, right) => left.route.localeCompare(right.route));

const counts = {
  total: routeEntries.length,
  byBucket: {},
  byMigrationPhase: {},
  dynamicRoutes: routeEntries.filter((entry) => entry.dynamic).length,
};

for (const entry of routeEntries) {
  counts.byBucket[entry.bucket] = (counts.byBucket[entry.bucket] || 0) + 1;
  counts.byMigrationPhase[entry.migrationPhase] =
    (counts.byMigrationPhase[entry.migrationPhase] || 0) + 1;
}

const output = {
  generatedAt: new Date().toISOString(),
  mirrorRoot,
  counts,
  routes: routeEntries,
};

fs.mkdirSync(path.dirname(outputPath), { recursive: true });
fs.writeFileSync(outputPath, `${JSON.stringify(output, null, 2)}\n`);

console.log(`Wrote ${routeEntries.length} routes to ${outputPath}`);
