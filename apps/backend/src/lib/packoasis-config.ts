import crypto from "node:crypto"

/** Runtime settings for the instant-quote / lead automation (all optional). */
export const packoasisConfig = {
  storefrontUrl: () =>
    (process.env.STOREFRONT_URL || "https://packoasis.com").replace(/\/$/, ""),
  backendUrl: () =>
    (process.env.MEDUSA_BACKEND_URL || "http://localhost:9000").replace(
      /\/$/,
      ""
    ),
  emailFrom: () => process.env.PACKOASIS_EMAIL_FROM || undefined,
  replyTo: () => process.env.PACKOASIS_REPLY_TO || "hello@packoasis.com",
  salesEmails: () =>
    (process.env.SALES_NOTIFY_EMAIL || "")
      .split(",")
      .map((email) => email.trim())
      .filter(Boolean),
  defaultCountry: () =>
    (process.env.INSTANT_QUOTE_DEFAULT_COUNTRY || "us").toLowerCase(),
  followupsEnabled: () => process.env.PACKOASIS_FOLLOWUPS_ENABLED !== "false",
  followupDelaysHours: () =>
    (process.env.PACKOASIS_FOLLOWUP_DELAYS_HOURS || "24,72")
      .split(",")
      .map((value) => Number(value.trim()))
      .filter((value) => Number.isFinite(value) && value > 0),
  enrichmentEnabled: () => process.env.PACKOASIS_ENRICHMENT_ENABLED !== "false",
  leadEventRetentionDays: () =>
    Number(process.env.PACKOASIS_LEAD_EVENT_RETENTION_DAYS || 180),
}

function signingSecret() {
  return (
    process.env.PACKOASIS_SIGNING_SECRET ||
    process.env.COOKIE_SECRET ||
    "supersecret"
  )
}

export function signToken(purpose: string, id: string) {
  return crypto
    .createHmac("sha256", signingSecret())
    .update(`${purpose}:${id}`)
    .digest("base64url")
    .slice(0, 32)
}

export function verifyToken(purpose: string, id: string, token: unknown) {
  if (typeof token !== "string" || !token) {
    return false
  }
  const expected = Buffer.from(signToken(purpose, id))
  const actual = Buffer.from(token)
  return (
    expected.length === actual.length &&
    crypto.timingSafeEqual(expected, actual)
  )
}

export function unsubscribeUrl(rfqId: string) {
  return `${packoasisConfig.backendUrl()}/packoasis/unsubscribe?rfq=${encodeURIComponent(
    rfqId
  )}&token=${signToken("unsubscribe", rfqId)}`
}

export function resumeQuoteUrl(rfqId: string) {
  return `${packoasisConfig.backendUrl()}/packoasis/resume?rfq=${encodeURIComponent(
    rfqId
  )}&token=${signToken("resume", rfqId)}`
}

/**
 * Storefront hand-off that attaches a quote cart to the buyer's browser. The
 * token lets the storefront check (via /packoasis/checkout-token) that the
 * link came from us, so nobody can plant their own cart in a victim's browser.
 */
export function storefrontCheckoutUrl(
  cartId: string,
  countryCode: string,
  origin?: string
) {
  const base = (origin || packoasisConfig.storefrontUrl()).replace(/\/$/, "")
  return `${base}/api/quote-checkout?cart_id=${encodeURIComponent(
    cartId
  )}&country=${encodeURIComponent(countryCode)}&token=${signToken(
    "checkout",
    cartId
  )}`
}
