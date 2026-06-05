#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";

const mirrorDirArg = process.argv[2];

if (!mirrorDirArg) {
  console.error("Usage: node ./scripts/rewrite-mirror-links.mjs <mirror-dir>");
  process.exit(1);
}

const mirrorDir = path.resolve(mirrorDirArg);
const cacheIndex = path.join(mirrorDir, "hts-cache", "new.txt");

if (!fs.existsSync(cacheIndex)) {
  console.error(`Missing HTTrack cache index: ${cacheIndex}`);
  process.exit(1);
}

const urlToLocalPath = new Map();
const predictedLocalPathCache = new Map();

function toPosix(relativePath) {
  return relativePath.split(path.sep).join("/");
}

function normalizeSiteUrl(rawUrl) {
  const cleanedUrl = rawUrl
    .replace(/&amp;/gi, "&")
    .replace(/([?&])amp(?:%3B|;)/gi, "$1")
    .replace(/([?&])amp=/gi, "$1");
  try {
    const parsed = new URL(cleanedUrl);
    if (parsed.origin !== "https://pakfactory.com") return null;
    return parsed;
  } catch {
    return null;
  }
}

function maybeAddMapping(rawUrl, rawLocalFile) {
  if (!rawUrl || !rawLocalFile) return;
  const parsed = normalizeSiteUrl(rawUrl);
  if (!parsed) return;

  const localFile = path.isAbsolute(rawLocalFile)
    ? rawLocalFile
    : path.resolve(mirrorDir, rawLocalFile);
  if (!localFile.startsWith(mirrorDir) || !fs.existsSync(localFile)) return;

  const relativeLocalFile = toPosix(path.relative(mirrorDir, localFile));
  const exactUrl = parsed.toString();
  urlToLocalPath.set(exactUrl, relativeLocalFile);

  // Also expose canonical variants for common trailing-slash redirects.
  const withoutHash = `${parsed.origin}${parsed.pathname}${parsed.search}`;
  urlToLocalPath.set(withoutHash, relativeLocalFile);

  if (parsed.pathname.endsWith("/")) {
    const withoutTrailingSlash = `${parsed.origin}${parsed.pathname.slice(0, -1)}${parsed.search}`;
    urlToLocalPath.set(withoutTrailingSlash, relativeLocalFile);
  } else if (parsed.pathname !== "/") {
    const withTrailingSlash = `${parsed.origin}${parsed.pathname}/${parsed.search}`;
    urlToLocalPath.set(withTrailingSlash, relativeLocalFile);
  }
}

function sanitizeSegment(segment) {
  return decodeURIComponent(segment).replace(/:/g, "_").replace(/\s+/g, "");
}

function findIndexLikeFile(dir) {
  if (!fs.existsSync(dir) || !fs.statSync(dir).isDirectory()) return null;

  const preferredNames = [
    "index.html",
    "inspiration.html",
  ];
  for (const name of preferredNames) {
    const fullPath = path.join(dir, name);
    if (fs.existsSync(fullPath) && fs.statSync(fullPath).isFile()) {
      return fullPath;
    }
  }

  const matches = fs
    .readdirSync(dir)
    .filter((name) => /^index-.*\.html$/i.test(name) || /^index\.[0-9a-f]+\.html$/i.test(name));
  if (matches.length >= 1) {
    return path.join(dir, matches.sort()[0]);
  }

  return null;
}

function looksLikeHtml(fullPath) {
  try {
    const sample = fs.readFileSync(fullPath, "utf8").slice(0, 2048).toLowerCase();
    return sample.includes("<!doctype html") || sample.includes("<html") || sample.includes("<head") || sample.includes("<body");
  } catch {
    return false;
  }
}

