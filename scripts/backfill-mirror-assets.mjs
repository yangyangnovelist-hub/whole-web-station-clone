#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";

const publicDirArg = process.argv[2];

if (!publicDirArg) {
  console.error(
    "Usage: node ./scripts/backfill-mirror-assets.mjs <public-dir> [--fetch] [--filter <substring>] [--limit <count>]",
  );
  process.exit(1);
}

const publicDir = path.resolve(publicDirArg);
const args = process.argv.slice(3);
const shouldFetch = args.includes("--fetch");

let filter = "";
let limit = Infinity;

for (let i = 0; i < args.length; i += 1) {
  if (args[i] === "--filter") {
    filter = args[i + 1] || "";
    i += 1;
  } else if (args[i] === "--limit") {
    limit = Number.parseInt(args[i + 1] || "", 10);
    i += 1;
  }
}

const workspaceRoot = path.resolve(publicDir, "..", "..");
const captureRoots = [
  path.join(
    workspaceRoot,
    "captures/pakfactory.com/20260417-132610-stable/mirror",
  ),
  path.join(workspaceRoot, "captures/pakfactory.com/20260417-131959/mirror"),
];

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

const assetPathCounts = new Map();

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

function looksLikeTextCandidate(fullPath) {
  const ext = path.extname(fullPath).toLowerCase();

  if (binaryExts.has(ext)) {
    return false;
  }

  try {
    const text = fs.readFileSync(fullPath, "utf8");
    return !text.includes("\u0000");
  } catch {
    return false;
  }
}

