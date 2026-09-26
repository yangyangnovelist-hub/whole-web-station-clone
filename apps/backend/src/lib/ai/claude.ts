import Anthropic from "@anthropic-ai/sdk"
import { betaZodOutputFormat } from "@anthropic-ai/sdk/helpers/beta/zod"
import * as z from "zod/v4"

/**
 * Thin wrapper around the Claude Messages API for PackOasis automations.
 * Every caller has a deterministic fallback, so any failure here (no key,
 * refusal, timeout, rate limit) returns null instead of throwing.
 */

export const AI_MODEL = process.env.PACKOASIS_AI_MODEL || "claude-opus-5"

type Logger = {
  warn: (message: string) => void
  info?: (message: string) => void
}

let client: Anthropic | null | undefined

export function isAIEnabled() {
  return (
    process.env.PACKOASIS_AI_DISABLED !== "true" &&
    Boolean(process.env.ANTHROPIC_API_KEY || process.env.ANTHROPIC_AUTH_TOKEN)
  )
}

/** Test seam: inject a client (e.g. one with a mocked fetch). */
export function setClaudeClientForTests(next: Anthropic | null | undefined) {
  client = next
}

export function getClaudeClient(): Anthropic | null {
  if (!isAIEnabled()) {
    return null
  }
  if (client === undefined) {
    client = new Anthropic({ maxRetries: 1 })
  }
  return client
}

export async function generateStructured<Schema extends z.ZodType>(options: {
  schema: Schema
  system: string
  prompt: string
  effort?: "low" | "medium" | "high"
  maxTokens?: number
  timeoutMs?: number
  logger?: Logger
  label: string
}): Promise<z.infer<Schema> | null> {
  const claude = getClaudeClient()
  if (!claude) {
    return null
  }

  const log = options.logger ?? console
  try {
    const response = await claude.beta.messages.parse(
      {
        model: AI_MODEL,
        max_tokens: options.maxTokens ?? 8000,
        // Re-run declined requests on Anthropic's recommended fallback model.
        betas: ["server-side-fallback-2026-07-01"],
        fallbacks: "default",
        output_config: {
          effort: options.effort ?? "low",
          format: betaZodOutputFormat(options.schema),
        },
        system: options.system,
        messages: [{ role: "user", content: options.prompt }],
      },
      { timeout: options.timeoutMs ?? 30_000 }
    )

    if (response.stop_reason === "refusal") {
      log.warn(`[ai:${options.label}] request declined; using fallback`)
      return null
    }
    if (response.stop_reason === "max_tokens") {
      log.warn(`[ai:${options.label}] hit max_tokens; using fallback`)
      return null
    }

    return (response.parsed_output ?? null) as z.infer<Schema> | null
  } catch (error) {
    if (error instanceof Anthropic.AuthenticationError) {
      log.warn(
        `[ai:${options.label}] invalid ANTHROPIC_API_KEY; using fallback`
      )
    } else if (error instanceof Anthropic.RateLimitError) {
      log.warn(`[ai:${options.label}] rate limited; using fallback`)
    } else if (error instanceof Anthropic.APIConnectionTimeoutError) {
      log.warn(`[ai:${options.label}] timed out; using fallback`)
    } else if (error instanceof Anthropic.APIConnectionError) {
      log.warn(`[ai:${options.label}] connection error; using fallback`)
    } else if (error instanceof Anthropic.APIError) {
      log.warn(
        `[ai:${options.label}] API error ${error.status}: ${error.message}`
      )
    } else {
      log.warn(
        `[ai:${options.label}] failed: ${(error as Error)?.message ?? error}`
      )
    }
    return null
  }
}