function guessLocalPathFromExistingFiles(parsedUrl) {
  const cacheKey = parsedUrl.toString();
  if (predictedLocalPathCache.has(cacheKey)) {
    return predictedLocalPathCache.get(cacheKey);
  }

  const rawSegments = parsedUrl.pathname.split("/").filter(Boolean);
  const segments = rawSegments.map(sanitizeSegment);
  let candidate = null;

  if (parsedUrl.pathname === "/") {
    const dir = path.join(mirrorDir, "pakfactory.com");
    const match = findIndexLikeFile(dir);
    if (match) {
      candidate = toPosix(path.relative(mirrorDir, match));
    }
  } else if (parsedUrl.pathname.endsWith("/")) {
    const dir = path.join(mirrorDir, "pakfactory.com", ...segments);
    const match = findIndexLikeFile(dir);
    if (match) {
      candidate = toPosix(path.relative(mirrorDir, match));
    }
  } else {
    const dirSegments = segments.slice(0, -1);
    const rawLeaf = rawSegments.at(-1) || "";
    const leaf = sanitizeSegment(rawLeaf);
    const parsedLeaf = path.parse(leaf);
    const basename = parsedLeaf.name || leaf;
    const ext = parsedLeaf.ext ? parsedLeaf.ext.replace(/^\./, "") : "html";
    const exactDirCandidates = [
      path.join(mirrorDir, "pakfactory.com", ...segments),
      path.join(mirrorDir, "pakfactory.com", ...dirSegments, basename),
    ];
    for (const exactDir of exactDirCandidates) {
      const exactDirIndex = findIndexLikeFile(exactDir);
      if (exactDirIndex) {
        candidate = toPosix(path.relative(mirrorDir, exactDirIndex));
        break;
      }
    }

    const dir = path.join(mirrorDir, "pakfactory.com", ...dirSegments);
    if (!candidate && fs.existsSync(dir) && fs.statSync(dir).isDirectory()) {
      const escapedBasename = basename.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      const names = fs.readdirSync(dir);

      const exactNames = [
        `${basename}.${ext}`,
        basename,
      ];
      for (const name of exactNames) {
        const fullPath = path.join(dir, name);
        if (fs.existsSync(fullPath) && fs.statSync(fullPath).isFile()) {
          candidate = toPosix(path.relative(mirrorDir, fullPath));
          break;
        }
      }

      if (!candidate) {
        const patterns = [
          new RegExp(`^${escapedBasename}-[0-9a-f]{32}\\.${ext}$`, "i"),
          new RegExp(`^${escapedBasename}[0-9a-f]{4}\\.${ext}$`, "i"),
          new RegExp(`^${escapedBasename}\\.[0-9a-f]+\\.del(?:aye?)?$`, "i"),
          new RegExp(`^${escapedBasename}.*\\.del(?:aye?)?$`, "i"),
          new RegExp(`^${escapedBasename}.*$`, "i"),
        ];
        for (const pattern of patterns) {
          const match = names.find((name) => {
            if (!pattern.test(name)) return false;
            const fullPath = path.join(dir, name);
            return fs.statSync(fullPath).isFile();
          });
          if (match) {
            candidate = toPosix(path.relative(mirrorDir, path.join(dir, match)));
            break;
          }
        }
      }
    }

    if (!candidate) {
      const ancestorDirs = [];
      for (let i = dirSegments.length - 1; i >= 0; i -= 1) {
        ancestorDirs.push(path.join(mirrorDir, "pakfactory.com", ...dirSegments.slice(0, i)));
      }

      for (const ancestorDir of ancestorDirs) {
        if (!fs.existsSync(ancestorDir) || !fs.statSync(ancestorDir).isDirectory()) continue;

        const nestedDirIndex = findIndexLikeFile(path.join(ancestorDir, basename));
        if (nestedDirIndex) {
          candidate = toPosix(path.relative(mirrorDir, nestedDirIndex));
          break;
        }

        const escapedBasename = basename.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
        const names = fs.readdirSync(ancestorDir);
        const exactNames = [
          `${basename}.${ext}`,
          basename,
        ];
        for (const name of exactNames) {
          const fullPath = path.join(ancestorDir, name);
          if (fs.existsSync(fullPath) && fs.statSync(fullPath).isFile()) {
            candidate = toPosix(path.relative(mirrorDir, fullPath));
            break;
          }
        }
        if (candidate) break;

        const patterns = [
          new RegExp(`^${escapedBasename}-[0-9a-f]{32}\\.${ext}$`, "i"),
          new RegExp(`^${escapedBasename}[0-9a-f]{4}\\.${ext}$`, "i"),
          new RegExp(`^${escapedBasename}\\.[0-9a-f]+\\.del(?:aye?)?$`, "i"),
          new RegExp(`^${escapedBasename}.*\\.del(?:aye?)?$`, "i"),
          new RegExp(`^${escapedBasename}.*$`, "i"),
        ];
        for (const pattern of patterns) {
          const match = names.find((name) => {
            if (!pattern.test(name)) return false;
            const fullPath = path.join(ancestorDir, name);
            return fs.statSync(fullPath).isFile();
          });
          if (match) {
            candidate = toPosix(path.relative(mirrorDir, path.join(ancestorDir, match)));
            break;
          }
        }
        if (candidate) break;
      }
    }
  }

  predictedLocalPathCache.set(cacheKey, candidate);
  return candidate;
}