function normalizeAssetPath(rawPath) {
  if (!rawPath) {
    return null;
  }

  let normalized = rawPath.trim();

  if (!normalized) {
    return null;
  }

  normalized = normalized.replace(/^https?:\/\/(?:www\.)?/i, "/");
  normalized = normalized.replace(/^\/\/(?:www\.)?/i, "/");

  if (!normalized.startsWith("/")) {
    return null;
  }

  const queryIndex = normalized.search(/[?#]/);
  if (queryIndex >= 0) {
    normalized = normalized.slice(0, queryIndex);
  }

  normalized = normalized.replace(/\\/g, "/");

  if (normalized.endsWith("/")) {
    return null;
  }

  if (
    !normalized.startsWith("/media.pakfactory.com/") &&
    !normalized.startsWith("/static.pakfactory.com/") &&
    !normalized.startsWith("/media.packoasis.com/") &&
    !normalized.startsWith("/static.packoasis.com/")
  ) {
    return null;
  }

  return normalized;
}

function extractAssetPaths(text) {
  const matches = text.match(
    /(?:https?:\/\/|\/\/|\/)(?:media|static)\.(?:pakfactory|packoasis)\.com\/[^"'()<>\s\\]+/gi,
  );

  if (!matches) {
    return [];
  }

  return matches
    .map(normalizeAssetPath)
    .filter(Boolean);
}

function toAbsolute(localAssetPath) {
  return path.join(publicDir, localAssetPath.slice(1));
}

function getRelativePathVariants(localAssetPath) {
  const relativePath = localAssetPath
    .replace(/^\/media\.(?:pakfactory|packoasis)\.com\//, "")
    .replace(/^\/static\.(?:pakfactory|packoasis)\.com\//, "");

  const variants = new Set([relativePath]);

  if (/packoasis/i.test(relativePath)) {
    variants.add(relativePath.replace(/packoasis/gi, "pakfactory"));
  }

  if (/PackOasis/.test(relativePath)) {
    variants.add(relativePath.replace(/PackOasis/g, "PakFactory"));
  }

  return [...variants];
}

function buildLocalAssetPathWithRelative(localAssetPath, relativePath) {
  if (localAssetPath.startsWith("/media.")) {
    const hostPrefix = localAssetPath.startsWith("/media.packoasis.com/")
      ? "/media.packoasis.com/"
      : "/media.pakfactory.com/";
    return `${hostPrefix}${relativePath}`;
  }

  const hostPrefix = localAssetPath.startsWith("/static.packoasis.com/")
    ? "/static.packoasis.com/"
    : "/static.pakfactory.com/";

  return `${hostPrefix}${relativePath}`;
}

function getAliasPaths(localAssetPath) {
  const aliases = new Set();
  const relativeVariants = getRelativePathVariants(localAssetPath);

  for (const relativePath of relativeVariants) {
    aliases.add(buildLocalAssetPathWithRelative(localAssetPath, relativePath));
  }

  if (localAssetPath.startsWith("/media.pakfactory.com/")) {
    for (const relativePath of relativeVariants) {
      aliases.add(`/media.packoasis.com/${relativePath}`);
    }
  } else if (localAssetPath.startsWith("/media.packoasis.com/")) {
    for (const relativePath of relativeVariants) {
      aliases.add(`/media.pakfactory.com/${relativePath}`);
    }
  } else if (localAssetPath.startsWith("/static.pakfactory.com/")) {
    for (const relativePath of relativeVariants) {
      aliases.add(`/static.packoasis.com/${relativePath}`);
    }
  } else if (localAssetPath.startsWith("/static.packoasis.com/")) {
    for (const relativePath of relativeVariants) {
      aliases.add(`/static.pakfactory.com/${relativePath}`);
    }
  }

  return [...aliases];
}

function ensureParentDir(targetPath) {
  fs.mkdirSync(path.dirname(targetPath), { recursive: true });
}

function copyIfMissing(sourcePath, targetPath) {
  if (!fs.existsSync(sourcePath) || fs.existsSync(targetPath)) {
    return false;
  }

  ensureParentDir(targetPath);
  fs.copyFileSync(sourcePath, targetPath);
  return true;
}

function tryCopyFromAliases(localAssetPath) {
  const absTargetPath = toAbsolute(localAssetPath);

  if (fs.existsSync(absTargetPath)) {
    return "existing";
  }

  for (const aliasPath of getAliasPaths(localAssetPath)) {
    if (aliasPath === localAssetPath) {
      continue;
    }

    const aliasAbsPath = toAbsolute(aliasPath);
    if (copyIfMissing(aliasAbsPath, absTargetPath)) {
      return "host_alias";
    }
  }

  return null;
}

function tryCopyFromVariant(localAssetPath) {
  const absTargetPath = toAbsolute(localAssetPath);

  if (fs.existsSync(absTargetPath)) {
    return "existing";
  }

  const directory = path.dirname(absTargetPath);
  if (!fs.existsSync(directory)) {
    return null;
  }

  const targetBase = path.basename(absTargetPath);
  const targetStem = targetBase.replace(/\.[^.]+$/, "");

  const candidates = fs
    .readdirSync(directory, { withFileTypes: true })
    .filter((entry) => entry.isFile())
    .map((entry) => entry.name)
    .filter((name) => name !== targetBase)
    .filter(
      (name) =>
        name.startsWith(`${targetStem}-`) ||
        name.startsWith(`${targetStem}.`),
    );

  if (candidates.length !== 1) {
    return null;
  }

  const candidateAbsPath = path.join(directory, candidates[0]);
  if (copyIfMissing(candidateAbsPath, absTargetPath)) {
    return "variant_alias";
  }

  return null;
}

function tryCopyFromCaptures(localAssetPath) {
  const absTargetPath = toAbsolute(localAssetPath);

  if (fs.existsSync(absTargetPath)) {
    return "existing";
  }

  for (const aliasPath of getAliasPaths(localAssetPath)) {
    const relativeAssetPath = aliasPath.slice(1);

    for (const captureRoot of captureRoots) {
      const sourcePath = path.join(captureRoot, relativeAssetPath);
      if (copyIfMissing(sourcePath, absTargetPath)) {
        return "capture";
      }
    }
  }

  return null;
}

async function tryFetchFromOrigin(localAssetPath) {
  if (!shouldFetch) {
    return null;
  }

  const absTargetPath = toAbsolute(localAssetPath);
  if (fs.existsSync(absTargetPath)) {
    return "existing";
  }

  const originCandidates = [];

  for (const relativePath of getRelativePathVariants(localAssetPath)) {
    if (localAssetPath.startsWith("/media.")) {
      originCandidates.push(`https://media.pakfactory.com/${relativePath}`);
    } else if (localAssetPath.startsWith("/static.")) {
      originCandidates.push(`https://static.pakfactory.com/${relativePath}`);
    }
  }

  for (const originUrl of originCandidates) {
    try {
      const response = await fetch(originUrl, { redirect: "follow" });
      if (!response.ok) {
        continue;
      }

      const arrayBuffer = await response.arrayBuffer();
      const fileBuffer = Buffer.from(arrayBuffer);

      ensureParentDir(absTargetPath);
      fs.writeFileSync(absTargetPath, fileBuffer);

      for (const aliasPath of getAliasPaths(localAssetPath)) {
        const aliasAbsPath = toAbsolute(aliasPath);
        if (!fs.existsSync(aliasAbsPath)) {
          ensureParentDir(aliasAbsPath);
          fs.writeFileSync(aliasAbsPath, fileBuffer);
        }
      }

      return "fetched";
    } catch {
      continue;
    }
  }

  return null;
}

walk(publicDir, (fullPath) => {
  if (!looksLikeTextCandidate(fullPath)) {
    return;
  }

  const text = fs.readFileSync(fullPath, "utf8");

  for (const assetPath of extractAssetPaths(text)) {
    if (filter && !assetPath.includes(filter)) {
      continue;
    }

    assetPathCounts.set(assetPath, (assetPathCounts.get(assetPath) || 0) + 1);
  }
});

const missingAssetPaths = [...assetPathCounts.entries()]
  .filter(([assetPath]) => !fs.existsSync(toAbsolute(assetPath)))
  .sort((left, right) => right[1] - left[1])
  .slice(0, Number.isFinite(limit) ? limit : undefined);

const summary = {
  publicDir,
  filter: filter || null,
  shouldFetch,
  assetReferences: assetPathCounts.size,
  missingAssets: missingAssetPaths.length,
  restored: {
    host_alias: 0,
    variant_alias: 0,
    capture: 0,
    fetched: 0,
  },
  stillMissing: [],
};

for (const [assetPath, count] of missingAssetPaths) {
  let restoredBy =
    tryCopyFromAliases(assetPath) ||
    tryCopyFromVariant(assetPath) ||
    tryCopyFromCaptures(assetPath) ||
    (await tryFetchFromOrigin(assetPath));

  if (fs.existsSync(toAbsolute(assetPath))) {
    continue;
  }

  if (!restoredBy) {
    summary.stillMissing.push({ assetPath, references: count });
    continue;
  }

  summary.restored[restoredBy] += 1;
}

console.log(JSON.stringify(summary, null, 2));
