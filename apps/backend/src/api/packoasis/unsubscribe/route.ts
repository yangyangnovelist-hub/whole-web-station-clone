import type { MedusaRequest, MedusaResponse } from "@medusajs/framework/http"
import { escapeHtml } from "../../../lib/email/templates"
import { verifyToken } from "../../../lib/packoasis-config"
import { RFQ_MODULE } from "../../../modules/rfq"

function linkParams(req: MedusaRequest) {
  const query = req.query as Record<string, unknown>
  const rfqId = String(query.rfq ?? "")
  const token = query.token
  return {
    rfqId,
    token,
    valid: Boolean(rfqId) && verifyToken("unsubscribe", rfqId, token),
  }
}

async function optOut(req: MedusaRequest) {
  const { rfqId, valid } = linkParams(req)
  if (!valid) {
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

function page(message: string, extra = "") {
  return `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><meta name="robots" content="noindex"><title>PackOasis</title></head><body style="font-family:system-ui,sans-serif;max-width:480px;margin:80px auto;padding:0 16px;color:#1f2430;"><h1 style="font-size:22px;">PackOasis</h1><p>${escapeHtml(
    message
  )}</p>${extra}</body></html>`
}

const INVALID_LINK =
  "This unsubscribe link is invalid or expired. Reply to any PackOasis email and we will remove you."

/**
 * Read-only confirmation page. Mail security gateways prefetch every link with
 * GET, so only the POST below (form button or RFC 8058 one-click) opts out.
 */
export async function GET(req: MedusaRequest, res: MedusaResponse) {
  const { rfqId, token, valid } = linkParams(req)
  if (!valid) {
    return res.status(400).type("html").send(page(INVALID_LINK))
  }
  const action = `/packoasis/unsubscribe?rfq=${encodeURIComponent(
    rfqId
  )}&token=${encodeURIComponent(String(token))}`
  res
    .status(200)
    .type("html")
    .send(
      page(
        "Stop reminder emails about this quote? Your quote stays valid if you want to order later.",
        `<form method="post" action="${escapeHtml(
          action
        )}"><button type="submit" style="font:inherit;padding:10px 18px;border:0;border-radius:8px;background:#1a7f45;color:#ffffff;cursor:pointer;">Stop quote reminders</button></form>`
      )
    )
}

/** Opts out: confirmation form submit or RFC 8058 one-click POST. */
export async function POST(req: MedusaRequest, res: MedusaResponse) {
  const ok = await optOut(req)
  res.status(ok ? 200 : 400)
  if (!req.get("accept")?.includes("text/html")) {
    return res.json({ unsubscribed: ok })
  }
  res
    .type("html")
    .send(
      page(
        ok
          ? "You will not receive more reminders about this quote. Your quote stays valid if you want to order later."
          : INVALID_LINK
      )
    )
}
