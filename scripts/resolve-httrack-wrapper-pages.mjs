#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";

const publicDirArg = process.argv[2];

if (!publicDirArg) {
  console.error("Usage: node ./scripts/resolve-httrack-wrapper-pages.mjs <public-dir>");
  process.exit(1);
}

const publicDir = path.resolve(publicDirArg);

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

function isWrapperPage(text) {
  return text.includes("Page has moved") && text.includes("Click here...")
}

function extractTarget(text) {
  const match = text.match(/URL=([^">]+)/i)
  return match?.[1] || null
}

const result = {
  publicDir,
  resolved: [],
  missingTargets: [],
}

walk(publicDir, (fullPath) => {
  if (!fullPath.endsWith(".html")) {
    return
  }

  const text = fs.readFileSync(fullPath, "utf8")

  if (!isWrapperPage(text)) {
    return
  }

  const target = extractTarget(text)

  if (!target) {
    result.missingTargets.push({
      wrapper: path.relative(publicDir, fullPath),
      target: null,
    })
    return
  }

  const targetPath = path.resolve(path.dirname(fullPath), target)

  if (!targetPath.startsWith(publicDir) || !fs.existsSync(targetPath)) {
    result.missingTargets.push({
      wrapper: path.relative(publicDir, fullPath),
      target: path.relative(publicDir, targetPath),
    })
    return
  }

  fs.copyFileSync(targetPath, fullPath)
  result.resolved.push({
    wrapper: path.relative(publicDir, fullPath),
    target: path.relative(publicDir, targetPath),
  })
})

console.log(JSON.stringify(result, null, 2))
