import type { MedusaRequest, MedusaResponse } from "@medusajs/framework/http"
import { packoasisConfig } from "../../../lib/packoasis-config"

/**
 * "Complete my order" links in emails sent before the link moved to the
 * storefront. Forwards them to the storefront's /api/quote-resume (see
 * resumeQuoteUrl), which checks the token and, once the buyer confirms, hands
 * them a checkout for the quote.
 */
export async function GET(req: MedusaRequest, res: MedusaResponse) {
  const query = req.query as Record<string, unknown>
  const target = new URL(`${packoasisConfig.storefrontUrl()}/api/quote-resume`)
  target.searchParams.set("rfq", typeof query.rfq === "string" ? query.rfq : "")
  target.searchParams.set(
    "token",
    typeof query.token === "string" ? query.token : ""
  )
  return res.redirect(302, target.toString())
}
