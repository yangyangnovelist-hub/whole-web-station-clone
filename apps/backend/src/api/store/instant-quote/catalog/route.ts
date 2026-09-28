import type { MedusaRequest, MedusaResponse } from "@medusajs/framework/http"
import { publicCatalog } from "../../../../lib/instant-quote/engine"
import { isAIEnabled } from "../../../../lib/ai/claude"

export async function GET(req: MedusaRequest, res: MedusaResponse) {
  res.setHeader("Cache-Control", "public, max-age=300")
  res.json({ catalog: publicCatalog(), ai_enabled: isAIEnabled() })
}
