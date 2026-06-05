#!/usr/bin/env node

import { spawnSync } from "node:child_process"
import path from "node:path"
import { fileURLToPath } from "node:url"

const __filename = fileURLToPath(import.meta.url)
const __dirname = path.dirname(__filename)
const backendRoot = path.resolve(__dirname, "..")

const email = process.env.MEDUSA_ADMIN_EMAIL || "packoasis@gmail.com"
const password = process.env.MEDUSA_ADMIN_PASSWORD

if (!password) {
  console.error(
    "Missing MEDUSA_ADMIN_PASSWORD. Set it in your shell or Railway service variables before running this script."
  )
  process.exit(1)
}

const result = spawnSync(
  "yarn",
  ["medusa", "user", "-e", email, "-p", password, "-i", "admin"],
  {
    cwd: backendRoot,
    stdio: "inherit",
    env: process.env,
  }
)

if (result.error) {
  console.error(result.error.message)
  process.exit(1)
}

process.exit(result.status ?? 0)
