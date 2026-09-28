import type { MedusaRequest, MedusaResponse } from "@medusajs/framework/http"
import { isAIEnabled } from "../../../lib/ai/claude"
import { publicCatalog } from "../../../lib/instant-quote/engine"
import { packoasisWidget } from "../../../lib/widget/widget-client"

/**
 * The embeddable instant-quote widget. The catalog is inlined so the widget
 * can price the first quote without an extra round trip.
 */
export async function GET(req: MedusaRequest, res: MedusaResponse) {
  const boot = JSON.stringify({ catalog: publicCatalog(), ai: isAIEnabled() })
  res.setHeader("Content-Type", "application/javascript; charset=utf-8")
  res.setHeader("Cache-Control", "public, max-age=300")
  res.setHeader("Access-Control-Allow-Origin", "*")
  res.send(
    `/* PackOasis instant quote widget */\n;(${packoasisWidget.toString()})(window, document, ${boot});\n`
  )
}
