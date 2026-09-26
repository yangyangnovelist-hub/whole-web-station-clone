import type { MedusaRequest, MedusaResponse } from "@medusajs/framework/http"
import { publicCatalog } from "../../../lib/instant-quote/engine"

/** Keyless product catalog with starting prices (widget, AI agents, llms.txt). */
export async function GET(req: MedusaRequest, res: MedusaResponse) {
  res.setHeader("Access-Control-Allow-Origin", "*")
  res.setHeader("Cache-Control", "public, max-age=300")
  res.json({ catalog: publicCatalog() })
}