const rawIndex = fs.readFileSync(cacheIndex, "utf8");
for (const line of rawIndex.split(/\r?\n/)) {
  if (!line.includes("\thttps://")) continue;
  const parts = line.split("\t");
  if (parts.length < 10) continue;
  const rawUrl = parts[7];
  const rawLocalFile = parts[8] || "";
  maybeAddMapping(rawUrl, rawLocalFile);
}

function resolveReplacement(currentHtmlPath, matchedUrl) {
  const parsed = normalizeSiteUrl(matchedUrl);
  if (!parsed) return null;

  const hash = parsed.hash || "";
  const normalized = `${parsed.origin}${parsed.pathname}${parsed.search}`;
  const candidates = [
    matchedUrl,
    normalized,
    `${parsed.origin}${parsed.pathname}`,
  ];

  if (parsed.pathname.endsWith("/")) {
    candidates.push(`${parsed.origin}${parsed.pathname.slice(0, -1)}${parsed.search}`);
    candidates.push(`${parsed.origin}${parsed.pathname.slice(0, -1)}`);
  } else if (parsed.pathname !== "/") {
    candidates.push(`${parsed.origin}${parsed.pathname}/${parsed.search}`);
    candidates.push(`${parsed.origin}${parsed.pathname}/`);
  }

  let targetRelativePath = null;
  for (const candidate of candidates) {
    if (urlToLocalPath.has(candidate)) {
      targetRelativePath = urlToLocalPath.get(candidate);
      break;
    }
  }

  if (!targetRelativePath) {
    targetRelativePath = guessLocalPathFromExistingFiles(parsed);
  }

  if (!targetRelativePath) return null;

  const currentDir = path.dirname(currentHtmlPath);
  const absoluteTarget = path.join(mirrorDir, targetRelativePath);
  let replacement = toPosix(path.relative(currentDir, absoluteTarget));
  if (!replacement) replacement = ".";
  if (hash) replacement += hash;
  return replacement;
}

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

const htmlFiles = [];
walk(mirrorDir, (fullPath) => {
  const ext = path.extname(fullPath).toLowerCase();
  if (ext === ".html" || ext === ".htm" || looksLikeHtml(fullPath)) {
    htmlFiles.push(fullPath);
  }
});

let filesTouched = 0;
let replacementsApplied = 0;

for (const htmlFile of htmlFiles) {
  const original = fs.readFileSync(htmlFile, "utf8");
  let changed = false;

  const rewritten = original.replace(/https:\/\/pakfactory\.com[^\s"'<>)]*/g, (matchedUrl) => {
    const replacement = resolveReplacement(htmlFile, matchedUrl);
    if (!replacement) return matchedUrl;
    if (replacement === matchedUrl) return matchedUrl;
    replacementsApplied += 1;
    changed = true;
    return replacement;
  });

  if (changed) {
    fs.writeFileSync(htmlFile, rewritten);
    filesTouched += 1;
  }
}

console.log(
  JSON.stringify(
    {
      mirrorDir,
      indexedUrls: urlToLocalPath.size,
      htmlFilesScanned: htmlFiles.length,
      filesTouched,
      replacementsApplied,
    },
    null,
    2,
  ),
);
