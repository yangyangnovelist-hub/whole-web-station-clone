import type { MedusaRequest, MedusaResponse } from "@medusajs/framework/http"
import { escapeHtml } from "../../../lib/email/templates"
import { verifyToken } from "../../../lib/packoasis-config"
import { RFQ_MODULE } from "../../../modules/rfq"

async function optOut(req: MedusaRequest) {
  const rfqId = String((req.query as Record<string, unknown>).rfq ?? "")
  const token = (req.query as Record<string, unknown>).token
  if (!rfqId || !verifyToken("unsubscribe", rfqId, token)) {
    return false
  }
  const rfqService: any = req.scope.resolve(RFQ_MODULE)
  try {
    await rfqService.updateRFQS({ id: rfqId, contact_opt_out: true })
    return true
  } catch {
    return false
  }
}

function page(message: string) {
  return `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><meta name="robots" content="noindex"><title>PackOasis</title></head><body style="font-family:system-ui,sans-serif;max-width:480px;margin:80px auto;padding:0 16px;color:#1f2430;"><h1 style="font-size:22px;">PackOasis</h1><p>${escapeHtml(
    message
  )}</p></body></html>`
}

export async function GET(req: MedusaRequest, res: MedusaResponse) {
  const ok = await optOut(req)
  res
    .status(ok ? 200 : 400)
    .type("html")
    .send(
      page(
        ok
          ? "You will not receive more reminders about this quote. Your quote stays valid if you want to order later."
          : "This unsubscribe link is invalid or expired. Reply to any PackOasis email and we will remove you."
      )
    )
}

/** RFC 8058 one-click unsubscribe (List-Unsubscribe-Post). */
export async function POST(req: MedusaRequest, res: MedusaResponse) {
  const ok = await optOut(req)
  res.status(ok ? 200 : 400).json({ unsubscribed: ok })
}
